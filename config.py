"""Load bot settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

import irinabot_api as api

load_dotenv()

APP_DIR = Path(__file__).resolve().parent
STATE_PATH = APP_DIR / "state.json"


def parse_channel_ids() -> list[int]:
    ids_raw = os.getenv("DISCORD_CHANNEL_IDS", "").strip()
    if not ids_raw:
        single = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        if not single:
            raise RuntimeError("DISCORD_CHANNEL_IDS (or DISCORD_CHANNEL_ID) is not set")
        ids_raw = single

    channel_ids: list[int] = []
    for part in ids_raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            channel_ids.append(int(part))

    if not channel_ids:
        raise RuntimeError("DISCORD_CHANNEL_IDS is empty")

    return channel_ids


@dataclass(frozen=True)
class Settings:
    discord_token: str
    channel_ids: list[int]
    map_id: int | None
    name_filter: str
    creator_user_id: str
    poll_interval_sec: float

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError("DISCORD_TOKEN is not set")

        name_filter = os.getenv("NAME_FILTER", api.DEFAULT_NAME_FILTER).strip()
        if not name_filter:
            raise RuntimeError("NAME_FILTER is not set")

        creator_user_id = os.getenv("CREATOR_USER_ID", "248324").strip()
        if not creator_user_id:
            raise RuntimeError("CREATOR_USER_ID is not set")

        map_raw = os.getenv("MAP_ID", "").strip()
        map_id = int(map_raw) if map_raw else None

        return cls(
            discord_token=token,
            channel_ids=parse_channel_ids(),
            map_id=map_id,
            name_filter=name_filter,
            creator_user_id=creator_user_id,
            poll_interval_sec=max(1.0, float(os.getenv("POLL_INTERVAL_SEC", "5"))),
        )
