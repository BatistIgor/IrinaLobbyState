"""IrInA Host Bot public API helpers."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

API_BASE = "https://apicf.irinabot.com/v1/games"
DEFAULT_MAP_ID = 73706
DEFAULT_NAME_FILTER = "ZemliBoga Re-Stored"
USER_AGENT = "discord-rv-bot/1.0"


@dataclass
class LobbyState:
    game_id: int
    name: str
    occupied: int
    total: int
    started: bool
    players: list[str]


@dataclass
class GameRow:
    game_id: int
    name: str
    occupied: int
    total: int
    started: bool
    players: list[str]


def count_occupied(game: dict[str, Any]) -> tuple[int, int]:
    slots = game.get("slots") or []
    occupied = sum(1 for slot in slots if slot.get("player"))
    return occupied, len(slots)


def extract_player_names(game: dict[str, Any]) -> list[str]:
    slots = game.get("slots") or []
    players: list[str] = []
    for slot in slots:
        player = slot.get("player")
        if not player:
            continue
        name = str(player.get("name") or "").strip()
        if name:
            players.append(name)
    return players


def game_to_row(game: dict[str, Any]) -> GameRow:
    occupied, total = count_occupied(game)
    return GameRow(
        game_id=int(game["id"]),
        name=str(game.get("name", "")),
        occupied=occupied,
        total=total,
        started=bool(game.get("started")),
        players=extract_player_names(game),
    )


def pick_open_lobby(games: list[dict[str, Any]]) -> LobbyState | None:
    candidates: list[LobbyState] = []
    for game in games:
        if game.get("started"):
            continue
        row = game_to_row(game)
        candidates.append(
            LobbyState(
                game_id=row.game_id,
                name=row.name,
                occupied=row.occupied,
                total=row.total,
                started=row.started,
                players=row.players,
            )
        )
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.occupied, item.game_id))


def list_games_for_map(games: list[dict[str, Any]]) -> list[GameRow]:
    rows = [game_to_row(game) for game in games]
    rows.sort(key=lambda row: (row.started, -row.occupied, row.name))
    return rows


def _filter_games(data: list[dict[str, Any]], name_contains: str | None) -> list[dict[str, Any]]:
    if not name_contains:
        return data
    needle = name_contains.lower()
    return [game for game in data if needle in game.get("name", "").lower()]


def filter_by_creator_user_id(
    games: list[dict[str, Any]],
    creator_user_id: str | None,
) -> list[dict[str, Any]]:
    if not creator_user_id:
        return games
    target = str(creator_user_id).strip()
    return [game for game in games if str(game.get("creatorUserId", "")) == target]


def fetch_games(map_id: int | None = None, name_contains: str | None = None) -> list[dict[str, Any]]:
    params: list[str] = []
    if map_id is not None:
        params.append(f"mapId={map_id}")
    if name_contains:
        params.append(f"name={urllib.parse.quote(name_contains)}")
    url = API_BASE + ("?" + "&".join(params) if params else "")
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    if not isinstance(data, list):
        raise ValueError(f"Unexpected API response: {type(data)}")
    return _filter_games(data, name_contains)


async def fetch_games_async(
    session: Any,
    map_id: int | None = None,
    name_contains: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, str | int] = {}
    if map_id is not None:
        params["mapId"] = map_id
    if name_contains:
        params["name"] = name_contains
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    async with session.get(API_BASE, params=params, headers=headers, timeout=20) as resp:
        resp.raise_for_status()
        data = await resp.json()
    if not isinstance(data, list):
        raise ValueError(f"Unexpected API response: {type(data)}")
    return _filter_games(data, name_contains)
