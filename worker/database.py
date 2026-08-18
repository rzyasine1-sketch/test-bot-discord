#!/usr/bin/env python3
"""
database.py — Unified SQLite Database Manager (Synchronous Side)
Provides both standalone functions for Flask/Bot and the `Database` class for ImageWorker.
"""

import sqlite3
import threading
import secrets
import string
import os
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Optional, List, Dict, Any, Tuple
from dotenv import load_dotenv

# ═══════════════════════════════════════════════════════════
# FIX: Force SHARED absolute database path
# ═══════════════════════════════════════════════════════════
script_dir = Path(__file__).resolve().parent          
project_root = script_dir.parent if script_dir.name in ["web", "worker"] else script_dir

# نحمل .env للمتغيرات الأخرى فقط
env_file = project_root / ".env"
if env_file.exists():
    load_dotenv(dotenv_path=str(env_file))

# ══ المسار المطلق المشترك ══
DATABASE_PATH = str(project_root / "profiles.db")
Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)

print(f"[WEB/WORKER] 📂 Database path: {DATABASE_PATH}")


CODE_LENGTH = 6
CODE_EXPIRY_MINUTES = 5
DEFAULT_COINS = 5000
DEFAULT_XP = 0

# ────────────────────────────────────────────────────────────
# Thread-local connection storage (connection-per-thread)
# ────────────────────────────────────────────────────────────
_thread_local = threading.local()


def _get_connection() -> sqlite3.Connection:
    """Get or create a thread-local SQLite connection."""
    if not hasattr(_thread_local, "conn") or _thread_local.conn is None:
        _thread_local.conn = sqlite3.connect(
            DATABASE_PATH,
            check_same_thread=False,
            timeout=20.0,
            isolation_level=None
        )
        _thread_local.conn.row_factory = sqlite3.Row
        _thread_local.conn.execute("PRAGMA foreign_keys = ON")
        _thread_local.conn.execute("PRAGMA journal_mode = WAL")
        _thread_local.conn.execute("PRAGMA synchronous = NORMAL")
    return _thread_local.conn


@contextmanager
def get_db():
    """Context manager for database transactions with automatic commit/rollback."""
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        conn.execute("BEGIN")
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


# ────────────────────────────────────────────────────────────
# Schema Initialization
# ────────────────────────────────────────────────────────────

def init_db() -> None:
    """Initialize the database with all required tables."""
    with get_db() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id         TEXT PRIMARY KEY NOT NULL,
                coins           INTEGER NOT NULL DEFAULT 5000 CHECK(coins >= 0),
                xp              INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0),
                last_daily      TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id         TEXT PRIMARY KEY NOT NULL,
                active_avatar_url TEXT,
                birth_date      TEXT,
                country         TEXT,
                bio             TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS auth_codes (
                user_id         TEXT PRIMARY KEY NOT NULL,
                login_code      TEXT NOT NULL UNIQUE,
                code            TEXT,
                expires_at      TIMESTAMP NOT NULL,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         TEXT NOT NULL,
                item_type       TEXT NOT NULL CHECK(item_type IN ('avatar', 'banner')),
                avatar_id       TEXT,
                local_file_path TEXT NOT NULL,
                purchased_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS remote_images (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                url             TEXT NOT NULL UNIQUE,
                subcategory     TEXT NOT NULL,
                display_name    TEXT NOT NULL,
                character_name  TEXT DEFAULT '',
                anime_name      TEXT DEFAULT '',
                tags            TEXT DEFAULT '',
                phash           TEXT DEFAULT '',
                face_count      INTEGER DEFAULT 0,
                pixel_price     REAL DEFAULT 0.0,
                width           INTEGER DEFAULT 0,
                height          INTEGER DEFAULT 0,
                source_dataset  TEXT DEFAULT '',
                is_featured     INTEGER DEFAULT 0,
                synced_to_hf    INTEGER DEFAULT 0,
                hf_path         TEXT DEFAULT '',
                hf_url          TEXT DEFAULT '',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migration for older SQLite databases created before the HF path/url columns existed.
        cursor.execute("PRAGMA table_info(remote_images)")
        columns = {row[1] for row in cursor.fetchall()}
        if "hf_path" not in columns:
            cursor.execute("ALTER TABLE remote_images ADD COLUMN hf_path TEXT DEFAULT ''")
        if "hf_url" not in columns:
            cursor.execute("ALTER TABLE remote_images ADD COLUMN hf_url TEXT DEFAULT ''")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_inventory_user_id ON inventory(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_inventory_item_type ON inventory(user_id, item_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_codes_expires ON auth_codes(expires_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_remote_phash ON remote_images(phash)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_remote_synced ON remote_images(synced_to_hf)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_remote_subcat ON remote_images(subcategory)")

    print("[DATABASE] ✅ Schema initialized successfully.")


# ────────────────────────────────────────────────────────────
# User Operations
# ────────────────────────────────────────────────────────────

def get_or_create_user(user_id: str) -> Dict[str, Any]:
    with get_db() as cursor:
        cursor.execute("SELECT user_id, coins, xp, created_at FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT INTO users (user_id, coins, xp) VALUES (?, ?, ?)", (user_id, DEFAULT_COINS, DEFAULT_XP))
            return {"user_id": user_id, "coins": DEFAULT_COINS, "xp": DEFAULT_XP, "created_at": datetime.utcnow().isoformat()}
        return dict(row)


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as cursor:
        cursor.execute("SELECT user_id, coins, xp, created_at FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_user_coins(user_id: str, delta: int) -> bool:
    with get_db() as cursor:
        cursor.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return False
        new_balance = row["coins"] + delta
        if new_balance < 0:
            return False
        cursor.execute("UPDATE users SET coins = ? WHERE user_id = ?", (new_balance, user_id))
        return True


def add_xp(user_id: str, amount: int) -> None:
    with get_db() as cursor:
        cursor.execute("UPDATE users SET xp = xp + ? WHERE user_id = ?", (amount, user_id))


# ────────────────────────────────────────────────────────────
# Auth Code Operations
# ────────────────────────────────────────────────────────────

def generate_login_code(user_id: str) -> str:
    alphabet = string.ascii_uppercase + string.digits
    code = ''.join(secrets.choice(alphabet) for _ in range(CODE_LENGTH))
    expires_at = datetime.utcnow() + timedelta(minutes=CODE_EXPIRY_MINUTES)

    with get_db() as cursor:
        cursor.execute("""
            INSERT INTO auth_codes (user_id, login_code, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                login_code = excluded.login_code,
                expires_at = excluded.expires_at,
                created_at = CURRENT_TIMESTAMP
        """, (user_id, code, expires_at.isoformat()))
    return code


def validate_login_code(code: str) -> Optional[str]:
    with get_db() as cursor:
        cursor.execute("SELECT user_id, expires_at FROM auth_codes WHERE login_code = ?", (code,))
        row = cursor.fetchone()
        if row is None:
            return None

        expires_at = datetime.fromisoformat(row["expires_at"])
        if datetime.utcnow() > expires_at:
            cursor.execute("DELETE FROM auth_codes WHERE login_code = ?", (code,))
            return None

        user_id = row["user_id"]
        cursor.execute("DELETE FROM auth_codes WHERE login_code = ?", (code,))
        return user_id


def cleanup_expired_codes() -> int:
    with get_db() as cursor:
        cursor.execute("DELETE FROM auth_codes WHERE expires_at < datetime('now')")
        return cursor.rowcount


# ────────────────────────────────────────────────────────────
# Inventory Operations
# ────────────────────────────────────────────────────────────

def add_inventory_item(user_id: str, item_type: str, file_path: str) -> int:
    with get_db() as cursor:
        cursor.execute("""
            INSERT INTO inventory (user_id, item_type, local_file_path)
            VALUES (?, ?, ?)
        """, (user_id, item_type, file_path))
        return cursor.lastrowid


def get_user_inventory(user_id: str) -> List[Dict[str, Any]]:
    with get_db() as cursor:
        cursor.execute("""
            SELECT id, item_type, local_file_path, purchased_at
            FROM inventory WHERE user_id = ? ORDER BY purchased_at DESC
        """, (user_id,))
        return [dict(row) for row in cursor.fetchall()]


def get_inventory_count(user_id: str, item_type: Optional[str] = None) -> int:
    with get_db() as cursor:
        if item_type:
            cursor.execute("SELECT COUNT(*) as count FROM inventory WHERE user_id = ? AND item_type = ?", (user_id, item_type))
        else:
            cursor.execute("SELECT COUNT(*) as count FROM inventory WHERE user_id = ?", (user_id,))
        return cursor.fetchone()["count"]


# ────────────────────────────────────────────────────────────
# Remote Image Operations
# ────────────────────────────────────────────────────────────

def add_remote_image(
    url: str,
    subcategory: str,
    display_name: str,
    character_name: str = "",
    anime_name: str = "",
    tags: str = "",
    phash: str = "",
    face_count: int = 0,
    pixel_price: float = 0.0,
    width: int = 0,
    height: int = 0,
    source_dataset: str = "",
    is_featured: bool = False,
    synced_to_hf: bool = False,
    hf_path: str = "",
    hf_url: str = ""
) -> int:
    with get_db() as cursor:
        cursor.execute("""
            INSERT OR IGNORE INTO remote_images 
            (url, subcategory, display_name, character_name, anime_name, tags, phash,
             face_count, pixel_price, width, height, source_dataset, is_featured, synced_to_hf,
             hf_path, hf_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            url, subcategory, display_name, character_name, anime_name, tags, phash,
            face_count, pixel_price, width, height, source_dataset,
            1 if is_featured else 0, 1 if synced_to_hf else 0, hf_path, hf_url
        ))
        return cursor.lastrowid


def url_exists(url: str) -> bool:
    with get_db() as cursor:
        cursor.execute("SELECT 1 FROM remote_images WHERE url = ?", (url,))
        return cursor.fetchone() is not None


def get_all_phashes() -> List[Tuple[int, str]]:
    with get_db() as cursor:
        cursor.execute("SELECT id, phash FROM remote_images WHERE phash IS NOT NULL AND phash != ''")
        return [(row["id"], row["phash"]) for row in cursor.fetchall()]


def get_unsynced_images(limit: int = 100) -> List[Dict[str, Any]]:
    with get_db() as cursor:
        cursor.execute("""
            SELECT * FROM remote_images 
            WHERE synced_to_hf = 0 AND subcategory != 'rejected'
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]


def mark_as_synced(image_ids: List[int]) -> bool:
    if not image_ids:
        return True
    with get_db() as cursor:
        placeholders = ",".join("?" for _ in image_ids)
        cursor.execute(f"UPDATE remote_images SET synced_to_hf = 1 WHERE id IN ({placeholders})", image_ids)
        return True


def get_remote_images(
    subcategory: Optional[str] = None,
    character_name: Optional[str] = None,
    anime_name: Optional[str] = None,
    search_query: Optional[str] = None,
    is_featured: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0
) -> List[Dict[str, Any]]:
    with get_db() as cursor:
        conditions, params = [], []
        if subcategory:
            conditions.append("subcategory = ?")
            params.append(subcategory)
        if character_name:
            conditions.append("character_name LIKE ?")
            params.append(f"%{character_name}%")
        if anime_name:
            conditions.append("anime_name LIKE ?")
            params.append(f"%{anime_name}%")
        if is_featured is not None:
            conditions.append("is_featured = ?")
            params.append(1 if is_featured else 0)
        if search_query:
            conditions.append("(display_name LIKE ? OR tags LIKE ? OR character_name LIKE ? OR anime_name LIKE ?)")
            like_term = f"%{search_query}%"
            params.extend([like_term, like_term, like_term, like_term])

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        cursor.execute(f"""
            SELECT id, url, subcategory, character_name, anime_name, display_name,
                   tags, face_count, pixel_price, width, height, source_dataset, is_featured, created_at
            FROM remote_images
            {where_clause}
            ORDER BY is_featured DESC, RANDOM()
            LIMIT ? OFFSET ?
        """, (*params, limit, offset))

        return [dict(row) for row in cursor.fetchall()]


def get_featured_by_subcategory(subcategory: str, limit: int = 12) -> List[Dict[str, Any]]:
    return get_remote_images(subcategory=subcategory, is_featured=True, limit=limit)


def get_random_showcase(limit_per_category: int = 8) -> Dict[str, List[Dict[str, Any]]]:
    result = {}
    for subcat in ["anime_girls", "anime_boys", "anime_couples", "anime_scenery"]:
        result[subcat] = get_featured_by_subcategory(subcat, limit=limit_per_category)
    return result


def get_remote_image_by_id(image_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as cursor:
        cursor.execute("SELECT * FROM remote_images WHERE id = ?", (image_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_remote_image_featured(image_id: int, is_featured: bool) -> bool:
    with get_db() as cursor:
        cursor.execute("UPDATE remote_images SET is_featured = ? WHERE id = ?", (1 if is_featured else 0, image_id))
        return cursor.rowcount > 0


def get_remote_stats() -> Dict[str, Any]:
    with get_db() as cursor:
        cursor.execute("SELECT COUNT(*) as total FROM remote_images")
        total = cursor.fetchone()["total"]
        cursor.execute("SELECT COUNT(*) as featured FROM remote_images WHERE is_featured = 1")
        featured = cursor.fetchone()["featured"]
        cursor.execute("SELECT subcategory, COUNT(*) as count FROM remote_images GROUP BY subcategory")
        by_category = {row["subcategory"]: row["count"] for row in cursor.fetchall()}
        return {"total": total, "featured": featured, "by_category": by_category}


def get_db_stats() -> Dict[str, Any]:
    with get_db() as cursor:
        stats = {}
        for table in ["users", "auth_codes", "inventory", "remote_images"]:
            cursor.execute(f"SELECT COUNT(*) as count FROM {table}")
            stats[table] = cursor.fetchone()["count"]
        return stats


def get_all_records() -> List[Dict[str, Any]]:
    """Fetch all remote_images records for dataset creation."""
    with get_db() as cursor:
        cursor.execute("""
            SELECT 
                id, url, subcategory, display_name, character_name, anime_name,
                tags, phash, face_count, pixel_price, width, height, 
                source_dataset, is_featured, hf_path, hf_url, created_at
            FROM remote_images
            WHERE subcategory != 'rejected'
            ORDER BY created_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]


# ────────────────────────────────────────────────────────────
# OOP Wrapper Class for Worker Compatibility
# ────────────────────────────────────────────────────────────

class Database:
    """Class wrapper expected by ImageWorker (`from database import Database`)."""
    
    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            global DATABASE_PATH
            DATABASE_PATH = str(Path(db_path).resolve())
            Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
        init_db()

    def init_db(self) -> None:
        init_db()

    def url_exists(self, url: str) -> bool:
        return url_exists(url)

    def get_all_phashes(self) -> List[Tuple[int, str]]:
        return get_all_phashes()

    def insert(self, meta: Any) -> bool:
        url = getattr(meta, 'url', '') if not isinstance(meta, dict) else meta.get('url', '')
        subcat = getattr(meta, 'subcategory', 'general') if not isinstance(meta, dict) else meta.get('subcategory', 'general')
        display_name = getattr(meta, 'display_name', 'Unnamed') if not isinstance(meta, dict) else meta.get('display_name', 'Unnamed')
        char_name = getattr(meta, 'character_name', '') if not isinstance(meta, dict) else meta.get('character_name', '')
        anime_name = getattr(meta, 'anime_name', '') if not isinstance(meta, dict) else meta.get('anime_name', '')
        tags = getattr(meta, 'tags', '') if not isinstance(meta, dict) else meta.get('tags', '')
        phash = getattr(meta, 'phash', '') if not isinstance(meta, dict) else meta.get('phash', '')
        face_count = getattr(meta, 'face_count', 0) if not isinstance(meta, dict) else meta.get('face_count', 0)
        pixel_price = getattr(meta, 'pixel_price', 0.0) if not isinstance(meta, dict) else meta.get('pixel_price', 0.0)
        width = getattr(meta, 'width', 0) if not isinstance(meta, dict) else meta.get('width', 0)
        height = getattr(meta, 'height', 0) if not isinstance(meta, dict) else meta.get('height', 0)
        source_dataset = getattr(meta, 'source_dataset', '') if not isinstance(meta, dict) else meta.get('source_dataset', '')
        is_featured = getattr(meta, 'is_featured', False) if not isinstance(meta, dict) else meta.get('is_featured', False)
        hf_path = getattr(meta, 'hf_path', '') if not isinstance(meta, dict) else meta.get('hf_path', '')
        hf_url = getattr(meta, 'hf_url', '') if not isinstance(meta, dict) else meta.get('hf_url', '')

        res = add_remote_image(
            url=url, subcategory=subcat, display_name=display_name,
            character_name=char_name, anime_name=anime_name, tags=tags,
            phash=phash, face_count=face_count, pixel_price=pixel_price,
            width=width, height=height, source_dataset=source_dataset,
            is_featured=is_featured, hf_path=hf_path, hf_url=hf_url
        )
        return res is not None and res > 0

    def get_unsynced_images(self, limit: int = 100) -> List[Dict[str, Any]]:
        return get_unsynced_images(limit)

    def mark_as_synced(self, image_ids: List[int]) -> bool:
        return mark_as_synced(image_ids)

    def get_all_records(self) -> List[Dict[str, Any]]:
        """Fetch all records for dataset creation."""
        return get_all_records()

    def get_stats(self) -> Dict[str, Any]:
        return get_remote_stats()


if __name__ == "__main__":
    init_db()
    print("[DATABASE] 🏗️ Database initialized and ready.")
    print(f"[DATABASE] 📊 Stats: {get_db_stats()}")