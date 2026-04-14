import os
import time
import math
import logging
import threading
from datetime import datetime, timedelta

from binance.client import Client
from binance.enums import *
from flask import Flask
import telebot

# ==============================================================================
# 1. إعدادات النظام والبيئة
# ==============================================================================

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_API_SECRET")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",   "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

# إعدادات التداول الأساسية
RISK_PER_TRADE = 0.05      # 5% من الرصيد المتاح لكل صفقة
LEVERAGE       = 20        # الرافعة المالية
TIMEFRAME      = "15m"     # الفريم الرئيسي للتحليل

# 🛡️ إعدادات الحماية المزدوجة (تعديلات v6.6)
STOP_LOSS_PCT   = 0.02         # وقف خسارة ثابت 2% (لحماية رأس المال)
TRAILING_CALLBACK_RATE = 1.0   # ملاحقة الربح (إغلاق عند ارتداد 1%)
TRAILING_ACTIVATION_PCT = 0.005 # تفعيل الملاحقة بعد ربح 0.5% (تأمين الربح مبكراً)

# فلاتر العملات
TOP_SYMBOLS = [
    "DOGEUSDT", "1000SHIBUSDT", "POLUSDT", "XRPUSDT", "SOLUSDT", 
    "LTCUSDT", "LINKUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT"
]
MIN_24H_QUOTE_VOLUME = 1_500_000  # الحد الأدنى للسيولة
MIN_SCORE_TO_ENTRY   = 40         # الحد الأدنى من النقاط للدخول

# إعدادات حماية المحفظة الكلية
DAILY_LOSS_LIMIT_PCT = 0.05       # التوقف إذا خسر الحساب 5% في يوم
TOTAL_LOSS_LIMIT_PCT = 0.15       # التوقف النهائي إذا خسر الحساب 15%

# ==============================================================================
# 2. تهيئة العملاء والمخازن المؤقتة
# ==============================================================================

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
bot    = telebot.TeleBot(TELEGRAM_TOKEN)
app    = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

_symbol_filters_cache = {}
open_trades = {}  # لتتبع الصفقات الحالية

# متغيرات الرقابة المالية
bot_start_balance    = None
daily_start_balance  = None
daily_reset_date     = None
bot_halted_total     = False
bot_halted_daily     = False

# ==============================================================================
# 3. الدوال الحسابية والمؤشرات الفنية (النسخة الموسعة)
# ==============================================================================

def ema(values, period):
    if len(values) < period: return sum(values) / len(values)
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def compute_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal: return 0, 0, 0
    k_fast, k_slow = 2/(fast+1), 2/(slow+1)
    ema_f, ema_s = closes[0], closes[0]
    macd_line = []
    for c in closes:
        ema_f = c * k_fast + ema_f * (1 - k_fast)
        ema_s = c * k_slow + ema_s * (1 - k_slow)
        macd_line.append(ema_f - ema_s)
    sig_val = ema(macd_line, signal)
    hist = macd_line[-1] - sig_val
    return macd_line[-1], sig_val, hist

def compute_rsi(closes, period=14):
    if len(closes) < period: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_atr(klines, period=14):
    if len(klines) < period + 1: return 0
    tr_list = []
    for i in range(1, len(klines)):
        h, l, pc = float(klines[i][2]), float(klines[i][3]), float(klines[i-1][4])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period

def analyze_candles_multi(symbol):
    results = {}
    intervals = ["1m", "5m", "15m", "1h"]
    for inv in intervals:
        kl = client.futures_klines(symbol=symbol, interval=inv, limit=5)
        o, h, l, c = float(kl[-1][1]), float(kl[-1][2]), float(kl[-1][3]), float(kl[-1][4])
        body = abs(c - o)
        range_t = h - l
        if range_t == 0: results[inv] = "Neutral"
        elif body <= range_t * 0.1: results[inv] = "Doji"
        elif c > o: results[inv] = "Bullish"
        else: results[inv] = "Bearish"
    return results

# ==============================================================================
# 4. إدارة أوامر المنصة (الضبط الدقيق)
# ==============================================================================

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

# ==============================================================================
# 5. منطق الدخول والحماية المزدوجة (The Core Logic)
# ==============================================================================

def open_long_position(symbol, entry_price, balance):
    try:
        # 1. فحص عدد الصفقات المفتوحة (تعديل لمنع استنزاف الهامش)
        MAX_OPEN_TRADES = 3 # يمكنك تغيير الرقم حسب رغبتك
        if len(open_trades) >= MAX_OPEN_TRADES:
            logging.info(f"⏭️ تخطي {symbol}: تم الوصول للحد الأقصى من الصفقات ({MAX_OPEN_TRADES})")
            return False

        # أ- ضبط الرافعة
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        lot, tick, min_notional = get_symbol_filters(symbol)

        # ب- حساب الكمية وفحص الرصيد المتاح (التعديل الجوهري هنا) 🛡️
        # نجلب الرصيد المتاح الفعلي من المنصة الآن
        available_balance = float(next(b["availableBalance"] for b in client.futures_account_balance() if b["asset"] == "USDT"))
        
        # حساب التكلفة المطلوبة (الهامش) لهذه الصفقة
        qty = adjust_quantity(symbol, (balance * RISK_PER_TRADE * LEVERAGE) / entry_price)
        required_margin = (qty * entry_price) / LEVERAGE

        # إذا كان الهامش المطلوب أكبر من المتاح، نلغي العملية فوراً
        if required_margin > available_balance:
            logging.warning(f"❌ رصيد غير كافٍ لفتح {symbol}: المطلوب {required_margin:.2f}, المتاح {available_balance:.2f}")
            return False

        if qty * entry_price < min_notional:
            return False

        # ج- تنفيذ أمر الشراء (Market)
        client.futures_create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
        
        # د- الحصول على سعر الدخول الفعلي
        time.sleep(0.5)
        pos = client.futures_position_information(symbol=symbol)[0]
        actual_entry = float(pos['entryPrice'])
        if actual_entry == 0: actual_entry = entry_price

        # هـ- إلغاء أي أوامر قديمة عالقة لهذا الرمز
        client.futures_cancel_all_open_orders(symbol=symbol)

        # و- وضع الدرع 1: Stop Loss ثابت
        sl_price = adjust_price(symbol, actual_entry * (1 - STOP_LOSS_PCT))
        client.futures_create_order(
            symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_STOP_MARKET,
            stopPrice=sl_price, quantity=qty, reduceOnly=True
        )

        # ز- وضع الدرع 2: Trailing Stop
        activation = adjust_price(symbol, actual_entry * (1 + TRAILING_ACTIVATION_PCT))
        client.futures_create_order(
            symbol=symbol, side=SIDE_SELL, type="TRAILING_STOP_MARKET",
            quantity=qty, callbackRate=TRAILING_CALLBACK_RATE,
            activationPrice=activation, reduceOnly=True, workingType="MARK_PRICE"
        )

        # ح- إرسال تقرير تليجرام
        msg = (f"✅ **صفقة LONG جديدة**\n"
               f"الزوج: `{symbol}`\n"
               f"الدخول: `{actual_entry}`\n"
               f"الوقف الثابت: `{sl_price}`\n"
               f"تفعيل الملاحقة: `{activation}`\n"
               f"الكمية: `{qty}`")
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        
        open_trades[symbol] = {"entry": actual_entry, "qty": qty}
        return True

    except Exception as e:
        logging.error(f"Error opening position for {symbol}: {e}")
        return False

# ==============================================================================
# 6. نظام الفحص والتقييم (Score System)
# ==============================================================================

def scan_market():
    global daily_start_balance, daily_reset_date, bot_halted_daily

    try:
        # فحص حماية الحساب اليومية
        total_bal = float(next(b["balance"] for b in client.futures_account_balance() if b["asset"] == "USDT"))
        avail_bal = float(next(b["availableBalance"] for b in client.futures_account_balance() if b["asset"] == "USDT"))

        # تحديث رصيد البداية اليومي
        today = datetime.utcnow().date()
        if daily_reset_date != today:
            daily_start_balance = total_bal
            daily_reset_date = today
            bot_halted_daily = False

        if (daily_start_balance - total_bal) / daily_start_balance >= DAILY_LOSS_LIMIT_PCT:
            if not bot_halted_daily:
                bot_halted_daily = True
                bot.send_message(TELEGRAM_CHAT_ID, "⚠️ تم بلوغ حد الخسارة اليومي. توقف التداول.")
            return

        for symbol in TOP_SYMBOLS:
            if symbol in open_trades:
                # التحقق إذا كانت الصفقة أغلقت من المنصة
                pos = client.futures_position_information(symbol=symbol)[0]
                if float(pos["positionAmt"]) == 0:
                    bot.send_message(TELEGRAM_CHAT_ID, f"🏁 تم إغلاق صفقة {symbol} (ضرب الوقف أو جني الربح).")
                    open_trades.pop(symbol)
                continue

            # تحليل البيانات الفنية
            klines = client.futures_klines(symbol=symbol, interval=TIMEFRAME, limit=100)
            closes = [float(k[4]) for k in klines]
            
            # حساب المؤشرات
            macd_l, sig_l, hist = compute_macd(closes)
            rsi_val = compute_rsi(closes)
            candles = analyze_candles_multi(symbol)
            
            # نظام النقاط (The Scoring)
            score = 0
            if hist > 0: score += 20
            if rsi_val < 65: score += 15
            if candles["15m"] == "Bullish": score += 10
            if candles["5m"] == "Bullish": score += 5
            
            # فحص الاتجاه العام (EMA 200)
            ema200 = ema(closes, 200)
            if closes[-1] > ema200: score += 20
            
            # فحص السيولة
            ticker = client.futures_ticker(symbol=symbol)
            volume = float(ticker['quoteVolume'])

            if score >= MIN_SCORE_TO_ENTRY and volume >= MIN_24H_QUOTE_VOLUME:
                open_long_position(symbol, float(ticker['lastPrice']), avail_bal)

    except Exception as e:
        logging.error(f"Scan error: {e}")

# ==============================================================================
# 7. تشغيل البوت والسيرفر
# ==============================================================================

def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date
    bot_start_balance = float(next(b["balance"] for b in client.futures_account_balance() if b["asset"] == "USDT"))
    daily_start_balance = bot_start_balance
    daily_reset_date = datetime.utcnow().date()
    
    bot.send_message(TELEGRAM_CHAT_ID, f"🤖 تم تشغيل بوت التداول v6.6\nالرصيد: {bot_start_balance} USDT")
    
    while True:
        scan_market()
        time.sleep(40) # فحص السوق كل 40 ثانية

@app.route('/')
def index(): return "Trading Bot is running..."

if __name__ == "__main__":
    # تشغيل منطق التداول في خيط منفصل
    threading.Thread(target=main_loop, daemon=True).start()
    # تشغيل Flask للتوافق مع Render
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
