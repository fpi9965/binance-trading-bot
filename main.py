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
#  1. الإعدادات
# ══════════════════════════════════════════════

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_API_SECRET")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

RISK_PER_TRADE  = 0.05
LEVERAGE        = 20
TIMEFRAME       = "15m"
MAX_OPEN_TRADES = 3

# 🛡️ الحماية
STOP_LOSS_PCT           = 0.02
TRAILING_CALLBACK_RATE  = 1.0
TRAILING_ACTIVATION_PCT = 0.005

DAILY_LOSS_LIMIT_PCT = 0.05
TOTAL_LOSS_LIMIT_PCT = 0.15

TOP_SYMBOLS = [
    "DOGEUSDT", "XRPUSDT", "SOLUSDT",
    "LTCUSDT",  "LINKUSDT", "POLUSDT"
]
MIN_24H_QUOTE_VOLUME = 500_000   # خُفِّض من 1M
MIN_SCORE            = 20        # خُفِّض من 35 — MACD صاعد يكفي

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

_filters_cache: dict = {}
open_trades:    dict = {}   # { symbol: {entry, qty, open_time} }

bot_start_balance:   float = 0.0
daily_start_balance: float = 0.0
daily_reset_date           = None
bot_halted_total           = False
bot_halted_daily           = False
_last_report_date          = None

# ══════════════════════════════════════════════
#  3. دوال مساعدة
# ══════════════════════════════════════════════

def utcnow():
    return datetime.now(timezone.utc)

def send_telegram(msg: str):
    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Telegram: {e}")

def get_futures_balance() -> float:
    try:
        for b in client.futures_account_balance():
            if b["asset"] == "USDT":
                return float(b["balance"])
    except Exception as e:
        logging.error(f"get_futures_balance: {e}")
    return 0.0

def get_available_margin() -> float:
    try:
        return float(client.futures_account()["availableBalance"])
    except Exception as e:
        logging.error(f"get_available_margin: {e}")
    return 0.0

def get_filters(symbol: str) -> tuple:
    """(lot_size, tick_size, min_notional) — يُجلب مرة واحدة لكل الرموز"""
    if symbol in _filters_cache:
        return _filters_cache[symbol]
    if not _filters_cache:
        try:
            for s in client.futures_exchange_info()["symbols"]:
                sym = s["symbol"]
                lot = tick = None
                notional = 5.0
                for f in s["filters"]:
                    ft = f["filterType"]
                    if ft == "LOT_SIZE":
                        lot = float(f["stepSize"])
                    elif ft == "PRICE_FILTER":
                        tick = float(f["tickSize"])
                    elif ft == "MIN_NOTIONAL":
                        notional = float(f["notional"])
                if lot and tick:
                    _filters_cache[sym] = (lot, tick, notional)
        except Exception as e:
            logging.error(f"get_filters: {e}")
    return _filters_cache.get(symbol, (0.001, 0.01, 5.0))

def round_qty(symbol: str, qty: float) -> float:
    lot, _, _ = get_filters(symbol)
    if lot <= 0:
        return round(qty, 3)
    prec = max(0, round(-math.log10(lot)))
    return float(f"{qty:.{prec}f}")

def round_price(symbol: str, price: float) -> float:
    _, tick, _ = get_filters(symbol)
    if tick <= 0:
        return round(price, 2)
    prec = max(0, round(-math.log10(tick)))
    return float(f"{price:.{prec}f}")

def get_actual_position(symbol: str) -> tuple:
    """يُعيد (positionAmt, entryPrice)"""
    try:
        for p in client.futures_position_information(symbol=symbol):
            amt   = float(p["positionAmt"])
            entry = float(p["entryPrice"])
            return amt, entry   # يُعيد أول سجل دائماً
    except Exception as e:
        logging.warning(f"get_actual_position {symbol}: {e}")
    return 0.0, 0.0

# ══════════════════════════════════════════════
#  4. إدارة الحماية
# ══════════════════════════════════════════════

PROTECTION_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"}

def cancel_protection_orders(symbol: str):
    try:
        for o in client.futures_get_open_orders(symbol=symbol):
            if o["type"] in PROTECTION_TYPES:
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                    logging.info(f"إلغاء {o['type']} لـ {symbol}")
                except Exception as e:
                    logging.warning(f"فشل إلغاء {symbol}: {e}")
    except Exception as e:
        logging.error(f"cancel_protection_orders {symbol}: {e}")

def place_protection(symbol: str, entry: float, qty: float) -> bool:
    """
    SL ثابت + Trailing Stop — بالكمية الفعلية للوضعية
    """
    if qty <= 0:
        logging.error(f"place_protection: qty=0 لـ {symbol}")
        return False

    cancel_protection_orders(symbol)
    time.sleep(0.5)

    ok_sl = ok_tr = False

    # — Stop Loss ثابت —
    sl_price = round_price(symbol, entry * (1 - STOP_LOSS_PCT))
    try:
        client.futures_create_order(
            symbol      = symbol,
            side        = SIDE_SELL,
            type        = ORDER_TYPE_STOP_MARKET,
            stopPrice   = sl_price,
            quantity    = qty,
            reduceOnly  = True,
            workingType = "MARK_PRICE"
        )
        ok_sl = True
        logging.info(f"✅ SL={sl_price} qty={qty} لـ {symbol}")
    except Exception as e:
        logging.error(f"❌ SL فشل {symbol}: {e}")
        send_telegram(f"⚠️ *فشل SL* لـ `{symbol}`\n`{e}`")

    # — Trailing Stop —
    activation = round_price(symbol, entry * (1 + TRAILING_ACTIVATION_PCT))
    try:
        client.futures_create_order(
            symbol          = symbol,
            side            = SIDE_SELL,
            type            = "TRAILING_STOP_MARKET",
            quantity        = qty,
            callbackRate    = TRAILING_CALLBACK_RATE,
            activationPrice = activation,
            reduceOnly      = True,
            workingType     = "MARK_PRICE"
        )
        ok_tr = True
        logging.info(f"✅ Trailing {TRAILING_CALLBACK_RATE}% @{activation} لـ {symbol}")
    except Exception as e:
        logging.error(f"❌ Trailing فشل {symbol}: {e}")
        send_telegram(f"⚠️ *فشل Trailing* لـ `{symbol}`\n`{e}`")

    return ok_sl or ok_tr

# ══════════════════════════════════════════════
#  5. تبنّي الوضعيات الحالية
# ══════════════════════════════════════════════

def adopt_existing_positions():
    """
    ✅ إصلاح v9:
    يجلب كل الوضعيات بطريقتين:
    1. futures_position_information() بدون symbol — يُعيد كل الرموز
    2. يفلتر أي positionAmt != 0
    """
    logging.info("🔍 جلب كل الوضعيات المفتوحة...")
    adopted = 0
    try:
        # ✅ جلب الكل دفعة واحدة بدون تحديد رمز
        all_positions = client.futures_position_information()
        logging.info(f"إجمالي الرموز في الحساب: {len(all_positions)}")

        for p in all_positions:
            sym   = p["symbol"]
            amt   = float(p["positionAmt"])
            entry = float(p["entryPrice"])

            if abs(amt) < 1e-8 or entry == 0:
                continue   # وضعية فارغة

            logging.info(f"وضعية موجودة: {sym} | كمية={amt} | دخول={entry}")

            if amt < 0:
                send_telegram(
                    f"⚠️ وضعية SHORT في `{sym}` (كمية: {amt})\n"
                    f"البوت لا يدير الـ SHORT — راجعها يدوياً."
                )
                continue

            # LONG — أضفها للسجل
            open_trades[sym] = {
                "entry":     entry,
                "qty":       abs(amt),
                "open_time": utcnow()
            }

            # فحص الحماية الحالية
            try:
                orders    = client.futures_get_open_orders(symbol=sym)
                has_sl    = any(o["type"] == "STOP_MARKET"          for o in orders)
                has_trail = any(o["type"] == "TRAILING_STOP_MARKET"  for o in orders)

                if not has_sl or not has_trail:
                    logging.warning(f"⚠️ {sym}: حماية ناقصة SL={has_sl} Trail={has_trail} — وضع حماية جديدة")
                    place_protection(sym, entry, abs(amt))
                else:
                    logging.info(f"✅ {sym}: حماية موجودة")
            except Exception as e:
                logging.error(f"فحص حماية {sym}: {e}")

            adopted += 1

    except Exception as e:
        logging.error(f"adopt_existing_positions: {e}")
        send_telegram(f"⚠️ خطأ في قراءة الوضعيات:\n`{e}`")

    # تقرير التبنّي
    msg = f"🔄 *تبنّي الوضعيات — v9*\nعقود LONG مفتوحة: `{adopted}`\n"
    if open_trades:
        for sym, t in open_trades.items():
            msg += f"  • `{sym}`: دخول `{t['entry']}` | كمية `{t['qty']}`\n"
    else:
        msg += "لا توجد وضعيات مفتوحة."
    send_telegram(msg)
    logging.info(f"تبنّي: {adopted} وضعية")

# ══════════════════════════════════════════════
#  6. مراقبة الصفقات
# ══════════════════════════════════════════════

def monitor_trades():
    for symbol in list(open_trades.keys()):
        try:
            amt, _ = get_actual_position(symbol)

            # الصفقة أُغلقت
            if abs(amt) < 1e-8:
                trade    = open_trades.pop(symbol)
                duration = utcnow() - trade["open_time"]
                try:
                    exit_p  = float(client.futures_symbol_ticker(symbol=symbol)["price"])
                    pnl_pct = ((exit_p - trade["entry"]) / trade["entry"]) * 100 * LEVERAGE
                    emoji   = "🟢" if pnl_pct >= 0 else "🔴"
                    send_telegram(
                        f"{emoji} *مُغلقة: {symbol}*\n"
                        f"دخول: `{trade['entry']}` → خروج: `{exit_p}`\n"
                        f"P&L: `{pnl_pct:+.2f}%` (رافعة {LEVERAGE}x)\n"
                        f"المدة: `{str(duration).split('.')[0]}`"
                    )
                except Exception:
                    send_telegram(f"🏁 *مُغلقة: {symbol}*")
                logging.info(f"صفقة مُغلقة: {symbol}")
                continue

            # الصفقة مفتوحة — فحص الحماية
            orders    = client.futures_get_open_orders(symbol=symbol)
            has_sl    = any(o["type"] == "STOP_MARKET"          for o in orders)
            has_trail = any(o["type"] == "TRAILING_STOP_MARKET"  for o in orders)

            if not has_sl and not has_trail:
                logging.warning(f"🚨 {symbol}: لا حماية! إعادة وضعها...")
                send_telegram(f"🚨 *{symbol}*: الحماية مفقودة كلياً!")
                place_protection(symbol, open_trades[symbol]["entry"], abs(amt))

            elif not has_sl:
                sl_price = round_price(symbol, open_trades[symbol]["entry"] * (1 - STOP_LOSS_PCT))
                try:
                    client.futures_create_order(
                        symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_STOP_MARKET,
                        stopPrice=sl_price, quantity=abs(amt),
                        reduceOnly=True, workingType="MARK_PRICE"
                    )
                    logging.info(f"✅ SL أُعيد: {symbol}")
                except Exception as e:
                    logging.error(f"إعادة SL {symbol}: {e}")

            elif not has_trail:
                activation = round_price(symbol, open_trades[symbol]["entry"] * (1 + TRAILING_ACTIVATION_PCT))
                try:
                    client.futures_create_order(
                        symbol=symbol, side=SIDE_SELL, type="TRAILING_STOP_MARKET",
                        quantity=abs(amt), callbackRate=TRAILING_CALLBACK_RATE,
                        activationPrice=activation, reduceOnly=True, workingType="MARK_PRICE"
                    )
                    logging.info(f"✅ Trailing أُعيد: {symbol}")
                except Exception as e:
                    logging.error(f"إعادة Trailing {symbol}: {e}")

        except Exception as e:
            logging.error(f"monitor_trades {symbol}: {e}")

# ══════════════════════════════════════════════
#  7. حماية الرصيد
# ══════════════════════════════════════════════

def close_all_futures(reason: str):
    send_telegram(f"🚨 *إغلاق إجباري*\nالسبب: {reason}")
    try:
        for p in client.futures_position_information():
            amt = float(p["positionAmt"])
            if abs(amt) < 1e-8:
                continue
            sym  = p["symbol"]
            side = SIDE_SELL if amt > 0 else SIDE_BUY
            cancel_protection_orders(sym)
            try:
                client.futures_create_order(
                    symbol=sym, side=side, type=ORDER_TYPE_MARKET,
                    quantity=abs(amt), reduceOnly=True
                )
                open_trades.pop(sym, None)
                logging.info(f"إغلاق إجباري: {sym}")
            except Exception as e:
                logging.error(f"فشل إغلاق {sym}: {e}")
    except Exception as e:
        logging.error(f"close_all_futures: {e}")

def check_protection(balance: float) -> bool:
    global bot_halted_total, bot_halted_daily
    global daily_start_balance, daily_reset_date

    if bot_halted_total:
        return False

    today = utcnow().date()
    if daily_reset_date != today:
        daily_start_balance = balance
        daily_reset_date    = today
        bot_halted_daily    = False
        send_telegram(f"✅ يوم جديد — رصيد: `{balance:.2f}` USDT")

    if daily_start_balance > 0:
        d = (daily_start_balance - balance) / daily_start_balance
        if d >= DAILY_LOSS_LIMIT_PCT:
            if not bot_halted_daily:
                bot_halted_daily = True
                close_all_futures(f"خسارة يومية {d*100:.1f}% ≥ {DAILY_LOSS_LIMIT_PCT*100:.0f}%")
            return False

    if bot_start_balance > 0:
        t = (bot_start_balance - balance) / bot_start_balance
        if t >= TOTAL_LOSS_LIMIT_PCT:
            bot_halted_total = True
            close_all_futures(f"خسارة إجمالية {t*100:.1f}% ≥ {TOTAL_LOSS_LIMIT_PCT*100:.0f}%")
            send_telegram("🚨 *البوت متوقف نهائياً* — مراجعة يدوية.")
            return False

    return True

# ══════════════════════════════════════════════
#  8. المؤشرات الفنية
# ══════════════════════════════════════════════

def ema_calc(values, period):
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

def compute_macd_bull(closes, fast=12, slow=26, signal=9) -> bool:
    if len(closes) < slow + signal:
        return False
    kf, ks = 2/(fast+1), 2/(slow+1)
    ef = es = closes[0]
    line = []
    for c in closes:
        ef = c*kf + ef*(1-kf)
        es = c*ks + es*(1-ks)
        line.append(ef - es)
    return line[-1] > ema_calc(line, signal)

def score_symbol(symbol: str) -> dict | None:
    """
    ✅ v9: لوق تفصيلي لكل رمز لمعرفة سبب الرفض
    """
    try:
        # بيانات 15m
        klines = client.futures_klines(symbol=symbol, interval=TIMEFRAME, limit=150)
        closes = [float(k[4]) for k in klines]
        macd_bull = compute_macd_bull(closes)
        rsi       = compute_rsi(closes)

        # فلتر الترند EMA200 ساعية
        h_klines = client.futures_klines(symbol=symbol, interval="1h", limit=210)
        h_closes = [float(k[4]) for k in h_klines]
        ema200   = ema_calc(h_closes, 200)
        uptrend  = h_closes[-1] >= ema200 * 0.98

        # حجم التداول
        ticker       = client.futures_ticker(symbol=symbol)
        quote_volume = float(ticker.get("quoteVolume", 0))
        price        = float(ticker["lastPrice"])

        # ✅ لوق تفصيلي لكل رمز
        logging.info(
            f"{symbol}: MACD={'✅صاعد' if macd_bull else '❌هابط'} | "
            f"RSI={rsi:.1f} | "
            f"Trend={'✅' if uptrend else f'❌ سعر={h_closes[-1]:.4f}<EMA={ema200:.4f}'} | "
            f"Vol={quote_volume/1e6:.1f}M"
        )

        if not uptrend:
            return None
        if quote_volume < MIN_24H_QUOTE_VOLUME:
            return None

        sc = 0
        if macd_bull:  sc += 30
        if rsi < 60:   sc += 30
        elif rsi < 70: sc += 10

        if sc < MIN_SCORE:
            logging.info(f"{symbol}: نقاط {sc} < {MIN_SCORE} — مرفوض")
            return None

        return {"symbol": symbol, "score": sc, "rsi": round(rsi, 1), "price": price}

    except Exception as e:
        logging.warning(f"score_symbol {symbol}: {e}")
        return None

# ══════════════════════════════════════════════
#  9. فتح صفقة
# ══════════════════════════════════════════════

def open_long(symbol: str, price: float, total_balance: float) -> bool:
    if len(open_trades) >= MAX_OPEN_TRADES:
        return False

    amt, _ = get_actual_position(symbol)
    if abs(amt) > 1e-8:
        logging.info(f"{symbol}: وضعية موجودة — تخطي")
        return False

    try:
        lot, tick, min_notional = get_filters(symbol)
        avail = get_available_margin()

        raw_qty = (total_balance * RISK_PER_TRADE * LEVERAGE) / price
        qty     = round_qty(symbol, raw_qty)

        if qty <= 0:
            logging.warning(f"⚠️ {symbol}: qty=0 (raw={raw_qty:.6f})")
            return False

        notional = qty * price
        if notional < min_notional:
            logging.warning(f"⚠️ {symbol}: notional={notional:.2f} < {min_notional}")
            return False

        req_margin = notional / LEVERAGE
        if req_margin > avail * 0.95:
            logging.info(f"⚠️ {symbol}: هامش مطلوب {req_margin:.2f} > متاح {avail:.2f}")
            return False

        # ضبط الرافعة
        try:
            client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        except Exception as e:
            logging.warning(f"رافعة {symbol}: {e}")

        # أمر الدخول
        client.futures_create_order(
            symbol=symbol, side=SIDE_BUY,
            type=ORDER_TYPE_MARKET, quantity=qty
        )

        # انتظار التأكيد
        time.sleep(1.5)
        actual_amt, actual_entry = get_actual_position(symbol)

        if abs(actual_amt) < 1e-8:
            logging.error(f"❌ {symbol}: أمر أُرسل لكن لا وضعية ظهرت!")
            send_telegram(f"⚠️ `{symbol}`: أُرسل أمر دخول لكن لا وضعية!")
            return False

        actual_qty   = abs(actual_amt)
        actual_entry = actual_entry or price

        # ✅ الحماية بالكمية الفعلية دائماً
        ok = place_protection(symbol, actual_entry, actual_qty)

        open_trades[symbol] = {
            "entry":     actual_entry,
            "qty":       actual_qty,
            "open_time": utcnow()
        }

        send_telegram(
            f"🚀 *دخول {symbol}*\n"
            f"سعر: `{actual_entry}` | كمية: `{actual_qty}`\n"
            f"SL: `{round_price(symbol, actual_entry*(1-STOP_LOSS_PCT))}`\n"
            f"Trailing: `{TRAILING_CALLBACK_RATE}%` ← يبدأ عند `+{TRAILING_ACTIVATION_PCT*100:.1f}%`\n"
            f"{'✅ الحماية وُضعت' if ok else '⚠️ راجع الحماية يدوياً!'}"
        )
        return True

    except Exception as e:
        logging.error(f"open_long {symbol}: {e}")
        return False

# ══════════════════════════════════════════════
#  10. التقرير اليومي
# ══════════════════════════════════════════════

def send_daily_report(balance: float):
    global _last_report_date
    today = utcnow().date()
    if _last_report_date == today:
        return
    _last_report_date = today
    try:
        positions = [p for p in client.futures_position_information() if abs(float(p["positionAmt"])) > 1e-8]
        d = (daily_start_balance - balance) / daily_start_balance * 100 if daily_start_balance else 0
        t = (bot_start_balance  - balance) / bot_start_balance  * 100 if bot_start_balance  else 0
        msg  = f"📊 *تقرير يومي — v9*\nالتاريخ: `{today}` UTC\n"
        msg += f"الرصيد: `{balance:.2f}` USDT\n"
        msg += f"اليوم: `{d:.2f}%` | إجمالي: `{t:.2f}%`\n"
        msg += f"عقود مفتوحة: `{len(positions)}`\n"
        for p in positions:
            upnl = float(p["unRealizedProfit"])
            msg += f"  • `{p['symbol']}` دخول:`{p['entryPrice']}` P&L:`{upnl:+.2f}$`\n"
        send_telegram(msg)
    except Exception as e:
        logging.error(f"send_daily_report: {e}")

# ══════════════════════════════════════════════
#  11. الحلقة الرئيسية
# ══════════════════════════════════════════════

def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date

    initial = get_futures_balance()
    bot_start_balance   = initial
    daily_start_balance = initial
    daily_reset_date    = utcnow().date()

    send_telegram(
        f"🤖 *بوت v9 يبدأ* ✅\n"
        f"رصيد: `{initial:.2f}` USDT\n"
        f"SL: `{STOP_LOSS_PCT*100:.0f}%` | Trailing: `{TRAILING_CALLBACK_RATE}%`\n"
        f"حد يومي: `{DAILY_LOSS_LIMIT_PCT*100:.0f}%` | إجمالي: `{TOTAL_LOSS_LIMIT_PCT*100:.0f}%`\n"
        f"أقصى صفقات: `{MAX_OPEN_TRADES}` | حد الدخول: `{MIN_SCORE}` نقطة"
    )

    # ✅ تبنّي الوضعيات أول شيء
    adopt_existing_positions()

    cycle = 0
    while True:
        cycle += 1
        logging.info(f"══ الدورة #{cycle} ══")
        try:
            balance = get_futures_balance()
            avail   = get_available_margin()
            logging.info(f"رصيد: {balance:.2f} | متاح: {avail:.2f} | صفقات: {len(open_trades)}")

            # 1. مراقبة الصفقات
            monitor_trades()

            # 2. حماية الرصيد
            if not check_protection(balance):
                time.sleep(40)
                continue

            # 3. فحص الهامش
            if avail < 1.0:
                logging.info("هامش < 1 USDT — تخطي")
                time.sleep(40)
                continue

            # 4. حد الصفقات
            if len(open_trades) >= MAX_OPEN_TRADES:
                logging.info(f"الحد الأقصى {MAX_OPEN_TRADES} — لا دخول جديد")
                time.sleep(40)
                continue

            # 5. تقييم الفرص
            candidates = []
            seen = set()
            for symbol in TOP_SYMBOLS:
                if symbol in seen or symbol in open_trades:
                    continue
                seen.add(symbol)

                amt, _ = get_actual_position(symbol)
                if abs(amt) > 1e-8:
                    logging.info(f"{symbol}: وضعية فعلية موجودة — تخطي")
                    continue

                r = score_symbol(symbol)
                if r is None:
                    continue

                est_notional = balance * RISK_PER_TRADE * LEVERAGE
                _, _, min_n  = get_filters(symbol)
                if est_notional < min_n:
                    logging.info(f"{symbol}: قيمة متوقعة {est_notional:.1f} < {min_n}")
                    continue

                candidates.append(r)

            # 6. فتح الأفضل
            if candidates:
                candidates.sort(key=lambda x: (-x["score"], x["rsi"]))
                logging.info(f"المرشحون: {[(c['symbol'], c['score']) for c in candidates]}")
                for c in candidates:
                    if open_long(c["symbol"], c["price"], balance):
                        break
                    logging.info(f"{c['symbol']}: فشل الفتح — التالي")
            else:
                logging.info("لا فرص — السوق لا يستوفي شروط الدخول.")

            # 7. تقرير يومي
            now = utcnow()
            if now.hour == 23 and now.minute == 0:
                send_daily_report(balance)

        except Exception as e:
            logging.error(f"main_loop: {e}")
            send_telegram(f"⚠️ خطأ:\n`{e}`")

        time.sleep(30)

# ══════════════════════════════════════════════
#  12. السيرفر
# ══════════════════════════════════════════════

@app.route("/")
def home():
    lines = [f"<b>Bot v9</b> | صفقات: {len(open_trades)}"]
    for sym, t in open_trades.items():
        lines.append(f"• {sym}: entry={t['entry']} qty={t['qty']}")
    return "<br>".join(lines)

if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
