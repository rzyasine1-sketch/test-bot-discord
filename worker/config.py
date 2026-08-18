"""Swarm 2.0 central configuration.

This module holds environment-driven settings, image storage paths, and the vetting
list of Hugging Face repository sources used by the image fetching pipeline.
"""

import logging
import os
from typing import Set


class Config:
    """Central runtime configuration for the bot and worker pipeline."""

    @staticmethod
    def _resolve_path(value: str) -> str:
        if not value:
            return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
        if os.path.isabs(value):
            return value
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", value))

    # Database and storage
    DB_PATH = _resolve_path(os.getenv("DB_PATH", "./data/images.db"))
    IMAGES_DIR = _resolve_path(os.getenv("IMAGES_DIR", "./data/images"))
    OUTPUT_IMAGES_DIR = _resolve_path(os.getenv("OUTPUT_IMAGES_DIR", "./output_images"))
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(IMAGES_DIR, exist_ok=True)
    os.makedirs(OUTPUT_IMAGES_DIR, exist_ok=True)

    # Hugging Face
    HF_TOKEN = os.getenv("HF_TOKEN", "")
    HF_DATASET_NAME = os.getenv("HF_DATASET_NAME", "ziroze2/shop-image")
    HF_UPLOAD_TARGET = os.getenv("HF_UPLOAD_TARGET", "ziroze2/shop-image")
    HF_REPO_TYPE = os.getenv("HF_REPO_TYPE", "dataset")
    HF_UPLOAD_ENABLED = bool(HF_TOKEN)
    HF_SYNC_INTERVAL = int(os.getenv("HF_SYNC_INTERVAL", "3600"))
    HF_SYNC_MIN_IMAGES = int(os.getenv("HF_SYNC_MIN_IMAGES", "10"))

    # Fetching and scanning
    RUN_ONCE = os.getenv("RUN_ONCE", "false").lower() == "true"
    BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
    SLEEP_INTERVAL = int(os.getenv("SLEEP_INTERVAL", "300"))
    ERROR_SLEEP = int(os.getenv("ERROR_SLEEP", "60"))
    MIN_WIDTH = int(os.getenv("MIN_WIDTH", "128"))
    MIN_HEIGHT = int(os.getenv("MIN_HEIGHT", "128"))
    HF_FETCH_LIMIT_PER_REPO = int(os.getenv("HF_FETCH_LIMIT_PER_REPO", "5"))
    MAX_HTTP_RETRIES = int(os.getenv("MAX_HTTP_RETRIES", "3"))
    HTTP_RETRY_DELAY = int(os.getenv("HTTP_RETRY_DELAY", "2"))

    # External source enablement flags
    # Keep external integrations disabled unless explicitly enabled with real credentials.
    SAFEBOORU_ENABLED = os.getenv("SAFEBOORU_ENABLED", "false").lower() == "true"
    NASA_API_KEY = os.getenv("NASA_API_KEY", "")
    NASA_ENABLED = os.getenv("NASA_ENABLED", "false").lower() == "true" and bool(NASA_API_KEY)
    UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
    UNSPLASH_ENABLED = os.getenv("UNSPLASH_ENABLED", "false").lower() == "true" and bool(UNSPLASH_ACCESS_KEY)
    REDDIT_ENABLED = os.getenv("REDDIT_ENABLED", "false").lower() == "true"

    # Reddit OAuth
    REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
    REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
    REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "python:image-curator:v1.0")

    # AI-tag exclusions
    AI_TAGS: Set[str] = {
        "ai_generated", "stable_diffusion", "midjourney", "novelai",
        "dall-e", "dalle", "ai-assisted", "generated_by_ai", "ai_art",
        "artificial_intelligence", "waifu_diffusion", "anything_v3",
        "sdxl", "controlnet", "txt2img", "img2img"
    }

    # Supported media categories
    CATEGORIES = ["anime", "nature", "moons", "galaxies"]

    # Category groupings used by validation logic
    AVATAR_SUBCATEGORIES = {"anime_girls", "anime_boys", "anime_couples", "avatars"}
    BANNER_SUBCATEGORIES = {"anime_scenery", "space_galaxies", "nature_real", "banners"}

    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL),
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("ImageWorker")

# Vetting list of external Hugging Face repositories used by the worker.
# Default to empty so the worker can complete without hitting external rate limits
# in local/test environments. This can be overridden with HF_SOURCE_REPOS in prod.
raw_hf_sources = os.getenv("HF_SOURCE_REPOS", "")
if raw_hf_sources.strip():
    HF_SOURCE_REPOS = [repo.strip() for repo in raw_hf_sources.split(",") if repo.strip()]
else:
    HF_SOURCE_REPOS = []