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

def get_klines(symbol, interval, limit=100):
    kl = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    closes = [float(k[4]) for k in kl]
    volumes = [float(k[7]) for k in kl]  # quote volume
    return closes, volumes
    
def adjust_quantity(symbol, quantity):
    lot_size, _ = get_symbol_filters(symbol)
    if lot_size is None:
        return float(f"{quantity:.3f}")
    precision = int(round(-math.log(lot_size, 10)))
    return float(f"{quantity:.{precision}f}")
    
def adjust_price(symbol, price):
    _, tick_size = get_symbol_filters(symbol)
    if tick_size is None:
        return float(f"{price:.2f}")
    precision = int(round(-math.log(tick_size, 10)))
    return float(f"{price:.{precision}f}") 
    
def ema(values, period):
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def get_trend_filter(symbol):
    closes, _ = get_klines(symbol, "1h", 200)
    last_price = closes[-1]
    ema200 = ema(closes, 200)
    # ترند صاعد إذا السعر فوق EMA200
    return last_price > ema200, last_price, ema200

def pass_volume_filter(symbol):
    try:
        tick = client.futures_ticker(symbol=symbol)

        # إذا Binance رجعت خطأ
        if not isinstance(tick, dict):
            return False, 0

        if "code" in tick:
            return False, 0

        # إذا quoteVolume غير موجود
        quote_volume = float(tick.get("quoteVolume", 0))

        return quote_volume >= MIN_24H_QUOTE_VOLUME, quote_volume

    except Exception as e:
        print(f"⚠️ خطأ في pass_volume_filter لـ {symbol}: {e}")
        return False, 0

def analyze_symbol(symbol):
    closes, _ = get_klines(symbol, TIMEFRAME, 100)
    # مثال بسيط: MACD + RSI تقريبي (مبسط)
    # هنا نستخدم منطق بسيط فقط كمثال – تقدر تطوره لاحقًا
    short_ema = ema(closes, 12)
    long_ema = ema(closes, 26)
    macd = short_ema - long_ema
    signal = ema([macd for _ in range(9)], 9)  # تبسيط
    macd_bullish = macd > signal

    # RSI بسيط
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-diff)
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14 if sum(losses[-14:]) != 0 else 1e-9
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    score = 0
    if macd_bullish:
        score += 30
    if rsi < 40:
        score += 40
    elif rsi < 50:
        score += 20

    return {
        "symbol": symbol,
        "score": score,
        "rsi": round(rsi, 1),
        "macd_bullish": macd_bullish
    }

def open_long_with_sl_tp(symbol, entry_price, usdt_balance):
    try:
        risk_amount = usdt_balance * RISK_PER_TRADE
        notional = risk_amount * LEVERAGE
        quantity = notional / entry_price

        quantity = adjust_quantity(symbol, quantity)
        entry_price = adjust_price(symbol, entry_price)

        # أمر السوق لفتح صفقة
        order = client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=quantity
        )

        # حساب SL و TP
        stop_loss_price = adjust_price(symbol, entry_price * (1 - STOP_LOSS_PCT))
        take_profit_price = adjust_price(symbol, entry_price * (1 + TAKE_PROFIT_PCT))

        # أمر وقف خسارة
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=stop_loss_price,
            closePosition=True,
            timeInForce=TIME_IN_FORCE_GTC
        )

        # أمر جني أرباح
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL,
            type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
            stopPrice=take_profit_price,
            closePosition=True,
            timeInForce=TIME_IN_FORCE_GTC
        )

        msg = (
            f"🟢 تم فتح صفقة LONG\n"
            f"زوج: {symbol}\n"
            f"السعر: {entry_price}\n"
            f"الكمية: {quantity}\n"
            f"وقف الخسارة: {stop_loss_price}\n"
            f"جني الأرباح: {take_profit_price}\n"
            f"المخاطرة: {RISK_PER_TRADE*100:.1f}% | الرافعة: {LEVERAGE}x"
        )
        logging.info(msg)
        send_telegram(msg)

    except Exception as e:
        logging.error(f"Binance order error: {e}")
        send_telegram(f"⚠️ خطأ في فتح الصفقة لـ {symbol}:\n{e}")

# ================== تقرير يومي ==================

last_daily_report_date = None

def send_daily_report():
    global last_daily_report_date
    today = datetime.utcnow().date()
    if last_daily_report_date == today:
        return

    balance = get_futures_balance_usdt()
    positions = client.futures_position_information()
    open_positions = [p for p in positions if float(p["positionAmt"]) != 0]

    msg = "📊 تقرير يومي لبوت التداول\n"
    msg += f"التاريخ (UTC): {today}\n"
    msg += f"رصيد العقود الآجلة (USDT): {balance:.2f}\n"
    msg += "الصفقات المفتوحة:\n"
    if not open_positions:
        msg += "لا توجد صفقات مفتوحة حاليًا.\n"
    else:
        for p in open_positions:
            symbol = p["symbol"]
            amt = float(p["positionAmt"])
            entry = float(p["entryPrice"])
            upnl = float(p["unRealizedProfit"])
            msg += f"- {symbol} | كمية: {amt} | سعر الدخول: {entry} | ربح/خسارة غير محققة: {upnl:.2f}\n"

    send_telegram(msg)
    last_daily_report_date = today

# ================== الحلقة الرئيسية ==================

@app.route("/")
def home():
    return "Binance Trading Bot is running."

def main_loop():
    global last_daily_report_date

    logging.info("✅ تم الاتصال بـ Binance بنجاح!")
    logging.info("✅ Telegram جاهز")
    logging.info("✅ Trading Manager جاهز")
    logging.info("============================================================")
    logging.info("🤖 بوت التداول الذكي v4 - محسن!")
    logging.info("============================================================")
    send_telegram("🤖 بوت التداول الذكي v4 بدأ العمل بنجاح على Render ✅")

    cycle = 0
    while True:
        cycle += 1
        logging.info(f"🔄 الدورة #{cycle}")
        logging.info("🔍 البحث عن فرص شراء...")

        try:
            usdt_balance = get_futures_balance_usdt()
            logging.info(f"رصيد العقود الآجلة USDT: {usdt_balance:.2f}")

            candidates = []
            for symbol in TOP_SYMBOLS:
                # فلتر حجم تداول
                pass_vol, qv = pass_volume_filter(symbol)
                if not pass_vol:
                    logging.info(f"{symbol}: مرفوض بسبب حجم تداول ضعيف ({qv:.0f})")
                    continue

                # فلتر ترند
                uptrend, last_price, ema200 = get_trend_filter(symbol)
                if not uptrend:
                    logging.info(f"{symbol}: مرفوض – ليس في ترند صاعد (السعر={last_price}, EMA200={ema200})")
                    continue

                info = analyze_symbol(symbol)
                candidates.append(info)
                logging.info(
                    f"📈 {symbol}: {info['score']} نقطة - RSI={info['rsi']} | MACD {'صاعد' if info['macd_bullish'] else 'هابط'}"
                )

            if not candidates:
                logging.info("لا توجد فرص مناسبة في هذه الدورة.")
            else:
                # اختيار أفضل فرصة
                best = max(candidates, key=lambda x: x["score"])
                symbol = best["symbol"]
                logging.info(f"🎯 أفضل فرصة: {symbol} ({best['score']} نقطة)")

                # جلب آخر سعر
                ticker = client.futures_symbol_ticker(symbol=symbol)
                price = float(ticker["price"])

                logging.info(f"🟢 جاري فتح صفقة: {symbol} بسعر {price}")
                open_long_with_sl_tp(symbol, price, usdt_balance)

            # تقرير يومي مرة واحدة في اليوم (مثلاً بعد 23:00 UTC)
            now_utc = datetime.utcnow()
            if now_utc.hour >= 23:
                send_daily_report()

        except Exception as e:
            logging.error(f"خطأ في الدورة: {e}")
            send_telegram(f"⚠️ خطأ في الدورة:\n{e}")

        # انتظر دقيقة بين كل دورة (عدّلها لو حاب)
        time.sleep(60)

# ================== تشغيل Flask + البوت ==================

if __name__ == "__main__":
    # تشغيل الحلقة في Thread منفصل لو حاب، لكن على Render غالبًا يكفي كذا
    from threading import Thread

    t = Thread(target=main_loop, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
