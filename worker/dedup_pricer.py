"""Swarm 2.0 v2 — pHash Deduplication & Pricing (Data Agent)"""
import imagehash
from PIL import Image
from io import BytesIO
from typing import Optional
import sqlite3
import logging

logger = logging.getLogger("ImageWorker")


class Deduplicator:
    """منع التكرار باستخدام Perceptual Hash (pHash)"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_phash_table()

    def _init_phash_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS image_hashes (
                    phash TEXT PRIMARY KEY,
                    url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def compute_phash(self, image_data: bytes) -> Optional[str]:
        try:
            img = Image.open(BytesIO(image_data))
            phash = str(imagehash.phash(img))
            return phash
        except Exception as e:
            logger.debug(f"pHash compute failed: {e}")
            return None

    def is_duplicate(self, phash: str) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT 1 FROM image_hashes WHERE phash = ?", (phash,))
                return cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"pHash check failed: {e}")
            return False

    def store_phash(self, phash: str, url: str):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("INSERT OR IGNORE INTO image_hashes (phash, url) VALUES (?, ?)", (phash, url))
                conn.commit()
        except Exception as e:
            logger.error(f"pHash store failed: {e}")


class PixelPricer:
    """حساب "سعر" الصورة بناءً على عدد البكسلات"""

    @staticmethod
    def calculate(width: int, height: int) -> float:
        """
        السعر = (العرض × الارتفاع) / 1,000,000
        مثال: 1920×1080 = 2.07 MP → $2.07
        """
        return round((width * height) / 1_000_000, 2)
