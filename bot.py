from __future__ import annotations

import asyncio
import base64
import binascii
import html
import io
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite
import httpx
from aiohttp import web
from PIL import Image, ImageOps
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
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

# ============================================================
# MarketPhoto AI — Telegram bot for Render + Cloudflare Workers AI
# Cloudflare image generation uses httpx multipart requests.
# aiohttp.FormData(force_multipart=True) is NOT used.
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)
log = logging.getLogger("marketphoto")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CF_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()

CF_MODEL = os.getenv(
    "CLOUDFLARE_MODEL",
    "@cf/black-forest-labs/flux-2-klein-9b",
).strip()

ADMIN_IDS = {
    int(part.strip())
    for part in os.getenv("ADMIN_IDS", "").split(",")
    if part.strip().isdigit()
}

PORT = int(os.getenv("PORT", "10000"))
DB_PATH = Path(os.getenv("DB_PATH", "marketphoto.sqlite3"))

FREE_DAILY_LIMIT = max(
    0, int(os.getenv("FREE_DAILY_LIMIT", "5"))
)
VIP_DAILY_LIMIT = max(
    1, int(os.getenv("VIP_DAILY_LIMIT", "100"))
)
CF_TIMEOUT_SECONDS = max(
    30, int(os.getenv("CF_TIMEOUT_SECONDS", "180"))
)

MAX_PROMPT_LENGTH = 1800
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Prices are in Telegram Stars (XTR).
# All prices can be changed in Render Environment Variables.
PRODUCTS: dict[str, dict[str, Any]] = {
    "credits_25": {
        "title": "25 генераций",
        "description": "25 дополнительных генераций изображений",
        "stars": max(
            1, int(os.getenv("PRICE_CREDITS_25", "50"))
        ),
        "credits": 25,
    },
    "credits_100": {
        "title": "100 генераций",
        "description": "100 дополнительных генераций изображений",
        "stars": max(
            1, int(os.getenv("PRICE_CREDITS_100", "150"))
        ),
        "credits": 100,
    },
    "vip_30d": {
        "title": "VIP на 30 дней",
        "description": (
            "VIP-доступ до 100 генераций в день на 30 дней"
        ),
        "stars": max(
            1, int(os.getenv("PRICE_VIP_30D", "250"))
        ),
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
            KeyboardButton(text="⭐ VIP / Пополнение"),
            KeyboardButton(text="📊 Мой профиль"),
        ],
        [KeyboardButton(text="🆘 Поддержка")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Выберите действие…",
)

CANCEL_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✖️ Отмена",
                callback_data="cancel",
            )
        ]
    ]
)

SHOP_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text=(
                    f"🎨 25 генераций — "
                    f"{PRODUCTS['credits_25']['stars']} ⭐"
                ),
                callback_data="buy:credits_25",
            )
        ],
        [
            InlineKeyboardButton(
                text=(
                    f"🚀 100 генераций — "
                    f"{PRODUCTS['credits_100']['stars']} ⭐"
                ),
                callback_data="buy:credits_100",
            )
        ],
        [
            InlineKeyboardButton(
                text=(
                    f"👑 VIP 30 дней — "
                    f"{PRODUCTS['vip_30d']['stars']} ⭐"
                ),
                callback_data="buy:vip_30d",
            )
        ],
        [
            InlineKeyboardButton(
                text="ℹ️ Условия и поддержка",
                callback_data="terms",
            )
        ],
    ]
)

RESULT_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔁 Создать ещё",
                callback_data="again",
            )
        ],
        [
            InlineKeyboardButton(
                text="⚠️ Сообщить о проблеме",
                callback_data="report",
            )
        ],
    ]
)

bot: Bot | None = None
dp = Dispatcher()
http_client: httpx.AsyncClient | None = None

# Conversation state is kept in memory.
# Database records, balances and payments are stored in SQLite.
user_flows: dict[int, dict[str, Any]] = {}
in_progress: set[int] = set()


class UserFacingError(Exception):
    """An error with a safe message for the Telegram user."""


@dataclass
class Reservation:
    kind: str
    day: str


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def day_key() -> str:
    return datetime.now(KYIV_TZ).date().isoformat()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def safe_error_text(value: Any, limit: int = 900) -> str:
    return str(value or "")[:limit]


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(str(DB_PATH), timeout=15)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=15000")
    return db


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with await get_db() as db:
        await db.execute("PRAGMA journal_mode=WAL")

        await db.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL,
                credits INTEGER NOT NULL DEFAULT 0,
                vip_until TEXT
            )"""
        )

        await db.execute(
            """CREATE TABLE IF NOT EXISTS daily_usage (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                free_used INTEGER NOT NULL DEFAULT 0,
                vip_used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            )"""
        )

        # Migrate an older daily_usage table if necessary.
        cur = await db.execute("PRAGMA table_info(daily_usage)")
        usage_columns = {
            row[1] for row in await cur.fetchall()
        }

        if "free_used" not in usage_columns:
            await db.execute(
                "ALTER TABLE daily_usage "
                "ADD COLUMN free_used INTEGER NOT NULL DEFAULT 0"
            )

        if "vip_used" not in usage_columns:
            await db.execute(
                "ALTER TABLE daily_usage "
                "ADD COLUMN vip_used INTEGER NOT NULL DEFAULT 0"
            )

        await db.execute(
            """CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                stars INTEGER NOT NULL,
                product TEXT NOT NULL,
                created_at TEXT NOT NULL,
                refunded INTEGER NOT NULL DEFAULT 0
            )"""
        )

        await db.execute(
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

        await db.commit()

    log.info("Database initialized at %s", DB_PATH)


async def ensure_user(user: Any) -> None:
    async with await get_db() as db:
        await db.execute(
            """INSERT INTO users(
                user_id, username, first_name, created_at
            )
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
        await db.commit()


async def user_row(
    user_id: int,
) -> aiosqlite.Row | None:
    async with await get_db() as db:
        cur = await db.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,),
        )
        return await cur.fetchone()


def vip_active(vip_until: str | None) -> bool:
    if not vip_until:
        return False

    try:
        expires = datetime.fromisoformat(vip_until)

        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)

        return expires > now_utc()

    except (TypeError, ValueError):
        return False


async def reserve_generation(user_id: int) -> Reservation:
    """
    Reserve one generation atomically.
    If the image request fails, refund the reservation.
    """
    today = day_key()

    async with await get_db() as db:
        await db.execute("BEGIN IMMEDIATE")

        cur = await db.execute(
            "SELECT credits, vip_until FROM users WHERE user_id=?",
            (user_id,),
        )
        row = await cur.fetchone()

        if row is None:
            await db.rollback()
            raise UserFacingError(
                "Нажмите /start, чтобы зарегистрировать профиль."
            )

        # VIP uses its own daily counter.
        if vip_active(row["vip_until"]):
            cur = await db.execute(
                """SELECT vip_used FROM daily_usage
                   WHERE user_id=? AND day=?""",
                (user_id, today),
            )
            usage = await cur.fetchone()
            used = int(usage["vip_used"]) if usage else 0

            if used >= VIP_DAILY_LIMIT:
                await db.rollback()
                raise UserFacingError(
                    f"На сегодня исчерпан VIP-лимит "
                    f"({VIP_DAILY_LIMIT} генераций). "
                    "Попробуйте завтра."
                )

            await db.execute(
                """INSERT INTO daily_usage(user_id, day, vip_used)
                   VALUES (?, ?, 1)
                   ON CONFLICT(user_id, day)
                   DO UPDATE SET vip_used=vip_used+1""",
                (user_id, today),
            )

            await db.commit()
            return Reservation("vip", today)

        # Paid image credits are used before the free daily quota.
        if int(row["credits"] or 0) > 0:
            await db.execute(
                """UPDATE users SET credits=credits-1
                   WHERE user_id=?""",
                (user_id,),
            )
            await db.commit()
            return Reservation("credit", today)

        cur = await db.execute(
            """SELECT free_used FROM daily_usage
               WHERE user_id=? AND day=?""",
            (user_id, today),
        )
        usage = await cur.fetchone()
        used = int(usage["free_used"]) if usage else 0

        if used >= FREE_DAILY_LIMIT:
            await db.rollback()
            raise UserFacingError(
                f"Бесплатные генерации на сегодня закончились "
                f"({FREE_DAILY_LIMIT}/{FREE_DAILY_LIMIT}).\n"
                "Пополните баланс или подключите VIP "
                "в разделе «⭐ VIP / Пополнение»."
            )

        await db.execute(
            """INSERT INTO daily_usage(user_id, day, free_used)
               VALUES (?, ?, 1)
               ON CONFLICT(user_id, day)
               DO UPDATE SET free_used=free_used+1""",
            (user_id, today),
        )

        await db.commit()
        return Reservation("free", today)


async def refund_reservation(
    user_id: int,
    reservation: Reservation,
) -> None:
    async with await get_db() as db:
        if reservation.kind == "credit":
            await db.execute(
                """UPDATE users SET credits=credits+1
                   WHERE user_id=?""",
                (user_id,),
            )
        else:
            column = (
                "vip_used"
                if reservation.kind == "vip"
                else "free_used"
            )
            await db.execute(
                f"""UPDATE daily_usage
                    SET {column}=CASE
                        WHEN {column}>0 THEN {column}-1
                        ELSE 0
                    END
                    WHERE user_id=? AND day=?""",
                (user_id, reservation.day),
            )

        await db.commit()


async def profile_text(user_id: int) -> str:
    row = await user_row(user_id)

    if not row:
        return "Профиль ещё не создан. Нажмите /start."

    credits = int(row["credits"] or 0)
    vip = row["vip_until"]

    if vip_active(vip):
        try:
            vip_display = (
                datetime.fromisoformat(vip)
                .astimezone(KYIV_TZ)
                .strftime("%d.%m.%Y %H:%M")
            )
        except ValueError:
            vip_display = vip

        tier = (
            f"👑 VIP активен до <b>{vip_display}</b> (Киев)"
        )
        limit = (
            f"Лимит VIP: {VIP_DAILY_LIMIT} генераций в сутки"
        )
    else:
        tier = "Бесплатный тариф"
        limit = (
            f"Бесплатно: {FREE_DAILY_LIMIT} генераций в сутки"
        )

    today = day_key()
    usage_column = (
        "vip_used" if vip_active(vip) else "free_used"
    )

    async with await get_db() as db:
        cur = await db.execute(
            f"""SELECT {usage_column} AS used
                FROM daily_usage
                WHERE user_id=? AND day=?""",
            (user_id, today),
        )
        usage = await cur.fetchone()

    used = int(usage["used"]) if usage else 0

    return (
        "<b>📊 Твой профиль MarketPhoto AI</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💎 Платные генерации: <b>{credits}</b>\n"
        f"🎨 Использовано сегодня: <b>{used}</b>\n"
        f"{tier}\n{limit}"
    )


def shop_text() -> str:
    return (
        "<b>⭐ VIP и пополнение</b>\n\n"
        f"🎨 <b>25 генераций</b> — "
        f"{PRODUCTS['credits_25']['stars']} ⭐\n"
        f"🚀 <b>100 генераций</b> — "
        f"{PRODUCTS['credits_100']['stars']} ⭐\n"
        f"👑 <b>VIP на 30 дней</b> — "
        f"{PRODUCTS['vip_30d']['stars']} ⭐\n\n"
        f"VIP: до {VIP_DAILY_LIMIT} генераций в сутки. "
        "Оплата — Telegram Stars."
    )


def resize_reference_image(raw: bytes) -> bytes:
    """
    FLUX.2 Klein supports reference-image editing.
    Resize input images to 448x448 maximum before upload.
    """
    try:
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail(
                (448, 448),
                Image.Resampling.LANCZOS,
            )

            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)

            return buffer.getvalue()

    except Exception as exc:
        raise UserFacingError(
            "Не удалось прочитать фото. "
            "Отправьте обычное изображение JPG или PNG."
        ) from exc


def extract_image_response(
    response: httpx.Response,
) -> bytes:
    content_type = response.headers.get(
        "content-type", ""
    ).lower()

    if response.status_code >= 400:
        details = response.text[:1000]

        try:
            obj = response.json()
            details = json.dumps(
                obj.get("errors", obj),
                ensure_ascii=False,
            )[:1000]
        except Exception:
            pass

        log.error(
            "Cloudflare HTTP %s: %s",
            response.status_code,
            details,
        )

        if response.status_code in (401, 403):
            raise UserFacingError(
                "Cloudflare отклонил доступ. Проверьте "
                "CLOUDFLARE_API_TOKEN и его права на Workers AI."
            )

        if response.status_code == 429:
            raise UserFacingError(
                "Сервис генерации перегружен или достигнут "
                "лимит Cloudflare. Попробуйте позже."
            )

        raise UserFacingError(
            "Cloudflare не смог создать изображение. "
            "Попробуйте изменить запрос или проверьте Render Logs."
        )

    # Some APIs return the image as raw bytes.
    if content_type.startswith("image/"):
        if not response.content:
            raise UserFacingError(
                "Cloudflare вернул пустое изображение. "
                "Попробуйте ещё раз."
            )
        return response.content

    try:
        payload = response.json()
    except Exception as exc:
        log.error(
            "Cloudflare returned unexpected content-type=%s body=%r",
            content_type,
            response.text[:300],
        )
        raise UserFacingError(
            "Cloudflare вернул ответ неизвестного формата. "
            "Проверьте модель в настройках Render."
        ) from exc

    if (
        isinstance(payload, dict)
        and payload.get("success") is False
    ):
        detail = (
            payload.get("errors")
            or payload.get("messages")
            or "unknown API error"
        )
        log.error(
            "Cloudflare API error: %s",
            safe_error_text(detail),
        )
        raise UserFacingError(
            "Cloudflare не выполнил запрос. "
            "Проверьте доступность модели и лимиты API."
        )

    result = (
        payload.get("result", payload)
        if isinstance(payload, dict)
        else payload
    )

    encoded = (
        result.get("image")
        if isinstance(result, dict)
        else None
    )

    if not encoded and isinstance(result, dict):
        encoded = result.get("output")

    if isinstance(encoded, list) and encoded:
        encoded = encoded[0]

    if isinstance(encoded, str):
        if encoded.startswith("data:") and "," in encoded:
            encoded = encoded.split(",", 1)[1]

        try:
            image_data = base64.b64decode(
                encoded,
                validate=False,
            )
        except (binascii.Error, ValueError) as exc:
            raise UserFacingError(
                "Cloudflare прислал повреждённые данные изображения."
            ) from exc

        if image_data:
            return image_data

    log.error(
        "Unexpected Cloudflare payload keys=%s",
        list(payload.keys())
        if isinstance(payload, dict)
        else type(payload),
    )

    raise UserFacingError(
        "Cloudflare не вернул файл изображения. "
        "Проверьте название модели и Render Logs."
    )


async def generate_image(
    prompt: str,
    width: int,
    height: int,
    reference: bytes | None = None,
) -> bytes:
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        raise UserFacingError(
            "Не настроены CLOUDFLARE_ACCOUNT_ID или "
            "CLOUDFLARE_API_TOKEN в Render Environment."
        )

    if http_client is None:
        raise UserFacingError(
            "HTTP-клиент ещё не запущен. "
            "Перезапустите сервис Render."
        )

    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"
    )

    # Important:
    # httpx creates a valid multipart boundary automatically.
    # Using the files argument forces multipart/form-data,
    # including for prompt-only image-generation requests.
    # Do NOT replace this with aiohttp.FormData(force_multipart=True).
    parts: list[tuple[str, tuple[Any, ...]]] = [
        (
            "prompt",
            (None, prompt, "text/plain; charset=utf-8"),
        ),
        ("width", (None, str(width), "text/plain")),
        ("height", (None, str(height), "text/plain")),
    ]

    if reference is not None:
        parts.append(
            (
                "input_image_0",
                (
                    "reference.png",
                    resize_reference_image(reference),
                    "image/png",
                ),
            )
        )

    try:
        response = await http_client.post(
            url,
            files=parts,
        )

    except httpx.TimeoutException as exc:
        raise UserFacingError(
            "Генерация заняла слишком много времени. "
            "Попробуйте ещё раз чуть позже."
        ) from exc

    except httpx.RequestError as exc:
        log.exception("Network error calling Cloudflare")
        raise UserFacingError(
            "Не удалось подключиться к Cloudflare. "
            "Проверьте доступность сервиса и повторите попытку."
        ) from exc

    return extract_image_response(response)


async def download_telegram_image(
    message: Message,
) -> bytes:
    file_id: str | None = None

    if message.photo:
        file_id = message.photo[-1].file_id

    elif (
        message.document
        and (message.document.mime_type or "").startswith(
            "image/"
        )
    ):
        file_id = message.document.file_id

        if (
            message.document.file_size
            and message.document.file_size > MAX_UPLOAD_BYTES
        ):
            raise UserFacingError(
                "Файл слишком большой. Максимальный размер — 10 МБ."
            )

    if not file_id:
        raise UserFacingError(
            "Отправьте изображение как фото или как файл JPG/PNG."
        )

    telegram_file = await bot.get_file(file_id)

    buffer = io.BytesIO()
    await bot.download_file(
        telegram_file.file_path,
        destination=buffer,
    )

    raw = buffer.getvalue()

    if not raw:
        raise UserFacingError(
            "Не удалось загрузить фото из Telegram. "
            "Попробуйте отправить его ещё раз."
        )

    if len(raw) > MAX_UPLOAD_BYTES:
        raise UserFacingError(
            "Файл слишком большой. Максимальный размер — 10 МБ."
        )

    # Validate before asking for the edit prompt.
    resize_reference_image(raw)
    return raw


async def send_to_admins(text: str) -> None:
    if not ADMIN_IDS:
        log.warning(
            "ADMIN_IDS is empty; support notification not sent: %s",
            text[:250],
        )
        return

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            log.exception(
                "Could not send admin notification to %s",
                admin_id,
            )


async def save_report(
    user_id: int,
    username: str | None,
    kind: str,
    body: str,
) -> int:
    async with await get_db() as db:
        cur = await db.execute(
            """INSERT INTO reports(
                user_id, username, kind, body, created_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (
                user_id,
                username,
                kind,
                body[:4000],
                now_iso(),
            ),
        )

        await db.commit()
        return int(cur.lastrowid)


async def begin_generation(
    message: Message,
    prompt: str,
    mode: str,
    reference: bytes | None = None,
) -> None:
    user = message.from_user

    if user is None:
        return

    user_id = user.id

    if user_id in in_progress:
        await message.answer(
            "⏳ Твой предыдущий запрос ещё выполняется. "
            "Дождись результата."
        )
        return

    prompt = prompt.strip()

    if not prompt:
        await message.answer(
            "Напиши, что именно нужно изобразить.",
            reply_markup=CANCEL_KB,
        )
        return

    if len(prompt) > MAX_PROMPT_LENGTH:
        await message.answer(
            f"Запрос слишком длинный. "
            f"Максимум {MAX_PROMPT_LENGTH} символов."
        )
        return

    prompt_to_send = prompt
    width, height = 1024, 1024

    if mode == "avatar":
        prompt_to_send = (
            "Create a high-quality social media profile avatar. "
            "Centered main subject, clear silhouette, professional "
            "lighting, clean composition, attractive detailed "
            "background, square image. User request: "
            + prompt
        )

    elif mode == "wallpaper":
        width, height = 768, 1365

        prompt_to_send = (
            "Create a beautiful vertical 9:16 smartphone wallpaper, "
            "high resolution, carefully composed, leave some clean "
            "negative space near the top for phone clock and icons, "
            "no text unless requested. User request: "
            + prompt
        )

    reservation: Reservation | None = None
    in_progress.add(user_id)
    status_message: Message | None = None

    try:
        reservation = await reserve_generation(user_id)

        status_message = await message.answer(
            "⏳ <b>Создаю изображение…</b>\n"
            "Это иногда занимает до пары минут. "
            "Не отправляй тот же запрос повторно.",
            reply_markup=MAIN_MENU,
        )

        await bot.send_chat_action(
            message.chat.id,
            ChatAction.UPLOAD_PHOTO,
        )

        image_bytes = await generate_image(
            prompt_to_send,
            width,
            height,
            reference,
        )

        # Validate the returned image before delivering it.
        with Image.open(io.BytesIO(image_bytes)) as check_image:
            check_image.verify()

        await message.answer_photo(
            photo=BufferedInputFile(
                image_bytes,
                filename="marketphoto.png",
            ),
            caption="✨ <b>Готово!</b> Создано в MarketPhoto AI.",
            reply_markup=RESULT_KB,
        )

        if status_message:
            try:
                await status_message.delete()
            except Exception:
                pass

        log.info(
            "Generation success user_id=%s mode=%s model=%s",
            user_id,
            mode,
            CF_MODEL,
        )

    except UserFacingError as exc:
        if reservation:
            await refund_reservation(user_id, reservation)

        if status_message:
            try:
                await status_message.edit_text(
                    "❌ " + html.escape(str(exc)),
                    reply_markup=MAIN_MENU,
                )
            except Exception:
                await message.answer(
                    "❌ " + html.escape(str(exc)),
                    reply_markup=MAIN_MENU,
                )
        else:
            await message.answer(
                "❌ " + html.escape(str(exc)),
                reply_markup=MAIN_MENU,
            )

        log.warning(
            "Generation failed for user %s: %s",
            user_id,
            exc,
        )

    except Exception:
        if reservation:
            await refund_reservation(user_id, reservation)

        log.exception(
            "Unhandled image generation error for user_id=%s",
            user_id,
        )

        text = (
            "❌ Не удалось отправить изображение. "
            "Лимит восстановлен. Попробуй ещё раз; "
            "если ошибка повторится, администратор увидит её "
            "в Render Logs."
        )

        if status_message:
            try:
                await status_message.edit_text(
                    text,
                    reply_markup=MAIN_MENU,
                )
            except Exception:
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
        in_progress.discard(user_id)


async def create_invoice(
    message: Message,
    product_code: str,
) -> None:
    product = PRODUCTS.get(product_code)

    if not product:
        await message.answer(
            "Неизвестный товар. "
            "Открой раздел пополнения ещё раз."
        )
        return

    payload = f"{product_code}:{secrets.token_urlsafe(10)}"

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
                    amount=product["stars"],
                )
            ],
        )

    except Exception:
        log.exception(
            "Could not send invoice product=%s",
            product_code,
        )
        await message.answer(
            "Не удалось открыть оплату. "
            "Проверь настройки платежей и попробуй позже."
        )


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if message.from_user:
        await ensure_user(message.from_user)

    await message.answer(
        "<b>✨ Добро пожаловать в MarketPhoto AI!</b>\n\n"
        "Создавай изображения по описанию, делай аватарки "
        "и обои, а также редактируй свои фото.\n\n"
        f"🎁 Бесплатно: <b>{FREE_DAILY_LIMIT} генераций в день</b>\n"
        "Выбери функцию в меню ниже.",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Команды</b>\n"
        "/start — главное меню\n"
        "/profile — профиль и лимиты\n"
        "/buy — пополнение и VIP\n"
        "/paysupport — помощь по оплате\n"
        "/terms — условия сервиса\n"
        "/cancel — отменить текущее действие",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    if message.from_user:
        user_flows.pop(message.from_user.id, None)

    await message.answer(
        "Действие отменено. Выбери пункт в меню.",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    if message.from_user:
        await ensure_user(message.from_user)
        await message.answer(
            await profile_text(message.from_user.id),
            reply_markup=MAIN_MENU,
        )


@dp.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer(
        shop_text(),
        reply_markup=SHOP_KB,
    )


@dp.message(Command("terms"))
async def cmd_terms(message: Message) -> None:
    await message.answer(
        "<b>Условия MarketPhoto AI</b>\n\n"
        f"• Бесплатный тариф: {FREE_DAILY_LIMIT} успешных "
        "генераций в сутки.\n"
        f"• VIP: до {VIP_DAILY_LIMIT} успешных генераций "
        "в сутки в период действия подписки.\n"
        "• Платные генерации добавляются после подтверждённого "
        "платежа Telegram Stars.\n"
        "• Если генерация завершилась технической ошибкой, "
        "использованный лимит автоматически возвращается.\n"
        "• Результаты зависят от модели и текста запроса. "
        "Не отправляй конфиденциальные данные.\n"
        "• По вопросам оплаты используй /paysupport. "
        "Возврат платежей рассматривается администратором "
        "в соответствии с правилами Telegram.",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {
            "mode": "support"
        }

    await message.answer(
        "Опиши проблему с оплатой или подпиской одним сообщением. "
        "Я передам обращение администратору.\n"
        "Для отмены отправь /cancel.",
        reply_markup=CANCEL_KB,
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if (
        not message.from_user
        or not is_admin(message.from_user.id)
    ):
        await message.answer(
            "Команда доступна только администратору."
        )
        return

    await message.answer(
        "<b>🛠 Панель администратора</b>\n"
        "/stats — статистика\n"
        "/setcredits USER_ID AMOUNT — изменить баланс\n"
        "/vip USER_ID DAYS — выдать VIP\n"
        "/refund CHARGE_ID — вернуть Stars-платёж\n"
        "/broadcast ТЕКСТ — рассылка пользователям\n"
        "/reply USER_ID ТЕКСТ — ответ пользователю"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if (
        not message.from_user
        or not is_admin(message.from_user.id)
    ):
        await message.answer(
            "Команда доступна только администратору."
        )
        return

    async with await get_db() as db:
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM users"
        )
        users_n = int((await cur.fetchone())["n"])

        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM reports WHERE status='new'"
        )
        reports_n = int((await cur.fetchone())["n"])

        cur = await db.execute(
            "SELECT COALESCE(SUM(stars),0) AS n "
            "FROM payments WHERE refunded=0"
        )
        stars_n = int((await cur.fetchone())["n"])

        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM payments WHERE refunded=0"
        )
        payments_n = int((await cur.fetchone())["n"])

    await message.answer(
        "<b>📈 Статистика MarketPhoto AI</b>\n"
        f"Пользователей: <b>{users_n}</b>\n"
        f"Новых обращений: <b>{reports_n}</b>\n"
        f"Успешных платежей: <b>{payments_n}</b>\n"
        f"Получено Stars по не возвращённым платежам: "
        f"<b>{stars_n}</b> ⭐"
    )


@dp.message(Command("setcredits"))
async def cmd_setcredits(
    message: Message,
    command: CommandObject,
) -> None:
    if (
        not message.from_user
        or not is_admin(message.from_user.id)
    ):
        await message.answer(
            "Команда доступна только администратору."
        )
        return

    try:
        user_id_text, amount_text = (
            command.args or ""
        ).split(maxsplit=1)

        target_id = int(user_id_text)
        amount = int(amount_text)

        if amount < 0 or amount > 1_000_000:
            raise ValueError

    except ValueError:
        await message.answer(
            "Формат: /setcredits USER_ID AMOUNT"
        )
        return

    async with await get_db() as db:
        await db.execute(
            "UPDATE users SET credits=? WHERE user_id=?",
            (amount, target_id),
        )

        cur = await db.execute("SELECT changes() AS n")
        changed = int((await cur.fetchone())["n"])

        await db.commit()

    if changed:
        await message.answer(
            f"Баланс пользователя {target_id} теперь {amount}."
        )

        try:
            await bot.send_message(
                target_id,
                f"Администратор изменил твой баланс генераций: {amount}.",
            )
        except Exception:
            pass

    else:
        await message.answer(
            "Пользователь не найден. "
            "Он должен хотя бы раз нажать /start."
        )


@dp.message(Command("vip"))
async def cmd_vip(
    message: Message,
    command: CommandObject,
) -> None:
    if (
        not message.from_user
        or not is_admin(message.from_user.id)
    ):
        await message.answer(
            "Команда доступна только администратору."
        )
        return

    try:
        user_text, days_text = (
            command.args or ""
        ).split(maxsplit=1)

        target_id = int(user_text)
        days = int(days_text)

        if days < 1 or days > 3650:
            raise ValueError

    except ValueError:
        await message.answer("Формат: /vip USER_ID DAYS")
        return

    row = await user_row(target_id)

    if not row:
        await message.answer(
            "Пользователь не найден. "
            "Сначала он должен нажать /start."
        )
        return

    current = now_utc()

    if vip_active(row["vip_until"]):
        current = max(
            current,
            datetime.fromisoformat(row["vip_until"]),
        )

    expiry = (current + timedelta(days=days)).isoformat()

    async with await get_db() as db:
        await db.execute(
            "UPDATE users SET vip_until=? WHERE user_id=?",
            (expiry, target_id),
        )
        await db.commit()

    await message.answer(
        f"VIP выдан пользователю {target_id} на {days} дней."
    )

    try:
        await bot.send_message(
            target_id,
            f"👑 Администратор выдал тебе VIP на {days} дней!",
        )
    except Exception:
        pass


@dp.message(Command("refund"))
async def cmd_refund(
    message: Message,
    command: CommandObject,
) -> None:
    if (
        not message.from_user
        or not is_admin(message.from_user.id)
    ):
        await message.answer(
            "Команда доступна только администратору."
        )
        return

    charge_id = (command.args or "").strip()

    if not charge_id:
        await message.answer(
            "Формат: /refund CHARGE_ID\n"
            "Charge ID хранится в базе данных после успешной оплаты."
        )
        return

    async with await get_db() as db:
        cur = await db.execute(
            "SELECT * FROM payments WHERE charge_id=?",
            (charge_id,),
        )
        payment = await cur.fetchone()

    if not payment:
        await message.answer(
            "Платёж с таким charge ID не найден."
        )
        return

    if int(payment["refunded"]):
        await message.answer(
            "Этот платёж уже помечен как возвращённый."
        )
        return

    try:
        await bot.refund_star_payment(
            user_id=int(payment["user_id"]),
            telegram_payment_charge_id=charge_id,
        )

    except Exception:
        log.exception(
            "Telegram Stars refund failed charge_id=%s",
            charge_id,
        )
        await message.answer(
            "Telegram не подтвердил возврат. "
            "Проверь charge ID и Render Logs; "
            "платёж не помечен возвращённым."
        )
        return

    product = PRODUCTS.get(
        str(payment["product"]),
        {},
    )

    async with await get_db() as db:
        await db.execute(
            "UPDATE payments SET refunded=1 WHERE charge_id=?",
            (charge_id,),
        )

        if product.get("credits"):
            await db.execute(
                """UPDATE users
                   SET credits=MAX(0, credits-?)
                   WHERE user_id=?""",
                (
                    int(product["credits"]),
                    int(payment["user_id"]),
                ),
            )

        elif product.get("days"):
            await db.execute(
                "UPDATE users SET vip_until=? WHERE user_id=?",
                (now_iso(), int(payment["user_id"])),
            )

        await db.commit()

    await message.answer(
        "✅ Возврат Stars выполнен. "
        "Купленные бонусы по этому платежу деактивированы "
        "насколько это возможно."
    )

    try:
        await bot.send_message(
            int(payment["user_id"]),
            "Возврат платежа в Telegram Stars обработан администратором.",
        )
    except Exception:
        pass


@dp.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message,
    command: CommandObject,
) -> None:
    if (
        not message.from_user
        or not is_admin(message.from_user.id)
    ):
        await message.answer(
            "Команда доступна только администратору."
        )
        return

    text = (command.args or "").strip()

    if not text:
        await message.answer(
            "Формат: /broadcast текст рассылки"
        )
        return

    if len(text) > 3500:
        await message.answer(
            "Текст слишком длинный (максимум 3500 символов)."
        )
        return

    async with await get_db() as db:
        cur = await db.execute(
            "SELECT user_id FROM users"
        )
        rows = await cur.fetchall()

    sent = failed = 0

    await message.answer(
        f"Рассылка началась для {len(rows)} пользователей…"
    )

    for row in rows:
        try:
            await bot.send_message(
                int(row["user_id"]),
                text,
                reply_markup=MAIN_MENU,
            )
            sent += 1
            await asyncio.sleep(0.04)

        except Exception:
            failed += 1

    await message.answer(
        f"Рассылка завершена. "
        f"Доставлено: {sent}. Не доставлено: {failed}."
    )


@dp.message(Command("reply"))
async def cmd_reply(
    message: Message,
    command: CommandObject,
) -> None:
    if (
        not message.from_user
        or not is_admin(message.from_user.id)
    ):
        await message.answer(
            "Команда доступна только администратору."
        )
        return

    try:
        user_text, response = (
            command.args or ""
        ).split(maxsplit=1)

        target_id = int(user_text)

    except ValueError:
        await message.answer(
            "Формат: /reply USER_ID текст ответа"
        )
        return

    try:
        await bot.send_message(
            target_id,
            "💬 Ответ поддержки:\n\n" + html.escape(response),
        )
        await message.answer("Ответ отправлен.")

    except Exception:
        await message.answer(
            "Не удалось отправить ответ. Проверь ID пользователя."
        )


@dp.pre_checkout_query()
async def process_pre_checkout(
    query: PreCheckoutQuery,
) -> None:
    try:
        product_code = (
            query.invoice_payload or ""
        ).split(":", 1)[0]

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
                    "Товар или цена устарели. "
                    "Откройте магазин и создайте новый счёт."
                ),
            )

    except Exception:
        log.exception("pre_checkout validation failed")
        await query.answer(
            ok=False,
            error_message=(
                "Не удалось проверить заказ. Попробуйте снова."
            ),
        )


@dp.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    if (
        not message.from_user
        or not message.successful_payment
    ):
        return

    await ensure_user(message.from_user)

    payment = message.successful_payment

    product_code = (
        payment.invoice_payload or ""
    ).split(":", 1)[0]

    product = PRODUCTS.get(product_code)

    if (
        not product
        or payment.currency != "XTR"
        or int(payment.total_amount) != int(product["stars"])
    ):
        log.error(
            "Unknown/mismatched successful payment "
            "user=%s payload=%s amount=%s currency=%s",
            message.from_user.id,
            payment.invoice_payload,
            payment.total_amount,
            payment.currency,
        )
        await message.answer(
            "Платёж получен, но заказ не удалось автоматически "
            "распознать. Напишите /paysupport."
        )
        return

    charge_id = payment.telegram_payment_charge_id
    awarded = False

    async with await get_db() as db:
        await db.execute("BEGIN IMMEDIATE")

        cur = await db.execute(
            """INSERT OR IGNORE INTO payments(
                charge_id, user_id, stars, product, created_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (
                charge_id,
                message.from_user.id,
                int(payment.total_amount),
                product_code,
                now_iso(),
            ),
        )

        # Avoid awarding the same Telegram charge twice.
        awarded = cur.rowcount == 1

        if awarded and product.get("credits"):
            await db.execute(
                """UPDATE users SET credits=credits+?
                   WHERE user_id=?""",
                (
                    int(product["credits"]),
                    message.from_user.id,
                ),
            )

        elif awarded and product.get("days"):
            cur = await db.execute(
                "SELECT vip_until FROM users WHERE user_id=?",
                (message.from_user.id,),
            )
            row = await cur.fetchone()

            start = now_utc()

            if row and vip_active(row["vip_until"]):
                start = max(
                    start,
                    datetime.fromisoformat(row["vip_until"]),
                )

            expiry = (
                start + timedelta(days=int(product["days"]))
            ).isoformat()

            await db.execute(
                "UPDATE users SET vip_until=? WHERE user_id=?",
                (expiry, message.from_user.id),
            )

        await db.commit()

    if not awarded:
        await message.answer(
            "Этот платёж уже был обработан. "
            "Повторно бонусы не начисляются.",
            reply_markup=MAIN_MENU,
        )
        return

    await message.answer(
        f"✅ Оплата подтверждена! Получено: "
        f"<b>{html.escape(product['title'])}</b>.",
        reply_markup=MAIN_MENU,
    )

    await send_to_admins(
        "💳 <b>Новый платёж MarketPhoto AI</b>\n"
        f"Пользователь: <code>{message.from_user.id}</code> "
        f"(@{html.escape(message.from_user.username or '—')})\n"
        f"Товар: {html.escape(product['title'])}\n"
        f"Сумма: {payment.total_amount} ⭐\n"
        f"Charge ID: <code>{html.escape(charge_id)}</code>"
    )


@dp.callback_query(F.data == "cancel")
async def callback_cancel(
    callback: CallbackQuery,
) -> None:
    user_flows.pop(callback.from_user.id, None)

    await callback.answer("Отменено")

    if callback.message:
        await callback.message.answer(
            "Действие отменено.",
            reply_markup=MAIN_MENU,
        )


@dp.callback_query(F.data == "terms")
async def callback_terms(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "Используй /terms для просмотра условий "
            "и /paysupport для вопросов по оплате."
        )


@dp.callback_query(F.data == "again")
async def callback_again(
    callback: CallbackQuery,
) -> None:
    user_flows[callback.from_user.id] = {
        "mode": "generate"
    }

    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "Опиши новое изображение одним сообщением.",
            reply_markup=CANCEL_KB,
        )


@dp.callback_query(F.data == "report")
async def callback_report(
    callback: CallbackQuery,
) -> None:
    user_flows[callback.from_user.id] = {
        "mode": "report"
    }

    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "Опиши проблему с последним результатом. "
            "Обращение увидит администратор.",
            reply_markup=CANCEL_KB,
        )


@dp.callback_query(F.data.startswith("buy:"))
async def callback_buy(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

    code = (
        callback.data or ""
    ).split(":", 1)[1]

    product = PRODUCTS.get(code)

    if not product:
        if callback.message:
            await callback.message.answer(
                "Товар не найден. Открой магазин заново через /buy."
            )
        return

    if callback.message:
        await create_invoice(callback.message, code)


@dp.message(F.text == "🎨 Создать изображение")
async def menu_generate(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {
            "mode": "generate"
        }

    await message.answer(
        "Напиши, какое изображение нужно создать. "
        "Чем подробнее описание, тем лучше результат.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == "🪄 Редактировать фото")
async def menu_edit(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {
            "mode": "edit_wait_photo"
        }

    await message.answer(
        "Сначала отправь фото, которое нужно изменить. "
        "Я попрошу описание после загрузки.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == "👤 Аватарка")
async def menu_avatar(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {
            "mode": "avatar"
        }

    await message.answer(
        "Опиши аватарку: персонаж, стиль, цвета и настроение.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == "📱 Обои на телефон")
async def menu_wallpaper(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {
            "mode": "wallpaper"
        }

    await message.answer(
        "Опиши обои для телефона. "
        "Буду создавать вертикальное изображение.",
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
        await message.answer(
            await profile_text(message.from_user.id),
            reply_markup=MAIN_MENU,
        )


@dp.message(F.text == "🆘 Поддержка")
async def menu_support(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {
            "mode": "support"
        }

    await message.answer(
        "Напиши сообщение для поддержки. "
        "Оно будет передано администратору.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.photo | F.document)
async def handle_incoming_image(message: Message) -> None:
    if not message.from_user:
        return

    flow = user_flows.get(message.from_user.id, {})

    if flow.get("mode") != "edit_wait_photo":
        await message.answer(
            "Чтобы редактировать фото, нажми "
            "«🪄 Редактировать фото» в меню и отправь "
            "изображение после этого.",
            reply_markup=MAIN_MENU,
        )
        return

    try:
        raw = await download_telegram_image(message)

    except UserFacingError as exc:
        await message.answer(
            "❌ " + html.escape(str(exc)),
            reply_markup=MAIN_MENU,
        )
        return

    user_flows[message.from_user.id] = {
        "mode": "edit_wait_prompt",
        "image": raw,
    }

    await message.answer(
        "Фото получено ✅ Теперь напиши, что изменить "
        "(например: «сделай фон ночным городом, сохрани лицо»).",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text)
async def handle_text_flow(message: Message) -> None:
    if not message.from_user or not message.text:
        return

    user_id = message.from_user.id

    await ensure_user(message.from_user)

    flow = user_flows.get(user_id)

    if not flow:
        await message.answer(
            "Выбери действие в меню или введи /help.",
            reply_markup=MAIN_MENU,
        )
        return

    mode = flow.get("mode")
    text = message.text.strip()

    # Support requests and payment questions.
    if mode == "support":
        user_flows.pop(user_id, None)

        report_id = await save_report(
            user_id,
            message.from_user.username,
            "support",
            text,
        )

        await send_to_admins(
            "🆘 <b>Обращение в поддержку</b>\n"
            f"Номер: #{report_id}\n"
            f"Пользователь: <code>{user_id}</code> "
            f"(@{html.escape(message.from_user.username or '—')})\n\n"
            f"{html.escape(text[:3500])}\n\n"
            f"Ответ: <code>/reply {user_id} текст</code>"
        )

        await message.answer(
            "✅ Сообщение передано поддержке. "
            "Для ответа используй /paysupport или меню.",
            reply_markup=MAIN_MENU,
        )
        return

    # Reports about generated images.
    if mode == "report":
        user_flows.pop(user_id, None)

        report_id = await save_report(
            user_id,
            message.from_user.username,
            "generation_issue",
            text,
        )

        await send_to_admins(
            "⚠️ <b>Жалоба на результат генерации</b>\n"
            f"Номер: #{report_id}\n"
            f"Пользователь: <code>{user_id}</code> "
            f"(@{html.escape(message.from_user.username or '—')})\n\n"
            f"{html.escape(text[:3500])}"
        )

        await message.answer(
            "✅ Жалоба отправлена. Спасибо за сообщение!",
            reply_markup=MAIN_MENU,
        )
        return

    if mode == "edit_wait_photo":
        await message.answer(
            "Сначала отправь фото, затем напиши, что поменять.",
            reply_markup=CANCEL_KB,
        )
        return

    # Use the uploaded reference photo for editing.
    if mode == "edit_wait_prompt":
        image = flow.get("image")
        user_flows.pop(user_id, None)

        if not image:
            await message.answer(
                "Фото потерялось из памяти бота. "
                "Начни заново: «🪄 Редактировать фото».",
                reply_markup=MAIN_MENU,
            )
            return

        prompt = (
            "Edit the provided reference image according "
            "to this instruction. Preserve the main subject "
            "and identity unless explicitly asked to change "
            "them. Instruction: "
            + text
        )

        await begin_generation(
            message,
            prompt,
            "edit",
            reference=image,
        )
        return

    # Normal image, avatar and wallpaper generation.
    if mode in {"generate", "avatar", "wallpaper"}:
        user_flows.pop(user_id, None)

        await begin_generation(
            message,
            text,
            str(mode),
        )
        return

    await message.answer(
        "Не удалось определить действие. "
        "Выбери его в меню ещё раз.",
        reply_markup=MAIN_MENU,
    )

    user_flows.pop(user_id, None)


async def health_handler(
    _request: web.Request,
) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "service": "marketphoto-ai",
            "model": CF_MODEL,
        }
    )


async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT,
    )

    await site.start()

    log.info("Health server listening on port %s", PORT)

    return runner


async def main() -> None:
    global bot, http_client

    if not BOT_TOKEN:
        raise RuntimeError(
            "Missing BOT_TOKEN environment variable"
        )

    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        log.warning(
            "CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN "
            "is missing; bot will start but generation will not work"
        )

    if not ADMIN_IDS:
        log.warning(
            "ADMIN_IDS is empty; support notifications, "
            "moderation and admin commands will not work"
        )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    await init_db()

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            CF_TIMEOUT_SECONDS,
            connect=20.0,
        ),
        follow_redirects=False,
    )

    runner = await start_health_server()

    log.info(
        "Starting MarketPhoto AI. Model=%s",
        CF_MODEL,
    )

    try:
        assert bot is not None

        # Polling and webhooks cannot be active simultaneously.
        await bot.delete_webhook(
            drop_pending_updates=False
        )

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:
        await runner.cleanup()

        if http_client is not None:
            await http_client.aclose()

        if bot is not None:
            await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("MarketPhoto AI stopped")
