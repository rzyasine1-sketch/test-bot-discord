#!/usr/bin/env python3
"""
database.py — SQLite Database Manager (Synchronous / Flask Side)
Production-grade SQLite wrapper with thread-safe connections,
connection pooling simulation via connection-per-thread,
and full CRUD operations for the Discord Bot Dashboard.

Schema unified with main_merged.py (aiosqlite async side) to ensure
full compatibility when both Bot and Flask access profiles.db simultaneously.
"""

import sqlite3
import threading
import secrets
import string
import os
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

# ═══════════════════════════════════════════════════════════
# FIX: Force SHARED absolute database path
# ═══════════════════════════════════════════════════════════
script_dir = Path(__file__).resolve().parent          # .../web/
project_root = script_dir.parent                       # .../bot-discord1/ (المجلد الأب المشترك)

# نحمل .env للمتغيرات الأخرى فقط
env_file = project_root / ".env"
if env_file.exists():
    load_dotenv(dotenv_path=str(env_file))

# ══ المسار المطلق المشترك — فوق مجلد bot/ و web/ ══
DATABASE_PATH = str(project_root / "profiles.db")

# نتأكد أن المجلد الأب موجود
Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)

print(f"[WEB] 📂 Database path: {DATABASE_PATH}")
print(f"[WEB] 📂 Absolute: {Path(DATABASE_PATH).resolve()}")


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
# Schema Initialization (Unified with async bot schema)
# ────────────────────────────────────────────────────────────

def init_db() -> None:
    """
    Initialize the database with all required tables.
    Safe to call multiple times — uses IF NOT EXISTS.
    """
    with get_db() as cursor:
        # Users table: core economy & progression
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id         TEXT PRIMARY KEY NOT NULL,
                coins           INTEGER NOT NULL DEFAULT 5000 CHECK(coins >= 0),
                xp              INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0),
                last_daily      TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Profiles table: active avatar, bio, country (unified with bot)
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

        # Auth codes table: single-use, time-bombed login tokens
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

        # Inventory table: purchased avatars & banners
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

        # Indexes for performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_inventory_user_id ON inventory(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_inventory_item_type ON inventory(user_id, item_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_codes_expires ON auth_codes(expires_at)")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS profile_ratings (
                user_id         TEXT NOT NULL,
                voter_id        TEXT NOT NULL,
                vote_type       TEXT NOT NULL CHECK(vote_type IN ('like', 'dislike')),
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, voter_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS profile_rating_posts (
                user_id         TEXT PRIMARY KEY NOT NULL,
                pending_post    INTEGER NOT NULL DEFAULT 0,
                state_timestamp TEXT,
                channel_id      TEXT,
                message_id      TEXT,
                thread_id       TEXT,
                posted_at       TEXT,
                last_error      TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_profile_ratings_user ON profile_ratings(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_profile_rating_posts_pending ON profile_rating_posts(pending_post)")

        _ensure_column(cursor, "profiles", "username", "TEXT")
        _ensure_column(cursor, "profiles", "display_name", "TEXT")
        _ensure_column(cursor, "profiles", "avatar_hash", "TEXT")
        _ensure_column(cursor, "profiles", "hide_balance", "INTEGER DEFAULT 0")
        _ensure_column(cursor, "profiles", "accent_theme", "TEXT DEFAULT 'default'")
        _ensure_column(cursor, "profiles", "show_toasts", "INTEGER DEFAULT 1")
        _ensure_column(cursor, "users", "likes", "INTEGER DEFAULT 0")
        _ensure_column(cursor, "users", "dislikes", "INTEGER DEFAULT 0")
        _ensure_column(cursor, "users", "last_profile_update", "TEXT")

    # Startup safety: clamp any historical negative values back to 0.
    # This is safe and idempotent, and prevents UI desync in case earlier versions allowed negatives.
    cleanup_negative_coin_balances()

    print("[DATABASE] ✅ Schema initialized successfully.")


def _ensure_column(cursor, table: str, column: str, definition: str) -> None:
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# ────────────────────────────────────────────────────────────
# User Operations
# ────────────────────────────────────────────────────────────

def get_or_create_user(user_id: str) -> Dict[str, Any]:
    """
    Fetch user by ID. If not found, create with default values.
    Returns: {user_id, coins, xp, created_at}
    """
    with get_db() as cursor:
        cursor.execute(
            "SELECT user_id, coins, xp, likes, dislikes, last_daily, last_profile_update, created_at FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()

        if row is None:
            cursor.execute(
                "INSERT INTO users (user_id, coins, xp) VALUES (?, ?, ?)",
                (user_id, DEFAULT_COINS, DEFAULT_XP)
            )
            return {
                "user_id": user_id,
                "coins": DEFAULT_COINS,
                "xp": DEFAULT_XP,
                "likes": 0,
                "dislikes": 0,
                "last_daily": None,
                "last_profile_update": None,
                "created_at": datetime.utcnow().isoformat()
            }

        return dict(row)


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch user by ID. Returns None if not found."""
    with get_db() as cursor:
        cursor.execute(
            "SELECT user_id, coins, xp, likes, dislikes, last_daily, last_profile_update, created_at FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def cleanup_negative_coin_balances() -> int:
    """Clamp any historical negative balances back to 0. Returns affected row count."""
    with get_db() as cursor:
        cursor.execute("UPDATE users SET coins = 0 WHERE coins < 0")
        return cursor.rowcount


def update_user_coins(user_id: str, delta: int) -> Optional[int]:
    """
    Atomically add/subtract coins from a user.
    delta can be negative (purchase) or positive (reward).

    Returns:
      - new_balance (int) on success
      - None on failure (e.g. user missing or insufficient funds for deductions)
    """
    if not isinstance(delta, int):
        try:
            delta = int(delta)
        except Exception:
            return None

    with get_db() as cursor:
        # Positive credits: no special constraints needed (schema already CHECK(coins >= 0)).
        if delta >= 0:
            cursor.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (delta, user_id))
            if cursor.rowcount == 0:
                return None
        else:
            # Negative delta: prevent negative balances atomically.
            # Example: delta = -price => coins = coins - price WHERE coins >= price
            required = -delta
            cursor.execute(
                "UPDATE users SET coins = coins + ? WHERE user_id = ? AND coins >= ?",
                (delta, user_id, required),
            )
            if cursor.rowcount == 0:
                return None

        cursor.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return int(row["coins"]) if row else None


def add_xp(user_id: str, amount: int) -> None:
    """Add XP to a user."""
    with get_db() as cursor:
        cursor.execute(
            "UPDATE users SET xp = xp + ? WHERE user_id = ?",
            (amount, user_id)
        )


def claim_daily_reward(user_id: str) -> Dict[str, Any]:
    """Claim daily coins (24h cooldown). Reward matches the Discord /daily range."""
    now = datetime.utcnow()
    reward = secrets.randbelow(1001) + 500
    with get_db() as cursor:
        cursor.execute(
            "SELECT coins, last_daily FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            return {"success": False, "message": "User not found."}

        last_daily_str = row["last_daily"]
        if last_daily_str:
            try:
                last_daily = datetime.fromisoformat(last_daily_str)
            except ValueError:
                last_daily = None
            if last_daily:
                elapsed = now - last_daily
                if elapsed < timedelta(hours=24):
                    remaining = timedelta(hours=24) - elapsed
                    hours, rem = divmod(int(remaining.total_seconds()), 3600)
                    minutes = rem // 60
                    return {
                        "success": False,
                        "on_cooldown": True,
                        "message": f"Already claimed. Come back in {hours}h {minutes}m.",
                        "hours": hours,
                        "minutes": minutes,
                        "coins": row["coins"],
                    }

        cursor.execute(
            "UPDATE users SET coins = coins + ?, last_daily = ? WHERE user_id = ?",
            (reward, now.isoformat(), user_id)
        )
        new_balance = row["coins"] + reward
        return {
            "success": True,
            "reward": reward,
            "coins": new_balance,
            "message": f"Claimed {reward:,} coins!",
        }


def get_profile(user_id: str) -> Dict[str, Any]:
    with get_db() as cursor:
        cursor.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return {
                "user_id": user_id,
                "username": "",
                "display_name": "",
                "active_avatar_url": "",
                "avatar_hash": "",
                "bio": "",
                "country": "",
                "hide_balance": 0,
                "accent_theme": "default",
                "show_toasts": 1,
            }
        data = dict(row)
        data.setdefault("username", "")
        data.setdefault("display_name", "")
        data.setdefault("hide_balance", 0)
        data.setdefault("accent_theme", "default")
        data.setdefault("show_toasts", 1)
        return data


def upsert_discord_identity(
    user_id: str,
    username: str = "",
    display_name: str = "",
    avatar_url: str = "",
    avatar_hash: str = "",
) -> None:
    with get_db() as cursor:
        cursor.execute("SELECT user_id FROM profiles WHERE user_id = ?", (user_id,))
        exists = cursor.fetchone() is not None
        if exists:
            cursor.execute(
                """
                UPDATE profiles
                SET username = ?, display_name = ?, active_avatar_url = ?, avatar_hash = ?
                WHERE user_id = ?
                """,
                (username, display_name, avatar_url, avatar_hash, user_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO profiles (user_id, username, display_name, active_avatar_url, avatar_hash, bio)
                VALUES (?, ?, ?, ?, ?, '')
                """,
                (user_id, username, display_name, avatar_url, avatar_hash),
            )


def update_user_settings(user_id: str, bio: Optional[str] = None, hide_balance: Optional[bool] = None,
                         accent_theme: Optional[str] = None, show_toasts: Optional[bool] = None) -> Dict[str, Any]:
    allowed_themes = {"default", "purple", "green", "crimson"}
    with get_db() as cursor:
        cursor.execute("SELECT user_id FROM profiles WHERE user_id = ?", (user_id,))
        if cursor.fetchone() is None:
            cursor.execute(
                "INSERT INTO profiles (user_id, bio) VALUES (?, '')",
                (user_id,)
            )

        if bio is not None:
            cursor.execute("UPDATE profiles SET bio = ? WHERE user_id = ?", (bio[:280], user_id))
        if hide_balance is not None:
            cursor.execute(
                "UPDATE profiles SET hide_balance = ? WHERE user_id = ?",
                (1 if hide_balance else 0, user_id),
            )
        if accent_theme is not None and accent_theme in allowed_themes:
            cursor.execute(
                "UPDATE profiles SET accent_theme = ? WHERE user_id = ?",
                (accent_theme, user_id),
            )
        if show_toasts is not None:
            cursor.execute(
                "UPDATE profiles SET show_toasts = ? WHERE user_id = ?",
                (1 if show_toasts else 0, user_id),
            )
    return get_profile(user_id)


def get_user_bundle(user_id: str) -> Optional[Dict[str, Any]]:
    user = get_user(user_id)
    if not user:
        return None
    profile = get_profile(user_id)
    user.update({
        "username": profile.get("username") or "",
        "display_name": profile.get("display_name") or "",
        "avatar_url": profile.get("active_avatar_url") or "",
        "bio": profile.get("bio") or "",
        "hide_balance": bool(profile.get("hide_balance")),
        "accent_theme": profile.get("accent_theme") or "default",
        "show_toasts": bool(profile.get("show_toasts", 1)),
        "likes": int(user.get("likes", 0) or 0),
        "dislikes": int(user.get("dislikes", 0) or 0),
        "net_score": int(user.get("likes", 0) or 0) - int(user.get("dislikes", 0) or 0),
        "last_profile_update": user.get("last_profile_update"),
    })
    return user


def get_rarity_tier_for_score(net_score: int) -> Dict[str, Any]:
    if net_score >= 10:
        return {"key": "mythic", "label": "Mythic", "tier": 4}
    if net_score >= 6:
        return {"key": "epic", "label": "Epic", "tier": 3}
    if net_score >= 3:
        return {"key": "rare", "label": "Rare", "tier": 2}
    return {"key": "common", "label": "Common", "tier": 1}


def get_profile_rating_summary(user_id: str) -> Dict[str, Any]:
    user = get_user(user_id) or {
        "likes": 0,
        "dislikes": 0,
        "last_profile_update": None,
    }
    likes = int(user.get("likes", 0) or 0)
    dislikes = int(user.get("dislikes", 0) or 0)
    net_score = likes - dislikes
    return {
        "likes": likes,
        "dislikes": dislikes,
        "net_score": net_score,
        "last_profile_update": user.get("last_profile_update"),
        "rarity": get_rarity_tier_for_score(net_score),
    }


def queue_profile_rating_post(user_id: str, cooldown_seconds: int = 3600) -> Dict[str, Any]:
    now = datetime.utcnow()
    with get_db() as cursor:
        cursor.execute(
            "SELECT last_profile_update FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        last_update_raw = row["last_profile_update"] if row else None

        if last_update_raw:
            try:
                last_update = datetime.fromisoformat(last_update_raw)
            except ValueError:
                last_update = None
            if last_update:
                elapsed = (now - last_update).total_seconds()
                if elapsed < cooldown_seconds:
                    remaining = max(0, int(cooldown_seconds - elapsed))
                    return {
                        "queued": False,
                        "on_cooldown": True,
                        "remaining_seconds": remaining,
                        "message": "Profile rating cooldown is still active.",
                    }

        cursor.execute(
            "UPDATE users SET last_profile_update = ? WHERE user_id = ?",
            (now.isoformat(), user_id),
        )
        cursor.execute("DELETE FROM profile_ratings WHERE user_id = ?", (user_id,))
        cursor.execute(
            """
            INSERT INTO profile_rating_posts (user_id, pending_post, state_timestamp, channel_id, message_id, thread_id, posted_at, last_error)
            VALUES (?, 1, ?, NULL, NULL, NULL, NULL, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                pending_post = 1,
                state_timestamp = excluded.state_timestamp,
                channel_id = NULL,
                message_id = NULL,
                thread_id = NULL,
                posted_at = NULL,
                last_error = NULL
            """,
            (user_id, now.isoformat()),
        )

    return {
        "queued": True,
        "on_cooldown": False,
        "remaining_seconds": 0,
        "message": "Profile queued for rating-room posting.",
        "queued_at": now.isoformat(),
    }


def register_profile_vote(user_id: str, voter_id: str, vote_type: str) -> Dict[str, Any]:
    if vote_type not in {"like", "dislike"}:
        return {"success": False, "message": "Invalid vote type."}
    if user_id == voter_id:
        return {"success": False, "message": "You cannot vote on your own profile."}

    with get_db() as cursor:
        cursor.execute(
            "SELECT vote_type FROM profile_ratings WHERE user_id = ? AND voter_id = ?",
            (user_id, voter_id),
        )
        existing = cursor.fetchone()

        if not existing:
            cursor.execute(
                "INSERT INTO profile_ratings (user_id, voter_id, vote_type) VALUES (?, ?, ?)",
                (user_id, voter_id, vote_type),
            )
            col = "likes" if vote_type == "like" else "dislikes"
            cursor.execute(f"UPDATE users SET {col} = COALESCE({col}, 0) + 1 WHERE user_id = ?", (user_id,))
            action = "added"
            msg = f"✅ You {'liked' if vote_type == 'like' else 'disliked'} this profile!"
        elif existing["vote_type"] == vote_type:
            # Same vote -> Toggle off (remove vote)
            cursor.execute(
                "DELETE FROM profile_ratings WHERE user_id = ? AND voter_id = ?",
                (user_id, voter_id),
            )
            col = "likes" if vote_type == "like" else "dislikes"
            cursor.execute(f"UPDATE users SET {col} = MAX(0, COALESCE({col}, 0) - 1) WHERE user_id = ?", (user_id,))
            action = "removed"
            msg = f"↩️ You removed your {vote_type}."
        else:
            # Switch vote
            old_col = "dislikes" if vote_type == "like" else "likes"
            new_col = "likes" if vote_type == "like" else "dislikes"
            cursor.execute(
                "UPDATE profile_ratings SET vote_type = ? WHERE user_id = ? AND voter_id = ?",
                (vote_type, user_id, voter_id),
            )
            cursor.execute(
                f"UPDATE users SET {old_col} = MAX(0, COALESCE({old_col}, 0) - 1), {new_col} = COALESCE({new_col}, 0) + 1 WHERE user_id = ?",
                (user_id,),
            )
            action = "switched"
            msg = f"🔄 You changed your vote to {vote_type.capitalize()}!"

        cursor.execute("SELECT likes, dislikes FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()

    likes = int(row["likes"] if row else 0)
    dislikes = int(row["dislikes"] if row else 0)
    net_score = likes - dislikes
    return {
        "success": True,
        "action": action,
        "message": msg,
        "likes": likes,
        "dislikes": dislikes,
        "net_score": net_score,
        "rarity": get_rarity_tier_for_score(net_score),
    }


# ────────────────────────────────────────────────────────────
# Auth Code Operations
# ────────────────────────────────────────────────────────────

def generate_login_code(user_id: str) -> str:
    """
    Generate a cryptographically secure 6-character login code.
    Stores it with a 5-minute expiration.
    Returns the generated code.
    """
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
    """
    Validate a login code. If valid and not expired:
      - Returns the associated user_id
      - DELETES the code (single-use)
    If invalid or expired: returns None.
    """
    with get_db() as cursor:
        cursor.execute("""
            SELECT user_id, expires_at FROM auth_codes WHERE login_code = ?
        """, (code,))
        row = cursor.fetchone()

        if row is None:
            return None

        expires_at = datetime.fromisoformat(row["expires_at"])
        if datetime.utcnow() > expires_at:
            # Expired — clean it up
            cursor.execute("DELETE FROM auth_codes WHERE login_code = ?", (code,))
            return None

        user_id = row["user_id"]
        # Single-use: delete immediately upon validation
        cursor.execute("DELETE FROM auth_codes WHERE login_code = ?", (code,))
        return user_id


def cleanup_expired_codes() -> int:
    """Delete all expired auth codes. Returns count of deleted rows."""
    with get_db() as cursor:
        cursor.execute("""
            DELETE FROM auth_codes WHERE expires_at < datetime('now')
        """)
        return cursor.rowcount


# ────────────────────────────────────────────────────────────
# Inventory Operations
# ────────────────────────────────────────────────────────────

def add_inventory_item(user_id: str, item_type: str, file_path: str) -> int:
    """
    Add an item to a user's inventory.
    Returns the auto-generated inventory ID.
    """
    with get_db() as cursor:
        cursor.execute("""
            INSERT INTO inventory (user_id, item_type, local_file_path)
            VALUES (?, ?, ?)
        """, (user_id, item_type, file_path))
        return cursor.lastrowid


def get_user_inventory(user_id: str) -> List[Dict[str, Any]]:
    """Get all inventory items for a user."""
    with get_db() as cursor:
        cursor.execute("""
            SELECT id, item_type, local_file_path, purchased_at
            FROM inventory WHERE user_id = ? ORDER BY purchased_at DESC
        """, (user_id,))
        return [dict(row) for row in cursor.fetchall()]


def get_inventory_count(user_id: str, item_type: Optional[str] = None) -> int:
    """Count inventory items for a user, optionally filtered by type."""
    with get_db() as cursor:
        if item_type:
            cursor.execute("""
                SELECT COUNT(*) as count FROM inventory
                WHERE user_id = ? AND item_type = ?
            """, (user_id, item_type))
        else:
            cursor.execute("""
                SELECT COUNT(*) as count FROM inventory WHERE user_id = ?
            """, (user_id,))
        return cursor.fetchone()["count"]


# ────────────────────────────────────────────────────────────
# Diagnostics
# ────────────────────────────────────────────────────────────

def get_db_stats() -> Dict[str, Any]:
    """Return database statistics for monitoring."""
    with get_db() as cursor:
        stats = {}
        for table in ["users", "auth_codes", "inventory"]:
            cursor.execute(f"SELECT COUNT(*) as count FROM {table}")
            stats[table] = cursor.fetchone()["count"]
        return stats


# ────────────────────────────────────────────────────────────
# Module Entry Point
# ────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────
# Remote Image Operations (Cloud-hosted, no local files)
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
    is_featured: bool = False
) -> int:
    """Add a cloud-hosted image to the remote catalog. Returns the image ID."""
    with get_db() as cursor:
        cursor.execute("""
            INSERT INTO remote_images 
            (url, subcategory, display_name, character_name, anime_name, tags, phash,
             face_count, pixel_price, width, height, source_dataset, is_featured)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            url, subcategory, display_name, character_name, anime_name, tags, phash,
            face_count, pixel_price, width, height, source_dataset, 1 if is_featured else 0
        ))
        return cursor.lastrowid


def get_remote_images(
    subcategory: Optional[str] = None,
    character_name: Optional[str] = None,
    anime_name: Optional[str] = None,
    search_query: Optional[str] = None,
    is_featured: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """Query remote images with optional filters."""
    with get_db() as cursor:
        conditions = []
        params = []

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
    """Get featured images for a specific subcategory."""
    return get_remote_images(subcategory=subcategory, is_featured=True, limit=limit)


def get_random_showcase(limit_per_category: int = 8) -> Dict[str, List[Dict[str, Any]]]:
    """Get random featured images grouped by subcategory for the shop showcase."""
    result = {}
    for subcat in ["anime_girls", "anime_boys", "anime_couples", "anime_scenery"]:
        result[subcat] = get_featured_by_subcategory(subcat, limit=limit_per_category)
    return result


def get_remote_image_by_id(image_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single remote image by ID."""
    with get_db() as cursor:
        cursor.execute("SELECT * FROM remote_images WHERE id = ?", (image_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_remote_image_featured(image_id: int, is_featured: bool) -> bool:
    """Toggle featured status for a remote image."""
    with get_db() as cursor:
        cursor.execute(
            "UPDATE remote_images SET is_featured = ? WHERE id = ?",
            (1 if is_featured else 0, image_id)
        )
        return cursor.rowcount > 0


def get_remote_stats() -> Dict[str, Any]:
    """Statistics for remote images."""
    with get_db() as cursor:
        cursor.execute("SELECT COUNT(*) as total FROM remote_images")
        total = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) as featured FROM remote_images WHERE is_featured = 1")
        featured = cursor.fetchone()["featured"]

        cursor.execute("SELECT subcategory, COUNT(*) as count FROM remote_images GROUP BY subcategory")
        by_category = {row["subcategory"]: row["count"] for row in cursor.fetchall()}

        return {"total": total, "featured": featured, "by_category": by_category}


if __name__ == "__main__":
    init_db()
    print("[DATABASE] 🏗️ Database initialized and ready.")
    print(f"[DATABASE] 📊 Stats: {get_db_stats()}")