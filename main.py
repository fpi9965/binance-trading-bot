import os
import time
import math
import logging
import threading
from datetime import datetime

from binance.client import Client
from binance.enums import *
from flask import Flask
import telebot

# ================== الإعدادات العامة ==================

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_API_SECRET")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",   "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

# إعدادات التداول
RISK_PER_TRADE = 0.05
LEVERAGE       = 20
TIMEFRAME      = "15m"

# 🛡️ إعدادات الحماية المتقدمة
STOP_LOSS_PCT   = 0.02         # وقف خسارة ثابت فور الدخول (2%)
TRAILING_CALLBACK_RATE = 1.0   # التراجع المطلوب لجني الربح (1%)
TRAILING_ACTIVATION_PCT = 0.005 # تفعيل الملاحقة بعد ربح (0.5%)

TOP_SYMBOLS = ["DOGEUSDT", "1000SHIBUSDT", "POLUSDT", "XRPUSDT", "SOLUSDT", "LTCUSDT", "LINKUSDT"]
MIN_24H_QUOTE_VOLUME = 1_000_000
MIN_SCORE            = 30

# حماية الحساب
DAILY_LOSS_LIMIT_PCT = 0.05
TOTAL_LOSS_LIMIT_PCT = 0.15

# ================== تهيئة العملاء ==================

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
bot    = telebot.TeleBot(TELEGRAM_TOKEN)
app    = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

_symbol_filters_cache = {}
open_trades = {}

# متغيرات حماية البوت
bot_start_balance    = None
daily_start_balance  = None
daily_reset_date     = None
bot_halted_total     = False
bot_halted_daily     = False

# ================== الدوال المساعدة ==================

def send_telegram(msg: str):
    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg)
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def get_futures_balance_usdt():
    try:
        acc = client.futures_account_balance()
        for b in acc:
            if b["asset"] == "USDT":
                return float(b["balance"])
    except: return 0.0
    return 0.0

def get_symbol_filters(symbol):
    if symbol in _symbol_filters_cache: return _symbol_filters_cache[symbol]
    try:
        info = client.futures_exchange_info()
        for s in info["symbols"]:
            sym = s["symbol"]
            lot_size = next(f["stepSize"] for f in s["filters"] if f["filterType"] == "LOT_SIZE")
            tick_size = next(f["tickSize"] for f in s["filters"] if f["filterType"] == "PRICE_FILTER")
            min_notional = next(f["notional"] for f in s["filters"] if f["filterType"] == "MIN_NOTIONAL")
            _symbol_filters_cache[sym] = (float(lot_size), float(tick_size), float(min_notional))
        return _symbol_filters_cache.get(symbol, (0.001, 0.01, 5.0))
    except: return (0.001, 0.01, 5.0)

def adjust_quantity(symbol, quantity):
    lot_size, _, _ = get_symbol_filters(symbol)
    precision = int(round(-math.log(lot_size, 10)))
    return float(f"{quantity:.{precision}f}")

def adjust_price(symbol, price):
    _, tick_size, _ = get_symbol_filters(symbol)
    precision = int(round(-math.log(tick_size, 10)))
    return float(f"{price:.{precision}f}")

# ================== تحليل الشموع (المسترجع من كودك) ==================

def analyze_candles(symbol, interval="5m"):
    kl = client.futures_klines(symbol=symbol, interval=interval, limit=5)
    if len(kl) < 2: return "Neutral"
    o, h, l, c = float(kl[-1][1]), float(kl[-1][2]), float(kl[-1][3]), float(kl[-1][4])
    body = abs(c - o)
    range_t = h - l
    prev_o, prev_c = float(kl[-2][1]), float(kl[-2][4])

    if range_t > 0 and body <= range_t * 0.1: return "Doji"
    if c > o and prev_c < prev_o and c > prev_o and o < prev_c: return "Bullish Engulfing"
    if c < o and prev_c > prev_o and o > prev_c and c < prev_o: return "Bearish Engulfing"
    return "Bullish" if c > o else "Bearish"

# ================== فتح الصفقة مع الحماية المزدوجة ==================

def open_long_with_protection(symbol, entry_price, available_balance):
    try:
        # 1. ضبط الرافعة والكمية
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        lot_size, tick_size, min_notional = get_symbol_filters(symbol)
        
        quantity = adjust_quantity(symbol, (available_balance * RISK_PER_TRADE * LEVERAGE) / entry_price)
        if quantity * entry_price < min_notional: return False

        # 2. تنفيذ أمر الشراء
        client.futures_create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=quantity)
        
        # جلب السعر الفعلي للدخول
        pos = client.futures_position_information(symbol=symbol)[0]
        actual_entry = float(pos['entryPrice']) if float(pos['entryPrice']) > 0 else entry_price

        # 3. إلغاء أي أوامر حماية قديمة معلقة
        client.futures_cancel_all_open_orders(symbol=symbol)

        # 4. الدرع الأول: وقف خسارة ثابت (Stop Loss) 🛡️
        stop_price = adjust_price(symbol, actual_entry * (1 - STOP_LOSS_PCT))
        client.futures_create_order(
            symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_STOP_MARKET,
            stopPrice=stop_price, quantity=quantity, reduceOnly=True
        )

        # 5. الدرع الثاني: ملاحقة أرباح (Trailing Stop) 📈
        act_price = adjust_price(symbol, actual_entry * (1 + TRAILING_ACTIVATION_PCT))
        client.futures_create_order(
            symbol=symbol, side=SIDE_SELL, type="TRAILING_STOP_MARKET",
            quantity=quantity, callbackRate=TRAILING_CALLBACK_RATE,
            activationPrice=act_price, reduceOnly=True, workingType="MARK_PRICE"
        )

        send_telegram(f"🚀 تم الدخول في {symbol}\n💰 سعر: {actual_entry}\n🛡️ وقف ثابت: {stop_price}\n📈 تفعيل الملاحقة: {act_price}")
        open_trades[symbol] = {"entry": actual_entry, "qty": quantity}
        return True

    except Exception as e:
        logging.error(f"Error opening {symbol}: {e}")
        return False

# ================== الحلقة الرئيسية ==================

def main_loop():
    global daily_start_balance, daily_reset_date
    daily_start_balance = get_futures_balance_usdt()
    daily_reset_date = datetime.utcnow().date()

    while True:
        try:
            current_balance = get_futures_balance_usdt()
            
            # فحص الصفقات المفتوحة لتنظيف السجل
            for sym in list(open_trades.keys()):
                pos = client.futures_position_information(symbol=sym)[0]
                if float(pos["positionAmt"]) == 0:
                    send_telegram(f"✅ صفقة {sym} أغلقت.")
                    open_trades.pop(sym)

            # البحث عن فرص جديدة
            for symbol in TOP_SYMBOLS:
                if symbol in open_trades: continue
                
                # فلتر الشموع ( Bullish فقط للدخول)
                candle_status = analyze_candles(symbol)
                if candle_status in ["Bullish", "Bullish Engulfing"]:
                    ticker = client.futures_symbol_ticker(symbol=symbol)
                    open_long_with_protection(symbol, float(ticker["price"]), current_balance)

            time.sleep(20) # دورة كل 20 ثانية لسرعة الاستجابة
        except Exception as e:
            logging.error(f"Loop Error: {e}")
            time.sleep(10)

@app.route('/')
def home(): return "Bot Protection v6.3 Active"

if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
