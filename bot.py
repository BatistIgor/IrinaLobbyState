"""Discord bot that broadcasts IrInA lobby state in a single live-updating message."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks

import irinabot_api as api
from config import STATE_PATH, Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("discord-rv-bot")


def progress_bar(occupied: int, total: int) -> str:
    total = max(total, 1)
    filled = round(occupied / total * 10)
    return "█" * filled + "░" * (10 - filled)


def format_games_table(rows: list[api.GameRow]) -> str:
    if not rows:
        return "Нет подходящих игр"
    lines: list[str] = []
    for row in rows[:12]:
        status = "▶ Игра" if row.started else "● Лобби"
        lines.append(f"{status} **{row.occupied}/{row.total}** — {row.name}")
    if len(rows) > 12:
        lines.append(f"_…ещё {len(rows) - 12}_")
    return "\n".join(lines)


def build_embed(
    lobby: api.LobbyState | None,
    rows: list[api.GameRow],
    *,
    error: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(title="IrInA — монитор лобби")

    if error:
        embed.description = "⚠️ Ошибка при запросе API"
        embed.add_field(name="Детали", value=error[:1000], inline=False)
        embed.color = discord.Color.red()
        embed.add_field(name="Подходящие игры", value=format_games_table(rows), inline=False)
        return embed

    if lobby is None:
        embed.description = "**Нет открытого лобби**\nЖдём новое лобби…"
        embed.color = discord.Color.light_grey()
        embed.add_field(name="Подходящие игры", value=format_games_table(rows), inline=False)
        return embed

    embed.description = (
        f"### {lobby.name}\n"
        f"# {lobby.occupied} / {lobby.total}\n"
        f"`{progress_bar(lobby.occupied, lobby.total)}`"
    )
    embed.color = discord.Color.blurple()

    players_text = "\n".join(f"• {name}" for name in lobby.players) if lobby.players else "_(пусто)_"
    embed.add_field(name="Игроки в лобби", value=players_text, inline=False)
    embed.add_field(name="Подходящие игры", value=format_games_table(rows), inline=False)
    return embed


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class MonitorState:
    message_ids: dict[int, int] = field(default_factory=dict)
    last_game_id: int | None = None
    prev_players: set[str] = field(default_factory=set)
    prev_occupied: int | None = None
    had_open_lobby: bool = False

    @classmethod
    def from_disk(cls, channel_ids: list[int]) -> MonitorState:
        raw = load_state()
        message_ids = {int(channel_id): int(message_id) for channel_id, message_id in raw.get("message_ids", {}).items()}
        legacy_message_id = raw.get("message_id")
        if legacy_message_id and len(channel_ids) == 1:
            message_ids.setdefault(channel_ids[0], int(legacy_message_id))
        return cls(message_ids=message_ids)


def save_message_ids(message_ids: dict[int, int]) -> None:
    state = load_state()
    state["message_ids"] = {str(channel_id): message_id for channel_id, message_id in message_ids.items()}
    state.pop("message_id", None)
    save_state(state)


async def start_render_health_server() -> web.AppRunner | None:
    port_raw = os.getenv("PORT")
    if not port_raw:
        return None

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port_raw)).start()
    log.info("Health server listening on port %s (Render)", port_raw)
    return runner


class LobbyBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.monitor = MonitorState.from_disk(settings.channel_ids)
        self._http_session: aiohttp.ClientSession | None = None
        self._health_runner: web.AppRunner | None = None
        self._skipped_channels: set[int] = set()

    async def setup_hook(self) -> None:
        self._http_session = aiohttp.ClientSession()
        self._health_runner = await start_render_health_server()
        self.poll_lobbies.start()

    async def close(self) -> None:
        self.poll_lobbies.cancel()
        if self._health_runner is not None:
            await self._health_runner.cleanup()
        if self._http_session is not None:
            await self._http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    async def fetch_snapshot(self) -> tuple[api.LobbyState | None, list[api.GameRow], str | None]:
        assert self._http_session is not None
        try:
            games = await api.fetch_games_async(
                self._http_session,
                map_id=self.settings.map_id,
                name_contains=self.settings.name_filter,
            )
            games = api.filter_by_creator_user_id(games, self.settings.creator_user_id)
            lobby = api.pick_open_lobby(games)
            rows = api.list_games_for_map(games)
            return lobby, rows, None
        except Exception as exc:  # noqa: BLE001
            return None, [], str(exc)

    def detect_events(
        self,
        lobby: api.LobbyState | None,
        rows: list[api.GameRow],
    ) -> list[str]:
        events: list[str] = []
        if self.monitor.had_open_lobby and lobby is None and any(row.started for row in rows):
            events.append("Игра началась")

        if lobby is not None:
            current_players = set(lobby.players)
            if self.monitor.last_game_id == lobby.game_id:
                joined = sorted(current_players - self.monitor.prev_players)
                left = sorted(self.monitor.prev_players - current_players)
                if joined:
                    events.append(f"Зашёл: {', '.join(joined[:5])}")
                if left:
                    events.append(f"Вышел: {', '.join(left[:5])}")
                if self.monitor.prev_occupied is not None and lobby.occupied < self.monitor.prev_occupied:
                    events.append("Игроков стало меньше")
            self.monitor.prev_players = current_players
            self.monitor.prev_occupied = lobby.occupied
            self.monitor.last_game_id = lobby.game_id
        else:
            self.monitor.prev_players = set()
            self.monitor.prev_occupied = None
            self.monitor.last_game_id = None

        self.monitor.had_open_lobby = lobby is not None
        return events

    async def get_text_channel(self, channel_id: int) -> discord.TextChannel | None:
        channel = self.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await self.fetch_channel(channel_id)
        except discord.DiscordException:
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def get_or_create_status_message(self, channel: discord.TextChannel) -> discord.Message | None:
        message_id = self.monitor.message_ids.get(channel.id)
        if message_id is not None:
            try:
                return await channel.fetch_message(message_id)
            except discord.NotFound:
                log.warning("Status message %s not found in channel %s, creating a new one", message_id, channel.id)
                self.monitor.message_ids.pop(channel.id, None)
            except discord.Forbidden:
                log.error("No permission to read messages in channel %s", channel.id)
                return None

        placeholder = build_embed(None, [])
        try:
            message = await channel.send(embed=placeholder)
        except discord.Forbidden:
            log.error("No permission to send messages in channel %s", channel.id)
            return None

        self.monitor.message_ids[channel.id] = message.id
        save_message_ids(self.monitor.message_ids)
        return message

    async def update_channel(self, channel_id: int, embed: discord.Embed) -> None:
        channel = await self.get_text_channel(channel_id)
        if channel is None:
            if channel_id not in self._skipped_channels:
                log.warning(
                    "Channel %s skipped — bot is not on that server or has no access",
                    channel_id,
                )
                self._skipped_channels.add(channel_id)
            return

        if channel_id in self._skipped_channels:
            log.info("Channel %s is available again", channel_id)
            self._skipped_channels.discard(channel_id)

        message = await self.get_or_create_status_message(channel)
        if message is None:
            return

        try:
            await message.edit(embed=embed)
        except discord.Forbidden:
            log.error("No permission to edit messages in channel %s", channel.id)
        except discord.HTTPException as exc:
            log.error("Failed to edit status message in channel %s: %s", channel.id, exc)

    async def update_status_messages(self) -> None:
        lobby, rows, error = await self.fetch_snapshot()
        events = self.detect_events(lobby, rows)
        embed = build_embed(lobby, rows, error=error)
        if events:
            embed.add_field(name="Последнее событие", value=" | ".join(events), inline=False)

        for channel_id in self.settings.channel_ids:
            await self.update_channel(channel_id, embed)

    @tasks.loop(seconds=5)
    async def poll_lobbies(self) -> None:
        await self.update_status_messages()

    @poll_lobbies.before_loop
    async def before_poll(self) -> None:
        await self.wait_until_ready()
        self.poll_lobbies.change_interval(seconds=self.settings.poll_interval_sec)
        log.info(
            "Monitoring name=%r host=%s mapId=%s every %.1fs in %d channel(s): %s",
            self.settings.name_filter,
            self.settings.creator_user_id,
            self.settings.map_id if self.settings.map_id is not None else "any",
            self.settings.poll_interval_sec,
            len(self.settings.channel_ids),
            ", ".join(str(channel_id) for channel_id in self.settings.channel_ids),
        )


def main() -> None:
    settings = Settings.from_env()
    bot = LobbyBot(settings)
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
