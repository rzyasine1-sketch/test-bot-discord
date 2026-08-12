"""
database.py — Unified Async SQLite Wrapper (aiosqlite)
Version: 2.0.0
Guarantees: WAL mode, strict SQL binding parity, zero leaked connections.
"""

import os
import secrets
import string
import logging
import aiosqlite
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("Database")
DB_PATH = os.getenv("DB_PATH", "profiles.db")


class Database:
    """Asynchronous SQLite database wrapper using aiosqlite.
    All methods are coroutines and must be awaited."""

    CODE_EXPIRY_MINUTES = 5
    DAILY_REWARD_MIN = 500
    DAILY_REWARD_MAX = 1500
    DAILY_XP_REWARD = 50
    DEFAULT_COINS = 5000
    DEFAULT_XP = 0

    # ── Schema ──

    @staticmethod
    async def init_db() -> None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA foreign_keys = ON;")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    xp INTEGER DEFAULT 0,
                    coins INTEGER DEFAULT 0,
                    last_daily TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id TEXT PRIMARY KEY,
                    active_avatar_url TEXT,
                    birth_date TEXT,
                    country TEXT,
                    bio TEXT
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS auth_codes (
                    user_id TEXT PRIMARY KEY,
                    login_code TEXT,
                    code TEXT,
                    expires_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

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

            await db.execute("CREATE INDEX IF NOT EXISTS idx_inventory_user_id ON inventory(user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_inventory_item_type ON inventory(user_id, item_type)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_auth_codes_expires ON auth_codes(expires_at)")

            await db.commit()
            logger.info("Database initialized successfully with WAL mode.")

    # ── User Core ──

    @staticmethod
    async def get_or_create_user(user_id: str) -> Dict[str, Any]:
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id, xp, coins, created_at FROM users WHERE user_id = ?",
                (uid,)
            ) as cursor:
                row = await cursor.fetchone()

            if row is None:
                await db.execute(
                    "INSERT INTO users (user_id, xp, coins) VALUES (?, ?, ?)",
                    (uid, Database.DEFAULT_XP, Database.DEFAULT_COINS)
                )
                await db.commit()
                return {
                    "user_id": uid,
                    "xp": Database.DEFAULT_XP,
                    "coins": Database.DEFAULT_COINS,
                    "created_at": datetime.utcnow().isoformat()
                }
            return dict(row)

    @staticmethod
    async def get_user(user_id: str) -> Optional[Dict[str, Any]]:
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id, xp, coins, created_at FROM users WHERE user_id = ?",
                (uid,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    @staticmethod
    async def add_xp_coins(user_id: int, xp: int, coins: int) -> None:
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
    async def add_xp_coins_batch(rewards: List[Tuple[int, int, int]]) -> None:
        if not rewards:
            return
        data = [(str(uid), xp, coins) for uid, xp, coins in rewards]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany("""
                INSERT INTO users (user_id, xp, coins)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    xp = users.xp + excluded.xp,
                    coins = users.coins + excluded.coins
            """, data)
            await db.commit()

    @staticmethod
    async def update_user_coins(user_id: str, delta: int) -> bool:
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT coins FROM users WHERE user_id = ?",
                (uid,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return False
            new_balance = row["coins"] + delta
            if new_balance < 0:
                return False
            await db.execute(
                "UPDATE users SET coins = ? WHERE user_id = ?",
                (new_balance, uid)
            )
            await db.commit()
            return True

    @staticmethod
    async def add_xp(user_id: str, amount: int) -> None:
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET xp = xp + ? WHERE user_id = ?",
                (amount, uid)
            )
            await db.commit()

    @staticmethod
    async def get_user_stats(user_id: int) -> Dict[str, int]:
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT xp, coins FROM users WHERE user_id = ?",
                (uid,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {"xp": row["xp"], "coins": row["coins"]}
                return {"xp": 0, "coins": 0}

    @staticmethod
    async def get_leaderboard(limit: int = 10) -> Dict[str, List[Dict[str, Any]]]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id, xp FROM users ORDER BY xp DESC LIMIT ?",
                (limit,)
            ) as cursor:
                top_xp = [dict(row) async for row in cursor]
            async with db.execute(
                "SELECT user_id, coins FROM users ORDER BY coins DESC LIMIT ?",
                (limit,)
            ) as cursor:
                top_coins = [dict(row) async for row in cursor]
            return {"xp": top_xp, "coins": top_coins}

    # ── Daily Rewards ──

    @staticmethod
    async def claim_daily(user_id: int) -> Tuple[bool, int, Optional[timedelta]]:
        uid = str(user_id)
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT coins, last_daily FROM users WHERE user_id = ?",
                (uid,)
            ) as cursor:
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

            reward_coins = secrets.randbelow(
                Database.DAILY_REWARD_MAX - Database.DAILY_REWARD_MIN + 1
            ) + Database.DAILY_REWARD_MIN

            await db.execute("""
                INSERT INTO users (user_id, xp, coins, last_daily)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    xp = users.xp + excluded.xp,
                    coins = users.coins + excluded.coins,
                    last_daily = excluded.last_daily
            """, (uid, Database.DAILY_XP_REWARD, reward_coins, now.isoformat()))
            await db.commit()

            async with db.execute(
                "SELECT coins FROM users WHERE user_id = ?",
                (uid,)
            ) as cursor:
                updated = await cursor.fetchone()
                new_coins = updated["coins"] if updated else reward_coins

            return True, new_coins, None

    # ── Auth Codes ──

    @staticmethod
    async def generate_login_code(user_id: str) -> str:
        alphabet = string.ascii_uppercase + string.digits
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        expires_at = datetime.utcnow() + timedelta(minutes=Database.CODE_EXPIRY_MINUTES)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO auth_codes (user_id, login_code, code, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    login_code = excluded.login_code,
                    code = excluded.code,
                    expires_at = excluded.expires_at,
                    created_at = CURRENT_TIMESTAMP
            """, (user_id, code, code, expires_at.isoformat()))
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
                    "DELETE FROM auth_codes WHERE login_code = ? OR code = ?",
                    (code, code)
                )
                await db.commit()
                return None
            user_id = row["user_id"]
            await db.execute(
                "DELETE FROM auth_codes WHERE login_code = ? OR code = ?",
                (code, code)
            )
            await db.commit()
            return user_id

    @staticmethod
    async def cleanup_expired_codes() -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "DELETE FROM auth_codes WHERE expires_at < datetime('now')"
            )
            await db.commit()
            return cursor.rowcount

    # ── Profiles ──

    @staticmethod
    async def get_profile(user_id: int) -> Optional[Dict[str, Any]]:
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM profiles WHERE user_id = ?",
                (uid,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    @staticmethod
    async def update_profile(user_id: str, **fields) -> None:
        uid = str(user_id)
        allowed = {"active_avatar_url", "birth_date", "country", "bio"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO profiles (user_id)
                VALUES (?)
                ON CONFLICT(user_id) DO NOTHING
            """, (uid,))
            for col, val in updates.items():
                await db.execute(
                    f"UPDATE profiles SET {col} = ? WHERE user_id = ?",
                    (val, uid)
                )
            await db.commit()

    # ── Inventory ──

    @staticmethod
    async def get_user_inventory(user_id: str) -> List[Dict[str, Any]]:
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT id, item_type, avatar_id, local_file_path, purchased_at
                FROM inventory WHERE user_id = ? ORDER BY purchased_at DESC
            """, (uid,)) as cursor:
                return [dict(row) async for row in cursor]

    @staticmethod
    async def get_user_inventory_ids(user_id: int) -> List[str]:
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT avatar_id FROM inventory
                WHERE user_id = ? AND avatar_id IS NOT NULL
            """, (uid,)) as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows if row[0]]

    @staticmethod
    async def get_inventory_count(user_id: str, item_type: Optional[str] = None) -> int:
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            if item_type:
                async with db.execute("""
                    SELECT COUNT(*) as cnt FROM inventory
                    WHERE user_id = ? AND item_type = ?
                """, (uid, item_type)) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else 0
            else:
                async with db.execute("""
                    SELECT COUNT(*) as cnt FROM inventory WHERE user_id = ?
                """, (uid,)) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else 0

    @staticmethod
    async def add_inventory_item(user_id: str, item_type: str, file_path: str, avatar_id: Optional[str] = None) -> int:
        uid = str(user_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO inventory (user_id, item_type, avatar_id, local_file_path)
                VALUES (?, ?, ?, ?)
            """, (uid, item_type, avatar_id, file_path))
            await db.commit()
            return db.last_insert_rowid()

    # ── Diagnostics ──

    @staticmethod
    async def get_db_stats() -> Dict[str, int]:
        async with aiosqlite.connect(DB_PATH) as db:
            stats = {}
            for table in ("users", "auth_codes", "inventory", "profiles"):
                async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                    row = await cursor.fetchone()
                    stats[table] = row[0] if row else 0
            return stats
