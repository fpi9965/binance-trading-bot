import os
import time
import math
import logging
import threading
from datetime import datetime, timezone

from binance.client import Client
from binance.enums import *
from flask import Flask
import telebot

# ================== 1. الإعدادات العامة ==================

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_API_SECRET")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",   "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

RISK_PER_TRADE = 0.05  # 5% من الرصيد
LEVERAGE       = 20
TIMEFRAME      = "15m"

# 🛡️ إعدادات الحماية المزدوجة
STOP_LOSS_PCT   = 0.02         
TRAILING_CALLBACK_RATE = 1.0   
TRAILING_ACTIVATION_PCT = 0.005 

TOP_SYMBOLS = ["DOGEUSDT", "1000SHIBUSDT", "POLUSDT", "XRPUSDT", "SOLUSDT", "LTCUSDT", "LINKUSDT"]
MIN_24H_QUOTE_VOLUME = 1_000_000
MIN_SCORE            = 35

# ================== 2. تهيئة النظام ==================

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
bot    = telebot.TeleBot(TELEGRAM_TOKEN)
app    = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

_symbol_filters_cache = {}
open_trades = {}

# متغيرات الرقابة
daily_start_balance  = None
daily_reset_date     = None
bot_halted_daily     = False

# ================== 3. الدوال الحسابية والمؤشرات ==================

def ema(values, period):
    if len(values) < period: return sum(values) / len(values)
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def compute_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal: return 0, 0, False
    k_fast, k_slow = 2/(fast+1), 2/(slow+1)
    ema_f, ema_s = closes[0], closes[0]
    macd_line = []
    for c in closes:
        ema_f = c * k_fast + ema_f * (1 - k_fast)
        ema_s = c * k_slow + ema_s * (1 - k_slow)
        macd_line.append(ema_f - ema_s)
    sig_val = ema(macd_line, signal)
    return macd_line[-1], sig_val, macd_line[-1] > sig_val

def get_symbol_filters(symbol):
    if symbol in _symbol_filters_cache: return _symbol_filters_cache[symbol]
    try:
        info = client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                lot = next(f["stepSize"] for f in s["filters"] if f["filterType"] == "LOT_SIZE")
                tick = next(f["tickSize"] for f in s["filters"] if f["filterType"] == "PRICE_FILTER")
                notional = next(f["notional"] for f in s["filters"] if f["filterType"] == "MIN_NOTIONAL")
                res = (float(lot), float(tick), float(notional))
                _symbol_filters_cache[symbol] = res
                return res
    except: pass
    return (0.001, 0.01, 5.0)

def adjust_quantity(symbol, qty):
    lot, _, _ = get_symbol_filters(symbol)
    precision = int(round(-math.log(lot, 10)))
    return float(f"{qty:.{precision}f}")

def adjust_price(symbol, price):
    _, tick, _ = get_symbol_filters(symbol)
    precision = int(round(-math.log(tick, 10)))
    return float(f"{price:.{precision}f}")

# ================== 4. دالة فتح الصفقة (النسخة المصححة) ==================

def open_long_position(symbol, entry_price, balance):
    try:
        # 1. فحص العدد الأقصى
        if len(open_trades) >= 3: return False

        # 2. فحص الرصيد والكمية
        acc_info = client.futures_account()
        avail_bal = float(acc_info['availableBalance'])
        
        lot, tick, min_notional = get_symbol_filters(symbol)
        
        raw_qty = (balance * RISK_PER_TRADE * LEVERAGE) / entry_price
        qty = adjust_quantity(symbol, raw_qty)

        # 🛑 حل مشكلة Quantity less than zero
        if qty <= 0:
            logging.warning(f"⚠️ {symbol}: الكمية ضئيلة جداً ({raw_qty}). تخطي الصفقة.")
            return False

        # 🛑 حل مشكلة الهامش
        required_margin = (qty * entry_price) / LEVERAGE
        if required_margin > avail_bal:
            logging.info(f"⚠️ رصيد غير كافٍ لـ {symbol}: متاح {avail_bal:.2f}, مطلوب {required_margin:.2f}")
            return False

        if qty * entry_price < min_notional: return False

        # 3. التنفيذ
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        
        try:
            client.futures_create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
        except Exception as e:
            if "Margin is insufficient" in str(e) or "Quantity" in str(e):
                return False
            raise e

        # 4. إعداد الحماية
        time.sleep(0.8)
        pos = client.futures_position_information(symbol=symbol)[0]
        actual_entry = float(pos['entryPrice']) or entry_price

        client.futures_cancel_all_open_orders(symbol=symbol)

        # الوقف الثابت
        sl_price = adjust_price(symbol, actual_entry * (1 - STOP_LOSS_PCT))
        client.futures_create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_STOP_MARKET, stopPrice=sl_price, quantity=qty, reduceOnly=True)

        # الملاحقة (Trailing)
        activation = adjust_price(symbol, actual_entry * (1 + TRAILING_ACTIVATION_PCT))
        client.futures_create_order(symbol=symbol, side=SIDE_SELL, type="TRAILING_STOP_MARKET", quantity=qty, callbackRate=TRAILING_CALLBACK_RATE, activationPrice=activation, reduceOnly=True)

        bot.send_message(TELEGRAM_CHAT_ID, f"🚀 **دخول صفقة {symbol}**\nسعر: {actual_entry}\nكمية: {qty}\nوقف: {sl_price}")
        open_trades[symbol] = {"entry": actual_entry, "qty": qty}
        return True

    except Exception as e:
        logging.error(f"❌ خطأ فتح صفقة {symbol}: {e}")
        return False

# ================== 5. الحلقة الرئيسية ==================

def main_loop():
    global daily_start_balance, daily_reset_date, bot_halted_daily
    
    initial_bal = float(next(b["balance"] for b in client.futures_account_balance() if b["asset"] == "USDT"))
    daily_start_balance = initial_bal
    daily_reset_date = datetime.now(timezone.utc).date()

    while True:
        try:
            # تحديث يومي للوقت
            now_utc = datetime.now(timezone.utc)
            if now_utc.date() != daily_reset_date:
                daily_reset_date = now_utc.date()
                daily_start_balance = float(next(b["balance"] for b in client.futures_account_balance() if b["asset"] == "USDT"))
                bot_halted_daily = False

            # مراقبة الصفقات المفتوحة
            for sym in list(open_trades.keys()):
                pos = client.futures_position_information(symbol=sym)[0]
                if float(pos["positionAmt"]) == 0:
                    bot.send_message(TELEGRAM_CHAT_ID, f"🏁 تم إغلاق {sym}")
                    open_trades.pop(sym)

            # فحص السوق
            current_total = float(next(b["balance"] for b in client.futures_account_balance() if b["asset"] == "USDT"))
            for symbol in TOP_SYMBOLS:
                if symbol in open_trades: continue
                
                klines = client.futures_klines(symbol=symbol, interval=TIMEFRAME, limit=100)
                closes = [float(k[4]) for k in klines]
                _, _, macd_bull = compute_macd(closes)
                
                if macd_bull:
                    ticker = client.futures_ticker(symbol=symbol)
                    if float(ticker['quoteVolume']) >= MIN_24H_QUOTE_VOLUME:
                        open_long_position(symbol, float(ticker['lastPrice']), current_total)
                        time.sleep(2) # تأخير بسيط لتحديث الرصيد

            time.sleep(30)
        except Exception as e:
            logging.error(f"Main Loop Error: {e}")
            time.sleep(10)

# ================== 6. السيرفر ==================

@app.route('/')
def home(): return "Bot v6.8 is LIVE"

if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
