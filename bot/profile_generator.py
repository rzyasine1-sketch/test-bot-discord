#!/usr/bin/env python3
"""
Generate rarity-based profile cards for Discord users.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, Tuple

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "assets" / "profile_templates"
CARD_SIZE = (1280, 720)
AVATAR_SIZE = 220


RARITY_TIERS = [
    {
        "key": "common",
        "label": "Common",
        "tier": 1,
        "min_score": None,
        "max_score": 2,
        "filename": "common_template.png",
        "accent": "#94a3b8",
        "bg_start": "#1f2937",
        "bg_end": "#0f172a",
    },
    {
        "key": "rare",
        "label": "Rare",
        "tier": 2,
        "min_score": 3,
        "max_score": 5,
        "filename": "rare_template.png",
        "accent": "#38bdf8",
        "bg_start": "#0f3b63",
        "bg_end": "#071827",
    },
    {
        "key": "epic",
        "label": "Epic",
        "tier": 3,
        "min_score": 6,
        "max_score": 9,
        "filename": "epic_template.png",
        "accent": "#a855f7",
        "bg_start": "#4c1d95",
        "bg_end": "#14071f",
    },
    {
        "key": "mythic",
        "label": "Mythic",
        "tier": 4,
        "min_score": 10,
        "max_score": None,
        "filename": "mythic_template.png",
        "accent": "#f59e0b",
        "bg_start": "#78350f",
        "bg_end": "#1a0f04",
    },
]


def get_rarity_tier(net_score: int) -> Dict[str, Any]:
    if net_score >= 10:
        return dict(RARITY_TIERS[3])
    if net_score >= 6:
        return dict(RARITY_TIERS[2])
    if net_score >= 3:
        return dict(RARITY_TIERS[1])
    return dict(RARITY_TIERS[0])


def ensure_templates_exist() -> None:
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    for tier in RARITY_TIERS:
        path = TEMPLATE_DIR / tier["filename"]
        if path.exists():
            continue
        image = _build_template(CARD_SIZE, tier["bg_start"], tier["bg_end"], tier["accent"], tier["label"])
        image.save(path, "PNG")


def generate_profile_card(payload: Dict[str, Any]) -> Tuple[io.BytesIO, Dict[str, Any]]:
    ensure_templates_exist()

    likes = int(payload.get("likes", 0) or 0)
    dislikes = int(payload.get("dislikes", 0) or 0)
    net_score = likes - dislikes
    tier = get_rarity_tier(net_score)

    template = Image.open(TEMPLATE_DIR / tier["filename"]).convert("RGBA")
    draw = ImageDraw.Draw(template)

    display_name = payload.get("display_name") or payload.get("username") or payload.get("user_id", "Member")
    username = payload.get("username") or "unknown"
    bio = (payload.get("bio") or "No bio set yet. Update your profile on the dashboard.").strip()
    if len(bio) > 120:
        bio = bio[:117].rstrip() + "..."

    title_font = _get_font(60, bold=True)
    body_font = _get_font(28)
    label_font = _get_font(24, bold=True)
    stat_font = _get_font(34, bold=True)

    avatar = _load_avatar(payload.get("avatar_url") or payload.get("active_avatar_url") or "")
    avatar = ImageOps.fit(avatar, (AVATAR_SIZE, AVATAR_SIZE))
    avatar_mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    ImageDraw.Draw(avatar_mask).ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)
    template.paste(avatar, (70, 90), avatar_mask)

    draw.text((330, 110), display_name, fill="white", font=title_font)
    draw.text((333, 182), f"@{username}", fill="#cbd5e1", font=body_font)

    badge_text = f"{tier['label']} · Tier {tier['tier']}"
    badge_box = (330, 235, 620, 285)
    draw.rounded_rectangle(badge_box, radius=18, fill=tier["accent"])
    draw.text((350, 245), badge_text, fill="#0b1020", font=label_font)

    draw.multiline_text(
        (70, 360),
        bio,
        fill="#f8fafc",
        font=body_font,
        spacing=8,
    )

    stats = [
        ("Coins", f"{int(payload.get('coins', 0) or 0):,}"),
        ("XP", f"{int(payload.get('xp', 0) or 0):,}"),
        ("Likes", str(likes)),
        ("Dislikes", str(dislikes)),
        ("Net Score", str(net_score)),
    ]
    start_x = 760
    start_y = 120
    card_w = 200
    card_h = 110
    gap = 24
    for idx, (label, value) in enumerate(stats):
        row = idx // 2
        col = idx % 2
        x = start_x + (card_w + gap) * col
        y = start_y + (card_h + gap) * row
        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=24, fill=(9, 14, 29, 180), outline=tier["accent"], width=3)
        draw.text((x + 20, y + 18), label, fill="#cbd5e1", font=label_font)
        draw.text((x + 20, y + 52), value, fill="white", font=stat_font)

    footer = f"Community Rated Profile · {tier['label']} rarity"
    draw.text((70, 650), footer, fill="#cbd5e1", font=label_font)

    output = io.BytesIO()
    template.save(output, format="PNG")
    output.seek(0)

    meta = {
        "tier_key": tier["key"],
        "tier_label": tier["label"],
        "tier": tier["tier"],
        "net_score": net_score,
        "template_path": str(TEMPLATE_DIR / tier["filename"]),
    }
    return output, meta


def _load_avatar(url: str) -> Image.Image:
    if url:
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return Image.open(io.BytesIO(response.content)).convert("RGBA")
        except Exception:
            pass
    image = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), "#1e293b")
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, AVATAR_SIZE - 8, AVATAR_SIZE - 8), fill="#334155")
    draw.text((AVATAR_SIZE // 2 - 18, AVATAR_SIZE // 2 - 30), "?", fill="white", font=_get_font(72, bold=True))
    return image


def _build_template(size: Tuple[int, int], start_hex: str, end_hex: str, accent_hex: str, title: str) -> Image.Image:
    width, height = size
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    start = _hex_to_rgb(start_hex)
    end = _hex_to_rgb(end_hex)
    accent = _hex_to_rgb(accent_hex)

    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = tuple(int(start[i] + (end[i] - start[i]) * ratio) for i in range(3))
        draw.line((0, y, width, y), fill=color, width=1)

    draw.rounded_rectangle((40, 40, width - 40, height - 40), radius=32, outline=accent, width=6)
    draw.rounded_rectangle((55, 55, width - 55, height - 55), radius=28, outline=(255, 255, 255, 32), width=2)
    draw.text((70, 55), title.upper(), fill=accent, font=_get_font(26, bold=True))
    return image


def _hex_to_rgb(value: str) -> Tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = []
    if bold:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            ]
        )
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()
