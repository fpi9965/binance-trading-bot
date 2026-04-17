"""
=============================================================
  SMART TRADING BOT v5.0 — بوت التداول الذكي
  ─────────────────────────────────────────
  ✅ Compounding — حجم الصفقة يكبر مع الرصيد تلقائياً
  ✅ Dynamic Risk — يزيد المخاطرة عند الفوز ويخفضها عند الخسارة
  ✅ Pyramid Entries — يضيف لمراكز رابحة عند تأكيد الاتجاه
  ✅ Market Filter — يتوقف في السوق الهابط ويعمل في الصاعد
  ✅ الحماية الداخلية الكاملة (بدون STOP_MARKET/TP Binance)
  ✅ Breakeven تلقائي + Trailing ذكي يحمي الأرباح
  ✅ تعلم ذاتي يطور المعاملات مع الوقت
=============================================================
"""

import os, time, math, logging, threading, json, statistics
from datetime import datetime, timezone

from binance.client import Client
from binance.enums import *
from flask import Flask

# ─── CREDENTIALS ─────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_API_SECRET")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

# ─── إعدادات التداول الأساسية ─────────────────────────────────
LEVERAGE           = 10
MAX_OPEN_TRADES    = 6
SCAN_INTERVAL_SEC  = 40

# ─── Dynamic Risk — يتغير تلقائياً ───────────────────────────
BASE_RISK_PCT      = 0.02    # 2% أساس
MIN_RISK_PCT       = 0.01    # 1% حد أدنى (بعد خسائر)
MAX_RISK_PCT       = 0.05    # 5% حد أقصى (بعد فوز متتالي)
RISK_STEP_WIN      = 0.003   # +0.3% لكل فوز متتالي
RISK_STEP_LOSS     = 0.005   # -0.5% لكل خسارة

# ─── Pyramid — إضافة لمراكز رابحة ────────────────────────────
PYRAMID_ENABLED        = True
PYRAMID_TRIGGER_PCT    = 0.020   # يضيف عند +2% ربح
PYRAMID_MAX_ADDS       = 2       # أقصى إضافتين لنفس العملة
PYRAMID_SIZE_RATIO     = 0.5     # حجم الإضافة = 50% من الأصلي

# ─── الحماية الداخلية ─────────────────────────────────────────
ATR_SL_MULTIPLIER      = 2.0
ATR_TP_MULTIPLIER      = 3.5     # نسبة أعلى لتعظيم الأرباح
MIN_RR_RATIO           = 1.5
BREAKEVEN_TRIGGER_PCT  = 0.015   # 1.5% → SL للدخول
TRAILING_START_PCT     = 0.025   # 2.5% → trailing يبدأ
TRAILING_STEP_PCT      = 0.007
MAX_TRADE_HOURS        = 36

# ─── حماية الرصيد ─────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT   = 0.06
TOTAL_LOSS_LIMIT_PCT   = 0.18

# ─── فلتر السوق ──────────────────────────────────────────────
MARKET_FILTER_SYMBOL   = "BTCUSDT"   # مؤشر السوق
MARKET_FILTER_EMA      = 50          # BTC فوق EMA50 = سوق صاعد

# ─── فلترة العملات ───────────────────────────────────────────
MIN_24H_QUOTE_VOLUME   = 1_000_000
MIN_SCORE              = 40

LEARNING_FILE = "bot_learning.json"

# ─── Logging ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

app    = Flask(__name__)
client: Client = None

# ─── حالة البوت ──────────────────────────────────────────────
open_trades:        dict  = {}
_filters_cache:     dict  = {}
_all_symbols_cache: list  = []

bot_start_balance:   float = 0.0
daily_start_balance: float = 0.0
daily_reset_date           = None
bot_halted_total           = False
bot_halted_daily           = False
_last_report_date          = None
_market_is_bull            = True   # فلتر السوق

# ─── نظام التعلم + Dynamic Risk ──────────────────────────────
learning = {
    "trade_history":      [],
    "symbol_stats":       {},
    "atr_sl":             ATR_SL_MULTIPLIER,
    "atr_tp":             ATR_TP_MULTIPLIER,
    "win_rate":           0.0,
    "total_trades":       0,
    "profitable_trades":  0,
    "current_risk_pct":   BASE_RISK_PCT,   # يتغير ديناميكياً
    "consecutive_wins":   0,
    "consecutive_losses": 0,
    "peak_balance":       0.0,             # للـ Compounding
    "compounding_mult":   1.0,             # مضاعف الحجم
}


# ══════════════════════════════════════════════════════════════
#  TradeState — حالة كل صفقة
# ══════════════════════════════════════════════════════════════

class TradeState:
    def __init__(self, symbol, entry, qty, atr,
                 score=0, rsi=50, reasons=None, is_pyramid=False, pyramid_level=0):
        self.symbol        = symbol
        self.entry         = entry
        self.qty           = qty
        self.atr           = atr
        self.score         = score
        self.rsi           = rsi
        self.reasons       = reasons or []
        self.is_pyramid    = is_pyramid
        self.pyramid_level = pyramid_level   # 0=أصلي, 1=إضافة1, 2=إضافة2
        self.pyramid_adds  = 0               # عدد الإضافات المنفذة
        self.open_time     = utcnow()

        sl_mult         = learning["atr_sl"]
        tp_mult         = learning["atr_tp"]
        self.sl_price   = entry - atr * sl_mult
        self.tp_price   = entry + atr * tp_mult

        risk   = entry - self.sl_price
        reward = self.tp_price - entry
        if risk > 0 and reward / risk < MIN_RR_RATIO:
            self.tp_price = entry + risk * MIN_RR_RATIO

        self.highest_price   = entry
        self.at_breakeven    = False
        self.trailing_active = False
        self.trail_sl        = self.sl_price
        self.last_notif_sl   = None

    def update(self, current_price: float) -> str:
        if current_price > self.highest_price:
            self.highest_price = current_price

        pnl_pct = (current_price - self.entry) / self.entry

        if current_price >= self.tp_price:
            return "tp_hit"
        if current_price <= self.trail_sl:
            return "sl_hit"

        # Trailing
        if pnl_pct >= TRAILING_START_PCT:
            new_trail = self.highest_price * (1 - TRAILING_STEP_PCT)
            if new_trail > self.trail_sl:
                self.trail_sl        = new_trail
                self.trailing_active = True
                if (self.last_notif_sl is None or
                        abs(new_trail - self.last_notif_sl) / self.entry > 0.003):
                    self.last_notif_sl = new_trail
                    return "trailing_move"

        # Breakeven
        elif pnl_pct >= BREAKEVEN_TRIGGER_PCT and not self.at_breakeven:
            self.at_breakeven = True
            self.trail_sl     = self.entry * 1.001
            self.last_notif_sl = self.trail_sl
            return "breakeven"

        # Pyramid trigger
        if (PYRAMID_ENABLED and not self.is_pyramid
                and self.pyramid_adds < PYRAMID_MAX_ADDS
                and pnl_pct >= PYRAMID_TRIGGER_PCT * (self.pyramid_adds + 1)):
            return "pyramid_trigger"

        return "none"

    def pnl_pct(self, price: float) -> float:
        return (price - self.entry) / self.entry * 100 * LEVERAGE

    def duration_hours(self) -> float:
        return (utcnow() - self.open_time).total_seconds() / 3600

    def rr(self) -> float:
        risk = self.entry - self.sl_price
        return (self.tp_price - self.entry) / risk if risk > 0 else 0


# ══════════════════════════════════════════════════════════════
#  LEARNING + DYNAMIC RISK + COMPOUNDING
# ══════════════════════════════════════════════════════════════

def load_learning():
    global learning
    try:
        if os.path.exists(LEARNING_FILE):
            with open(LEARNING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                learning.update(data)
            log.info(f"📚 تعلم محمّل | صفقات:{learning['total_trades']} | Win%:{learning['win_rate']*100:.1f}% | Risk:{learning['current_risk_pct']*100:.1f}%")
    except Exception as e:
        log.error(f"load_learning: {e}")


def save_learning():
    try:
        with open(LEARNING_FILE, "w", encoding="utf-8") as f:
            json.dump(learning, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_learning: {e}")


def update_dynamic_risk(won: bool, balance: float):
    """
    يضبط المخاطرة ديناميكياً:
    - فوز متتالي → زيادة تدريجية حتى MAX
    - خسارة → تخفيض سريع للحماية
    - Compounding → حجم يكبر مع الرصيد
    """
    risk = learning["current_risk_pct"]

    if won:
        learning["consecutive_wins"]   += 1
        learning["consecutive_losses"]  = 0
        # زيادة تدريجية
        risk = min(risk + RISK_STEP_WIN, MAX_RISK_PCT)
        log.info(f"📈 Risk ↑ {risk*100:.2f}% (فوز متتالي: {learning['consecutive_wins']})")
    else:
        learning["consecutive_losses"] += 1
        learning["consecutive_wins"]    = 0
        # تخفيض أسرع عند الخسارة
        risk = max(risk - RISK_STEP_LOSS, MIN_RISK_PCT)
        log.info(f"📉 Risk ↓ {risk*100:.2f}% (خسارة متتالية: {learning['consecutive_losses']})")

    learning["current_risk_pct"] = risk

    # Compounding Multiplier — يُحسب من نمو الرصيد
    if learning["peak_balance"] > 0:
        growth = balance / learning["peak_balance"]
        learning["compounding_mult"] = max(1.0, growth)
    if balance > learning["peak_balance"]:
        learning["peak_balance"] = balance


def record_trade(trade: TradeState, exit_price: float, balance: float):
    won  = exit_price > trade.entry
    pnl  = trade.pnl_pct(exit_price)

    learning["trade_history"].append({
        "symbol":   trade.symbol,
        "entry":    trade.entry,
        "exit":     exit_price,
        "pnl_pct":  round(pnl, 3),
        "duration": round(trade.duration_hours() * 60, 1),
        "rsi":      trade.rsi,
        "score":    trade.score,
        "atr":      round(trade.atr, 8),
        "pyramid":  trade.is_pyramid,
        "won":      won,
        "ts":       utcnow().isoformat(),
    })
    if len(learning["trade_history"]) > 500:
        learning["trade_history"] = learning["trade_history"][-500:]

    sym_st = learning["symbol_stats"].setdefault(
        trade.symbol, {"wins": 0, "losses": 0, "pnl": 0.0}
    )
    if won:
        sym_st["wins"] += 1
    else:
        sym_st["losses"] += 1
    sym_st["pnl"] += pnl

    learning["total_trades"]      += 1
    if won:
        learning["profitable_trades"] += 1
    learning["win_rate"] = learning["profitable_trades"] / learning["total_trades"]

    update_dynamic_risk(won, balance)
    _adapt_atr(won)
    save_learning()

    log.info(f"📊 {trade.symbol} {'✅' if won else '❌'} {pnl:+.2f}% | Win%:{learning['win_rate']*100:.1f}% | Risk:{learning['current_risk_pct']*100:.1f}%")


def _adapt_atr(won: bool):
    """يضبط ATR multipliers بناءً على آخر 30 صفقة"""
    history = learning["trade_history"]
    if len(history) < 15:
        return
    recent    = history[-30:]
    loss_rate = sum(1 for t in recent if not t["won"]) / len(recent)

    if loss_rate > 0.55:
        learning["atr_sl"] = min(learning["atr_sl"] * 1.08, 4.0)
        log.info(f"🎓 ATR SL ↑ {learning['atr_sl']:.2f}")
    elif loss_rate < 0.28:
        learning["atr_sl"] = max(learning["atr_sl"] * 0.96, 1.3)
        log.info(f"🎓 ATR SL ↓ {learning['atr_sl']:.2f}")

    wins = [t for t in recent if t["won"]]
    if wins:
        avg_win = statistics.mean(t["pnl_pct"] for t in wins)
        if avg_win > 10:
            learning["atr_tp"] = min(learning["atr_tp"] * 1.05, 8.0)
        elif avg_win < 3:
            learning["atr_tp"] = max(learning["atr_tp"] * 0.97, 2.0)


def is_blacklisted(symbol: str) -> bool:
    st = learning["symbol_stats"].get(symbol)
    if not st:
        return False
    total = st["wins"] + st["losses"]
    if total < 6:
        return False
    return (st["wins"] / total) < 0.28


def sym_win_rate(symbol: str) -> float:
    st = learning["symbol_stats"].get(symbol)
    if not st:
        return 0.5
    total = st["wins"] + st["losses"]
    return st["wins"] / total if total else 0.5


def get_effective_risk() -> float:
    """المخاطرة الفعلية = ديناميكية × Compounding"""
    base    = learning["current_risk_pct"]
    comp    = min(learning["compounding_mult"], 2.0)  # حد Compounding ×2
    return min(base * comp, MAX_RISK_PCT)


# ══════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "YOUR_TOKEN":
        return
    try:
        import requests as _r
        _r.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
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
        log.error(f"balance: {e}")
    return 0.0


def get_available_margin() -> float:
    try:
        return float(client.futures_account()["availableBalance"])
    except Exception as e:
        log.error(f"margin: {e}")
    return 0.0


def get_all_positions() -> list:
    try:
        return client.futures_position_information()
    except Exception as e:
        log.error(f"positions: {e}")
        return []


def get_actual_position(symbol: str) -> tuple:
    try:
        for p in client.futures_position_information(symbol=symbol):
            return float(p["positionAmt"]), float(p["entryPrice"])
    except Exception as e:
        if "-1022" not in str(e):
            log.warning(f"actual_pos {symbol}: {e}")
    return 0.0, 0.0


def get_current_price(symbol: str) -> float:
    try:
        return float(client.futures_symbol_ticker(symbol=symbol)["price"])
    except Exception as e:
        log.error(f"price {symbol}: {e}")
    return 0.0


def get_filters(symbol: str) -> tuple:
    if symbol in _filters_cache:
        return _filters_cache[symbol]
    if not _filters_cache:
        try:
            for s in client.futures_exchange_info()["symbols"]:
                sym  = s["symbol"]
                lot  = tick = None
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
            log.error(f"filters: {e}")
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
        return round(price, 6)
    prec = max(0, round(-math.log10(tick)))
    return float(f"{price:.{prec}f}")


def _get_all_symbols() -> list:
    global _all_symbols_cache
    if _all_symbols_cache:
        return _all_symbols_cache
    try:
        symbols = []
        for s in client.futures_exchange_info()["symbols"]:
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
        log.info(f"📋 عملات: {len(symbols)}")
        return symbols
    except Exception as e:
        log.error(f"symbols: {e}")
        return []


# ══════════════════════════════════════════════════════════════
#  MARKET FILTER — فلتر السوق العام
# ══════════════════════════════════════════════════════════════

def update_market_filter():
    """يتحقق إذا BTC فوق EMA50 → سوق صاعد"""
    global _market_is_bull
    try:
        kl  = client.futures_klines(symbol=MARKET_FILTER_SYMBOL, interval="1h", limit=60)
        cls = [float(k[4]) for k in kl]
        k   = 2 / (MARKET_FILTER_EMA + 1)
        v   = sum(cls[:MARKET_FILTER_EMA]) / MARKET_FILTER_EMA
        for c in cls[MARKET_FILTER_EMA:]:
            v = c * k + v * (1 - k)
        ema50        = v
        prev         = _market_is_bull
        _market_is_bull = cls[-1] >= ema50 * 0.98   # هامش 2%
        if prev != _market_is_bull:
            status = "🟢 صاعد" if _market_is_bull else "🔴 هابط"
            send_telegram(f"📡 *تغيير اتجاه السوق: {status}*\nBTC: `{cls[-1]:.2f}` | EMA50: `{ema50:.2f}`")
            log.info(f"Market filter → {'BULL' if _market_is_bull else 'BEAR'}")
    except Exception as e:
        log.error(f"market_filter: {e}")


# ══════════════════════════════════════════════════════════════
#  MARKET ORDER
# ══════════════════════════════════════════════════════════════

def market_close(symbol: str, qty: float) -> bool:
    qty = abs(qty)
    if qty <= 0:
        return False
    for attempt in range(3):
        try:
            client.futures_create_order(
                symbol=symbol,
                side=SIDE_SELL,
                type=ORDER_TYPE_MARKET,
                quantity=qty,
                reduceOnly=True,
            )
            log.info(f"✅ إغلاق: {symbol} qty={qty}")
            return True
        except Exception as e:
            log.warning(f"market_close {symbol} #{attempt+1}: {e}")
            time.sleep(1)
    log.error(f"❌ فشل إغلاق {symbol}")
    return False


# ══════════════════════════════════════════════════════════════
#  TECHNICAL ANALYSIS
# ══════════════════════════════════════════════════════════════

def ema(values: list, period: int) -> float:
    if len(values) < period:
        return sum(values) / len(values)
    k = 2 / (period + 1)
    v = sum(values[:period]) / period
    for x in values[period:]:
        v = x * k + v * (1 - k)
    return v


def compute_rsi(closes: list, period=14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period or 1e-9
    return 100 - 100 / (1 + ag / al)


def compute_atr(highs, lows, closes, period=14) -> float:
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)
    if not trs:
        return closes[-1] * 0.01
    return sum(trs[-period:]) / min(period, len(trs))


def compute_macd_bull(closes, fast=12, slow=26, signal=9) -> bool:
    if len(closes) < slow + signal:
        return False
    kf, ks = 2 / (fast + 1), 2 / (slow + 1)
    ef = es = closes[0]
    line = []
    for c in closes:
        ef = c * kf + ef * (1 - kf)
        es = c * ks + es * (1 - ks)
        line.append(ef - es)
    sig_val = ema(line, signal)
    return line[-1] > sig_val and (line[-1] - sig_val) > 0


def compute_bb_pct(closes, period=20) -> float:
    if len(closes) < period:
        return 0.5
    window = closes[-period:]
    mid    = sum(window) / period
    std    = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    upper  = mid + 2 * std
    lower  = mid - 2 * std
    width  = upper - lower or 1
    return (closes[-1] - lower) / width


def detect_patterns(klines) -> list:
    found = []
    if len(klines) < 3:
        return found

    def c(k):
        o, h, l, cl = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        body = abs(cl - o)
        rng  = h - l or 1e-9
        return o, h, l, cl, body, rng, h - max(o, cl), min(o, cl) - l

    o1,h1,l1,c1,b1,r1,u1,lo1 = c(klines[-3])
    o2,h2,l2,c2,b2,r2,u2,lo2 = c(klines[-2])
    o3,h3,l3,c3,b3,r3,u3,lo3 = c(klines[-1])

    if lo3 > b3 * 2 and u3 < b3 * 0.3 and c3 > o3:
        found.append("hammer")
    if c2 < o2 and c3 > o3 and c3 > o2 and o3 < c2:
        found.append("bullish_engulfing")
    if c1 < o1 and b2 < b1 * 0.3 and c3 > o3 and c3 > (o1 + c1) / 2:
        found.append("morning_star")
    if c1 > o1 and c2 > o2 and c3 > o3 and c3 > c2 > c1:
        found.append("three_soldiers")
    if b3 / r3 > 0.85 and c3 > o3:
        found.append("strong_bull")
    return found


def score_symbol(symbol: str) -> dict | None:
    try:
        if is_blacklisted(symbol):
            return None

        kl15 = client.futures_klines(symbol=symbol, interval="15m", limit=210)
        if len(kl15) < 80:
            return None
        cl15 = [float(k[4]) for k in kl15]
        hi15 = [float(k[2]) for k in kl15]
        lo15 = [float(k[3]) for k in kl15]
        vo15 = [float(k[5]) for k in kl15]

        kl1h = client.futures_klines(symbol=symbol, interval="1h", limit=210)
        cl1h = [float(k[4]) for k in kl1h]

        kl4h = client.futures_klines(symbol=symbol, interval="4h", limit=100)
        cl4h = [float(k[4]) for k in kl4h]

        ticker = client.futures_ticker(symbol=symbol)
        vol24  = float(ticker.get("quoteVolume", 0))
        price  = float(ticker["lastPrice"])

        if vol24 < MIN_24H_QUOTE_VOLUME or price <= 0:
            return None

        rsi15    = compute_rsi(cl15)
        macd15   = compute_macd_bull(cl15)
        bb_pct   = compute_bb_pct(cl15)
        atr15    = compute_atr(hi15, lo15, cl15)
        patterns = detect_patterns(kl15)

        ema200_1h = ema(cl1h, 200)
        ema50_1h  = ema(cl1h, 50)
        ema20_1h  = ema(cl1h, 20)
        macd4h    = compute_macd_bull(cl4h)
        ema50_4h  = ema(cl4h, 50)
        rsi1h     = compute_rsi(cl1h)

        cur = cl15[-1]

        # حجم
        avg_vol   = sum(vo15[-20:]) / 20
        vol_ratio = vo15[-1] / avg_vol if avg_vol > 0 else 1

        # ── SCORE ──────────────────────────────────────────────
        score   = 0
        reasons = []

        # اتجاه طويل — 35
        if cur > ema200_1h:
            score += 15; reasons.append("↑EMA200")
        if ema20_1h > ema50_1h:
            score += 10; reasons.append("EMA20>50")
        if cur > ema50_4h:
            score += 5;  reasons.append("↑EMA50(4h)")
        if macd4h:
            score += 5;  reasons.append("MACD↑(4h)")

        # RSI — 20
        if 38 <= rsi15 <= 58:
            score += 20; reasons.append(f"RSI✓{rsi15:.0f}")
        elif 28 <= rsi15 < 38:
            score += 15; reasons.append(f"RSI-OS{rsi15:.0f}")
        elif 58 < rsi15 <= 65:
            score += 8;  reasons.append(f"RSI~{rsi15:.0f}")
        elif rsi15 > 70:
            score -= 15

        # RSI 1h تأكيد
        if rsi1h < 65:
            score += 5; reasons.append(f"RSI1h{rsi1h:.0f}")

        # MACD 15m — 15
        if macd15:
            score += 15; reasons.append("MACD↑")

        # Bollinger — 10
        if bb_pct < 0.30:
            score += 10; reasons.append("BB-low")
        elif bb_pct > 0.85:
            score -= 5

        # أنماط — 15 max
        pattern_score = {
            "morning_star":      15,
            "bullish_engulfing": 15,
            "three_soldiers":    12,
            "hammer":            10,
            "strong_bull":       8,
        }
        best = max((pattern_score.get(p, 0) for p in patterns), default=0)
        if best:
            score += best
            reasons.append(f"🕯️{patterns[0]}")

        # حجم — 10
        if vol_ratio > 1.8:
            score += 10; reasons.append(f"Vol×{vol_ratio:.1f}")
        elif vol_ratio > 1.3:
            score += 5;  reasons.append(f"Vol×{vol_ratio:.1f}")

        # سمعة
        wr = sym_win_rate(symbol)
        if wr > 0.62:
            score += 5; reasons.append(f"WR{wr*100:.0f}%")

        if score < MIN_SCORE:
            return None

        return {
            "symbol":   symbol,
            "score":    score,
            "rsi":      round(rsi15, 1),
            "price":    price,
            "atr":      atr15,
            "reasons":  reasons,
            "patterns": patterns,
        }

    except Exception as e:
        if "-1022" not in str(e) and "-1000" not in str(e):
            log.warning(f"score {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  PROTECTION MONITOR — Thread مستقل كل 5 ثواني
# ══════════════════════════════════════════════════════════════

def protection_monitor():
    while True:
        try:
            for symbol in list(open_trades.keys()):
                trade = open_trades.get(symbol)
                if trade is None:
                    continue

                amt, _ = get_actual_position(symbol)
                if abs(amt) < 1e-8:
                    _handle_closed_externally(symbol, trade)
                    continue

                current = get_current_price(symbol)
                if current <= 0:
                    continue

                # Timeout
                if trade.duration_hours() >= MAX_TRADE_HOURS:
                    log.warning(f"⏰ {symbol}: timeout — إغلاق")
                    _execute_close(symbol, trade, current, "timeout")
                    continue

                event = trade.update(current)

                if event == "sl_hit":
                    _execute_close(symbol, trade, current, "sl")

                elif event == "tp_hit":
                    _execute_close(symbol, trade, current, "tp")

                elif event == "breakeven":
                    send_telegram(
                        f"🔒 *Breakeven: {symbol}*\n"
                        f"سعر:`{current:.6f}` → SL:`{trade.trail_sl:.6f}`\n"
                        f"P&L: `+{trade.pnl_pct(current):.2f}%`"
                    )

                elif event == "trailing_move":
                    send_telegram(
                        f"📈 *Trailing ↑ {symbol}*\n"
                        f"سعر:`{current:.6f}` | SL:`{trade.trail_sl:.6f}`\n"
                        f"P&L: `+{trade.pnl_pct(current):.2f}%`"
                    )

                elif event == "pyramid_trigger" and not trade.is_pyramid:
                    _execute_pyramid(symbol, trade, current)

        except Exception as e:
            log.error(f"protection_monitor: {e}")

        time.sleep(5)


def _execute_close(symbol: str, trade: TradeState, current_price: float, reason: str):
    amt, _ = get_actual_position(symbol)
    if abs(amt) < 1e-8:
        open_trades.pop(symbol, None)
        return

    ok = market_close(symbol, abs(amt))
    if ok:
        open_trades.pop(symbol, None)
        pnl   = trade.pnl_pct(current_price)
        emoji = "🟢" if pnl >= 0 else "🔴"
        dur   = f"{trade.duration_hours():.1f}h"

        balance = get_futures_balance()
        record_trade(trade, current_price, balance)

        reason_txt = {
            "sl":      "وقف الخسارة ⛔",
            "tp":      "جني الأرباح 💰",
            "timeout": f"انتهاء {MAX_TRADE_HOURS}h ⏰",
        }.get(reason, reason)

        risk_now = learning["current_risk_pct"] * 100
        comp     = learning["compounding_mult"]

        send_telegram(
            f"{emoji} *مُغلقة: {symbol}*\n"
            f"السبب: {reason_txt}\n"
            f"دخول:`{trade.entry:.6f}` → خروج:`{current_price:.6f}`\n"
            f"P&L: `{pnl:+.2f}%` (×{LEVERAGE})\n"
            f"المدة: `{dur}` | أعلى: `{trade.highest_price:.6f}`\n"
            f"─────────────────\n"
            f"📊 Win%:`{learning['win_rate']*100:.1f}%` | Risk:`{risk_now:.1f}%` | Comp:`×{comp:.2f}`\n"
            f"💰 رصيد:`{balance:.2f}` USDT"
        )
    else:
        send_telegram(f"🚨 *فشل إغلاق {symbol}* — راجع يدوياً!")


def _handle_closed_externally(symbol: str, trade: TradeState):
    open_trades.pop(symbol, None)
    current = get_current_price(symbol)
    if current > 0:
        pnl   = trade.pnl_pct(current)
        emoji = "🟢" if pnl >= 0 else "🔴"
        balance = get_futures_balance()
        record_trade(trade, current, balance)
        send_telegram(
            f"{emoji} *خارجية: {symbol}*\n"
            f"P&L تقريبي: `{pnl:+.2f}%`\n"
            f"Win%:`{learning['win_rate']*100:.1f}%`"
        )


def _execute_pyramid(symbol: str, trade: TradeState, current_price: float):
    """
    Pyramid — يضيف مركز إضافي للصفقة الرابحة.
    الحجم = 50% من الأصلي. SL يُرفع للمستوى الأحدث.
    """
    if trade.pyramid_adds >= PYRAMID_MAX_ADDS:
        return
    if len(open_trades) >= MAX_OPEN_TRADES + PYRAMID_MAX_ADDS:
        return

    avail = get_available_margin()
    if avail < 5.0:
        return

    # حجم الإضافة
    add_qty_raw = trade.qty * PYRAMID_SIZE_RATIO
    add_qty     = round_qty(symbol, add_qty_raw)

    if add_qty <= 0:
        return

    notional = add_qty * current_price
    _, _, min_notional = get_filters(symbol)
    if notional < min_notional:
        return

    try:
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=add_qty,
        )
        time.sleep(1.0)

        amt, new_entry = get_actual_position(symbol)
        if abs(amt) < 1e-8:
            return

        # رفع SL للدخول الأصلي (breakeven الجديد)
        if not trade.at_breakeven:
            trade.trail_sl    = trade.entry * 1.001
            trade.at_breakeven = True

        trade.pyramid_adds += 1
        level = trade.pyramid_adds

        log.info(f"🔺 Pyramid L{level}: {symbol} +{add_qty} @ {current_price:.6f}")
        send_telegram(
            f"🔺 *Pyramid L{level}: {symbol}*\n"
            f"إضافة: `{add_qty}` @ `{current_price:.6f}`\n"
            f"SL الجديد: `{trade.trail_sl:.6f}` (Breakeven)\n"
            f"P&L الحالي: `+{trade.pnl_pct(current_price):.2f}%`"
        )

    except Exception as e:
        log.error(f"pyramid {symbol}: {e}")


# ══════════════════════════════════════════════════════════════
#  OPEN POSITION
# ══════════════════════════════════════════════════════════════

def open_long(candidate: dict) -> bool:
    symbol = candidate["symbol"]
    price  = candidate["price"]
    atr    = candidate["atr"]

    amt, _ = get_actual_position(symbol)
    if abs(amt) > 1e-8:
        return False
    if len(open_trades) >= MAX_OPEN_TRADES:
        return False

    try:
        lot, tick, min_notional = get_filters(symbol)
        balance = get_futures_balance()
        avail   = get_available_margin()

        # ── Compounding + Dynamic Risk ────────────────────────
        effective_risk = get_effective_risk()
        sl_distance    = atr * learning["atr_sl"]
        sl_pct         = sl_distance / price if price > 0 else 0.02

        qty_by_risk  = (balance * effective_risk) / (price * sl_pct)
        qty_by_avail = (avail * 0.88 * LEVERAGE) / price
        raw_qty      = min(qty_by_risk, qty_by_avail)
        qty          = round_qty(symbol, raw_qty)

        if qty <= 0:
            return False

        notional = qty * price
        if notional < min_notional:
            return False

        # رافعة
        try:
            client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        except Exception as e:
            log.warning(f"leverage {symbol}: {e}")

        # أمر الدخول
        for attempt in range(3):
            try:
                client.futures_create_order(
                    symbol=symbol, side=SIDE_BUY,
                    type=ORDER_TYPE_MARKET, quantity=qty
                )
                break
            except Exception as e:
                log.warning(f"entry {symbol} #{attempt+1}: {e}")
                time.sleep(1)
                if attempt == 2:
                    return False

        time.sleep(1.5)
        actual_amt, actual_entry = get_actual_position(symbol)

        if abs(actual_amt) < 1e-8:
            log.error(f"❌ {symbol}: لا وضعية بعد الأمر")
            return False

        actual_qty   = abs(actual_amt)
        actual_entry = actual_entry or price

        trade = TradeState(
            symbol  = symbol,
            entry   = actual_entry,
            qty     = actual_qty,
            atr     = atr,
            score   = candidate["score"],
            rsi     = candidate["rsi"],
            reasons = candidate.get("reasons", []),
        )
        open_trades[symbol] = trade

        rr       = trade.rr()
        risk_pct = effective_risk * 100
        comp     = learning["compounding_mult"]

        send_telegram(
            f"🚀 *دخول {symbol}*\n"
            f"سعر:`{actual_entry:.6f}` | كمية:`{actual_qty}`\n"
            f"SL:`{trade.sl_price:.6f}` | TP:`{trade.tp_price:.6f}`\n"
            f"RR:`{rr:.2f}` | ATR:`{atr:.6f}`\n"
            f"─────────────────\n"
            f"⚙️ Risk:`{risk_pct:.1f}%` | Comp:`×{comp:.2f}`\n"
            f"فوز متتالي:`{learning['consecutive_wins']}` | خسارة:`{learning['consecutive_losses']}`\n"
            f"Breakeven عند`+{BREAKEVEN_TRIGGER_PCT*100:.1f}%` | Pyramid عند`+{PYRAMID_TRIGGER_PCT*100:.1f}%`\n"
            f"📋 {' | '.join(candidate.get('reasons',[])[:4])}"
        )
        log.info(f"✅ {symbol} @ {actual_entry:.6f} Risk={risk_pct:.1f}% Comp=×{comp:.2f}")
        return True

    except Exception as e:
        log.error(f"open_long {symbol}: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  ADOPT EXISTING
# ══════════════════════════════════════════════════════════════

def adopt_existing_positions():
    log.info("🔍 جلب الوضعيات...")
    adopted = 0
    try:
        for p in get_all_positions():
            sym   = p["symbol"]
            amt   = float(p["positionAmt"])
            entry = float(p["entryPrice"])

            if abs(amt) < 1e-8 or entry == 0 or sym in open_trades:
                continue
            if amt < 0:
                send_telegram(f"⚠️ SHORT في `{sym}` — راجع يدوياً")
                continue

            try:
                kl  = client.futures_klines(symbol=sym, interval="15m", limit=30)
                atr = compute_atr(
                    [float(k[2]) for k in kl],
                    [float(k[3]) for k in kl],
                    [float(k[4]) for k in kl],
                )
            except Exception:
                atr = entry * 0.01

            trade = TradeState(sym, entry, abs(amt), atr, reasons=["موروثة"])
            open_trades[sym] = trade
            adopted += 1

    except Exception as e:
        log.error(f"adopt: {e}")

    msg = f"🔄 *تبنّي* — {adopted} وضعية\n"
    for sym, t in open_trades.items():
        msg += f"  • `{sym}` @ `{t.entry:.6f}` | SL:`{t.sl_price:.6f}`\n"
    if not open_trades:
        msg += "لا وضعيات."
    send_telegram(msg)


# ══════════════════════════════════════════════════════════════
#  RISK MANAGEMENT
# ══════════════════════════════════════════════════════════════

def close_all_futures(reason: str):
    send_telegram(f"🚨 *إغلاق إجباري:* {reason}")
    for p in get_all_positions():
        amt = float(p["positionAmt"])
        if abs(amt) < 1e-8:
            continue
        sym  = p["symbol"]
        side = SIDE_SELL if amt > 0 else SIDE_BUY
        try:
            client.futures_create_order(
                symbol=sym, side=side, type=ORDER_TYPE_MARKET,
                quantity=abs(amt), reduceOnly=True
            )
            open_trades.pop(sym, None)
        except Exception as e:
            log.error(f"close_all {sym}: {e}")


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
        send_telegram(f"✅ يوم جديد | رصيد:`{balance:.2f}` USDT | Risk:`{learning['current_risk_pct']*100:.1f}%`")

    if daily_start_balance > 0:
        d = (daily_start_balance - balance) / daily_start_balance
        if d >= DAILY_LOSS_LIMIT_PCT:
            if not bot_halted_daily:
                bot_halted_daily = True
                close_all_futures(f"خسارة يومية {d*100:.1f}%")
            return False

    if bot_start_balance > 0:
        t = (bot_start_balance - balance) / bot_start_balance
        if t >= TOTAL_LOSS_LIMIT_PCT:
            bot_halted_total = True
            close_all_futures(f"خسارة إجمالية {t*100:.1f}%")
            send_telegram("🚨 *البوت متوقف نهائياً*")
            return False

    return True


def send_daily_report(balance: float):
    global _last_report_date
    today = utcnow().date()
    if _last_report_date == today:
        return
    _last_report_date = today
    try:
        d    = (daily_start_balance - balance) / daily_start_balance * 100 if daily_start_balance else 0
        t    = (bot_start_balance   - balance) / bot_start_balance   * 100 if bot_start_balance   else 0
        risk = learning["current_risk_pct"] * 100
        comp = learning["compounding_mult"]

        msg  = f"📊 *تقرير يومي — {today}*\n"
        msg += f"رصيد: `{balance:.2f}` USDT\n"
        msg += f"اليوم:`{d:.2f}%` | إجمالي:`{t:.2f}%`\n"
        msg += f"─────────────────\n"
        msg += f"Win%:`{learning['win_rate']*100:.1f}%` ({learning['total_trades']} صفقة)\n"
        msg += f"Risk الحالي:`{risk:.1f}%` | Comp:`×{comp:.2f}`\n"
        msg += f"فوز متتالي:`{learning['consecutive_wins']}` | ATR SL×`{learning['atr_sl']:.2f}`\n"
        msg += f"─────────────────\n"
        msg += f"مفتوحة:`{len(open_trades)}`\n"

        for sym, tr in open_trades.items():
            cp  = get_current_price(sym)
            pnl = tr.pnl_pct(cp)
            msg += f"  • `{sym}` P&L:`{pnl:+.2f}%` {'🔒' if tr.at_breakeven else ''} {'📈' if tr.trailing_active else ''} {'🔺×'+str(tr.pyramid_adds) if tr.pyramid_adds else ''}\n"

        # أفضل وأسوأ
        stats = learning["symbol_stats"]
        if stats:
            ranked = sorted(
                [(s, v["wins"]/(v["wins"]+v["losses"])) for s, v in stats.items() if v["wins"]+v["losses"] >= 3],
                key=lambda x: -x[1]
            )
            if ranked:
                msg += f"🏆 أفضل:`{ranked[0][0]}` ({ranked[0][1]*100:.0f}%)\n"
                msg += f"💔 أسوأ:`{ranked[-1][0]}` ({ranked[-1][1]*100:.0f}%)\n"

        send_telegram(msg)
    except Exception as e:
        log.error(f"daily_report: {e}")


# ══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════

def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date, client

    log.info("🚀 بوت التداول الذكي v5.0")
    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
    load_learning()

    # تهيئة peak_balance
    initial = get_futures_balance()
    if learning["peak_balance"] == 0:
        learning["peak_balance"] = initial

    bot_start_balance   = initial
    daily_start_balance = initial
    daily_reset_date    = utcnow().date()

    all_symbols = _get_all_symbols()

    # threads
    threading.Thread(target=protection_monitor, daemon=True, name="ProtMon").start()
    log.info("🛡️ Protection Monitor يعمل")

    send_telegram(
        f"🤖 *بوت التداول v5.0* ✅\n"
        f"رصيد:`{initial:.2f}` USDT\n"
        f"─────────────────\n"
        f"⚙️ Risk:`{learning['current_risk_pct']*100:.1f}%` (ديناميكي {MIN_RISK_PCT*100:.0f}%-{MAX_RISK_PCT*100:.0f}%)\n"
        f"Compounding:`×{learning['compounding_mult']:.2f}`\n"
        f"Pyramid: عند`+{PYRAMID_TRIGGER_PCT*100:.0f}%` (×`{PYRAMID_MAX_ADDS}`)\n"
        f"فلتر السوق: BTC EMA{MARKET_FILTER_EMA}\n"
        f"─────────────────\n"
        f"Win% سابق:`{learning['win_rate']*100:.1f}%` | ATR SL×`{learning['atr_sl']:.2f}`\n"
        f"عملات للفحص:`{len(all_symbols)}`"
    )

    adopt_existing_positions()
    update_market_filter()

    cycle           = 0
    market_check_c  = 0

    while True:
        cycle          += 1
        market_check_c += 1

        try:
            balance = get_futures_balance()
            avail   = get_available_margin()
            log.info(
                f"══ #{cycle} | رصيد:{balance:.2f} متاح:{avail:.2f} "
                f"صفقات:{len(open_trades)} | Risk:{learning['current_risk_pct']*100:.1f}% "
                f"Comp:×{learning['compounding_mult']:.2f} | {'🟢BULL' if _market_is_bull else '🔴BEAR'} ══"
            )

            # فلتر السوق كل 20 دورة (~13 دقيقة)
            if market_check_c >= 20:
                update_market_filter()
                market_check_c = 0

            if not check_protection(balance):
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            # لا ندخل في السوق الهابط
            if not _market_is_bull:
                log.info("🔴 السوق هابط — تخطي البحث")
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            if avail < 3.0 or len(open_trades) >= MAX_OPEN_TRADES:
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            # تجديد رموز كل 100 دورة
            if cycle % 100 == 0:
                _all_symbols_cache.clear()
                all_symbols = _get_all_symbols()

            # ── بحث وتقييم ───────────────────────────────────
            candidates = []
            for sym in all_symbols:
                if sym in open_trades:
                    continue
                r = score_symbol(sym)
                if r:
                    candidates.append(r)

            if candidates:
                candidates.sort(key=lambda x: (-x["score"], x["rsi"]))
                top = [(c["symbol"], c["score"]) for c in candidates[:5]]
                log.info(f"مرشحون: {top}")

                for c in candidates:
                    if len(open_trades) >= MAX_OPEN_TRADES:
                        break
                    if get_available_margin() < 3.0:
                        break
                    if open_long(c):
                        time.sleep(2)
            else:
                log.info("لا فرص مناسبة.")

            # تقرير منتصف الليل
            now = utcnow()
            if now.hour == 0 and now.minute < 2:
                send_daily_report(balance)

        except Exception as e:
            log.error(f"main #{cycle}: {e}")
            send_telegram(f"⚠️ خطأ:\n`{e}`")

        time.sleep(SCAN_INTERVAL_SEC)


# ══════════════════════════════════════════════════════════════
#  FLASK
# ══════════════════════════════════════════════════════════════

@app.route("/")
def home():
    bal  = get_futures_balance()
    risk = learning["current_risk_pct"] * 100
    comp = learning["compounding_mult"]
    wr   = learning["win_rate"] * 100
    bull = "🟢 صاعد" if _market_is_bull else "🔴 هابط"

    lines = [
        f"<b>🤖 Bot v5.0</b> | السوق: {bull}",
        f"رصيد: {bal:.2f} USDT | مفتوحة: {len(open_trades)}",
        f"Win%: {wr:.1f}% ({learning['total_trades']} صفقة)",
        f"Risk: {risk:.1f}% | Compounding: ×{comp:.2f}",
        f"فوز متتالي: {learning['consecutive_wins']} | خسارة متتالية: {learning['consecutive_losses']}",
        "<hr>",
    ]
    for sym, t in open_trades.items():
        cp  = get_current_price(sym)
        pnl = t.pnl_pct(cp)
        flags = []
        if t.at_breakeven:   flags.append("🔒BE")
        if t.trailing_active: flags.append("📈Trail")
        if t.pyramid_adds:   flags.append(f"🔺×{t.pyramid_adds}")
        lines.append(
            f"• <b>{sym}</b> @ {t.entry:.6f} | P&L: {pnl:+.2f}% | "
            f"SL: {t.trail_sl:.6f} | {' '.join(flags)}"
        )
    return "<br>".join(lines)


@app.route("/stats")
def stats_route():
    return json.dumps(learning["symbol_stats"], ensure_ascii=False, indent=2)


@app.route("/trades")
def trades_route():
    result = {}
    for sym, t in open_trades.items():
        cp = get_current_price(sym)
        result[sym] = {
            "entry":       t.entry,
            "current":     cp,
            "pnl_pct":     round(t.pnl_pct(cp), 2),
            "sl":          round(t.trail_sl, 8),
            "tp":          round(t.tp_price, 8),
            "highest":     round(t.highest_price, 8),
            "breakeven":   t.at_breakeven,
            "trailing":    t.trailing_active,
            "pyramid_adds": t.pyramid_adds,
            "hours":       round(t.duration_hours(), 2),
        }
    return json.dumps(result, ensure_ascii=False, indent=2)


@app.route("/learning")
def learning_route():
    return json.dumps({
        "win_rate":           learning["win_rate"],
        "total_trades":       learning["total_trades"],
        "current_risk_pct":   learning["current_risk_pct"],
        "compounding_mult":   learning["compounding_mult"],
        "consecutive_wins":   learning["consecutive_wins"],
        "consecutive_losses": learning["consecutive_losses"],
        "atr_sl":             learning["atr_sl"],
        "atr_tp":             learning["atr_tp"],
        "peak_balance":       learning["peak_balance"],
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
