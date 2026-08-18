"""Swarm 2.0 v2 — Main Worker Orchestrator (Updated)"""
import sys
import time
import logging
from typing import List

# استيراد الإعدادات والجوالب بأسلوب موحد لتجنب أخطاء المسارات
from config import Config, HF_SOURCE_REPOS
from models import ImageMetadata
from database import Database
from validator import ImageValidator
from fetchers import (
    BaseFetcher, 
    SafebooruFetcher, 
    NasaApodFetcher, 
    UnsplashFetcher, 
    RedditFetcher,
    HuggingFaceFetcher
)
from hf_manager import HFDatasetManager

logger = logging.getLogger("ImageWorker")


class ImageWorker:
    """المنسق الرئيسي v2 — يمرر db_path للـ Validator"""

    def __init__(self):
        logger.info("=" * 60)
        logger.info("Swarm 2.0 Image Worker v2 Initializing...")
        logger.info("=" * 60)

        self.db = Database(Config.DB_PATH)
        # تمرير db_path للـ Validator ليتمكن من pHash dedup
        self.validator = ImageValidator(Config.DB_PATH)
        self.hf_manager = HFDatasetManager(Config.HF_DATASET_NAME, Config.HF_TOKEN)

        # قائمة الجوالب النشطة (Fetchers)
        self.fetchers: List[BaseFetcher] = []
        
        if getattr(Config, "SAFEBOORU_ENABLED", False):
            self.fetchers.append(SafebooruFetcher())
        if getattr(Config, "NASA_ENABLED", False):
            self.fetchers.append(NasaApodFetcher())
        if getattr(Config, "UNSPLASH_ENABLED", False) and getattr(Config, "UNSPLASH_ACCESS_KEY", None):
            self.fetchers.append(UnsplashFetcher())
        if getattr(Config, "REDDIT_ENABLED", False):
            self.fetchers.append(RedditFetcher())
            
        # 🟢 جديد: إضافة جالب HuggingFace تلقائياً إذا كانت المستودعات محددة
        if HF_SOURCE_REPOS:
            self.fetchers.append(HuggingFaceFetcher(repo_ids=HF_SOURCE_REPOS))

        if not self.fetchers:
            logger.warning("No fetchers active. Worker will exit gracefully without external network calls.")

        logger.info(f"Active: {[getattr(f, 'source_name', f.__class__.__name__) for f in self.fetchers]}")
        logger.info(f"DB: {Config.DB_PATH} | Images: {Config.IMAGES_DIR}")

    def _run_cycle(self):
        new_images = 0
        for fetcher in self.fetchers:
            try:
                source_name = getattr(fetcher, 'source_name', fetcher.__class__.__name__)
                logger.info(f"Fetching from {source_name}...")
                candidates = fetcher.fetch(Config.BATCH_SIZE)
                logger.info(f"  {source_name}: {len(candidates)} candidates")

                for meta in candidates:
                    logger.debug(f"[{source_name}] Validating: {meta.url[:80]}...")
                    validated = self.validator.validate(meta)
                    if validated is None:
                        logger.debug(f"[{source_name}] Validation returned None for: {meta.url[:80]}")
                    elif validated.subcategory != "rejected":
                        logger.debug(f"[{source_name}] Validation passed: {validated.subcategory}")
                        if self.db.insert(validated):
                            new_images += 1
                        else:
                            logger.warning(f"[{source_name}] Failed to insert validated image")
                    elif validated.subcategory == "rejected":
                        logger.debug(f"[{source_name}] Image rejected during validation")
                        # نحفظ المرفوضة في DB لكن لا نحسبها "جديدة"
                        self.db.insert(validated)
            except Exception as e:
                source_name = getattr(fetcher, 'source_name', fetcher.__class__.__name__)
                logger.error(f"{source_name} failed: {e}", exc_info=True)

        stats = self.db.get_stats()
        logger.info(f"Cycle done | New: {new_images} | Stats: {stats}")

        if self.hf_manager.should_sync(self.db):
            self.hf_manager.sync(self.db)
        return new_images

    def run(self):
        logger.info("Entering main loop...")
        if not self.fetchers:
            logger.info("No active fetchers configured; exiting cleanly.")
            return

        while True:
            try:
                # تدوير السحب على كل الجوالب (شاملة HuggingFace)
                self._run_cycle()
                if Config.RUN_ONCE:
                    logger.info("RUN_ONCE enabled. Exiting after single cycle.")
                    break
                logger.info(f"Sleeping {Config.SLEEP_INTERVAL}s...")
                time.sleep(Config.SLEEP_INTERVAL)
            except KeyboardInterrupt:
                logger.info("Shutdown signal received.")
                break
            except Exception as e:
                logger.error(f"Critical error: {e}")
                if Config.RUN_ONCE:
                    break
                time.sleep(Config.ERROR_SLEEP)

        logger.info("Final sync...")
        self.hf_manager.sync(self.db)
        logger.info("Worker stopped.")