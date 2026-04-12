import os
import time
import math
import logging
from datetime import datetime, timedelta

from binance.client import Client
from binance.enums import *
from flask import Flask
import telebot

# ================== الإعدادات العامة ==================

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_BINANCE_API_SECRET")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

RISK_PER_TRADE = 0.05        # 5% من الرصيد
LEVERAGE = 20                # 20x
TIMEFRAME = "15m"            # الفريم المستخدم للتحليل
TOP_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT",
    "LTCUSDT", "DOGEUSDT", "MATICUSDT", "LINKUSDT", "SHIBUSDT"
]

# فلتر حجم تداول (قيمة تقريبية – عدّلها لو حاب)
MIN_24H_QUOTE_VOLUME = 50_000_000  # 50 مليون USDT

# إعدادات وقف الخسارة وجني الأرباح (نِسَب من سعر الدخول)
STOP_LOSS_PCT = 0.01   # 1% وقف خسارة
TAKE_PROFIT_PCT = 0.02 # 2% جني أرباح

# ================== تهيئة العملاء ==================

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

# ================== دوال مساعدة ==================

def send_telegram(msg: str):
    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg)
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def get_futures_balance_usdt():
    acc = client.futures_account_balance()
    for b in acc:
        if b["asset"] == "USDT":
            return float(b["balance"])
    return 0.0

def get_symbol_filters(symbol):
    info = client.futures_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            lot_size = None
            price_filter = None
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    lot_size = float(f["stepSize"])
                if f["filterType"] == "PRICE_FILTER":
                    price_filter = float(f["tickSize"])
            return lot_size, price_filter
    return None, None

def adjust_quantity(symbol, quantity):
    lot_size, _ = get_symbol_filters(symbol)
    if lot_size is None:
        # fallback
        return float(f"{quantity:.3f}")
    precision = int(round(-math.log(lot_size, 10)))
    return float(f"{quantity:.{precision}f}")

def adjust_price(symbol, price):
    _, tick_size = get_symbol_filters(symbol)
    if tick_size is None:
        return float(f"{price:.2f}")
    precision = int(round(-math.log(tick_size, 10)))
    return float(f"{price:.{precision}f}")

def get_klines(symbol, interval, limit=100):
    kl = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    closes = [float(k[4]) for k in kl]
    volumes = [float(k[7]) for k in kl]  # quote volume
    return closes, volumes

def ema(values
