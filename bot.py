# BUILD_ID: marketphoto-httpx-sqlite-2026-10-20-v3
from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import logging
import os
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
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

# Cloudflare requests use httpx multipart.
# SQLite uses Python standard library.

BUILD_ID = "marketphoto-httpx-sqlite-2026-10-20-v3"

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
    "CLOUDFLARE_MODEL",
    "@cf/black-forest-labs/flux-2-klein-9b",
).strip()

ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

PORT = int(os.getenv("PORT", "10000"))
DB_PATH = Path(os.getenv("DB_PATH", "marketphoto.sqlite3"))

FREE_DAILY_LIMIT = max(
    0,
    int(os.getenv("FREE_DAILY_LIMIT", "5")),
)
VIP_DAILY_LIMIT = max(
    1,
    int(os.getenv("VIP_DAILY_LIMIT", "100")),
)
CF_TIMEOUT = max(
    30,
    int(os.getenv("CF_TIMEOUT_SECONDS", "180")),
)

MAX_PROMPT_LENGTH = 1800
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
KYIV_TZ = ZoneInfo("Europe/Kyiv")

PRODUCTS: dict[str, dict[str, Any]] = {
    "credits_25": {
        "title": "25 генераций",
        "description": "25 дополнительных генераций изображений",
        "stars": max(
            1,
            int(os.getenv("PRICE_CREDITS_25", "50")),
        ),
        "credits": 25,
        "days": 0,
    },
    "credits_100": {
        "title": "100 генераций",
        "description": "100 дополнительных генераций изображений",
        "stars": max(
            1,
            int(os.getenv("PRICE_CREDITS_100", "150")),
        ),
        "credits": 100,
        "days": 0,
    },
    "vip_30d": {
        "title": "VIP на 30 дней",
        "description": (
            f"VIP-доступ до {VIP_DAILY_LIMIT} генераций "
            "в день на 30 дней"
        ),
        "stars": max(
            1,
            int(os.getenv("PRICE_VIP_30D", "250")),
        ),
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
                text="ℹ️ Условия",
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

user_flows: dict[int, dict[str, Any]] = {}
in_progress: set[int] = set()


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
    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=20,
    )
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
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                credits INTEGER NOT NULL DEFAULT 0,
                vip_until TEXT
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                free_used INTEGER NOT NULL DEFAULT 0,
                vip_used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                stars INTEGER NOT NULL,
                product TEXT NOT NULL,
                created_at TEXT NOT NULL,
                vip_previous TEXT,
                vip_granted_until TEXT,
                refunded INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                kind TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new'
            )
            """
        )

        # Migrations for older database versions.
        columns = {
            r[1]
            for r in db.execute(
                "PRAGMA table_info(daily_usage)"
            ).fetchall()
        }

        if "free_used" not in columns:
            db.execute(
                """
                ALTER TABLE daily_usage
                ADD COLUMN free_used INTEGER NOT NULL DEFAULT 0
                """
            )

        if "vip_used" not in columns:
            db.execute(
                """
                ALTER TABLE daily_usage
                ADD COLUMN vip_used INTEGER NOT NULL DEFAULT 0
                """
            )

        payment_columns = {
            r[1]
            for r in db.execute(
                "PRAGMA table_info(payments)"
            ).fetchall()
        }

        if "vip_previous" not in payment_columns:
            db.execute(
                "ALTER TABLE payments ADD COLUMN vip_previous TEXT"
            )

        if "vip_granted_until" not in payment_columns:
            db.execute(
                "ALTER TABLE payments ADD COLUMN vip_granted_until TEXT"
            )

        if "refunded" not in payment_columns:
            db.execute(
                """
                ALTER TABLE payments
                ADD COLUMN refunded INTEGER NOT NULL DEFAULT 0
                """
            )

    log.info("Database initialized: %s", DB_PATH)


def ensure_user(user: Any) -> None:
    with database() as db:
        db.execute(
            """
            INSERT INTO users(
                user_id, username, first_name, created_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
            """,
            (
                user.id,
                user.username,
                user.first_name or "",
                now_iso(),
            ),
        )


def get_user(
    user_id: int,
) -> sqlite3.Row | None:
    with database() as db:
        return db.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()


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


def reserve_generation(user_id: int) -> tuple[str, str]:
    """
    Reserve one credit or daily quota atomically.

    Returns a reservation type and the applicable day.
    """
    today = day_key()

    with database() as db:
        db.execute("BEGIN IMMEDIATE")

        row = db.execute(
            """
            SELECT credits, vip_until
            FROM users
            WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()

        if row is None:
            db.rollback()
            raise UserFacingError(
                "Нажми /start, чтобы создать профиль."
            )

        active_vip = vip_active(row["vip_until"])

        usage = db.execute(
            """
            SELECT free_used, vip_used
            FROM daily_usage
            WHERE user_id=? AND day=?
            """,
            (user_id, today),
        ).fetchone()

        free_used = (
            int(usage["free_used"]) if usage else 0
        )
        vip_used = (
            int(usage["vip_used"]) if usage else 0
        )

        if active_vip and vip_used < VIP_DAILY_LIMIT:
            db.execute(
                """
                INSERT INTO daily_usage(
                    user_id, day, vip_used
                )
                VALUES (?, ?, 1)
                ON CONFLICT(user_id, day)
                DO UPDATE SET vip_used=vip_used+1
                """,
                (user_id, today),
            )
            return ("vip", today)

        # If VIP quota is exhausted, paid credits can still be used.
        if int(row["credits"] or 0) > 0:
            db.execute(
                """
                UPDATE users
                SET credits=credits-1
                WHERE user_id=?
                """,
                (user_id,),
            )
            return ("credit", today)

        if active_vip:
            db.rollback()
            raise UserFacingError(
                f"Лимит VIP на сегодня исчерпан "
                f"({VIP_DAILY_LIMIT}). Попробуй завтра "
                "или пополни баланс."
            )

        if free_used >= FREE_DAILY_LIMIT:
            db.rollback()
            raise UserFacingError(
                "Бесплатные генерации на сегодня закончились "
                f"({FREE_DAILY_LIMIT}/{FREE_DAILY_LIMIT}).\n"
                "Открой «⭐ VIP / Пополнение», чтобы продолжить."
            )

        db.execute(
            """
            INSERT INTO daily_usage(
                user_id, day, free_used
            )
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, day)
            DO UPDATE SET free_used=free_used+1
            """,
            (user_id, today),
        )

        return ("free", today)


def refund_reservation(
    user_id: int,
    reservation: tuple[str, str],
) -> None:
    kind, day = reservation

    with database() as db:
        if kind == "credit":
            db.execute(
                """
                UPDATE users
                SET credits=credits+1
                WHERE user_id=?
                """,
                (user_id,),
            )

        else:
            column = (
                "vip_used"
                if kind == "vip"
                else "free_used"
            )

            db.execute(
                f"""
                UPDATE daily_usage
                SET {column}=MAX(0, {column}-1)
                WHERE user_id=? AND day=?
                """,
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
            f"""
            SELECT {column} AS n
            FROM daily_usage
            WHERE user_id=? AND day=?
            """,
            (user_id, today),
        ).fetchone()

    used = int(usage["n"]) if usage else 0

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
            f"👑 VIP активен до <b>"
            f"{html.escape(shown_until)}</b> (Киев)\n"
            f"Лимит: {VIP_DAILY_LIMIT} генераций в сутки"
        )

    else:
        tier = (
            f"Бесплатный тариф: "
            f"{FREE_DAILY_LIMIT} генераций в сутки"
        )

    return (
        "<b>📊 Твой профиль MarketPhoto AI</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💎 Платные генерации: <b>{credits}</b>\n"
        f"🎨 Использовано сегодня: <b>{used}</b>\n"
        f"{tier}"
    )


def shop_text() -> str:
    return (
        "<b>⭐ VIP и пополнение</b>\n\n"
        f"🎨 25 генераций — "
        f"{PRODUCTS['credits_25']['stars']} ⭐\n"
        f"🚀 100 генераций — "
        f"{PRODUCTS['credits_100']['stars']} ⭐\n"
        f"👑 VIP на 30 дней — "
        f"{PRODUCTS['vip_30d']['stars']} ⭐\n\n"
        f"VIP: до {VIP_DAILY_LIMIT} генераций в сутки. "
        "Оплата — Telegram Stars."
    )


def resize_reference_image(raw: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(
                source
            ).convert("RGB")

            # Reference image dimensions must be under 512x512.
            image.thumbnail(
                (448, 448),
                Image.Resampling.LANCZOS,
            )

            buffer = io.BytesIO()
            image.save(
                buffer,
                format="PNG",
                optimize=True,
            )

            return buffer.getvalue()

    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise UserFacingError(
            "Не удалось прочитать фото. "
            "Отправь обычное изображение JPG или PNG."
        ) from exc


def safe_text(
    value: Any,
    max_len: int = 900,
) -> str:
    try:
        return str(value or "")[:max_len]
    except Exception:
        return "unknown error"


def extract_image_response(
    response: httpx.Response,
) -> bytes:
    content_type = response.headers.get(
        "content-type", ""
    ).lower()

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

        log.error(
            "Cloudflare HTTP %s: %s",
            response.status_code,
            detail,
        )

        if response.status_code in (401, 403):
            raise UserFacingError(
                "Cloudflare отклонил доступ. Проверь "
                "CLOUDFLARE_API_TOKEN и права Workers AI."
            )

        if response.status_code == 429:
            raise UserFacingError(
                "Достигнут лимит Cloudflare или сервис "
                "перегружен. Попробуй позже."
            )

        raise UserFacingError(
            "Cloudflare вернул ошибку. Проверь Render Logs "
            "и доступность модели."
        )

    if content_type.startswith("image/"):
        if response.content:
            return response.content

        raise UserFacingError(
            "Cloudflare вернул пустое изображение. "
            "Попробуй ещё раз."
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

    if (
        isinstance(payload, dict)
        and payload.get("success") is False
    ):
        log.error(
            "Cloudflare API error: %s",
            safe_text(
                payload.get("errors")
                or payload.get("messages")
            ),
        )

        raise UserFacingError(
            "Cloudflare не смог создать изображение. "
            "Проверь модель и лимит API."
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

    if isinstance(encoded, str):
        if encoded.startswith("data:") and "," in encoded:
            encoded = encoded.split(",", 1)[1]

        try:
            decoded = base64.b64decode(
                encoded,
                validate=False,
            )

            if decoded:
                return decoded

        except Exception as exc:
            raise UserFacingError(
                "Cloudflare вернул повреждённые данные изображения."
            ) from exc

    log.error(
        "Unexpected Cloudflare payload keys=%s",
        list(payload.keys())
        if isinstance(payload, dict)
        else type(payload),
    )

    raise UserFacingError(
        "Cloudflare не вернул файл изображения. "
        "Проверь название модели в Render."
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
            "HTTP-клиент не запущен. Перезапусти Render."
        )

    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"
    )

    # httpx creates valid multipart/form-data boundaries.
    parts: list[tuple[str, tuple[Any, ...]]] = [
        (
            "prompt",
            (None, prompt, "text/plain; charset=utf-8"),
        ),
        (
            "width",
            (None, str(width), "text/plain"),
        ),
        (
            "height",
            (None, str(height), "text/plain"),
        ),
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
            headers={
                "Authorization": f"Bearer {CF_API_TOKEN}"
            },
        )

    except httpx.TimeoutException as exc:
        raise UserFacingError(
            "Время генерации истекло. "
            "Попробуй ещё раз позже."
        ) from exc

    except httpx.RequestError as exc:
        log.exception("Network error calling Cloudflare")

        raise UserFacingError(
            "Не удалось подключиться к Cloudflare. "
            "Проверь Render и повтори попытку."
        ) from exc

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


async def download_telegram_image(
    message: Message,
) -> bytes:
    if bot is None:
        raise UserFacingError(
            "Бот ещё запускается. Попробуй через несколько секунд."
        )

    file_id: str | None = None

    if message.photo:
        file_id = message.photo[-1].file_id

    elif (
        message.document
        and (message.document.mime_type or "").startswith(
            "image/"
        )
    ):
        if (
            message.document.file_size
            and message.document.file_size > MAX_UPLOAD_BYTES
        ):
            raise UserFacingError(
                "Файл слишком большой. Максимум 10 МБ."
            )

        file_id = message.document.file_id

    if not file_id:
        raise UserFacingError(
            "Отправь фото JPG или PNG как фото или документ."
        )

    tg_file = await bot.get_file(file_id)
    buffer = io.BytesIO()

    await bot.download_file(
        tg_file.file_path,
        destination=buffer,
    )

    raw = buffer.getvalue()

    if not raw:
        raise UserFacingError(
            "Не удалось загрузить фото. Отправь его ещё раз."
        )

    if len(raw) > MAX_UPLOAD_BYTES:
        raise UserFacingError(
            "Файл слишком большой. Максимум 10 МБ."
        )

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
            """
            INSERT INTO reports(
                user_id, username, kind, body, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                kind,
                body[:4000],
                now_iso(),
            ),
        )

        return int(cursor.lastrowid)


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
            "Напиши, какое изображение нужно создать.",
            reply_markup=CANCEL_KB,
        )
        return

    if len(prompt) > MAX_PROMPT_LENGTH:
        await message.answer(
            f"Запрос слишком длинный. "
            f"Максимум {MAX_PROMPT_LENGTH} символов."
        )
        return

    final_prompt = prompt
    width, height = 1024, 1024

    if mode == "avatar":
        final_prompt = (
            "Create a high quality social media profile avatar, "
            "centered subject, clear silhouette, professional "
            "lighting, polished composition. User request: "
            + prompt
        )

    elif mode == "wallpaper":
        width, height = 768, 1365

        final_prompt = (
            "Create a vertical smartphone wallpaper, elegant "
            "composition, visually rich background, leave clean "
            "negative space near the top for phone clock and icons. "
            "User request: "
            + prompt
        )

    elif mode == "edit":
        final_prompt = (
            "Edit the reference image according to the instruction. "
            "Preserve the main subject and identity unless explicitly "
            "asked to change them. Instruction: "
            + prompt
        )

    in_progress.add(user_id)
    reservation: tuple[str, str] | None = None
    status_message: Message | None = None

    try:
        reservation = reserve_generation(user_id)

        status_message = await message.answer(
            "⏳ <b>Создаю изображение…</b>\n"
            "Подожди немного, не отправляй запрос повторно.",
            reply_markup=MAIN_MENU,
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

        await message.answer_photo(
            BufferedInputFile(
                image_bytes,
                filename="marketphoto.png",
            ),
            caption="✨ <b>Готово!</b> Создано в MarketPhoto AI.",
            reply_markup=RESULT_KB,
        )

        if status_message:
            try:
                await status_message.delete()
            except TelegramAPIError:
                pass

        log.info(
            "Generation success user_id=%s mode=%s model=%s",
            user_id,
            mode,
            CF_MODEL,
        )

    except UserFacingError as exc:
        if reservation:
            refund_reservation(user_id, reservation)

        answer = "❌ " + html.escape(str(exc))

        if status_message:
            try:
                await status_message.edit_text(
                    answer,
                    reply_markup=MAIN_MENU,
                )
            except TelegramAPIError:
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
        if reservation:
            refund_reservation(user_id, reservation)

        log.exception(
            "Unhandled generation error user_id=%s",
            user_id,
        )

        text = (
            "❌ Не удалось создать изображение. "
            "Лимит восстановлен. Проверь Render Logs "
            "и попробуй снова."
        )

        if status_message:
            try:
                await status_message.edit_text(
                    text,
                    reply_markup=MAIN_MENU,
                )
            except TelegramAPIError:
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
    if bot is None:
        return

    product = PRODUCTS.get(product_code)

    if not product:
        await message.answer(
            "Товар не найден. Открой магазин через /buy."
        )
        return

    payload = (
        f"{product_code}:{secrets.token_urlsafe(12)}"
    )

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
            "Не удалось открыть оплату. "
            "Проверь Render Logs и повтори позже."
        )


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if message.from_user:
        ensure_user(message.from_user)

    await message.answer(
        "<b>✨ Добро пожаловать в MarketPhoto AI!</b>\n\n"
        "Создавай изображения по описанию, делай аватарки "
        "и обои, редактируй фотографии.\n\n"
        f"🎁 Бесплатно: <b>{FREE_DAILY_LIMIT} генераций в сутки</b>\n"
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
        "/cancel — отменить действие",
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


@dp.message(Command("terms"))
async def cmd_terms(message: Message) -> None:
    await message.answer(
        "<b>Условия MarketPhoto AI</b>\n\n"
        f"• Бесплатно: {FREE_DAILY_LIMIT} генераций в сутки.\n"
        f"• VIP: до {VIP_DAILY_LIMIT} генераций в сутки, "
        "пока активна подписка.\n"
        "• Платные генерации начисляются после подтверждения "
        "оплаты Telegram Stars.\n"
        "• При технической ошибке генерации использованный "
        "лимит возвращается.\n"
        "• Не отправляй конфиденциальные данные. Результат "
        "зависит от модели и запроса.\n"
        "• Вопросы по оплате: /paysupport.",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message) -> None:
    if message.from_user:
        user_flows[message.from_user.id] = {
            "mode": "support"
        }

    await message.answer(
        "Опиши проблему с оплатой одним сообщением. "
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
        "/broadcast ТЕКСТ — рассылка\n"
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

    with database() as db:
        users_n = db.execute(
            "SELECT COUNT(*) n FROM users"
        ).fetchone()["n"]

        reports_n = db.execute(
            "SELECT COUNT(*) n FROM reports WHERE status='new'"
        ).fetchone()["n"]

        stars_n = db.execute(
            """
            SELECT COALESCE(SUM(stars),0) n
            FROM payments WHERE refunded=0
            """
        ).fetchone()["n"]

        payments_n = db.execute(
            "SELECT COUNT(*) n FROM payments WHERE refunded=0"
        ).fetchone()["n"]

    await message.answer(
        "<b>📈 Статистика</b>\n"
        f"Пользователей: <b>{users_n}</b>\n"
        f"Новых обращений: <b>{reports_n}</b>\n"
        f"Не возвращённых платежей: <b>{payments_n}</b>\n"
        f"Получено Stars: <b>{stars_n}</b> ⭐"
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
        user_id_str, amount_str = (
            command.args or ""
        ).split(maxsplit=1)

        target_id = int(user_id_str)
        amount = int(amount_str)

        if amount < 0 or amount > 1_000_000:
            raise ValueError

    except ValueError:
        await message.answer(
            "Формат: /setcredits USER_ID AMOUNT"
        )
        return

    with database() as db:
        cur = db.execute(
            "UPDATE users SET credits=? WHERE user_id=?",
            (amount, target_id),
        )
        changed = cur.rowcount > 0

    if not changed:
        await message.answer(
            "Пользователь не найден. "
            "Сначала он должен нажать /start."
        )
        return

    await message.answer(
        f"Баланс пользователя {target_id}: "
        f"{amount} генераций."
    )

    if bot:
        try:
            await bot.send_message(
                target_id,
                f"Администратор изменил твой баланс "
                f"генераций: {amount}.",
            )
        except TelegramAPIError:
            pass


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
        user_str, days_str = (
            command.args or ""
        ).split(maxsplit=1)

        target_id = int(user_str)
        days = int(days_str)

        if not 1 <= days <= 3650:
            raise ValueError

    except ValueError:
        await message.answer(
            "Формат: /vip USER_ID DAYS"
        )
        return

    row = get_user(target_id)

    if not row:
        await message.answer(
            "Пользователь не найден. "
            "Сначала он должен нажать /start."
        )
        return

    expiry_base = now_utc()

    if vip_active(row["vip_until"]):
        expiry_base = max(
            expiry_base,
            datetime.fromisoformat(row["vip_until"]),
        )

    expiry = (
        expiry_base + timedelta(days=days)
    ).isoformat()

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
            "Формат: /refund CHARGE_ID"
        )
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
        await message.answer(
            "Этот платёж уже отмечен как возвращённый."
        )
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
            "Telegram не подтвердил возврат. "
            "Платёж не изменён в базе."
        )
        return

    product = PRODUCTS.get(
        str(payment["product"]),
        {},
    )

    with database() as db:
        db.execute(
            "UPDATE payments SET refunded=1 WHERE charge_id=?",
            (charge_id,),
        )

        if int(product.get("credits", 0)) > 0:
            db.execute(
                """
                UPDATE users
                SET credits=MAX(0, credits-?)
                WHERE user_id=?
                """,
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

            # Do not overwrite a newer VIP purchase/admin extension.
            if (
                current
                and current["vip_until"]
                == payment["vip_granted_until"]
            ):
                db.execute(
                    "UPDATE users SET vip_until=? WHERE user_id=?",
                    (
                        payment["vip_previous"],
                        int(payment["user_id"]),
                    ),
                )

    await message.answer(
        "✅ Возврат Telegram Stars выполнен. "
        "Доступные бонусы от платежа отозваны, если это было безопасно."
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
    if (
        not message.from_user
        or not is_admin(message.from_user.id)
    ):
        await message.answer(
            "Команда доступна только администратору."
        )
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

    sent = 0
    failed = 0

    for row in rows:
        try:
            await message.bot.send_message(
                int(row["user_id"]),
                text,
                reply_markup=MAIN_MENU,
            )
            sent += 1
            await asyncio.sleep(0.05)

        except TelegramAPIError:
            failed += 1

    await message.answer(
        f"Рассылка завершена. Доставлено: {sent}; "
        f"не доставлено: {failed}."
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
        user_str, response_text = (
            command.args or ""
        ).split(maxsplit=1)

        target_id = int(user_str)

    except ValueError:
        await message.answer(
            "Формат: /reply USER_ID текст ответа"
        )
        return

    try:
        await message.bot.send_message(
            target_id,
            "💬 Ответ поддержки:\n\n"
            + html.escape(response_text),
        )
        await message.answer("Ответ отправлен.")

    except TelegramAPIError:
        await message.answer(
            "Не удалось отправить ответ. "
            "Проверь ID пользователя и доступность бота."
        )


@dp.pre_checkout_query()
async def pre_checkout(
    query: PreCheckoutQuery,
) -> None:
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
                "Цена или товар изменились. "
                "Открой магазин и создай новый счёт."
            ),
        )


@dp.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    user = message.from_user
    payment = message.successful_payment

    if user is None or payment is None:
        return

    ensure_user(user)

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
            "Unmatched successful payment user=%s payload=%s "
            "amount=%s currency=%s",
            user.id,
            payment.invoice_payload,
            payment.total_amount,
            payment.currency,
        )

        await message.answer(
            "Платёж получен, но заказ не удалось распознать "
            "автоматически. Напиши /paysupport."
        )
        return

    charge_id = payment.telegram_payment_charge_id
    awarded = False
    vip_previous: str | None = None
    vip_granted_until: str | None = None

    with database() as db:
        db.execute("BEGIN IMMEDIATE")

        cursor = db.execute(
            """
            INSERT OR IGNORE INTO payments(
                charge_id, user_id, stars, product, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
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
                """
                UPDATE users
                SET credits=credits+?
                WHERE user_id=?
                """,
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
                """
                UPDATE payments
                SET vip_previous=?, vip_granted_until=?
                WHERE charge_id=?
                """,
                (
                    vip_previous,
                    vip_granted_until,
                    charge_id,
                ),
            )

    if not awarded:
        await message.answer(
            "Этот платёж уже был обработан. "
            "Повторно бонусы не начисляются.",
            reply_markup=MAIN_MENU,
        )
        return

    await message.answer(
        "✅ Оплата подтверждена! Получено: "
        f"<b>{html.escape(product['title'])}</b>.",
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
            "Для просмотра условий отправь /terms; "
            "по оплате — /paysupport."
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
                "Товар не найден. Открой магазин через /buy."
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
        "Потом я попрошу описание.",
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
        "Создам вертикальное изображение.",
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

    flow = user_flows.get(
        message.from_user.id,
        {},
    )

    if flow.get("mode") != "edit_wait_photo":
        await message.answer(
            "Чтобы редактировать фото, нажми "
            "«🪄 Редактировать фото» и отправь фото после этого.",
            reply_markup=MAIN_MENU,
        )
        return

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
    }

    await message.answer(
        "Фото получено ✅ Теперь напиши, что изменить.",
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

    if mode in {"support", "report"}:
        kind = (
            "support"
            if mode == "support"
            else "generation_issue"
        )

        user_flows.pop(user_id, None)

        report_id = save_report(
            user_id,
            user.username,
            kind,
            text,
        )

        title = (
            "Обращение в поддержку"
            if kind == "support"
            else "Жалоба на результат генерации"
        )

        await send_to_admins(
            f"🆘 <b>{title}</b>\n"
            f"Номер: #{report_id}\n"
            f"Пользователь: <code>{user_id}</code> "
            f"(@{html.escape(user.username or '—')})\n\n"
            f"{html.escape(text[:3500])}\n\n"
            f"Ответ: <code>/reply {user_id} текст</code>"
        )

        await message.answer(
            "✅ Сообщение передано. Спасибо!",
            reply_markup=MAIN_MENU,
        )
        return

    if mode == "edit_wait_photo":
        await message.answer(
            "Сначала отправь фото, затем напиши, что изменить.",
            reply_markup=CANCEL_KB,
        )
        return

    if mode == "edit_wait_prompt":
        reference = flow.get("image")
        user_flows.pop(user_id, None)

        if not reference:
            await message.answer(
                "Фото потерялось после перезапуска. "
                "Начни заново через «🪄 Редактировать фото».",
                reply_markup=MAIN_MENU,
            )
            return

        await begin_generation(
            message,
            text,
            "edit",
            reference=reference,
        )
        return

    if mode in {"generate", "avatar", "wallpaper"}:
        user_flows.pop(user_id, None)

        await begin_generation(
            message,
            text,
            str(mode),
        )
        return

    user_flows.pop(user_id, None)

    await message.answer(
        "Действие сброшено. Выбери его в меню ещё раз.",
        reply_markup=MAIN_MENU,
    )


async def health_server() -> asyncio.AbstractServer:
    """Small dependency-free HTTP health endpoint for Render."""

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
                request_line.decode(
                    "latin-1",
                    errors="ignore",
                ).split(" ")[1]
                if request_line
                else "/"
            )

            body_obj = {
                "ok": True,
                "service": "marketphoto-ai",
                "build": BUILD_ID,
                "model": CF_MODEL,
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
    global bot, http_client

    log.info("BUILD_ID=%s", BUILD_ID)
    log.info("RUNNING_FILE=%s", Path(__file__).resolve())
    log.info("IMAGE_TRANSPORT=httpx-multipart")

    if not BOT_TOKEN:
        raise RuntimeError(
            "Missing BOT_TOKEN environment variable"
        )

    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        log.warning(
            "Cloudflare variables missing; bot starts, "
            "but generation will fail until configured"
        )

    if not ADMIN_IDS:
        log.warning(
            "ADMIN_IDS is empty; admin notifications "
            "and admin commands are disabled"
        )

    init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
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

        log.info(
            "Health server listening on port %s",
            PORT,
        )
        log.info(
            "Starting MarketPhoto AI. Model=%s",
            CF_MODEL,
        )

        # Clear any webhook configured for this bot.
        # This does not terminate another polling process.
        await bot.delete_webhook(
            drop_pending_updates=False
        )

        try:
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
            )

        except TelegramConflictError:
            log.critical(
                "TelegramConflictError: another process is polling "
                "this same bot token. Stop every other local/Render "
                "instance and leave exactly one poller running."
            )
            raise

    finally:
        if server is not None:
            server.close()
            await server.wait_closed()

        if http_client is not None:
            await http_client.aclose()

        if bot is not None:
            await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except (KeyboardInterrupt, SystemExit):
        log.info("MarketPhoto AI stopped")
