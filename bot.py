import asyncio
import base64
import io
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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
    BotCommand,
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
from aiohttp import web


# =========================================================
# НАЛАШТУВАННЯ
# =========================================================

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN", "")
CF_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CF_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "marketphoto.db")

MODEL_NAME = "@cf/black-forest-labs/flux-2-klein-4b"

FREE_DAILY_LIMIT = 3
PACKAGE_CREDITS = 5
PACKAGE_PRICE_STARS = 100
MAX_PROMPT_LENGTH = 2500
CF_TIMEOUT = 180

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("marketphoto")

if not BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_TOKEN is missing. Add it in Render Environment."
    )

if not CF_ACCOUNT_ID or not CF_API_TOKEN:
    log.warning(
        "Cloudflare credentials missing. "
        "Add CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN."
    )

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())

BUSY_USERS = set()


# =========================================================
# МЕНЮ
# =========================================================

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🖼 Звичайне фото"),
            KeyboardButton(text="👤 Аватарка"),
        ],
        [
            KeyboardButton(text="📱 Заставка телефона"),
            KeyboardButton(text="🖥 Заставка ПК"),
        ],
        [
            KeyboardButton(text="✏️ Редагувати фото"),
            KeyboardButton(text="💎 Купити генерації"),
        ],
        [
            KeyboardButton(text="📊 Мій профіль"),
            KeyboardButton(text="❓ Допомога"),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Вибери дію або напиши запит…",
)

BUY_BUTTON = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"⭐ {PACKAGE_CREDITS} генерацій — "
                     f"{PACKAGE_PRICE_STARS} зірок",
                callback_data="buy_package",
            )
        ]
    ]
)

ACCEPT_TERMS_BUTTON = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Прочитав і погоджуюсь",
                callback_data="accept_terms",
            )
        ]
    ]
)


class ImageFlow(StatesGroup):
    waiting_prompt = State()
    waiting_photo = State()
    waiting_edit_prompt = State()


# =========================================================
# БАЗА ДАНИХ
# =========================================================

def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    parent = Path(DB_PATH).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)

    with connect_db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                daily_date TEXT DEFAULT '',
                daily_count INTEGER DEFAULT 0,
                credits INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                success INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                stars INTEGER NOT NULL,
                credits INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.commit()


def today():
    return datetime.now(timezone.utc).date().isoformat()


def touch_user(user):
    if not user:
        return

    with connect_db() as con:
        con.execute("""
            INSERT INTO users(user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
        ))
        con.commit()


def get_stats(user_id):
    with connect_db() as con:
        row = con.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()

    if not row:
        return {"used": 0, "credits": 0}

    used = row["daily_count"] if row["daily_date"] == today() else 0

    return {
        "used": used,
        "credits": row["credits"],
    }


def reserve_generation(user_id):
    """
    Резервує одну генерацію до запуску AI.
    Якщо генерація не вдалася, резерв повертається.
    """
    day = today()
    con = connect_db()

    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT daily_date, daily_count, credits "
            "FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()

        if not row:
            con.execute("""
                INSERT OR IGNORE INTO users(user_id)
                VALUES (?)
            """, (user_id,))
            row = con.execute(
                "SELECT daily_date, daily_count, credits "
                "FROM users WHERE user_id=?",
                (user_id,),
            ).fetchone()

        used = row["daily_count"] if row["daily_date"] == day else 0

        if used < FREE_DAILY_LIMIT:
            con.execute("""
                UPDATE users SET daily_date=?, daily_count=?
                WHERE user_id=?
            """, (day, used + 1, user_id))
            con.commit()
            return ("daily", day)

        if row["credits"] > 0:
            con.execute("""
                UPDATE users SET credits=credits-1
                WHERE user_id=?
            """, (user_id,))
            con.commit()
            return ("credit", day)

        con.rollback()
        return None

    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def refund_generation(user_id, reservation):
    if not reservation:
        return

    kind, reserved_day = reservation

    with connect_db() as con:
        if kind == "credit":
            con.execute("""
                UPDATE users SET credits=credits+1
                WHERE user_id=?
            """, (user_id,))

        elif kind == "daily":
            con.execute("""
                UPDATE users
                SET daily_count=MAX(0, daily_count-1)
                WHERE user_id=? AND daily_date=?
            """, (user_id, reserved_day))

        con.commit()


def log_generation(user_id, mode, success):
    with connect_db() as con:
        con.execute("""
            INSERT INTO generations(user_id, mode, success)
            VALUES (?, ?, ?)
        """, (user_id, mode, int(success)))
        con.commit()


def register_payment(user_id, stars, credits, charge_id):
    """
    Повторний update про той самий платіж не зарахує кредити двічі.
    """
    con = connect_db()

    try:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute("""
            INSERT OR IGNORE INTO payments(
                charge_id, user_id, stars, credits
            ) VALUES (?, ?, ?, ?)
        """, (charge_id, user_id, stars, credits))

        if cur.rowcount != 1:
            con.rollback()
            return False

        con.execute("""
            UPDATE users SET credits=credits+?
            WHERE user_id=?
        """, (credits, user_id))

        con.commit()
        return True

    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def admin_statistics():
    with connect_db() as con:
        users = con.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]
        ok = con.execute(
            "SELECT COUNT(*) FROM generations WHERE success=1"
        ).fetchone()[0]
        failed = con.execute(
            "SELECT COUNT(*) FROM generations WHERE success=0"
        ).fetchone()[0]
        payments = con.execute(
            "SELECT COUNT(*) FROM payments"
        ).fetchone()[0]

    return users, ok, failed, payments


# =========================================================
# ЗАПИТИ ТА БЕЗПЕЧНІСТЬ
# =========================================================

BLOCKED_IMAGE_REQUESTS = [
    "раздень",
    "раздев",
    "раздеть",
    "раздета",
    "раздетая",
    "раздетый",
    "снять одежду",
    "без одежды",
    "голая",
    "голый",
    "обнажи",
    "обнажить",
    "deepnude",
    "undress",
    "naked",
    "remove clothes",
    "take off her clothes",
    "take off his clothes",
    "make her nude",
    "make him nude",
]


def is_blocked_request(prompt):
    lowered = prompt.casefold()
    return any(term in lowered for term in BLOCKED_IMAGE_REQUESTS)


class ImageServiceError(Exception):
    def __init__(self, message, detail=""):
        super().__init__(detail or message)
        self.user_message = message
        self.detail = detail


# =========================================================
# CLOUDFLARE: СТВОРЕННЯ / РЕДАГУВАННЯ ЗОБРАЖЕННЯ
# =========================================================

def cloudflare_error(status, raw):
    detail = raw.decode("utf-8", errors="replace")[:1200]
    try:
        data = json.loads(detail)
        errors = data.get("errors") or []
        if errors:
            detail = "; ".join(
                str(e.get("message", e))
                if isinstance(e, dict) else str(e)
                for e in errors
            )[:1200]
    except Exception:
        pass

    if status in (401, 403):
        text = (
            "Cloudflare відхилив доступ. Перевір "
            "CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID "
            "та дозволи Workers AI у токені."
        )
    elif status == 429:
        text = (
            "Cloudflare обмежив кількість запитів. "
            "Перевір квоту й спробуй пізніше."
        )
    elif status == 400:
        text = (
            "Cloudflare не прийняв параметри запиту. "
            "Адміністратору потрібно перевірити Render Logs."
        )
    elif status >= 500:
        text = (
            "Cloudflare тимчасово недоступний. "
            "Спробуй знову трохи пізніше."
        )
    else:
        text = f"Cloudflare повернув HTTP {status}."

    return ImageServiceError(text, f"HTTP {status}: {detail}")


def parse_cloudflare_image(raw, content_type):
    if content_type.lower().startswith("image/"):
        return raw

    # Деякі відповіді повертають PNG напряму, без JSON.
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return raw
    if raw.startswith(b"\xff\xd8\xff") or raw.startswith(b"RIFF"):
        return raw

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ImageServiceError(
            "Cloudflare повернув невідомий формат відповіді.",
            f"content-type={content_type}; size={len(raw)}",
        ) from exc

    if isinstance(data, dict) and data.get("success") is False:
        errors = data.get("errors") or []
        detail = "; ".join(
            str(e.get("message", e))
            if isinstance(e, dict) else str(e)
            for e in errors
        )
        raise ImageServiceError(
            "Cloudflare не зміг створити фото. Перевір Render Logs.",
            detail,
        )

    result = data.get("result", data) if isinstance(data, dict) else data

    if isinstance(result, dict):
        result = (
            result.get("image")
            or result.get("images")
            or result.get("output")
            or result.get("b64_json")
            or result.get("data")
        )

    if isinstance(result, list) and result:
        result = result[0]

    if isinstance(result, dict):
        result = (
            result.get("image")
            or result.get("b64_json")
            or result.get("data")
        )

    if not isinstance(result, str) or not result.strip():
        raise ImageServiceError(
            "Cloudflare не повернув готове зображення. "
            "Перевір Render Logs.",
            f"Unexpected response structure: {type(result).__name__}",
        )

    encoded = result.strip()

    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]

    try:
        return base64.b64decode(encoded)
    except Exception as exc:
        raise ImageServiceError(
            "Не вдалося прочитати картинку від Cloudflare.",
            repr(exc),
        ) from exc


async def create_image(prompt, width, height, input_image=None):
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        raise ImageServiceError(
            "Cloudflare не налаштовано. Додай "
            "CLOUDFLARE_ACCOUNT_ID і CLOUDFLARE_API_TOKEN "
            "у Render → Environment."
        )

    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/ai/run/{MODEL_NAME}"
    )

    # Важливо: FLUX.2 Klein використовує multipart/form-data.
    # Не задавай Content-Type вручну — aiohttp додасть boundary.
    form = aiohttp.FormData()
    form.add_field("prompt", prompt)
    form.add_field("width", str(width))
    form.add_field("height", str(height))
    form.add_field("guidance", "4")

    if input_image:
        form.add_field(
            "input_image_0",
            input_image,
            filename="reference.png",
            content_type="image/png",
        )

    timeout = aiohttp.ClientTimeout(
        total=CF_TIMEOUT,
        connect=20,
        sock_read=CF_TIMEOUT,
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

                if not 200 <= response.status < 300:
                    raise cloudflare_error(response.status, raw)

                content_type = response.headers.get("Content-Type", "")
                return parse_cloudflare_image(raw, content_type)

        except asyncio.TimeoutError as exc:
            raise ImageServiceError(
                "Генерація триває занадто довго. Спробуй іще раз.",
                "Cloudflare timeout",
            ) from exc

        except aiohttp.ClientError as exc:
            raise ImageServiceError(
                "Не вдалося з'єднатися з Cloudflare. "
                "Перевір сервіс і спробуй пізніше.",
                repr(exc),
            ) from exc


def normalize_reference_image(raw):
    """
    Усі референсні зображення зменшуються до максимуму 511x511:
    це відповідає обмеженню для input_image_0 у FLUX.2 Klein.
    """
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            image.thumbnail((511, 511), Image.Resampling.LANCZOS)

            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()

    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageServiceError(
            "Не вдалося прочитати фото. Надішли звичайний JPG або PNG.",
            repr(exc),
        ) from exc


def normalize_generated_image(raw):
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            output = io.BytesIO()
            image.convert("RGB").save(
                output,
                format="PNG",
                optimize=True,
            )
            return output.getvalue()

    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageServiceError(
            "Модель повернула пошкоджений файл зображення.",
            repr(exc),
        ) from exc


# =========================================================
# АНІМАЦІЯ СТВОРЕННЯ
# =========================================================

async def animate_progress(status_message, stop_event):
    frames = [
        "🎨 <b>Створюю зображення</b>\n▰▱▱▱ Аналіз запиту",
        "🎨 <b>Створюю зображення.</b>\n▰▰▱▱ Побудова композиції",
        "🎨 <b>Створюю зображення..</b>\n▰▰▰▱ Деталізація",
        "✨ <b>Завершую обробку</b>\n▰▰▰▰ Підготовка файлу",
    ]

    index = 0

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3)
            break
        except asyncio.TimeoutError:
            try:
                await status_message.edit_text(frames[index % len(frames)])
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    log.debug("Progress edit failed: %s", exc)
            except TelegramAPIError as exc:
                log.debug("Progress update failed: %s", exc)

            index += 1


# =========================================================
# ПРОФІЛЬ, ДОПОМОГА, УМОВИ
# =========================================================

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    touch_user(message.from_user)

    await message.answer(
        "<b>✨ MarketPhoto AI PRO</b>\n\n"
        "Створюй зображення за описом, роби аватарки та заставки, "
        "редагуй власні фото.\n\n"
        f"🎁 Щодня доступно до {FREE_DAILY_LIMIT} безкоштовних генерацій.\n"
        f"💎 Пакет {PACKAGE_CREDITS} додаткових генерацій — "
        f"{PACKAGE_PRICE_STARS} Telegram Stars.\n\n"
        "Обери потрібну функцію в меню нижче 👇",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("help"))
@dp.message(F.text == "❓ Допомога")
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "<b>❓ Як користуватися ботом</b>\n\n"
        "🖼 <b>Звичайне фото</b> — опиши зображення.\n"
        "👤 <b>Аватарка</b> — портрет для профілю.\n"
        "📱 <b>Заставка телефона</b> — вертикальний формат.\n"
        "🖥 <b>Заставка ПК</b> — широкий формат.\n"
        "✏️ <b>Редагувати фото</b> — надішли фото та опиши зміни.\n\n"
        "Команди:\n"
        "/start — головне меню\n"
        "/imagine опис — генерація за описом\n"
        "/profile — твій профіль\n"
        "/services — послуги та пакет генерацій\n"
        "/terms — умови використання\n"
        "/paysupport — допомога з оплатою\n"
        "/cancel — скасувати дію\n"
        "/id — твій Telegram ID",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✅ Дію скасовано. Обери наступну функцію.",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(
        f"Твій Telegram ID: <code>{message.from_user.id}</code>"
    )


@dp.message(Command("terms"))
async def cmd_terms(message: Message):
    await message.answer(
        "<b>Умови MarketPhoto AI PRO</b>\n\n"
        "1. Генерації залежать від доступності стороннього сервісу AI.\n"
        "2. Безкоштовний ліміт становить до "
        f"{FREE_DAILY_LIMIT} успішних генерацій на день.\n"
        f"3. Платний пакет містить {PACKAGE_CREDITS} генерацій "
        f"за {PACKAGE_PRICE_STARS} Stars.\n"
        "4. Не завантажуй фото без дозволу людей, зображених на них.\n"
        "5. Заборонено використовувати сервіс для створення "
        "інтимних зображень реальних людей без їхньої згоди.\n"
        "6. Якщо виникла проблема з оплатою, скористайся /paysupport.\n\n"
        "Натискання кнопки згоди перед оплатою підтверджує, "
        "що ти прочитав ці умови."
    )


@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    await message.answer(
        "💳 <b>Допомога з оплатою</b>\n\n"
        "Якщо Stars списалися, а генерації не з'явилися, "
        "звернись до адміністратора та додай:\n"
        f"• Telegram ID: <code>{message.from_user.id}</code>\n"
        "• дату платежу й короткий опис проблеми.\n\n"
        "Не надсилай токени, паролі або дані платіжних карток."
    )


@dp.message(Command("profile"))
@dp.message(F.text == "📊 Мій профіль")
async def cmd_profile(message: Message, state: FSMContext):
    await state.clear()
    touch_user(message.from_user)
    stats = get_stats(message.from_user.id)

    left = max(0, FREE_DAILY_LIMIT - stats["used"])

    await message.answer(
        "<b>📊 Твій профіль</b>\n\n"
        f"ID: <code>{message.from_user.id}</code>\n"
        f"Безкоштовні сьогодні: {stats['used']}/{FREE_DAILY_LIMIT}\n"
        f"Залишок безкоштовних: {left}\n"
        f"Платні генерації: {stats['credits']}",
        reply_markup=MAIN_MENU,
    )


@dp.message(Command("services"))
@dp.message(F.text == "💎 Купити генерації")
async def cmd_services(message: Message, state: FSMContext):
    await state.clear()
    touch_user(message.from_user)

    await message.answer(
        "<b>💎 Пакет генерацій</b>\n\n"
        f"⭐ {PACKAGE_CREDITS} додаткових генерацій — "
        f"{PACKAGE_PRICE_STARS} Telegram Stars.\n\n"
        "Платіж обробляється Telegram. Генерації зараховуються "
        "після підтвердження успішної оплати.",
        reply_markup=BUY_BUTTON,
    )


# =========================================================
# ОПЛАТА TELEGRAM STARS
# =========================================================

@dp.callback_query(F.data == "buy_package")
async def buy_package(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "Перед оплатою прочитай умови через /terms.\n\n"
        f"Після згоди бот надішле рахунок на "
        f"{PACKAGE_PRICE_STARS} Telegram Stars.",
        reply_markup=ACCEPT_TERMS_BUTTON,
    )


@dp.callback_query(F.data == "accept_terms")
async def accept_terms(callback: CallbackQuery):
    await callback.answer()
    user = callback.from_user
    touch_user(user)

    payload = f"credits:{PACKAGE_CREDITS}:{user.id}"

    try:
        await bot.send_invoice(
            chat_id=user.id,
            title=f"{PACKAGE_CREDITS} генерацій MarketPhoto AI",
            description=(
                f"Пакет із {PACKAGE_CREDITS} додаткових генерацій "
                "для фото, аватарок, заставок і редагування."
            ),
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[
                LabeledPrice(
                    label=f"{PACKAGE_CREDITS} генерацій",
                    amount=PACKAGE_PRICE_STARS,
                )
            ],
        )
    except Exception:
        log.exception("Could not create Stars invoice")
        await callback.message.answer(
            "Не вдалося створити рахунок. Спробуй пізніше."
        )


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    parts = query.invoice_payload.split(":")
    valid = False

    try:
        valid = (
            len(parts) == 3
            and parts[0] == "credits"
            and int(parts[1]) == PACKAGE_CREDITS
            and int(parts[2]) == query.from_user.id
            and query.currency == "XTR"
            and query.total_amount == PACKAGE_PRICE_STARS
        )
    except (ValueError, TypeError):
        valid = False

    await query.answer(
        ok=valid,
        error_message=(
            None if valid else
            "Не вдалося перевірити рахунок. Відкрий послуги ще раз."
        ),
    )


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    parts = payment.invoice_payload.split(":")

    try:
        valid = (
            len(parts) == 3
            and parts[0] == "credits"
            and int(parts[1]) == PACKAGE_CREDITS
            and int(parts[2]) == message.from_user.id
            and payment.currency == "XTR"
            and payment.total_amount == PACKAGE_PRICE_STARS
        )
    except (ValueError, TypeError):
        valid = False

    if not valid:
        await message.answer(
            "Не вдалося підтвердити платіж для цього профілю. "
            "Скористайся /paysupport."
        )
        return

    touch_user(message.from_user)

    charge_id = payment.telegram_payment_charge_id

    try:
        added = register_payment(
            user_id=message.from_user.id,
            stars=payment.total_amount,
            credits=PACKAGE_CREDITS,
            charge_id=charge_id,
        )

        if added:
            await message.answer(
                f"✅ Оплату підтверджено!\n"
                f"Додано {PACKAGE_CREDITS} генерацій.",
                reply_markup=MAIN_MENU,
            )
        else:
            await message.answer(
                "Цей платіж уже був зарахований раніше."
            )

    except Exception:
        log.exception("Payment database update failed")
        await message.answer(
            "Платіж отримано, але баланс не оновився автоматично. "
            "Звернись через /paysupport."
        )


# =========================================================
# АДМІНІСТРАТОР
# =========================================================

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if ADMIN_ID == 0 or message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Немає доступу до адміністратора.")
        return

    users, success, failed, payments = admin_statistics()

    await message.answer(
        "<b>🛠 Статистика бота</b>\n\n"
        f"Користувачів: {users}\n"
        f"Успішних генерацій: {success}\n"
        f"Невдалих генерацій: {failed}\n"
        f"Платежів: {payments}"
    )


# =========================================================
# ВИБІР РЕЖИМУ
# =========================================================

async def ask_prompt(message, state, mode, width, height, style_prompt):
    await state.clear()
    touch_user(message.from_user)

    stats = get_stats(message.from_user.id)
    if stats["used"] >= FREE_DAILY_LIMIT and stats["credits"] <= 0:
        await message.answer(
            "🎁 Безкоштовні генерації закінчилися. "
            "Відкрий «💎 Купити генерації» для додаткового пакета.",
            reply_markup=MAIN_MENU,
        )
        return

    await state.set_state(ImageFlow.waiting_prompt)
    await state.update_data(
        mode=mode,
        width=width,
        height=height,
        style_prompt=style_prompt,
    )

    await message.answer(
        f"📝 <b>{mode}</b>\n\n"
        "Опиши, що саме потрібно створити.\n\n"
        "Чим докладніший опис — тим легше моделі зрозуміти задум.\n"
        "Для скасування введи /cancel.",
        reply_markup=MAIN_MENU,
    )


@dp.message(F.text == "🖼 Звичайне фото")
async def mode_photo(message: Message, state: FSMContext):
    await ask_prompt(
        message, state, "Звичайне фото",
        1024, 1024,
        "Create a high-quality, detailed image. "
        "Professional composition and realistic lighting. ",
    )


@dp.message(F.text == "👤 Аватарка")
async def mode_avatar(message: Message, state: FSMContext):
    await ask_prompt(
        message, state, "Аватарка",
        1024, 1024,
        "Create a polished square social media avatar, "
        "centered subject, clear face if a person is requested, "
        "beautiful lighting, clean background, no text, no watermark. ",
    )


@dp.message(F.text == "📱 Заставка телефона")
async def mode_phone_wallpaper(message: Message, state: FSMContext):
    await ask_prompt(
        message, state, "Заставка телефона",
        1080, 1920,
        "Create a vertical 9:16 smartphone wallpaper, "
        "beautiful high-quality detail, balanced composition, "
        "keep the main subject away from the top clock area, "
        "no text, no watermark. ",
    )


@dp.message(F.text == "🖥 Заставка ПК")
async def mode_pc_wallpaper(message: Message, state: FSMContext):
    await ask_prompt(
        message, state, "Заставка ПК",
        1920, 1080,
        "Create a wide 16:9 desktop computer wallpaper, "
        "beautiful cinematic composition, high detail, "
        "leave some uncluttered space for desktop icons, "
        "no text, no watermark. ",
    )


@dp.message(F.text == "✏️ Редагувати фото")
async def mode_edit(message: Message, state: FSMContext):
    await state.clear()
    touch_user(message.from_user)

    stats = get_stats(message.from_user.id)
    if stats["used"] >= FREE_DAILY_LIMIT and stats["credits"] <= 0:
        await message.answer(
            "🎁 Безкоштовні генерації закінчилися. "
            "Відкрий «💎 Купити генерації».",
            reply_markup=MAIN_MENU,
        )
        return

    await state.set_state(ImageFlow.waiting_photo)

    await message.answer(
        "✏️ <b>Редагування фото</b>\n\n"
        "Надішли фото, яке потрібно змінити. "
        "Після цього я попрошу описати бажані зміни.\n\n"
        "Можна змінити фон, світло, художній стиль, зачіску "
        "або одяг на звичайний повсякденний образ.\n"
        "Я не створюю оголені зображення реальних людей.\n\n"
        "Для скасування — /cancel.",
        reply_markup=MAIN_MENU,
    )


# =========================================================
# ОБРОБКА ФОТО
# =========================================================

@dp.message(StateFilter(ImageFlow.waiting_photo), F.photo)
async def receive_photo(message: Message, state: FSMContext):
    try:
        photo = message.photo[-1]
        telegram_file = await bot.get_file(photo.file_id)

        if not telegram_file.file_path:
            raise RuntimeError("Telegram returned an empty file path")

        buffer = io.BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)

        reference_image = normalize_reference_image(buffer.getvalue())

        await state.update_data(input_image=reference_image)
        await state.set_state(ImageFlow.waiting_edit_prompt)

        await message.answer(
            "✅ Фото отримано!\n\n"
            "Тепер напиши, що потрібно змінити.\n\n"
            "Приклад: «Зроби фон професійною фотостудією, "
            "додай м'яке освітлення, збережи обличчя та одяг».\n\n"
            "Для скасування — /cancel.",
            reply_markup=MAIN_MENU,
        )

    except ImageServiceError as exc:
        await message.answer(exc.user_message)

    except Exception:
        log.exception("Could not download reference image")
        await message.answer(
            "Не вдалося завантажити фото з Telegram. "
            "Спробуй надіслати його знову."
        )


@dp.message(StateFilter(ImageFlow.waiting_photo), F.text)
async def text_while_waiting_photo(message: Message):
    await message.answer(
        "Спочатку надішли саме фото, а потім опиши зміни. "
        "Для скасування — /cancel."
    )


# =========================================================
# ГОЛОВНА ЛОГІКА ГЕНЕРАЦІЇ
# =========================================================

async def generate_and_send(
    message,
    state,
    prompt,
    mode,
    width,
    height,
    input_image=None,
    style_prompt="",
):
    user = message.from_user
    touch_user(user)

    prompt = (prompt or "").strip()

    if not prompt:
        await message.answer("Запит порожній. Опиши потрібне зображення.")
        return

    if len(prompt) > MAX_PROMPT_LENGTH:
        await message.answer(
            f"Запит задовгий. Максимум — {MAX_PROMPT_LENGTH} символів."
        )
        return

    if is_blocked_request(prompt):
        await message.answer(
            "Я не можу роздягати реальних людей на фото або створювати "
            "їхні оголені зображення. Можу змінити одяг на інший "
            "повсякденний образ, фон, зачіску чи стиль фото."
        )
        return

    if user.id in BUSY_USERS:
        await message.answer(
            "⏳ Твій попередній запит ще виконується. "
            "Дочекайся результату."
        )
        return

    reservation = reserve_generation(user.id)

    if reservation is None:
        await message.answer(
            "🎁 Генерації закінчилися. "
            "Відкрий «💎 Купити генерації» для додаткового пакета.",
            reply_markup=MAIN_MENU,
        )
        return

    BUSY_USERS.add(user.id)

    status = await message.answer(
        "🎨 <b>Готую запит до AI…</b>"
    )

    stop_event = asyncio.Event()
    animation_task = asyncio.create_task(
        animate_progress(status, stop_event)
    )

    delivered = False

    try:
        if input_image:
            full_prompt = (
                "Edit the supplied reference image according to the user's "
                "instructions. Keep the depicted person fully clothed. "
                "Do not create nudity or sexualized disrobing. "
                "Keep the person's identity recognizable unless the user "
                "requests a permitted stylistic transformation. "
                "Instructions: " + prompt
            )
        else:
            full_prompt = style_prompt + prompt

        raw_image = await create_image(
            full_prompt,
            width,
            height,
            input_image=input_image,
        )

        png_image = normalize_generated_image(raw_image)

        stop_event.set()
        await animation_task

        try:
            await status.edit_text(
                "✅ <b>Зображення створено!</b>\n📤 Надсилаю файл…"
            )
        except TelegramAPIError:
            pass

        file = BufferedInputFile(png_image, filename="marketphoto.png")

        # Якщо файл завеликий для фото, надсилаємо як документ.
        if len(png_image) > 9 * 1024 * 1024:
            await message.answer_document(
                file,
                caption="✨ <b>Готово!</b> Зображення прикріплено файлом.",
                reply_markup=MAIN_MENU,
            )
        else:
            await message.answer_photo(
                file,
                caption="✨ <b>Готово!</b> Обери наступну дію в меню.",
                reply_markup=MAIN_MENU,
            )

        delivered = True

        try:
            log_generation(user.id, mode, True)
        except Exception:
            log.exception("Could not save successful generation")

        try:
            await status.edit_text("✅ <b>Готово! Зображення надіслано.</b>")
        except TelegramAPIError:
            pass

    except ImageServiceError as exc:
        log.error(
            "Image generation error: mode=%s detail=%s",
            mode,
            exc.detail or str(exc),
        )

        if not delivered:
            try:
                refund_generation(user.id, reservation)
            except Exception:
                log.exception("Could not refund generation")

            try:
                log_generation(user.id, mode, False)
            except Exception:
                log.exception("Could not record failed generation")

        stop_event.set()
        await animation_task

        try:
            await status.edit_text(
                "❌ <b>Не вдалося створити зображення.</b>\n\n"
                + exc.user_message
            )
        except TelegramAPIError:
            await message.answer(
                "❌ " + exc.user_message,
                reply_markup=MAIN_MENU,
            )

    except Exception:
        log.exception("Unexpected failure during image generation")

        if not delivered:
            try:
                refund_generation(user.id, reservation)
            except Exception:
                log.exception("Could not refund failed generation")

            try:
                log_generation(user.id, mode, False)
            except Exception:
                log.exception("Could not record failed generation")

        stop_event.set()
        await animation_task

        try:
            await status.edit_text(
                "❌ <b>Сталася помилка.</b>\n\n"
                "Генерацію повернуто на баланс, якщо вона не була "
                "успішно надіслана. Перевір Render → Logs."
            )
        except TelegramAPIError:
            await message.answer(
                "❌ Сталася помилка. Перевір Render → Logs.",
                reply_markup=MAIN_MENU,
            )

    finally:
        stop_event.set()

        if not animation_task.done():
            animation_task.cancel()
            try:
                await animation_task
            except asyncio.CancelledError:
                pass

        BUSY_USERS.discard(user.id)
        await state.clear()


# =========================================================
# ТЕКСТОВІ ЗАПИТИ
# =========================================================

@dp.message(Command("imagine"))
async def cmd_imagine(message: Message, state: FSMContext):
    prompt = (message.text or "").partition(" ")[2].strip()

    if not prompt:
        await ask_prompt(
            message, state, "Звичайне фото",
            1024, 1024, "",
        )
        return

    await state.clear()

    await generate_and_send(
        message=message,
        state=state,
        prompt=prompt,
        mode="photo",
        width=1024,
        height=1024,
    )


@dp.message(StateFilter(ImageFlow.waiting_prompt), F.text)
async def receive_prompt(message: Message, state: FSMContext):
    if (message.text or "").startswith("/"):
        return

    data = await state.get_data()

    await generate_and_send(
        message=message,
        state=state,
        prompt=message.text or "",
        mode=data.get("mode", "photo"),
        width=int(data.get("width", 1024)),
        height=int(data.get("height", 1024)),
        style_prompt=data.get("style_prompt", ""),
    )


@dp.message(StateFilter(ImageFlow.waiting_edit_prompt), F.text)
async def receive_edit_prompt(message: Message, state: FSMContext):
    if (message.text or "").startswith("/"):
        return

    data = await state.get_data()
    reference = data.get("input_image")

    if not reference:
        await state.set_state(ImageFlow.waiting_photo)
        await message.answer("Фото втрачено. Надішли його ще раз.")
        return

    await generate_and_send(
        message=message,
        state=state,
        prompt=message.text or "",
        mode="edit",
        width=1024,
        height=1024,
        input_image=reference,
    )


@dp.message(F.photo)
async def photo_without_edit_mode(message: Message):
    await message.answer(
        "Щоб відредагувати фото, спочатку натисни "
        "«✏️ Редагувати фото», а потім надішли зображення.",
        reply_markup=MAIN_MENU,
    )


@dp.message(F.text)
async def fallback_text(message: Message, state: FSMContext):
    await message.answer(
        "Я отримав повідомлення, але спочатку вибери режим у меню.\n\n"
        "🖼 Фото · 👤 Аватарка · 📱 Заставка телефона · "
        "🖥 Заставка ПК · ✏️ Редагування",
        reply_markup=MAIN_MENU,
    )


# =========================================================
# HEALTH CHECK ДЛЯ RENDER
# =========================================================

async def health(_request):
    return web.json_response({
        "ok": True,
        "service": "MarketPhoto AI PRO",
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

    log.info("HEALTH SERVER STARTED on port %s", PORT)
    return runner


# =========================================================
# ЗАПУСК
# =========================================================

async def main():
    init_db()
    runner = await start_health_server()

    try:
        log.info("Checking Telegram connection...")

        me = await asyncio.wait_for(bot.get_me(), timeout=20)
        log.info("TELEGRAM CONNECTED: @%s, id=%s", me.username, me.id)

        # Polling не може одночасно працювати з webhook.
        await bot.delete_webhook(drop_pending_updates=False)

        try:
            await asyncio.wait_for(
                bot.set_my_commands([
                    BotCommand(command="start", description="Головне меню"),
                    BotCommand(command="help", description="Допомога"),
                    BotCommand(command="imagine", description="Створити фото"),
                    BotCommand(command="profile", description="Мій профіль"),
                    BotCommand(command="services", description="Купити генерації"),
                    BotCommand(command="terms", description="Умови використання"),
                    BotCommand(command="paysupport", description="Допомога з оплатою"),
                    BotCommand(command="cancel", description="Скасувати дію"),
                    BotCommand(command="id", description="Мій Telegram ID"),
                    BotCommand(command="admin", description="Адміністратор"),
                ]),
                timeout=20,
            )
            log.info("TELEGRAM COMMANDS REGISTERED")
        except Exception:
            log.exception("Could not register Telegram commands")

        log.info("STARTING TELEGRAM POLLING")

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
    except Exception:
        log.exception("BOT TERMINATED WITH ERROR")
        raise
