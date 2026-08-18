#!/usr/bin/env python3
"""
discord_gallery.py — Fetch images directly from Discord channels (Legendary Version).
No local image DB — live Discord API only.
"""

import os
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

logger = logging.getLogger("DiscordGallery")

DISCORD_API_BASE = "https://discord.com/api/v10"
IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

GALLERY_CATEGORIES: Dict[str, Dict[str, Any]] = {
    "rome1": {
        "label": "Female PFPs",
        "channel_env": "DISCORD_CHANNEL_ROME1",
        "layout": "avatar",
        "shape": "circle",
        "item_type": "avatar",
        "price": 500,
        "gif": False,
    },
    "rome2": {
        "label": "Male PFPs",
        "channel_env": "DISCORD_CHANNEL_ROME2",
        "layout": "avatar",
        "shape": "circle",
        "item_type": "avatar",
        "price": 500,
        "gif": False,
    },
    "rome3": {
        "label": "Female GIFs",
        "channel_env": "DISCORD_CHANNEL_ROME3",
        "layout": "avatar",
        "shape": "circle",
        "item_type": "avatar",
        "price": 750,
        "gif": True,
    },
    "rome4": {
        "label": "Male GIFs",
        "channel_env": "DISCORD_CHANNEL_ROME4",
        "layout": "avatar",
        "shape": "circle",
        "item_type": "avatar",
        "price": 750,
        "gif": True,
    },
    "rome5": {
        "label": "Banners",
        "channel_env": "DISCORD_CHANNEL_ROME5",
        "layout": "banner",
        "shape": "rectangle",
        "item_type": "banner",
        "price": 800,
        "gif": False,
    },
    "rome6": {
        "label": "Banner GIFs",
        "channel_env": "DISCORD_CHANNEL_ROME6",
        "layout": "banner",
        "shape": "rectangle",
        "item_type": "banner",
        "price": 1000,
        "gif": True,
    },
    "rome7": {
        "label": "Anime",
        "channel_env": "DISCORD_CHANNEL_ROME7",
        "layout": "avatar",
        "shape": "circle",
        "item_type": "avatar",
        "price": 600,
        "gif": False,
    },
    "rome8": {
        "label": "Manga",
        "channel_env": "DISCORD_CHANNEL_ROME8",
        "layout": "banner",
        "shape": "rectangle",
        "item_type": "banner",
        "price": 700,
        "gif": False,
    },
    "rome9": {
        "label": "Anime Banners",
        "channel_env": "DISCORD_CHANNEL_ROME9",
        "layout": "banner",
        "shape": "rectangle",
        "item_type": "banner",
        "price": 850,
        "gif": False,
        "coming_soon": True,
    },
}

TARGET_IMAGES = 100
MAX_FETCH_ROUNDS = 15


def get_category_list() -> List[Dict[str, Any]]:
    """Public category metadata for the frontend."""
    result = []
    for key, cfg in GALLERY_CATEGORIES.items():
        configured = bool(_get_channel_id(key))
        coming_soon = bool(cfg.get("coming_soon")) and not configured
        result.append({
            "id": key,
            "label": cfg["label"],
            "layout": cfg["layout"],
            "shape": cfg["shape"],
            "item_type": cfg["item_type"],
            "price": cfg["price"],
            "configured": configured,
            "coming_soon": coming_soon,
        })
    return result


def is_valid_category(category: str) -> bool:
    return category in GALLERY_CATEGORIES


def _get_bot_token() -> str:
    return os.getenv("DISCORD_BOT_TOKEN", "")


def _get_channel_id(category: str) -> Optional[str]:
    cfg = GALLERY_CATEGORIES.get(category)
    if not cfg:
        return None
    channel_id = os.getenv(cfg["channel_env"], "").strip()
    return channel_id or None


def _path_from_url(url: str) -> str:
    if not url:
        return ""
    return urlparse(url).path.lower()


def _is_gif_asset(url: str = "", content_type: str = "", filename: str = "") -> bool:
    """Detect GIFs even when Discord appends signed query params (?ex=&is=&hm=)."""
    ctype = (content_type or "").lower()
    if "gif" in ctype:
        return True
    name = (filename or "").lower()
    path = _path_from_url(url)
    blob = f"{name} {path} {url.lower()}"
    return ".gif" in path or name.endswith(".gif") or ".gif?" in blob


def _clean_discord_url(url: str, is_gif: bool = False) -> str:
    """
    Keep Discord signed CDN params (ex/is/hm) so the file actually loads,
    but strip proxy resize/format flags that freeze GIFs into a still frame.
    """
    if not url:
        return url

    parsed = urlparse(url)
    netloc = parsed.netloc
    qs = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if is_gif:
        if netloc.endswith("media.discordapp.net"):
            netloc = "cdn.discordapp.com"
        qs.pop("format", None)
        qs.pop("width", None)
        qs.pop("height", None)

    return urlunparse(parsed._replace(netloc=netloc, query=urlencode(qs)))


def _is_image_attachment(attachment: Dict[str, Any]) -> bool:
    content_type = (attachment.get("content_type") or "").lower()
    if content_type in IMAGE_CONTENT_TYPES:
        return True
    filename = (attachment.get("filename") or "").lower()
    return any(filename.endswith(ext) for ext in IMAGE_EXTENSIONS)


def _attachment_to_item(
    attachment: Dict[str, Any],
    message: Dict[str, Any],
    category: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    raw_url = attachment.get("url") or attachment.get("proxy_url", "")
    filename = attachment.get("filename", "")
    content_type = attachment.get("content_type") or ""
    is_gif = _is_gif_asset(raw_url, content_type, filename)
    url = _clean_discord_url(raw_url, is_gif=is_gif)
    proxy_url = _clean_discord_url(attachment.get("proxy_url") or url, is_gif=is_gif)
    display_url = url if is_gif else (proxy_url or url)
    return {
        "id": f"{message['id']}_{attachment.get('id', filename or '0')}",
        "message_id": message["id"],
        "url": url,
        "proxy_url": display_url,
        "filename": filename,
        "width": attachment.get("width") or 0,
        "height": attachment.get("height") or 0,
        "content_type": content_type or ("image/gif" if is_gif else "image/png"),
        "is_gif": is_gif,
        "layout": cfg["layout"],
        "shape": cfg["shape"],
        "item_type": cfg["item_type"],
        "price": cfg["price"],
        "category": category,
        "timestamp": message.get("timestamp"),
    }


def _embed_to_item(
    embed: Dict[str, Any],
    message: Dict[str, Any],
    category: str,
    cfg: Dict[str, Any],
    index: int,
) -> Optional[Dict[str, Any]]:
    image = embed.get("image") or embed.get("thumbnail")
    if not image or not image.get("url"):
        return None
    url = image["url"]
    filename = url.split("/")[-1].split("?")[0]
    is_gif = _is_gif_asset(url, "", filename)
    url = _clean_discord_url(url, is_gif=is_gif)
    return {
        "id": f"{message['id']}_embed_{index}",
        "message_id": message["id"],
        "url": url,
        "proxy_url": url,
        "filename": filename,
        "width": image.get("width") or 0,
        "height": image.get("height") or 0,
        "content_type": "image/gif" if is_gif else "image/png",
        "is_gif": is_gif,
        "layout": cfg["layout"],
        "shape": cfg["shape"],
        "item_type": cfg["item_type"],
        "price": cfg["price"],
        "category": category,
        "timestamp": message.get("timestamp"),
    }


def _extract_images_from_message(
    message: Dict[str, Any],
    category: str,
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    want_gif = cfg.get("gif")

    for attachment in message.get("attachments", []):
        if not _is_image_attachment(attachment):
            continue
        item = _attachment_to_item(attachment, message, category, cfg)
        if want_gif is True and not item["is_gif"]:
            continue
        if want_gif is False and item["is_gif"]:
            continue
        items.append(item)

    for idx, embed in enumerate(message.get("embeds", [])):
        item = _embed_to_item(embed, message, category, cfg, idx)
        if not item:
            continue
        if want_gif is True and not item["is_gif"]:
            continue
        if want_gif is False and item["is_gif"]:
            continue
        items.append(item)

    return items


def _fetch_messages(
    channel_id: str,
    token: str,
    before: Optional[str] = None,
    limit: int = 100,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    headers = {"Authorization": f"Bot {token}"}
    params: Dict[str, Any] = {"limit": min(limit, 100)}
    if before:
        params["before"] = before

    response = requests.get(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
        headers=headers,
        params=params,
        timeout=15,
    )

    if response.status_code == 404:
        raise ValueError("Discord channel not found. Check channel ID configuration.")
    if response.status_code == 403:
        raise ValueError("Bot lacks permission to read this channel.")
    if response.status_code == 401:
        raise ValueError("Invalid Discord bot token.")
    if response.status_code == 429:
        retry_after = response.json().get("retry_after", 5)
        raise ValueError(f"Discord rate limit hit. Retry in {retry_after}s.")

    response.raise_for_status()
    messages = response.json()
    if not messages:
        return [], before

    oldest_id = messages[-1]["id"]
    return messages, oldest_id


def fetch_gallery(
    category: str,
    before: Optional[str] = None,
    target: int = TARGET_IMAGES,
) -> Dict[str, Any]:
    """
    Fetch up to `target` images from a Discord channel.
    Uses Discord's `before` message ID for pagination.
    """
    if not is_valid_category(category):
        return {"success": False, "message": f"Unknown category: {category}"}

    channel_id = _get_channel_id(category)
    cfg = GALLERY_CATEGORIES[category]
    if not channel_id:
        if cfg.get("coming_soon"):
            return {
                "success": True,
                "category": category,
                "label": cfg["label"],
                "layout": cfg["layout"],
                "shape": cfg["shape"],
                "item_type": cfg["item_type"],
                "price": cfg["price"],
                "results": [],
                "count": 0,
                "has_more": False,
                "next_before": None,
                "coming_soon": True,
                "message": "Anime Banners is ready to wire — set DISCORD_CHANNEL_ROME9 when the banner bot is live.",
            }
        return {
            "success": False,
            "message": f"Channel not configured. Set {cfg['channel_env']} in .env",
        }

    token = _get_bot_token()
    if not token:
        return {"success": False, "message": "DISCORD_BOT_TOKEN is not configured."}

    cfg = GALLERY_CATEGORIES[category]
    items: List[Dict[str, Any]] = []
    cursor = before
    has_more = False
    next_before: Optional[str] = before

    for _ in range(MAX_FETCH_ROUNDS):
        if len(items) >= target:
            break

        try:
            messages, oldest_id = _fetch_messages(channel_id, token, before=cursor, limit=100)
        except requests.RequestException as exc:
            logger.error("Discord API request failed: %s", exc)
            return {"success": False, "message": "Failed to reach Discord API."}
        except ValueError as exc:
            return {"success": False, "message": str(exc)}

        if not messages:
            has_more = False
            break

        for message in messages:
            items.extend(_extract_images_from_message(message, category, cfg))
            if len(items) >= target:
                break

        next_before = oldest_id
        cursor = oldest_id
        has_more = len(messages) == 100

        if len(messages) < 100:
            has_more = False
            break

    return {
        "success": True,
        "category": category,
        "label": cfg["label"],
        "layout": cfg["layout"],
        "shape": cfg["shape"],
        "item_type": cfg["item_type"],
        "price": cfg["price"],
        "results": items[:target],
        "count": len(items[:target]),
        "has_more": has_more,
        "next_before": next_before if has_more else None,
    }
