import asyncio
import base64
import html
import io
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from PIL import Image, ImageOps, UnidentifiedImageError

# ==================== SETTINGS ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
CLOUDFLARE_MODEL = os.getenv(
    "CLOUDFLARE_MODEL", "@cf/black-forest-labs/flux-2-klein-4b"
).strip()
FREE_DAILY_LIMIT = 5
PRICE_PER_GENERATION_STARS = max(1, int(os.getenv("PRICE_PER_GENERATION_STARS", "3")))
DB_PATH = os.getenv("DATABASE_PATH", "marketphoto.db")
REQUEST_TIMEOUT_SECONDS = 180
MAX_TELEGRAM_DOWNLOAD_BYTES = 10 * 1024 * 1024
INPUT_IMAGE_MAX_SIDE = 511  # Cloudflare's FLUX.2 Klein input images must be under 512x512.

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)
log = logging.getLogger("marketphoto")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable")
if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
    raise RuntimeError("Missing CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN")
if ADMIN_ID <= 0:
    raise RuntimeError("Missing/invalid ADMIN_ID environment variable")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
http_session: Optional[aiohttp.ClientSession] = None
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db_lock = asyncio.Lock()
user_locks: dict[int, asyncio.Lock] = {}

# Requests that explicitly ask to undress or expose a real person are rejected.
# Clothing replacement, fashion restyling, background changes and other ordinary edits remain available.
UNDRESS_PATTERN = re.compile(
    r"(раздень|раздев(и|ать|ание)|снять\s+одежд|убрать\s+одежд|без\s+одежд|"
    r"гол(ая|ый|ое|ую|ым|ыми)\b|обнаж(ить|ённ|енн|ить)|нюдс|nude\b|naked\b|"
    r"undress|remove\s+(her|his|their|the)\s+clothes|remove\s+clothing|take\s+off\s+(her|his|their)\s+clothes|"
    r"make\s+(her|him|them)\s+naked|topless\s+edit)",
    re.IGNORECASE,
)


def safe_html(value: object) -> str:
    return html.escape(str(value), quote=False)


def utc_day() -> str:
    # The daily quota follows the user's local calendar day in Ukraine.
    return datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()


def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]


def init_db() -> None:
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, day)
        );
        CREATE TABLE IF NOT EXISTS orders (
            payload TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            prompt TEXT NOT NULL,
            kind TEXT NOT NULL,
            image_bytes BLOB,
            price_stars INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS terms_acceptance (
            user_id INTEGER PRIMARY KEY,
            accepted_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments (
            charge_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            payload TEXT NOT NULL,
            stars INTEGER NOT NULL,
            refunded INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            prompt TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            photo_file_id TEXT,
            stars_paid INTEGER NOT NULL DEFAULT 0,
            charge_id TEXT,
            complaint_status TEXT NOT NULL DEFAULT 'none',
            refunded INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generation_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            admin_chat_id INTEGER,
            admin_message_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS pending_edits (
            user_id INTEGER PRIMARY KEY,
            image_bytes BLOB NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_modes (
            user_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        """
    )
    db.commit()


def register_user(user_id: int, username: Optional[str]) -> None:
    db.execute(
        "INSERT INTO users(user_id, username, created_at) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
        (user_id, username, int(time.time())),
    )
    db.commit()


def usage_count(user_id: int) -> int:
    row = db.execute(
        "SELECT count FROM daily_usage WHERE user_id=? AND day=?",
        (user_id, utc_day()),
    ).fetchone()
    return int(row["count"]) if row else 0


def increment_usage(user_id: int) -> None:
    db.execute(
        "INSERT INTO daily_usage(user_id, day, count) VALUES(?,?,1) "
        "ON CONFLICT(user_id, day) DO UPDATE SET count=count+1",
        (user_id, utc_day()),
    )
    db.commit()


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎨 Создать изображение", callback_data="mode_generate")],
            [InlineKeyboardButton(text="👤 Создать аватарку", callback_data="mode_avatar")],
            [InlineKeyboardButton(text="🖥 Обои для телефона/ПК", callback_data="mode_wallpaper")],
            [InlineKeyboardButton(text="🖼 Редактировать фото", callback_data="mode_edit")],
            [InlineKeyboardButton(text="📊 Мой лимит", callback_data="my_limit")],
            [InlineKeyboardButton(text="⭐ Цена и оплата", callback_data="pricing")],
        ]
    )


def defect_keyboard(generation_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Брак", callback_data=f"defect:{generation_id}")]
        ]
    )


def admin_complaint_keyboard(complaint_id: int, can_refund: bool, accepted: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if not accepted:
        rows.append([InlineKeyboardButton(text="✅ Подтвердить брак", callback_data=f"admin_accept:{complaint_id}")])
        rows.append([InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject:{complaint_id}")])
    if can_refund:
        rows.append([InlineKeyboardButton(text="⭐ Вернуть Stars", callback_data=f"admin_refund:{complaint_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==================== CLOUDFLARE ====================
def prepare_input_image(raw: bytes) -> bytes:
    """Convert the uploaded reference image to PNG and fit below 512x512."""
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((INPUT_IMAGE_MAX_SIDE, INPUT_IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Не вдалося прочитати фото. Надішли звичайне JPG або PNG-зображення.") from exc


async def cloudflare_generate(prompt: str, image_bytes: Optional[bytes] = None, kind: str = "text") -> bytes:
    """FLUX.2 Klein REST API requires multipart form-data, even for text-only generation."""
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        )

    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CLOUDFLARE_ACCOUNT_ID}/ai/run/{CLOUDFLARE_MODEL}"
    )
    form = aiohttp.FormData()
    form.add_field("prompt", prompt)
    # FLUX.2 Klein dimensions are sent as multipart text fields.
    if kind == "avatar":
        width, height = 1024, 1024
    elif kind == "wallpaper":
        width, height = 768, 1365
    else:
        width, height = 1024, 768
    form.add_field("width", str(width))
    form.add_field("height", str(height))
    if image_bytes:
        prepared = prepare_input_image(image_bytes)
        form.add_field(
            "input_image_0", prepared,
            filename="reference.png", content_type="image/png"
        )

    headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"}
    async with http_session.post(url, data=form, headers=headers) as response:
        content_type = response.headers.get("Content-Type", "")
        raw = await response.read()
        if response.status >= 400:
            detail = raw.decode("utf-8", errors="replace")[:1500]
            log.error("Cloudflare HTTP %s: %s", response.status, detail)
            raise RuntimeError(f"Cloudflare HTTP {response.status}: {detail}")

        if content_type.startswith("image/"):
            return raw
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.error("Unexpected Cloudflare response (first 600 bytes): %r", raw[:600])
            raise RuntimeError("Cloudflare повернув невідомий формат відповіді") from exc

        if body.get("success") is False or body.get("errors"):
            detail = json.dumps(body.get("errors") or body.get("messages") or body, ensure_ascii=False)[:1500]
            log.error("Cloudflare rejected request: %s", detail)
            raise RuntimeError(f"Cloudflare відхилив запит: {detail}")

        result = body.get("result", {})
        image_b64 = result.get("image") if isinstance(result, dict) else None
        if image_b64:
            try:
                return base64.b64decode(image_b64)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Cloudflare повернув некоректні дані зображення") from exc

        log.error("Cloudflare response has no image field; keys=%s", list(body.keys()))
        raise RuntimeError("У відповіді Cloudflare немає поля image")


# ==================== GENERATION AND BILLING ====================
async def send_order_invoice(chat_id: int, payload: str) -> None:
    order = db.execute("SELECT * FROM orders WHERE payload=?", (payload,)).fetchone()
    if not order or order["status"] != "pending":
        return
    try:
        await bot.send_invoice(
            chat_id=chat_id,
            title="MarketPhoto AI — генерація",
            description="Одна генерація або одне редагування фото",
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(label="Одна генерація", amount=int(order["price_stars"]))],
            # Telegram Stars digital-service invoices do not need provider_token.
        )
    except Exception:
        db.execute("UPDATE orders SET status='invoice_error' WHERE payload=?", (payload,))
        db.commit()
        log.exception("Could not send Stars invoice")
        await bot.send_message(chat_id, "Не вдалося створити рахунок Telegram Stars. Перевір налаштування бота.")


async def create_invoice(message: Message, prompt: str, kind: str, image_bytes: Optional[bytes]) -> None:
    uid = message.from_user.id
    payload = f"gen:{uid}:{uuid.uuid4().hex}"
    terms = db.execute("SELECT 1 FROM terms_acceptance WHERE user_id=?", (uid,)).fetchone()
    status = "pending" if terms else "awaiting_terms"
    db.execute(
        "INSERT INTO orders(payload,user_id,prompt,kind,image_bytes,price_stars,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (payload, uid, prompt, kind, image_bytes, PRICE_PER_GENERATION_STARS, status, int(time.time())),
    )
    db.commit()
    if terms:
        await send_order_invoice(message.chat.id, payload)
    else:
        short_payload = payload
        await message.answer(
            "📃 <b>Умови користування MarketPhoto AI</b>\n\n"
            "1. Оплата стосується однієї генерації або одного редагування.\n"
            "2. Якщо оплачена генерація технічно не вдається, бот спробує автоматично повернути Stars.\n"
            "3. Для скарги на якість натисни «Брак» під результатом; адміністратор перевірить її вручну.\n"
            "4. Не завантажуй чужі фото без дозволу та не використовуй бот для створення оголених зображень реальних людей.\n"
            "5. Питання щодо платежів: /paysupport.\n\n"
            "Натискаючи «Згоден», ти підтверджуєш, що прочитав умови.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Згоден з умовами", callback_data=f"terms_accept:{short_payload}")],
                [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"terms_decline:{short_payload}")],
            ]),
        )


async def generate_and_send(
    message: Message,
    prompt: str,
    kind: str,
    image_bytes: Optional[bytes] = None,
    charge_id: Optional[str] = None,
    stars_paid: int = 0,
    consume_free: bool = False,
) -> bool:
    uid = message.from_user.id
    progress = await message.answer("⏳ Генерую зображення…")
    try:
        image_data = await cloudflare_generate(prompt, image_bytes, kind)
        if len(image_data) < 100:
            raise RuntimeError("Cloudflare повернув порожнє зображення")
        sent_photo = await message.answer_photo(
            BufferedInputFile(image_data, filename="marketphoto.png"),
            caption="✨ Готово! Якщо зображення має дефект, натисни «Брак».",
        )
        cursor = db.execute(
            "INSERT INTO generations(user_id,chat_id,prompt,kind,created_at,photo_file_id,stars_paid,charge_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uid, message.chat.id, prompt, kind, int(time.time()), sent_photo.photo[-1].file_id, stars_paid, charge_id),
        )
        generation_id = int(cursor.lastrowid)
        db.commit()
        await sent_photo.edit_reply_markup(reply_markup=defect_keyboard(generation_id))
        if consume_free:
            increment_usage(uid)
        left = "∞" if uid == ADMIN_ID else str(max(0, FREE_DAILY_LIMIT - usage_count(uid)))
        try:
            await progress.delete()
        except Exception:
            pass
        await message.answer(f"Готово! Безкоштовних генерацій залишилося сьогодні: <b>{left}</b>")
        return True
    except Exception as exc:
        log.exception("Generation failed, user_id=%s, kind=%s", uid, kind)
        try:
            await progress.edit_text(
                "❌ Не вдалося створити зображення.\n"
                "Безкоштовна спроба не списана. Адміністратор може перевірити Render Logs."
            )
        except Exception:
            pass
        if charge_id:
            # A paid request that failed to deliver an image is automatically refunded when possible.
            try:
                await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge_id)
                db.execute("UPDATE payments SET refunded=1 WHERE charge_id=?", (charge_id,))
                db.commit()
                await message.answer(f"⭐ Оскільки генерація не вдалася, {stars_paid} Stars автоматично повернуто.")
            except Exception:
                log.exception("Automatic refund failed for charge_id=%s", charge_id)
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ Генерація після оплати не вдалася, автоматичне повернення теж не виконалося.\n"
                    f"User ID: <code>{uid}</code>\nCharge ID: <code>{safe_html(charge_id)}</code>\n"
                    f"Stars: {stars_paid}\nПомилка: <code>{safe_html(str(exc)[:500])}</code>",
                )
        return False


async def start_generation(message: Message, prompt: str, kind: str, image_bytes: Optional[bytes] = None) -> None:
    uid = message.from_user.id
    if UNDRESS_PATTERN.search(prompt):
        await message.answer(
            "Я не редагую фото реальних людей, знімаючи з них одяг. "
            "Можу натомість змінити одяг на інший фасон, колір чи стиль, створити новий образ або змінити фон."
        )
        return

    async with get_user_lock(uid):
        if uid == ADMIN_ID:
            await generate_and_send(message, prompt, kind, image_bytes, consume_free=False)
            return

        if usage_count(uid) < FREE_DAILY_LIMIT:
            await generate_and_send(message, prompt, kind, image_bytes, consume_free=True)
        else:
            await create_invoice(message, prompt, kind, image_bytes)


# ==================== MENUS AND COMMANDS ====================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    register_user(message.from_user.id, message.from_user.username)
    admin_text = "\n👑 Для тебе як адміністратора генерації безлімітні." if message.from_user.id == ADMIN_ID else ""
    await message.answer(
        "👋 <b>MarketPhoto AI</b>\n\n"
        "🎨 Створюй зображення за описом.\n"
        "🖼 Надсилай фото й описуй, що змінити.\n"
        f"🎁 Кожному користувачу доступно {FREE_DAILY_LIMIT} безкоштовних генерацій на день.\n"
        f"⭐ Далі — {PRICE_PER_GENERATION_STARS} Stars за одну генерацію.\n"
        "📩 Під кожним результатом є кнопка «Брак» для перевірки адміністратором."
        f"{admin_text}\n\n"
        "Редагування одягу й образу доступне; роздягання реальних людей — ні.",
        reply_markup=home_keyboard(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Як користуватися:\n"
        "• Напиши опис — створиться нове зображення.\n"
        "• Для редагування спершу надішли фото, потім окремим повідомленням напиши зміни.\n"
        "• Щодня є 5 безкоштовних генерацій; після ліміту оплата за одну генерацію — Telegram Stars.\n"
        "• Якщо результат зіпсований, натисни «Брак».\n"
        "Для питань щодо оплати використай /paysupport."
    )


@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    await message.answer(f"Підтримка з питань оплати: напиши адміністратору {ADMIN_ID} та вкажи свій Telegram ID {message.from_user.id}.")


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Ця команда доступна лише адміністратору.")
        return
    total_users = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    open_complaints = db.execute("SELECT COUNT(*) AS n FROM complaints WHERE status IN ('pending','accepted')").fetchone()["n"]
    await message.answer(
        f"👑 <b>Адмін-панель</b>\nКористувачів: {total_users}\nСкарг, що потребують уваги: {open_complaints}\n"
        "Твої генерації безлімітні та безкоштовні. Скарги надходять сюди автоматично."
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    db.execute("DELETE FROM pending_edits WHERE user_id=?", (message.from_user.id,))
    db.execute("DELETE FROM pending_modes WHERE user_id=?", (message.from_user.id,))
    db.commit()
    await message.answer("Редагування скасовано. Надішли новий опис або фото.", reply_markup=home_keyboard())


@dp.callback_query(F.data == "mode_generate")
async def cb_generate(call: CallbackQuery):
    await call.answer()
    db.execute(
        "INSERT INTO pending_modes(user_id,mode,created_at) VALUES(?, 'text', ?) "
        "ON CONFLICT(user_id) DO UPDATE SET mode='text',created_at=excluded.created_at",
        (call.from_user.id, int(time.time())),
    )
    db.commit()
    await call.message.answer("Напиши опис зображення, яке треба створити.")


@dp.callback_query(F.data == "mode_avatar")
async def cb_avatar(call: CallbackQuery):
    await call.answer()
    db.execute(
        "INSERT INTO pending_modes(user_id,mode,created_at) VALUES(?, 'avatar', ?) "
        "ON CONFLICT(user_id) DO UPDATE SET mode='avatar',created_at=excluded.created_at",
        (call.from_user.id, int(time.time())),
    )
    db.commit()
    await call.message.answer("Опиши аватарку: персонаж, кольори та стиль. Я зроблю квадратне зображення 1:1.")


@dp.callback_query(F.data == "mode_wallpaper")
async def cb_wallpaper(call: CallbackQuery):
    await call.answer()
    db.execute(
        "INSERT INTO pending_modes(user_id,mode,created_at) VALUES(?, 'wallpaper', ?) "
        "ON CONFLICT(user_id) DO UPDATE SET mode='wallpaper',created_at=excluded.created_at",
        (call.from_user.id, int(time.time())),
    )
    db.commit()
    await call.message.answer("Опиши шпалери: тему, кольори й настрій. Я зроблю вертикальні шпалери для телефона.")


@dp.callback_query(F.data == "mode_edit")
async def cb_edit(call: CallbackQuery):
    await call.answer()
    await call.message.answer("Надішли фото, яке треба відредагувати. Потім окремо напиши, що змінити.")


@dp.callback_query(F.data == "my_limit")
async def cb_limit(call: CallbackQuery):
    await call.answer()
    if call.from_user.id == ADMIN_ID:
        await call.message.answer("Твій ліміт: <b>∞ безкоштовних генерацій</b>.")
    else:
        used = usage_count(call.from_user.id)
        await call.message.answer(
            f"Використано сьогодні: <b>{used}/{FREE_DAILY_LIMIT}</b>\n"
            f"Залишилося: <b>{max(0, FREE_DAILY_LIMIT - used)}</b>."
        )


@dp.callback_query(F.data == "pricing")
async def cb_pricing(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        f"🎁 Перші {FREE_DAILY_LIMIT} успішних генерацій на день безкоштовні.\n"
        f"⭐ Після ліміту одна генерація або редагування коштує {PRICE_PER_GENERATION_STARS} Stars.\n"
        "Коли надішлеш наступний запит, бот покаже рахунок для оплати."
    )


@dp.message(F.photo)
async def receive_photo(message: Message):
    register_user(message.from_user.id, message.from_user.username)
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    buffer = io.BytesIO()
    await bot.download_file(file_info.file_path, destination=buffer)
    image_bytes = buffer.getvalue()
    if len(image_bytes) > MAX_TELEGRAM_DOWNLOAD_BYTES:
        await message.answer("Фото завелике. Надішли зображення розміром до 10 МБ.")
        return
    try:
        prepare_input_image(image_bytes)
    except ValueError as exc:
        await message.answer(safe_html(exc))
        return
    db.execute(
        "INSERT INTO pending_edits(user_id,image_bytes,created_at) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET image_bytes=excluded.image_bytes,created_at=excluded.created_at",
        (message.from_user.id, image_bytes, int(time.time())),
    )
    db.commit()
    await message.answer(
        "📷 Фото отримано. Тепер напиши, що змінити: наприклад, фон, освітлення, зачіску, "
        "колір або фасон одягу, чи художній стиль."
    )


@dp.message(F.text)
async def receive_text(message: Message):
    if message.text.startswith("/"):
        return
    register_user(message.from_user.id, message.from_user.username)
    prompt = message.text.strip()
    if not prompt:
        return
    row = db.execute("SELECT image_bytes FROM pending_edits WHERE user_id=?", (message.from_user.id,)).fetchone()
    if row:
        image_bytes = row["image_bytes"]
        db.execute("DELETE FROM pending_edits WHERE user_id=?", (message.from_user.id,))
        db.commit()
        await start_generation(message, prompt, "edit", image_bytes)
    else:
        mode_row = db.execute("SELECT mode FROM pending_modes WHERE user_id=?", (message.from_user.id,)).fetchone()
        kind = mode_row["mode"] if mode_row else "text"
        db.execute("DELETE FROM pending_modes WHERE user_id=?", (message.from_user.id,))
        db.commit()
        if kind == "avatar":
            prompt = "Create a square 1:1 profile avatar, polished composition, centered subject, high quality. User's request: " + prompt
        elif kind == "wallpaper":
            prompt = "Create vertical smartphone wallpaper in 9:16 composition, clean subject placement, high quality. User's request: " + prompt
        await start_generation(message, prompt, kind)


@dp.message(Command("terms"))
async def cmd_terms(message: Message):
    await message.answer(
        "📃 <b>Умови MarketPhoto AI</b>\n\n"
        "Послуга надає генерацію та редагування зображень. Після 5 безкоштовних генерацій одна наступна генерація коштує "
        f"{PRICE_PER_GENERATION_STARS} Telegram Stars.\n"
        "Якщо оплачена генерація технічно не вдається, бот спробує автоматично повернути Stars. Для перевірки якості натисни «Брак» під фото. "
        "Не завантажуй чужі фото без дозволу та не використовуй бот для створення оголених зображень реальних людей.\n"
        "Питання оплати: /paysupport."
    )


@dp.callback_query(F.data.startswith("terms_accept:"))
async def cb_terms_accept(call: CallbackQuery):
    payload = call.data.split(":", 1)[1]
    order = db.execute("SELECT * FROM orders WHERE payload=?", (payload,)).fetchone()
    if not order or order["user_id"] != call.from_user.id or order["status"] != "awaiting_terms":
        await call.answer("Це замовлення вже неактивне.", show_alert=True)
        return
    db.execute(
        "INSERT INTO terms_acceptance(user_id,accepted_at) VALUES(?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET accepted_at=excluded.accepted_at",
        (call.from_user.id, int(time.time())),
    )
    db.execute("UPDATE orders SET status='pending' WHERE payload=?", (payload,))
    db.commit()
    await call.answer("Умови прийнято.")
    await call.message.edit_reply_markup(reply_markup=None)
    await send_order_invoice(call.message.chat.id, payload)


@dp.callback_query(F.data.startswith("terms_decline:"))
async def cb_terms_decline(call: CallbackQuery):
    payload = call.data.split(":", 1)[1]
    order = db.execute("SELECT * FROM orders WHERE payload=?", (payload,)).fetchone()
    if not order or order["user_id"] != call.from_user.id or order["status"] != "awaiting_terms":
        await call.answer("Це замовлення вже неактивне.", show_alert=True)
        return
    db.execute("UPDATE orders SET status='cancelled' WHERE payload=?", (payload,))
    db.commit()
    await call.answer("Замовлення скасовано.")
    await call.message.edit_text("Оплату скасовано. Можеш продовжити користування ботом без покупки.")


# ==================== STARS PAYMENTS ====================
@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    order = db.execute("SELECT * FROM orders WHERE payload=?", (pre_checkout_query.invoice_payload,)).fetchone()
    valid = (
        order is not None
        and order["status"] == "pending"
        and order["user_id"] == pre_checkout_query.from_user.id
        and pre_checkout_query.currency == "XTR"
        and pre_checkout_query.total_amount == order["price_stars"]
    )
    if not valid:
        await pre_checkout_query.answer(ok=False, error_message="Рахунок недійсний або застарів. Надішли запит повторно.")
        return
    await pre_checkout_query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    uid = message.from_user.id
    payload = payment.invoice_payload
    order = db.execute("SELECT * FROM orders WHERE payload=?", (payload,)).fetchone()
    charge_id = payment.telegram_payment_charge_id
    if not order or order["user_id"] != uid or order["status"] != "pending":
        log.error("Payment received with missing/invalid order. uid=%s payload=%s", uid, payload)
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ Оплату отримано, але замовлення не знайдено. User ID: {uid}; payload: <code>{safe_html(payload)}</code>; charge: <code>{safe_html(charge_id)}</code>"
        )
        await message.answer("Оплату отримано, але замовлення не вдалося відновити. Адміністратор перевірить платіж.")
        return

    db.execute(
        "INSERT OR IGNORE INTO payments(charge_id,user_id,payload,stars,refunded,created_at) VALUES(?,?,?,?,0,?)",
        (charge_id, uid, payload, payment.total_amount, int(time.time())),
    )
    db.execute("UPDATE orders SET status='paid' WHERE payload=?", (payload,))
    db.commit()
    await message.answer("⭐ Оплату отримано. Створюю зображення…")
    await generate_and_send(
        message,
        order["prompt"],
        order["kind"],
        order["image_bytes"],
        charge_id=charge_id,
        stars_paid=payment.total_amount,
        consume_free=False,
    )


# ==================== COMPLAINTS / REFUNDS ====================
@dp.callback_query(F.data.startswith("defect:"))
async def cb_defect(call: CallbackQuery):
    try:
        generation_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некоректний запит.", show_alert=True)
        return
    gen = db.execute("SELECT * FROM generations WHERE id=?", (generation_id,)).fetchone()
    if not gen or gen["user_id"] != call.from_user.id:
        await call.answer("Це зображення не знайдено або воно не твоє.", show_alert=True)
        return
    existing = db.execute("SELECT id,status FROM complaints WHERE generation_id=?", (generation_id,)).fetchone()
    if existing:
        await call.answer("Скаргу на це зображення вже відправлено.", show_alert=True)
        return

    reason = "Користувач натиснув кнопку «Брак»"
    cursor = db.execute(
        "INSERT INTO complaints(generation_id,user_id,reason,status,created_at) VALUES(?,?,?,'pending',?)",
        (generation_id, call.from_user.id, reason, int(time.time())),
    )
    complaint_id = int(cursor.lastrowid)
    db.execute("UPDATE generations SET complaint_status='pending' WHERE id=?", (generation_id,))
    db.commit()
    await call.answer("Скаргу надіслано адміністратору.")

    username = f"@{call.from_user.username}" if call.from_user.username else "без username"
    can_refund = bool(gen["charge_id"] and gen["stars_paid"] > 0 and not gen["refunded"])
    caption = (
        f"🚨 <b>Скарга на зображення #{generation_id}</b>\n"
        f"Скарга ID: <code>{complaint_id}</code>\n"
        f"Користувач: {safe_html(username)}\nUser ID: <code>{gen['user_id']}</code>\n"
        f"Тип: {safe_html(gen['kind'])}\nЗапит: {safe_html(gen['prompt'][:500])}\n"
        f"Оплата: {gen['stars_paid']} Stars\nПричина: {safe_html(reason)}"
    )
    keyboard = admin_complaint_keyboard(complaint_id, can_refund)
    try:
        admin_msg = await bot.send_photo(ADMIN_ID, gen["photo_file_id"], caption=caption, reply_markup=keyboard)
        db.execute(
            "UPDATE complaints SET admin_chat_id=?,admin_message_id=? WHERE id=?",
            (ADMIN_ID, admin_msg.message_id, complaint_id),
        )
        db.commit()
    except Exception:
        log.exception("Failed sending complaint to administrator")
        await call.message.answer("Скарга збережена, але бот не зміг надіслати її адміністратору. Перевір ADMIN_ID і логи.")


async def get_admin_complaint(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Лише адміністратор може виконувати цю дію.", show_alert=True)
        return None, None
    try:
        complaint_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некоректний ID скарги.", show_alert=True)
        return None, None
    complaint = db.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
    if not complaint:
        await call.answer("Скаргу не знайдено.", show_alert=True)
        return None, None
    generation = db.execute("SELECT * FROM generations WHERE id=?", (complaint["generation_id"],)).fetchone()
    return complaint, generation


@dp.callback_query(F.data.startswith("admin_accept:"))
async def cb_admin_accept(call: CallbackQuery):
    complaint, gen = await get_admin_complaint(call)
    if not complaint:
        return
    if complaint["status"] not in ("pending", "accepted"):
        await call.answer("Скаргу вже закрито.", show_alert=True)
        return
    db.execute("UPDATE complaints SET status='accepted' WHERE id=?", (complaint["id"],))
    db.execute("UPDATE generations SET complaint_status='accepted' WHERE id=?", (gen["id"],))
    db.commit()
    can_refund = bool(gen["charge_id"] and gen["stars_paid"] > 0 and not gen["refunded"])
    await call.message.edit_reply_markup(reply_markup=admin_complaint_keyboard(complaint["id"], can_refund, accepted=True))
    await bot.send_message(gen["user_id"], "✅ Адміністратор підтвердив, що результат має дефект. Дякуємо за скаргу.")
    await call.answer("Брак підтверджено. Якщо це платне фото, можеш окремо повернути Stars.")


@dp.callback_query(F.data.startswith("admin_reject:"))
async def cb_admin_reject(call: CallbackQuery):
    complaint, gen = await get_admin_complaint(call)
    if not complaint:
        return
    if complaint["status"] not in ("pending",):
        await call.answer("Скаргу вже оброблено.", show_alert=True)
        return
    db.execute("UPDATE complaints SET status='rejected' WHERE id=?", (complaint["id"],))
    db.execute("UPDATE generations SET complaint_status='rejected' WHERE id=?", (gen["id"],))
    db.commit()
    await call.message.edit_reply_markup(reply_markup=None)
    await bot.send_message(gen["user_id"], "❌ Адміністратор перевірив скаргу, але не підтвердив брак.")
    await call.answer("Скаргу відхилено.")


@dp.callback_query(F.data.startswith("admin_refund:"))
async def cb_admin_refund(call: CallbackQuery):
    complaint, gen = await get_admin_complaint(call)
    if not complaint:
        return
    if not gen or not gen["charge_id"] or gen["stars_paid"] < 1:
        await call.answer("Це зображення не було оплачено Stars.", show_alert=True)
        return
    payment = db.execute("SELECT * FROM payments WHERE charge_id=?", (gen["charge_id"],)).fetchone()
    if not payment or payment["refunded"] or gen["refunded"]:
        await call.answer("Платіж уже повернуто або його не знайдено.", show_alert=True)
        return
    try:
        await bot.refund_star_payment(user_id=gen["user_id"], telegram_payment_charge_id=gen["charge_id"])
    except Exception as exc:
        log.exception("Telegram Stars refund failed")
        await call.answer(f"Telegram не виконав повернення: {safe_html(str(exc)[:130])}", show_alert=True)
        return
    db.execute("UPDATE payments SET refunded=1 WHERE charge_id=?", (gen["charge_id"],))
    db.execute("UPDATE generations SET refunded=1,complaint_status='refunded' WHERE id=?", (gen["id"],))
    db.execute("UPDATE complaints SET status='refunded' WHERE id=?", (complaint["id"],))
    db.commit()
    await call.message.edit_reply_markup(reply_markup=None)
    await bot.send_message(gen["user_id"], f"⭐ Адміністратор повернув {gen['stars_paid']} Stars за зображення з браком.")
    await call.answer("Stars успішно повернуто.")


# ==================== START / STOP ====================
async def main():
    init_db()
    log.info("Starting MarketPhoto AI; model=%s; free_limit=%s; price=%s XTR", CLOUDFLARE_MODEL, FREE_DAILY_LIMIT, PRICE_PER_GENERATION_STARS)
    try:
        await dp.start_polling(bot)
    finally:
        if http_session and not http_session.closed:
            await http_session.close()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
