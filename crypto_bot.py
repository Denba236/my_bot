import logging
import sqlite3
from decimal import Decimal, ROUND_DOWN
import requests

from aiogram import Bot, Dispatcher, executor, types

from config import TOKEN

API_TOKEN = TOKEN  # заміни на свій

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

DB_PATH = "crypto_bot.db"
INITIAL_USD_BALANCE = Decimal("10000.00")

# Підтримувані монети і їх пари на Binance
AVAILABLE_COINS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "bnb": "BNBUSDT",
    "ada": "ADAUSDT",
    "usdt": "USDTUSDT",  # спеціальний випадок
}

HELP_TEXT = (
    "Команди:\n"
    "/start та /help — допомога.\n"
    "/coins — список доступних монет.\n"
    "/balance — баланс USD.\n"
    "/portfolio — криптоактиви з приблизною оцінкою в USD.\n"
    "/price <монета> — поточна ціна монети.\n"
    "/buy <монета> <сума USD> — купити крипту.\n"
    "/sell <монета> <кількість> — продати крипту.\n"
    "/history — останні 10 транзакцій."
)


# --- DB helpers ---
def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_connection() as conn:
        c = conn.cursor()
        # користувачі: баланс в USD
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                usd_balance TEXT NOT NULL
            )
        """)
        # портфель: скільки монет у користувача
        c.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                user_id INTEGER,
                coin TEXT,
                amount TEXT,
                PRIMARY KEY (user_id, coin),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        # історія операцій
        c.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                coin TEXT,
                amount TEXT,
                price TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)

def ensure_user(user_id: int):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT usd_balance FROM users WHERE user_id = ?", (user_id,))
        if c.fetchone() is None:
            c.execute("INSERT INTO users (user_id, usd_balance) VALUES (?, ?)", (user_id, str(INITIAL_USD_BALANCE)))


def get_usd_balance(user_id: int) -> Decimal:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT usd_balance FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        return Decimal(row["usd_balance"]) if row else Decimal("0")

def update_usd_balance(user_id: int, new_balance: Decimal):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET usd_balance = ? WHERE user_id = ?", (str(new_balance), user_id))

def get_portfolio(user_id: int) -> dict:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT coin, amount FROM portfolio WHERE user_id = ?", (user_id,))
        rows = c.fetchall()
        return {row["coin"]: Decimal(row["amount"]) for row in rows}

def update_portfolio(user_id: int, coin: str, amount: Decimal):
    with get_connection() as conn:
        c = conn.cursor()
        if amount <= 0:
            c.execute("DELETE FROM portfolio WHERE user_id = ? AND coin = ?", (user_id, coin))
        else:
            c.execute("""
                INSERT INTO portfolio(user_id, coin, amount) VALUES (?, ?, ?)
                ON CONFLICT(user_id, coin) DO UPDATE SET amount=excluded.amount
            """, (user_id, coin, str(amount)))

def add_history(user_id: int, action: str, coin: str, amount: Decimal, price: Decimal):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO history(user_id, action, coin, amount, price)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, action, coin, str(amount), str(price)))

def get_history(user_id: int, limit: int = 10) -> list:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT action, coin, amount, price, timestamp FROM history
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (user_id, limit))
        return c.fetchall()

def get_price(symbol="BTCUSDT") -> Decimal | None:
    if symbol == "USDTUSDT":
        return Decimal("1")
    url = f'https://api.binance.com/api/v3/ticker/price?symbol={symbol}'
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        price = data.get("price")
        if price is not None:
            return Decimal(price)
    except Exception as e:
        logging.error(f"Binance API error for {symbol}: {e}")
    return None


@dp.message_handler(commands=['start', 'help'])
async def cmd_start_help(message: types.Message):
    ensure_user(message.from_user.id)
    await message.reply(HELP_TEXT)

@dp.message_handler(commands=['coins'])
async def cmd_coins(message: types.Message):
    await message.reply("Доступні монети: " + ", ".join(AVAILABLE_COINS.keys()))

@dp.message_handler(commands=['balance'])
async def cmd_balance(message: types.Message):
    user_id = message.from_user.id
    ensure_user(user_id)
    balance = get_usd_balance(user_id)
    await message.reply(f"Ваш баланс USD: {balance.quantize(Decimal('0.01'))}")

@dp.message_handler(commands=['portfolio'])
async def cmd_portfolio(message: types.Message):
    user_id = message.from_user.id
    ensure_user(user_id)
    portfolio = get_portfolio(user_id)
    if not portfolio:
        await message.reply("Ваш портфель порожній.")
        return
    lines = []
    total = Decimal("0")
    for coin, amt in portfolio.items():
        pair = AVAILABLE_COINS.get(coin)
        price = get_price(pair) if pair else None
        value = (amt * price).quantize(Decimal("0.01")) if price else Decimal("0")
        total += value
        lines.append(f"{coin.upper()}: {amt.quantize(Decimal('0.0001'))} (~{value} USD)")
    lines.append(f"\nЗагальна оцінка: {total.quantize(Decimal('0.01'))} USD")
    await message.reply("\n".join(lines))

@dp.message_handler(commands=['price'])
async def cmd_price(message: types.Message):
    arg = message.get_args().strip().lower()
    if not arg:
        await message.reply("Використання: /price <монета>")
        return
    if arg not in AVAILABLE_COINS:
        await message.reply("Невідома монета. Доступні: " + ", ".join(AVAILABLE_COINS.keys()))
        return
    pair = AVAILABLE_COINS[arg]
    price = get_price(pair)
    if price is None:
        await message.reply(f"Не вдалося отримати ціну для {arg.upper()}.")
    else:
        await message.reply(f"Ціна {arg.upper()} = {price.quantize(Decimal('0.01'))} USD")

@dp.message_handler(commands=['buy'])
async def cmd_buy(message: types.Message):
    parts = message.get_args().lower().split()
    if len(parts) != 2:
        await message.reply("Використання: /buy <монета> <сума USD>")
        return
    coin, usd_str = parts
    if coin not in AVAILABLE_COINS:
        await message.reply("Невідома монета.")
        return
    try:
        usd_amount = Decimal(usd_str)
        if usd_amount <= 0:
            raise ValueError
    except:
        await message.reply("Неправильна сума USD.")
        return

    user_id = message.from_user.id
    ensure_user(user_id)
    balance = get_usd_balance(user_id)
    if usd_amount > balance:
        await message.reply(f"Недостатньо USD. У вас: {balance.quantize(Decimal('0.01'))}")
        return

    pair = AVAILABLE_COINS[coin]
    price = get_price(pair)
    if price is None:
        await message.reply("Не вдалося отримати ціну.")
        return

    coin_amount = (usd_amount / price).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    # оновлюємо баланс і портфель
    update_usd_balance(user_id, balance - usd_amount)
    current_portfolio = get_portfolio(user_id)
    update_portfolio(user_id, coin, current_portfolio.get(coin, Decimal("0")) + coin_amount)
    add_history(user_id, "buy", coin, coin_amount, price)

    await message.reply(
        f"Куплено {coin_amount} {coin.upper()} за {usd_amount} USD.\n"
        f"Новий баланс: {get_usd_balance(user_id).quantize(Decimal('0.01'))} USD"
    )

@dp.message_handler(commands=['sell'])
async def cmd_sell(message: types.Message):
    parts = message.get_args().lower().split()
    if len(parts) != 2:
        await message.reply("Використання: /sell <монета> <кількість>")
        return
    coin, coin_str = parts
    if coin not in AVAILABLE_COINS:
        await message.reply("Невідома монета.")
        return
    try:
        coin_amount = Decimal(coin_str)
        if coin_amount <= 0:
            raise ValueError
    except:
        await message.reply("Неправильна кількість.")
        return

    user_id = message.from_user.id
    ensure_user(user_id)
    portfolio = get_portfolio(user_id)
    have = portfolio.get(coin, Decimal("0"))
    if coin_amount > have:
        await message.reply(f"У вас недостатньо {coin.upper()}. Маєте: {have}")
        return

    pair = AVAILABLE_COINS[coin]
    price = get_price(pair)
    if price is None:
        await message.reply("Не вдалося отримати ціну.")
        return

    usd_gain = (coin_amount * price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    # оновлюємо
    update_portfolio(user_id, coin, have - coin_amount)
    update_usd_balance(user_id, get_usd_balance(user_id) + usd_gain)
    add_history(user_id, "sell", coin, coin_amount, price)

    await message.reply(
        f"Продано {coin_amount} {coin.upper()} за {usd_gain} USD.\n"
        f"Новий баланс: {get_usd_balance(user_id).quantize(Decimal('0.01'))} USD"
    )



if __name__ == '__main__':
    init_db()
    executor.start_polling(dp, skip_updates=False)