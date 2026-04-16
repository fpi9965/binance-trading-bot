"""
=============================================================
  SMART TRADING BOT v3.0 — بوت التداول الذكي
  - يصلح أخطاء SL/Trailing تلقائياً
  - يحلل شموع يابانية متعددة الإطار
  - يبحث في كل العملات المتاحة
  - يتعلم من الصفقات السابقة
  - يضبط SL/TP ديناميكياً بناءً على ATR
=============================================================
"""

import os, time, math, hmac, hashlib, logging, threading, json, statistics
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from binance.client import Client
from binance.enums import *
from flask import Flask

# ─── CREDENTIALS ────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_API_SECRET")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

# ─── TRADING CONFIG ──────────────────────────────────────────
LEVERAGE              = 10          # رافعة أكثر أماناً
RISK_PER_TRADE_PCT    = 0.02        # 2% من الرصيد لكل صفقة
TIMEFRAME             = "15m"
MAX_OPEN_TRADES       = 5

# ATR-based dynamic SL/TP
ATR_PERIOD            = 14
ATR_SL_MULTIPLIER     = 2.0         # SL = entry - 2×ATR
ATR_TP_MULTIPLIER     = 3.0         # TP = entry + 3×ATR
MIN_RR_RATIO          = 1.5         # نسبة مكسب/خسارة دنيا

TRAILING_CALLBACK_RATE   = 1.5      # %
TRAILING_ACTIVATION_PCT  = 0.008    # 0.8% تفعيل trailing

# حماية الرصيد
DAILY_LOSS_LIMIT_PCT  = 0.05
TOTAL_LOSS_LIMIT_PCT  = 0.15

# فلترة العملات
MIN_24H_QUOTE_VOLUME  = 1_000_000   # مليون دولار حجم أدنى
MIN_SCORE             = 40
SCAN_INTERVAL_SEC     = 45

# ─── LEARNING SYSTEM ─────────────────────────────────────────
LEARNING_FILE         = "bot_learning.json"

# ─── LOGGING ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
log = logging.getLogger(__name__)

# ─── FLASK ───────────────────────────────────────────────────
app = Flask(__name__)

# ─── GLOBALS ─────────────────────────────────────────────────
client: Client = None
open_trades:    dict  = {}
_filters_cache: dict  = {}
_all_symbols_cache     = []

bot_start_balance:    float = 0.0
daily_start_balance:  float = 0.0
daily_reset_date            = None
bot_halted_total            = False
bot_halted_daily            = False
_last_report_date           = None

# نظام التعلم
learning_data = {
    "trade_history":     [],      # كل الصفقات المغلقة
    "symbol_stats":      {},      # إحصائيات لكل عملة
    "best_conditions":   {},      # أفضل ظروف دخول
    "atr_multipliers":   {        # يُحدَّث ديناميكياً
        "sl":  ATR_SL_MULTIPLIER,
        "tp":  ATR_TP_MULTIPLIER,
    },
    "win_rate":          0.0,
    "total_trades":      0,
    "profitable_trades": 0,
}


# ══════════════════════════════════════════════════════════════
#  LEARNING SYSTEM
# ══════════════════════════════════════════════════════════════

def load_learning():
    global learning_data
    try:
        if os.path.exists(LEARNING_FILE):
            with open(LEARNING_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                learning_data.update(loaded)
            log.info(f"📚 تم تحميل بيانات التعلم | صفقات: {learning_data['total_trades']}")
    except Exception as e:
        log.error(f"load_learning: {e}")


def save_learning():
    try:
        with open(LEARNING_FILE, "w", encoding="utf-8") as f:
            json.dump(learning_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_learning: {e}")


def record_closed_trade(symbol: str, entry: float, exit_price: float,
                         pnl_pct: float, duration_min: float,
                         entry_rsi: float, entry_score: int, atr: float):
    """يسجل الصفقة المغلقة ويحدّث نظام التعلم"""
    won = pnl_pct > 0

    trade_record = {
        "symbol":       symbol,
        "entry":        entry,
        "exit":         exit_price,
        "pnl_pct":      round(pnl_pct, 3),
        "duration_min": round(duration_min, 1),
        "entry_rsi":    round(entry_rsi, 1) if entry_rsi else 0,
        "entry_score":  entry_score,
        "atr":          round(atr, 6) if atr else 0,
        "won":          won,
        "ts":           utcnow().isoformat(),
    }

    learning_data["trade_history"].append(trade_record)
    # احتفظ بآخر 500 صفقة فقط
    if len(learning_data["trade_history"]) > 500:
        learning_data["trade_history"] = learning_data["trade_history"][-500:]

    # إحصائيات العملة
    if symbol not in learning_data["symbol_stats"]:
        learning_data["symbol_stats"][symbol] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
    st = learning_data["symbol_stats"][symbol]
    if won:
        st["wins"] += 1
    else:
        st["losses"] += 1
    st["total_pnl"] += pnl_pct

    # إجماليات
    learning_data["total_trades"]      += 1
    if won:
        learning_data["profitable_trades"] += 1

    total = learning_data["total_trades"]
    wins  = learning_data["profitable_trades"]
    learning_data["win_rate"] = wins / total if total else 0

    # ضبط ATR multipliers تلقائياً
    _adapt_atr_multipliers()

    save_learning()
    log.info(f"📊 تسجيل صفقة {symbol} | {'✅ ربح' if won else '❌ خسارة'} {pnl_pct:+.2f}% | نسبة الفوز: {learning_data['win_rate']*100:.1f}%")


def _adapt_atr_multipliers():
    """يضبط معاملات ATR بناءً على أداء آخر 20 صفقة"""
    history = learning_data["trade_history"]
    if len(history) < 20:
        return

    recent = history[-20:]
    losses = [t for t in recent if not t["won"]]
    wins   = [t for t in recent if t["won"]]

    # إذا خسائر كثيرة → وسّع SL قليلاً
    loss_rate = len(losses) / len(recent)
    if loss_rate > 0.5:
        learning_data["atr_multipliers"]["sl"] = min(
            learning_data["atr_multipliers"]["sl"] * 1.1, 4.0
        )
        log.info(f"🎓 ATR SL → {learning_data['atr_multipliers']['sl']:.2f} (خسائر كثيرة)")

    # إذا فوز جيد → شدّد SL لحماية أكبر
    elif loss_rate < 0.3:
        learning_data["atr_multipliers"]["sl"] = max(
            learning_data["atr_multipliers"]["sl"] * 0.95, 1.5
        )
        log.info(f"🎓 ATR SL → {learning_data['atr_multipliers']['sl']:.2f} (أداء جيد)")

    # إذا كسب متوسط PnL عالٍ → ابقِ TP أعلى
    if wins:
        avg_win_pnl = statistics.mean([t["pnl_pct"] for t in wins])
        if avg_win_pnl > 5:
            learning_data["atr_multipliers"]["tp"] = min(
                learning_data["atr_multipliers"]["tp"] * 1.05, 6.0
            )


def get_symbol_win_rate(symbol: str) -> float:
    st = learning_data["symbol_stats"].get(symbol)
    if not st:
        return 0.5  # افتراضي 50%
    total = st["wins"] + st["losses"]
    return st["wins"] / total if total else 0.5


def is_blacklisted(symbol: str) -> bool:
    """يتجنب العملات ذات الأداء السيء باستمرار"""
    st = learning_data["symbol_stats"].get(symbol)
    if not st:
        return False
    total = st["wins"] + st["losses"]
    if total < 5:
        return False
    win_rate = st["wins"] / total
    # إذا خسر أكثر من 70% من الصفقات → تجنبه
    return win_rate < 0.30


# ══════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "YOUR_TOKEN":
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       msg,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        log.error(f"Telegram: {e}")


# ══════════════════════════════════════════════════════════════
#  BINANCE HELPERS
# ══════════════════════════════════════════════════════════════

def utcnow():
    return datetime.now(timezone.utc)


def get_futures_balance() -> float:
    try:
        for b in client.futures_account_balance():
            if b["asset"] == "USDT":
                return float(b["balance"])
    except Exception as e:
        log.error(f"get_futures_balance: {e}")
    return 0.0


def get_available_margin() -> float:
    try:
        return float(client.futures_account()["availableBalance"])
    except Exception as e:
        log.error(f"get_available_margin: {e}")
    return 0.0


def get_all_positions() -> list:
    try:
        return client.futures_position_information()
    except Exception as e:
        log.error(f"get_all_positions: {e}")
        return []


def get_actual_position(symbol: str) -> tuple:
    try:
        for p in client.futures_position_information(symbol=symbol):
            amt   = float(p["positionAmt"])
            entry = float(p["entryPrice"])
            return amt, entry
    except Exception as e:
        if "-1022" not in str(e):
            log.warning(f"get_actual_position {symbol}: {e}")
    return 0.0, 0.0


def get_filters(symbol: str) -> tuple:
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
            log.error(f"get_filters: {e}")
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
        return round(price, 4)
    prec = max(0, round(-math.log10(tick)))
    return float(f"{price:.{prec}f}")


def _get_all_symbols() -> list:
    global _all_symbols_cache
    if _all_symbols_cache:
        return _all_symbols_cache
    try:
        exchange_info = client.futures_exchange_info()
        symbols = []
        for s in exchange_info["symbols"]:
            sym = s["symbol"]
            if (sym.endswith("USDT")
                    and s["status"] == "TRADING"
                    and s["contractType"] == "PERPETUAL"):
                try:
                    sym.encode("ascii")
                    symbols.append(sym)
                except UnicodeEncodeError:
                    pass
        _all_symbols_cache = symbols
        log.info(f"📋 إجمالي العملات المتاحة: {len(symbols)}")
        return symbols
    except Exception as e:
        log.error(f"_get_all_symbols: {e}")
        return []


# ══════════════════════════════════════════════════════════════
#  PROTECTION ORDERS — الطريقة الصحيحة بدون algo endpoint
# ══════════════════════════════════════════════════════════════

PROTECTION_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"}


def cancel_protection_orders(symbol: str):
    try:
        for o in client.futures_get_open_orders(symbol=symbol):
            if o["type"] in PROTECTION_TYPES:
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                    log.info(f"إلغاء {o['type']} لـ {symbol}")
                except Exception as e:
                    log.warning(f"فشل إلغاء {symbol}: {e}")
    except Exception as e:
        log.error(f"cancel_protection_orders {symbol}: {e}")


def _place_stop_market(symbol: str, stop_price: float, qty: float) -> bool:
    """وقف الخسارة — الطريقة الصحيحة"""
    sp = round_price(symbol, stop_price)
    for attempt in range(3):
        try:
            client.futures_create_order(
                symbol=symbol,
                side=SIDE_SELL,
                type="STOP_MARKET",
                stopPrice=sp,
                quantity=qty,
                reduceOnly=True,
                workingType="MARK_PRICE",
                timeInForce="GTC"
            )
            log.info(f"✅ SL={sp} qty={qty} لـ {symbol}")
            return True
        except Exception as e:
            err_str = str(e)
            log.warning(f"SL {symbol} (محاولة {attempt+1}): {e}")
            if "-2021" in err_str:
                # السعر أقل من السوق — تعديل
                try:
                    current = float(client.futures_symbol_ticker(symbol=symbol)["price"])
                    sp = round_price(symbol, current * 0.985)
                    log.info(f"تعديل SL إلى {sp}")
                except Exception:
                    pass
            time.sleep(1)
    return False


def _place_take_profit(symbol: str, tp_price: float, qty: float) -> bool:
    """جني الأرباح"""
    tp = round_price(symbol, tp_price)
    for attempt in range(3):
        try:
            client.futures_create_order(
                symbol=symbol,
                side=SIDE_SELL,
                type="TAKE_PROFIT_MARKET",
                stopPrice=tp,
                quantity=qty,
                reduceOnly=True,
                workingType="MARK_PRICE",
                timeInForce="GTC"
            )
            log.info(f"✅ TP={tp} qty={qty} لـ {symbol}")
            return True
        except Exception as e:
            log.warning(f"TP {symbol} (محاولة {attempt+1}): {e}")
            time.sleep(1)
    return False


def _place_trailing_stop(symbol: str, activation_price: float, qty: float) -> bool:
    """Trailing Stop — تأمين الأرباح"""
    ap = round_price(symbol, activation_price)
    for attempt in range(3):
        try:
            client.futures_create_order(
                symbol=symbol,
                side=SIDE_SELL,
                type="TRAILING_STOP_MARKET",
                quantity=qty,
                callbackRate=TRAILING_CALLBACK_RATE,
                activationPrice=ap,
                reduceOnly=True,
                workingType="MARK_PRICE",
                timeInForce="GTC"
            )
            log.info(f"✅ Trailing {TRAILING_CALLBACK_RATE}% @{ap} لـ {symbol}")
            return True
        except Exception as e:
            log.warning(f"Trailing {symbol} (محاولة {attempt+1}): {e}")
            time.sleep(1)
    return False


def place_full_protection(symbol: str, entry: float, qty: float, atr: float) -> dict:
    """يضع SL + TP + Trailing بناءً على ATR الديناميكي"""
    if qty <= 0:
        return {"sl": False, "tp": False, "trail": False}

    cancel_protection_orders(symbol)
    time.sleep(0.5)

    sl_mult = learning_data["atr_multipliers"]["sl"]
    tp_mult = learning_data["atr_multipliers"]["tp"]

    sl_price = entry - (atr * sl_mult)
    tp_price = entry + (atr * tp_mult)
    activation = entry * (1 + TRAILING_ACTIVATION_PCT)

    # تحقق من RR ratio
    risk   = entry - sl_price
    reward = tp_price - entry
    rr     = reward / risk if risk > 0 else 0

    if rr < MIN_RR_RATIO:
        log.warning(f"⚠️ {symbol}: RR={rr:.2f} ضعيف — تعديل TP")
        tp_price = entry + risk * MIN_RR_RATIO

    log.info(f"🛡️ {symbol}: SL={round_price(symbol, sl_price)} TP={round_price(symbol, tp_price)} RR={rr:.2f}")

    ok_sl    = _place_stop_market(symbol, sl_price, qty)
    ok_tp    = _place_take_profit(symbol, tp_price, qty)
    ok_trail = _place_trailing_stop(symbol, activation, qty)

    return {"sl": ok_sl, "tp": ok_tp, "trail": ok_trail}


# ══════════════════════════════════════════════════════════════
#  TECHNICAL ANALYSIS — تحليل الشموع اليابانية
# ══════════════════════════════════════════════════════════════

def ema(values: list, period: int) -> float:
    if len(values) < period:
        return sum(values) / len(values)
    k = 2 / (period + 1)
    v = sum(values[:period]) / period
    for x in values[period:]:
        v = x * k + v * (1 - k)
    return v


def ema_series(values: list, period: int) -> list:
    if len(values) < period:
        return [sum(values[:i+1])/(i+1) for i in range(len(values))]
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for x in values[period:]:
        result.append(x * k + result[-1] * (1 - k))
    return [None] * (period - 1) + result


def compute_rsi(closes: list, period=14) -> float:
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    if len(gains) < period:
        return 50.0
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period or 1e-9
    return 100 - 100 / (1 + ag / al)


def compute_atr(highs: list, lows: list, closes: list, period=14) -> float:
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1])
        )
        trs.append(tr)
    if not trs:
        return closes[-1] * 0.01
    return sum(trs[-period:]) / min(period, len(trs))


def compute_macd(closes: list, fast=12, slow=26, signal=9) -> dict:
    if len(closes) < slow + signal:
        return {"macd": 0, "signal": 0, "hist": 0, "bull": False}
    kf, ks = 2 / (fast + 1), 2 / (slow + 1)
    ef = es = closes[0]
    line = []
    for c in closes:
        ef = c * kf + ef * (1 - kf)
        es = c * ks + es * (1 - ks)
        line.append(ef - es)
    sig_val = ema(line, signal)
    hist    = line[-1] - sig_val
    return {
        "macd":   line[-1],
        "signal": sig_val,
        "hist":   hist,
        "bull":   line[-1] > sig_val and hist > 0,
    }


def compute_bollinger(closes: list, period=20, std_mult=2.0) -> dict:
    if len(closes) < period:
        c = closes[-1]
        return {"upper": c * 1.02, "mid": c, "lower": c * 0.98, "pct_b": 0.5}
    window = closes[-period:]
    mid    = sum(window) / period
    std    = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    upper  = mid + std_mult * std
    lower  = mid - std_mult * std
    width  = upper - lower or 1
    pct_b  = (closes[-1] - lower) / width
    return {"upper": upper, "mid": mid, "lower": lower, "pct_b": pct_b}


def detect_candlestick_patterns(klines: list) -> dict:
    """يكتشف الأنماط الشمعية الأساسية"""
    if len(klines) < 3:
        return {}

    patterns = {}

    def candle(k):
        o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        body  = abs(c - o)
        rng   = h - l or 1e-9
        upper = h - max(o, c)
        lower = min(o, c) - l
        return o, h, l, c, body, rng, upper, lower

    o1, h1, l1, c1, b1, r1, u1, lo1 = candle(klines[-3])
    o2, h2, l2, c2, b2, r2, u2, lo2 = candle(klines[-2])
    o3, h3, l3, c3, b3, r3, u3, lo3 = candle(klines[-1])

    # Hammer (مطرقة)
    if lo3 > b3 * 2 and u3 < b3 * 0.3 and c3 > o3:
        patterns["hammer"] = True

    # Bullish Engulfing (ابتلاع صاعد)
    if c2 < o2 and c3 > o3 and c3 > o2 and o3 < c2:
        patterns["bullish_engulfing"] = True

    # Morning Star (نجمة الصباح)
    if (c1 < o1 and b2 < b1 * 0.3 and c3 > o3
            and c3 > (o1 + c1) / 2):
        patterns["morning_star"] = True

    # Doji (دوجي صاعد)
    if b3 < r3 * 0.1 and lo3 > u3:
        patterns["doji_bullish"] = True

    # Bullish Marubozu (شمعة صاعدة قوية)
    if c3 > o3 and b3 / r3 > 0.85:
        patterns["marubozu_bull"] = True

    # Three White Soldiers (ثلاثة جنود بيض)
    if (c1 > o1 and c2 > o2 and c3 > o3
            and c2 > c1 and c3 > c2):
        patterns["three_white_soldiers"] = True

    return patterns


def analyze_volume(klines: list) -> dict:
    """تحليل حجم التداول"""
    vols   = [float(k[5]) for k in klines]
    closes = [float(k[4]) for k in klines]

    avg_vol_20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else sum(vols) / len(vols)
    cur_vol    = vols[-1]
    vol_ratio  = cur_vol / avg_vol_20 if avg_vol_20 > 0 else 1

    # OBV بسيط
    obv = 0
    obv_series = []
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv += vols[i]
        elif closes[i] < closes[i - 1]:
            obv -= vols[i]
        obv_series.append(obv)

    obv_trend = obv_series[-1] > obv_series[-10] if len(obv_series) >= 10 else True

    return {
        "vol_ratio":  round(vol_ratio, 2),
        "obv_rising": obv_trend,
        "high_vol":   vol_ratio > 1.5,
    }


def score_symbol(symbol: str) -> dict | None:
    """يُقيّم العملة بتحليل متكامل"""
    try:
        # تجنب العملات ذات الأداء السيء
        if is_blacklisted(symbol):
            return None

        # ── شموع 15 دقيقة ──────────────────────────
        klines_15 = client.futures_klines(symbol=symbol, interval="15m", limit=200)
        if len(klines_15) < 60:
            return None

        closes_15 = [float(k[4]) for k in klines_15]
        highs_15  = [float(k[2]) for k in klines_15]
        lows_15   = [float(k[3]) for k in klines_15]

        # ── شموع ساعة ───────────────────────────────
        klines_1h = client.futures_klines(symbol=symbol, interval="1h", limit=210)
        closes_1h = [float(k[4]) for k in klines_1h]

        # ── شموع 4 ساعة ─────────────────────────────
        klines_4h = client.futures_klines(symbol=symbol, interval="4h", limit=100)
        closes_4h = [float(k[4]) for k in klines_4h]

        # حجم
        ticker       = client.futures_ticker(symbol=symbol)
        quote_volume = float(ticker.get("quoteVolume", 0))
        price        = float(ticker["lastPrice"])

        if quote_volume < MIN_24H_QUOTE_VOLUME:
            return None
        if price <= 0:
            return None

        # ── حسابات ──────────────────────────────────
        rsi_15    = compute_rsi(closes_15)
        macd_15   = compute_macd(closes_15)
        bb_15     = compute_bollinger(closes_15)
        atr_15    = compute_atr(highs_15, lows_15, closes_15)
        patterns  = detect_candlestick_patterns(klines_15)
        vol_data  = analyze_volume(klines_15)

        ema20_1h  = ema(closes_1h, 20)
        ema50_1h  = ema(closes_1h, 50)
        ema200_1h = ema(closes_1h, 200)
        ema20_4h  = ema(closes_4h, 20)
        ema50_4h  = ema(closes_4h, 50)

        rsi_1h    = compute_rsi(closes_1h)
        macd_4h   = compute_macd(closes_4h)

        current   = closes_15[-1]

        # ── التقييم ──────────────────────────────────
        score = 0
        reasons = []

        # 1. اتجاه 1h (25 نقطة)
        if current > ema200_1h:
            score += 15; reasons.append("فوق EMA200")
        if ema20_1h > ema50_1h:
            score += 10; reasons.append("EMA20>EMA50 (1h)")

        # 2. اتجاه 4h (20 نقطة)
        if current > ema50_4h:
            score += 10; reasons.append("فوق EMA50 (4h)")
        if macd_4h["bull"]:
            score += 10; reasons.append("MACD صاعد (4h)")

        # 3. RSI (20 نقطة)
        if 40 <= rsi_15 <= 60:
            score += 20; reasons.append(f"RSI مثالي {rsi_15:.0f}")
        elif 30 <= rsi_15 < 40:
            score += 15; reasons.append(f"RSI تشبع بيع {rsi_15:.0f}")
        elif 60 < rsi_15 <= 65:
            score += 10; reasons.append(f"RSI مرتفع قليلاً {rsi_15:.0f}")
        elif rsi_15 > 70:
            score -= 10; reasons.append("RSI تشبع شراء — تخفيض")

        # 4. MACD 15m (15 نقطة)
        if macd_15["bull"]:
            score += 15; reasons.append("MACD صاعد (15m)")

        # 5. Bollinger (10 نقطة)
        if bb_15["pct_b"] < 0.35:
            score += 10; reasons.append("سعر عند القاع (BB)")
        elif bb_15["pct_b"] > 0.8:
            score -= 5

        # 6. الأنماط الشمعية (15 نقطة max)
        if patterns.get("bullish_engulfing"):
            score += 15; reasons.append("ابتلاع صاعد 🕯️")
        elif patterns.get("morning_star"):
            score += 15; reasons.append("نجمة الصباح 🌅")
        elif patterns.get("three_white_soldiers"):
            score += 15; reasons.append("ثلاثة جنود ⚔️")
        elif patterns.get("hammer"):
            score += 10; reasons.append("مطرقة 🔨")
        elif patterns.get("marubozu_bull"):
            score += 8; reasons.append("شمعة قوية")

        # 7. حجم (10 نقطة)
        if vol_data["high_vol"] and vol_data["obv_rising"]:
            score += 10; reasons.append(f"حجم مرتفع ×{vol_data['vol_ratio']}")
        elif vol_data["obv_rising"]:
            score += 5; reasons.append("OBV صاعد")

        # 8. سمعة العملة من التعلم (5 نقطة)
        sym_wr = get_symbol_win_rate(symbol)
        if sym_wr > 0.6:
            score += 5; reasons.append(f"أداء سابق جيد {sym_wr*100:.0f}%")

        if score < MIN_SCORE:
            return None

        # ATR للحماية
        atr = compute_atr(highs_15, lows_15, closes_15)

        return {
            "symbol":   symbol,
            "score":    score,
            "rsi":      round(rsi_15, 1),
            "price":    price,
            "atr":      atr,
            "reasons":  reasons,
            "patterns": list(patterns.keys()),
        }

    except Exception as e:
        if "-1022" not in str(e) and "-1000" not in str(e):
            log.warning(f"score_symbol {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  TRADE MANAGEMENT
# ══════════════════════════════════════════════════════════════

def open_long(candidate: dict) -> bool:
    symbol = candidate["symbol"]
    price  = candidate["price"]
    atr    = candidate["atr"]

    # تحقق من عدم وجود وضعية
    amt, _ = get_actual_position(symbol)
    if abs(amt) > 1e-8:
        return False

    if len(open_trades) >= MAX_OPEN_TRADES:
        log.info(f"⚠️ حد أقصى {MAX_OPEN_TRADES} صفقات مفتوحة")
        return False

    try:
        lot, tick, min_notional = get_filters(symbol)
        balance = get_futures_balance()
        avail   = get_available_margin()

        # حجم الصفقة بناءً على المخاطرة
        risk_usdt  = balance * RISK_PER_TRADE_PCT
        sl_distance = atr * learning_data["atr_multipliers"]["sl"]
        sl_pct      = sl_distance / price

        # حجم القطعة لتحديد الخسارة بالضبط
        raw_qty = (risk_usdt * LEVERAGE) / (price * sl_pct * LEVERAGE)
        # أو الطريقة البسيطة
        raw_qty2 = (avail * 0.9 * LEVERAGE) / price
        raw_qty  = min(raw_qty, raw_qty2)

        qty = round_qty(symbol, raw_qty)

        if qty <= 0:
            log.warning(f"⚠️ {symbol}: qty=0")
            return False

        notional = qty * price
        if notional < min_notional:
            log.warning(f"⚠️ {symbol}: notional={notional:.2f} < {min_notional}")
            return False

        # رافعة
        try:
            client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        except Exception as e:
            log.warning(f"رافعة {symbol}: {e}")

        # أمر الدخول
        for attempt in range(3):
            try:
                client.futures_create_order(
                    symbol=symbol, side=SIDE_BUY,
                    type=ORDER_TYPE_MARKET, quantity=qty
                )
                break
            except Exception as e:
                log.warning(f"أمر دخول {symbol} (محاولة {attempt+1}): {e}")
                time.sleep(1)
                if attempt == 2:
                    return False

        time.sleep(1.5)

        actual_amt, actual_entry = get_actual_position(symbol)
        if abs(actual_amt) < 1e-8:
            log.error(f"❌ {symbol}: أمر أُرسل لكن لا وضعية!")
            return False

        actual_qty   = abs(actual_amt)
        actual_entry = actual_entry or price

        # حماية كاملة
        prot = place_full_protection(symbol, actual_entry, actual_qty, atr)

        sl_price = actual_entry - atr * learning_data["atr_multipliers"]["sl"]
        tp_price = actual_entry + atr * learning_data["atr_multipliers"]["tp"]

        open_trades[symbol] = {
            "entry":       actual_entry,
            "qty":         actual_qty,
            "open_time":   utcnow(),
            "atr":         atr,
            "score":       candidate["score"],
            "rsi":         candidate["rsi"],
            "reasons":     candidate.get("reasons", []),
            "sl_price":    sl_price,
            "tp_price":    tp_price,
        }

        prot_status = "✅" if all(prot.values()) else f"⚠️ SL:{prot['sl']} TP:{prot['tp']} Trail:{prot['trail']}"
        reasons_str = " | ".join(candidate.get("reasons", [])[:4])

        send_telegram(
            f"🚀 *دخول {symbol}*\n"
            f"سعر: `{actual_entry}` | كمية: `{actual_qty}`\n"
            f"SL: `{round_price(symbol, sl_price)}` | TP: `{round_price(symbol, tp_price)}`\n"
            f"ATR: `{atr:.6f}` | RR: `{(tp_price-actual_entry)/(actual_entry-sl_price):.2f}`\n"
            f"نقاط: `{candidate['score']}` | RSI: `{candidate['rsi']}`\n"
            f"📋 {reasons_str}\n"
            f"الحماية: {prot_status}"
        )
        log.info(f"✅ فتح {symbol} @ {actual_entry} | SL={round(sl_price,4)} TP={round(tp_price,4)}")
        return True

    except Exception as e:
        log.error(f"open_long {symbol}: {e}")
        return False


def monitor_trades():
    for symbol in list(open_trades.keys()):
        try:
            amt, _ = get_actual_position(symbol)

            if abs(amt) < 1e-8:
                # الصفقة مُغلقة
                trade    = open_trades.pop(symbol)
                duration = utcnow() - trade["open_time"]
                dur_min  = duration.total_seconds() / 60

                try:
                    exit_p  = float(client.futures_symbol_ticker(symbol=symbol)["price"])
                    pnl_pct = ((exit_p - trade["entry"]) / trade["entry"]) * 100 * LEVERAGE
                    emoji   = "🟢" if pnl_pct >= 0 else "🔴"

                    # تسجيل في نظام التعلم
                    record_closed_trade(
                        symbol     = symbol,
                        entry      = trade["entry"],
                        exit_price = exit_p,
                        pnl_pct    = pnl_pct,
                        duration_min = dur_min,
                        entry_rsi  = trade.get("rsi", 0),
                        entry_score = trade.get("score", 0),
                        atr        = trade.get("atr", 0),
                    )

                    wr = learning_data["win_rate"] * 100
                    send_telegram(
                        f"{emoji} *مُغلقة: {symbol}*\n"
                        f"دخول: `{trade['entry']}` → خروج: `{exit_p:.6f}`\n"
                        f"P&L: `{pnl_pct:+.2f}%` (رافعة {LEVERAGE}x)\n"
                        f"المدة: `{str(duration).split('.')[0]}`\n"
                        f"📊 نسبة الفوز الإجمالية: `{wr:.1f}%`"
                    )
                except Exception:
                    send_telegram(f"🏁 *مُغلقة: {symbol}*")

                log.info(f"صفقة مُغلقة: {symbol}")
                continue

            # تحقق من الحماية
            try:
                orders    = client.futures_get_open_orders(symbol=symbol)
                has_sl    = any(o["type"] == "STOP_MARKET"         for o in orders)
                has_tp    = any(o["type"] == "TAKE_PROFIT_MARKET"  for o in orders)
                has_trail = any(o["type"] == "TRAILING_STOP_MARKET" for o in orders)

                if not has_sl and not has_tp and not has_trail:
                    log.warning(f"🚨 {symbol}: لا حماية! إعادة وضعها...")
                    send_telegram(f"🚨 *{symbol}*: الحماية مفقودة — إعادة وضعها")
                    atr = open_trades[symbol].get("atr", open_trades[symbol]["entry"] * 0.01)
                    place_full_protection(symbol, open_trades[symbol]["entry"], abs(amt), atr)

                elif not has_sl:
                    sl_price = open_trades[symbol]["sl_price"]
                    _place_stop_market(symbol, sl_price, abs(amt))

                elif not has_tp:
                    tp_price = open_trades[symbol]["tp_price"]
                    _place_take_profit(symbol, tp_price, abs(amt))

                elif not has_trail:
                    entry = open_trades[symbol]["entry"]
                    _place_trailing_stop(symbol, entry * (1 + TRAILING_ACTIVATION_PCT), abs(amt))

            except Exception as e:
                log.error(f"فحص حماية {symbol}: {e}")

        except Exception as e:
            log.error(f"monitor_trades {symbol}: {e}")


def adopt_existing_positions():
    log.info("🔍 جلب الوضعيات المفتوحة...")
    adopted = 0
    try:
        for p in get_all_positions():
            sym   = p["symbol"]
            amt   = float(p["positionAmt"])
            entry = float(p["entryPrice"])

            if abs(amt) < 1e-8 or entry == 0:
                continue

            log.info(f"وضعية موجودة: {sym} | كمية={amt} | دخول={entry}")

            if amt < 0:
                send_telegram(f"⚠️ وضعية SHORT في `{sym}` (كمية: {amt}) — راجعها يدوياً")
                continue

            # تقدير ATR
            try:
                klines = client.futures_klines(symbol=sym, interval="15m", limit=30)
                highs  = [float(k[2]) for k in klines]
                lows   = [float(k[3]) for k in klines]
                closes = [float(k[4]) for k in klines]
                atr    = compute_atr(highs, lows, closes)
            except Exception:
                atr = entry * 0.01

            open_trades[sym] = {
                "entry":     entry,
                "qty":       abs(amt),
                "open_time": utcnow(),
                "atr":       atr,
                "score":     0,
                "rsi":       50,
                "reasons":   ["وضعية موروثة"],
                "sl_price":  entry - atr * learning_data["atr_multipliers"]["sl"],
                "tp_price":  entry + atr * learning_data["atr_multipliers"]["tp"],
            }

            try:
                orders    = client.futures_get_open_orders(symbol=sym)
                has_sl    = any(o["type"] == "STOP_MARKET"         for o in orders)
                has_tp    = any(o["type"] == "TAKE_PROFIT_MARKET"  for o in orders)
                has_trail = any(o["type"] == "TRAILING_STOP_MARKET" for o in orders)

                if not has_sl or not has_tp:
                    log.warning(f"⚠️ {sym}: حماية ناقصة — إعادة وضعها")
                    place_full_protection(sym, entry, abs(amt), atr)
                else:
                    log.info(f"✅ {sym}: حماية موجودة")
            except Exception as e:
                log.error(f"فحص حماية {sym}: {e}")

            adopted += 1

    except Exception as e:
        log.error(f"adopt_existing_positions: {e}")

    msg = f"🔄 *تبنّي الوضعيات*\nLONG مفتوحة: `{adopted}`\n"
    for sym, t in open_trades.items():
        msg += f"  • `{sym}`: دخول `{t['entry']}` | كمية `{t['qty']}`\n"
    if not open_trades:
        msg += "لا توجد وضعيات مفتوحة."
    send_telegram(msg)
    log.info(f"تبنّي: {adopted} وضعية")


# ══════════════════════════════════════════════════════════════
#  RISK MANAGEMENT
# ══════════════════════════════════════════════════════════════

def close_all_futures(reason: str):
    send_telegram(f"🚨 *إغلاق إجباري*\nالسبب: {reason}")
    for p in get_all_positions():
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
        except Exception as e:
            log.error(f"فشل إغلاق {sym}: {e}")


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


def send_daily_report(balance: float):
    global _last_report_date
    today = utcnow().date()
    if _last_report_date == today:
        return
    _last_report_date = today

    try:
        positions = [p for p in get_all_positions() if abs(float(p["positionAmt"])) > 1e-8]
        d = (daily_start_balance - balance) / daily_start_balance * 100 if daily_start_balance else 0
        t = (bot_start_balance  - balance) / bot_start_balance  * 100 if bot_start_balance  else 0

        msg  = f"📊 *تقرير يومي — {today}*\n"
        msg += f"الرصيد: `{balance:.2f}` USDT\n"
        msg += f"اليوم: `{d:.2f}%` | إجمالي: `{t:.2f}%`\n"
        msg += f"عقود مفتوحة: `{len(positions)}`\n"
        msg += f"نسبة الفوز: `{learning_data['win_rate']*100:.1f}%` ({learning_data['total_trades']} صفقة)\n"
        msg += f"ATR SL: `{learning_data['atr_multipliers']['sl']:.2f}x` | TP: `{learning_data['atr_multipliers']['tp']:.2f}x`\n"

        for p in positions:
            upnl = float(p["unRealizedProfit"])
            msg += f"  • `{p['symbol']}` | دخول:`{p['entryPrice']}` | P&L:`{upnl:+.2f}$`\n"

        # أفضل وأسوأ عملة من التعلم
        stats = learning_data["symbol_stats"]
        if stats:
            best  = max(stats.items(), key=lambda x: x[1]["wins"] / max(x[1]["wins"]+x[1]["losses"], 1))
            worst = min(stats.items(), key=lambda x: x[1]["wins"] / max(x[1]["wins"]+x[1]["losses"], 1))
            msg += f"\n🏆 أفضل عملة: `{best[0]}`\n💔 أسوأ عملة: `{worst[0]}`"

        send_telegram(msg)
    except Exception as e:
        log.error(f"send_daily_report: {e}")


# ══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════

def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date, client

    log.info("🚀 تهيئة البوت...")

    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
    load_learning()

    initial = get_futures_balance()
    bot_start_balance   = initial
    daily_start_balance = initial
    daily_reset_date    = utcnow().date()

    all_symbols = _get_all_symbols()

    send_telegram(
        f"🤖 *بوت التداول الذكي v3.0*\n"
        f"رصيد: `{initial:.2f}` USDT\n"
        f"رافعة: `{LEVERAGE}x` | مخاطرة: `{RISK_PER_TRADE_PCT*100:.0f}%`\n"
        f"ATR SL×`{learning_data['atr_multipliers']['sl']:.1f}` TP×`{learning_data['atr_multipliers']['tp']:.1f}`\n"
        f"عملات للفحص: `{len(all_symbols)}`\n"
        f"نسبة الفوز التاريخية: `{learning_data['win_rate']*100:.1f}%`"
    )

    adopt_existing_positions()

    cycle = 0
    while True:
        cycle += 1
        try:
            balance = get_futures_balance()
            avail   = get_available_margin()
            log.info(f"══ الدورة #{cycle} | رصيد:{balance:.2f} | متاح:{avail:.2f} | صفقات:{len(open_trades)} ══")

            monitor_trades()

            if not check_protection(balance):
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            if avail < 2.0 or len(open_trades) >= MAX_OPEN_TRADES:
                log.info(f"تخطي الفحص — متاح:{avail:.2f} | صفقات:{len(open_trades)}/{MAX_OPEN_TRADES}")
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            # ── فحص العملات ──────────────────────────
            candidates = []
            # تجديد قائمة الرموز كل 100 دورة
            if cycle % 100 == 0:
                _all_symbols_cache.clear()
                all_symbols = _get_all_symbols()

            for symbol in all_symbols:
                if symbol in open_trades:
                    continue

                r = score_symbol(symbol)
                if r is None:
                    continue

                candidates.append(r)

            if candidates:
                candidates.sort(key=lambda x: (-x["score"], x["rsi"]))
                log.info(f"مرشحون: {[(c['symbol'], c['score']) for c in candidates[:5]]}")

                for c in candidates:
                    if len(open_trades) >= MAX_OPEN_TRADES:
                        break
                    avail = get_available_margin()
                    if avail < 2.0:
                        break
                    if open_long(c):
                        time.sleep(2)
            else:
                log.info("لا فرص مناسبة حالياً.")

            # تقرير يومي
            now = utcnow()
            if now.hour == 0 and now.minute < 2:
                send_daily_report(balance)

        except Exception as e:
            log.error(f"main_loop: {e}")
            send_telegram(f"⚠️ خطأ في البوت:\n`{e}`")

        time.sleep(SCAN_INTERVAL_SEC)


# ══════════════════════════════════════════════════════════════
#  FLASK
# ══════════════════════════════════════════════════════════════

@app.route("/")
def home():
    wr   = learning_data["win_rate"] * 100
    tot  = learning_data["total_trades"]
    lines = [
        f"<b>🤖 Trading Bot v3.0</b>",
        f"صفقات مفتوحة: {len(open_trades)}",
        f"نسبة الفوز: {wr:.1f}% ({tot} صفقة)",
        f"ATR SL×{learning_data['atr_multipliers']['sl']:.2f} TP×{learning_data['atr_multipliers']['tp']:.2f}",
        "<hr>"
    ]
    for sym, t in open_trades.items():
        lines.append(f"• <b>{sym}</b>: entry={t['entry']} qty={t['qty']} score={t.get('score',0)}")
    return "<br>".join(lines)


@app.route("/stats")
def stats():
    return json.dumps(learning_data["symbol_stats"], ensure_ascii=False, indent=2)


@app.route("/learning")
def learning():
    summary = {
        "win_rate":        learning_data["win_rate"],
        "total_trades":    learning_data["total_trades"],
        "profitable":      learning_data["profitable_trades"],
        "atr_multipliers": learning_data["atr_multipliers"],
        "top_symbols":     sorted(
            [(s, v["wins"]/(v["wins"]+v["losses"])) for s, v in learning_data["symbol_stats"].items() if v["wins"]+v["losses"] >= 3],
            key=lambda x: -x[1]
        )[:10],
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import os as _os
    threading.Thread(target=main_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(_os.environ.get("PORT", 10000)))
