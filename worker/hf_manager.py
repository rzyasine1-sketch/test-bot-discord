"""Swarm 2.0 v2 — Hugging Face Dataset Sync (Updated)"""
import os
import logging
from datetime import datetime
from typing import List, Dict, Optional
from datasets import Dataset, Features, Value, Image as DatasetImage
from huggingface_hub import HfApi
from config import Config
from database import Database

logger = logging.getLogger("ImageWorker")


class HFDatasetManager:
    """رفع Dataset إلى Hugging Face Hub — يتجاهل rejected"""

    def __init__(self, dataset_name: str, token: str):
        self.dataset_name = dataset_name
        self.token = token
        self.last_sync = datetime.min
        self.api = HfApi(token=token) if token else None

        if not token:
            logger.warning("HF_TOKEN missing. Upload disabled.")

    def _create_dataset(self, records: List[Dict]) -> Optional[Dataset]:
        # ← جديد: استبعاد الصور المرفوضة
        valid = [
            r for r in records 
            if r.get("file_path") 
            and os.path.exists(r["file_path"])
            and r.get("subcategory") != "rejected"
        ]
        if not valid:
            return None

        features = Features({
            "image": DatasetImage(),
            "url": Value("string"),
            "title": Value("string"),
            "category": Value("string"),
            "subcategory": Value("string"),      # ← جديد
            "source": Value("string"),
            "width": Value("int32"),
            "height": Value("int32"),
            "tags": Value("string"),
            "checksum": Value("string"),
            "phash": Value("string"),            # ← جديد
            "face_count": Value("int32"),        # ← جديد
            "pixel_price": Value("float32"),     # ← جديد
        })

        def _gen():
            for r in valid:
                yield {
                    "image": r["file_path"],
                    "url": r["url"],
                    "title": r["title"] or "",
                    "category": r["category"],
                    "subcategory": r.get("subcategory", ""),
                    "source": r["source"],
                    "width": r["width"] or 0,
                    "height": r["height"] or 0,
                    "tags": r["tags"] or "",
                    "checksum": r["checksum"] or "",
                    "phash": r.get("phash", ""),
                    "face_count": r.get("face_count", 0),
                    "pixel_price": r.get("pixel_price", 0.0),
                }

        return Dataset.from_generator(_gen, features=features)

    def sync(self, db: Database) -> bool:
        if not self.token:
            return False
        try:
            records = db.get_all_records()
            dataset = self._create_dataset(records)
            if not dataset:
                return False

            logger.info(f"Uploading {len(dataset)} images to {self.dataset_name}")
            dataset.push_to_hub(self.dataset_name, token=self.token, private=False)
            self.last_sync = datetime.now()
            logger.info("✓ HF Dataset synced")
            return True
        except Exception as e:
            logger.error(f"HF sync error: {e}")
            return False

    def should_sync(self, db: Database) -> bool:
        if not self.token:
            return False
        elapsed = (datetime.now() - self.last_sync).total_seconds()
        if elapsed < Config.HF_SYNC_INTERVAL:
            return False
        stats = db.get_stats()
        total_images = stats.get("total", 0)
        return total_images >= Config.HF_SYNC_MIN_IMAGES
