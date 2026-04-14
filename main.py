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
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

RISK_PER_TRADE  = 0.05   # 5% من الرصيد
LEVERAGE        = 20
TIMEFRAME       = "15m"
MAX_OPEN_TRADES = 3      # أقصى عدد صفقات متزامنة

# 🛡️ إعدادات الحماية
STOP_LOSS_PCT           = 0.02   # 2% وقف خسارة ثابت كشبكة أمان
TRAILING_CALLBACK_RATE  = 1.0    # 1% trailing
TRAILING_ACTIVATION_PCT = 0.005  # يُفعَّل بعد +0.5% من الدخول

# حماية الرصيد
DAILY_LOSS_LIMIT_PCT = 0.05   # 5% يومي
TOTAL_LOSS_LIMIT_PCT = 0.15   # 15% إجمالي

TOP_SYMBOLS          = ["DOGEUSDT", "XRPUSDT", "SOLUSDT", "LTCUSDT", "LINKUSDT", "DOGEUSDT", "POLUSDT"]
MIN_24H_QUOTE_VOLUME = 1_000_000
MIN_SCORE            = 35

# ================== 2. تهيئة النظام ==================

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
bot    = telebot.TeleBot(TELEGRAM_TOKEN)
app    = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

_symbol_filters_cache = {}

# سجل الصفقات المفتوحة
# { symbol: { "entry": float, "qty": float, "open_time": datetime } }
open_trades: dict = {}

# متغيرات الحماية
bot_start_balance   = None
daily_start_balance = None
daily_reset_date    = None
bot_halted_total    = False
bot_halted_daily    = False

# ================== 3. الدوال المساعدة ==================

def send_telegram(msg: str):
    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def utcnow():
    return datetime.now(timezone.utc)

def get_futures_balance():
    """الرصيد الكلي للعقود الآجلة فقط"""
    for b in client.futures_account_balance():
        if b["asset"] == "USDT":
            return float(b["balance"])
    return 0.0

def get_available_balance():
    """الهامش المتاح للعقود الآجلة"""
    acc = client.futures_account()
    return float(acc["availableBalance"])

# ================== 4. فلاتر الرموز ==================

def get_symbol_filters(symbol):
    if symbol in _symbol_filters_cache:
        return _symbol_filters_cache[symbol]
    # جلب الكل دفعة واحدة
    if not _symbol_filters_cache:
        for s in client.futures_exchange_info()["symbols"]:
            sym = s["symbol"]
            lot = tick = None
            notional = 5.0
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    lot = float(f["stepSize"])
                elif f["filterType"] == "PRICE_FILTER":
                    tick = float(f["tickSize"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    notional = float(f["notional"])
            if lot and tick:
                _symbol_filters_cache[sym] = (lot, tick, notional)
    return _symbol_filters_cache.get(symbol, (0.001, 0.01, 5.0))

def adjust_quantity(symbol, qty):
    lot, _, _ = get_symbol_filters(symbol)
    if lot <= 0:
        return round(qty, 3)
    precision = max(0, int(round(-math.log10(lot))))
    return float(f"{qty:.{precision}f}")

def adjust_price(symbol, price):
    _, tick, _ = get_symbol_filters(symbol)
    if tick <= 0:
        return round(price, 2)
    precision = max(0, int(round(-math.log10(tick))))
    return float(f"{price:.{precision}f}")

def has_futures_position(symbol) -> bool:
    """فحص وجود وضعية مفتوحة في العقود الآجلة"""
    try:
        positions = client.futures_position_information(symbol=symbol)
        for p in positions:
            if abs(float(p["positionAmt"])) > 0:
                return True
    except Exception as e:
        logging.warning(f"خطأ فحص وضعية {symbol}: {e}")
        return True   # احتياط: افترض مفتوحة
    return False

# ================== 5. المؤشرات الفنية ==================

def ema(values, period):
    if len(values) < period:
        return sum(values) / len(values)
    k = 2 / (period + 1)
    v = sum(values[:period]) / period
    for x in values[period:]:
        v = x * k + v * (1 - k)
    return v

def compute_rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period or 1e-9
    return 100 - 100 / (1 + ag / al)

def compute_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return 0, 0, False
    k_f, k_s = 2/(fast+1), 2/(slow+1)
    ef = es = closes[0]
    macd_line = []
    for c in closes:
        ef = c * k_f + ef * (1 - k_f)
        es = c * k_s + es * (1 - k_s)
        macd_line.append(ef - es)
    sig = ema(macd_line, signal)
    return macd_line[-1], sig, macd_line[-1] > sig

def score_symbol(symbol):
    """يُعيد نقاط التقييم أو None إذا فشل جلب البيانات"""
    try:
        klines = client.futures_klines(symbol=symbol, interval=TIMEFRAME, limit=150)
        closes = [float(k[4]) for k in klines]

        _, _, macd_bull = compute_macd(closes)
        rsi             = compute_rsi(closes)

        # فلتر الترند: السعر فوق EMA200 (ساعة)
        h_klines    = client.futures_klines(symbol=symbol, interval="1h", limit=210)
        h_closes    = [float(k[4]) for k in h_klines]
        ema200      = ema(h_closes, 200)
        in_uptrend  = h_closes[-1] >= ema200 * 0.98

        if not in_uptrend:
            return None

        # فلتر الحجم
        ticker = client.futures_ticker(symbol=symbol)
        if float(ticker.get("quoteVolume", 0)) < MIN_24H_QUOTE_VOLUME:
            return None

        sc = 0
        if macd_bull:    sc += 30
        if rsi < 60:     sc += 30
        elif rsi < 70:   sc += 10

        return {
            "symbol": symbol,
            "score":  sc,
            "rsi":    round(rsi, 1),
            "price":  float(ticker["lastPrice"])
        }
    except Exception as e:
        logging.warning(f"خطأ تقييم {symbol}: {e}")
        return None

# ================== 6. إدارة أوامر الحماية ==================

def cancel_protection_orders(symbol):
    """يلغي أوامر SL/TP/Trailing القديمة فقط — لا يلغي أوامر الدخول"""
    try:
        orders = client.futures_get_open_orders(symbol=symbol)
        for o in orders:
            if o["type"] in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"):
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                    logging.info(f"إلغاء أمر {o['type']} لـ {symbol}")
                except Exception as e:
                    logging.warning(f"فشل إلغاء {o['type']} لـ {symbol}: {e}")
    except Exception as e:
        logging.error(f"خطأ cancel_protection_orders {symbol}: {e}")

def place_protection_orders(symbol, actual_entry, actual_qty):
    """
    ✅ الإصلاح الجوهري:
    يستخدم actual_qty (الكمية الفعلية من الوضعية) وليس qty المحسوبة
    يضع SL ثابت + Trailing Stop معاً
    """
    if actual_qty <= 0:
        logging.error(f"place_protection_orders: كمية صفر لـ {symbol}!")
        return False

    # إلغاء أي حماية قديمة أولاً
    cancel_protection_orders(symbol)
    time.sleep(0.5)

    success_sl      = False
    success_trailing = False

    # --- وقف الخسارة الثابت (شبكة أمان) ---
    sl_price = adjust_price(symbol, actual_entry * (1 - STOP_LOSS_PCT))
    try:
        client.futures_create_order(
            symbol     = symbol,
            side       = SIDE_SELL,
            type       = ORDER_TYPE_STOP_MARKET,
            stopPrice  = sl_price,
            quantity   = actual_qty,
            reduceOnly = True,
            workingType= "MARK_PRICE"
        )
        success_sl = True
        logging.info(f"✅ SL ثابت وُضع لـ {symbol} عند {sl_price}")
    except Exception as e:
        logging.error(f"❌ فشل SL لـ {symbol}: {e}")
        send_telegram(f"⚠️ *تحذير*: فشل وضع Stop Loss لـ `{symbol}`!\nالخطأ: {e}")

    # --- Trailing Stop ---
    activation = adjust_price(symbol, actual_entry * (1 + TRAILING_ACTIVATION_PCT))
    try:
        client.futures_create_order(
            symbol          = symbol,
            side            = SIDE_SELL,
            type            = "TRAILING_STOP_MARKET",
            quantity        = actual_qty,
            callbackRate    = TRAILING_CALLBACK_RATE,
            activationPrice = activation,
            reduceOnly      = True,
            workingType     = "MARK_PRICE"
        )
        success_trailing = True
        logging.info(f"✅ Trailing Stop وُضع لـ {symbol} | تفعيل: {activation} | Callback: {TRAILING_CALLBACK_RATE}%")
    except Exception as e:
        logging.error(f"❌ فشل Trailing لـ {symbol}: {e}")
        send_telegram(f"⚠️ *تحذير*: فشل وضع Trailing Stop لـ `{symbol}`!\nالخطأ: {e}")

    return success_sl or success_trailing

# ================== 7. فتح الصفقة ==================

def open_long_position(symbol, entry_price, total_balance):
    try:
        # فحص الحد الأقصى
        if len(open_trades) >= MAX_OPEN_TRADES:
            logging.info(f"تجاوز الحد الأقصى ({MAX_OPEN_TRADES}) — تخطي {symbol}")
            return False

        # فحص وجود وضعية مسبقة
        if has_futures_position(symbol):
            logging.info(f"{symbol}: وضعية مفتوحة بالفعل — تخطي")
            return False

        lot, tick, min_notional = get_symbol_filters(symbol)
        avail = get_available_balance()

        # حساب الكمية
        raw_qty = (total_balance * RISK_PER_TRADE * LEVERAGE) / entry_price
        qty     = adjust_quantity(symbol, raw_qty)

        if qty <= 0:
            logging.warning(f"⚠️ {symbol}: الكمية بعد التقريب = صفر (raw={raw_qty:.6f}). تخطي.")
            return False

        notional = qty * entry_price
        if notional < min_notional:
            logging.warning(f"⚠️ {symbol}: قيمة الصفقة {notional:.2f} < {min_notional} USDT. تخطي.")
            return False

        required_margin = notional / LEVERAGE
        if required_margin > avail * 0.95:   # 5% احتياط
            logging.info(f"⚠️ {symbol}: هامش مطلوب {required_margin:.2f} > متاح {avail:.2f}. تخطي.")
            return False

        # ضبط الرافعة
        try:
            client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        except Exception as e:
            logging.warning(f"تعذّر ضبط رافعة {symbol}: {e}")

        # أمر الدخول
        try:
            client.futures_create_order(
                symbol   = symbol,
                side     = SIDE_BUY,
                type     = ORDER_TYPE_MARKET,
                quantity = qty
            )
        except Exception as e:
            logging.error(f"❌ فشل أمر دخول {symbol}: {e}")
            return False

        # ✅ انتظار تأكيد الوضعية وجلب الكمية الفعلية
        time.sleep(1.0)
        actual_qty   = qty
        actual_entry = entry_price
        try:
            positions = client.futures_position_information(symbol=symbol)
            for p in positions:
                amt = float(p["positionAmt"])
                if amt > 0:
                    actual_qty   = amt
                    actual_entry = float(p["entryPrice"]) or entry_price
                    break
        except Exception as e:
            logging.warning(f"فشل جلب وضعية {symbol} بعد الدخول: {e}")

        logging.info(f"✅ دخول {symbol}: سعر={actual_entry}, كمية={actual_qty}")

        # ✅ وضع الحماية بالكمية الفعلية
        protection_ok = place_protection_orders(symbol, actual_entry, actual_qty)

        # تسجيل الصفقة
        open_trades[symbol] = {
            "entry":     actual_entry,
            "qty":       actual_qty,
            "open_time": utcnow()
        }

        send_telegram(
            f"🚀 *دخول صفقة {symbol}*\n"
            f"سعر: `{actual_entry}`\n"
            f"كمية: `{actual_qty}`\n"
            f"SL ثابت: `{adjust_price(symbol, actual_entry * (1 - STOP_LOSS_PCT))}`\n"
            f"Trailing: `{TRAILING_CALLBACK_RATE}%` | يُفعَّل عند `+{TRAILING_ACTIVATION_PCT*100:.1f}%`\n"
            f"{'✅ الحماية وُضعت' if protection_ok else '⚠️ تحقق من الحماية يدوياً!'}"
        )
        return True

    except Exception as e:
        logging.error(f"❌ خطأ غير متوقع في {symbol}: {e}")
        return False

# ================== 8. مراقبة الصفقات المفتوحة ==================

def monitor_trades():
    """
    يُشغَّل كل دورة:
    1. يتحقق من الصفقات المُغلقة ويُرسل إشعاراً
    2. يتحقق من وجود أوامر الحماية ويُعيدها إذا اختفت
    """
    for symbol in list(open_trades.keys()):
        try:
            positions = client.futures_position_information(symbol=symbol)
            amt = 0.0
            for p in positions:
                amt = float(p["positionAmt"])
                break

            # الصفقة أُغلقت
            if abs(amt) == 0:
                trade    = open_trades.pop(symbol)
                duration = utcnow() - trade["open_time"]
                ticker   = client.futures_symbol_ticker(symbol=symbol)
                exit_p   = float(ticker["price"])
                pnl_pct  = ((exit_p - trade["entry"]) / trade["entry"]) * 100 * LEVERAGE

                send_telegram(
                    f"{'🟢' if pnl_pct >= 0 else '🔴'} *صفقة مُغلقة: {symbol}*\n"
                    f"دخول: `{trade['entry']}` | خروج: `{exit_p}`\n"
                    f"ربح/خسارة: `{pnl_pct:+.2f}%` (رافعة {LEVERAGE}x)\n"
                    f"المدة: `{str(duration).split('.')[0]}`"
                )
                logging.info(f"صفقة مُغلقة: {symbol} | P&L: {pnl_pct:+.2f}%")
                continue

            # الصفقة مفتوحة — تحقق من الحماية
            orders = client.futures_get_open_orders(symbol=symbol)
            has_sl       = any(o["type"] == "STOP_MARKET"          for o in orders)
            has_trailing = any(o["type"] == "TRAILING_STOP_MARKET"  for o in orders)

            if not has_sl and not has_trailing:
                logging.warning(f"⚠️ {symbol}: لا يوجد SL ولا Trailing! إعادة وضع الحماية...")
                send_telegram(f"⚠️ *{symbol}*: الحماية مفقودة! جاري إعادة الوضع...")
                place_protection_orders(symbol, open_trades[symbol]["entry"], abs(amt))

            elif not has_trailing:
                logging.warning(f"⚠️ {symbol}: Trailing مفقود — إعادة وضعه فقط...")
                activation = adjust_price(symbol, open_trades[symbol]["entry"] * (1 + TRAILING_ACTIVATION_PCT))
                try:
                    client.futures_create_order(
                        symbol          = symbol,
                        side            = SIDE_SELL,
                        type            = "TRAILING_STOP_MARKET",
                        quantity        = abs(amt),
                        callbackRate    = TRAILING_CALLBACK_RATE,
                        activationPrice = activation,
                        reduceOnly      = True,
                        workingType     = "MARK_PRICE"
                    )
                    logging.info(f"✅ Trailing أُعيد لـ {symbol}")
                except Exception as e:
                    logging.error(f"فشل إعادة Trailing لـ {symbol}: {e}")

            elif not has_sl:
                logging.warning(f"⚠️ {symbol}: SL مفقود — إعادة وضعه...")
                sl_price = adjust_price(symbol, open_trades[symbol]["entry"] * (1 - STOP_LOSS_PCT))
                try:
                    client.futures_create_order(
                        symbol     = symbol,
                        side       = SIDE_SELL,
                        type       = ORDER_TYPE_STOP_MARKET,
                        stopPrice  = sl_price,
                        quantity   = abs(amt),
                        reduceOnly = True,
                        workingType= "MARK_PRICE"
                    )
                    logging.info(f"✅ SL أُعيد لـ {symbol}")
                except Exception as e:
                    logging.error(f"فشل إعادة SL لـ {symbol}: {e}")

        except Exception as e:
            logging.error(f"خطأ monitor_trades {symbol}: {e}")

# ================== 9. حماية الرصيد ==================

def close_all_positions(reason: str):
    """يُغلق كل الصفقات المفتوحة بأوامر سوق"""
    send_telegram(f"🚨 *إغلاق إجباري لكل الصفقات*\nالسبب: {reason}")
    try:
        positions = client.futures_position_information()
        for p in positions:
            amt = float(p["positionAmt"])
            if abs(amt) == 0:
                continue
            sym  = p["symbol"]
            side = SIDE_SELL if amt > 0 else SIDE_BUY
            cancel_protection_orders(sym)
            try:
                client.futures_create_order(
                    symbol     = sym,
                    side       = side,
                    type       = ORDER_TYPE_MARKET,
                    quantity   = abs(amt),
                    reduceOnly = True
                )
                open_trades.pop(sym, None)
                logging.info(f"تم إغلاق {sym}")
            except Exception as e:
                logging.error(f"فشل إغلاق {sym}: {e}")
    except Exception as e:
        logging.error(f"خطأ close_all_positions: {e}")

def check_protection(current_balance) -> bool:
    global bot_halted_total, bot_halted_daily, daily_start_balance, daily_reset_date

    if bot_halted_total:
        return False

    today = utcnow().date()
    if daily_reset_date != today:
        daily_start_balance = current_balance
        daily_reset_date    = today
        bot_halted_daily    = False
        send_telegram(f"✅ يوم جديد — رصيد البداية: `{current_balance:.2f}` USDT")

    if daily_start_balance and daily_start_balance > 0:
        d_loss = (daily_start_balance - current_balance) / daily_start_balance
        if d_loss >= DAILY_LOSS_LIMIT_PCT:
            if not bot_halted_daily:
                bot_halted_daily = True
                close_all_positions(f"خسارة يومية {d_loss*100:.1f}% ≥ {DAILY_LOSS_LIMIT_PCT*100:.0f}%")
            return False

    if bot_start_balance and bot_start_balance > 0:
        t_loss = (bot_start_balance - current_balance) / bot_start_balance
        if t_loss >= TOTAL_LOSS_LIMIT_PCT:
            bot_halted_total = True
            close_all_positions(f"خسارة إجمالية {t_loss*100:.1f}% ≥ {TOTAL_LOSS_LIMIT_PCT*100:.0f}%")
            send_telegram("🚨 *البوت متوقف نهائياً* — يرجى المراجعة اليدوية.")
            return False

    return True

# ================== 10. الحلقة الرئيسية ==================

def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date

    initial_bal         = get_futures_balance()
    bot_start_balance   = initial_bal
    daily_start_balance = initial_bal
    daily_reset_date    = utcnow().date()

    send_telegram(
        f"🤖 *بوت التداول v7 بدأ* ✅\n"
        f"رصيد البداية: `{initial_bal:.2f}` USDT\n"
        f"SL ثابت: `{STOP_LOSS_PCT*100:.0f}%` | Trailing: `{TRAILING_CALLBACK_RATE}%`\n"
        f"حد يومي: `{DAILY_LOSS_LIMIT_PCT*100:.0f}%` | حد إجمالي: `{TOTAL_LOSS_LIMIT_PCT*100:.0f}%`"
    )

    cycle = 0
    while True:
        cycle += 1
        logging.info(f"--- الدورة #{cycle} ---")
        try:
            current_balance = get_futures_balance()
            avail_balance   = get_available_balance()
            logging.info(f"رصيد: {current_balance:.2f} | متاح: {avail_balance:.2f}")

            # 1. مراقبة الصفقات المفتوحة
            if open_trades:
                monitor_trades()

            # 2. فحص الحماية
            if not check_protection(current_balance):
                logging.info("التداول محظور — تخطي الدورة")
                time.sleep(40)
                continue

            # 3. لا هامش متاح
            if avail_balance < 1.0:
                logging.info("الهامش المتاح أقل من 1 USDT — تخطي")
                time.sleep(40)
                continue

            # 4. حد الصفقات المفتوحة
            if len(open_trades) >= MAX_OPEN_TRADES:
                logging.info(f"وصلنا الحد الأقصى ({MAX_OPEN_TRADES} صفقات)")
                time.sleep(40)
                continue

            # 5. جمع وتقييم المرشحين
            candidates = []
            seen = set()
            for symbol in TOP_SYMBOLS:
                if symbol in seen:
                    continue
                seen.add(symbol)

                if symbol in open_trades or has_futures_position(symbol):
                    logging.info(f"{symbol}: متجاوز — مفتوح")
                    continue

                result = score_symbol(symbol)
                if result is None:
                    continue
                if result["score"] < MIN_SCORE:
                    logging.info(f"{symbol}: نقاط ضعيفة ({result['score']})")
                    continue

                # فلتر مسبق للقيمة
                notional_est = (current_balance * RISK_PER_TRADE * LEVERAGE)
                _, _, min_notional = get_symbol_filters(symbol)
                if notional_est < min_notional:
                    logging.info(f"{symbol}: قيمة متوقعة {notional_est:.1f} < {min_notional} USDT")
                    continue

                candidates.append(result)
                logging.info(f"{symbol}: {result['score']}pts | RSI={result['rsi']}")

            # 6. اختيار الأفضل وفتح الصفقة
            if not candidates:
                logging.info("لا توجد فرص في هذه الدورة.")
            else:
                candidates.sort(key=lambda x: (-x["score"], x["rsi"]))
                for best in candidates:
                    success = open_long_position(best["symbol"], best["price"], current_balance)
                    if success:
                        break
                    logging.info(f"{best['symbol']}: فشل — الانتقال للتالي")

            # 7. تقرير يومي عند 23:00 UTC
            if utcnow().hour == 23 and utcnow().minute < 1:
                _send_daily_report(current_balance)

        except Exception as e:
            logging.error(f"خطأ الدورة الرئيسية: {e}")
            send_telegram(f"⚠️ خطأ في الدورة:\n`{e}`")

        time.sleep(30)

# ================== 11. التقرير اليومي ==================

_last_report_date = None

def _send_daily_report(balance):
    global _last_report_date
    today = utcnow().date()
    if _last_report_date == today:
        return
    _last_report_date = today

    try:
        positions = client.futures_position_information()
        open_pos  = [p for p in positions if abs(float(p["positionAmt"])) > 0]

        d_loss = ((daily_start_balance or balance) - balance) / (daily_start_balance or balance) * 100
        t_loss = ((bot_start_balance  or balance) - balance) / (bot_start_balance  or balance) * 100

        msg  = f"📊 *تقرير يومي — v7*\n"
        msg += f"التاريخ: `{today}` UTC\n"
        msg += f"الرصيد: `{balance:.2f}` USDT\n"
        msg += f"خسارة اليوم: `{d_loss:.2f}%` | إجمالي: `{t_loss:.2f}%`\n"
        msg += f"صفقات مفتوحة: `{len(open_pos)}`\n"
        for p in open_pos:
            upnl = float(p["unRealizedProfit"])
            msg += f"  • {p['symbol']}: دخول `{p['entryPrice']}` | P&L `{upnl:+.2f}` USDT\n"

        send_telegram(msg)
    except Exception as e:
        logging.error(f"خطأ التقرير اليومي: {e}")

# ================== 12. السيرفر ==================

@app.route("/")
def home():
    return f"Bot v7 | صفقات مفتوحة: {len(open_trades)}"

if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
