"""
core/database.py — База данных.
Используем SQLite + aiosqlite (без ORM — проще, быстрее, надёжнее для такого масштаба).
"""

import sqlite3
import aiosqlite
from datetime import datetime
from core.config import settings

DB_PATH = settings.DB_FILE


async def init_db():
    """Создаём таблицы при первом запуске."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            -- Таблица лицензий
            CREATE TABLE IF NOT EXISTS licenses (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key     TEXT    NOT NULL UNIQUE,
                plan            TEXT    NOT NULL DEFAULT 'STARTER',
                status          TEXT    NOT NULL DEFAULT 'NEW',
                -- NEW → ACTIVE → EXPIRED / REVOKED

                master_secret   TEXT    NOT NULL,  -- случайная соль для токена
                months          INTEGER NOT NULL DEFAULT 12,

                -- Заполняется при активации
                activated_at    TEXT,
                inn             TEXT,
                mac_address     TEXT,
                hostname        TEXT,
                device_token    TEXT,   -- HMAC(master_secret, inn:mac) — токен устройства
                expires_at      TEXT,

                -- Статистика
                last_seen       TEXT,
                validation_count INTEGER DEFAULT 0,

                -- Метаданные (для тебя)
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                comment         TEXT,   -- заметка при создании (например "продан ООО Ромашка")
                invoice_number  TEXT    -- номер счёта/договора
            );

            -- Индексы для быстрого поиска
            CREATE INDEX IF NOT EXISTS idx_license_key ON licenses(license_key);
            CREATE INDEX IF NOT EXISTS idx_inn         ON licenses(inn);
            CREATE INDEX IF NOT EXISTS idx_status      ON licenses(status);

            -- Лог всех запросов (для аудита и диагностики)
            CREATE TABLE IF NOT EXISTS request_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL DEFAULT (datetime('now')),
                license_key TEXT,
                action      TEXT,   -- activate / validate / revoke
                ip          TEXT,
                inn         TEXT,
                mac_address TEXT,
                success     INTEGER,
                details     TEXT    -- JSON с деталями
            );

            CREATE INDEX IF NOT EXISTS idx_log_key ON request_log(license_key);
            CREATE INDEX IF NOT EXISTS idx_log_ts  ON request_log(ts);
        """)
        await db.commit()


# ─── Операции с лицензиями ────────────────────────────────────────────────────

async def get_license(key: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM licenses WHERE license_key = ?", (key.upper(),)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_license(key: str, plan: str, months: int,
                          master_secret: str, comment: str = "",
                          invoice: str = "") -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO licenses (license_key, plan, status, master_secret, months, comment, invoice_number)
            VALUES (?, ?, 'NEW', ?, ?, ?, ?)
        """, (key.upper(), plan.upper(), master_secret, months, comment, invoice))
        await db.commit()
    return await get_license(key)


async def activate_license(key: str, inn: str, mac: str, hostname: str,
                             device_token: str, expires_at: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE licenses SET
                status        = 'ACTIVE',
                inn           = ?,
                mac_address   = ?,
                hostname      = ?,
                device_token  = ?,
                expires_at    = ?,
                activated_at  = datetime('now'),
                last_seen     = datetime('now')
            WHERE license_key = ?
        """, (inn, mac, hostname, device_token, expires_at, key.upper()))
        await db.commit()
    return await get_license(key)


async def update_last_seen(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE licenses SET
                last_seen        = datetime('now'),
                validation_count = validation_count + 1
            WHERE license_key = ?
        """, (key.upper(),))
        await db.commit()


async def revoke_license(key: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE licenses SET status = 'REVOKED' WHERE license_key = ?",
            (key.upper(),)
        )
        await db.commit()
    lic = await get_license(key)
    return lic is not None


async def get_all_licenses(status: str | None = None,
                            limit: int = 100, offset: int = 0) -> list[dict]:
    query = "SELECT * FROM licenses"
    params = []
    if status:
        query  += " WHERE status = ?"
        params.append(status.upper())
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(status = 'NEW')     as new_count,
                SUM(status = 'ACTIVE')  as active_count,
                SUM(status = 'REVOKED') as revoked_count,
                SUM(status = 'ACTIVE' AND expires_at < datetime('now')) as expired_count
            FROM licenses
        """) as cur:
            row = await cur.fetchone()
            stats = dict(row)

        # Выручка по тарифам (активные)
        async with db.execute("""
            SELECT plan, COUNT(*) as cnt
            FROM licenses WHERE status = 'ACTIVE'
            GROUP BY plan
        """) as cur:
            rows       = await cur.fetchall()
            stats["by_plan"] = {r["plan"]: r["cnt"] for r in rows}

        return stats


# ─── Лог запросов ────────────────────────────────────────────────────────────

async def log_request(license_key: str, action: str, ip: str,
                       inn: str, mac: str, success: bool, details: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO request_log (license_key, action, ip, inn, mac_address, success, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (license_key, action, ip, inn, mac, int(success), details))
        await db.commit()
