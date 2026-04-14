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

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_BINANCE_API_SECRET")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

RISK_PER_TRADE = 0.05
LEVERAGE       = 20
TIMEFRAME      = "15m"

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
MIN_SCORE            = 30

# ✅ Trailing Stop: نسبة التراجع المسموح قبل الإغلاق (1% = 1.0)
TRAILING_CALLBACK_RATE = 1.0   # 1%

# ✅ Activation Price: تُفعَّل الـ Trailing فقط بعد ارتفاع السعر بهذه النسبة
TRAILING_ACTIVATION_PCT = 0.005  # 0.5% فوق سعر الدخول

# ================== حماية البوت ==================

DAILY_LOSS_LIMIT_PCT = 0.05
TOTAL_LOSS_LIMIT_PCT = 0.15

# ================== تهيئة العملاء ==================

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
bot    = telebot.TeleBot(TELEGRAM_TOKEN)
app    = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

_symbol_filters_cache = {}

# ================== متغيرات حماية البوت ==================

bot_start_balance   = None
daily_start_balance = None
daily_reset_date    = None
bot_halted_total    = False
bot_halted_daily    = False

# ================== متغيرات تتبع الصفقات المفتوحة ==================
# { symbol: { "entry_price": float, "quantity": float, "open_time": datetime } }
open_trades = {}

# ================== دوال مساعدة عامة ==================

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

def get_available_balance_usdt():
    acc = client.futures_account_balance()
    for b in acc:
        if b["asset"] == "USDT":
            return float(b["availableBalance"])
    return 0.0

def get_symbol_filters(symbol):
    """يجلب exchange_info مرة واحدة فقط ويخزّن الكل في الـ cache"""
    if symbol in _symbol_filters_cache:
        return _symbol_filters_cache[symbol]

    if not _symbol_filters_cache:
        info = client.futures_exchange_info()
        for s in info["symbols"]:
            sym          = s["symbol"]
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
    kl      = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
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
    ema200     = ema(closes, 200)
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
        "symbol":       symbol,
        "score":        score,
        "rsi":          round(rsi, 1),
        "macd_bullish": macd_bullish
    }

def estimate_notional(symbol, price, available_balance):
    _, _, min_notional = get_symbol_filters(symbol)
    risk_amount = available_balance * RISK_PER_TRADE
    notional    = risk_amount * LEVERAGE
    return notional, min_notional

# ================== تحليل الشموع اليابانية (1m / 5m / 15m) ==================

def analyze_candles(symbol, interval="5m", limit=5):
    """
    تحليل الشمعة الأخيرة:
    - Bullish / Bearish
    - Doji
    - Hammer
    - Shooting Star
    - Bullish Engulfing
    - Bearish Engulfing
    """
    kl = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    if len(kl) < 2:
        return "Neutral"

    o = float(kl[-1][1])
    h = float(kl[-1][2])
    l = float(kl[-1][3])
    c = float(kl[-1][4])

    body        = abs(c - o)
    range_total = h - l
    upper       = h - max(o, c)
    lower       = min(o, c) - l

    # Doji
    if range_total > 0 and body <= range_total * 0.1:
        return "Doji"

    # Hammer
    if lower > body * 2 and upper < body:
        return "Hammer"

    # Shooting Star
    if upper > body * 2 and lower < body:
        return "Shooting Star"

    # Engulfing (نحتاج الشمعة السابقة)
    prev_o = float(kl[-2][1])
    prev_c = float(kl[-2][4])

    # Bullish Engulfing
    if c > o and prev_c < prev_o and c > prev_o and o < prev_c:
        return "Bullish Engulfing"

    # Bearish Engulfing
    if c < o and prev_c > prev_o and o > prev_c and c < prev_o:
        return "Bearish Engulfing"

    # Bullish / Bearish عادية
    if c > o:
        return "Bullish"
    elif c < o:
        return "Bearish"
    else:
        return "Neutral"
# ================== إلغاء أوامر الحماية القديمة ==================

def cancel_existing_sl_tp(symbol):
    """
    يلغي أي أوامر STOP_MARKET أو TAKE_PROFIT_MARKET أو TRAILING_STOP_MARKET
    مفتوحة لرمز معين — ضروري قبل وضع trailing جديد
    """
    try:
        orders = client.futures_get_open_orders(symbol=symbol)
        cancelled = 0
        for order in orders:
            if order["type"] in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"):
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=order["orderId"])
                    cancelled += 1
                    logging.info(f"تم إلغاء أمر {order['type']} لـ {symbol} (ID: {order['orderId']})")
                except Exception as e:
                    logging.warning(f"فشل إلغاء أمر {symbol}: {e}")
        if cancelled > 0:
            logging.info(f"تم إلغاء {cancelled} أمر قديم لـ {symbol}")
    except Exception as e:
        logging.error(f"خطأ في cancel_existing_sl_tp لـ {symbol}: {e}")


# ================== وضع Trailing Stop ==================

def place_trailing_stop(symbol, entry_price, quantity):
    """
    يضع TRAILING_STOP_MARKET
    - callbackRate: نسبة التراجع المسموح (مثلاً 1%)
    - activationPrice: السعر الذي تبدأ منه المتابعة (0.5% فوق الدخول)
    """
    activation_price = adjust_price(
        symbol,
        entry_price * (1 + TRAILING_ACTIVATION_PCT)
    )

    try:
        client.futures_create_order(
            symbol          = symbol,
            side            = SIDE_SELL,
            type            = "TRAILING_STOP_MARKET",
            quantity        = quantity,
            callbackRate    = TRAILING_CALLBACK_RATE,
            activationPrice = activation_price,
            reduceOnly      = True,
            workingType     = "MARK_PRICE"
        )
        logging.info(
            f"✅ Trailing Stop وُضع لـ {symbol} | "
            f"تفعيل عند: {activation_price} | Callback: {TRAILING_CALLBACK_RATE}%"
        )
        return True

    except Exception as e:
        logging.error(f"فشل وضع Trailing Stop لـ {symbol}: {e}")
        send_telegram(
            f"⚠️ تحذير: تم فتح صفقة {symbol} لكن فشل وضع Trailing Stop!\n"
            f"الخطأ: {e}\n"
            f"يرجى المتابعة يدوياً."
        )
        return False


# ================== مراقبة الصفقات المفتوحة ==================

def monitor_open_trades():
    """
    يراقب الصفقات المفتوحة ويتحقق من:
    1. هل الصفقة أُغلقت (بواسطة الـ Trailing Stop)؟ → تنظيف + إشعار
    2. هل الـ Trailing Stop لا يزال موجوداً؟ → إذا اختفى يُعاد وضعه
    """
    global open_trades

    closed_symbols = []

    for symbol, trade_info in list(open_trades.items()):
        try:
            # فحص الوضعية الحالية
            positions = client.futures_position_information(symbol=symbol)
            position_amt = float(positions[0]["positionAmt"])

            # الصفقة أُغلقت
            if position_amt == 0:
                entry_price = trade_info["entry_price"]
                open_time   = trade_info["open_time"]
                duration    = datetime.utcnow() - open_time

                ticker      = client.futures_symbol_ticker(symbol=symbol)
                exit_price  = float(ticker["price"])
                pnl_pct     = ((exit_price - entry_price) / entry_price) * 100 * LEVERAGE

                msg = (
                    f"{'🟢' if pnl_pct >= 0 else '🔴'} صفقة مُغلقة بواسطة Trailing Stop\n"
                    f"زوج: {symbol}\n"
                    f"دخول: {entry_price}\n"
                    f"خروج: {exit_price}\n"
                    f"ربح/خسارة: {pnl_pct:+.2f}%\n"
                    f"مدة الصفقة: {str(duration).split('.')[0]}"
                )
                send_telegram(msg)
                logging.info(msg)

                closed_symbols.append(symbol)
                continue

            # الصفقة مفتوحة — تحقق من وجود Trailing Stop
            orders = client.futures_get_open_orders(symbol=symbol)
            has_trailing = any(o["type"] == "TRAILING_STOP_MARKET" for o in orders)

            if not has_trailing:
                logging.warning(f"⚠️ {symbol}: Trailing Stop مفقود! إعادة وضعه...")
                send_telegram(f"⚠️ {symbol}: Trailing Stop مفقود — إعادة وضعه...")

                ticker        = client.futures_symbol_ticker(symbol=symbol)
                current_price = float(ticker["price"])
                quantity      = abs(position_amt)

                cancel_existing_sl_tp(symbol)
                placed = place_trailing_stop(symbol, current_price, quantity)

                if placed:
                    send_telegram(f"✅ {symbol}: تم إعادة وضع Trailing Stop بنجاح.")
                    open_trades[symbol]["entry_price"] = current_price

        except Exception as e:
            logging.error(f"خطأ في مراقبة {symbol}: {e}")

    # تنظيف الصفقات المغلقة
    for symbol in closed_symbols:
        open_trades.pop(symbol, None)
        logging.info(f"تم حذف {symbol} من سجل الصفقات المفتوحة.")
# ================== فتح الصفقات ==================

def open_long_with_trailing(symbol, entry_price, available_balance):
    """
    v6 — فتح صفقة LONG + تقييم شموع + Trailing Stop ذكي
    لا يوجد Take Profit ثابت — البوت يُدير الصفقة بالـ Trailing
    """
    try:
        setup_symbol(symbol)

        lot_size, _, min_notional = get_symbol_filters(symbol)
        if lot_size is None:
            lot_size = 0.0

        # حساب المخاطرة
        risk_amount = available_balance * RISK_PER_TRADE
        notional    = risk_amount * LEVERAGE
        raw_qty     = notional / entry_price
        quantity    = adjust_quantity(symbol, raw_qty)

        # الحد الأدنى لقيمة الصفقة
        if quantity * entry_price < min_notional:
            msg = f"قيمة الصفقة لـ {symbol} أقل من {min_notional} USDT — إلغاء الصفقة."
            logging.warning(msg)
            send_telegram(f"⚠️ {msg}")
            return False

        # محاولة استخدام أقل كمية مسموحة
        if quantity <= 0 and lot_size > 0:
            if lot_size * entry_price <= notional:
                quantity = adjust_quantity(symbol, lot_size)
            else:
                send_telegram(f"⚠️ لا يمكن فتح صفقة {symbol} — الكمية أقل من الحد الأدنى.")
                return False

        if quantity <= 0:
            send_telegram(f"⚠️ الكمية صفر بعد التقريب — إلغاء صفقة {symbol}.")
            return False

        entry_price_adjusted = adjust_price(symbol, entry_price)

        # ================== أمر الدخول مع retry ==================
        max_retries  = 6
        order_placed = False

        for attempt in range(max_retries):
            try:
                client.futures_create_order(
                    symbol=symbol,
                    side=SIDE_BUY,
                    type="MARKET",
                    quantity=quantity
                )
                order_placed = True
                break

            except Exception as e:
                err_str = str(e)
                if "Margin is insufficient" in err_str:
                    quantity = adjust_quantity(symbol, quantity * 0.7)
                    logging.warning(
                        f"الهامش غير كافٍ لـ {symbol} — تقليل الكمية إلى {quantity} "
                        f"(محاولة {attempt + 1}/{max_retries})"
                    )
                    if quantity * entry_price_adjusted < min_notional:
                        send_telegram(
                            f"❌ فشل فتح صفقة {symbol} — الكمية وصلت للحد الأدنى ({min_notional} USDT)."
                        )
                        return False
                else:
                    raise e

        if not order_placed:
            send_telegram(f"❌ فشل فتح صفقة {symbol} — استُنفدت كل المحاولات.")
            return False

        # ================== تحليل الشموع اليابانية ==================

        c1  = analyze_candles(symbol, "1m", 5)
        c5  = analyze_candles(symbol, "5m", 5)
        c15 = analyze_candles(symbol, "15m", 5)

        reversal_patterns = [
            "Hammer", "Bullish Engulfing",
            "Shooting Star", "Bearish Engulfing",
            "Doji"
        ]

        bearish_strong = ["Bearish Engulfing", "Shooting Star"]

        # تنبيه شمعة انعكاسية
        if c1 in reversal_patterns or c5 in reversal_patterns or c15 in reversal_patterns:
            send_telegram(
                f"⚠️ تنبيه شمعة انعكاسية على {symbol}:\n"
                f"1m={c1} | 5m={c5} | 15m={c15}"
            )

        # إغلاق فوري عند شموع هبوط قوية
        if c1 in bearish_strong and c5 in bearish_strong and c15 in bearish_strong:
            send_telegram(
                f"❌ إغلاق تلقائي لصفقة {symbol} — شموع هبوط قوية على 3 فريمات."
            )
            client.futures_create_order(
                symbol=symbol,
                side=SIDE_SELL,
                type="MARKET",
                reduceOnly=True
            )
            return False

        # ================== Trailing Stop بعد الدخول ==================

        cancel_existing_sl_tp(symbol)
        trailing_placed = place_trailing_stop(symbol, entry_price_adjusted, quantity)

        # ================== تسجيل الصفقة ==================

        open_trades[symbol] = {
            "entry_price": entry_price_adjusted,
            "quantity":    quantity,
            "open_time":   datetime.utcnow()
        }

        # ================== رسالة النجاح ==================

        msg = (
            f"🟢 تم فتح صفقة LONG (v6 - Trailing + شموع)\n"
            f"زوج: {symbol}\n"
            f"سعر الدخول: {entry_price_adjusted}\n"
            f"الكمية: {quantity}\n\n"
            f"📊 تقييم الشموع:\n"
            f"• 1m: {c1}\n"
            f"• 5m: {c5}\n"
            f"• 15m: {c15}\n\n"
            f"Trailing Stop: {TRAILING_CALLBACK_RATE}% | "
            f"Activation: +{TRAILING_ACTIVATION_PCT*100:.1f}%\n"
            f"المخاطرة: {RISK_PER_TRADE*100:.1f}% | الرافعة: {LEVERAGE}x\n"
            f"{'✅ Trailing Stop وُضع بنجاح' if trailing_placed else '⚠️ فشل وضع Trailing Stop!'}"
        )

        send_telegram(msg)
        logging.info(msg)
        return True

    except Exception as e:
        logging.error(f"Binance order error: {e}")
        send_telegram(f"⚠️ خطأ في فتح الصفقة لـ {symbol}:\n{e}")
        return False
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

            # إلغاء أوامر الحماية قبل الإغلاق
            cancel_existing_sl_tp(symbol)

            try:
                client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type="MARKET",
                    quantity=quantity,
                    reduceOnly=True
                )
                closed += 1
                open_trades.pop(symbol, None)
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

    # توقف نهائي
    if bot_halted_total:
        logging.warning("البوت متوقف نهائيًا (تجاوز حد الخسارة الإجمالية).")
        return False

    today = datetime.utcnow().date()

    # يوم جديد
    if daily_reset_date != today:
        reset_daily_state(current_balance)

    # حماية يومية
    if daily_start_balance and daily_start_balance > 0:
        daily_loss_pct = (daily_start_balance - current_balance) / daily_start_balance

        if daily_loss_pct >= DAILY_LOSS_LIMIT_PCT:
            if not bot_halted_daily:
                bot_halted_daily = True
                close_all_positions()

                msg = (
                    f"🛑 تم تفعيل وقف الخسارة اليومي!\n"
                    f"الخسارة اليومية: {daily_loss_pct*100:.2f}%\n"
                    f"رصيد البداية: {daily_start_balance:.2f} USDT\n"
                    f"الرصيد الحالي: {current_balance:.2f} USDT\n"
                    f"سيستأنف البوت غدًا تلقائيًا."
                )
                send_telegram(msg)
                logging.warning(msg)

            return False

    # حماية إجمالية
    if bot_start_balance and bot_start_balance > 0:
        total_loss_pct = (bot_start_balance - current_balance) / bot_start_balance

        if total_loss_pct >= TOTAL_LOSS_LIMIT_PCT:
            bot_halted_total = True
            close_all_positions()

            msg = (
                f"🚨 تم تفعيل وقف الخسارة الإجمالي النهائي!\n"
                f"الخسارة الإجمالية: {total_loss_pct*100:.2f}%\n"
                f"رصيد البداية: {bot_start_balance:.2f} USDT\n"
                f"الرصيد الحالي: {current_balance:.2f} USDT\n"
                f"البوت متوقف نهائيًا — يرجى المراجعة."
            )
            send_telegram(msg)
            logging.critical(msg)
            return False

    return True


# ================== التقرير اليومي ==================

last_daily_report_date = None

def send_daily_report(total_balance):
    global last_daily_report_date

    today = datetime.utcnow().date()
    if last_daily_report_date == today:
        return

    positions      = client.futures_position_information()
    open_positions = [p for p in positions if float(p["positionAmt"]) != 0]

    daily_loss = (
        ((daily_start_balance or total_balance) - total_balance)
        / (daily_start_balance or total_balance) * 100
        if (daily_start_balance or total_balance) > 0 else 0
    )

    total_loss = (
        ((bot_start_balance or total_balance) - total_balance)
        / (bot_start_balance or total_balance) * 100
        if (bot_start_balance or total_balance) > 0 else 0
    )

    msg  = "📊 تقرير يومي — بوت التداول v6\n"
    msg += f"التاريخ (UTC): {today}\n"
    msg += f"رصيد USDT: {total_balance:.2f}\n"
    msg += f"خسارة اليوم: {daily_loss:.2f}% | إجمالي: {total_loss:.2f}%\n"
    msg += f"Trailing Stop: {TRAILING_CALLBACK_RATE}%\n"
    msg += "الصفقات المفتوحة:\n"

    if not open_positions:
        msg += "لا توجد صفقات مفتوحة.\n"
    else:
        for p in open_positions:
            sym   = p["symbol"]
            amt   = float(p["positionAmt"])
            entry = float(p["entryPrice"])
            upnl  = float(p["unRealizedProfit"])
            msg  += f"- {sym} | كمية: {amt} | دخول: {entry} | ر/خ: {upnl:.2f} USDT\n"

    send_telegram(msg)
    last_daily_report_date = today


# ================== الحلقة الرئيسية ==================

@app.route("/")
def home():
    return "Binance Trailing Bot v6 is running."


def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date

    logging.info("تم الاتصال بـ Binance بنجاح!")
    logging.info("بوت التداول الذكي v6 — Trailing Stop + شموع")

    initial_balance     = get_futures_balance_usdt()
    bot_start_balance   = initial_balance
    daily_start_balance = initial_balance
    daily_reset_date    = datetime.utcnow().date()

    send_telegram(
        f"🤖 بوت التداول v6 بدأ العمل بنجاح\n"
        f"رصيد البداية: {initial_balance:.2f} USDT\n"
        f"Trailing Callback: {TRAILING_CALLBACK_RATE}% | "
        f"Activation: +{TRAILING_ACTIVATION_PCT*100:.1f}%\n"
        f"حد الخسارة اليومي: {DAILY_LOSS_LIMIT_PCT*100:.0f}% | "
        f"الحد الإجمالي: {TOTAL_LOSS_LIMIT_PCT*100:.0f}%"
    )

    cycle = 0

    while True:
        cycle += 1
        logging.info(f"الدورة #{cycle} — البحث عن فرص + مراقبة الصفقات...")

        try:
            total_balance     = get_futures_balance_usdt()
            available_balance = get_available_balance_usdt()

            logging.info(f"رصيد USDT: {total_balance:.2f} | متاح: {available_balance:.2f}")

            # مراقبة الصفقات المفتوحة
            if open_trades:
                monitor_open_trades()

            # حماية البوت
            if not check_bot_protection(total_balance):
                logging.info("التداول محظور — تخطي الدورة.")
                time.sleep(40)
                continue

            if available_balance <= 0:
                logging.info("لا يوجد هامش متاح — جميع الصفقات مفتوحة.")
                time.sleep(40)
                continue

            # ================== جمع المرشحين ==================
            candidates = []

            for symbol in TOP_SYMBOLS:

                if has_open_position(symbol):
                    logging.info(f"{symbol}: متجاوز — صفقة مفتوحة.")
                    continue

                pass_vol, qv = pass_volume_filter(symbol)
                if not pass_vol:
                    logging.info(f"{symbol}: مرفوض — حجم تداول ضعيف ({qv:.0f})")
                    continue

                uptrend, last_price, ema200 = get_trend_filter(symbol)
                if not uptrend:
                    logging.info(
                        f"{symbol}: مرفوض — ليس في ترند صاعد "
                        f"(السعر={last_price:.4f}, EMA200={ema200:.4f})"
                    )
                    continue

                info = analyze_symbol(symbol)

                if info["score"] < MIN_SCORE:
                    logging.info(
                        f"{symbol}: مرفوض — نقاط ضعيفة ({info['score']} < {MIN_SCORE})"
                    )
                    continue

                ticker = client.futures_symbol_ticker(symbol=symbol)
                price  = float(ticker["price"])

                notional, min_notional = estimate_notional(symbol, price, available_balance)
                if notional < min_notional:
                    logging.info(
                        f"{symbol}: مرفوض — قيمة الصفقة {notional:.2f} < {min_notional} USDT"
                    )
                    continue

                info["price"] = price
                candidates.append(info)

                logging.info(
                    f"{symbol}: {info['score']} نقطة — "
                    f"RSI={info['rsi']} | MACD={'صاعد' if info['macd_bullish'] else 'هابط'}"
                )

            # ================== اختيار الأفضل ==================
            if not candidates:
                logging.info("لا توجد فرص مناسبة.")
            else:
                candidates.sort(key=lambda x: (-x["score"], x["rsi"]))

                order_opened = False

                for best in candidates:
                    symbol = best["symbol"]
                    price  = best["price"]

                    logging.info(
                        f"محاولة فتح صفقة: {symbol} "
                        f"({best['score']} نقطة | RSI={best['rsi']})"
                    )

                    success = open_long_with_trailing(symbol, price, available_balance)

                    if success:
                        order_opened = True
                        break

                if not order_opened:
                    logging.info("فشلت كل المحاولات في هذه الدورة.")

            # ================== التقرير اليومي ==================
            now_utc = datetime.utcnow()
            if now_utc.hour == 23 and now_utc.minute == 0:
                send_daily_report(total_balance)

        except Exception as e:
            logging.error(f"خطأ في الدورة: {e}")
            send_telegram(f"⚠️ خطأ في الدورة:\n{e}")

        time.sleep(40)
