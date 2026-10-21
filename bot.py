# BUILD_ID: marketphoto-creator-suite-stable-2026-10-20-v8
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
import html
import io
import json
import logging
import math
import os
import secrets
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramConflictError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)

BUILD_ID = "marketphoto-creator-suite-stable-2026-10-20-v8"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("marketphoto")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CF_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
CF_MODEL = os.getenv(
    "CLOUDFLARE_MODEL", "@cf/black-forest-labs/flux-2-klein-9b"
).strip()

ENV_ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
ADMIN_IDS = {5694967765} | ENV_ADMIN_IDS

PORT = int(os.getenv("PORT", "10000"))
DB_PATH = Path(os.getenv("DB_PATH", "marketphoto.sqlite3"))
FREE_DAILY_LIMIT = max(0, int(os.getenv("FREE_DAILY_LIMIT", "5")))
VIP_DAILY_LIMIT = max(1, int(os.getenv("VIP_DAILY_LIMIT", "100")))
CF_TIMEOUT = max(30, int(os.getenv("CF_TIMEOUT_SECONDS", "180")))
MAX_PROMPT_LENGTH = 1800
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
KYIV_TZ = ZoneInfo("Europe/Kyiv")

PRODUCTS: dict[str, dict[str, Any]] = {
    "credits_25": {
        "title": "25 генераций",
        "description": "25 дополнительных генераций изображений",
        "stars": max(1, int(os.getenv("PRICE_CREDITS_25", "50"))),
        "credits": 25,
        "days": 0,
    },
    "credits_100": {
        "title": "100 генераций",
        "description": "100 дополнительных генераций изображений",
        "stars": max(1, int(os.getenv("PRICE_CREDITS_100", "150"))),
        "credits": 100,
        "days": 0,
    },
    "vip_30d": {
        "title": "VIP на 30 дней",
        "description": f"VIP-доступ до {VIP_DAILY_LIMIT} генераций в день на 30 дней",
        "stars": max(1, int(os.getenv("PRICE_VIP_30D", "250"))),
        "credits": 0,
        "days": 30,
    },
}

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🎨 Создать изображение"),
            KeyboardButton(text="🪄 Редактировать фото"),
        ],
        [
            KeyboardButton(text="👤 Аватарка"),
            KeyboardButton(text="📱 Обои на телефон"),
        ],
        [
            KeyboardButton(text="🎭 Стили AI"),
            KeyboardButton(text="🧰 Другие функции"),
        ],
        [
            KeyboardButton(text="⭐ VIP / Пополнение"),
            KeyboardButton(text="📊 Мой профиль"),
        ],
        [
            KeyboardButton(text="🎁 Бонус дня"),
            KeyboardButton(text="🕘 История"),
        ],
        [KeyboardButton(text="🆘 Поддержка")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Выберите функцию MarketPhoto AI…",
)

STYLE_PRESETS: dict[str, tuple[str, str]] = {
    "realism": ("📸 Реализм", "Photorealistic image, natural materials and skin texture, professional camera photography, balanced exposure, realistic shadows, crisp details, tasteful color grading."),
    "anime": ("🌸 Аниме", "High-quality Japanese anime illustration, expressive character design, clean line art, beautifully controlled colors, cinematic anime lighting, polished background art."),
    "3d": ("🧸 3D-мультфильм", "Polished stylized 3D animated movie look, appealing rounded shapes, expressive face, detailed materials, soft cinematic lighting, charming colorful composition."),
    "cyberpunk": ("🌃 Киберпанк", "Cyberpunk science-fiction visual style, neon signs, rain reflections, atmospheric night lighting, rich but controlled cyan and magenta accents, intricate futuristic details."),
    "minecraft": ("🟩 Minecraft", "Voxel art inspired by block-building games, recognizable cubic shapes, pixelated textures, playful blocky environment, clear composition and vivid but balanced colors."),
    "cartoon": ("🎨 Мультфильм", "Professional 2D cartoon illustration, friendly expressive shapes, clean outlines, appealing characters, readable silhouettes, lively colors and polished background."),
    "watercolor": ("🖌️ Акварель", "Traditional watercolor illustration on textured paper, delicate pigment blooms, elegant brushwork, soft edges, subtle paper grain, harmonious artistic palette."),
    "cinematic": ("🎬 Кинематограф", "Cinematic composition, dramatic but natural lighting, atmospheric depth, carefully directed visual focus, professional color grading, high detail and film-still quality."),
}

TOOLS_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🛍️ Фото товара", callback_data="tool:product"), InlineKeyboardButton(text="😂 Мем", callback_data="tool:meme")],
    [InlineKeyboardButton(text="📺 Обложка YouTube", callback_data="tool:youtube_cover"), InlineKeyboardButton(text="👕 Дизайн одежды", callback_data="tool:clothing")],
    [InlineKeyboardButton(text="🪧 Постер", callback_data="tool:poster"), InlineKeyboardButton(text="🧑‍🎨 Персонаж", callback_data="tool:character")],
    [InlineKeyboardButton(text="🧽 Улучшить фото", callback_data="tool:improve_wait_photo"), InlineKeyboardButton(text="🌄 Заменить фон", callback_data="tool:background_wait_photo")],
    [InlineKeyboardButton(text="🧠 Улучшить промпт", callback_data="tool:improve_prompt"), InlineKeyboardButton(text="🌍 Перевести промпт", callback_data="tool:translate_prompt")],
    [InlineKeyboardButton(text="🎬 Сценарий TikTok", callback_data="tool:tiktok_script"), InlineKeyboardButton(text="💼 Заказать дизайн", callback_data="tool:custom_order")],
    [InlineKeyboardButton(text="💡 Каталог промптов", callback_data="tool:prompt_library"), InlineKeyboardButton(text="👥 Реферальная программа", callback_data="tool:referral")],
])

STYLE_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📸 Реализм", callback_data="style:realism"), InlineKeyboardButton(text="🌸 Аниме", callback_data="style:anime")],
    [InlineKeyboardButton(text="🧸 3D-мультфильм", callback_data="style:3d"), InlineKeyboardButton(text="🌃 Киберпанк", callback_data="style:cyberpunk")],
    [InlineKeyboardButton(text="🟩 Minecraft", callback_data="style:minecraft"), InlineKeyboardButton(text="🎨 Мультфильм", callback_data="style:cartoon")],
    [InlineKeyboardButton(text="🖌️ Акварель", callback_data="style:watercolor"), InlineKeyboardButton(text="🎬 Кино", callback_data="style:cinematic")],
])

PROMPT_LIBRARY: dict[str, tuple[str, str]] = {
    "fantasy": ("🐉 Фэнтези", "An epic fantasy landscape with an ancient castle on a mountain, misty valleys, tiny glowing fireflies, richly textured stone, magical atmosphere, cinematic golden-hour light, intricate environment design."),
    "product": ("🛍️ Реклама товара", "A premium commercial product photograph of a beautifully arranged modern product on a clean studio set, precise softbox lighting, elegant shadows, uncluttered background, realistic materials, luxury advertising art direction."),
    "portrait": ("📸 Портрет", "A tasteful professional editorial portrait of an adult person, natural expression, beautiful soft key light, realistic skin texture, 85mm portrait lens look, subtle background blur, polished magazine photography."),
    "space": ("🚀 Космос", "A breathtaking view of a futuristic space station above a blue planet, detailed spacecraft, stars and nebulae, cinematic wide composition, realistic light scattering, crisp science-fiction concept art."),
    "food": ("🍓 Еда", "A delicious gourmet food editorial photograph, appetizing natural textures, delicate steam, carefully arranged ingredients, soft directional studio light, professional restaurant menu photography, rich realistic detail."),
    "game": ("🎮 Игровая сцена", "A polished game concept-art scene with a memorable hero silhouette, dramatic environment, layered atmospheric perspective, detailed materials, readable composition, high-end game key art."),
}

PROMPT_LIBRARY_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🐉 Фэнтези", callback_data="promptlib:fantasy"), InlineKeyboardButton(text="🛍️ Товар", callback_data="promptlib:product")],
    [InlineKeyboardButton(text="📸 Портрет", callback_data="promptlib:portrait"), InlineKeyboardButton(text="🚀 Космос", callback_data="promptlib:space")],
    [InlineKeyboardButton(text="🍓 Еда", callback_data="promptlib:food"), InlineKeyboardButton(text="🎮 Игры", callback_data="promptlib:game")],
])

CANCEL_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")]
    ]
)

SHOP_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"🎨 25 генераций — {PRODUCTS['credits_25']['stars']} ⭐",
                callback_data="buy:credits_25",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"🚀 100 генераций — {PRODUCTS['credits_100']['stars']} ⭐",
                callback_data="buy:credits_100",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"👑 VIP 30 дней — {PRODUCTS['vip_30d']['stars']} ⭐",
                callback_data="buy:vip_30d",
            )
        ],
        [InlineKeyboardButton(text="ℹ️ Условия", callback_data="terms")],
    ]
)

RESULT_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Ещё вариант", callback_data="variant"), InlineKeyboardButton(text="🧪 Сделать 4 варианта", callback_data="variants4")],
        [InlineKeyboardButton(text="🔁 Новый запрос", callback_data="again"), InlineKeyboardButton(text="🕘 История", callback_data="history")],
        [InlineKeyboardButton(text="⚠️ Сообщить о проблеме", callback_data="report")],
    ]
)

bot: Bot | None = None
dp = Dispatcher()
http_client: httpx.AsyncClient | None = None
user_flows: dict[int, dict[str, Any]] = {}
last_enhanced_prompts: dict[int, str] = {}
in_progress: set[int] = set()
polling_active = False


class UserFacingError(Exception):
    """An expected error that can be shown to the user."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def day_key() -> str:
    return datetime.now(KYIV_TZ).date().isoformat()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def database():
    """Commit/rollback and always close each SQLite connection."""
    conn = db_connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with database() as db:
        db.execute("PRAGMA journal_mode=WAL")

        db.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                credits INTEGER NOT NULL DEFAULT 0,
                vip_until TEXT
            )"""
        )

        db.execute(
            """CREATE TABLE IF NOT EXISTS daily_usage (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                free_used INTEGER NOT NULL DEFAULT 0,
                vip_used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            )"""
        )

        db.execute(
            """CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                stars INTEGER NOT NULL,
                product TEXT NOT NULL,
                created_at TEXT NOT NULL,
                vip_previous TEXT,
                vip_granted_until TEXT,
                refunded INTEGER NOT NULL DEFAULT 0
            )"""
        )

        db.execute(
            """CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                kind TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new'
            )"""
        )

        db.execute(
            """CREATE TABLE IF NOT EXISTS daily_bonus (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, day)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS referrals (
                invited_user_id INTEGER PRIMARY KEY,
                referrer_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS generation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                mode TEXT NOT NULL,
                file_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                credits INTEGER NOT NULL,
                max_uses INTEGER NOT NULL DEFAULT 100,
                uses INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS promo_redemptions (
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(code, user_id)
            )"""
        )

        columns = {
            r[1]
            for r in db.execute("PRAGMA table_info(daily_usage)").fetchall()
        }
        if "free_used" not in columns:
            db.execute(
                "ALTER TABLE daily_usage ADD COLUMN free_used INTEGER NOT NULL DEFAULT 0"
            )
        if "vip_used" not in columns:
            db.execute(
                "ALTER TABLE daily_usage ADD COLUMN vip_used INTEGER NOT NULL DEFAULT 0"
            )

        payment_columns = {
            r[1]
            for r in db.execute("PRAGMA table_info(payments)").fetchall()
        }

        if "vip_previous" not in payment_columns:
            db.execute("ALTER TABLE payments ADD COLUMN vip_previous TEXT")
        if "vip_granted_until" not in payment_columns:
            db.execute("ALTER TABLE payments ADD COLUMN vip_granted_until TEXT")
        if "refunded" not in payment_columns:
            db.execute(
                "ALTER TABLE payments ADD COLUMN refunded INTEGER NOT NULL DEFAULT 0"
            )


def ensure_user(user: Any) -> None:
    with database() as db:
        db.execute(
            """INSERT INTO users(user_id, username, first_name, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 first_name=excluded.first_name""",
            (
                user.id,
                user.username,
                user.first_name or "",
                now_iso(),
            ),
        )


def get_user(user_id: int) -> sqlite3.Row | None:
    with database() as db:
        return db.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()


def vip_active(until: str | None) -> bool:
    if not until:
        return False
    try:
        expires = datetime.fromisoformat(until)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > now_utc()
    except (TypeError, ValueError):
        return False


def reserve_generation(user_id: int) -> tuple[str, str]:
    """Reserve quota for normal users; admin use is unlimited inside the bot."""
    today = day_key()

    with database() as db:
        db.execute("BEGIN IMMEDIATE")

        row = db.execute(
            "SELECT credits, vip_until FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()

        if row is None:
            db.rollback()
            raise UserFacingError("Нажми /start, чтобы создать профиль.")

        if is_admin(user_id):
            return ("admin", today)

        active_vip = vip_active(row["vip_until"])

        usage = db.execute(
            "SELECT free_used, vip_used FROM daily_usage WHERE user_id=? AND day=?",
            (user_id, today),
        ).fetchone()

        free_used = int(usage["free_used"]) if usage else 0
        vip_used = int(usage["vip_used"]) if usage else 0

        if active_vip and vip_used < VIP_DAILY_LIMIT:
            db.execute(
                """INSERT INTO daily_usage(user_id, day, vip_used)
                   VALUES (?, ?, 1)
                   ON CONFLICT(user_id, day)
                   DO UPDATE SET vip_used=vip_used+1""",
                (user_id, today),
            )
            return ("vip", today)

        if int(row["credits"] or 0) > 0:
            db.execute(
                "UPDATE users SET credits=credits-1 WHERE user_id=?",
                (user_id,),
            )
            return ("credit", today)

        if active_vip:
            db.rollback()
            raise UserFacingError(
                f"Лимит VIP на сегодня исчерпан ({VIP_DAILY_LIMIT}). "
                "Попробуй завтра или пополни баланс."
            )

        if free_used >= FREE_DAILY_LIMIT:
            db.rollback()
            raise UserFacingError(
                f"Бесплатные генерации на сегодня закончились "
                f"({FREE_DAILY_LIMIT}/{FREE_DAILY_LIMIT}).\n"
                "Открой «⭐ VIP / Пополнение», чтобы продолжить."
            )

        db.execute(
            """INSERT INTO daily_usage(user_id, day, free_used)
               VALUES (?, ?, 1)
               ON CONFLICT(user_id, day)
               DO UPDATE SET free_used=free_used+1""",
            (user_id, today),
        )

        return ("free", today)


def refund_reservation(user_id: int, reservation: tuple[str, str]) -> None:
    kind, day = reservation

    if kind == "admin":
        return

    with database() as db:
        if kind == "credit":
            db.execute(
                "UPDATE users SET credits=credits+1 WHERE user_id=?",
                (user_id,),
            )
        else:
            column = "vip_used" if kind == "vip" else "free_used"
            db.execute(
                f"""UPDATE daily_usage
                    SET {column}=MAX(0, {column}-1)
                    WHERE user_id=? AND day=?""",
                (user_id, day),
            )


def profile_text(user_id: int) -> str:
    row = get_user(user_id)

    if not row:
        return "Профиль ещё не создан. Нажми /start."

    credits = int(row["credits"] or 0)
    vip_until = row["vip_until"]
    today = day_key()
    active = vip_active(vip_until)
    column = "vip_used" if active else "free_used"

    with database() as db:
        usage = db.execute(
            f"SELECT {column} AS n FROM daily_usage WHERE user_id=? AND day=?",
            (user_id, today),
        ).fetchone()

    used = int(usage["n"]) if usage else 0

    if is_admin(user_id):
        used_display = "∞"
        tier = (
            "♾️ <b>Администратор: безлимитные генерации</b>\n"
            "<i>Квоты и фильтры самого Cloudflare по-прежнему действуют.</i>"
        )
    else:
        used_display = str(used)

        if active:
            try:
                shown_until = (
                    datetime.fromisoformat(vip_until)
                    .astimezone(KYIV_TZ)
                    .strftime("%d.%m.%Y %H:%M")
                )
            except Exception:
                shown_until = str(vip_until)

            tier = (
                f"👑 VIP активен до <b>{html.escape(shown_until)}</b> (Киев)\n"
                f"Лимит: {VIP_DAILY_LIMIT} генераций в сутки"
            )
        else:
            tier = (
                f"Бесплатный тариф: {FREE_DAILY_LIMIT} генераций в сутки"
            )

    return (
        "<b>📊 Твой профиль MarketPhoto AI</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💎 Платные генерации: <b>{credits}</b>\n"
        f"🎨 Использовано сегодня: <b>{used_display}</b>\n"
        f"👥 Приглашено друзей: <b>{get_referral_count(user_id)}</b>\n{tier}"
    )


def shop_text() -> str:
    return (
        "<b>⭐ VIP и пополнение</b>\n\n"
        f"🎨 25 генераций — {PRODUCTS['credits_25']['stars']} ⭐\n"
        f"🚀 100 генераций — {PRODUCTS['credits_100']['stars']} ⭐\n"
        f"👑 VIP на 30 дней — {PRODUCTS['vip_30d']['stars']} ⭐\n\n"
        f"VIP: до {VIP_DAILY_LIMIT} генераций в сутки. "
        "Оплата — Telegram Stars."
    )


def claim_daily_bonus(user_id: int) -> bool:
    """Award one bonus credit per Kyiv calendar day."""
    today = day_key()
    with database() as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO daily_bonus(user_id, day, created_at) VALUES (?, ?, ?)",
            (user_id, today, now_iso()),
        )
        if cursor.rowcount != 1:
            return False
        db.execute("UPDATE users SET credits=credits+1 WHERE user_id=?", (user_id,))
        return True


def save_generation_history(user_id: int, prompt: str, mode: str, file_id: str) -> None:
    with database() as db:
        db.execute(
            "INSERT INTO generation_history(user_id, prompt, mode, file_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, prompt[:MAX_PROMPT_LENGTH], mode[:60], file_id, now_iso()),
        )
        db.execute(
            "DELETE FROM generation_history WHERE user_id=? AND id NOT IN (SELECT id FROM generation_history WHERE user_id=? ORDER BY id DESC LIMIT 20)",
            (user_id, user_id),
        )


def get_last_generation(user_id: int) -> sqlite3.Row | None:
    with database() as db:
        return db.execute(
            "SELECT * FROM generation_history WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()


def list_generation_history(user_id: int, limit: int = 5) -> list[sqlite3.Row]:
    with database() as db:
        return list(db.execute(
            "SELECT * FROM generation_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, max(1, min(limit, 10))),
        ).fetchall())


def get_referral_count(user_id: int) -> int:
    with database() as db:
        row = db.execute("SELECT COUNT(*) AS n FROM referrals WHERE referrer_user_id=?", (user_id,)).fetchone()
        return int(row["n"] if row else 0)


def available_generation_count(user_id: int, ceiling: int = 4) -> int:
    if is_admin(user_id):
        return ceiling
    row = get_user(user_id)
    if not row:
        return 0
    credits = max(0, int(row["credits"] or 0))
    today = day_key()
    with database() as db:
        usage = db.execute("SELECT free_used, vip_used FROM daily_usage WHERE user_id=? AND day=?", (user_id, today)).fetchone()
    free_used = int(usage["free_used"]) if usage else 0
    vip_used = int(usage["vip_used"]) if usage else 0
    if vip_active(row["vip_until"]):
        vip_remaining = max(0, VIP_DAILY_LIMIT - vip_used)
        return min(ceiling, vip_remaining + credits)
    free_remaining = max(0, FREE_DAILY_LIMIT - free_used)
    return min(ceiling, free_remaining + credits)


async def generate_variant_batch(message: Message, user_id: int, prompt: str, mode: str, count: int) -> None:
    """Generate variants sequentially to avoid parallel Cloudflare calls per user."""
    completed = 0
    for index in range(count):
        try:
            await message.answer(f"🎲 Вариант {index + 1} из {count}: запускаю генерацию…")
            await begin_generation(message, prompt, mode, user_id_override=user_id)
            completed += 1
        except Exception:
            log.exception("Variant batch failed user_id=%s index=%s", user_id, index)
    log.info("Variant batch finished user_id=%s completed=%s requested=%s", user_id, completed, count)


def redeem_promo(user_id: int, code: str) -> tuple[bool, str]:
    normalized = code.strip().upper()
    if not normalized or len(normalized) > 32:
        return False, "Неверный код промоакции."
    with database() as db:
        promo = db.execute("SELECT * FROM promo_codes WHERE code=?", (normalized,)).fetchone()
        if not promo:
            return False, "Промокод не найден или уже отключён."
        if int(promo["uses"]) >= int(promo["max_uses"]):
            return False, "Лимит активаций этого промокода исчерпан."
        try:
            db.execute("INSERT INTO promo_redemptions(code, user_id, created_at) VALUES (?, ?, ?)", (normalized, user_id, now_iso()))
        except sqlite3.IntegrityError:
            return False, "Ты уже активировал этот промокод."
        credits = max(0, int(promo["credits"]))
        db.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (credits, user_id))
        db.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (normalized,))
        return True, f"Промокод активирован! Начислено {credits} кредитов 🎁"


def enhance_prompt_text(prompt: str) -> str:
    prompt = " ".join(prompt.split())
    return (
        f"{prompt}. Create a polished, high-quality image with a clear main subject, "
        "strong visual hierarchy, balanced composition, carefully controlled lighting, "
        "natural shadows, detailed materials, coherent color palette, clean edges, "
        "sharp focal point and professional art direction. Keep the scene readable, "
        "avoid clutter and unintended text or logos."
    )


async def translate_prompt_to_english(prompt: str) -> str:
    """Translate a prompt through a public endpoint; keep a safe fallback if unavailable."""
    if http_client is not None:
        try:
            response = await http_client.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": prompt},
                timeout=18.0,
            )
            response.raise_for_status()
            data = response.json()
            translated = "".join(str(part[0]) for part in data[0] if part and part[0])
            if translated.strip():
                return translated.strip()
        except Exception as exc:
            log.warning("Prompt translation endpoint failed: %s", exc)
    return (
        "Переводчик временно недоступен. Исходное описание: "
        + " ".join(prompt.split())
        + "\n\nПопробуй повторить запрос чуть позже."
    )


def make_tiktok_script(topic: str) -> str:
    idea = html.escape(topic[:500])
    return (
        "<b>🎬 Сценарий TikTok — 6 сцен по 10 секунд</b>\n"
        f"<b>Тема:</b> {idea}\n\n"
        "<b>Сцена 1 · 0–10 сек — Хук</b>\n"
        "Действие: показать героя и необычную ситуацию крупным планом.\n"
        "Реплика: «Подожди… ты сейчас серьёзно?!»\n"
        "Промпт: vertical 9:16 cinematic cartoon shot, expressive character, readable setting, slow camera push-in, Russian dialogue.\n\n"
        "<b>Сцена 2 · 10–20 сек — Объяснение</b>\n"
        "Действие: герой показывает, что случилось; не менять локацию резко.\n"
        "Реплика: «Сейчас объясню, почему всё пошло не так».\n"
        "Промпт: same character and clothes, same location, medium shot, natural gestures, Russian speech, consistent art style.\n\n"
        "<b>Сцена 3 · 20–30 сек — Проблема</b>\n"
        "Действие: появляется препятствие, герой реагирует удивлением.\n"
        "Реплика: «О нет, это ещё хуже, чем я думал!»\n"
        "Промпт: vertical animated scene, emotional reaction, clear continuity, no abrupt cuts, Russian dialogue.\n\n"
        "<b>Сцена 4 · 30–40 сек — Поворот</b>\n"
        "Действие: второй персонаж раскрывает неожиданный факт.\n"
        "Реплика: «Вообще-то, у меня есть один секрет…»\n"
        "Промпт: two characters in the same environment, expressive faces, carefully staged reveal, Russian speech.\n\n"
        "<b>Сцена 5 · 40–50 сек — Кульминация</b>\n"
        "Действие: быстрый, но понятный эмоциональный момент.\n"
        "Реплика: «Вот это поворот!»\n"
        "Промпт: dramatic but friendly climax, consistent character design, energetic camera move, vertical 9:16.\n\n"
        "<b>Сцена 6 · 50–60 сек — Финал</b>\n"
        "Действие: короткая развязка и повод посмотреть продолжение.\n"
        "Реплика: «А дальше будет ещё интереснее…»\n"
        "Промпт: satisfying final shot, hold on the characters for a moment, clean composition, Russian dialogue.\n\n"
        "<i>Заменяй реплики под свою историю. Для стабильности повторяй описание внешности персонажей в каждом видеопромпте.</i>"
    )


def referral_help_text(user_id: int, bot_username: str) -> str:
    count = get_referral_count(user_id)
    link = f"https://t.me/{bot_username}?start=ref_{user_id}" if bot_username else "Открой /start после получения username бота."
    return (
        "<b>👥 Реферальная программа</b>\n\n"
        "Пригласи друга своей ссылкой: когда новый пользователь впервые запустит бота по ней, "
        "ты получишь 3 кредита, а друг — 2. Один приглашённый пользователь учитывается только один раз.\n\n"
        f"👤 Приглашено: <b>{count}</b>\n"
        f"🔗 Твоя ссылка:\n<code>{html.escape(link)}</code>"
    )


def resize_reference_image(raw: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((448, 448), Image.Resampling.LANCZOS)

            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()

    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UserFacingError(
            "Не удалось прочитать фото. Отправь обычное изображение JPG или PNG."
        ) from exc


def cloudflare_error_codes(response: httpx.Response) -> set[int]:
    """Extract Cloudflare error codes from nested JSON."""

    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return set()

    codes: set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "code" in value:
                try:
                    codes.add(int(value["code"]))
                except (TypeError, ValueError):
                    pass

            for child in value.values():
                walk(child)

        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return codes


def extract_image_response(response: httpx.Response) -> bytes:
    content_type = response.headers.get("content-type", "").lower()

    if response.status_code >= 400:
        detail = response.text[:1000]

        try:
            body = response.json()
            detail = json.dumps(
                body.get("errors", body),
                ensure_ascii=False,
            )[:1000]
        except Exception:
            pass

        codes = cloudflare_error_codes(response)

        log.error(
            "Cloudflare HTTP %s codes=%s: %s",
            response.status_code,
            sorted(codes),
            detail,
        )

        if 3030 in codes:
            raise UserFacingError(
                "Cloudflare отклонил запрос проверкой безопасности (код 3030). "
                "Этот фильтр работает на стороне Cloudflare и не отключается настройкой бота. "
                "Попробуй безопасное, нейтральное описание сцены без жестокости "
                "и других деталей, которые могут быть неверно истолкованы. "
                "Лимит за неудачную генерацию возвращён."
            )

        if 3036 in codes:
            raise UserFacingError(
                "Бесплатная дневная квота Cloudflare исчерпана. "
                "Попробуй после её обновления или проверь план Cloudflare. "
                "Лимит бота за эту попытку возвращён."
            )

        if 3040 in codes:
            raise UserFacingError(
                "Cloudflare временно перегружен. Подожди немного и повтори попытку. "
                "Лимит за неудачную попытку возвращён."
            )

        if response.status_code in (401, 403):
            raise UserFacingError(
                "Cloudflare отклонил доступ. Проверь CLOUDFLARE_API_TOKEN "
                "и права Workers AI."
            )

        if response.status_code == 429:
            raise UserFacingError(
                "Cloudflare ограничил запрос или временно перегружен. "
                "Попробуй позже; лимит возвращён."
            )

        if response.status_code == 400:
            raise UserFacingError(
                "Cloudflare не принял запрос. Проверь описание изображения "
                "и попробуй сформулировать его иначе. "
                "Лимит за эту попытку возвращён."
            )

        raise UserFacingError(
            "Cloudflare временно не смог создать изображение. "
            "Лимит за эту попытку возвращён; попробуй позже."
        )

    if content_type.startswith("image/"):
        if response.content:
            return response.content
        raise UserFacingError(
            "Cloudflare вернул пустое изображение. Попробуй ещё раз."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        log.error(
            "Unexpected Cloudflare response type=%s body=%r",
            content_type,
            response.text[:300],
        )
        raise UserFacingError(
            "Cloudflare вернул ответ неизвестного формата. "
            "Проверь CLOUDFLARE_MODEL."
        ) from exc

    if isinstance(payload, dict) and payload.get("success") is False:
        log.error(
            "Cloudflare API error: %s",
            safe_text(payload.get("errors") or payload.get("messages")),
        )
        raise UserFacingError(
            "Cloudflare не смог создать изображение. "
            "Проверь модель и лимит API."
        )

    result = payload.get("result", payload) if isinstance(payload, dict) else payload
    encoded = result.get("image") if isinstance(result, dict) else None

    if not encoded and isinstance(result, dict):
        encoded = result.get("output")

    if isinstance(encoded, str):
        if encoded.startswith("data:") and "," in encoded:
            encoded = encoded.split(",", 1)[1]

        try:
            decoded = base64.b64decode(encoded, validate=False)
            if decoded:
                return decoded
        except Exception as exc:
            raise UserFacingError(
                "Cloudflare вернул повреждённые данные изображения."
            ) from exc

    log.error(
        "Unexpected Cloudflare payload keys=%s",
        list(payload.keys()) if isinstance(payload, dict) else type(payload),
    )

    raise UserFacingError(
        "Cloudflare не вернул файл изображения. Проверь название модели в Render."
    )


def _font_for_image(size: int):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/impact.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap_overlay_text(text: str, draw: ImageDraw.ImageDraw, font: Any, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        if draw.textbbox((0, 0), candidate, font=font, stroke_width=2)[2] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def apply_caption_overlay(image_bytes: bytes, prompt: str, mode: str) -> bytes:
    """Draw optional captions when the user separates text with a vertical bar."""
    if "|" not in prompt:
        return image_bytes
    parts = [part.strip() for part in prompt.split("|", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return image_bytes
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = source.convert("RGB")
        draw = ImageDraw.Draw(image)
        width, height = image.size
        max_width = max(120, width - 56)

        def draw_caption(caption: str, y: int, area_height: int) -> None:
            font = _font_for_image(max(22, min(76, width // 10)))
            lines = _wrap_overlay_text(caption, draw, font, max_width)
            while len(lines) * (font.size + 8) > area_height and getattr(font, "size", 26) > 22:
                font = _font_for_image(max(22, font.size - 4))
                lines = _wrap_overlay_text(caption, draw, font, max_width)
            line_height = max(24, getattr(font, "size", 26) + 8)
            total_height = line_height * len(lines)
            current_y = y
            for line in lines:
                box = draw.textbbox((0, 0), line, font=font, stroke_width=4)
                text_width = box[2] - box[0]
                x = max(12, (width - text_width) // 2)
                draw.text((x, current_y), line, font=font, fill="white", stroke_width=4, stroke_fill="black")
                current_y += line_height

        if mode == "meme":
            draw_caption(parts[0], 18, max(80, height // 4))
            bottom_area = max(80, height // 4)
            draw_caption(parts[1], max(16, height - bottom_area - 18), bottom_area)
        else:
            # For thumbnails/posters use the first part as the headline; the second is scene guidance.
            draw_caption(parts[0], 20, max(90, height // 3))

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except Exception:
        log.exception("Could not draw requested caption overlay")
        return image_bytes


def safe_text(value: Any, max_len: int = 900) -> str:
    try:
        return str(value or "")[:max_len]
    except Exception:
        return "unknown error"


async def generate_image(
    prompt: str,
    width: int,
    height: int,
    reference: bytes | None = None,
) -> bytes:
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        raise UserFacingError(
            "Не настроены CLOUDFLARE_ACCOUNT_ID или CLOUDFLARE_API_TOKEN "
            "в Render Environment."
        )

    if http_client is None:
        raise UserFacingError("HTTP-клиент не запущен. Перезапусти Render.")

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"
    )

    base_parts: list[tuple[str, tuple[Any, ...]]] = [
        ("prompt", (None, prompt, "text/plain; charset=utf-8")),
        ("width", (None, str(width), "text/plain")),
        ("height", (None, str(height), "text/plain")),
    ]

    if reference is not None:
        base_parts.append(
            (
                "input_image_0",
                ("reference.png", resize_reference_image(reference), "image/png"),
            )
        )

    response: httpx.Response | None = None
    # Retry only temporary server/capacity errors, never a safety-moderation rejection.
    max_attempts = 3

    try:
        for attempt in range(max_attempts):
            # Use a different seed for each attempt to obtain a different sample.
            # This does not disable or bypass Cloudflare's safety filter.
            attempt_parts = list(base_parts)
            attempt_parts.append(
                (
                    "seed",
                    (
                        None,
                        str(secrets.randbelow(2_147_483_647)),
                        "text/plain",
                    ),
                )
            )

            response = await asyncio.wait_for(
                http_client.post(
                    url,
                    files=attempt_parts,
                    headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
                ),
                timeout=CF_TIMEOUT,
            )

            codes = (
                cloudflare_error_codes(response)
                if response.status_code >= 400
                else set()
            )

            # Do not retry code 3030: it is a safety decision made by Cloudflare.
            # Retrying with new seeds to get around that decision is intentionally disabled.
            if 3030 in codes:
                break

            retryable = response.status_code in (
                408,
                500,
                502,
                503,
                504,
            ) or (
                response.status_code == 429 and 3040 in codes
            )

            if not retryable or attempt >= max_attempts - 1:
                break

            retry_after = response.headers.get("retry-after", "").strip()

            try:
                delay = (
                    min(8.0, max(1.0, float(retry_after)))
                    if retry_after
                    else 1.5 * (attempt + 1)
                )
            except ValueError:
                delay = 1.5 * (attempt + 1)

            log.warning(
                "Temporary Cloudflare response HTTP %s codes=%s; "
                "retry %s/%s in %.1fs",
                response.status_code,
                sorted(codes),
                attempt + 1,
                max_attempts - 1,
                delay,
            )

            await asyncio.sleep(delay)

    except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
        raise UserFacingError(
            f"Генерация не завершилась за {CF_TIMEOUT} секунд. "
            "Лимит будет возвращён; попробуй ещё раз позже."
        ) from exc

    except httpx.RequestError as exc:
        log.exception("Network error calling Cloudflare")

        raise UserFacingError(
            "Не удалось подключиться к Cloudflare. "
            "Проверь Render и повтори попытку."
        ) from exc

    if response is None:
        raise UserFacingError(
            "Cloudflare не вернул ответ. "
            "Лимит за эту попытку возвращён; попробуй ещё раз."
        )

    image_bytes = extract_image_response(response)

    try:
        with Image.open(io.BytesIO(image_bytes)) as result_image:
            result_image.verify()
    except Exception as exc:
        log.error(
            "Cloudflare response is not a valid image: %r",
            image_bytes[:40],
        )
        raise UserFacingError(
            "Cloudflare вернул некорректный файл изображения. "
            "Проверь Render Logs."
        ) from exc

    return image_bytes


async def download_telegram_image(message: Message) -> bytes:
    if bot is None:
        raise UserFacingError(
            "Бот ещё запускается. Попробуй через несколько секунд."
        )

    file_id: str | None = None

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (
        message.document.mime_type or ""
    ).startswith("image/"):
        if (
            message.document.file_size
            and message.document.file_size > MAX_UPLOAD_BYTES
        ):
            raise UserFacingError("Файл слишком большой. Максимум 10 МБ.")

        file_id = message.document.file_id

    if not file_id:
        raise UserFacingError(
            "Отправь фото JPG или PNG как фото или документ."
        )

    tg_file = await bot.get_file(file_id)
    buffer = io.BytesIO()

    await bot.download_file(tg_file.file_path, destination=buffer)

    raw = buffer.getvalue()

    if not raw:
        raise UserFacingError(
            "Не удалось загрузить фото. Отправь его ещё раз."
        )

    if len(raw) > MAX_UPLOAD_BYTES:
        raise UserFacingError("Файл слишком большой. Максимум 10 МБ.")

    resize_reference_image(raw)
    return raw


async def send_to_admins(text: str) -> None:
    if bot is None:
        return

    if not ADMIN_IDS:
        log.warning(
            "ADMIN_IDS is empty; admin notification skipped: %s",
            text[:200],
        )
        return

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            log.exception(
                "Could not send admin notification to %s",
                admin_id,
            )


def save_report(
    user_id: int,
    username: str | None,
    kind: str,
    body: str,
) -> int:
    with database() as db:
        cursor = db.execute(
            """INSERT INTO reports(user_id, username, kind, body, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, username, kind, body[:4000], now_iso()),
        )
        return int(cursor.lastrowid)


def estimated_generation_progress(elapsed_seconds: float) -> int:
    """A moving waiting-time estimate, not the model's internal progress."""
    elapsed = max(0.0, float(elapsed_seconds))
    # A rough waiting estimate drives the filling bar; the numeric value is not displayed.
    return min(92, max(8, int(8 + 84 * (1 - math.exp(-elapsed / 52.0)))))


def generation_progress_text(percent: int, frame_index: int, mode: str = "generate") -> str:
    """Animated text-only progress screen with rotating clock emojis and a filling bar."""
    percent = max(8, min(92, int(percent)))

    # These emojis change on each Telegram message edit, creating a small moving clock.
    clocks = ("🕛", "🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚")
    clock = clocks[frame_index % len(clocks)]

    phases = [
        ("🚀", "ЗАПУСКАЮ МАСТЕРСКУЮ", "Подготавливаю идею и композицию"),
        ("🧠", "СОБИРАЮ ОБРАЗ", "Персонажи и формы постепенно складываются вместе"),
        ("🎨", "РАБОТАЮ С ЦВЕТОМ", "Подбираю свет, оттенки и атмосферу"),
        ("🪄", "ДОБАВЛЯЮ ДЕТАЛИ", "Маленькие детали оживают на картинке"),
        ("💎", "ШЛИФУЮ РЕЗУЛЬТАТ", "Остаются последние творческие штрихи"),
        ("✨", "ПОЧТИ ГОТОВО", "Жду готовое изображение от сервиса"),
    ]
    phase_index = min(len(phases) - 1, max(0, (percent - 8) // 15))
    phase_icon, headline, subtitle = phases[phase_index]

    mode_names = {
        "generate": "Свободная генерация",
        "avatar": "Аватарка",
        "wallpaper": "Обои на телефон",
        "edit": "Редактирование фото",
        "product": "Фото товара",
        "meme": "Мем",
        "youtube_cover": "Обложка YouTube",
        "clothing": "Дизайн одежды",
        "poster": "Постер",
        "character": "Дизайн персонажа",
        "preset": "Выбранный стиль",
    }
    mode_name = html.escape(mode_names.get(mode, "Создание изображения"))

    # A progressive bar plus a sparkle that slides across the filled portion.
    width = 14
    filled = max(1, min(width - 1, round(percent / 100 * width)))
    bar_slots = ["▫️"] * width
    for slot in range(filled):
        bar_slots[slot] = "🟦"
    shine_pos = frame_index % filled
    bar_slots[shine_pos] = "✨"
    bar = "".join(bar_slots)

    # A tiny sparkle trail also changes location every frame.
    trails = (
        "✦ · · · · ·",
        "· ✧ · · · ·",
        "· · ✦ · · ·",
        "· · · ✧ · ·",
        "· · · · ✦ ·",
        "· · · · · ✧",
    )
    trail = trails[frame_index % len(trails)]
    spinners = ("◜", "◝", "◞", "◟")
    spinner = spinners[frame_index % len(spinners)]

    return (
        "╭━━━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "       <b>✨ MARKETPHOTO AI</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"{clock} {phase_icon} <b>{headline}</b> {spinner}\n"
        f"<i>{html.escape(subtitle)}</i>\n\n"
        f"<code>{bar}</code>\n"
        f"　　 {trail}\n\n"
        f"🎨 <b>Режим:</b> {mode_name}\n"
        "<i>Создаю изображение — полоска заполняется, огоньки движутся…</i>"
    )


async def animate_generation(status_message: Message, mode: str = "generate") -> None:
    """Update the text animation until generation completes."""
    started = time.monotonic()
    frame_index = 1  # frame zero is shown by begin_generation before this task starts

    while True:
        elapsed = time.monotonic() - started
        percent = estimated_generation_progress(elapsed)
        try:
            await status_message.edit_text(
                generation_progress_text(percent, frame_index, mode)
            )
        except TelegramAPIError as exc:
            # Telegram sometimes rate-limits edits; keep the generation alive.
            log.debug("Could not update generation animation: %s", exc)
        except Exception:
            log.exception("Unexpected generation animation error")

        frame_index += 1
        await asyncio.sleep(2.2)

async def stop_generation_animation(
    animation_task: asyncio.Task[None] | None,
) -> None:
    """Stop the animation cleanly."""
    if animation_task is None:
        return

    if not animation_task.done():
        animation_task.cancel()

    try:
        await animation_task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.debug("Generation animation task stopped: %s", exc)


def is_rick_and_morty_request(prompt: str) -> bool:
    """Detect common English and Russian spellings of the characters."""
    folded = prompt.casefold()

    has_rick = any(
        term in folded
        for term in ("rick", "рик", "рік", "санчез", "sanchez")
    )

    has_morty = any(
        term in folded
        for term in ("morty", "морти", "морті")
    )

    return has_rick and has_morty


def build_generation_prompt(prompt: str, mode: str) -> str:
    """Improve prompts for avatars and Rick-and-Morty fan-art."""
    is_rm = is_rick_and_morty_request(prompt)

    if is_rm and mode == "avatar":
        return (
            "Create a polished square 1:1 fan-art profile avatar of the "
            "fictional animated characters Rick Sanchez and Morty Smith "
            "from Rick and Morty. Make them recognizable: Rick is a tall, "
            "slender elderly cartoon scientist with spiky pale blue hair, "
            "a unibrow and a white lab coat; Morty is a teenage cartoon boy "
            "with short brown hair and a yellow T-shirt. Use clean 2D "
            "television-cartoon linework, expressive friendly faces, "
            "centered close-up composition, balanced color, crisp details "
            "and a simple contrasting background. Keep the scene "
            "non-violent and family-friendly: no blood, injuries, weapons, "
            "nudity, sexual content or disturbing details. "
            "User's requested details: " + prompt
        )

    if is_rm and mode != "edit":
        orientation = (
            "vertical 9:16 composition suitable for a phone wallpaper"
            if mode == "wallpaper"
            else "square 1:1 composition suitable for a profile image"
            if mode == "avatar"
            else "balanced 1:1 cartoon illustration composition"
        )

        return (
            "Create high-quality, clean 2D fan-art featuring the fictional "
            "animated characters Rick Sanchez and Morty Smith from Rick and "
            "Morty. Rick has spiky pale blue hair, a unibrow and a white lab "
            "coat; Morty has short brown hair and a yellow T-shirt. Keep both "
            "characters recognizable, with clear silhouettes, expressive "
            "faces, crisp linework and tasteful cinematic lighting. Use a "
            "friendly, non-violent scene with no blood, injuries, weapons, "
            "nudity, sexual content or disturbing details. "
            f"Composition: {orientation}. "
            f"User's requested scene and details: {prompt}"
        )

    if mode == "avatar":
        return (
            "Create a high-quality square 1:1 social media profile avatar. "
            "Center the main subject, use a clear silhouette, professional "
            "lighting, crisp detail, balanced colors and a clean background. "
            "User request: " + prompt
        )

    if mode == "wallpaper":
        return (
            "Create a vertical smartphone wallpaper, 9:16 composition, "
            "elegant design, visually rich background, and leave clean "
            "negative space near the top for phone clock and icons. "
            "User request: " + prompt
        )

    if mode == "edit":
        return (
            "Edit the reference image according to the instruction. "
            "Preserve the main subject and identity unless explicitly asked "
            "to change them. User instruction: " + prompt
        )

    if mode == "product":
        return "Create a premium e-commerce product photograph from this brief: " + prompt + ". Show the product clearly, accurate proportions, realistic materials, clean studio setup, softbox lighting, tasteful shadows, uncluttered background, commercial quality. Do not invent brand logos or tiny unreadable text."
    if mode == "meme":
        return "Create a funny, readable meme illustration based on this idea: " + prompt + ". Clear central joke, expressive but friendly character, strong visual hierarchy, simple background, leave clean space at top and bottom for caption text. Do not render gibberish lettering; reserve blank caption areas."
    if mode == "youtube_cover":
        return "Create a compelling YouTube thumbnail based on this topic: " + prompt + ". Landscape 16:9 composition, one dominant visual focal point, bold contrast, expressive subject, readable negative space for a title to be added later, crisp professional creator-style art direction. Avoid tiny text and watermarks."
    if mode == "clothing":
        return "Create an original fashion design concept for clothing based on: " + prompt + ". Show the garment clearly on a neutral mannequin or clean flat-lay, detailed fabric and seams, cohesive color palette, production-inspired design presentation, no existing brand logos."
    if mode == "poster":
        return "Create a polished poster design concept for: " + prompt + ". Strong central artwork, clear visual hierarchy, elegant negative space reserved for title and date, professional typography-friendly layout, balanced colors, print-poster quality. Keep any generated lettering minimal and readable."
    if mode == "character":
        return "Design an original fictional character based on: " + prompt + ". Show a full-body character sheet, front-facing pose, expressive face, distinctive but coherent clothing, clear silhouette, consistent materials and colors, simple neutral background, professional animation concept art."
    if mode == "wallpaper":
        return "Create a vertical smartphone wallpaper, 9:16 composition, elegant design, visually rich background, leave clean negative space near the top for phone clock and icons. User request: " + prompt
    if mode == "preset":
        return prompt

    return prompt


async def begin_generation(
    message: Message,
    prompt: str,
    mode: str,
    reference: bytes | None = None,
    user_id_override: int | None = None,
) -> None:
    user = message.from_user

    if user is None:
        return

    user_id = user_id_override if user_id_override is not None else user.id

    if user_id in in_progress:
        await message.answer(
            "⏳ Твой предыдущий запрос ещё выполняется. Дождись результата."
        )
        return

    prompt = prompt.strip()

    if not prompt:
        await message.answer(
            "Напиши, какое изображение нужно создать.",
            reply_markup=CANCEL_KB,
        )
        return

    if len(prompt) > MAX_PROMPT_LENGTH:
        await message.answer(
            f"Запрос слишком длинный. Максимум {MAX_PROMPT_LENGTH} символов."
        )
        return

    width, height = 1024, 1024

    if mode == "wallpaper":
        width, height = 768, 1360
    elif mode == "youtube_cover":
        width, height = 1024, 576
    elif mode == "poster":
        width, height = 768, 1024

    final_prompt = build_generation_prompt(prompt, mode)

    in_progress.add(user_id)

    reservation: tuple[str, str] | None = None
    status_message: Message | None = None
    animation_task: asyncio.Task[None] | None = None

    try:
        reservation = reserve_generation(user_id)

        status_message = await message.answer(
            generation_progress_text(8, 0, mode),
            reply_markup=MAIN_MENU,
        )

        animation_task = asyncio.create_task(
            animate_generation(status_message, mode)
        )

        if bot:
            await bot.send_chat_action(
                message.chat.id,
                ChatAction.UPLOAD_PHOTO,
            )

        image_bytes = await generate_image(
            final_prompt,
            width,
            height,
            reference,
        )
        if mode in {"meme", "youtube_cover", "poster"}:
            image_bytes = apply_caption_overlay(image_bytes, prompt, mode)

        await stop_generation_animation(animation_task)
        animation_task = None

        sent_image = await message.answer_photo(
            BufferedInputFile(
                image_bytes,
                filename="marketphoto.png",
            ),
            caption="✨ <b>Готово!</b> Создано в MarketPhoto AI.",
            reply_markup=RESULT_KB,
        )
        try:
            file_id = sent_image.photo[-1].file_id if sent_image.photo else ""
            if file_id:
                save_generation_history(user_id, prompt, mode, file_id)
        except Exception:
            log.exception("Could not save generation history for user_id=%s", user_id)

        if status_message:
            try:
                await status_message.edit_text(
                    "╭━━━━━━━━━━━━━━━━━━━━━━━━╮\n"
                    "       <b>✨ MARKETPHOTO AI</b>\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                    "✅ <b>ТВОЯ КАРТИНКА ГОТОВА!</b>\n\n"
                    "🖼️ <i>Изображение создано и отправлено в чат.</i>\n"
                    "🌟 <i>Нажми «Ещё вариант», чтобы попробовать другой образ.</i>"
                )
            except Exception as exc:
                log.debug(
                    "Could not show completed progress (image already sent): %s",
                    exc,
                )

        log.info(
            "Generation success user_id=%s mode=%s model=%s",
            user_id,
            mode,
            CF_MODEL,
        )

    except UserFacingError as exc:
        await stop_generation_animation(animation_task)
        animation_task = None

        if reservation:
            refund_reservation(user_id, reservation)

        answer = "❌ " + html.escape(str(exc))

        if status_message:
            try:
                await status_message.edit_text(answer)
            except Exception as edit_exc:
                log.debug(
                    "Could not edit generation error status: %s",
                    edit_exc,
                )
                await message.answer(
                    answer,
                    reply_markup=MAIN_MENU,
                )
        else:
            await message.answer(
                answer,
                reply_markup=MAIN_MENU,
            )

        log.warning(
            "Generation failed user_id=%s: %s",
            user_id,
            exc,
        )

    except Exception:
        await stop_generation_animation(animation_task)
        animation_task = None

        if reservation:
            refund_reservation(user_id, reservation)

        log.exception(
            "Unhandled generation error user_id=%s",
            user_id,
        )

        text = (
            "❌ Не удалось создать изображение. Лимит восстановлен. "
            "Проверь Render Logs и попробуй снова."
        )

        if status_message:
            try:
                await status_message.edit_text(text)
            except Exception as edit_exc:
                log.debug(
                    "Could not edit generation failure status: %s",
                    edit_exc,
                )
                await message.answer(
                    text,
                    reply_markup=MAIN_MENU,
                )
        else:
            await message.answer(
                text,
                reply_markup=MAIN_MENU,
            )

    finally:
        await stop_generation_animation(animation_task)
        in_progress.discard(user_id)


async def create_invoice(
    message: Message,
    product_code: str,
) -> None:
    if bot is None:
        return

    product = PRODUCTS.get(product_code)

    if not product:
        await message.answer(
            "Товар не найден. Открой магазин через /buy."
        )
        return

    payload = f"{product_code}:{secrets.token_urlsafe(12)}"

    try:
        await message.answer_invoice(
            title=product["title"],
            description=product["description"],
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[
                LabeledPrice(
                    label=product["title"],
                    amount=int(product["stars"]),
                )
            ],
        )
    except TelegramAPIError:
        log.exception(
            "Invoice creation failed product=%s",
            product_code,
        )
        await message.answer(
            "Не удалось открыть оплату. Проверь Render Logs и повтори позже."
        )


@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject) -> None:
    if message.from_user:
        user = message.from_user
        existing = get_user(user.id)
        ensure_user(user)
        args = (command.args or "").strip()
        if existing is None and args.startswith("ref_"):
            try:
                referrer_id = int(args[4:])
            except ValueError:
                referrer_id = 0
            if referrer_id > 0 and referrer_id != user.id:
                with database() as db:
                    referrer = db.execute("SELECT user_id FROM users WHERE user_id=?", (referrer_id,)).fetchone()
                    if referrer:
                        cursor = db.execute(
                            "INSERT OR IGNORE INTO referrals(invited_user_id, referrer_user_id, created_at) VALUES (?, ?, ?)",
                            (user.id, referrer_id, now_iso()),
                        )
                        if cursor.rowcount == 1:
                            db.execute("UPDATE users SET credits=credits+3 WHERE user_id=?", (referrer_id,))
                            db.execute("UPDATE users SET credits=credits+2 WHERE user_id=?", (user.id,))
                            try:
                                await bot.send_message(referrer_id, "🎉 Друг пришёл по твоей ссылке! Тебе начислено 3 кредита, ему — 2.")
                            except Exception:
                                log.debug("Could not notify referrer %s", referrer_id)
        await message.answer(
            "<b>✨ Добро пожаловать в MarketPhoto AI!</b>\n\n"
            "Создавай изображения, аватарки, обложки, мемы, фото товаров, постеры и дизайн одежды. "
            "Можно улучшать промпты, делать сценарии TikTok и редактировать фото.\n\n"
            f"🎁 Бесплатно: <b>{FREE_DAILY_LIMIT} генераций в сутки</b>\n"
            "🎁 Заходи каждый день за бонусным кредитом.\n"
            "Выбери функцию в меню ниже.",
            reply_markup=MAIN_MENU,
        )

@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Команды MarketPhoto AI</b>\n"
        "/start — главное меню\n"
        "/profile — профиль и лимиты\n"
        "/buy — пополнение и VIP\n"
        "/terms — условия сервиса\n"
        "/paysupport — поддержка по оплате\n"
        "/cancel — отменить действие\n"
        "/bonus — ежедневный бонус\n"
        "/history — последние изображения\n"
        "/referral — реферальная ссылка\n"
        "/promo CODE — активировать промокод",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    if message.from_user:
        user_flows.pop(message.from_user.id, None)

    await message.answer(
        "Действие отменено.",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    if message.from_user:
        ensure_user(message.from_user)
        await message.answer(
            profile_text(message.from_user.id),
            reply_markup=MAIN_MENU,
        )


@dp.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer(
        shop_text(),
        reply_markup=SHOP_KB,
    )


@dp.message(Command("bonus"))
async def cmd_bonus(message: Message) -> None:
    if not message.from_user:
        return
    ensure_user(message.from_user)
    if claim_daily_bonus(message.from_user.id):
        await message.answer("🎁 <b>Ежедневный бонус получен!</b>\nНа баланс начислен 1 кредит. Возвращайся завтра за следующим.", reply_markup=MAIN_MENU)
    else:
        await message.answer("🎁 Сегодня ты уже забрал бонус. Приходи завтра!", reply_markup=MAIN_MENU)


@dp.message(Command("history"))
async def cmd_history(message: Message) -> None:
    if not message.from_user:
        return
    rows = list_generation_history(message.from_user.id, 5)
    if not rows:
        await message.answer("🕘 История пока пустая. Создай первое изображение!", reply_markup=MAIN_MENU)
        return
    buttons = []
    lines = ["<b>🕘 Последние изображения</b>"]
    for row in rows:
        created = html.escape(str(row["created_at"])[:16].replace("T", " "))
        title = html.escape(str(row["prompt"])[:38])
        lines.append(f"• {created} — {title}")
        buttons.append([InlineKeyboardButton(text=f"🖼️ {title[:32]}", callback_data=f"history:{int(row['id'])}")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.message(Command("referral"))
async def cmd_referral(message: Message) -> None:
    if not message.from_user:
        return
    username = ""
    if bot:
        try:
            username = (await bot.get_me()).username or ""
        except Exception:
            log.exception("Unable to get bot username")
    await message.answer(referral_help_text(message.from_user.id, username), reply_markup=MAIN_MENU)


@dp.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    code = (command.args or "").strip()
    if not code:
        await message.answer("Введи промокод так: <code>/promo MARKET10</code>")
        return
    ok, result = redeem_promo(message.from_user.id, code)
    await message.answer(("✅ " if ok else "❌ ") + html.escape(result), reply_markup=MAIN_MENU)


@dp.message(Command("promocode"))
async def cmd_create_promo(message: Message, command: CommandObject) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    parts = (command.args or "").split()
    if len(parts) not in (2, 3):
        await message.answer("Формат: <code>/promocode CODE КРЕДИТЫ [ЛИМИТ]</code>")
        return
    code = parts[0].upper()[:32]
    try:
        credits = int(parts[1])
        max_uses = int(parts[2]) if len(parts) == 3 else 100
    except ValueError:
        await message.answer("Кредиты и лимит должны быть целыми числами.")
        return
    if credits < 1 or credits > 10000 or max_uses < 1 or max_uses > 100000:
        await message.answer("Кредиты: 1–10000, лимит активаций: 1–100000.")
        return
    with database() as db:
        try:
            db.execute("INSERT INTO promo_codes(code, credits, max_uses, created_at) VALUES (?, ?, ?, ?)", (code, credits, max_uses, now_iso()))
        except sqlite3.IntegrityError:
            await message.answer("Такой промокод уже существует.")
            return
    await message.answer(f"✅ Промокод <code>{html.escape(code)}</code> создан: {credits} кредитов, до {max_uses} активаций.")


@dp.message(Command("terms"))
async def cmd_terms(message: Message) -> None:
    await message.answer(
        "<b>Условия MarketPhoto AI</b>\n\n"
        f"• Бесплатно: {FREE_DAILY_LIMIT} генераций в сутки.\n"
        f"• VIP: до {VIP_DAILY_LIMIT} генераций в сутки, пока активна подписка.\n"
        "• Платные генерации начисляются после подтверждения оплаты Telegram Stars.\n"
        "• При технической ошибке генерации использованный лимит возвращается.\n"
        "• Не отправляй конфиденциальные данные. Результат зависит от модели и запроса.\n"
        "• Вопросы по оплате: /paysupport.",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {"mode": "support"}

    await message.answer(
        "Опиши проблему с оплатой одним сообщением. Для отмены отправь /cancel.",
        reply_markup=CANCEL_KB,
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return

    await message.answer(
        "<b>🛠 Панель администратора</b>\n"
        "/stats — статистика\n"
        "/setcredits USER_ID AMOUNT — изменить баланс\n"
        "/vip USER_ID DAYS — выдать VIP\n"
        "/refund CHARGE_ID — вернуть Stars-платёж\n"
        "/broadcast ТЕКСТ — рассылка\n"
        "/reply USER_ID ТЕКСТ — ответ пользователю\n"
        "/promocode CODE КРЕДИТЫ [ЛИМИТ] — создать промокод"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return

    with database() as db:
        users_n = db.execute(
            "SELECT COUNT(*) n FROM users"
        ).fetchone()["n"]

        reports_n = db.execute(
            "SELECT COUNT(*) n FROM reports WHERE status='new'"
        ).fetchone()["n"]

        stars_n = db.execute(
            "SELECT COALESCE(SUM(stars),0) n FROM payments WHERE refunded=0"
        ).fetchone()["n"]

        payments_n = db.execute(
            "SELECT COUNT(*) n FROM payments WHERE refunded=0"
        ).fetchone()["n"]

        generations_n = db.execute(
            "SELECT COUNT(*) n FROM generation_history"
        ).fetchone()["n"]

        today = today_key()
        generations_today_n = db.execute(
            "SELECT COUNT(*) n FROM generation_history WHERE substr(created_at, 1, 10)=?",
            (today,),
        ).fetchone()["n"]

        active_today_n = db.execute(
            "SELECT COUNT(DISTINCT user_id) n FROM daily_usage WHERE day=?",
            (today,),
        ).fetchone()["n"]

    await message.answer(
        "<b>📈 Статистика MarketPhoto AI</b>\n"
        f"👥 Всего пользователей: <b>{users_n}</b>\n"
        f"🟢 Активных сегодня: <b>{active_today_n}</b>\n"
        f"🎨 Успешных генераций всего: <b>{generations_n}</b>\n"
        f"📅 Успешных генераций сегодня: <b>{generations_today_n}</b>\n"
        f"📩 Новых обращений: <b>{reports_n}</b>\n"
        f"💳 Не возвращённых платежей: <b>{payments_n}</b>\n"
        f"⭐ Получено Stars: <b>{stars_n}</b>"
    )


@dp.message(Command("setcredits"))
async def cmd_setcredits(
    message: Message,
    command: CommandObject,
) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return

    try:
        user_id_str, amount_str = (command.args or "").split(maxsplit=1)
        target_id, amount = int(user_id_str), int(amount_str)

        if amount < 0 or amount > 1_000_000:
            raise ValueError

    except ValueError:
        await message.answer("Формат: /setcredits USER_ID AMOUNT")
        return

    with database() as db:
        cur = db.execute(
            "UPDATE users SET credits=? WHERE user_id=?",
            (amount, target_id),
        )
        changed = cur.rowcount > 0

    if not changed:
        await message.answer(
            "Пользователь не найден. Сначала он должен нажать /start."
        )
        return

    await message.answer(
        f"Баланс пользователя {target_id}: {amount} генераций."
    )

    if bot:
        try:
            await bot.send_message(
                target_id,
                f"Администратор изменил твой баланс генераций: {amount}.",
            )
        except TelegramAPIError:
            pass


@dp.message(Command("vip"))
async def cmd_vip(
    message: Message,
    command: CommandObject,
) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return

    try:
        user_str, days_str = (command.args or "").split(maxsplit=1)
        target_id, days = int(user_str), int(days_str)

        if not 1 <= days <= 3650:
            raise ValueError

    except ValueError:
        await message.answer("Формат: /vip USER_ID DAYS")
        return

    row = get_user(target_id)

    if not row:
        await message.answer(
            "Пользователь не найден. Сначала он должен нажать /start."
        )
        return

    expiry_base = now_utc()

    if vip_active(row["vip_until"]):
        expiry_base = max(
            expiry_base,
            datetime.fromisoformat(row["vip_until"]),
        )

    expiry = (expiry_base + timedelta(days=days)).isoformat()

    with database() as db:
        db.execute(
            "UPDATE users SET vip_until=? WHERE user_id=?",
            (expiry, target_id),
        )

    await message.answer(
        f"VIP выдан пользователю {target_id} на {days} дней."
    )

    if bot:
        try:
            await bot.send_message(
                target_id,
                f"👑 Администратор выдал тебе VIP на {days} дней!",
            )
        except TelegramAPIError:
            pass


@dp.message(Command("refund"))
async def cmd_refund(
    message: Message,
    command: CommandObject,
) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return

    charge_id = (command.args or "").strip()

    if not charge_id:
        await message.answer("Формат: /refund CHARGE_ID")
        return

    with database() as db:
        payment = db.execute(
            "SELECT * FROM payments WHERE charge_id=?",
            (charge_id,),
        ).fetchone()

    if not payment:
        await message.answer("Платёж не найден.")
        return

    if payment["refunded"]:
        await message.answer("Этот платёж уже отмечен как возвращённый.")
        return

    if bot is None:
        await message.answer("Бот ещё запускается.")
        return

    try:
        await bot.refund_star_payment(
            user_id=int(payment["user_id"]),
            telegram_payment_charge_id=charge_id,
        )
    except TelegramAPIError:
        log.exception(
            "Stars refund failed charge_id=%s",
            charge_id,
        )
        await message.answer(
            "Telegram не подтвердил возврат. Платёж не изменён в базе."
        )
        return

    product = PRODUCTS.get(str(payment["product"]), {})

    with database() as db:
        db.execute(
            "UPDATE payments SET refunded=1 WHERE charge_id=?",
            (charge_id,),
        )

        if int(product.get("credits", 0)) > 0:
            db.execute(
                "UPDATE users SET credits=MAX(0, credits-?) WHERE user_id=?",
                (
                    int(product["credits"]),
                    int(payment["user_id"]),
                ),
            )

        elif int(product.get("days", 0)) > 0:
            current = db.execute(
                "SELECT vip_until FROM users WHERE user_id=?",
                (int(payment["user_id"]),),
            ).fetchone()

            if (
                current
                and current["vip_until"] == payment["vip_granted_until"]
            ):
                db.execute(
                    "UPDATE users SET vip_until=? WHERE user_id=?",
                    (
                        payment["vip_previous"],
                        int(payment["user_id"]),
                    ),
                )

    await message.answer(
        "✅ Возврат Telegram Stars выполнен. Доступные бонусы от платежа "
        "отозваны, если это было безопасно."
    )

    try:
        await bot.send_message(
            int(payment["user_id"]),
            "Возврат платежа в Telegram Stars обработан администратором.",
        )
    except TelegramAPIError:
        pass


@dp.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message,
    command: CommandObject,
) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return

    text = (command.args or "").strip()

    if not text or len(text) > 3500:
        await message.answer(
            "Формат: /broadcast текст (до 3500 символов)"
        )
        return

    with database() as db:
        rows = db.execute(
            "SELECT user_id FROM users"
        ).fetchall()

    await message.answer(
        f"Начинаю рассылку для {len(rows)} пользователей…"
    )

    sent = failed = 0

    for row in rows:
        try:
            await message.bot.send_message(
                int(row["user_id"]),
                html.escape(text),
                reply_markup=MAIN_MENU,
            )
            sent += 1
            await asyncio.sleep(0.05)

        except TelegramAPIError:
            failed += 1

    await message.answer(
        f"Рассылка завершена. Доставлено: {sent}; не доставлено: {failed}."
    )


@dp.message(Command("reply"))
async def cmd_reply(
    message: Message,
    command: CommandObject,
) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return

    try:
        user_str, response_text = (command.args or "").split(maxsplit=1)
        target_id = int(user_str)
    except ValueError:
        await message.answer("Формат: /reply USER_ID текст ответа")
        return

    try:
        await message.bot.send_message(
            target_id,
            "💬 Ответ поддержки:\n\n" + html.escape(response_text),
        )
        await message.answer("Ответ отправлен.")

    except TelegramAPIError:
        await message.answer(
            "Не удалось отправить ответ. Проверь ID пользователя и доступность бота."
        )


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    product_code = (query.invoice_payload or "").split(":", 1)[0]
    product = PRODUCTS.get(product_code)

    valid = (
        bool(product)
        and query.currency == "XTR"
        and int(query.total_amount) == int(product["stars"])
    )

    if valid:
        await query.answer(ok=True)
    else:
        await query.answer(
            ok=False,
            error_message=(
                "Цена или товар изменились. Открой магазин и создай новый счёт."
            ),
        )


@dp.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    user = message.from_user
    payment = message.successful_payment

    if user is None or payment is None:
        return

    ensure_user(user)

    product_code = (payment.invoice_payload or "").split(":", 1)[0]
    product = PRODUCTS.get(product_code)

    if (
        not product
        or payment.currency != "XTR"
        or int(payment.total_amount) != int(product["stars"])
    ):
        log.error(
            "Unmatched successful payment user=%s payload=%s amount=%s currency=%s",
            user.id,
            payment.invoice_payload,
            payment.total_amount,
            payment.currency,
        )
        await message.answer(
            "Платёж получен, но заказ не удалось распознать автоматически. "
            "Напиши /paysupport."
        )
        return

    charge_id = payment.telegram_payment_charge_id
    awarded = False
    vip_previous: str | None = None
    vip_granted_until: str | None = None

    with database() as db:
        db.execute("BEGIN IMMEDIATE")

        cursor = db.execute(
            """INSERT OR IGNORE INTO payments(
                   charge_id, user_id, stars, product, created_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                charge_id,
                user.id,
                int(payment.total_amount),
                product_code,
                now_iso(),
            ),
        )

        awarded = cursor.rowcount == 1

        if awarded and int(product["credits"]) > 0:
            db.execute(
                "UPDATE users SET credits=credits+? WHERE user_id=?",
                (int(product["credits"]), user.id),
            )

        elif awarded and int(product["days"]) > 0:
            row = db.execute(
                "SELECT vip_until FROM users WHERE user_id=?",
                (user.id,),
            ).fetchone()

            vip_previous = row["vip_until"] if row else None
            base = now_utc()

            if vip_active(vip_previous):
                base = max(
                    base,
                    datetime.fromisoformat(vip_previous),
                )

            vip_granted_until = (
                base + timedelta(days=int(product["days"]))
            ).isoformat()

            db.execute(
                "UPDATE users SET vip_until=? WHERE user_id=?",
                (vip_granted_until, user.id),
            )

            db.execute(
                """UPDATE payments
                   SET vip_previous=?, vip_granted_until=?
                   WHERE charge_id=?""",
                (
                    vip_previous,
                    vip_granted_until,
                    charge_id,
                ),
            )

    if not awarded:
        await message.answer(
            "Этот платёж уже был обработан. Повторно бонусы не начисляются.",
            reply_markup=MAIN_MENU,
        )
        return

    await message.answer(
        f"✅ Оплата подтверждена! Получено: <b>{html.escape(product['title'])}</b>.",
        reply_markup=MAIN_MENU,
    )

    await send_to_admins(
        "💳 <b>Новый платёж MarketPhoto AI</b>\n"
        f"Пользователь: <code>{user.id}</code> "
        f"(@{html.escape(user.username or '—')})\n"
        f"Товар: {html.escape(product['title'])}\n"
        f"Сумма: {payment.total_amount} ⭐\n"
        f"Charge ID: <code>{html.escape(charge_id)}</code>"
    )


@dp.callback_query(F.data == "variant")
async def callback_variant(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    row = get_last_generation(uid)
    if not row or not callback.message:
        await callback.answer("Сначала создай изображение", show_alert=True)
        return
    if row["mode"] == "edit":
        await callback.answer("Для ещё одного варианта редактирования отправь исходное фото заново.", show_alert=True)
        return
    await callback.answer("Создаю ещё один вариант…")
    asyncio.create_task(begin_generation(callback.message, str(row["prompt"]), str(row["mode"]), user_id_override=uid))


@dp.callback_query(F.data == "variants4")
async def callback_variants4(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    row = get_last_generation(user_id)
    if not row or not callback.message:
        await callback.answer("Сначала создай изображение", show_alert=True)
        return
    if row["mode"] == "edit":
        await callback.answer("Для вариантов редактирования нужно снова отправить исходное фото.", show_alert=True)
        return
    count = available_generation_count(user_id, 4)
    if count <= 0:
        await callback.answer("Нет доступных генераций. Получи бонус или пополни баланс.", show_alert=True)
        return
    await callback.answer(f"Готовлю {count} вариант(а). Каждый вариант расходует одну генерацию.")
    asyncio.create_task(generate_variant_batch(callback.message, user_id, str(row["prompt"]), str(row["mode"]), count))


@dp.callback_query(F.data == "history")
async def callback_history(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return
    rows = list_generation_history(callback.from_user.id, 5)
    if not rows:
        await callback.message.answer("История пока пустая.")
        return
    buttons = []
    lines = ["<b>🕘 Последние изображения</b>"]
    for row in rows:
        title = html.escape(str(row["prompt"])[:36])
        created = html.escape(str(row["created_at"])[:16].replace("T", " "))
        lines.append(f"• {created} — {title}")
        buttons.append([InlineKeyboardButton(text=f"🖼️ {title[:30]}", callback_data=f"history:{int(row['id'])}")])
    await callback.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("history:"))
async def callback_history_item(callback: CallbackQuery) -> None:
    if not callback.message or bot is None:
        await callback.answer("Не удалось открыть историю", show_alert=True)
        return
    try:
        item_id = int((callback.data or "").split(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный элемент истории", show_alert=True)
        return
    with database() as db:
        row = db.execute("SELECT * FROM generation_history WHERE id=? AND user_id=?", (item_id, callback.from_user.id)).fetchone()
    if not row:
        await callback.answer("Элемент не найден", show_alert=True)
        return
    await callback.answer()
    await bot.send_photo(callback.message.chat.id, photo=str(row["file_id"]), caption="🕘 <b>Из истории MarketPhoto AI</b>\n" + html.escape(str(row["prompt"])[:700]), reply_markup=RESULT_KB)


@dp.callback_query(F.data == "use_enhanced")
async def callback_use_enhanced(callback: CallbackQuery) -> None:
    prompt = last_enhanced_prompts.get(callback.from_user.id)
    if not prompt or not callback.message:
        await callback.answer("Сначала улучши промпт", show_alert=True)
        return
    await callback.answer("Запускаю генерацию…")
    last_enhanced_prompts.pop(callback.from_user.id, None)
    await begin_generation(callback.message, prompt, "generate", user_id_override=callback.from_user.id)


@dp.callback_query(F.data.startswith("style:"))
async def callback_style(callback: CallbackQuery) -> None:
    key = (callback.data or "").split(":", 1)[1]
    style = STYLE_PRESETS.get(key)
    if not style:
        await callback.answer("Стиль не найден", show_alert=True)
        return
    user_flows[callback.from_user.id] = {"mode": "style_input", "style_key": key}
    await callback.answer()
    if callback.message:
        await callback.message.answer(f"Выбран стиль: <b>{html.escape(style[0])}</b>\nТеперь напиши, что нужно изобразить.", reply_markup=CANCEL_KB)


@dp.callback_query(F.data.startswith("tool:"))
async def callback_tool(callback: CallbackQuery) -> None:
    key = (callback.data or "").split(":", 1)[1]
    await callback.answer()
    if not callback.message:
        return
    if key == "prompt_library":
        await callback.message.answer("<b>💡 Каталог готовых промптов</b>\nВыбери шаблон — бот создаст изображение по готовому описанию.", reply_markup=PROMPT_LIBRARY_KB)
        return
    if key == "referral":
        username = ""
        if bot:
            try:
                username = (await bot.get_me()).username or ""
            except Exception:
                pass
        await callback.message.answer(referral_help_text(callback.from_user.id, username), reply_markup=MAIN_MENU)
        return
    if key == "custom_order":
        user_flows[callback.from_user.id] = {"mode": "custom_order"}
        await callback.message.answer("💼 Опиши, какой дизайн нужен, где он будет использоваться и желаемый срок. Я передам заявку администратору. Оплату и сроки согласуете отдельно.", reply_markup=CANCEL_KB)
        return
    if key == "improve_wait_photo":
        user_flows[callback.from_user.id] = {"mode": "improve_wait_photo", "edit_task": "improve"}
        await callback.message.answer("🧽 Отправь фото, которое нужно улучшить. Затем напиши пожелания.", reply_markup=CANCEL_KB)
        return
    if key == "background_wait_photo":
        user_flows[callback.from_user.id] = {"mode": "background_wait_photo", "edit_task": "background"}
        await callback.message.answer("🌄 Отправь фото, затем опиши желаемый фон. Я попробую сохранить главный объект.", reply_markup=CANCEL_KB)
        return
    if key in {"improve_prompt", "translate_prompt", "tiktok_script"}:
        user_flows[callback.from_user.id] = {"mode": key}
        descriptions = {
            "improve_prompt": "🧠 Отправь короткий промпт — улучшу его и дам кнопку для генерации.",
            "translate_prompt": "🌍 Отправь описание на украинском или русском — переведу его на английский для модели.",
            "tiktok_script": "🎬 Опиши идею видео. Составлю сценарий из 6 сцен по 10 секунд с русскими репликами и промптами.",
        }
        await callback.message.answer(descriptions[key], reply_markup=CANCEL_KB)
        return
    if key in {"product", "meme", "youtube_cover", "clothing", "poster", "character"}:
        user_flows[callback.from_user.id] = {"mode": key}
        descriptions = {
            "product": "🛍️ Опиши товар и рекламный стиль. Сделаю коммерческое изображение товара.",
            "meme": "😂 Опиши мем. Чтобы добавить надписи, напиши: ВЕРХНЯЯ НАДПИСЬ | НИЖНЯЯ НАДПИСЬ.",
            "youtube_cover": "📺 Для заголовка на обложке напиши: ЗАГОЛОВОК | описание картинки.",
            "clothing": "👕 Опиши одежду, фасон, ткань и цвета для оригинального дизайна.",
            "poster": "🪧 Для заголовка постера напиши: ЗАГОЛОВОК | описание дизайна.",
            "character": "🧑‍🎨 Опиши оригинального персонажа: внешний вид, одежду, характер и стиль.",
        }
        await callback.message.answer(descriptions[key], reply_markup=CANCEL_KB)
        return


@dp.callback_query(F.data.startswith("promptlib:"))
async def callback_prompt_library(callback: CallbackQuery) -> None:
    key = (callback.data or "").split(":", 1)[1]
    entry = PROMPT_LIBRARY.get(key)
    if not entry or not callback.message:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    await callback.answer("Готовлю изображение по шаблону…")
    asyncio.create_task(begin_generation(callback.message, entry[1], "generate", user_id_override=callback.from_user.id))


@dp.callback_query(F.data == "cancel")
async def callback_cancel(callback: CallbackQuery) -> None:
    user_flows.pop(callback.from_user.id, None)
    await callback.answer("Отменено")

    if callback.message:
        await callback.message.answer(
            "Действие отменено.",
            reply_markup=MAIN_MENU,
        )


@dp.callback_query(F.data == "terms")
async def callback_terms(callback: CallbackQuery) -> None:
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "Для просмотра условий отправь /terms; по оплате — /paysupport."
        )


@dp.callback_query(F.data == "again")
async def callback_again(callback: CallbackQuery) -> None:
    user_flows[callback.from_user.id] = {"mode": "generate"}
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "Опиши новое изображение одним сообщением.",
            reply_markup=CANCEL_KB,
        )


@dp.callback_query(F.data == "report")
async def callback_report(callback: CallbackQuery) -> None:
    user_flows[callback.from_user.id] = {"mode": "report"}
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "Опиши проблему с последним результатом. "
            "Обращение увидит администратор.",
            reply_markup=CANCEL_KB,
        )


@dp.callback_query(F.data.startswith("buy:"))
async def callback_buy(callback: CallbackQuery) -> None:
    await callback.answer()

    code = (callback.data or "").split(":", 1)[1]
    product = PRODUCTS.get(code)

    if not product:
        if callback.message:
            await callback.message.answer(
                "Товар не найден. Открой магазин через /buy."
            )
        return

    if callback.message:
        await create_invoice(callback.message, code)


@dp.message(F.text == "🎭 Стили AI")
async def menu_styles(message: Message) -> None:
    await message.answer("<b>🎭 Выбери стиль изображения</b>\nПосле выбора напиши тему — я объединю её со стилем.", reply_markup=STYLE_KB)


@dp.message(F.text == "🧰 Другие функции")
async def menu_tools(message: Message) -> None:
    await message.answer("<b>🧰 Инструменты MarketPhoto AI</b>\nВыбери нужную функцию:", reply_markup=TOOLS_KB)


@dp.message(F.text == "🎁 Бонус дня")
async def menu_daily_bonus(message: Message) -> None:
    if not message.from_user:
        return
    ensure_user(message.from_user)
    if claim_daily_bonus(message.from_user.id):
        await message.answer("🎁 <b>Бонус получен!</b> На баланс начислен 1 кредит. Возвращайся завтра.", reply_markup=MAIN_MENU)
    else:
        await message.answer("🎁 Сегодня бонус уже получен. Новый бонус будет доступен завтра.", reply_markup=MAIN_MENU)


@dp.message(F.text == "🕘 История")
async def menu_history(message: Message) -> None:
    if not message.from_user:
        return
    rows = list_generation_history(message.from_user.id, 5)
    if not rows:
        await message.answer("История пока пустая. Создай первое изображение!", reply_markup=MAIN_MENU)
        return
    buttons = []
    lines = ["<b>🕘 Последние изображения</b>"]
    for row in rows:
        title = html.escape(str(row["prompt"])[:36])
        created = html.escape(str(row["created_at"])[:16].replace("T", " "))
        lines.append(f"• {created} — {title}")
        buttons.append([InlineKeyboardButton(text=f"🖼️ {title[:30]}", callback_data=f"history:{int(row['id'])}")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.message(F.text == "🎨 Создать изображение")
async def menu_generate(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {"mode": "generate"}

    await message.answer(
        "Напиши, какое изображение нужно создать. "
        "Чем подробнее описание, тем лучше результат.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == "🪄 Редактировать фото")
async def menu_edit(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {"mode": "edit_wait_photo"}

    await message.answer(
        "Сначала отправь фото, которое нужно изменить. "
        "Потом я попрошу описание.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == "👤 Аватарка")
async def menu_avatar(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {"mode": "avatar"}

    await message.answer(
        "Опиши аватарку: персонаж, стиль, цвета и настроение.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == "📱 Обои на телефон")
async def menu_wallpaper(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {"mode": "wallpaper"}

    await message.answer(
        "Опиши обои для телефона. Создам вертикальное изображение.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == "⭐ VIP / Пополнение")
async def menu_shop(message: Message) -> None:
    await message.answer(
        shop_text(),
        reply_markup=SHOP_KB,
    )


@dp.message(F.text == "📊 Мой профиль")
async def menu_profile(message: Message) -> None:
    if message.from_user:
        ensure_user(message.from_user)
        await message.answer(
            profile_text(message.from_user.id),
            reply_markup=MAIN_MENU,
        )


@dp.message(F.text == "🆘 Поддержка")
async def menu_support(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {"mode": "support"}

    await message.answer(
        "Напиши сообщение для поддержки. Оно будет передано администратору.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.photo | F.document)
async def handle_incoming_image(message: Message) -> None:
    if not message.from_user:
        return

    flow = user_flows.get(message.from_user.id, {})

    if flow.get("mode") not in {"edit_wait_photo", "improve_wait_photo", "background_wait_photo"}:
        await message.answer(
            "Сначала выбери в меню «Редактировать фото», «Улучшить фото» или «Заменить фон», а затем отправь изображение.",
            reply_markup=MAIN_MENU,
        )
        return
    edit_task = flow.get("edit_task", "edit")

    try:
        image = await download_telegram_image(message)
    except UserFacingError as exc:
        await message.answer(
            "❌ " + html.escape(str(exc)),
            reply_markup=MAIN_MENU,
        )
        return

    user_flows[message.from_user.id] = {
        "mode": "edit_wait_prompt",
        "image": image,
        "edit_task": edit_task,
    }

    instruction = {
        "improve": "Фото получено ✅ Напиши, что улучшить (например, резкость, свет, цвета).",
        "background": "Фото получено ✅ Опиши новый фон (например, студия, пляж, город).",
    }.get(str(edit_task), "Фото получено ✅ Теперь напиши, что изменить.")
    await message.answer(
        instruction,
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text)
async def handle_text_flow(message: Message) -> None:
    if not message.from_user or not message.text:
        return

    user = message.from_user
    user_id = user.id
    ensure_user(user)

    flow = user_flows.get(user_id)

    if not flow:
        await message.answer(
            "Выбери действие в меню или введи /help.",
            reply_markup=MAIN_MENU,
        )
        return

    mode = flow.get("mode")
    text = message.text.strip()

    if mode in {"support", "report", "custom_order"}:
        kind = "custom_order" if mode == "custom_order" else ("support" if mode == "support" else "generation_issue")
        user_flows.pop(user_id, None)

        report_id = save_report(
            user_id,
            user.username,
            kind,
            text,
        )

        title = {
            "support": "Обращение в поддержку",
            "generation_issue": "Жалоба на результат генерации",
            "custom_order": "Новая заявка на дизайн",
        }.get(kind, "Обращение")

        await send_to_admins(
            f"🆘 <b>{title}</b>\n"
            f"Номер: #{report_id}\n"
            f"Пользователь: <code>{user_id}</code> "
            f"(@{html.escape(user.username or '—')})\n\n"
            f"{html.escape(text[:3500])}\n\n"
            f"Ответ: <code>/reply {user_id} текст</code>"
        )

        user_message = "✅ Заявка передана администратору. Он свяжется с тобой, если сможет выполнить заказ." if kind == "custom_order" else "✅ Сообщение передано. Спасибо!"
        await message.answer(user_message, reply_markup=MAIN_MENU)
        return

    if mode == "edit_wait_photo":
        await message.answer(
            "Сначала отправь фото, затем напиши, что изменить.",
            reply_markup=CANCEL_KB,
        )
        return

    if mode == "edit_wait_prompt":
        reference = flow.get("image")
        edit_task = str(flow.get("edit_task", "edit"))
        user_flows.pop(user_id, None)

        if not reference:
            await message.answer(
                "Фото потерялось после перезапуска. Начни заново "
                "через «🪄 Редактировать фото».",
                reply_markup=MAIN_MENU,
            )
            return

        task_prompts = {
            "improve": "Improve the supplied photo: enhance clarity, lighting, color balance and visible details while preserving the subject's identity and composition. User requests: ",
            "background": "Replace only the background as requested, keeping the main subject, pose, clothes and identity as consistent as possible. New background instruction: ",
        }
        edit_prompt = task_prompts.get(edit_task, "") + text
        await begin_generation(message, edit_prompt, "edit", reference=reference)
        return

    if mode == "style_input":
        style = STYLE_PRESETS.get(str(flow.get("style_key", "")))
        user_flows.pop(user_id, None)
        if not style:
            await message.answer("Стиль потерялся. Выбери его ещё раз.", reply_markup=MAIN_MENU)
            return
        await begin_generation(message, style[1] + " Subject and scene: " + text, "preset")
        return

    if mode == "improve_prompt":
        user_flows.pop(user_id, None)
        enhanced = enhance_prompt_text(text)
        last_enhanced_prompts[user_id] = enhanced
        await message.answer("<b>🧠 Улучшенный промпт</b>\n\n<code>" + html.escape(enhanced) + "</code>\n\nНажми кнопку, чтобы сгенерировать изображение по этому описанию.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎨 Сгенерировать", callback_data="use_enhanced")]]))
        return

    if mode == "translate_prompt":
        user_flows.pop(user_id, None)
        translated = await translate_prompt_to_english(text)
        await message.answer("<b>🌍 Английский промпт</b>\n\n<code>" + html.escape(translated[:3500]) + "</code>", reply_markup=MAIN_MENU)
        return

    if mode == "tiktok_script":
        user_flows.pop(user_id, None)
        await message.answer(make_tiktok_script(text), reply_markup=MAIN_MENU)
        return

    if mode in {"generate", "avatar", "wallpaper", "product", "meme", "youtube_cover", "clothing", "poster", "character"}:
        user_flows.pop(user_id, None)
        await begin_generation(message, text, str(mode))
        return

    user_flows.pop(user_id, None)

    await message.answer(
        "Действие сброшено. Выбери его в меню ещё раз.",
        reply_markup=MAIN_MENU,
    )


async def health_server() -> asyncio.AbstractServer:
    """Small HTTP health endpoint for Render."""

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await asyncio.wait_for(
                reader.readline(),
                timeout=3,
            )

            while True:
                line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=3,
                )

                if line in (b"\r\n", b"\n", b""):
                    break

            path = (
                request_line.decode("latin-1", errors="ignore").split(" ")[1]
                if request_line
                else "/"
            )

            body_obj = {
                "ok": True,
                "service": "marketphoto-ai",
                "build": BUILD_ID,
                "model": CF_MODEL,
                "telegram_polling": polling_active,
            }

            if path not in ("/", "/health"):
                status = "404 Not Found"
                body_obj = {
                    "ok": False,
                    "error": "not found",
                }
            else:
                status = "200 OK"

            body = json.dumps(
                body_obj,
                ensure_ascii=False,
            ).encode("utf-8")

            headers = (
                f"HTTP/1.1 {status}\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")

            writer.write(headers + body)
            await writer.drain()

        except Exception:
            pass

        finally:
            writer.close()

            try:
                await writer.wait_closed()
            except Exception:
                pass

    return await asyncio.start_server(
        handle,
        host="0.0.0.0",
        port=PORT,
    )


async def main() -> None:
    global bot, http_client, polling_active

    log.info("BUILD_ID=%s", BUILD_ID)
    log.info("RUNNING_FILE=%s", Path(__file__).resolve())
    log.info("IMAGE_TRANSPORT=httpx-multipart")

    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN environment variable")

    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        log.warning(
            "Cloudflare variables missing; bot starts, "
            "but generation will fail until configured"
        )

    if not ADMIN_IDS:
        log.warning(
            "ADMIN_IDS is empty; admin notifications and admin commands are disabled"
        )

    init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            CF_TIMEOUT,
            connect=20.0,
        ),
        follow_redirects=False,
    )

    server: asyncio.AbstractServer | None = None

    try:
        server = await health_server()
        log.info("Health server listening on port %s", PORT)
        log.info("Starting MarketPhoto AI. Model=%s", CF_MODEL)

        retry_delay = 2.0
        while True:
            try:
                # Keep any pending Telegram updates when a service restarts.
                await bot.delete_webhook(drop_pending_updates=False)
                polling_active = True
                log.info("Telegram polling starting")
                await dp.start_polling(
                    bot,
                    allowed_updates=dp.resolve_used_update_types(),
                )

                # A normal return usually means Render is shutting the service down
                # or the dispatcher was deliberately stopped; don't create a restart loop.
                polling_active = False
                log.info("Telegram polling stopped normally; shutting down cleanly")
                break

            except TelegramConflictError:
                polling_active = False
                log.critical(
                    "TelegramConflictError: another process is polling this same bot token. "
                    "Stop every other local/Render instance and leave exactly one poller running."
                )
                # A conflict won't fix itself while a second instance is running.
                raise

            except (KeyboardInterrupt, SystemExit):
                polling_active = False
                raise

            except Exception:
                polling_active = False
                log.exception(
                    "Telegram polling crashed. Restarting in %.1f seconds",
                    retry_delay,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2.0, 60.0)

    finally:
        polling_active = False
        if server is not None:
            server.close()
            await server.wait_closed()

        if http_client is not None:
            await http_client.aclose()
            http_client = None

        if bot is not None:
            await bot.session.close()
            bot = None


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("MarketPhoto AI stopped")
