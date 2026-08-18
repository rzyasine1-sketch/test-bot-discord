"""Swarm 2.0 - All Source Fetchers"""
import hashlib
import logging
import os
import tempfile
from abc import ABC, abstractmethod
from io import BytesIO
from typing import List, Optional, Dict
import requests
from config import Config
from models import ImageMetadata

logger = logging.getLogger("ImageWorker")

# Reddit اختياري
try:
    import praw
    PRAW_AVAILABLE = True
except ImportError:
    PRAW_AVAILABLE = False

# PIL for handling dataset images
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


class BaseFetcher(ABC):
    @abstractmethod
    def fetch(self, count: int) -> List[ImageMetadata]:
        pass

    @property
    @abstractmethod
    def source_name(self) -> str:
        pass


class SafebooruFetcher(BaseFetcher):
    """Safebooru API: rating:safe + استبعاد AI tags"""
    BASE_URL = "https://safebooru.org/index.php"

    CATEGORY_TAGS = {
        "anime": "rating:safe -ai_generated -stable_diffusion -midjourney -novelai -dall-e -sdxl",
        "nature": "rating:safe scenery nature landscape -ai_generated -stable_diffusion",
        "moons": "rating:safe moon night_sky -ai_generated -stable_diffusion",
        "galaxies": "rating:safe space galaxy stars -ai_generated -stable_diffusion"
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ImageCuratorBot/1.0 (HuggingFace Worker)"})

    @property
    def source_name(self) -> str:
        return "safebooru"

    def fetch(self, count: int) -> List[ImageMetadata]:
        results = []
        per_category = max(1, count // len(Config.CATEGORIES))

        for category in Config.CATEGORIES:
            tags = self.CATEGORY_TAGS.get(category, "rating:safe")
            try:
                params = {
                    "page": "dapi", "s": "post", "q": "index",
                    "json": "1", "limit": min(per_category, 100), "tags": tags
                }
                resp = self.session.get(self.BASE_URL, params=params, timeout=30)
                resp.raise_for_status()
                posts = resp.json()

                if not isinstance(posts, list):
                    logger.warning(f"Safebooru non-list response for {category}")
                    continue

                for post in posts:
                    file_url = post.get("file_url") or post.get("image")
                    if not file_url:
                        continue
                    if file_url.startswith("//"):
                        file_url = "https:" + file_url
                    elif file_url.startswith("/"):
                        file_url = "https://safebooru.org" + file_url

                    results.append(ImageMetadata(
                        url=file_url,
                        title=(post.get("tags") or "safebooru")[:100],
                        category=category,
                        source=self.source_name,
                        tags=post.get("tags", "")
                    ))
            except Exception as e:
                logger.error(f"Safebooru error [{category}]: {e}")

        return results


class NasaApodFetcher(BaseFetcher):
    """NASA APOD: صور فضاء حقيقية"""
    BASE_URL = "https://api.nasa.gov/planetary/apod"

    KEYWORDS = {
        "moons": ["moon", "lunar", "eclipse", "titan", "europa", "io", "callisto"],
        "galaxies": ["galaxy", "galaxies", "nebula", "milky way", "andromeda", "cluster", "supernova"],
        "nature": ["earth", "aurora", "forest", "ocean", "desert", "mountain"]
    }

    def __init__(self):
        self.session = requests.Session()

    @property
    def source_name(self) -> str:
        return "nasa_apod"

    def _classify(self, title: str, explanation: str) -> Optional[str]:
        text = f"{title} {explanation}".lower()
        for cat, kws in self.KEYWORDS.items():
            if any(k in text for k in kws):
                return cat
        return "galaxies"

    def fetch(self, count: int) -> List[ImageMetadata]:
        results = []
        try:
            params = {"api_key": Config.NASA_API_KEY, "count": min(count, 100)}
            resp = self.session.get(self.BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            items = resp.json()
            items = items if isinstance(items, list) else [items]

            for item in items:
                if item.get("media_type") != "image":
                    continue
                url = item.get("hdurl") or item.get("url")
                if not url:
                    continue

                results.append(ImageMetadata(
                    url=url,
                    title=item.get("title", "NASA_APOD")[:100],
                    category=self._classify(item.get("title", ""), item.get("explanation", "")),
                    source=self.source_name,
                    tags="nasa apod astronomy"
                ))
        except Exception as e:
            logger.error(f"NASA error: {e}")
        return results


class UnsplashFetcher(BaseFetcher):
    """Unsplash API: طبيعة وفضاء عالي الدقة"""
    BASE_URL = "https://api.unsplash.com/search/photos"

    QUERIES = {
        "nature": "nature landscape forest mountain ocean",
        "moons": "moon lunar night sky",
        "galaxies": "galaxy space nebula stars universe",
        "anime": "anime art illustration"
    }

    def __init__(self):
        self.session = requests.Session()
        if not Config.UNSPLASH_ACCESS_KEY:
            logger.warning("Unsplash key missing")

    @property
    def source_name(self) -> str:
        return "unsplash"

    def fetch(self, count: int) -> List[ImageMetadata]:
        if not Config.UNSPLASH_ACCESS_KEY:
            return []

        results = []
        per_cat = max(1, count // len(Config.CATEGORIES))

        for category, query in self.QUERIES.items():
            try:
                params = {
                    "query": query, "per_page": min(per_cat, 30),
                    "client_id": Config.UNSPLASH_ACCESS_KEY
                }
                resp = self.session.get(self.BASE_URL, params=params, timeout=30)
                resp.raise_for_status()

                for photo in resp.json().get("results", []):
                    url = photo.get("urls", {}).get("raw") or photo.get("urls", {}).get("full")
                    if not url:
                        continue

                    results.append(ImageMetadata(
                        url=url + "&q=85&w=1920",
                        title=(photo.get("description") or photo.get("alt_description") or "unsplash")[:100],
                        category=category,
                        source=self.source_name,
                        tags="unsplash photography"
                    ))
            except Exception as e:
                logger.error(f"Unsplash error [{category}]: {e}")
        return results


class RedditFetcher(BaseFetcher):
    """Reddit عبر PRAW. يتطلب OAuth."""

    SUBREDDITS = {
        "anime": ["animepfp", "AnimeWallpaper", "awwnime"],
        "nature": ["EarthPorn", "natureporn", "NatureIsFuckingLit"],
        "moons": ["spaceporn", "luna"],
        "galaxies": ["spaceporn", "Amoledbackgrounds", "astrophotography"]
    }

    def __init__(self):
        self.reddit = None
        if not (Config.REDDIT_ENABLED and PRAW_AVAILABLE):
            return
        try:
            self.reddit = praw.Reddit(
                client_id=Config.REDDIT_CLIENT_ID,
                client_secret=Config.REDDIT_CLIENT_SECRET,
                user_agent=Config.REDDIT_USER_AGENT
            )
            self.reddit.user.me()
            logger.info("Reddit API connected")
        except Exception as e:
            logger.warning(f"Reddit failed: {e}")

    @property
    def source_name(self) -> str:
        return "reddit"

    def fetch(self, count: int) -> List[ImageMetadata]:
        if not self.reddit:
            return []

        results = []
        per_sub = max(1, count // 8)

        for category, subs in self.SUBREDDITS.items():
            for sub_name in subs:
                try:
                    for post in self.reddit.subreddit(sub_name).hot(limit=per_sub):
                        if post.is_self or not post.url:
                            continue
                        url = post.url
                        if not any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                            if "imgur.com" in url and not url.endswith(('.jpg', '.png')):
                                url += ".jpg"
                            else:
                                continue

                        results.append(ImageMetadata(
                            url=url, title=post.title[:100],
                            category=category, source=f"reddit/{sub_name}", tags=""
                        ))
                except Exception as e:
                    logger.error(f"Reddit r/{sub_name} error: {e}")
        return results

import gc
import logging
from datasets import load_dataset

logger = logging.getLogger(__name__)


class HuggingFaceFetcher:
    """جالب الصور من مستودعات Hugging Face بشكل أكثر كفاءة.

    لا نقوم بتحميل كل صورة بشكل منفصل عبر HTTP؛ بدلاً من ذلك نقرأ البيانات مباشرة من
    dataset ثم نستخدم URL الموجود في كل صف إن وجد. هذا يقلل الطلبات المتكررة ويفادي 307
    Redirects غير الضرورية.
    """

    def __init__(self, repo_ids: list = None):
        self.repo_ids = list(repo_ids or [])
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ImageCuratorBot/1.0 (HuggingFace Worker)"})

    @property
    def source_name(self) -> str:
        return "huggingface"

    @staticmethod
    def _normalize_tags(value) -> str:
        if not value:
            return "huggingface"
        if isinstance(value, (list, tuple, set)):
            return " ".join(str(v) for v in value)
        return str(value)

    @staticmethod
    def _extract_url(row: dict) -> Optional[str]:
        """Extract image URL from a row, or create a local temp file for PIL Images."""
        # Try to extract URL from common column names
        for key in ("image_url", "url", "link", "source", "download_url"):
            if key in row:
                value = row.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value

        # Check for PIL Image objects and convert to temporary file
        if PIL_AVAILABLE and "image" in row:
            image_field = row.get("image")
            
            # Handle direct PIL Image objects
            if hasattr(image_field, 'save') and hasattr(image_field, 'format'):
                try:
                    # Save PIL Image to a temporary file
                    temp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    image_field.save(temp_file.name, format="PNG")
                    logger.debug(f"Converted PIL Image to temp file: {temp_file.name}")
                    return f"file://{temp_file.name}"
                except Exception as e:
                    logger.warning(f"Failed to save PIL Image: {e}")
                    return None
            
            # Handle dict-wrapped images
            if isinstance(image_field, dict):
                for url_key in ("url", "src", "image_url", "download_url", "link"):
                    if url_key in image_field:
                        url_val = image_field.get(url_key)
                        if isinstance(url_val, str) and url_val.startswith("http"):
                            return url_val

        return None

    @staticmethod
    def _infer_category(repo_id: str) -> str:
        repo_name = repo_id.lower()
        if any(token in repo_name for token in ("anime", "avatar", "portrait", "face")):
            return "anime"
        if any(token in repo_name for token in ("space", "galaxy", "nasa", "nebula", "universe")):
            return "galaxies"
        if any(token in repo_name for token in ("nature", "landscape", "scenery", "forest", "mountain")):
            return "nature"
        return "anime"

    def fetch(self, count: int) -> List[ImageMetadata]:
        fetched_items: List[ImageMetadata] = []
        target_count = max(1, min(count, 10))

        for repo_id in self.repo_ids:
            try:
                logger.info(f"🔍 Loading dataset: {repo_id}")
                dataset = load_dataset(repo_id, split="train", streaming=True)
                row_count = 0

                for row in dataset:
                    if row_count >= min(Config.HF_FETCH_LIMIT_PER_REPO, 10):
                        break
                    row_count += 1

                    img_url = self._extract_url(row)
                    if not img_url:
                        logger.debug(f"[{repo_id}] Row {row_count}: No URL found. Keys: {list(row.keys())}")
                        continue

                    title = row.get("title") or row.get("caption") or row.get("prompt") or f"Image from {repo_id}"
                    tags = self._normalize_tags(row.get("tags"))

                    logger.info(f"[{repo_id}] Row {row_count}: Found URL - {img_url[:60]}...")
                    fetched_items.append(ImageMetadata(
                        url=img_url,
                        title=str(title)[:100],
                        category=self._infer_category(repo_id),
                        source=f"HF:{repo_id}",
                        tags=tags
                    ))

                    gc.collect()

                    if len(fetched_items) >= target_count:
                        return fetched_items

                logger.info(f"[{repo_id}] Completed: {row_count} rows scanned, {len(fetched_items)} items collected")

            except Exception as e:
                logger.error(f"❌ Error loading {repo_id}: {e}", exc_info=True)

        return fetched_items

    def fetch_images(self, limit_per_repo: int = 25) -> list:
        fetched_items = []
        per_repo_limit = max(1, min(limit_per_repo, Config.HF_FETCH_LIMIT_PER_REPO))

        for repo_id in self.repo_ids:
            try:
                logger.info(f"🔍 جاري جلب الصور من مستودع: {repo_id}")
                dataset = load_dataset(repo_id, split="train", streaming=True)
                row_count = 0

                for row in dataset:
                    if row_count >= per_repo_limit:
                        break
                    row_count += 1

                    img_url = self._extract_url(row)
                    if not img_url:
                        continue

                    fetched_items.append({
                        "url": img_url,
                        "source": f"HF:{repo_id}",
                        "title": str(row.get("title") or row.get("caption") or f"Image from {repo_id}")[:100]
                    })

                    gc.collect()

                    if len(fetched_items) >= per_repo_limit:
                        return fetched_items

            except Exception as e:
                logger.error(f"❌ خطأ أثناء السحب من {repo_id}: {e}")

        return fetched_items