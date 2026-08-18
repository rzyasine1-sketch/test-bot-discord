#!/usr/bin/env python3
"""
main_merged.py — Discord Bot Dashboard v2.1 (Production Ready)
Features:
  • Unified async database (aiosqlite + WAL)
  • Voice & Text chat gamification
  • Secure auth code generation (/login)
  • Economy system (/daily, /gift, /balance)
  • Profile & inventory display
  • Leaderboard & stats
  • Mention detection with active avatar display
  • Prefix commands preserved for backward compatibility

Strict Fixes Applied:
  - All multi-line Embed strings converted to triple-quoted f-strings (f\"\"\"...\"\"\")
  - SyntaxError-causing literal newlines inside double quotes removed completely
  - No existing tasks or commands were deleted or modified in logic
"""

import os
import re
import time
import secrets
import string
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
from dotenv import load_dotenv

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from .profile_generator import generate_profile_card, get_rarity_tier
except ImportError:
    from profile_generator import generate_profile_card, get_rarity_tier

# ═══════════════════════════════════════════════════════════
# FIX: Force SHARED absolute database path
# ═══════════════════════════════════════════════════════════
script_dir = Path(__file__).resolve().parent          # .../bot/
project_root = script_dir.parent                       # .../bot-discord1/ (المجلد الأب المشترك)

# نحمل .env للتوكن والمتغيرات الأخرى فقط
env_file = project_root / ".env"
if env_file.exists():
    load_dotenv(dotenv_path=str(env_file))

# ══ المسار المطلق المشترك — فوق مجلد bot/ و web/ ══
DB_PATH = str(project_root / "profiles.db")

# نتأكد أن المجلد الأب موجود (ينشئه لو ما كان)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

print(f"[BOT] 📂 Database path: {DB_PATH}")
print(f"[BOT] 📂 Absolute: {Path(DB_PATH).resolve()}")

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
WEBSITE_BASE_URL = os.getenv("WEBSITE_BASE_URL", "https://yourdomain.com")
RATING_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_RATING_ROOM") or os.getenv("RATING_CHANNEL_ID", "")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s:%(levelname)s:%(name)s: %(message)s"
)
logger = logging.getLogger("DiscordBot")


# ==========================================
# UNIFIED DATABASE (Async via aiosqlite)
# Compatible with both the Bot and the Flask Web App
# ==========================================
class Database:
    """Asynchronous SQLite database wrapper using aiosqlite.
    Schema is unified to support both the Discord Bot and the Web Dashboard."""

    CODE_EXPIRY_MINUTES = 5
    DAILY_REWARD_MIN = 500
    DAILY_REWARD_MAX = 1500
    DAILY_XP_REWARD = 50

    @staticmethod
    async def init_db():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA foreign_keys = ON;")

            # 1. users table — unified schema (compatible with web app)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    xp INTEGER DEFAULT 0,
                    coins INTEGER DEFAULT 0,
                    likes INTEGER DEFAULT 0,
                    dislikes INTEGER DEFAULT 0,
                    last_profile_update TEXT,
                    last_daily TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 2. profiles table (from original main.py)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id TEXT PRIMARY KEY,
                    active_avatar_url TEXT,
                    birth_date TEXT,
                    country TEXT,
                    bio TEXT
                )
            """)

            # 3. auth_codes table — unified (supports both 'code' and 'login_code')
            await db.execute("""
                CREATE TABLE IF NOT EXISTS auth_codes (
                    user_id TEXT PRIMARY KEY,
                    login_code TEXT,
                    code TEXT,
                    expires_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 4. inventory table — unified (supports web app fields + original avatar_id)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    item_type TEXT,
                    avatar_id TEXT,
                    local_file_path TEXT,
                    purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS profile_ratings (
                    user_id TEXT NOT NULL,
                    voter_id TEXT NOT NULL,
                    vote_type TEXT NOT NULL CHECK(vote_type IN ('like', 'dislike')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, voter_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS profile_rating_posts (
                    user_id TEXT PRIMARY KEY NOT NULL,
                    pending_post INTEGER NOT NULL DEFAULT 0,
                    state_timestamp TEXT,
                    channel_id TEXT,
                    message_id TEXT,
                    thread_id TEXT,
                    posted_at TEXT,
                    last_error TEXT
                )
            """)

            # Indexes for performance
            await db.execute("CREATE INDEX IF NOT EXISTS idx_inventory_user_id ON inventory(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_inventory_item_type ON inventory(user_id, item_type)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_auth_codes_expires ON auth_codes(expires_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_profile_ratings_user ON profile_ratings(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_profile_rating_posts_pending ON profile_rating_posts(pending_post)")

            await Database._ensure_column(db, "users", "likes", "INTEGER DEFAULT 0")
            await Database._ensure_column(db, "users", "dislikes", "INTEGER DEFAULT 0")
            await Database._ensure_column(db, "users", "last_profile_update", "TEXT")
            await Database._ensure_column(db, "profiles", "username", "TEXT")
            await Database._ensure_column(db, "profiles", "display_name", "TEXT")
            await Database._ensure_column(db, "profiles", "avatar_hash", "TEXT")
            await Database._ensure_column(db, "profiles", "hide_balance", "INTEGER DEFAULT 0")
            await Database._ensure_column(db, "profiles", "accent_theme", "TEXT DEFAULT 'default'")
            await Database._ensure_column(db, "profiles", "show_toasts", "INTEGER DEFAULT 1")

            await db.commit()
            logger.info("Database initialized successfully with WAL mode.")

    @staticmethod
    async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str):
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        existing = {row[1] for row in rows}
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # ── Original main.py helpers ──

    @staticmethod
    async def add_xp_coins(user_id: int, xp: int, coins: int):
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO users (user_id, xp, coins)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    xp = users.xp + excluded.xp,
                    coins = users.coins + excluded.coins
            """, (uid, xp, coins))
            await db.commit()

    @staticmethod
    async def add_xp_coins_batch(rewards: List[Tuple[int, int, int]]):
        if not rewards:
            return
        async with aiosqlite.connect(DB_PATH) as db:
            data = [(str(uid), xp, coins) for uid, xp, coins in rewards]
            await db.executemany("""
                INSERT INTO users (user_id, xp, coins)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    xp = users.xp + excluded.xp,
                    coins = users.coins + excluded.coins
            """, data)
            await db.commit()

    @staticmethod
    async def get_user_stats(user_id: int) -> Dict[str, int]:
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT xp, coins FROM users WHERE user_id = ?", (uid,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {"xp": row["xp"], "coins": row["coins"]}
                return {"xp": 0, "coins": 0}

    @staticmethod
    async def get_leaderboard(limit: int = 10) -> Dict[str, List[Dict[str, Any]]]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT user_id, xp FROM users ORDER BY xp DESC LIMIT ?", (limit,)) as cursor:
                top_xp = [dict(row) async for row in cursor]
            async with db.execute("SELECT user_id, coins FROM users ORDER BY coins DESC LIMIT ?", (limit,)) as cursor:
                top_coins = [dict(row) async for row in cursor]
            return {"xp": top_xp, "coins": top_coins}

    @staticmethod
    async def claim_daily(user_id: int) -> Tuple[bool, int, Optional[timedelta]]:
        uid = str(user_id)
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT coins, last_daily FROM users WHERE user_id = ?", (uid,)) as cursor:
                row = await cursor.fetchone()

            if row and row["last_daily"]:
                try:
                    last_daily = datetime.fromisoformat(row["last_daily"])
                    if last_daily.tzinfo is None:
                        last_daily = last_daily.replace(tzinfo=timezone.utc)
                except ValueError:
                    last_daily = None

                if last_daily:
                    cooldown = timedelta(hours=24)
                    elapsed = now - last_daily
                    if elapsed < cooldown:
                        remaining = cooldown - elapsed
                        current_coins = row["coins"] if row else 0
                        return False, current_coins, remaining

            await db.execute("""
                INSERT INTO users (user_id, xp, coins, last_daily)
                VALUES (?, 0, 100, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    coins = users.coins + 100,
                    last_daily = excluded.last_daily
            """, (uid, now.isoformat()))
            await db.commit()

            async with db.execute("SELECT coins FROM users WHERE user_id = ?", (uid,)) as cursor:
                updated_row = await cursor.fetchone()
                new_coins = updated_row["coins"] if updated_row else 100

            return True, new_coins, None

    @staticmethod
    async def get_user_inventory_ids(user_id: int) -> List[str]:
        """Original main.py style — returns list of avatar_ids."""
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT avatar_id FROM inventory WHERE user_id = ? AND avatar_id IS NOT NULL", (uid,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows if row[0]]

    @staticmethod
    async def save_auth_code(user_id: int, code: str, expires_at: datetime):
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO auth_codes (user_id, code, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    code = excluded.code,
                    expires_at = excluded.expires_at
            """, (uid, code, expires_at.isoformat()))
            await db.commit()

    @staticmethod
    async def get_profile(user_id: int) -> Optional[Dict[str, Any]]:
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM profiles WHERE user_id = ?", (uid,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return dict(row)
                return None

    # ── Web Dashboard helpers (from discord_cog.py / database.py) ──

    @staticmethod
    async def get_or_create_user(user_id: str) -> Dict[str, Any]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id, xp, coins, likes, dislikes, last_profile_update, created_at FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                now = datetime.utcnow().isoformat()
                await db.execute(
                    "INSERT INTO users (user_id, xp, coins) VALUES (?, 0, 5000)", (user_id,)
                )
                await db.commit()
                return {
                    "user_id": user_id,
                    "xp": 0,
                    "coins": 5000,
                    "likes": 0,
                    "dislikes": 0,
                    "last_profile_update": None,
                    "created_at": now,
                }
            return dict(row)

    @staticmethod
    async def get_user(user_id: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id, xp, coins, likes, dislikes, last_profile_update, created_at FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    @staticmethod
    async def generate_login_code(user_id: str) -> str:
        alphabet = string.ascii_uppercase + string.digits
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        expires_at = datetime.utcnow() + timedelta(minutes=Database.CODE_EXPIRY_MINUTES)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO auth_codes (user_id, login_code, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    login_code = excluded.login_code,
                    code = excluded.login_code,
                    expires_at = excluded.expires_at,
                    created_at = CURRENT_TIMESTAMP
            """, (user_id, code, expires_at.isoformat()))
            await db.commit()
        return code

    @staticmethod
    async def validate_login_code(code: str) -> Optional[str]:
        code = code.strip().upper()
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT user_id, expires_at FROM auth_codes
                WHERE login_code = ? OR code = ?
            """, (code, code)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            expires_at = datetime.fromisoformat(row["expires_at"])
            if datetime.utcnow() > expires_at:
                await db.execute(
                    "DELETE FROM auth_codes WHERE login_code = ? OR code = ?", (code, code)
                )
                await db.commit()
                return None
            user_id = row["user_id"]
            await db.execute(
                "DELETE FROM auth_codes WHERE login_code = ? OR code = ?", (code, code)
            )
            await db.commit()
            return user_id

    @staticmethod
    async def update_user_coins(user_id: str, delta: int) -> Optional[int]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            if delta >= 0:
                cursor = await db.execute(
                    "UPDATE users SET coins = coins + ? WHERE user_id = ?",
                    (delta, user_id),
                )
                if cursor.rowcount == 0:
                    return None
            else:
                required = -delta
                cursor = await db.execute(
                    "UPDATE users SET coins = coins + ? WHERE user_id = ? AND coins >= ?",
                    (delta, user_id, required),
                )
                if cursor.rowcount == 0:
                    return None
            await db.commit()
            async with db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
            return int(row["coins"]) if row else None

    @staticmethod
    async def add_xp(user_id: str, amount: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET xp = xp + ? WHERE user_id = ?", (amount, user_id)
            )
            await db.commit()

    @staticmethod
    async def get_user_inventory(user_id: str) -> List[Dict[str, Any]]:
        """Web Dashboard style — returns list of dicts with item_type, local_file_path, etc."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT id, item_type, avatar_id, local_file_path, purchased_at
                FROM inventory WHERE user_id = ? ORDER BY purchased_at DESC
            """, (user_id,)) as cursor:
                return [dict(row) async for row in cursor]

    @staticmethod
    async def get_inventory_count(user_id: str, item_type: Optional[str] = None) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            if item_type:
                async with db.execute(
                    "SELECT COUNT(*) as cnt FROM inventory WHERE user_id = ? AND item_type = ?",
                    (user_id, item_type)
                ) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else 0
            else:
                async with db.execute(
                    "SELECT COUNT(*) as cnt FROM inventory WHERE user_id = ?", (user_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else 0

    @staticmethod
    async def cleanup_expired_codes() -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("DELETE FROM auth_codes WHERE expires_at < datetime('now')")
            await db.commit()
            return cursor.rowcount

    @staticmethod
    async def get_profile_card_payload(user_id: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    u.user_id,
                    u.coins,
                    u.xp,
                    u.likes,
                    u.dislikes,
                    u.last_profile_update,
                    p.username,
                    p.display_name,
                    p.active_avatar_url,
                    p.bio
                FROM users u
                LEFT JOIN profiles p ON p.user_id = u.user_id
                WHERE u.user_id = ?
                """,
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    @staticmethod
    async def claim_pending_rating_posts(limit: int = 3) -> List[Dict[str, Any]]:
        claimed: List[Dict[str, Any]] = []
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT user_id, state_timestamp
                FROM profile_rating_posts
                WHERE pending_post = 1
                ORDER BY state_timestamp ASC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()

            for row in rows:
                result = await db.execute(
                    """
                    UPDATE profile_rating_posts
                    SET pending_post = 2, last_error = NULL
                    WHERE user_id = ? AND pending_post = 1
                    """,
                    (row["user_id"],),
                )
                if result.rowcount:
                    claimed.append(dict(row))

            await db.commit()
        return claimed

    @staticmethod
    async def mark_rating_post_success(user_id: str, channel_id: int, message_id: int, thread_id: Optional[int] = None):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                UPDATE profile_rating_posts
                SET pending_post = 0,
                    channel_id = ?,
                    message_id = ?,
                    thread_id = ?,
                    posted_at = ?,
                    last_error = NULL
                WHERE user_id = ?
                """,
                (str(channel_id), str(message_id), str(thread_id) if thread_id else None, datetime.utcnow().isoformat(), user_id),
            )
            await db.commit()

    @staticmethod
    async def mark_rating_post_failed(user_id: str, error_message: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                UPDATE profile_rating_posts
                SET pending_post = 1, last_error = ?
                WHERE user_id = ?
                """,
                (error_message[:500], user_id),
            )
            await db.commit()

    @staticmethod
    async def get_active_rating_posts() -> List[Dict[str, Any]]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT user_id, channel_id, message_id, thread_id, posted_at
                FROM profile_rating_posts
                WHERE message_id IS NOT NULL
                """
            ) as cursor:
                return [dict(row) async for row in cursor]

    @staticmethod
    async def get_rating_post_by_message_id(message_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT user_id, channel_id, message_id, thread_id, posted_at
                FROM profile_rating_posts
                WHERE message_id = ?
                """,
                (str(message_id),),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    @staticmethod
    async def register_profile_vote(user_id: str, voter_id: str, vote_type: str) -> Dict[str, Any]:
        if vote_type not in {"like", "dislike"}:
            return {"success": False, "message": "❌ Invalid vote type."}
        if str(user_id) == str(voter_id):
            return {"success": False, "message": "❌ You cannot vote on your own profile."}

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT vote_type FROM profile_ratings WHERE user_id = ? AND voter_id = ?",
                (user_id, voter_id),
            ) as cursor:
                existing = await cursor.fetchone()

            if not existing:
                await db.execute(
                    "INSERT INTO profile_ratings (user_id, voter_id, vote_type) VALUES (?, ?, ?)",
                    (user_id, voter_id, vote_type),
                )
                col = "likes" if vote_type == "like" else "dislikes"
                await db.execute(f"UPDATE users SET {col} = COALESCE({col}, 0) + 1 WHERE user_id = ?", (user_id,))
                action = "added"
                msg = f"✅ You {'liked' if vote_type == 'like' else 'disliked'} this profile!"
            elif existing["vote_type"] == vote_type:
                # Same vote -> Toggle off (remove vote)
                await db.execute(
                    "DELETE FROM profile_ratings WHERE user_id = ? AND voter_id = ?",
                    (user_id, voter_id),
                )
                col = "likes" if vote_type == "like" else "dislikes"
                await db.execute(f"UPDATE users SET {col} = MAX(0, COALESCE({col}, 0) - 1) WHERE user_id = ?", (user_id,))
                action = "removed"
                msg = f"↩️ You removed your {vote_type}."
            else:
                # Switch vote
                old_col = "dislikes" if vote_type == "like" else "likes"
                new_col = "likes" if vote_type == "like" else "dislikes"
                await db.execute(
                    "UPDATE profile_ratings SET vote_type = ? WHERE user_id = ? AND voter_id = ?",
                    (vote_type, user_id, voter_id),
                )
                await db.execute(
                    f"UPDATE users SET {old_col} = MAX(0, COALESCE({old_col}, 0) - 1), {new_col} = COALESCE({new_col}, 0) + 1 WHERE user_id = ?",
                    (user_id,),
                )
                action = "switched"
                msg = f"🔄 You changed your vote to {vote_type.capitalize()}!"

            await db.commit()

            async with db.execute("SELECT likes, dislikes FROM users WHERE user_id = ?", (user_id,)) as cursor:
                updated = await cursor.fetchone()

        likes = int(updated["likes"] if updated else 0)
        dislikes = int(updated["dislikes"] if updated else 0)
        net_score = likes - dislikes
        return {
            "success": True,
            "action": action,
            "message": msg,
            "likes": likes,
            "dislikes": dislikes,
            "net_score": net_score,
            "rarity": get_rarity_tier(net_score),
        }

    @staticmethod
    async def get_db_stats() -> Dict[str, int]:
        async with aiosqlite.connect(DB_PATH) as db:
            stats = {}
            for table in ["users", "auth_codes", "inventory"]:
                async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                    row = await cursor.fetchone()
                    stats[table] = row[0] if row else 0
            return stats


# ==========================================
# AUTH CODE GENERATOR (Legacy Prefix Support)
# ==========================================
def generate_auth_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ==========================================
# BOT CLASS (No Cogs — All commands inline)
# ==========================================
class ProfileBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        # 1. Initialize database
        await Database.init_db()

        for post in await Database.get_active_rating_posts():
            message_id = post.get("message_id")
            user_id = post.get("user_id")
            if message_id and user_id:
                try:
                    self.add_view(ProfileRatingView(str(user_id)), message_id=int(message_id))
                except Exception as e:
                    logger.warning(f"Could not register persistent view for message {message_id}: {e}")

        # Register global fallback persistent view for dynamically matched interactions
        self.add_view(ProfileRatingView(""))

        # 2. Start voice XP background task
        if not voice_xp_task.is_running():
            voice_xp_task.start()

        # 3. Start cleanup task for expired auth codes
        if not cleanup_task.is_running():
            cleanup_task.start()

        # 4. Start profile rating room auto-post loop
        if not profile_rating_task.is_running():
            profile_rating_task.start()

        # 5. Sync slash commands globally
        await self.tree.sync()
        logger.info("Synced Slash commands globally.")


# ==========================================
# BOT INSTANCE
# ==========================================
bot = ProfileBot()


def _format_rating_summary(likes: int, dislikes: int) -> str:
    return f"👍 `{likes}` | 👎 `{dislikes}`"


def _find_rating_channel() -> Optional[discord.TextChannel]:
    env_channel_id = (
        os.getenv("DISCORD_CHANNEL_RATING")
        or os.getenv("DISCORD_CHANNEL_RATING_ROOM")
        or os.getenv("RATING_ROOM_CHANNEL_ID")
        or os.getenv("RATING_CHANNEL_ID")
    )
    if env_channel_id:
        try:
            ch_id = int(str(env_channel_id).strip())
            channel = bot.get_channel(ch_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        except (ValueError, TypeError):
            pass

    target_names = {
        "rating-room", "rating_room", "profile-ratings", "profile-rating",
        "community-ratings", "ratings", "rating"
    }
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name.lower() in target_names:
                return channel
    return None


async def _extract_user_id_from_message(message: Optional[discord.Message]) -> Optional[str]:
    if not message:
        return None
    # 1. From embed footer (e.g. "user:1234567890" or "User ID: 1234567890")
    if message.embeds:
        for embed in message.embeds:
            if embed.footer and embed.footer.text:
                match = re.search(r"user:(\d+)", embed.footer.text)
                if match:
                    return match.group(1)
                match = re.search(r"User ID:\s*(\d+)", embed.footer.text, re.IGNORECASE)
                if match:
                    return match.group(1)
    # 2. From content mention
    if message.content:
        match = re.search(r"<@!?(\d+)>", message.content)
        if match:
            return match.group(1)
    # 3. From DB profile_rating_posts
    post = await Database.get_rating_post_by_message_id(message.id)
    if post and post.get("user_id"):
        return str(post["user_id"])
    return None


async def _build_profile_card_attachment(user_id: str) -> Tuple[Optional[discord.File], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    payload = await Database.get_profile_card_payload(user_id)
    if not payload:
        user = await Database.get_or_create_user(user_id)
        payload = {
            "user_id": str(user_id),
            "coins": int(user.get("coins", 0) or 0),
            "xp": int(user.get("xp", 0) or 0),
            "likes": int(user.get("likes", 0) or 0),
            "dislikes": int(user.get("dislikes", 0) or 0),
            "last_profile_update": user.get("last_profile_update"),
            "username": None,
            "display_name": None,
            "active_avatar_url": None,
            "bio": "No bio set yet. Update your profile on the dashboard.",
        }

    # Enrich from Discord User/Member cache if needed
    discord_user = bot.get_user(int(user_id)) if str(user_id).isdigit() else None
    if discord_user is None and str(user_id).isdigit():
        for guild in bot.guilds:
            member = guild.get_member(int(user_id))
            if member:
                discord_user = member
                break

    if discord_user:
        if not payload.get("username"):
            payload["username"] = discord_user.name
        if not payload.get("display_name"):
            payload["display_name"] = discord_user.display_name
        if not payload.get("active_avatar_url"):
            payload["active_avatar_url"] = discord_user.display_avatar.url

    card_bytes, rarity_meta = generate_profile_card(payload)
    file = discord.File(card_bytes, filename=f"profile_{user_id}.png")
    return file, payload, rarity_meta


def _build_rating_embed(payload: Dict[str, Any], rarity_meta: Dict[str, Any]) -> discord.Embed:
    likes = int(payload.get("likes", 0) or 0)
    dislikes = int(payload.get("dislikes", 0) or 0)
    net_score = likes - dislikes
    display_name = payload.get("display_name") or payload.get("username") or f"User {payload.get('user_id', '')}"
    user_id = str(payload.get("user_id", ""))

    rarity_colors = {
        "common": discord.Color.from_rgb(148, 163, 184),
        "rare": discord.Color.from_rgb(56, 189, 248),
        "epic": discord.Color.from_rgb(168, 85, 247),
        "mythic": discord.Color.from_rgb(245, 158, 11),
    }
    tier_key = rarity_meta.get("key", "common")
    color = rarity_colors.get(tier_key, discord.Color.blurple())

    embed = discord.Embed(
        title=f"🎮 {display_name}'s Profile Card",
        description="Community rarity rating is live! Cast your vote or leave feedback below.",
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="🌟 Rarity Tier", value=f"**{rarity_meta['tier_label']}** (Tier {rarity_meta['tier']})", inline=True)
    embed.add_field(name="⚖️ Net Score", value=f"**{net_score:+d}**" if net_score != 0 else "**0**", inline=True)
    embed.add_field(name="📊 Community Votes", value=_format_rating_summary(likes, dislikes), inline=True)
    embed.set_image(url=f"attachment://profile_{user_id}.png")
    embed.set_footer(text=f"user:{user_id} • Community Rating Room")
    return embed


async def _refresh_rating_message(message: discord.Message, user_id: str):
    payload = await Database.get_profile_card_payload(user_id)
    if not payload:
        return
    likes = int(payload.get("likes", 0) or 0)
    dislikes = int(payload.get("dislikes", 0) or 0)
    net_score = likes - dislikes
    rarity_meta = get_rarity_tier(net_score)
    embed = _build_rating_embed(payload, rarity_meta)
    await message.edit(embed=embed, view=ProfileRatingView(user_id))


class ProfileCommentModal(discord.ui.Modal, title="Leave a Comment"):
    comment = discord.ui.TextInput(
        label="Feedback / Comment",
        style=discord.TextStyle.paragraph,
        max_length=300,
        placeholder="Share your thoughts about this profile card...",
    )

    def __init__(self, target_user_id: str, message_id: int):
        super().__init__()
        self.target_user_id = str(target_user_id)
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.channel:
            await interaction.response.send_message("❌ Rating channel not available.", ephemeral=True)
            return

        try:
            parent_message = await interaction.channel.fetch_message(self.message_id)
        except Exception:
            parent_message = interaction.message

        target_name = f"<@{self.target_user_id}>"
        if interaction.guild and self.target_user_id.isdigit():
            member = interaction.guild.get_member(int(self.target_user_id))
            if member:
                target_name = member.display_name

        thread = None
        if parent_message and hasattr(parent_message, "thread") and parent_message.thread:
            thread = parent_message.thread
        elif parent_message and hasattr(parent_message, "create_thread"):
            try:
                thread = await parent_message.create_thread(
                    name=f"💬 {target_name[:20]}'s Profile Ratings",
                    auto_archive_duration=1440,
                )
            except discord.HTTPException:
                thread = None

        destination = thread or interaction.channel
        await destination.send(
            f"💬 **{interaction.user.display_name}** commented on <@{self.target_user_id}>'s profile card:\n> {self.comment.value}"
        )
        await interaction.response.send_message("✅ Your comment has been posted!", ephemeral=True)


class ProfileRatingView(discord.ui.View):
    def __init__(self, target_user_id: str = ""):
        super().__init__(timeout=None)
        self.target_user_id = str(target_user_id)

    @discord.ui.button(label="Like", style=discord.ButtonStyle.green, emoji="👍", custom_id="profile_rating_like")
    async def like_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, "like")

    @discord.ui.button(label="Dislike", style=discord.ButtonStyle.danger, emoji="👎", custom_id="profile_rating_dislike")
    async def dislike_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, "dislike")

    @discord.ui.button(label="Comment", style=discord.ButtonStyle.secondary, emoji="💬", custom_id="profile_rating_comment")
    async def comment_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        target_uid = self.target_user_id or await _extract_user_id_from_message(interaction.message)
        if not target_uid:
            await interaction.response.send_message("❌ Target user could not be determined.", ephemeral=True)
            return
        if not interaction.message:
            await interaction.response.send_message("❌ Rating message not available.", ephemeral=True)
            return
        await interaction.response.send_modal(ProfileCommentModal(target_uid, interaction.message.id))

    async def _handle_vote(self, interaction: discord.Interaction, vote_type: str):
        target_uid = self.target_user_id or await _extract_user_id_from_message(interaction.message)
        if not target_uid:
            await interaction.response.send_message("❌ Target user could not be determined.", ephemeral=True)
            return
        if str(interaction.user.id) == target_uid:
            await interaction.response.send_message("❌ You cannot vote on your own profile.", ephemeral=True)
            return
        result = await Database.register_profile_vote(target_uid, str(interaction.user.id), vote_type)
        if not result.get("success"):
            await interaction.response.send_message(result.get("message", "Vote could not be recorded."), ephemeral=True)
            return
        if interaction.message:
            await _refresh_rating_message(interaction.message, target_uid)
        await interaction.response.send_message(result.get("message", "✅ Vote recorded."), ephemeral=True)


# ==========================================
# BACKGROUND TASK: VOICE CHAT GAMIFICATION
# ==========================================
@tasks.loop(minutes=1)
async def voice_xp_task():
    try:
        rewards: List[Tuple[int, int, int]] = []
        for guild in bot.guilds:
            for channel in guild.voice_channels + guild.stage_channels:
                for member in channel.members:
                    if member.bot:
                        continue
                    voice_state = member.voice
                    if voice_state is None:
                        continue
                    is_muted = voice_state.self_mute or voice_state.mute
                    is_deafened = voice_state.self_deaf or voice_state.deaf
                    if is_muted or is_deafened:
                        continue
                    rewards.append((member.id, 1, 15))
        if rewards:
            await Database.add_xp_coins_batch(rewards)
            logger.debug(f"Rewarded {len(rewards)} users in voice channels.")
    except Exception as e:
        logger.error(f"Error in voice_xp_task: {e}")


@voice_xp_task.before_loop
async def before_voice_xp_task():
    await bot.wait_until_ready()


# ==========================================
# BACKGROUND TASK: PROFILE RATING ROOM POSTS
# ==========================================
@tasks.loop(seconds=30)
async def profile_rating_task():
    channel = _find_rating_channel()
    if channel is None:
        logger.debug("Rating room channel not found; skipping queued profile posts.")
        return

    pending_posts = await Database.claim_pending_rating_posts(limit=5)
    if not pending_posts:
        return

    for queued in pending_posts:
        user_id = str(queued["user_id"])
        try:
            file, payload, rarity_meta = await _build_profile_card_attachment(user_id)
            if not file or not payload or not rarity_meta:
                await Database.mark_rating_post_failed(user_id, "Profile payload missing or could not be generated.")
                continue

            embed = _build_rating_embed(payload, rarity_meta)
            view = ProfileRatingView(user_id)
            message = await channel.send(
                content=f"🗳️ **Community Rating:** New profile update for <@{user_id}>!",
                embed=embed,
                file=file,
                view=view,
            )
            # Register persistent view
            bot.add_view(view, message_id=message.id)
            await Database.mark_rating_post_success(user_id, channel.id, message.id, getattr(message.thread, "id", None))
            logger.info(f"Successfully posted rating card for user {user_id} in #{channel.name}")
        except Exception as exc:
            logger.exception("Failed to post rating card for user %s", user_id)
            await Database.mark_rating_post_failed(user_id, str(exc))


@profile_rating_task.before_loop
async def before_profile_rating_task():
    await bot.wait_until_ready()


# ==========================================
# BACKGROUND TASK: CLEANUP EXPIRED AUTH CODES
# ==========================================
@tasks.loop(minutes=5)
async def cleanup_task():
    try:
        deleted = await Database.cleanup_expired_codes()
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} expired auth code(s).")
    except Exception as e:
        logger.error(f"Error in cleanup_task: {e}")


@cleanup_task.before_loop
async def before_cleanup_task():
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════
# SLASH COMMANDS (All using @bot.tree.command — No Cogs)
# ═══════════════════════════════════════════════════════════

# ==========================================
# /login
# ==========================================
@bot.tree.command(name="login", description="Generate a secure login code for the web dashboard.")
@app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
async def login_slash(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    await Database.get_or_create_user(user_id)
    login_code = await Database.generate_login_code(user_id)
    # ══ الرابط بدون كود — المستخدم يدخله يدوياً في الموقع ══
    login_url = f"{WEBSITE_BASE_URL}/login"

    embed = discord.Embed(
        title="🔐 Secure Login Code",
        description=f"""Hello **{interaction.user.display_name}**!
Use the code below to log into the web dashboard.""",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    # ══ الكود في كود بلوك للنسخ السهل ══
    embed.add_field(
        name="Your Login Code",
        value=f"```\n{login_code}\n```",
        inline=False
    )
    embed.add_field(
        name="🔗 Dashboard URL",
        value=f"[Click here to open login page]({login_url})",
        inline=False
    )
    embed.add_field(
        name="⏰ Expiration",
        value="This code expires in **5 minutes** and is **single-use**.",
        inline=False
    )
    embed.add_field(
        name="🛡️ Security",
        value="If you didn't request this, ignore this message.",
        inline=False
    )
    embed.set_footer(text="Discord Bot Dashboard • Secure Auth")

    try:
        await interaction.user.send(embed=embed)
        await interaction.response.send_message(
            "📩 Check your **DMs** for the secure login code!", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I couldn't DM you. Please enable DMs from server members and try again.",
            ephemeral=True
        )


@login_slash.error
async def login_slash_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏳ Please wait **{error.retry_after:.0f}** seconds before requesting another login code.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ An unexpected error occurred. Please try again later.", ephemeral=True
        )
        logger.error(f"/login error: {error}")
        


# ==========================================
# /profile
# ==========================================
@bot.tree.command(name="profile", description="View your Fortnite-style gamified profile card and community rating.")
@app_commands.describe(member="Member whose profile card you want to view (defaults to you)")
async def profile_slash(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
    user_id = str(target.id)
    await Database.get_or_create_user(user_id)

    # Defer response while card is generated
    await interaction.response.defer(thinking=True)

    file, payload, rarity_meta = await _build_profile_card_attachment(user_id)
    if not file or not payload or not rarity_meta:
        await interaction.followup.send("❌ Could not generate profile card.", ephemeral=True)
        return

    likes = int(payload.get("likes", 0) or 0)
    dislikes = int(payload.get("dislikes", 0) or 0)
    net_score = likes - dislikes
    coins = int(payload.get("coins", 0) or 0)
    xp = int(payload.get("xp", 0) or 0)
    avatar_count = await Database.get_inventory_count(user_id, "avatar")
    banner_count = await Database.get_inventory_count(user_id, "banner")

    rarity_colors = {
        "common": discord.Color.from_rgb(148, 163, 184),
        "rare": discord.Color.from_rgb(56, 189, 248),
        "epic": discord.Color.from_rgb(168, 85, 247),
        "mythic": discord.Color.from_rgb(245, 158, 11),
    }
    tier_key = rarity_meta.get("key", "common")
    color = rarity_colors.get(tier_key, discord.Color.blurple())

    embed = discord.Embed(
        title=f"🎮 {target.display_name}'s Profile Card",
        description=f"**Rarity:** `{rarity_meta['tier_label']}` (Tier {rarity_meta['tier']})",
        color=color,
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="🌟 Rarity & Community Rating",
        value=f"""**Tier:** `{rarity_meta['tier_label']} (Tier {rarity_meta['tier']})`
**Net Score:** `{net_score:+d}`
**Votes:** 👍 `{likes}` | 👎 `{dislikes}`""",
        inline=False
    )
    embed.add_field(
        name="💰 Economy & Stats",
        value=f"""🪙 **Coins:** `{coins:,}`
⚡ **XP:** `{xp:,}`""",
        inline=True
    )
    embed.add_field(
        name="🎒 Inventory",
        value=f"""🖼️ **Avatars:** `{avatar_count}`
🎨 **Banners:** `{banner_count}`""",
        inline=True
    )
    if payload.get("bio"):
        embed.add_field(name="📝 Bio", value=f"> {payload['bio']}", inline=False)

    embed.set_image(url=f"attachment://profile_{user_id}.png")
    embed.set_footer(
        text=f"Requested by {interaction.user.display_name} • {WEBSITE_BASE_URL}",
        icon_url=interaction.user.display_avatar.url
    )
    await interaction.followup.send(embed=embed, file=file)


# ==========================================
# /balance
# ==========================================
@bot.tree.command(name="balance", description="Check your coin balance quickly.")
async def balance_slash(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user = await Database.get_or_create_user(user_id)
    # ══ FIX: Triple-quoted f-string for multi-line description ══
    embed = discord.Embed(
        title="💰 Your Balance",
        description=f"""**{interaction.user.display_name}** currently has:

🪙 **{user['coins']:,}** coins""",
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="Use /daily to claim free coins!")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================
# /inventory
# ==========================================
@bot.tree.command(name="inventory", description="List your purchased avatars and banners.")
@app_commands.describe(item_type="Filter by item type", page="Page number")
@app_commands.choices(item_type=[
    app_commands.Choice(name="All", value="all"),
    app_commands.Choice(name="Avatars", value="avatar"),
    app_commands.Choice(name="Banners", value="banner"),
])
async def inventory_slash(
    interaction: discord.Interaction,
    item_type: app_commands.Choice[str] = None,
    page: int = 1
):
    user_id = str(interaction.user.id)
    await Database.get_or_create_user(user_id)
    filter_type = item_type.value if item_type else "all"
    inventory = await Database.get_user_inventory(user_id)

    if filter_type != "all":
        inventory = [item for item in inventory if item.get("item_type") == filter_type]

    if not inventory:
        await interaction.response.send_message(
            "🎒 Your inventory is empty! Visit the shop to buy some items.", ephemeral=True
        )
        return

    per_page = 5
    total_pages = max(1, (len(inventory) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    page_items = inventory[start:end]

    embed = discord.Embed(
        title=f"🎒 {interaction.user.display_name}'s Inventory",
        description=f"Showing {len(page_items)} of {len(inventory)} items (Page {page}/{total_pages})",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )

    for idx, item in enumerate(page_items, start=start + 1):
        name = item.get("avatar_id") or item.get("local_file_path") or item.get("item_type", "Item")
        # ══ FIX: Triple-quoted f-string for multi-line field value ══
        embed.add_field(
            name=f"#{idx} {item.get('item_type', 'Item').capitalize()}",
            value=f"""ID: `{name}`
Purchased: `{item.get('purchased_at', 'N/A')}`""",
            inline=True
        )

    embed.set_footer(text=f"Filter: {filter_type.capitalize()} | Use /inventory page:2")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================
# /daily
# ==========================================
@bot.tree.command(name="daily", description="Claim your daily reward! Resets every 24 hours.")
async def daily_slash(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    now = datetime.utcnow()
    user = await Database.get_or_create_user(user_id)

    last_daily_str = user.get("last_daily")
    if last_daily_str:
        try:
            last_daily = datetime.fromisoformat(last_daily_str)
            if last_daily.tzinfo:
                last_daily = last_daily.replace(tzinfo=None)
        except ValueError:
            last_daily = None

        if last_daily:
            cooldown = timedelta(hours=24)
            elapsed = now - last_daily
            if elapsed < cooldown:
                remaining = cooldown - elapsed
                hours, remainder = divmod(int(remaining.total_seconds()), 3600)
                minutes, _ = divmod(remainder, 60)
                await interaction.response.send_message(
                    f"⏳ You already claimed your daily reward!\nCome back in **{hours}h {minutes}m**.",
                    ephemeral=True
                )
                return

    coins_reward = secrets.randbelow(
        Database.DAILY_REWARD_MAX - Database.DAILY_REWARD_MIN + 1
    ) + Database.DAILY_REWARD_MIN
    await Database.update_user_coins(user_id, coins_reward)
    await Database.add_xp(user_id, Database.DAILY_XP_REWARD)

    now_iso = now.isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_daily = ? WHERE user_id = ?", (now_iso, user_id))
        await db.commit()

    embed = discord.Embed(
        title="🎁 Daily Reward Claimed!",
        description=f"**{interaction.user.display_name}** claimed their daily reward!",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="🪙 Coins", value=f"+{coins_reward:,}", inline=True)
    embed.add_field(name="⭐ XP", value=f"+{Database.DAILY_XP_REWARD}", inline=True)
    embed.set_footer(text="Come back tomorrow for another reward!")
    await interaction.response.send_message(embed=embed)


# ==========================================
# /gift
# ==========================================
@bot.tree.command(name="gift", description="Gift coins to another user.")
@app_commands.describe(user="The user to gift coins to", amount="Amount of coins to gift")
@app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id)
async def gift_slash(interaction: discord.Interaction, user: discord.User, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't gift coins to yourself!", ephemeral=True)
        return

    sender_id = str(interaction.user.id)
    receiver_id = str(user.id)

    sender = await Database.get_or_create_user(sender_id)
    if sender["coins"] < amount:
        await interaction.response.send_message(
            f"❌ Insufficient funds! You have {sender['coins']:,} coins.", ephemeral=True
        )
        return

    await Database.update_user_coins(sender_id, -amount)
    await Database.get_or_create_user(receiver_id)
    await Database.update_user_coins(receiver_id, amount)

    embed = discord.Embed(
        title="🎁 Gift Sent!",
        description=f"**{interaction.user.display_name}** gifted **{amount:,}** coins to **{user.display_name}**!",
        color=discord.Color.pink(),
        timestamp=discord.utils.utcnow()
    )
    await interaction.response.send_message(embed=embed)


@gift_slash.error
async def gift_slash_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏳ Wait **{error.retry_after:.0f}** seconds between gifts.", ephemeral=True
        )
    else:
        await interaction.response.send_message("❌ Gift failed.", ephemeral=True)


# ==========================================
# /shop
# ==========================================
@bot.tree.command(name="shop", description="Get a link to the web shop.")
async def shop_slash(interaction: discord.Interaction):
    # ══ FIX: Triple-quoted f-string for multi-line description ══
    embed = discord.Embed(
        title="🛒 Image Marketplace",
        description=f"""Browse and buy avatars & banners!

[Open Shop]({WEBSITE_BASE_URL}/shop)""",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="💡 Tip",
        value="Use `/balance` to check your coins before shopping!",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================
# /dashboard
# ==========================================
@bot.tree.command(name="dashboard", description="Get a quick link to the web dashboard.")
async def dashboard_slash(interaction: discord.Interaction):
    # ══ FIX: Triple-quoted f-string for multi-line description ══
    embed = discord.Embed(
        title="🌐 Web Dashboard",
        description=f"""Access your full dashboard here:
{WEBSITE_BASE_URL}""",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="📝 Note",
        value="Use `/login` if you need a fresh authentication link.",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================
# /stats
# ==========================================
async def build_stats_embed(
    target: discord.User | discord.Member,
    requester: discord.User | discord.Member
) -> discord.Embed:
    stats = await Database.get_user_stats(target.id)
    embed = discord.Embed(title=f"📊 {target.display_name}'s Stats", color=discord.Color.gold())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="⭐ Total XP", value=f"**{stats['xp']:,}**", inline=True)
    embed.add_field(name="🪙 Total Coins", value=f"**{stats['coins']:,}**", inline=True)
    embed.set_footer(
        text=f"Requested by {requester.display_name}",
        icon_url=requester.display_avatar.url
    )
    return embed


@bot.tree.command(name="stats", description="View XP and Coins stats for yourself or another member.")
@app_commands.describe(member="The member whose stats you want to view.")
async def stats_slash(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
    embed = await build_stats_embed(target, interaction.user)
    await interaction.response.send_message(embed=embed)


# ==========================================
# /leaderboard
# ==========================================
async def build_leaderboard_embed(
    requester: discord.User | discord.Member
) -> discord.Embed:
    data = await Database.get_leaderboard(limit=10)
    top_xp = data["xp"]
    top_coins = data["coins"]

    embed = discord.Embed(title="🏆 Server Leaderboard", color=discord.Color.purple())

    if top_xp:
        xp_lines = []
        medals = ["🥇", "🥈", "🥉"]
        for idx, entry in enumerate(top_xp, start=1):
            prefix = medals[idx - 1] if idx <= 3 else f"`#{idx}`"
            xp_lines.append(f"{prefix} <@{entry['user_id']}> - **{entry['xp']:,}** XP")
        # ══ FIX: Explicit \\n inside single-line string ══
        embed.add_field(name="⭐ Top XP", value="\n".join(xp_lines), inline=False)
    else:
        embed.add_field(name="⭐ Top XP", value="No data recorded yet.", inline=False)

    if top_coins:
        coins_lines = []
        medals = ["🥇", "🥈", "🥉"]
        for idx, entry in enumerate(top_coins, start=1):
            prefix = medals[idx - 1] if idx <= 3 else f"`#{idx}`"
            coins_lines.append(f"{prefix} <@{entry['user_id']}> - **{entry['coins']:,}** Coins")
        # ══ FIX: Explicit \\n inside single-line string ══
        embed.add_field(name="🪙 Top Coins", value="\n".join(coins_lines), inline=False)
    else:
        embed.add_field(name="🪙 Top Coins", value="No data recorded yet.", inline=False)

    embed.set_footer(
        text=f"Requested by {requester.display_name}",
        icon_url=requester.display_avatar.url
    )
    return embed


@bot.tree.command(name="leaderboard", description="View the top 10 users by XP and Coins.")
async def leaderboard_slash(interaction: discord.Interaction):
    embed = await build_leaderboard_embed(interaction.user)
    await interaction.response.send_message(embed=embed)


# ═══════════════════════════════════════════════════════════
# PREFIX COMMANDS (Original main.py style — kept for compatibility)
# ═══════════════════════════════════════════════════════════

@bot.command(name="login")
async def login_prefix(ctx: commands.Context):
    user_id = str(ctx.author.id)
    await Database.get_or_create_user(user_id)
    login_code = await Database.generate_login_code(user_id)
    login_url = f"{WEBSITE_BASE_URL}/login"

    embed = discord.Embed(
        title="🔐 Web Dashboard Login Code",
        description=f"""Use the authentication code below to log into the Web Dashboard:

# `{login_code}`
⏰ **Expiration:** This code expires in **5 minutes**.
🔗 **Website:** [Go to Dashboard]({login_url})""",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="Discord Web Authentication", icon_url=ctx.author.display_avatar.url)

    try:
        dm_channel = await ctx.author.create_dm()
        await dm_channel.send(embed=embed)
        await ctx.reply(
            "📩 I have sent your authentication code to your Direct Messages! Check your DMs.",
            mention_author=True
        )
    except discord.Forbidden:
        await ctx.reply(
            "❌ I couldn't send you a DM. Please enable Direct Messages from server members and try again.",
            mention_author=True
        )


@bot.command(name="profile")
async def profile_prefix(ctx: commands.Context, member: Optional[discord.Member] = None):
    target = member or ctx.author
    user_id = str(target.id)
    await Database.get_or_create_user(user_id)

    async with ctx.typing():
        file, payload, rarity_meta = await _build_profile_card_attachment(user_id)
        if not file or not payload or not rarity_meta:
            await ctx.reply("❌ Could not generate profile card.")
            return

        likes = int(payload.get("likes", 0) or 0)
        dislikes = int(payload.get("dislikes", 0) or 0)
        net_score = likes - dislikes
        coins = int(payload.get("coins", 0) or 0)
        xp = int(payload.get("xp", 0) or 0)
        avatar_count = await Database.get_inventory_count(user_id, "avatar")
        banner_count = await Database.get_inventory_count(user_id, "banner")

        rarity_colors = {
            "common": discord.Color.from_rgb(148, 163, 184),
            "rare": discord.Color.from_rgb(56, 189, 248),
            "epic": discord.Color.from_rgb(168, 85, 247),
            "mythic": discord.Color.from_rgb(245, 158, 11),
        }
        tier_key = rarity_meta.get("key", "common")
        color = rarity_colors.get(tier_key, discord.Color.blurple())

        embed = discord.Embed(
            title=f"🎮 {target.display_name}'s Profile Card",
            description=f"**Rarity:** `{rarity_meta['tier_label']}` (Tier {rarity_meta['tier']})",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="🌟 Rarity & Community Rating",
            value=f"""**Tier:** `{rarity_meta['tier_label']} (Tier {rarity_meta['tier']})`
**Net Score:** `{net_score:+d}`
**Votes:** 👍 `{likes}` | 👎 `{dislikes}`""",
            inline=False
        )
        embed.add_field(
            name="💰 Economy & Stats",
            value=f"""🪙 **Coins:** `{coins:,}`
⚡ **XP:** `{xp:,}`""",
            inline=True
        )
        embed.add_field(
            name="🎒 Inventory",
            value=f"""🖼️ **Avatars:** `{avatar_count}`
🎨 **Banners:** `{banner_count}`""",
            inline=True
        )
        if payload.get("bio"):
            embed.add_field(name="📝 Bio", value=f"> {payload['bio']}", inline=False)

        embed.set_image(url=f"attachment://profile_{user_id}.png")
        embed.set_footer(
            text=f"Requested by {ctx.author.display_name} • {WEBSITE_BASE_URL}",
            icon_url=ctx.author.display_avatar.url
        )
        await ctx.reply(embed=embed, file=file)


@bot.command(name="stats")
async def stats_prefix(ctx: commands.Context, member: Optional[discord.Member] = None):
    target = member or ctx.author
    embed = await build_stats_embed(target, ctx.author)
    await ctx.reply(embed=embed)


@bot.command(name="leaderboard")
async def leaderboard_prefix(ctx: commands.Context):
    embed = await build_leaderboard_embed(ctx.author)
    await ctx.reply(embed=embed)


@bot.command(name="daily")
async def daily_prefix(ctx: commands.Context):
    success, new_coins, remaining = await Database.claim_daily(ctx.author.id)
    if success:
        # ══ FIX: Triple-quoted f-string for multi-line description ══
        embed = discord.Embed(
            title="🎁 Daily Reward Claimed!",
            description=f"""You successfully claimed your daily reward of **+100 Coins**! 🪙

💰 **New Balance:** **{new_coins:,}** Coins""",
            color=discord.Color.green()
        )
    else:
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
        # ══ FIX: Triple-quoted f-string for multi-line description ══
        embed = discord.Embed(
            title="⏰ Daily Reward Cooldown",
            description=f"""You have already claimed your daily reward today!

Please wait **{time_str}** before claiming again.""",
            color=discord.Color.red()
        )
    embed.set_footer(
        text=f"Requested by {ctx.author.display_name}",
        icon_url=ctx.author.display_avatar.url
    )
    await ctx.reply(embed=embed)


@bot.command(name="inventory")
async def inventory_prefix(ctx: commands.Context):
    items = await Database.get_user_inventory_ids(ctx.author.id)
    embed = discord.Embed(title=f"🎒 {ctx.author.display_name}'s Inventory", color=discord.Color.blue())
    if not items:
        embed.description = "You haven't bought any avatars from the Web Dashboard yet!"
    else:
        item_list = "\n".join([f"• `{avatar_id}`" for avatar_id in items])
        # ══ FIX: Triple-quoted f-string for multi-line description ══
        embed.description = f"""**Owned Custom Avatars:**

{item_list}"""
    embed.set_footer(
        text=f"Requested by {ctx.author.display_name}",
        icon_url=ctx.author.display_avatar.url
    )
    await ctx.reply(embed=embed)


# ==========================================
# ON_MESSAGE EVENT (TEXT CHAT GAMIFICATION & MENTIONS)
# ==========================================
_chat_cooldowns: Dict[int, float] = {}
CHAT_COOLDOWN_SECONDS = 60


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    # Text Chat XP & Economy System (+1 XP, +5 Coins with 60s cooldown)
    user_id = message.author.id
    now = time.time()
    last_earned = _chat_cooldowns.get(user_id, 0)
    if now - last_earned >= CHAT_COOLDOWN_SECONDS:
        _chat_cooldowns[user_id] = now
        await Database.add_xp_coins(user_id, xp=1, coins=5)

    # Server Mention Detection (Show user's active avatar when mentioned)
    if message.guild and message.mentions and not message.mention_everyone:
        for mentioned_user in message.mentions:
            if mentioned_user.bot:
                continue
            profile = await Database.get_profile(mentioned_user.id)
            if profile and profile.get("active_avatar_url"):
                embed = discord.Embed(
                    title=f"📸 {mentioned_user.display_name}'s Active Avatar",
                    color=discord.Color.teal()
                )
                embed.set_image(url=profile["active_avatar_url"])
                details = []
                if profile.get("country"):
                    details.append(f"**Country:** {profile['country']}")
                if profile.get("birth_date"):
                    details.append(f"**Birth Date:** {profile['birth_date']}")
                if profile.get("bio"):
                    details.append(f"**Bio:** {profile['bio']}")
                if details:
                    embed.description = " • ".join(details)
                embed.set_footer(
                    text=f"Requested by {message.author.display_name}",
                    icon_url=message.author.display_avatar.url
                )
                await message.channel.send(embed=embed)
                break

    await bot.process_commands(message)


# ==========================================
# READY EVENT
# ==========================================
@bot.event
async def on_ready():
    logger.info(f"✅ Bot is online! Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info(f"🌐 Connected to {len(bot.guilds)} guild(s)")
    logger.info(f"📊 Database path: {DB_PATH}")


# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        logger.error("❌ DISCORD_BOT_TOKEN is missing or not set in .env file!")
    else:
        bot.run(TOKEN)