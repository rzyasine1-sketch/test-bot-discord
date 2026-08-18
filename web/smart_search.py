#!/usr/bin/env python3
"""
smart_search.py — Intelligent API Manager with Load Balancing
Features:
  • Round-robin API key rotation across N keys (reads from env UNSPLASH_API_KEYS)
  • Per-user 60-second cooldown with threading.Lock
  • Automatic failover on HTTP 429 / timeout
  • Query enhancement pipeline
  • Mock engagement metrics (likes, comments, views) for dynamic pricing

Dependencies: requests
"""

import os
import time
import threading
import random
import hashlib
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass
from datetime import datetime

try:
    import requests
except ImportError:
    raise ImportError("[smart_search] ❌ 'requests' library required. Run: pip install requests")

# ────────────────────────────────────────────────────────────
# Configuration — Load keys from environment (comma-separated)
# ────────────────────────────────────────────────────────────

_env_keys = os.getenv("UNSPLASH_API_KEYS", "")
API_KEYS: List[str] = [k.strip() for k in _env_keys.split(",") if k.strip()] or [
    "UNSPLASH_KEY_1",
    "UNSPLASH_KEY_2",
    "UNSPLASH_KEY_3",
    "UNSPLASH_KEY_4",
    "UNSPLASH_KEY_5",
]

API_BASE_URL = "https://api.unsplash.com/search/photos"
REQUEST_TIMEOUT = 3.0
MAX_RETRIES = len(API_KEYS) if API_KEYS else 1
COOLDOWN_SECONDS = 60

# ────────────────────────────────────────────────────────────
# Data Structures
# ────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    id: str
    url: str
    thumb_url: str
    author: str
    description: Optional[str]
    likes: int
    comments: int
    views: int
    price: int
    width: int
    height: int


# ────────────────────────────────────────────────────────────
# Thread-Safe API Key Rotator (Round-Robin)
# ────────────────────────────────────────────────────────────

class APIKeyRotator:
    def __init__(self, keys: List[str]):
        if not keys:
            raise ValueError("[APIKeyRotator] ❌ API key list cannot be empty.")
        self._keys = keys
        self._index = 0
        self._lock = threading.Lock()

    def current(self) -> str:
        with self._lock:
            return self._keys[self._index]

    def next(self) -> str:
        with self._lock:
            self._index = (self._index + 1) % len(self._keys)
            return self._keys[self._index]

    def rotate(self) -> str:
        return self.next()

    def all_keys(self) -> List[str]:
        return self._keys.copy()


# ────────────────────────────────────────────────────────────
# Cooldown Manager (Per-User Rate Limiting)
# ────────────────────────────────────────────────────────────

class CooldownManager:
    def __init__(self, cooldown_seconds: float = COOLDOWN_SECONDS):
        self._cooldown_seconds = cooldown_seconds
        self._last_request: Dict[str, float] = {}
        self._lock = threading.Lock()

    def is_on_cooldown(self, user_id: str) -> Tuple[bool, float]:
        with self._lock:
            last = self._last_request.get(user_id)
            if last is None:
                return False, 0.0
            elapsed = time.time() - last
            remaining = self._cooldown_seconds - elapsed
            if remaining > 0:
                return True, remaining
            return False, 0.0

    def record_request(self, user_id: str) -> None:
        with self._lock:
            self._last_request[user_id] = time.time()

    def clear_user(self, user_id: str) -> None:
        with self._lock:
            self._last_request.pop(user_id, None)


# ────────────────────────────────────────────────────────────
# Query Enhancement Engine
# ────────────────────────────────────────────────────────────

class QueryEnhancer:
    ENHANCEMENTS: Dict[str, str] = {
        "anime": "anime style digital art high quality",
        "nature": "nature landscape photography high resolution",
        "cyberpunk": "cyberpunk neon futuristic digital art",
        "portrait": "professional portrait photography",
        "abstract": "abstract art high quality digital",
    }

    @classmethod
    def enhance(cls, query: str, category: Optional[str] = None) -> str:
        base = query.strip()
        if category and category.lower() in cls.ENHANCEMENTS:
            modifier = cls.ENHANCEMENTS[category.lower()]
            return f"{base} {modifier}"
        lowered = base.lower()
        for keyword, modifier in cls.ENHANCEMENTS.items():
            if keyword in lowered:
                return f"{base} {modifier}"
        return base


# ────────────────────────────────────────────────────────────
# Mock Engagement Generator (For Dynamic Pricing)
# ────────────────────────────────────────────────────────────

def generate_mock_engagement(seed: str) -> Tuple[int, int, int]:
    hash_val = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    random.seed(hash_val)
    likes = random.randint(50, 5000)
    comments = random.randint(5, 500)
    views = random.randint(1000, 100000)
    random.seed()
    return likes, comments, views


def compute_dynamic_price(likes: int, comments: int, views: int) -> int:
    price = 3000 + (likes * 15) + (comments * 30) + (views * 2)
    return price


# ────────────────────────────────────────────────────────────
# Smart Search Orchestrator
# ────────────────────────────────────────────────────────────

class SmartSearch:
    def __init__(self, api_keys: Optional[List[str]] = None):
        keys = api_keys or API_KEYS
        self.rotator = APIKeyRotator(keys)
        self.cooldown = CooldownManager()
        self.enhancer = QueryEnhancer()

    def search(
        self,
        user_id: str,
        query: str,
        category: Optional[str] = None,
        per_page: int = 12,
        page: int = 1
    ) -> Dict[str, Any]:
        on_cd, remaining = self.cooldown.is_on_cooldown(user_id)
        if on_cd:
            return {
                "success": False,
                "results": [],
                "message": f"⏳ Cooldown active. Please wait {int(remaining)} seconds.",
                "remaining_cooldown": int(remaining)
            }

        enhanced_query = self.enhancer.enhance(query, category)
        results = self._fetch_with_retry(enhanced_query, per_page, page)

        if results is None:
            return {
                "success": False,
                "results": [],
                "message": "❌ All API keys exhausted or service unavailable.",
                "remaining_cooldown": 0
            }

        self.cooldown.record_request(user_id)

        enriched_results = []
        for item in results:
            seed = f"{item['id']}:{user_id}:{datetime.utcnow().strftime('%Y%m%d')}"
            likes, comments, views = generate_mock_engagement(seed)
            price = compute_dynamic_price(likes, comments, views)

            enriched_results.append(SearchResult(
                id=item["id"],
                url=item["urls"]["regular"],
                thumb_url=item["urls"]["small"],
                author=item["user"]["name"],
                description=item.get("description") or item.get("alt_description"),
                likes=likes,
                comments=comments,
                views=views,
                price=price,
                width=item["width"],
                height=item["height"]
            ))

        return {
            "success": True,
            "results": [self._result_to_dict(r) for r in enriched_results],
            "message": f"✅ Found {len(enriched_results)} results.",
            "remaining_cooldown": COOLDOWN_SECONDS,
            "query_used": enhanced_query
        }

    def _fetch_with_retry(
        self,
        query: str,
        per_page: int,
        page: int
    ) -> Optional[List[Dict]]:
        for attempt in range(MAX_RETRIES):
            key = self.rotator.current()
            params = {
                "query": query,
                "per_page": min(per_page, 30),
                "page": page,
                "client_id": key
            }
            try:
                response = requests.get(API_BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
                if response.status_code == 429:
                    print(f"[smart_search] ⚠️ Rate limited on key {attempt + 1}, rotating...")
                    self.rotator.rotate()
                    continue
                response.raise_for_status()
                data = response.json()
                return data.get("results", [])
            except requests.exceptions.Timeout:
                print(f"[smart_search] ⏱️ Timeout on key {attempt + 1}, rotating...")
                self.rotator.rotate()
                continue
            except requests.exceptions.RequestException as e:
                print(f"[smart_search] ❌ Request error: {e}")
                self.rotator.rotate()
                continue
        return None

    @staticmethod
    def _result_to_dict(result: SearchResult) -> Dict[str, Any]:
        return {
            "id": result.id,
            "url": result.url,
            "thumb_url": result.thumb_url,
            "author": result.author,
            "description": result.description,
            "likes": result.likes,
            "comments": result.comments,
            "views": result.views,
            "price": result.price,
            "width": result.width,
            "height": result.height,
        }


smart_search = SmartSearch()

def search_images(user_id: str, query: str, category: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    return smart_search.search(user_id, query, category, **kwargs)

def get_cooldown_status(user_id: str) -> Dict[str, Any]:
    on_cd, remaining = smart_search.cooldown.is_on_cooldown(user_id)
    return {
        "on_cooldown": on_cd,
        "remaining_seconds": int(remaining),
        "total_cooldown": COOLDOWN_SECONDS
    }


if __name__ == "__main__":
    print("[smart_search] 🧠 Smart Search Engine initialized.")
    print(f"[smart_search] 🔑 Loaded {len(API_KEYS)} API keys.")
    print(f"[smart_search] ⏱️ Cooldown: {COOLDOWN_SECONDS}s per user.")