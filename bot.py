import asyncio
import json
import uuid
import bcrypt
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import asyncpg

with open("config.json", "r") as f:
    config = json.load(f)

BOT_TOKEN = config["bot_token"]
DB_URL = config["db_url"]
CHANNEL_ID = config["channel_id"]
CHANNEL_URL = config["channel_url"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
db_pool = None


class RegisterStates(StatesGroup):
    waiting_login = State()
    waiting_password = State()


class LoginStates(StatesGroup):
    waiting_login = State()
    waiting_password = State()


class AdminStates(StatesGroup):
    waiting_download_url = State()


class ActivateKeyStates(StatesGroup):
    waiting_key = State()


async def get_db():
    return db_pool


async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return False


async def get_session_user(telegram_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT u.* FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.telegram_id = $1",
            telegram_id
        )
        return row


async def is_admin(telegram_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM admins WHERE telegram_id = $1", telegram_id)
        return row is not None


async def ensure_admin_exists(telegram_id: int):
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM admins")
        if count == 0:
            await conn.execute("INSERT INTO admins (telegram_id) VALUES ($1)", telegram_id)


def main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Приобрести клиент", callback_data="buy")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")]
    ])


def subscribe_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подписаться", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✔️ Проверить", callback_data="check_sub")]
    ])


def auth_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Зарегистрироваться", callback_data="register")],
        [InlineKeyboardButton(text="Войти", callback_data="login")]
    ])


def back_keyboard(callback: str = "back_to_main"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=callback)]
    ])


def buy_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Купить за звезды ⭐", callback_data="buy_stars")],
        [InlineKeyboardButton(text="Купить за деньги 💸", url="https://t.me/FloraClientHelp")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
    ])


async def profile_keyboard(telegram_id: int):
    buttons = []
    user = await get_session_user(telegram_id)
    if user and user["subscription_active"]:
        async with db_pool.acquire() as conn:
            url_row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'download_url'")
            url = url_row["value"] if url_row else ""
        if url:
            buttons.append([InlineKeyboardButton(text="⬇️ Скачать клиент", url=url)])
    buttons.append([InlineKeyboardButton(text="🔑 Активировать ключ", callback_data="activate_key")])
    if await is_admin(telegram_id):
        buttons.append([InlineKeyboardButton(text="⚙️ Админ-Панель", callback_data="admin")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Создать ключ", callback_data="admin_create_key")],
        [InlineKeyboardButton(text="👥 Юзеры", callback_data="admin_users_0")],
        [InlineKeyboardButton(text="🔗 Ссылка на скачивание", callback_data="admin_set_url")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="profile")]
    ])


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await ensure_admin_exists(message.from_user.id)
    subscribed = await is_subscribed(message.from_user.id)
    if not subscribed:
        await message.answer(
            "Пожалуйста, для использования бота подпишитесь на канал ниже",
            reply_markup=subscribe_keyboard()
        )
        return
    await show_auth_or_main(message.from_user.id, message)


async def show_auth_or_main(telegram_id: int, target):
    user = await get_session_user(telegram_id)
    if user:
        text = (
            "Добро пожаловать в бота FloraClient.\n\n"
            "В данном боте вы можете приобрести подписку на данный клиент, "
            "подписка выдаётся автоматически при покупке за звезды, "
            "если вы хотите купить за деньги, то вам нужно написать поддержке — @FloraClientHelp."
        )
        if isinstance(target, Message):
            await target.answer(text, reply_markup=main_menu_keyboard())
        else:
            await target.message.edit_text(text, reply_markup=main_menu_keyboard())
    else:
        text = "Выберите действие"
        if isinstance(target, Message):
            await target.answer(text, reply_markup=auth_keyboard())
        else:
            await target.message.edit_text(text, reply_markup=auth_keyboard())


@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery, state: FSMContext):
    subscribed = await is_subscribed(callback.from_user.id)
    if not subscribed:
        await callback.answer("Вы ещё не подписались на канал!", show_alert=True)
        return
    await callback.message.delete()
    await show_auth_or_main(callback.from_user.id, callback)


@dp.callback_query(F.data == "register")
async def start_register(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RegisterStates.waiting_login)
    await callback.message.edit_text(
        "Впишите логин для регистрации",
        reply_markup=back_keyboard("back_to_auth")
    )


@dp.message(RegisterStates.waiting_login)
async def register_login(message: Message, state: FSMContext):
    login = message.text.strip()
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM users WHERE username = $1", login)
    if existing:
        await message.answer(
            "Данный логин уже занят, попытайтесь войти в аккаунт.",
            reply_markup=back_keyboard("back_to_auth")
        )
        await state.clear()
        return
    await state.update_data(login=login)
    await state.set_state(RegisterStates.waiting_password)
    await message.answer(
        "Введите пароль для аккаунта, после ввода ваш пароль удалится в чате‼️",
        reply_markup=back_keyboard("back_to_auth")
    )


@dp.message(RegisterStates.waiting_password)
async def register_password(message: Message, state: FSMContext):
    password = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    login = data.get("login")
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow(
            "INSERT INTO users (telegram_id, username, password_hash) VALUES ($1, $2, $3) RETURNING *",
            message.from_user.id, login, hashed
        )
        await conn.execute(
            "INSERT INTO sessions (telegram_id, user_id) VALUES ($1, $2) ON CONFLICT (telegram_id) DO UPDATE SET user_id = $2",
            message.from_user.id, user["id"]
        )
    await state.clear()
    text = (
        "Добро пожаловать в бота FloraClient.\n\n"
        "В данном боте вы можете приобрести подписку на данный клиент, "
        "подписка выдаётся автоматически при покупке за звезды, "
        "если вы хотите купить за деньги, то вам нужно написать поддержке — @FloraClientHelp."
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@dp.callback_query(F.data == "login")
async def start_login(callback: CallbackQuery, state: FSMContext):
    await state.set_state(LoginStates.waiting_login)
    await callback.message.edit_text(
        "Введите логин для входа",
        reply_markup=back_keyboard("back_to_auth")
    )


@dp.message(LoginStates.waiting_login)
async def login_login(message: Message, state: FSMContext):
    login = message.text.strip()
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE username = $1", login)
    if not user:
        await message.answer(
            "Аккаунт с таким логином не найден.",
            reply_markup=back_keyboard("back_to_auth")
        )
        await state.clear()
        return
    await state.update_data(login=login, user_id=user["id"])
    await state.set_state(LoginStates.waiting_password)
    await message.answer(
        "Введите пароль для аккаунта, после ввода ваш пароль удалится в чате‼️",
        reply_markup=back_keyboard("back_to_auth")
    )


@dp.message(LoginStates.waiting_password)
async def login_password(message: Message, state: FSMContext):
    password = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    login = data.get("login")
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE username = $1", login)
    if not user or not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        await message.answer(
            "Неверный пароль.",
            reply_markup=back_keyboard("back_to_auth")
        )
        await state.clear()
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sessions (telegram_id, user_id) VALUES ($1, $2) ON CONFLICT (telegram_id) DO UPDATE SET user_id = $2",
            message.from_user.id, user["id"]
        )
    await state.clear()
    text = (
        "Добро пожаловать в бота FloraClient.\n\n"
        "В данном боте вы можете приобрести подписку на данный клиент, "
        "подписка выдаётся автоматически при покупке за звезды, "
        "если вы хотите купить за деньги, то вам нужно написать поддержке — @FloraClientHelp."
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@dp.callback_query(F.data == "back_to_auth")
async def back_to_auth(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Выберите действие", reply_markup=auth_keyboard())


@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_auth_or_main(callback.from_user.id, callback)


@dp.callback_query(F.data == "buy")
async def buy_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "Тут производится оплата клиента, в данном меню находится покупка клиента",
        reply_markup=buy_keyboard()
    )


@dp.callback_query(F.data == "buy_stars")
async def buy_stars(callback: CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Подписка FloraClient",
        description="Подписка на клиент FloraClient",
        payload="floraclient_subscription",
        currency="XTR",
        prices=[LabeledPrice(label="Подписка FloraClient", amount=250)]
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    key_value = str(uuid.uuid4()).replace("-", "").upper()[:24]
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO keys (key_value) VALUES ($1)",
            key_value
        )
    await message.answer(
        f"Спасибо за покупку🥳\n\n"
        f"Ключ для получения подписки:\n<code>{key_value}</code>\n"
        f"Активируйте его в профиле",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "profile")
async def profile_menu(callback: CallbackQuery):
    user = await get_session_user(callback.from_user.id)
    if not user:
        await callback.message.edit_text("Выберите действие", reply_markup=auth_keyboard())
        return
    sub_status = "Активна ✅" if user["subscription_active"] else "Нет ❌"
    hwid = user["hwid"] if user["hwid"] else "Нету"
    reg_date = user["registered_at"].strftime("%d.%m.%Y") if user["registered_at"] else "—"
    text = (
        f"👤 Юзер: <b>{user['username']}</b>\n"
        f"📅 Дата регистрации: <b>{reg_date}</b>\n"
        f"🔑 Подписка: <b>{sub_status}</b>\n"
        f"🖥 HWID: <b>{hwid}</b>"
    )
    kb = await profile_keyboard(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "activate_key")
async def activate_key(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ActivateKeyStates.waiting_key)
    await callback.message.edit_text(
        "Введите ключ для активации подписки:",
        reply_markup=back_keyboard("profile")
    )


@dp.message(ActivateKeyStates.waiting_key)
async def process_activate_key(message: Message, state: FSMContext):
    key_value = message.text.strip()
    async with db_pool.acquire() as conn:
        key = await conn.fetchrow(
            "SELECT * FROM keys WHERE key_value = $1 AND used = FALSE",
            key_value
        )
        if not key:
            await message.answer(
                "Ключ не найден или уже использован.",
                reply_markup=back_keyboard("profile")
            )
            await state.clear()
            return
        user = await get_session_user(message.from_user.id)
        await conn.execute("UPDATE keys SET used = TRUE WHERE id = $1", key["id"])
        await conn.execute("UPDATE users SET subscription_active = TRUE WHERE id = $1", user["id"])
    await state.clear()
    await message.answer(
        "✅ Подписка успешно активирована!",
        reply_markup=main_menu_keyboard()
    )


@dp.callback_query(F.data == "admin")
async def admin_panel(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text("Выбери действие", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admin_create_key")
async def admin_create_key(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    key_value = str(uuid.uuid4()).replace("-", "").upper()[:24]
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO keys (key_value) VALUES ($1)", key_value)
    await callback.message.edit_text(
        f"✅ Ключ создан:\n<code>{key_value}</code>",
        reply_markup=back_keyboard("admin"),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("admin_users_"))
async def admin_users(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    page = int(callback.data.split("_")[-1])
    per_page = 10
    offset = page * per_page
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users")
        users = await conn.fetch(
            "SELECT * FROM users ORDER BY id LIMIT $1 OFFSET $2",
            per_page, offset
        )
    text = f"👥 Юзеры (страница {page + 1}):\n\n"
    for u in users:
        sub = "✅" if u["subscription_active"] else "❌"
        text += f"• <b>{u['username']}</b> | Sub: {sub} | ID: {u['telegram_id']}\n"
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_users_{page - 1}"))
    if offset + per_page < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_users_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@dp.callback_query(F.data == "admin_set_url")
async def admin_set_url(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_download_url)
    await callback.message.edit_text(
        "Отправьте новую ссылку на скачивание клиента:",
        reply_markup=back_keyboard("admin")
    )


@dp.message(AdminStates.waiting_download_url)
async def process_download_url(message: Message, state: FSMContext):
    url = message.text.strip()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('download_url', $1) ON CONFLICT (key) DO UPDATE SET value = $1",
            url
        )
    await state.clear()
    await message.answer("✅ Ссылка на скачивание обновлена.", reply_markup=admin_keyboard())


async def on_startup():
    global db_pool
    db_pool = await asyncpg.create_pool(dsn=DB_URL)


async def main():
    await on_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
    