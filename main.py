import os
import time
import math
import logging
from datetime import datetime

from binance.client import Client
from binance.enums import *
from flask import Flask
import telebot

# ================== الإعدادات العامة ==================

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_BINANCE_API_SECRET")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

RISK_PER_TRADE = 0.05
LEVERAGE = 20
TIMEFRAME = "15m"

TOP_SYMBOLS = [
    "DOGEUSDT",
    "1000SHIBUSDT",
    "POLUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "LTCUSDT",
    "LINKUSDT"
]

MIN_24H_QUOTE_VOLUME = 1_000_000

STOP_LOSS_PCT   = 0.01
TAKE_PROFIT_PCT = 0.02

# ================== حماية البوت ==================

DAILY_LOSS_LIMIT_PCT = 0.05
TOTAL_LOSS_LIMIT_PCT = 0.15

# ================== تهيئة العملاء ==================

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

_symbol_filters_cache = {}

# ================== متغيرات حماية البوت ==================

bot_start_balance   = None
daily_start_balance = None
daily_reset_date    = None
bot_halted_total    = False
bot_halted_daily    = False

# ================== دوال مساعدة ==================

def send_telegram(msg: str):
    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg)
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def get_futures_balance_usdt():
    """الرصيد الكلي — للحماية والتقارير"""
    acc = client.futures_account_balance()
    for b in acc:
        if b["asset"] == "USDT":
            return float(b["balance"])
    return 0.0

def get_available_balance_usdt():
    """الهامش الحر فقط — لحساب حجم الصفقة الجديدة"""
    acc = client.futures_account_balance()
    for b in acc:
        if b["asset"] == "USDT":
            return float(b["availableBalance"])
    return 0.0

def get_symbol_filters(symbol):
    """يُعيد (lot_size, tick_size, min_notional)"""
    if symbol in _symbol_filters_cache:
        return _symbol_filters_cache[symbol]

    info = client.futures_exchange_info()
    for s in info["symbols"]:
        sym = s["symbol"]
        lot_size     = None
        tick_size    = None
        min_notional = 5.0
        for f in s["filters"]:
            if f["filterType"] == "LOT_SIZE":
                lot_size = float(f["stepSize"])
            if f["filterType"] == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
            if f["filterType"] == "MIN_NOTIONAL":
                min_notional = float(f["notional"])
        _symbol_filters_cache[sym] = (lot_size, tick_size, min_notional)

    return _symbol_filters_cache.get(symbol, (None, None, 5.0))

def get_klines(symbol, interval, limit=100):
    kl = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    closes  = [float(k[4]) for k in kl]
    volumes = [float(k[7]) for k in kl]
    return closes, volumes

def adjust_quantity(symbol, quantity):
    lot_size, _, _ = get_symbol_filters(symbol)
    if lot_size is None or lot_size == 0:
        return float(f"{quantity:.3f}")
    precision = int(round(-math.log(lot_size, 10)))
    return float(f"{quantity:.{precision}f}")

def adjust_price(symbol, price):
    _, tick_size, _ = get_symbol_filters(symbol)
    if tick_size is None or tick_size == 0:
        return float(f"{price:.2f}")
    precision = int(round(-math.log(tick_size, 10)))
    return float(f"{price:.{precision}f}")

def ema(values, period):
    if len(values) < period:
        return sum(values) / len(values)
    ema_val = sum(values[:period]) / period
    k = 2 / (period + 1)
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def compute_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return 0, 0, False
    k_fast = 2 / (fast + 1)
    k_slow = 2 / (slow + 1)
    ema_fast = sum(closes[:fast]) / fast
    ema_slow = sum(closes[:slow]) / slow
    macd_line = []
    for i in range(slow, len(closes)):
        ema_fast = closes[i] * k_fast + ema_fast * (1 - k_fast)
        ema_slow = closes[i] * k_slow + ema_slow * (1 - k_slow)
        macd_line.append(ema_fast - ema_slow)
    signal_val = ema(macd_line, signal)
    macd_val   = macd_line[-1]
    return macd_val, signal_val, macd_val > signal_val

def get_trend_filter(symbol):
    closes, _ = get_klines(symbol, "1h", 210)
    last_price = closes[-1]
    ema200 = ema(closes, 200)
    return last_price >= ema200 * 0.98, last_price, ema200

def pass_volume_filter(symbol):
    try:
        tick = client.futures_ticker(symbol=symbol)
        if not isinstance(tick, dict):
            return False, 0
        if "code" in tick:
            return False, 0
        quote_volume = float(tick.get("quoteVolume", 0))
        return quote_volume >= MIN_24H_QUOTE_VOLUME, quote_volume
    except Exception as e:
        logging.warning(f"خطأ في pass_volume_filter لـ {symbol}: {e}")
        return False, 0

def has_open_position(symbol):
    try:
        positions = client.futures_position_information(symbol=symbol)
        for p in positions:
            if float(p["positionAmt"]) != 0:
                return True
        return False
    except Exception as e:
        logging.warning(f"خطأ في فحص الصفقات المفتوحة لـ {symbol}: {e}")
        return True

def setup_symbol(symbol):
    try:
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
    except Exception as e:
        logging.warning(f"تعذّر ضبط الرافعة لـ {symbol}: {e}")

def analyze_symbol(symbol):
    closes, _ = get_klines(symbol, TIMEFRAME, 150)
    macd_val, signal_val, macd_bullish = compute_macd(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-diff)
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14 if sum(losses[-14:]) != 0 else 1e-9
    rs  = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    score = 0
    if macd_bullish:
        score += 30
    if rsi < 60:
        score += 30
    elif rsi < 70:
        score += 10
    return {
        "symbol": symbol,
        "score": score,
        "rsi": round(rsi, 1),
        "macd_bullish": macd_bullish
    }

# ================== حماية البوت ==================

def close_all_positions():
    try:
        positions = client.futures_position_information()
        closed = 0
        for p in positions:
            amt = float(p["positionAmt"])
            if amt == 0:
                continue
            symbol   = p["symbol"]
            side     = SIDE_SELL if amt > 0 else SIDE_BUY
            quantity = abs(amt)
            try:
                client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type="MARKET",
                    quantity=quantity,
                    reduceOnly=True
                )
                closed += 1
                logging.info(f"تم إغلاق صفقة {symbol} (الكمية: {quantity})")
            except Exception as e:
                logging.error(f"فشل إغلاق {symbol}: {e}")
        if closed > 0:
            send_telegram(f"🔴 تم إغلاق {closed} صفقة مفتوحة بسبب تفعيل حماية البوت.")
    except Exception as e:
        logging.error(f"خطأ في close_all_positions: {e}")

def reset_daily_state(current_balance):
    global daily_start_balance, daily_reset_date, bot_halted_daily
    daily_start_balance = current_balance
    daily_reset_date    = datetime.utcnow().date()
    bot_halted_daily    = False
    logging.info(f"يوم جديد — رصيد البداية اليومي: {current_balance:.2f} USDT")
    send_telegram(
        f"✅ بداية يوم جديد (UTC) — استئناف التداول\n"
        f"رصيد اليوم: {current_balance:.2f} USDT"
    )

def check_bot_protection(current_balance) -> bool:
    global bot_halted_total, bot_halted_daily, daily_start_balance, daily_reset_date

    if bot_halted_total:
        logging.warning("البوت متوقف نهائيًا (تجاوز حد الخسارة الإجمالية 15%).")
        return False

    today = datetime.utcnow().date()
    if daily_reset_date != today:
        reset_daily_state(current_balance)

    # حماية من القسمة على صفر
    if daily_start_balance and daily_start_balance > 0:
        daily_loss_pct = (daily_start_balance - current_balance) / daily_start_balance
        if daily_loss_pct >= DAILY_LOSS_LIMIT_PCT:
            if not bot_halted_daily:
                bot_halted_daily = True
                close_all_positions()
                msg = (
                    f"🛑 تم تفعيل وقف الخسارة اليومي!\n"
                    f"الخسارة اليومية: {daily_loss_pct*100:.2f}% (الحد: {DAILY_LOSS_LIMIT_PCT*100:.0f}%)\n"
                    f"رصيد البداية اليوم: {daily_start_balance:.2f} USDT\n"
                    f"الرصيد الحالي: {current_balance:.2f} USDT\n"
                    f"سيستأنف البوت تلقائيًا غدًا."
                )
                logging.warning(msg)
                send_telegram(msg)
            return False

    if bot_start_balance and bot_start_balance > 0:
        total_loss_pct = (bot_start_balance - current_balance) / bot_start_balance
        if total_loss_pct >= TOTAL_LOSS_LIMIT_PCT:
            bot_halted_total = True
            close_all_positions()
            msg = (
                f"🚨 تم تفعيل وقف الخسارة الإجمالي النهائي!\n"
                f"الخسارة الإجمالية: {total_loss_pct*100:.2f}% (الحد: {TOTAL_LOSS_LIMIT_PCT*100:.0f}%)\n"
                f"رصيد البداية: {bot_start_balance:.2f} USDT\n"
                f"الرصيد الحالي: {current_balance:.2f} USDT\n"
                f"البوت متوقف نهائيًا — يرجى المراجعة اليدوية."
            )
            logging.critical(msg)
            send_telegram(msg)
            return False

    return True

# ================== فتح الصفقات ==================

def open_long_with_sl_tp(symbol, entry_price, available_balance):
    try:
        setup_symbol(symbol)

        lot_size, _, min_notional = get_symbol_filters(symbol)
        if lot_size is None:
            lot_size = 0.0

        risk_amount = available_balance * RISK_PER_TRADE
        notional    = risk_amount * LEVERAGE
        raw_qty     = notional / entry_price
        quantity    = adjust_quantity(symbol, raw_qty)

        if quantity * entry_price < min_notional:
            msg = f"قيمة الصفقة لـ {symbol} أقل من {min_notional} USDT — إلغاء الصفقة."
            logging.warning(msg)
            send_telegram(f"⚠️ {msg}")
            return

        if quantity <= 0 and lot_size > 0:
            if lot_size * entry_price <= notional:
                quantity = adjust_quantity(symbol, lot_size)
            else:
                send_telegram(f"⚠️ لا يمكن فتح صفقة {symbol} — الكمية أقل من الحد الأدنى.")
                return

        if quantity <= 0:
            send_telegram(f"⚠️ الكمية صفر بعد التقريب — إلغاء صفقة {symbol}.")
            return

        entry_price = adjust_price(symbol, entry_price)

        max_retries = 6
        attempt = 0
        while attempt < max_retries:
            try:
                client.futures_create_order(
                    symbol=symbol,
                    side=SIDE_BUY,
                    type="MARKET",
                    quantity=quantity
                )
                break
            except Exception as e:
                if "Margin is insufficient" in str(e):
                    quantity = adjust_quantity(symbol, quantity * 0.7)
                    attempt += 1
                    logging.warning(f"الهامش غير كافٍ لـ {symbol} — تقليل الكمية إلى {quantity}")
                    if quantity * entry_price < min_notional:
                        send_telegram(
                            f"❌ فشل فتح صفقة {symbol} — الكمية وصلت للحد الأدنى ({min_notional} USDT)."
                        )
                        return
                else:
                    raise e

        if attempt == max_retries:
            send_telegram(f"❌ فشل فتح صفقة {symbol} — الهامش غير كافٍ حتى بعد تقليل الكمية.")
            return

        stop_loss_price   = adjust_price(symbol, entry_price * (1 - STOP_LOSS_PCT))
        take_profit_price = adjust_price(symbol, entry_price * (1 + TAKE_PROFIT_PCT))

        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL,
            type="STOP_MARKET",
            stopPrice=stop_loss_price,
            closePosition=True
        )

        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL,
            type="TAKE_PROFIT_MARKET",
            stopPrice=take_profit_price,
            closePosition=True
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

def send_daily_report(total_balance):
    global last_daily_report_date
    today = datetime.utcnow().date()
    if last_daily_report_date == today:
        return

    positions      = client.futures_position_information()
    open_positions = [p for p in positions if float(p["positionAmt"]) != 0]

    daily_loss = ((daily_start_balance or total_balance) - total_balance) / (daily_start_balance or total_balance) * 100 if (daily_start_balance or total_balance) > 0 else 0
    total_loss = ((bot_start_balance  or total_balance) - total_balance) / (bot_start_balance  or total_balance) * 100 if (bot_start_balance  or total_balance) > 0 else 0

    msg  = "📊 تقرير يومي لبوت التداول\n"
    msg += f"التاريخ (UTC): {today}\n"
    msg += f"رصيد العقود الآجلة (USDT): {total_balance:.2f}\n"
    msg += f"خسارة اليوم: {daily_loss:.2f}% | خسارة إجمالية: {total_loss:.2f}%\n"
    msg += "الصفقات المفتوحة:\n"
    if not open_positions:
        msg += "لا توجد صفقات مفتوحة حاليًا.\n"
    else:
        for p in open_positions:
            symbol = p["symbol"]
            amt    = float(p["positionAmt"])
            entry  = float(p["entryPrice"])
            upnl   = float(p["unRealizedProfit"])
            msg += f"- {symbol} | كمية: {amt} | سعر الدخول: {entry} | ربح/خسارة: {upnl:.2f}\n"

    send_telegram(msg)
    last_daily_report_date = today

# ================== الحلقة الرئيسية ==================

@app.route("/")
def home():
    return "Binance Trading Bot is running."

def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date

    logging.info("تم الاتصال بـ Binance بنجاح!")
    logging.info("بوت التداول الذكي v4 - محسن مع حماية البوت!")

    initial_balance     = get_futures_balance_usdt()
    bot_start_balance   = initial_balance
    daily_start_balance = initial_balance
    daily_reset_date    = datetime.utcnow().date()

    send_telegram(
        f"🤖 بوت التداول الذكي v4 بدأ العمل بنجاح على Render ✅\n"
        f"رصيد البداية: {initial_balance:.2f} USDT\n"
        f"حد خسارة يومي: {DAILY_LOSS_LIMIT_PCT*100:.0f}% | حد خسارة إجمالي: {TOTAL_LOSS_LIMIT_PCT*100:.0f}%"
    )

    cycle = 0
    while True:
        cycle += 1
        logging.info(f"الدورة #{cycle} — البحث عن فرص شراء...")

        try:
            total_balance     = get_futures_balance_usdt()      # للحماية
            available_balance = get_available_balance_usdt()    # لحساب الصفقة

            logging.info(f"رصيد USDT: {total_balance:.2f} | متاح: {available_balance:.2f}")

            if not check_bot_protection(total_balance):
                logging.info("التداول محظور — تخطي هذه الدورة.")
                time.sleep(40)
                continue

            # إذا لا يوجد هامش متاح — تخطي دون خطأ
            if available_balance <= 0:
                logging.info("لا يوجد هامش متاح للتداول — جميع الصفقات مفتوحة.")
                time.sleep(40)
                continue

            candidates = []
            for symbol in TOP_SYMBOLS:

                if has_open_position(symbol):
                    logging.info(f"{symbol}: متجاوز — صفقة مفتوحة بالفعل.")
                    continue

                pass_vol, qv = pass_volume_filter(symbol)
                if not pass_vol:
                    logging.info(f"{symbol}: مرفوض — حجم تداول ضعيف ({qv:.0f})")
                    continue

                uptrend, last_price, ema200 = get_trend_filter(symbol)
                if not uptrend:
                    logging.info(f"{symbol}: مرفوض — ليس في ترند صاعد (السعر={last_price:.4f}, EMA200={ema200:.4f})")
                    continue

                info = analyze_symbol(symbol)
                candidates.append(info)
                logging.info(
                    f"{symbol}: {info['score']} نقطة — RSI={info['rsi']} | MACD={'صاعد' if info['macd_bullish'] else 'هابط'}"
                )

            if not candidates:
                logging.info("لا توجد فرص مناسبة في هذه الدورة.")
            else:
                best   = max(candidates, key=lambda x: x["score"])
                symbol = best["symbol"]
                logging.info(f"أفضل فرصة: {symbol} ({best['score']} نقطة)")

                ticker = client.futures_symbol_ticker(symbol=symbol)
                price  = float(ticker["price"])

                logging.info(f"جاري فتح صفقة: {symbol} بسعر {price}")
                open_long_with_sl_tp(symbol, price, available_balance)

            now_utc = datetime.utcnow()
            if now_utc.hour >= 23:
                send_daily_report(total_balance)

        except Exception as e:
            logging.error(f"خطأ في الدورة: {e}")
            send_telegram(f"⚠️ خطأ في الدورة:\n{e}")

        time.sleep(40)

# ================== تشغيل Flask + البوت ==================

if __name__ == "__main__":
    from threading import Thread

    t = Thread(target=main_loop, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
