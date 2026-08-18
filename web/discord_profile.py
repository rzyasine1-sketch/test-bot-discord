#!/usr/bin/env python3
"""Fetch Discord user identity (avatar, username, display name) via the Bot token."""

import os
import logging
from typing import Dict, Optional

import requests

logger = logging.getLogger("DiscordProfile")
DISCORD_API_BASE = "https://discord.com/api/v10"


def discord_avatar_url(user_id: str, avatar_hash: Optional[str]) -> str:
    if avatar_hash:
        ext = "gif" if avatar_hash.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{ext}?size=128"
    try:
        index = (int(user_id) >> 22) % 6
    except ValueError:
        index = 0
    return f"https://cdn.discordapp.com/embed/avatars/{index}.png"


def fetch_discord_user(user_id: str) -> Optional[Dict[str, str]]:
    token = os.getenv("DISCORD_BOT_TOKEN", "")
    if not token or not user_id:
        return None

    try:
        response = requests.get(
            f"{DISCORD_API_BASE}/users/{user_id}",
            headers={"Authorization": f"Bot {token}"},
            timeout=8,
        )
        if response.status_code != 200:
            logger.warning("Discord user fetch failed (%s): %s", response.status_code, response.text[:200])
            return None
        data = response.json()
    except requests.RequestException as exc:
        logger.warning("Discord user fetch error: %s", exc)
        return None

    avatar_hash = data.get("avatar") or ""
    username = data.get("username") or ""
    display_name = data.get("global_name") or username
    return {
        "user_id": str(data.get("id") or user_id),
        "username": username,
        "display_name": display_name,
        "avatar_hash": avatar_hash,
        "avatar_url": discord_avatar_url(user_id, avatar_hash),
    }
