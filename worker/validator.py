"""Swarm 2.0 image validation and filtering.

This module validates downloaded images before cataloging them into the shop.
Safe images are accepted for both avatar and background/banner collections, but
face detection is only enforced for avatar-oriented categories.
"""

import gc
import hashlib
import json
import logging
import os
import time
from io import BytesIO
from typing import Optional

import requests
from PIL import Image
from huggingface_hub import HfApi

from ai_filter import WD14Classifier
from config import Config
from dedup_pricer import Deduplicator, PixelPricer
from face_detector import FaceDetector
from models import ImageMetadata

logger = logging.getLogger("ImageWorker")


def _normalize_subcategory(subcategory: Optional[str]) -> str:
    """Normalize category strings such as 'Anime Girls' or 'banners' into a canonical key."""
    if not subcategory:
        return "anime_scenery"
    normalized = subcategory.strip().lower().replace(" ", "_")
    aliases = {
        "avatar": "anime_girls",
        "avatars": "anime_girls",
        "anime_avatar": "anime_girls",
        "background": "anime_scenery",
        "banner": "anime_scenery",
        "banners": "anime_scenery",
    }
    return aliases.get(normalized, normalized)


def _is_avatar_subcategory(subcategory: Optional[str]) -> bool:
    """Return True for avatar-style categories that require human face validation."""
    normalized = _normalize_subcategory(subcategory)
    return normalized in Config.AVATAR_SUBCATEGORIES


def _is_banner_subcategory(subcategory: Optional[str]) -> bool:
    """Return True for scenery/background categories that should skip face enforcement."""
    normalized = _normalize_subcategory(subcategory)
    return normalized in Config.BANNER_SUBCATEGORIES or normalized in {"anime_scenery", "nature_real", "space_galaxies"}


def _upload_file_to_hf(file_path: str, subcategory: str, filename: str) -> Optional[str]:
    """Upload a validated local file to the configured HF dataset and return its remote URL."""
    if not Config.HF_TOKEN:
        logger.warning("HF_TOKEN missing. Upload disabled for %s/%s", subcategory, filename)
        return None

    repo_id = Config.HF_UPLOAD_TARGET or Config.HF_DATASET_NAME
    repo_path = f"{subcategory}/{filename}"
    try:
        api = HfApi(token=Config.HF_TOKEN)
        api.upload_file(
            path_or_fileobj=file_path,
            path_in_repo=repo_path,
            repo_id=repo_id,
            repo_type=Config.HF_REPO_TYPE,
        )
        hf_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{repo_path}"
        logger.info("[HF UPLOAD] uploaded %s -> %s", file_path, hf_url)
        return hf_url
    except Exception as exc:
        logger.error("[HF UPLOAD] failed for %s/%s: %s", subcategory, filename, exc)
        return None


def _delete_local_file(file_path: str) -> None:
    """Remove the local image after a successful upload to keep the workspace disk clean."""
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            logger.info("[LOCAL CLEANUP] deleted %s after successful HF upload", file_path)
    except Exception as exc:
        logger.warning("[LOCAL CLEANUP] failed to delete %s: %s", file_path, exc)


def _upload_images_json(repo_id: str, payload: list) -> None:
    """Upload a metadata snapshot as images.json to the target dataset repo."""
    if not Config.HF_TOKEN:
        return
    local_json = os.path.join(Config.OUTPUT_IMAGES_DIR, '.images.json.tmp')
    try:
        with open(local_json, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        api = HfApi(token=Config.HF_TOKEN)
        api.upload_file(
            path_or_fileobj=local_json,
            path_in_repo='images.json',
            repo_id=repo_id,
            repo_type=Config.HF_REPO_TYPE,
        )
        logger.info("[HF UPLOAD] metadata synced to %s/images.json", repo_id)
    except Exception as exc:
        logger.error("[HF UPLOAD] metadata sync failed: %s", exc)
    finally:
        try:
            if os.path.exists(local_json):
                os.remove(local_json)
        except Exception:
            pass


def validate_image(image_path: str, subcategory: Optional[str], tags: Optional[str] = None) -> dict:
    """Validate a local image path against the safety gate and category requirements.

    Rules:
      1. All images must pass the WD14 safety check.
      2. Avatar categories require face detection.
      3. Banner/background categories skip face detection when safe.
    """
    if not image_path or not os.path.exists(image_path):
        return {"valid": False, "reason": "missing_image_path", "subcategory": "rejected", "face_count": 0}

    norm_subcat = _normalize_subcategory(subcategory)
    wd14 = WD14Classifier()
    wd14_result = wd14.classify(tags or "")

    if not wd14_result.get("is_safe", False):
        logger.warning("Rejected image: %s (%s)", image_path, wd14_result.get("reason", "unsafe"))
        return {"valid": False, "reason": "unsafe", "subcategory": "rejected", "face_count": 0}

    if _is_avatar_subcategory(norm_subcat):
        detector = FaceDetector()
        try:
            with open(image_path, "rb") as image_file:
                image_data = image_file.read()
            detection = detector.detect_and_crop(image_data, target_size=(512, 512))
            face_count = int((detection or {}).get("face_count", 0) or 0)
        except Exception as exc:
            logger.warning("Face detection failed for avatar image %s: %s", image_path, exc)
            face_count = 0

        if face_count <= 0:
            return {"valid": False, "reason": "avatar_missing_faces", "subcategory": "rejected", "face_count": 0}

        return {"valid": True, "reason": "approved_avatar", "subcategory": norm_subcat, "face_count": face_count}

    if _is_banner_subcategory(norm_subcat):
        return {"valid": True, "reason": "approved_banner", "subcategory": norm_subcat, "face_count": 0}

    return {"valid": True, "reason": "approved_default", "subcategory": norm_subcat, "face_count": 0}


class ImageValidator:
    """Validation and filtering pipeline for fetcher-produced image metadata."""

    def __init__(self, db_path: str):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; ImageCurator/2.0; HF Worker)"
        })
        self.face_detector = FaceDetector()
        self.wd14 = WD14Classifier()
        self.dedup = Deduplicator(db_path)
        self.pricer = PixelPricer()
        self._uploaded_metadata = []

    def _record_upload_metadata(self, meta: ImageMetadata, repo_id: str) -> None:
        record = {
            "url": meta.hf_url or meta.file_path or meta.url,
            "subcategory": meta.subcategory,
            "title": meta.title,
            "source": meta.source,
            "tags": meta.tags or "",
            "width": getattr(meta, "width", 0),
            "height": getattr(meta, "height", 0),
            "face_count": getattr(meta, "face_count", 0),
            "pixel_price": getattr(meta, "pixel_price", 0.0),
            "hf_path": getattr(meta, "hf_path", ""),
        }
        if record not in self._uploaded_metadata:
            self._uploaded_metadata.append(record)
        _upload_images_json(repo_id, self._uploaded_metadata)

    def _has_ai_tags(self, tags: Optional[str]) -> bool:
        if not tags:
            return False
        tag_set = set(tags.lower().split())
        return not tag_set.isdisjoint(Config.AI_TAGS)

    def _has_ai_indicators(self, url: str, title: Optional[str]) -> bool:
        combined = f"{url} {title or ''}".lower()
        indicators = [
            "ai-generated", "ai_generated", "stable_diffusion",
            "midjourney", "dalle", "openai", "novelai", "generated"
        ]
        return any(ind in combined for ind in indicators)

    def _detect_faces_for_validation(self, image_path: str) -> int:
        """Return the number of human faces for an avatar image.

        Prefer a dedicated detector method when available and fall back to the
        crop detection pipeline used by the existing face detector.
        """
        detector = self.face_detector
        if hasattr(detector, "detect_faces") and callable(detector.detect_faces):
            try:
                return int(detector.detect_faces(image_path))
            except Exception:
                pass

        try:
            with open(image_path, "rb") as image_file:
                image_data = image_file.read()
            detection = detector.detect_and_crop(image_data, target_size=(512, 512))
            return int((detection or {}).get("face_count", 0) or 0)
        except Exception as exc:
            logger.warning("Fallback face detection failed for %s: %s", image_path, exc)
            return 0

    def validate(self, meta: ImageMetadata) -> Optional[ImageMetadata]:
        """Full validation pipeline for metadata and downloaded image bytes."""
        if self._has_ai_tags(meta.tags):
            logger.info("AI tag rejected: %s...", meta.url[:50])
            return None
        if self._has_ai_indicators(meta.url, meta.title):
            logger.info("AI indicator rejected: %s...", meta.url[:50])
            return None

        try:
            # Handle file:// URLs (from local temp files) vs HTTP URLs
            if meta.url.startswith("file://"):
                # Read from local file
                file_path = meta.url[7:]  # Remove 'file://' prefix
                try:
                    with open(file_path, "rb") as f:
                        image_data = f.read()
                    logger.info("Loaded image from local file: %s", file_path)
                except Exception as e:
                    logger.warning("Failed to read local file %s: %s", file_path, e)
                    return None
            else:
                # Download from HTTP URL
                for attempt in range(1, Config.MAX_HTTP_RETRIES + 2):
                    try:
                        response = self.session.get(meta.url, timeout=30, stream=True)
                        response.raise_for_status()
                        image_data = response.content
                        break
                    except requests.HTTPError as exc:
                        if exc.response is not None and exc.response.status_code == 429:
                            retry_after = exc.response.headers.get("Retry-After")
                            sleep_for = int(retry_after) if retry_after and retry_after.isdigit() else Config.HTTP_RETRY_DELAY * attempt
                            logger.warning("429 on %s, retry %s/%s in %ss", meta.url[:80], attempt, Config.MAX_HTTP_RETRIES + 1, sleep_for)
                            if attempt > Config.MAX_HTTP_RETRIES:
                                return None
                            time.sleep(sleep_for)
                            continue
                        raise
                else:
                    return None
            
            # Determine content type
            if meta.url.startswith("file://"):
                content_type = "image/png"  # Default for local files from PIL
            else:
                content_type = response.headers.get("content-type", "")
            
            if not meta.url.startswith("file://") and not content_type.startswith("image/"):
                return None
            if len(image_data) < 1024:
                logger.debug("Image too small: %d bytes", len(image_data))
                return None

            wd14_result = self.wd14.classify(meta.tags)
            logger.debug("[VALIDATION] WD14 result for %s: %s", meta.url[:50], wd14_result)
            if not wd14_result["is_safe"]:
                logger.warning("WD14 rejected [%s]: %s", meta.url[:50], wd14_result.get("reason", "unsafe"))
                meta.subcategory = "rejected"
                meta.is_nsfw = 1 if wd14_result.get("is_nsfw") else 0
                meta.is_loli = 1 if wd14_result.get("is_loli") else 0
                return self._save_rejected(meta, image_data)

            intended_subcategory = _normalize_subcategory(meta.subcategory or meta.category)
            logger.debug("[VALIDATION] Category: %s -> %s", meta.category, intended_subcategory)

            if _is_avatar_subcategory(intended_subcategory):
                logger.debug("[VALIDATION] Running face detection for avatar category")
                face_result = self.face_detector.detect_and_crop(image_data, target_size=(512, 512))
                face_count = int((face_result or {}).get("face_count", 0) or 0)
                logger.debug("[VALIDATION] Face detection result: %d faces", face_count)
                if face_count <= 0:
                    logger.info("Avatar category rejected for missing faces: %s", meta.url[:80])
                    meta.subcategory = "rejected"
                    return self._save_rejected(meta, image_data)
            else:
                face_count = 0

            if _is_banner_subcategory(intended_subcategory):
                final_subcategory = intended_subcategory
                logger.debug("[VALIDATION] Using banner subcategory: %s", final_subcategory)
            elif face_count > 0:
                gender = wd14_result.get("gender", "unknown")
                if gender == "girls":
                    final_subcategory = "anime_girls"
                elif gender == "boys":
                    final_subcategory = "anime_boys"
                elif gender in {"couples_or_group", "unknown"}:
                    final_subcategory = "anime_couples"
                else:
                    final_subcategory = "anime_scenery"
            else:
                final_subcategory = "anime_scenery"

            phash = self.dedup.compute_phash(image_data)
            if phash and self.dedup.is_duplicate(phash):
                logger.info("pHash duplicate: %s...", meta.url[:50])
                return None

            img = Image.open(BytesIO(image_data))
            img.verify()
            img = Image.open(BytesIO(image_data))
            width, height = img.size

            if width < Config.MIN_WIDTH or height < Config.MIN_HEIGHT:
                logger.debug("Too small (%sx%s)", width, height)
                return None

            pixel_price = self.pricer.calculate(width, height)

            use_cropped = (face_count > 0 and face_result is not None and face_result.get("cropped_bytes"))
            final_data = face_result["cropped_bytes"] if use_cropped else image_data

            ext = self._get_extension(content_type, meta.url)
            safe_title = "".join(c for c in (meta.title or "img") if c.isalnum() or c in (" ", "-", "_")).rstrip()[:30]
            filename = f"{final_subcategory}_{hashlib.md5(final_data).hexdigest()[:12]}_{safe_title}.{ext}"

            category_dir = os.path.join(Config.OUTPUT_IMAGES_DIR, final_subcategory)
            os.makedirs(category_dir, exist_ok=True)
            file_path = os.path.join(category_dir, filename)

            with open(file_path, "wb") as image_file:
                image_file.write(final_data)

            if phash:
                self.dedup.store_phash(phash, meta.url)

            meta.width = width
            meta.height = height
            meta.checksum = hashlib.md5(final_data).hexdigest()
            meta.subcategory = final_subcategory
            meta.phash = phash
            meta.face_count = face_count
            meta.pixel_price = pixel_price
            meta.is_nsfw = 0
            meta.is_loli = 0

            hf_url = _upload_file_to_hf(file_path, final_subcategory, filename)
            if hf_url:
                # Keep local file path for dataset creation, store HF URL separately
                meta.file_path = file_path
                meta.hf_url = hf_url
                meta.hf_path = f"{final_subcategory}/{filename}"
                self._record_upload_metadata(meta, Config.HF_UPLOAD_TARGET or Config.HF_DATASET_NAME)
                # Keep local files on disk for dataset push_to_hub() to read
                logger.info("[VALIDATION] Local file kept for dataset: %s", file_path)
                gc.collect()
            else:
                # Upload failed - still keep local file in case of retry
                meta.file_path = file_path
                meta.hf_url = ""
                meta.hf_path = ""
                logger.warning("[VALIDATION] HF upload failed, keeping local file for retry: %s", file_path)

            logger.info("[VALIDATION] ✓ [%s] faces=%s %sx%s $%s | %s...", final_subcategory, face_count, width, height, pixel_price, meta.url[:50])
            return meta

        except Exception as exc:
            logger.debug("Validation failed: %s", exc)
            return None

    def _save_rejected(self, meta: ImageMetadata, image_data: bytes) -> Optional[ImageMetadata]:
        """Do not keep rejected images on disk; local cache must remain empty at all times."""
        try:
            meta.file_path = ""
            meta.subcategory = "rejected"
            logger.info("[LOCAL CLEANUP] rejected image skipped and not stored locally: %s", meta.url[:80])
            return meta
        except Exception as exc:
            logger.error("Rejected handling failed: %s", exc)
            return None

    def _get_extension(self, content_type: str, url: str) -> str:
        """Return a safe extension for an image based on content type or URL."""
        ext_map = {
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
        }
        if content_type in ext_map:
            return ext_map[content_type]
        url_ext = url.split(".")[-1].split("?")[0].lower()
        return url_ext if url_ext in ("jpg", "jpeg", "png", "gif", "webp") else "jpg"
