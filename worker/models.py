"""Swarm 2.0 v2 — Data Models (Updated)"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ImageMetadata:
    """العقد الموحد — يدعم التصنيف الفرعي والوجوه والتسعير"""
    url: str
    title: str
    category: str
    source: str
    width: Optional[int] = None
    height: Optional[int] = None
    tags: Optional[str] = None
    file_path: Optional[str] = None
    checksum: Optional[str] = None
    # ← حقول جديدة v2
    subcategory: Optional[str] = None      # anime_girls, anime_boys, anime_couples, anime_scenery, rejected
    phash: Optional[str] = None            # pHash للصورة
    face_count: int = 0                    # عدد الوجوه المكتشفة
    pixel_price: float = 0.0               # السعر حسب البكسلات
    is_nsfw: int = 0                       # 1 = NSFW, 0 = آمن
    is_loli: int = 0                       # 1 = Loli, 0 = آمن
    hf_path: Optional[str] = None
    hf_url: Optional[str] = None
