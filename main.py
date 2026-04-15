import os
import time
import math
import logging
import threading
from datetime import datetime, timezone

from binance.client import Client
# تم تغيير الاستيراد هنا لضمان الوصول لكل التعريفات
import binance.enums as be
from flask import Flask
import telebot

# ══════════════════════════════════════════════
#  1. الإعدادات
# ══════════════════════════════════════════════

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "YOUR_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_SECRET")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "YOUR_ID")

RISK_PER_TRADE  = 0.05
LEVERAGE        = 20
TIMEFRAME       = "15m"
MAX_OPEN_TRADES = 3

STOP_LOSS_PCT           = 0.02
TRAILING_CALLBACK_RATE  = 1.0
TRAILING_ACTIVATION_PCT = 0.005

DAILY_LOSS_LIMIT_PCT = 0.05
TOTAL_LOSS_LIMIT_PCT = 0.15

TOP_SYMBOLS = ["DOGEUSDT", "XRPUSDT", "SOLUSDT", "LTCUSDT", "LINKUSDT", "POLUSDT"]
MIN_24H_QUOTE_VOLUME = 1_000_000
MIN_SCORE            = 35

# ══════════════════════════════════════════════
#  2. تهيئة النظام
# ══════════════════════════════════════════════

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
bot    = telebot.TeleBot(TELEGRAM_TOKEN)
app    = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

_filters_cache = {}
open_trades = {}

bot_start_balance = None
daily_start_balance = None
daily_reset_date = None
bot_halted_total = False
bot_halted_daily = False

# ══════════════════════════════════════════════
#  3. الدوال المساعدة (المحسنة)
# ══════════════════════════════════════════════

def utcnow():
    return datetime.now(timezone.utc)

def clean_tg_msg(text):
    """تنظيف النصوص من الرموز التي تكسر Markdown تليجرام"""
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")

def send_telegram(msg: str):
    try:
        # استخدام HTML بدلاً من Markdown لأنه أكثر استقراراً مع النصوص البرمجية
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def get_futures_balance() -> float:
    try:
        acc = client.futures_account()
        return float(acc["totalWalletBalance"])
    except Exception as e:
        logging.error(f"Balance error: {e}")
        return 0.0

def get_filters(symbol: str):
    if symbol in _filters_cache: return _filters_cache[symbol]
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                lot = 1.0
                tick = 1.0
                notional = 5.0
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE': lot = float(f['stepSize'])
                    if f['filterType'] == 'PRICE_FILTER': tick = float(f['tickSize'])
                    if f['filterType'] == 'MIN_NOTIONAL': notional = float(f['notional'])
                _filters_cache[symbol] = (lot, tick, notional)
                return _filters_cache[symbol]
    except: pass
    return (0.001, 0.01, 5.0)

def round_step(value, step):
    if step == 0: return value
    precision = int(round(-math.log10(step)))
    return round(math.floor(value / step) * step, precision)

# ══════════════════════════════════════════════
#  4. إدارة أوامر الحماية (تم حل مشكلة عدم التعريف)
# ══════════════════════════════════════════════

def place_protection(symbol, entry, qty):
    try:
        # إلغاء كافة الأوامر المعلقة أولاً
        client.futures_cancel_all_open_orders(symbol=symbol)
        time.sleep(0.5)

        _, tick, _ = get_filters(symbol)
        sl_price = round_step(entry * (1 - STOP_LOSS_PCT), tick)
        
        # 1. Stop Loss (استخدام النصوص مباشرة بدلاً من Enums لتفادي أخطاء التعريف)
        client.futures_create_order(
            symbol=symbol,
            side='SELL',
            type='STOP_MARKET',
            stopPrice=sl_price,
            quantity=qty,
            reduceOnly=True,
            workingType='MARK_PRICE'
        )
        logging.info(f"✅ SL set for {symbol} at {sl_price}")

        # 2. Trailing Stop
        # إذا استمر خطأ 4120، سنستخدم Take Profit ثابت كبديل مؤقت
        try:
            activation = round_step(entry * (1 + TRAILING_ACTIVATION_PCT), tick)
            client.futures_create_order(
                symbol=symbol,
                side='SELL',
                type='TRAILING_STOP_MARKET',
                quantity=qty,
                callbackRate=TRAILING_CALLBACK_RATE,
                activationPrice=activation,
                reduceOnly=True
            )
            logging.info(f"✅ Trailing set for {symbol}")
        except Exception as te:
            logging.warning(f"⚠️ Trailing Stop not supported for {symbol}, placing TP instead: {te}")
            tp_price = round_step(entry * 1.05, tick) # هدف 5%
            client.futures_create_order(
                symbol=symbol, side='SELL', type='TAKE_PROFIT_MARKET',
                stopPrice=tp_price, quantity=qty, reduceOnly=True
            )

        return True
    except Exception as e:
        logging.error(f"❌ Protection error {symbol}: {e}")
        return False

# ══════════════════════════════════════════════
#  5. التحليل والدورة الرئيسية
# ══════════════════════════════════════════════

def get_actual_position(symbol: str):
    try:
        pos = client.futures_position_information(symbol=symbol)
        for p in pos:
            if p['symbol'] == symbol:
                return float(p["positionAmt"]), float(p["entryPrice"])
    except: pass
    return 0.0, 0.0

def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date
    
    send_telegram("<b>🤖 البوت بدأ العمل (النسخة V9)</b>")
    bot_start_balance = get_futures_balance()
    daily_start_balance = bot_start_balance
    daily_reset_date = utcnow().date()

    while True:
        try:
            curr_balance = get_futures_balance()
            
            # فحص الوضعيات الحالية وتحديث الـ SL إذا سقط
            for symbol in TOP_SYMBOLS:
                amt, entry = get_actual_position(symbol)
                if abs(amt) > 0:
                    orders = client.futures_get_open_orders(symbol=symbol)
                    has_sl = any(o['type'] in ['STOP_MARKET', 'STOP'] for o in orders)
                    if not has_sl:
                        logging.warning(f"🚨 {symbol} missing SL! Resetting...")
                        place_protection(symbol, entry, abs(amt))
                    open_trades[symbol] = True
                else:
                    if symbol in open_trades: open_trades.pop(symbol)

            # البحث عن فرص (إذا كان هناك مكان متاح)
            if len(open_trades) < MAX_OPEN_TRADES:
                for symbol in TOP_SYMBOLS:
                    if symbol in open_trades: continue
                    
                    # (هنا تضع كود score_symbol من النسخة السابقة)
                    # للتبسيط، سأقوم بمحاكاة الدخول إذا تحقق الشرط
                    klines = client.futures_klines(symbol=symbol, interval=TIMEFRAME, limit=50)
                    # ... حساب المؤشرات ...
                    
                    # مثال دخول (يجب إضافة منطق Score هنا)
                    # if score >= MIN_SCORE: 
                    #     execute_trade(symbol)

        except Exception as e:
            logging.error(f"Error in main loop: {e}")
        
        time.sleep(30)

@app.route('/')
def index(): return "Bot V9 is running"

if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
