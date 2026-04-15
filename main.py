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

# ══════════════════════════════════════════════
#  1. الإعدادات (يفضل استخدام Variables للمحيط)
# ══════════════════════════════════════════════

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_API_SECRET")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

RISK_PER_TRADE  = 0.05   # 5% من الرصيد
LEVERAGE        = 20
TIMEFRAME        = "15m"
MAX_OPEN_TRADES = 3      

# 🛡️ إعدادات الحماية
STOP_LOSS_PCT           = 0.02    # 2%
TRAILING_CALLBACK_RATE  = 1.0     # 1%
TRAILING_ACTIVATION_PCT = 0.005   # +0.5%

# حماية المحفظة
DAILY_LOSS_LIMIT_PCT = 0.05
TOTAL_LOSS_LIMIT_PCT = 0.15

TOP_SYMBOLS = [
    "DOGEUSDT", "XRPUSDT", "SOLUSDT",
    "LTCUSDT",  "LINKUSDT", "POLUSDT"
]
MIN_24H_QUOTE_VOLUME = 1_000_000
MIN_SCORE            = 35

# ══════════════════════════════════════════════
#  2. تهيئة النظام
# ══════════════════════════════════════════════

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
bot    = telebot.TeleBot(TELEGRAM_TOKEN)
app    = Flask(__name__)

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s - %(levelname)s - %(message)s"
)

_filters_cache = {}
open_trades = {}

bot_start_balance = None
daily_start_balance = None
daily_reset_date = None
bot_halted_total = False
bot_halted_daily = False
_last_report_date = None

# ══════════════════════════════════════════════
#  3. الدوال المساعدة
# ══════════════════════════════════════════════

def utcnow():
    return datetime.now(timezone.utc)

def send_telegram(msg: str):
    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def get_futures_balance() -> float:
    try:
        balances = client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b["balance"])
    except Exception as e:
        logging.error(f"Error getting balance: {e}")
    return 0.0

def get_filters(symbol: str):
    global _filters_cache
    if symbol in _filters_cache:
        return _filters_cache[symbol]
    
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            sym = s['symbol']
            f_data = {
                "lot": 0.001,
                "tick": 0.01,
                "min_notional": 5.0
            }
            for filt in s['filters']:
                if filt['filterType'] == 'LOT_SIZE':
                    f_data["lot"] = float(filt['stepSize'])
                elif filt['filterType'] == 'PRICE_FILTER':
                    f_data["tick"] = float(filt['tickSize'])
                elif filt['filterType'] == 'MIN_NOTIONAL':
                    f_data["min_notional"] = float(filt['notional'])
            _filters_cache[sym] = (f_data["lot"], f_data["tick"], f_data["min_notional"])
        return _filters_cache.get(symbol, (0.001, 0.01, 5.0))
    except Exception as e:
        logging.error(f"Filter error: {e}")
        return (0.001, 0.01, 5.0)

def round_step(value, step):
    if step == 0: return value
    precision = int(round(-math.log10(step)))
    return round(math.floor(value / step) * step, precision)

def get_actual_position(symbol: str):
    try:
        pos = client.futures_position_information(symbol=symbol)
        for p in pos:
            amt = float(p["positionAmt"])
            entry = float(p["entryPrice"])
            if abs(amt) > 0:
                return amt, entry
    except Exception as e:
        logging.error(f"Position check error: {e}")
    return 0.0, 0.0

# ══════════════════════════════════════════════
#  4. إدارة الحماية
# ══════════════════════════════════════════════

def place_protection(symbol, entry, qty):
    try:
        # إلغاء الأوامر القديمة
        client.futures_cancel_all_open_orders(symbol=symbol)
        time.sleep(0.5)

        lot, tick, _ = get_filters(symbol)
        # SL Price (لعمليات الشراء Long يكون السعر تحت الدخول)
        sl_price = round_step(entry * (1 - STOP_LOSS_PCT), tick)
        
        # 1. Stop Loss
        client.futures_create_order(
            symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_STOP_MARKET,
            stopPrice=sl_price, quantity=qty, reduceOnly=True, workingType="MARK_PRICE"
        )

        # 2. Trailing Stop
        activation = round_step(entry * (1 + TRAILING_ACTIVATION_PCT), tick)
        client.futures_create_order(
            symbol=symbol, side=SIDE_SELL, type="TRAILING_STOP_MARKET",
            quantity=qty, callbackRate=TRAILING_CALLBACK_RATE,
            activationPrice=activation, reduceOnly=True, workingType="MARK_PRICE"
        )
        return True
    except Exception as e:
        logging.error(f"Protection error {symbol}: {e}")
        return False

# ══════════════════════════════════════════════
#  5. المؤشرات الفنية
# ══════════════════════════════════════════════

def compute_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    deltas = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
    gain = [d if d > 0 else 0 for d in deltas]
    loss = [-d if d < 0 else 0 for d in deltas]
    
    avg_gain = sum(gain[:period]) / period
    avg_loss = sum(loss[:period]) / period
    
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_ema(closes, period):
    if len(closes) < period: return closes[-1]
    alpha = 2 / (period + 1)
    ema_val = sum(closes[:period]) / period
    for price in closes[period:]:
        ema_val = (price - ema_val) * alpha + ema_val
    return ema_val

def score_symbol(symbol):
    try:
        klines = client.futures_klines(symbol=symbol, interval=TIMEFRAME, limit=100)
        closes = [float(k[4]) for k in klines]
        
        # ترند الساعة
        h_klines = client.futures_klines(symbol=symbol, interval="1h", limit=200)
        h_closes = [float(k[4]) for k in h_klines]
        ema200 = get_ema(h_closes, 200)
        
        if closes[-1] < ema200: return None # فلتر السعر فوق المتوسط

        rsi = compute_rsi(closes)
        
        score = 0
        if rsi < 40: score += 40  # مناطق تشبع بيعي أو بداية ارتداد
        elif rsi < 60: score += 20
        
        # تحقق من الفوليوم
        t = client.futures_ticker(symbol=symbol)
        if float(t['quoteVolume']) < MIN_24H_QUOTE_VOLUME: return None

        return {"symbol": symbol, "score": score, "rsi": rsi, "price": closes[-1]}
    except:
        return None

# ══════════════════════════════════════════════
#  6. المنطق الأساسي (Main Logic)
# ══════════════════════════════════════════════

def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date, bot_halted_total
    
    logging.info("🚀 تشغيل البوت...")
    bot_start_balance = get_futures_balance()
    daily_start_balance = bot_start_balance
    daily_reset_date = utcnow().date()

    while not bot_halted_total:
        try:
            curr_balance = get_futures_balance()
            
            # فحص إغلاق اليوم
            if utcnow().date() != daily_reset_date:
                daily_start_balance = curr_balance
                daily_reset_date = utcnow().date()
                send_telegram(f"📅 *بداية يوم جديد*\nالرصيد: `{curr_balance}`")

            # مراقبة الصفقات المفتوحة
            active_positions = client.futures_position_information()
            current_open_syms = []
            for p in active_positions:
                amt = float(p["positionAmt"])
                sym = p["symbol"]
                if abs(amt) > 0:
                    current_open_syms.append(sym)
                    # إذا كانت الصفقة غير مسجلة لدينا، نؤمنها
                    if sym not in open_trades:
                        open_trades[sym] = {"entry": float(p["entryPrice"]), "qty": abs(amt)}
                        place_protection(sym, float(p["entryPrice"]), abs(amt))
            
            # تنظيف القائمة المحلية
            for sym in list(open_trades.keys()):
                if sym not in current_open_syms:
                    send_telegram(f"✅ تم إغلاق صفقة: `{sym}`")
                    open_trades.pop(sym)

            # البحث عن فرص جديدة
            if len(open_trades) < MAX_OPEN_TRADES:
                for symbol in TOP_SYMBOLS:
                    if symbol in open_trades: continue
                    
                    analysis = score_symbol(symbol)
                    if analysis and analysis["score"] >= MIN_SCORE:
                        # حساب حجم الصفقة
                        lot, tick, min_notional = get_filters(symbol)
                        price = analysis["price"]
                        
                        dollar_risk = curr_balance * RISK_PER_TRADE * LEVERAGE
                        quantity = round_step(dollar_risk / price, lot)
                        
                        if quantity * price < min_notional: continue
                        
                        # تنفيذ الأمر
                        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
                        client.futures_create_order(
                            symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=quantity
                        )
                        
                        time.sleep(1)
                        actual_amt, actual_entry = get_actual_position(symbol)
                        if abs(actual_amt) > 0:
                            place_protection(symbol, actual_entry, abs(actual_amt))
                            open_trades[symbol] = {"entry": actual_entry, "qty": abs(actual_amt)}
                            send_telegram(f"🚀 *صفقة جديدة:* `{symbol}`\nالسعر: `{actual_entry}`\nالكمية: `{actual_amt}`")
                            break # فتح صفقة واحدة في كل دورة

        except Exception as e:
            logging.error(f"Loop error: {e}")
        
        time.sleep(30)

# ══════════════════════════════════════════════
#  7. التشغيل
# ══════════════════════════════════════════════

@app.route('/')
def index():
    return f"Bot is running. Active trades: {list(open_trades.keys())}"

if __name__ == "__main__":
    # تشغيل البوت في Thread منفصل
    threading.Thread(target=main_loop, daemon=True).start()
    # تشغيل Flask
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
