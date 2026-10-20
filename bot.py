import asyncio
import base64
import binascii
import io
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from PIL import Image, UnidentifiedImageError
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiohttp import web

# =========================
# Налаштування Render
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN", "")
CF_ACCOUNT_ID = (
    os.getenv("CLOUDFLARE_ACCOUNT_ID")
    or os.getenv("CF_ACCOUNT_ID", "")
)
CF_API_TOKEN = (
    os.getenv("CLOUDFLARE_API_TOKEN")
    or os.getenv("CF_API_TOKEN", "")
)

ADMIN_ID_RAW = os.getenv("ADMIN_ID", "5694967765")
DB_PATH = os.getenv("DB_PATH", "marketphoto.db")
PORT = int(os.getenv("PORT", "10000"))

MODEL_NAME = "@cf/black-forest-labs/flux-2-klein-4b"

DAILY_LIMIT = 5
STAR_PRICE = 25
PURCHASE_CREDITS = 10
CLOUDFLARE_TIMEOUT = 180

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("marketphoto")

if not BOT_TOKEN:
    raise RuntimeError(
        "Missing TELEGRAM_TOKEN (or BOT_TOKEN) environment variable"
    )

if not CF_ACCOUNT_ID or not CF_API_TOKEN:
    log.warning(
        "Cloudflare credentials are missing. "
        "Image generation will not work until they are set in Render."
    )

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    ADMIN_ID = 0


# =========================
# База даних
# =========================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def init_db():
    parent = Path(DB_PATH).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)

    with db_connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                daily_date TEXT NOT NULL DEFAULT '',
                daily_count INTEGER NOT NULL DEFAULT 0,
                credits INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                success INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'text'
            )
        """)

        con.commit()


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def touch_user(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
):
    with db_connect() as con:
        con.execute("""
            INSERT INTO users(user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (user_id, username or "", first_name or ""))
        con.commit()


def get_user_stats(user_id: int):
    with db_connect() as con:
        row = con.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()

        if row is None:
            return {"daily_count": 0, "credits": 0}

        used = (
            row["daily_count"]
            if row["daily_date"] == today_utc()
            else 0
        )

        return {
            "daily_count": used,
            "credits": row["credits"],
        }


def can_generate(user_id: int) -> bool:
    stats = get_user_stats(user_id)
    return (
        stats["daily_count"] < DAILY_LIMIT
        or stats["credits"] > 0
    )


def charge_successful_generation(user_id: int, mode: str):
    """
    Списує генерацію лише після того, як фото успішно надіслане.
    """
    day = today_utc()

    with db_connect() as con:
        row = con.execute(
            "SELECT daily_date, daily_count, credits "
            "FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()

        if row is None:
            con.execute("""
                INSERT INTO users(
                    user_id, daily_date, daily_count
                ) VALUES (?, ?, 1)
            """, (user_id, day))
        else:
            used = (
                row["daily_count"]
                if row["daily_date"] == day
                else 0
            )

            if used < DAILY_LIMIT:
                con.execute("""
                    UPDATE users
                    SET daily_date=?, daily_count=?
                    WHERE user_id=?
                """, (day, used + 1, user_id))
            elif row["credits"] > 0:
                con.execute("""
                    UPDATE users
                    SET credits=credits-1
                    WHERE user_id=?
                """, (user_id,))

        con.execute("""
            INSERT INTO generations(user_id, success, mode)
            VALUES (?, 1, ?)
        """, (user_id, mode))

        con.commit()


def log_failed_generation(user_id: int, mode: str):
    with db_connect() as con:
        con.execute("""
            INSERT INTO generations(user_id, success, mode)
            VALUES (?, 0, ?)
        """, (user_id, mode))
        con.commit()


def add_credits(user_id: int, amount: int):
    with db_connect() as con:
        cur = con.execute("""
            UPDATE users SET credits=credits+?
            WHERE user_id=?
        """, (amount, user_id))

        if cur.rowcount == 0:
            con.execute("""
                INSERT OR IGNORE INTO users(user_id, credits)
                VALUES (?, ?)
            """, (user_id, amount))

        con.commit()


def admin_stats():
    with db_connect() as con:
        users = con.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        successes = con.execute(
            "SELECT COUNT(*) FROM generations WHERE success=1"
        ).fetchone()[0]

        failures = con.execute(
            "SELECT COUNT(*) FROM generations WHERE success=0"
        ).fetchone()[0]

        return users, successes, failures


# =========================
# Меню та стани
# =========================

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🖼 Створити фото"),
            KeyboardButton(text="✏️ Редагувати фото"),
        ],
        [
            KeyboardButton(text="👤 Профіль"),
            KeyboardButton(text="🛍 Послуги"),
        ],
        [
            KeyboardButton(text="ℹ️ Допомога"),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Оберіть дію або введіть запит…",
)

BUY_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text=(
                    f"⭐ Купити {PURCHASE_CREDITS} генерацій "
                    f"за {STAR_PRICE} зірок"
                ),
                callback_data="buy_credits",
            )
        ]
    ]
)


class Flow(StatesGroup):
    waiting_prompt = State()
    waiting_photo = State()
    waiting_edit_prompt = State()


busy_users: set[int] = set()

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher(storage=MemoryStorage())


async def ensure_user(message: Message):
    if message.from_user:
        touch_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
        )


async def show_home(
    message: Message,
    state: FSMContext,
    intro: bool = False,
):
    await state.clear()
    await ensure_user(message)

    if intro:
        text = (
            "<b>✨ MarketPhoto AI</b>\n\n"
            "Створюй зображення за описом або завантаж фото "
            "й напиши, що потрібно змінити.\n\n"
            f"🎁 Щодня доступно до <b>{DAILY_LIMIT} "
            "успішних безкоштовних генерацій</b>."
        )
    else:
        text = "Оберіть, що потрібно зробити 👇"

    await message.answer(text, reply_markup=MAIN_KB)


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await show_home(message, state, intro=True)


@dp.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "<b>ℹ️ Як користуватись MarketPhoto AI</b>\n\n"
        "🖼 <b>Створити фото</b>\n"
        "Натисни кнопку, потім надішли опис картинки "
        "одним звичайним текстовим повідомленням.\n\n"
        "✏️ <b>Редагувати фото</b>\n"
        "Натисни кнопку, надішли фото, а потім окремо "
        "опиши, що потрібно змінити.\n\n"
        "👤 <b>Профіль</b> — залишок генерацій.\n"
        "🛍 <b>Послуги</b> — інформація про ліміт і покупки.\n\n"
        "Команди: /start, /help, /imagine, /profile, "
        "/services, /cancel, /id.\n\n"
        "Для скасування поточної дії введи /cancel.",
        reply_markup=MAIN_KB,
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✅ Дію скасовано. Обери наступну дію 👇",
        reply_markup=MAIN_KB,
    )


@dp.message(Command("id"))
async def cmd_id(message: Message):
    if message.from_user:
        await message.answer(
            f"Твій Telegram ID: <code>{message.from_user.id}</code>"
        )


@dp.message(Command("profile"))
@dp.message(F.text == "👤 Профіль")
async def cmd_profile(message: Message, state: FSMContext):
    await state.clear()
    await ensure_user(message)

    stats = get_user_stats(message.from_user.id)
    left = max(0, DAILY_LIMIT - stats["daily_count"])

    await message.answer(
        "<b>👤 Твій профіль</b>\n\n"
        f"ID: <code>{message.from_user.id}</code>\n"
        f"Безкоштовні генерації сьогодні: "
        f"<b>{stats['daily_count']}/{DAILY_LIMIT}</b>\n"
        f"Залишилось безкоштовних: <b>{left}</b>\n"
        f"Додаткові генерації: <b>{stats['credits']}</b>",
        reply_markup=MAIN_KB,
    )


@dp.message(Command("services"))
@dp.message(F.text == "🛍 Послуги")
async def cmd_services(message: Message, state: FSMContext):
    await state.clear()
    await ensure_user(message)

    await message.answer(
        "<b>🛍 Послуги</b>\n\n"
        f"• До {DAILY_LIMIT} успішних безкоштовних генерацій щодня.\n"
        f"• Додатково: {PURCHASE_CREDITS} генерацій "
        f"за {STAR_PRICE} Telegram Stars.\n\n"
        "Покупка добровільна.",
        reply_markup=BUY_KB,
    )


@dp.message(F.text == "ℹ️ Допомога")
async def menu_help(message: Message, state: FSMContext):
    await cmd_help(message, state)


@dp.message(F.text == "🖼 Створити фото")
async def begin_create(message: Message, state: FSMContext):
    await state.clear()
    await ensure_user(message)

    if not can_generate(message.from_user.id):
        await message.answer(
            "🎁 Безкоштовні генерації на сьогодні закінчилися. "
            "Перейди в «🛍 Послуги» для інформації про додаткові генерації.",
            reply_markup=MAIN_KB,
        )
        return

    await state.set_state(Flow.waiting_prompt)
    await message.answer(
        "📝 <b>Напиши запит для зображення.</b>\n\n"
        "Наприклад: професійне фото чорних кросівок "
        "на темному студійному фоні, м'яке бокове світло, "
        "реалістична фотографія, без тексту.\n\n"
        "Надішли запит звичайним повідомленням. "
        "Для скасування — /cancel.",
        reply_markup=MAIN_KB,
    )


@dp.message(F.text == "✏️ Редагувати фото")
async def begin_edit(message: Message, state: FSMContext):
    await state.clear()
    await ensure_user(message)

    if not can_generate(message.from_user.id):
        await message.answer(
            "🎁 Безкоштовні генерації на сьогодні закінчилися. "
            "Перейди в «🛍 Послуги» для інформації про додаткові генерації.",
            reply_markup=MAIN_KB,
        )
        return

    await state.set_state(Flow.waiting_photo)
    await message.answer(
        "📷 <b>Надішли фото, яке потрібно відредагувати.</b>\n\n"
        "Після отримання фото я попрошу тебе описати зміни.\n"
        "Для скасування — /cancel.",
        reply_markup=MAIN_KB,
    )


# =========================
# Telegram Stars
# =========================

@dp.callback_query(F.data == "buy_credits")
async def buy_credits(callback: CallbackQuery):
    user = callback.from_user
    touch_user(user.id, user.username, user.first_name)

    try:
        await bot.send_invoice(
            chat_id=user.id,
            title=f"{PURCHASE_CREDITS} генерацій MarketPhoto AI",
            description=(
                f"Додайте {PURCHASE_CREDITS} додаткових генерацій."
            ),
            payload=f"credits:{PURCHASE_CREDITS}:{user.id}",
            currency="XTR",
            prices=[
                LabeledPrice(
                    label=f"{PURCHASE_CREDITS} генерацій",
                    amount=STAR_PRICE,
                )
            ],
            provider_token="",
        )
        await callback.answer("Рахунок надіслано в приватний чат.")
    except Exception:
        log.exception("Could not create Telegram Stars invoice")
        await callback.answer(
            "Не вдалося створити рахунок. Спробуй пізніше.",
            show_alert=True,
        )


@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    payload = pre_checkout_query.invoice_payload.split(":")

    ok = (
        len(payload) == 3
        and payload[0] == "credits"
        and payload[2].isdigit()
        and int(payload[2]) == pre_checkout_query.from_user.id
        and pre_checkout_query.currency == "XTR"
        and pre_checkout_query.total_amount == STAR_PRICE
        and payload[1].isdigit()
        and int(payload[1]) == PURCHASE_CREDITS
    )

    await pre_checkout_query.answer(
        ok=ok,
        error_message=(
            None if ok
            else "Рахунок не вдалося підтвердити. "
                 "Відкрий послуги в боті ще раз."
        ),
    )


@dp.message(F.successful_payment)
async def payment_success(message: Message):
    payment = message.successful_payment
    parts = payment.invoice_payload.split(":")

    if len(parts) != 3 or parts[0] != "credits":
        await message.answer(
            "Оплату отримано, але дані рахунку не розпізнано. "
            "Напиши адміністратору."
        )
        return

    amount = int(parts[1])

    if (
        int(parts[2]) != message.from_user.id
        or payment.currency != "XTR"
        or amount != PURCHASE_CREDITS
    ):
        await message.answer(
            "Не вдалося підтвердити цей платіж для твого профілю."
        )
        return

    touch_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )

    add_credits(message.from_user.id, amount)

    await message.answer(
        f"✅ Оплату підтверджено! Додано <b>{amount}</b> генерацій.",
        reply_markup=MAIN_KB,
    )


# =========================
# Адміністратор
# =========================

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Немає доступу.")
        return

    users, successes, failures = admin_stats()

    await message.answer(
        "<b>🛠 Панель адміністратора</b>\n\n"
        f"Користувачів: <b>{users}</b>\n"
        f"Успішних генерацій: <b>{successes}</b>\n"
        f"Невдалих запитів: <b>{failures}</b>\n\n"
        "Детальні причини помилок доступні в Render → Logs. "
        "Не надсилай токени в чат.",
    )


# =========================
# Cloudflare AI
# =========================

class ImageServiceError(Exception):
    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.detail = detail


def safe_cf_error(status: int, raw: bytes) -> ImageServiceError:
    detail = ""

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
        pieces = []

        if isinstance(data, dict):
            for item in data.get("errors", []):
                if isinstance(item, dict):
                    pieces.append(
                        str(item.get("message") or item.get("code") or item)
                    )
                else:
                    pieces.append(str(item))

            if not pieces:
                pieces.append(str(data.get("message", "")))

        detail = "; ".join(p for p in pieces if p)[:1000]

    except Exception:
        detail = raw.decode("utf-8", errors="replace")[:500]

    low = detail.lower()

    if status in (401, 403):
        if "5035" in low or "paid plan" in low:
            text = (
                "Cloudflare повідомляє, що для цієї моделі потрібен "
                "платний план або доступ не ввімкнений. "
                "Перевір доступність моделі й дозволи токена."
            )
        else:
            text = (
                "Cloudflare відхилив авторизацію. Перевір "
                "CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID "
                "і дозволи токена."
            )
    elif status == 429:
        text = (
            "Cloudflare обмежив запити. Можливо, ліміт вичерпано "
            "або сервіс тимчасово перевантажений. Спробуй пізніше."
        )
    elif status == 400:
        text = (
            "Cloudflare не прийняв формат запиту. "
            "Дивись докладнішу помилку в Render → Logs."
        )
    elif status >= 500:
        text = (
            "Сервіс Cloudflare тимчасово недоступний. "
            "Спробуй повторити запит пізніше."
        )
    else:
        text = f"Cloudflare повернув помилку HTTP {status}."

    return ImageServiceError(text, f"HTTP {status}: {detail}")


def decode_image_response(raw: bytes, content_type: str) -> bytes:
    # Відповідь може бути готовим зображенням.
    if (
        raw.startswith(b"\x89PNG\r\n\x1a\n")
        or raw.startswith(b"\xff\xd8\xff")
        or raw.startswith(b"RIFF")
    ):
        return raw

    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ImageServiceError(
            "Cloudflare відповів у невідомому форматі. Спробуй ще раз.",
            f"Unexpected content-type={content_type}, bytes={len(raw)}",
        )

    if isinstance(payload, dict) and payload.get("success") is False:
        errors = payload.get("errors") or []
        detail = "; ".join(
            str(e.get("message", e)) if isinstance(e, dict) else str(e)
            for e in errors
        )[:1000]

        raise ImageServiceError(
            "Cloudflare не зміг створити зображення. Перевір Render → Logs.",
            detail,
        )

    result = payload.get("result", payload) if isinstance(payload, dict) else payload

    if isinstance(result, dict):
        for key in ("image", "images", "output", "data"):
            if key in result:
                result = result[key]
                break

    if isinstance(result, list) and result:
        result = result[0]

    if isinstance(result, dict):
        result = (
            result.get("image")
            or result.get("b64_json")
            or result.get("data")
        )

    if not isinstance(result, str) or not result.strip():
        keys = list(payload.keys()) if isinstance(payload, dict) else []
        raise ImageServiceError(
            "Cloudflare не повернув зображення. Перевір Render → Logs.",
            f"Unexpected JSON result; keys={keys}",
        )

    encoded = result.strip()

    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]

    try:
        image_bytes = base64.b64decode(encoded, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ImageServiceError(
            "Не вдалося прочитати зображення від Cloudflare.",
            str(exc),
        )

    if not (
        image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        or image_bytes.startswith(b"\xff\xd8\xff")
        or image_bytes.startswith(b"RIFF")
    ):
        raise ImageServiceError(
            "Cloudflare повернув дані, які не схожі на зображення.",
            f"Decoded response isn't image; bytes={len(image_bytes)}",
        )

    return image_bytes


async def cloudflare_generate(
    prompt: str,
    input_image: Optional[bytes] = None,
) -> bytes:
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        raise ImageServiceError(
            "Не налаштовано Cloudflare. Додай CLOUDFLARE_ACCOUNT_ID "
            "і CLOUDFLARE_API_TOKEN у Render → Environment."
        )

    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/ai/run/{MODEL_NAME}"
    )

    form = aiohttp.FormData()
    form.add_field("prompt", prompt)
    form.add_field("width", "1024")
    form.add_field("height", "1024")
    form.add_field("guidance", "4")

    if input_image:
        form.add_field(
            "input_image_0",
            input_image,
            filename="reference.png",
            content_type="image/png",
        )

    timeout = aiohttp.ClientTimeout(
        total=CLOUDFLARE_TIMEOUT,
        connect=20,
        sock_read=CLOUDFLARE_TIMEOUT,
    )

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Accept": "application/json, image/png, image/*",
    }

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(
                url,
                headers=headers,
                data=form,
            ) as response:
                raw = await response.read()
                content_type = response.headers.get("Content-Type", "")

                if not 200 <= response.status < 300:
                    raise safe_cf_error(response.status, raw)

                return decode_image_response(raw, content_type)

        except asyncio.TimeoutError as exc:
            raise ImageServiceError(
                "Генерація перевищила час очікування. Спробуй ще раз.",
                "Cloudflare request timed out",
            ) from exc

        except aiohttp.ClientError as exc:
            raise ImageServiceError(
                "Не вдалося з'єднатися з Cloudflare. "
                "Перевір сервіс і спробуй знову.",
                repr(exc),
            ) from exc


async def make_small_png(download: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(download)) as im:
            im = im.convert("RGB")
            im.thumbnail((511, 511), Image.Resampling.LANCZOS)

            out = io.BytesIO()
            im.save(out, format="PNG", optimize=True)
            return out.getvalue()

    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageServiceError(
            "Не вдалося прочитати це фото. Надішли звичайне JPG або PNG.",
            repr(exc),
        ) from exc


# =========================
# Анімація генерації
# =========================

async def animate_status(status: Message, stop_event: asyncio.Event):
    stages = [
        "🎨 <b>Створюю зображення…</b>\n⏳ Аналізую твій запит",
        "🧩 <b>Створюю зображення…</b>\n⏳ Формую композицію",
        "🖌️ <b>Створюю зображення…</b>\n⏳ Промальовую деталі",
        "✨ <b>Створюю зображення…</b>\n⏳ Завершую обробку",
    ]

    idx = 0

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3.0)
            break
        except asyncio.TimeoutError:
            try:
                await status.edit_text(stages[idx % len(stages)])
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    log.debug("Progress edit skipped: %s", exc)
            except TelegramAPIError as exc:
                log.debug("Progress edit skipped: %s", exc)

            idx += 1


# =========================
# Створення та надсилання фото
# =========================

async def do_generation(
    message: Message,
    state: FSMContext,
    prompt: str,
    input_image: Optional[bytes] = None,
):
    user = message.from_user

    if not user:
        return

    touch_user(user.id, user.username, user.first_name)

    prompt = prompt.strip()

    if not prompt:
        await message.answer("Запит порожній. Напиши, що потрібно створити.")
        return

    if len(prompt) > 2500:
        await message.answer(
            "Запит занадто довгий. Скороти його до 2500 символів."
        )
        return

    if user.id in busy_users:
        await message.answer(
            "⏳ Попередній запит ще обробляється. Дочекайся результату."
        )
        return

    if not can_generate(user.id):
        await state.clear()
        await message.answer(
            "🎁 Безкоштовні генерації на сьогодні закінчилися. "
            "Перейди в «🛍 Послуги».",
            reply_markup=MAIN_KB,
        )
        return

    busy_users.add(user.id)
    mode = "edit" if input_image else "text"

    status = await message.answer(
        "🎨 <b>Створюю зображення…</b>\n⏳ Аналізую твій запит"
    )

    stop_event = asyncio.Event()
    animation_task = asyncio.create_task(
        animate_status(status, stop_event)
    )

    try:
        if input_image:
            full_prompt = (
                "Edit the provided reference image according to these "
                "instructions. Keep the main subject recognizable unless "
                "the user asks to change it. Produce a coherent, "
                "high-quality image. User instructions: " + prompt
            )
        else:
            full_prompt = prompt

        image_bytes = await cloudflare_generate(
            full_prompt,
            input_image=input_image,
        )

        # Перевіряємо відповідь моделі та нормалізуємо файл у PNG.
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.load()
                normalized = io.BytesIO()
                image.convert("RGB").save(
                    normalized,
                    format="PNG",
                    optimize=True,
                )
                image_bytes = normalized.getvalue()

        except Exception as exc:
            raise ImageServiceError(
                "Модель повернула пошкоджене зображення. Спробуй ще раз.",
                repr(exc),
            ) from exc

        stop_event.set()
        await animation_task

        try:
            await status.edit_text(
                "✅ <b>Зображення створено.</b>\n📤 Надсилаю файл…"
            )
        except TelegramAPIError:
            pass

        try:
            await message.answer_photo(
                BufferedInputFile(
                    image_bytes,
                    filename="marketphoto.png",
                ),
                caption=(
                    "✨ <b>Готово!</b> Можеш створити ще одне зображення "
                    "або змінити це фото."
                ),
                reply_markup=MAIN_KB,
            )

        except TelegramAPIError:
            log.exception(
                "Telegram failed to send generated image to user_id=%s",
                user.id,
            )

            try:
                await status.edit_text(
                    "Фото створене, але Telegram не зміг його надіслати. "
                    "Ліміт не списано. Спробуй ще раз."
                )
            except TelegramAPIError:
                pass

        else:
            try:
                charge_successful_generation(user.id, mode)
            except Exception:
                log.exception(
                    "Could not update quota for user_id=%s",
                    user.id,
                )

            try:
                await status.edit_text("✅ <b>Готово!</b> Фото надіслано.")
            except TelegramAPIError:
                pass

    except ImageServiceError as exc:
        log_failed_generation(user.id, mode)

        log.error(
            "Image generation failed for user_id=%s mode=%s detail=%s",
            user.id,
            mode,
            exc.detail or str(exc),
        )

        stop_event.set()
        await animation_task

        try:
            await status.edit_text(
                "❌ <b>Не вдалося створити фото.</b>\n\n"
                + exc.user_message
            )
        except TelegramAPIError:
            await message.answer(
                "❌ Не вдалося створити фото. " + exc.user_message,
                reply_markup=MAIN_KB,
            )

    except Exception:
        log_failed_generation(user.id, mode)

        log.exception(
            "Unexpected image generation failure for user_id=%s mode=%s",
            user.id,
            mode,
        )

        stop_event.set()
        await animation_task

        try:
            await status.edit_text(
                "❌ <b>Сталася внутрішня помилка.</b>\n\n"
                "Ліміт не списано. Спробуй ще раз. "
                "Якщо помилка повторюється, адміністратор має перевірити "
                "Render → Logs."
            )
        except TelegramAPIError:
            await message.answer(
                "❌ Сталася внутрішня помилка. "
                "Ліміт не списано; перевір Render → Logs.",
                reply_markup=MAIN_KB,
            )

    finally:
        stop_event.set()

        if not animation_task.done():
            try:
                await animation_task
            except Exception:
                pass

        busy_users.discard(user.id)
        await state.clear()


# =========================
# Отримання фото для редагування
# =========================

@dp.message(StateFilter(Flow.waiting_photo), F.photo)
async def receive_edit_photo(message: Message, state: FSMContext):
    if not message.photo:
        return

    try:
        tg_file = await bot.get_file(message.photo[-1].file_id)

        if not tg_file.file_path:
            raise RuntimeError("Telegram did not return file_path")

        buffer = io.BytesIO()

        await bot.download_file(
            tg_file.file_path,
            destination=buffer,
        )

        small_image = await make_small_png(buffer.getvalue())

        await state.update_data(input_image=small_image)
        await state.set_state(Flow.waiting_edit_prompt)

        await message.answer(
            "✅ Фото отримано!\n\n"
            "<b>Тепер напиши, що саме потрібно змінити.</b>\n\n"
            "Наприклад: «Заміни фон на світлу професійну фотостудію, "
            "обличчя й одяг залиш без змін».",
            reply_markup=MAIN_KB,
        )

    except ImageServiceError as exc:
        await message.answer(exc.user_message)

    except Exception:
        log.exception(
            "Failed to download reference photo for user_id=%s",
            message.from_user.id,
        )

        await message.answer(
            "Не вдалося завантажити фото з Telegram. "
            "Спробуй надіслати його ще раз або введи /cancel."
        )


@dp.message(StateFilter(Flow.waiting_photo), F.text)
async def waiting_for_photo_text(message: Message):
    await message.answer(
        "Спочатку надішли саме фото. Після цього я попрошу "
        "написати, що потрібно змінити.\n\n"
        "Для скасування — /cancel."
    )


@dp.message(StateFilter(Flow.waiting_prompt), F.text)
async def receive_prompt(message: Message, state: FSMContext):
    # Приймаємо текст тільки після кнопки «Створити фото».
    if message.text.startswith("/"):
        return
    await do_generation(message, state, message.text or "")


@dp.message(StateFilter(Flow.waiting_edit_prompt), F.text)
async def receive_edit_prompt(message: Message, state: FSMContext):
    # Не передаємо команди замість інструкції редагування.
    if (message.text or "").startswith("/"):
        return

    data = await state.get_data()
    image_bytes = data.get("input_image")

    if not image_bytes:
        await state.set_state(Flow.waiting_photo)
        await message.answer("Фото не знайдено. Надішли його ще раз.")
        return

    await do_generation(
        message,
        state,
        message.text or "",
        input_image=image_bytes,
    )


# =========================
# Команда /imagine
# =========================

@dp.message(Command("imagine"))
async def cmd_imagine(message: Message, state: FSMContext):
    prompt = (message.text or "").partition(" ")[2].strip()

    if not prompt:
        await state.clear()
        await state.set_state(Flow.waiting_prompt)

        await message.answer(
            "Напиши опис зображення наступним повідомленням "
            "або введи /cancel для скасування.",
            reply_markup=MAIN_KB,
        )
        return

    await state.clear()
    await do_generation(message, state, prompt)


# =========================
# Фото без режиму редагування
# =========================

@dp.message(F.photo)
async def photo_outside_edit_mode(message: Message):
    await message.answer(
        "Щоб змінити фото, спочатку натисни "
        "«✏️ Редагувати фото», а потім надішли зображення.",
        reply_markup=MAIN_KB,
    )


@dp.message(F.text)
async def fallback_text(message: Message, state: FSMContext):
    # Кнопки обробляються окремо. Звичайний текст поза режимом
    # генерації не повинен автоматично запускати модель.
    await message.answer(
        "Я отримав твоє повідомлення.\n\n"
        "🖼 Для створення фото натисни «🖼 Створити фото».\n"
        "✏️ Для редагування натисни «✏️ Редагувати фото».\n\n"
        "Команди: /start, /help, /profile, /cancel, /imagine.",
        reply_markup=MAIN_KB,
    )


# =========================
# Render health endpoint
# =========================

async def health(_request: web.Request):
    return web.json_response({
        "ok": True,
        "service": "MarketPhoto AI",
    })


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT,
    )

    await site.start()
    log.info("Health server listening on 0.0.0.0:%s", PORT)

    return runner


# =========================
# Запуск бота
# =========================

async def main():
    init_db()

    await bot.set_my_commands([
        BotCommand(command="start", description="Головне меню"),
        BotCommand(command="help", description="Допомога"),
        BotCommand(command="imagine", description="Створити фото за описом"),
        BotCommand(command="profile", description="Мій профіль"),
        BotCommand(command="services", description="Послуги та генерації"),
        BotCommand(command="cancel", description="Скасувати поточну дію"),
        BotCommand(command="id", description="Мій Telegram ID"),
    ])

    runner = await start_health_server()

    log.info(
        "MarketPhoto AI starting; model=%s",
        MODEL_NAME,
    )

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
