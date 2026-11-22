"""
Star Sale — Telegram-бот: автоматическая продажа звёзд (Telegram Stars) за RUB или GRAM.

Флоу:
  /start → «⭐ Продать звезды» → курс + [Продать в RUB] [Продать в GRAM]
  → реквизиты (карта / адрес кошелька) → количество звёзд
  → счёт на оплату в Stars (XTR) → после оплаты админам приходит лог
    с реквизитами и суммой выплаты + кнопка «✅ Выплачено».

Картинки к сообщениям лежат в папке images/ (см. images/README.txt).
"""

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing, suppress
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from html import escape
from pathlib import Path

import aiogram
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # на хостинге переменные обычно задаются в панели, .env не нужен
    pass

# ───────────────────────────── Настройки ─────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN (токен от @BotFather). Добавьте её в настройках хостинга.")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DB_PATH = os.getenv("DB_PATH", "orders.db")

MIN_STARS = int(os.getenv("MIN_STARS", "50"))
MAX_STARS = int(os.getenv("MAX_STARS", "10000"))  # лимит Telegram на один счёт в Stars

CURRENCIES = {
    "RUB": {
        "rate": Decimal("1.2"),          # 1 звезда = 1.2 RUB
        "exp": Decimal("0.01"),
        "requisites_label": "карта",
    },
    "GRAM": {
        "rate": Decimal("0.016"),        # 1 звезда = 0.016 GRAM
        "exp": Decimal("0.001"),
        "requisites_label": "кошелёк",
    },
}

WELCOME_TEXT = (
    "👋 Добро пожаловать в Star Sale - сервис для автоматической продажи звезд.\n\n"
    "Здесь можно продать свои звёзды за RUB или GRAM."
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stars_bot")
router = Router()


class Sell(StatesGroup):
    requisites = State()
    amount = State()


# ───────────────────────────── Картинки ─────────────────────────────
# Для каждого экрана бот ищет файл images/<ключ>.(jpg|jpeg|png|webp).
# Если файла нет — сообщение уходит обычным текстом.
#
# Ключи: welcome, rates, card, wallet, amount, invoice, success, done, refund
# Для card и wallet, если нет своих файлов, используется общая картинка requisites.

IMAGES_DIR = Path(__file__).parent / "images"
IMAGE_EXTS = ("jpg", "jpeg", "png", "webp")
IMAGE_FALLBACKS = {"card": "requisites", "wallet": "requisites"}
_file_ids: dict[str, str] = {}  # кеш file_id: файл загружается в Telegram один раз


def photo_for(key: str):
    if key in _file_ids:
        return _file_ids[key]
    names = [key] + ([IMAGE_FALLBACKS[key]] if key in IMAGE_FALLBACKS else [])
    for name in names:
        for ext in IMAGE_EXTS:
            path = IMAGES_DIR / f"{name}.{ext}"
            if path.exists():
                return FSInputFile(path)
    return None


async def send_screen(bot: Bot, chat_id: int, key: str, text: str, markup=None) -> None:
    """Отправляет сообщение с картинкой <key> и подписью text (или просто текст, если картинки нет)."""
    photo = photo_for(key)
    if photo is not None:
        try:
            msg = await bot.send_photo(chat_id, photo, caption=text, reply_markup=markup)
            if key not in _file_ids and msg.photo:
                _file_ids[key] = msg.photo[-1].file_id
            return
        except TelegramBadRequest:
            log.exception("Не удалось отправить картинку '%s', отправляю текстом", key)
            _file_ids.pop(key, None)
    await bot.send_message(chat_id, text, reply_markup=markup)


async def edit_screen(call: CallbackQuery, bot: Bot, key: str, text: str, markup=None) -> None:
    """Заменяет сообщение, под которым нажали кнопку, на новый экран (с картинкой <key>)."""
    msg = call.message
    photo = photo_for(key)
    try:
        if photo is not None and msg.photo:
            res = await msg.edit_media(InputMediaPhoto(media=photo, caption=text), reply_markup=markup)
            if key not in _file_ids and isinstance(res, Message) and res.photo:
                _file_ids[key] = res.photo[-1].file_id
            return
        if photo is None and not msg.photo:
            await msg.edit_text(text, reply_markup=markup)
            return
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        log.warning("edit_screen('%s') не удалось, пересоздаю сообщение: %s", key, e)
        _file_ids.pop(key, None)

    # Тип сообщения не совпал (текст ↔ фото) — удаляем старое и отправляем новое
    with suppress(TelegramBadRequest):
        await msg.delete()
    await send_screen(bot, msg.chat.id, key, text, markup)


# ───────────────────────────── База данных ─────────────────────────────

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                username    TEXT,
                full_name   TEXT,
                currency    TEXT NOT NULL,
                requisites  TEXT NOT NULL,
                stars       INTEGER NOT NULL,
                payout      TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',  -- pending / paid / done / refunded
                charge_id   TEXT,
                created_at  TEXT NOT NULL
            )
            """
        )


def create_order(user, currency: str, requisites: str, stars: int, payout: Decimal) -> int:
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO orders (user_id, username, full_name, currency, requisites, stars, payout, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user.id,
                user.username,
                user.full_name,
                currency,
                requisites,
                stars,
                f"{payout:f}",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        return cur.lastrowid


def get_order(order_id: int):
    with closing(db()) as conn:
        return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def set_status(order_id: int, new_status: str, expected: str, charge_id: str | None = None) -> bool:
    """Меняет статус, только если текущий == expected. Возвращает True, если обновили."""
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "UPDATE orders SET status = ?, charge_id = COALESCE(?, charge_id) WHERE id = ? AND status = ?",
            (new_status, charge_id, order_id, expected),
        )
        return cur.rowcount == 1


# ───────────────────────────── Валидация ─────────────────────────────

def parse_card(text: str) -> str | None:
    """Принимает номер карты в любом виде: с пробелами, дефисами, скрытыми символами при копировании.
    Проверяем только длину (16–19 цифр), без проверки по алгоритму Луна."""
    digits = re.sub(r"[^0-9]", "", text)
    if 16 <= len(digits) <= 19:
        return digits
    return None


# Адрес в формате TON: user-friendly (48 символов base64url) или raw (0:hex64).
# Если у GRAM другой формат адреса — поправьте регулярки.
_WALLET_FRIENDLY = re.compile(r"[A-Za-z0-9_-]{48}")
_WALLET_RAW = re.compile(r"-?[0-9]:[0-9a-fA-F]{64}")


def parse_wallet(text: str) -> str | None:
    t = text.strip()
    if _WALLET_FRIENDLY.fullmatch(t) or _WALLET_RAW.fullmatch(t):
        return t
    return None


def fmt_card(card: str) -> str:
    return " ".join(card[i : i + 4] for i in range(0, len(card), 4))


# ───────────────────────────── Клавиатуры и тексты ─────────────────────────────

def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


MAIN_MENU = kb([("⭐ Продать звезды", "sell")])
CANCEL_KB = kb([("✖️ Отмена", "menu")])
SELL_MENU = kb(
    [("Продать в RUB", "sell:RUB"), ("Продать в GRAM", "sell:GRAM")],
    [("⬅️ Назад", "menu")],
)


def rates_text() -> str:
    return (
        "💱 <b>Курс продажи</b>\n\n"
        f"1 ⭐ = {CURRENCIES['RUB']['rate']} RUB\n"
        f"1 ⭐ = {CURRENCIES['GRAM']['rate']} GRAM\n\n"
        "Выберите, в чём хотите получить выплату:"
    )


CARD_PROMPT = "💳 Введите номер банковской карты, на которую придёт выплата:"
WALLET_PROMPT = "👛 Введите адрес вашего GRAM-кошелька, на который придёт выплата:"


def amount_prompt() -> str:
    return f"⭐ Сколько звёзд хотите продать?\nМинимум: {MIN_STARS}, максимум: {MAX_STARS}."


# ───────────────────────────── Меню ─────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await send_screen(bot, message.chat.id, "welcome", WELCOME_TEXT, MAIN_MENU)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await send_screen(bot, message.chat.id, "welcome", "Отменено.\n\n" + WELCOME_TEXT, MAIN_MENU)


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await edit_screen(call, bot, "welcome", WELCOME_TEXT, MAIN_MENU)
    await call.answer()


@router.callback_query(F.data == "sell")
async def cb_sell(call: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await edit_screen(call, bot, "rates", rates_text(), SELL_MENU)
    await call.answer()


# ───────────────────────────── Продажа ─────────────────────────────

@router.callback_query(F.data.in_({"sell:RUB", "sell:GRAM"}))
async def cb_choose_currency(call: CallbackQuery, state: FSMContext, bot: Bot):
    currency = call.data.split(":")[1]
    await state.set_state(Sell.requisites)
    await state.update_data(currency=currency)

    if currency == "RUB":
        await edit_screen(call, bot, "card", CARD_PROMPT, CANCEL_KB)
    else:
        await edit_screen(call, bot, "wallet", WALLET_PROMPT, CANCEL_KB)
    await call.answer()


@router.message(Sell.requisites, F.text)
async def got_requisites(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    currency = data["currency"]

    if currency == "RUB":
        value = parse_card(message.text)
        if not value:
            await send_screen(
                bot, message.chat.id, "card",
                "❌ Не похоже на номер карты (нужно 16–19 цифр). Попробуйте ещё раз:",
                CANCEL_KB,
            )
            return
    else:
        value = parse_wallet(message.text)
        if not value:
            await send_screen(
                bot, message.chat.id, "wallet",
                "❌ Не похоже на адрес кошелька. Проверьте и отправьте ещё раз:",
                CANCEL_KB,
            )
            return

    await state.update_data(requisites=value)
    await state.set_state(Sell.amount)
    await send_screen(bot, message.chat.id, "amount", amount_prompt(), CANCEL_KB)


@router.message(Sell.requisites)
@router.message(Sell.amount, ~F.text)
async def not_text(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if await state.get_state() == Sell.requisites.state:
        key = "card" if data.get("currency") == "RUB" else "wallet"
    else:
        key = "amount"
    await send_screen(bot, message.chat.id, key, "Пожалуйста, отправьте ответ текстом.", CANCEL_KB)


@router.message(Sell.amount, F.text)
async def got_amount(message: Message, state: FSMContext, bot: Bot):
    text = message.text.strip()
    if not re.fullmatch(r"[0-9]+", text):
        await send_screen(bot, message.chat.id, "amount", "Введите целое число звёзд, например: 100", CANCEL_KB)
        return

    stars = int(text)
    if not MIN_STARS <= stars <= MAX_STARS:
        await send_screen(
            bot, message.chat.id, "amount",
            f"Количество звёзд должно быть от {MIN_STARS} до {MAX_STARS}.",
            CANCEL_KB,
        )
        return

    data = await state.get_data()
    currency = data["currency"]
    cfg = CURRENCIES[currency]
    payout = (Decimal(stars) * cfg["rate"]).quantize(cfg["exp"], rounding=ROUND_DOWN)

    order_id = create_order(message.from_user, currency, data["requisites"], stars, payout)
    await state.clear()

    if photo_for("invoice") is not None:  # без картинки это сообщение лишнее: всё то же есть в самом счёте
        await send_screen(
            bot, message.chat.id, "invoice",
            f"🧾 <b>Заявка #{order_id}</b>\n"
            f"⭐ Продаёте: {stars}\n"
            f"💸 Получите: <b>{payout:f} {currency}</b>\n\n"
            "Оплатите счёт ниже 👇",
        )
    await bot.send_invoice(
        chat_id=message.chat.id,
        title=f"Продажа {stars} ⭐",
        description=f"Вы продаёте {stars} звёзд и получаете {payout:f} {currency}. Заявка #{order_id}.",
        payload=str(order_id),
        currency="XTR",
        prices=[LabeledPrice(label=f"{stars} ⭐", amount=stars)],
    )


# ───────────────────────────── Оплата ─────────────────────────────

@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    order = None
    try:
        order = get_order(int(query.invoice_payload))
    except ValueError:
        pass

    ok = bool(
        order
        and order["status"] == "pending"
        and order["user_id"] == query.from_user.id
        and order["stars"] == query.total_amount
        and query.currency == "XTR"
    )
    await query.answer(
        ok=ok,
        error_message=None if ok else "Заявка не найдена или уже оплачена. Создайте новую.",
    )


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot):
    sp = message.successful_payment
    order_id = int(sp.invoice_payload)

    if not set_status(order_id, "paid", expected="pending", charge_id=sp.telegram_payment_charge_id):
        log.warning("Duplicate/unknown payment for order %s", order_id)
        return

    order = get_order(order_id)
    await send_screen(
        bot, message.chat.id, "success",
        f"✅ Оплата получена! Заявка #{order_id} принята.\n"
        f"Выплата: <b>{order['payout']} {order['currency']}</b>. Мы переведём средства в ближайшее время.",
        MAIN_MENU,
    )

    # Лог для администраторов (без картинки — это служебное сообщение)
    label = CURRENCIES[order["currency"]]["requisites_label"]
    requisites = order["requisites"]
    if order["currency"] == "RUB":
        requisites = fmt_card(requisites)

    username = f"@{escape(order['username'])}" if order["username"] else "—"
    admin_text = (
        f"🆕 <b>Оплаченная заявка #{order_id}</b>\n\n"
        f"👤 {escape(order['full_name'] or '')} ({username}, <code>{order['user_id']}</code>)\n"
        f"⭐ Звёзд: <b>{order['stars']}</b>\n"
        f"💸 К выплате: <b>{order['payout']} {order['currency']}</b>\n"
        f"🏦 Реквизиты ({label}): <code>{escape(requisites)}</code>\n"
        f"🧾 charge_id: <code>{escape(sp.telegram_payment_charge_id)}</code>"
    )
    markup = kb([("✅ Выплачено", f"done:{order_id}")])

    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty, order %s was not sent to admins:\n%s", order_id, admin_text)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text, reply_markup=markup)
        except Exception:
            log.exception("Failed to notify admin %s", admin_id)


# ───────────────────────────── Админ ─────────────────────────────

@router.callback_query(F.data.startswith("done:"))
async def admin_done(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа", show_alert=True)
        return

    order_id = int(call.data.split(":")[1])
    if not set_status(order_id, "done", expected="paid"):
        await call.answer("Заявка уже обработана", show_alert=True)
        return

    order = get_order(order_id)
    await call.message.edit_text(call.message.html_text + "\n\n✅ <b>Выплачено</b>", reply_markup=None)
    await call.answer("Отмечено как выплаченное")
    try:
        await send_screen(
            bot, order["user_id"], "done",
            f"💸 Выплата по заявке #{order_id} ({order['payout']} {order['currency']}) отправлена!",
        )
    except Exception:
        log.exception("Failed to notify user %s", order["user_id"])


@router.message(Command("refund"))
async def admin_refund(message: Message, bot: Bot):
    """/refund <id заявки> — вернуть звёзды пользователю (только для оплаченных, не выплаченных заявок)."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /refund <id заявки>")
        return

    order = get_order(int(parts[1]))
    if not order or order["status"] != "paid":
        await message.answer("Заявка не найдена или не в статусе «оплачена».")
        return

    try:
        await bot.refund_star_payment(user_id=order["user_id"], telegram_payment_charge_id=order["charge_id"])
    except Exception as e:
        await message.answer(f"Ошибка возврата: {escape(str(e))}")
        return

    set_status(order["id"], "refunded", expected="paid")
    await message.answer(f"↩️ Звёзды по заявке #{order['id']} возвращены.")
    try:
        await send_screen(bot, order["user_id"], "refund", f"↩️ Заявка #{order['id']} отменена, звёзды возвращены.")
    except Exception:
        pass


# ───────────────────────────── Запуск ─────────────────────────────

async def main():
    log.info("aiogram %s", aiogram.__version__)
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
