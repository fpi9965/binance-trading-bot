"""
=============================================================
  SMART TRADING BOT v4.0 — بوت التداول الذكي
  ─────────────────────────────────────────
  ✅ الحماية داخل البوت بالكامل (بدون STOP_MARKET/TP_MARKET)
  ✅ Breakeven تلقائي عند +1.5% ربح
  ✅ Trailing داخلي يتبع السعر ويحمي الأرباح
  ✅ فحص ذكي لا يرسل تنبيهات مكررة
  ✅ يبحث في كل العملات المتاحة
  ✅ تحليل شموع يابانية متعدد الإطار
  ✅ نظام تعلم يطور نفسه تلقائياً
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

# ─── إعدادات التداول ─────────────────────────────────────────
LEVERAGE             = 10
RISK_PER_TRADE_PCT   = 0.02      # 2% مخاطرة لكل صفقة
MAX_OPEN_TRADES      = 5
SCAN_INTERVAL_SEC    = 45

# ─── إعدادات الحماية الداخلية ────────────────────────────────
ATR_SL_MULTIPLIER      = 2.0     # SL = entry - 2 × ATR
ATR_TP_MULTIPLIER      = 3.0     # TP = entry + 3 × ATR
MIN_RR_RATIO           = 1.5
BREAKEVEN_TRIGGER_PCT  = 0.015   # 1.5% ربح → نقل SL للدخول
TRAILING_START_PCT     = 0.025   # 2.5% ربح → تفعيل trailing
TRAILING_STEP_PCT      = 0.008   # trailing يتحرك كل 0.8%
MAX_TRADE_HOURS        = 48      # إغلاق إجباري بعد 48 ساعة

# ─── حماية الرصيد ────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT  = 0.05
TOTAL_LOSS_LIMIT_PCT  = 0.15

# ─── فلترة العملات ───────────────────────────────────────────
MIN_24H_QUOTE_VOLUME  = 1_000_000
MIN_SCORE             = 40

# ─── تعلم ────────────────────────────────────────────────────
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
open_trades:        dict  = {}   # symbol → TradeState
_filters_cache:     dict  = {}
_all_symbols_cache: list  = []

bot_start_balance:   float = 0.0
daily_start_balance: float = 0.0
daily_reset_date           = None
bot_halted_total           = False
bot_halted_daily           = False
_last_report_date          = None

# ─── نظام التعلم ─────────────────────────────────────────────
learning = {
    "trade_history":     [],
    "symbol_stats":      {},
    "atr_sl":            ATR_SL_MULTIPLIER,
    "atr_tp":            ATR_TP_MULTIPLIER,
    "win_rate":          0.0,
    "total_trades":      0,
    "profitable_trades": 0,
}


# ══════════════════════════════════════════════════════════════
#  TradeState — حالة كل صفقة مفتوحة
# ══════════════════════════════════════════════════════════════

class TradeState:
    """يتتبع كل بيانات الصفقة ويدير الحماية داخلياً"""

    def __init__(self, symbol, entry, qty, atr, score=0, rsi=50, reasons=None):
        self.symbol     = symbol
        self.entry      = entry
        self.qty        = qty
        self.atr        = atr
        self.score      = score
        self.rsi        = rsi
        self.reasons    = reasons or []
        self.open_time  = utcnow()

        # حدود الحماية
        sl_mult         = learning["atr_sl"]
        tp_mult         = learning["atr_tp"]
        self.sl_price   = entry - atr * sl_mult
        self.tp_price   = entry + atr * tp_mult

        # تأكد من RR كافي
        risk   = entry - self.sl_price
        reward = self.tp_price - entry
        if risk > 0 and reward / risk < MIN_RR_RATIO:
            self.tp_price = entry + risk * MIN_RR_RATIO

        # حالة الحماية
        self.highest_price  = entry    # أعلى سعر وصله
        self.at_breakeven   = False    # هل SL انتقل للدخول؟
        self.trailing_active= False    # هل trailing يعمل؟
        self.trail_sl       = self.sl_price  # SL الحالي (يتحرك)
        self.last_notif_sl  = None     # آخر قيمة SL أُبلّغ عنها
        self.alert_count    = 0        # عدد التنبيهات

    def update(self, current_price: float) -> str:
        """
        يُحدَّث مع كل سعر جديد.
        يُعيد: "none" | "breakeven" | "trailing_move" | "sl_hit" | "tp_hit"
        """
        if current_price > self.highest_price:
            self.highest_price = current_price

        pnl_pct = (current_price - self.entry) / self.entry

        # ── وصل TP ──────────────────────────────────────
        if current_price >= self.tp_price:
            return "tp_hit"

        # ── وصل SL ──────────────────────────────────────
        if current_price <= self.trail_sl:
            return "sl_hit"

        # ── تفعيل Trailing ───────────────────────────────
        if pnl_pct >= TRAILING_START_PCT:
            new_trail = self.highest_price * (1 - TRAILING_STEP_PCT)
            if new_trail > self.trail_sl:
                self.trail_sl        = new_trail
                self.trailing_active = True
                # إشعار فقط إذا تحرك SL بشكل ملحوظ
                if (self.last_notif_sl is None or
                        abs(new_trail - self.last_notif_sl) / self.entry > 0.003):
                    self.last_notif_sl = new_trail
                    return "trailing_move"

        # ── Breakeven ────────────────────────────────────
        elif pnl_pct >= BREAKEVEN_TRIGGER_PCT and not self.at_breakeven:
            self.at_breakeven = True
            self.trail_sl     = self.entry * 1.001   # SL فوق الدخول بقليل
            self.last_notif_sl = self.trail_sl
            return "breakeven"

        return "none"

    def rr_now(self, current_price: float) -> float:
        risk = self.entry - self.sl_price
        if risk <= 0:
            return 0
        return (current_price - self.entry) / risk

    def pnl_pct(self, current_price: float) -> float:
        return (current_price - self.entry) / self.entry * 100 * LEVERAGE

    def duration_hours(self) -> float:
        return (utcnow() - self.open_time).total_seconds() / 3600


# ══════════════════════════════════════════════════════════════
#  LEARNING SYSTEM
# ══════════════════════════════════════════════════════════════

def load_learning():
    global learning
    try:
        if os.path.exists(LEARNING_FILE):
            with open(LEARNING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                learning.update(data)
            log.info(f"📚 بيانات التعلم | صفقات: {learning['total_trades']} | فوز: {learning['win_rate']*100:.1f}%")
    except Exception as e:
        log.error(f"load_learning: {e}")


def save_learning():
    try:
        with open(LEARNING_FILE, "w", encoding="utf-8") as f:
            json.dump(learning, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_learning: {e}")


def record_trade(trade: TradeState, exit_price: float):
    won      = exit_price >= trade.entry
    pnl      = (exit_price - trade.entry) / trade.entry * 100 * LEVERAGE
    dur_min  = trade.duration_hours() * 60

    learning["trade_history"].append({
        "symbol":   trade.symbol,
        "entry":    trade.entry,
        "exit":     exit_price,
        "pnl_pct":  round(pnl, 3),
        "duration": round(dur_min, 1),
        "rsi":      trade.rsi,
        "score":    trade.score,
        "atr":      round(trade.atr, 8),
        "won":      won,
        "ts":       utcnow().isoformat(),
    })
    if len(learning["trade_history"]) > 500:
        learning["trade_history"] = learning["trade_history"][-500:]

    sym_st = learning["symbol_stats"].setdefault(trade.symbol, {"wins": 0, "losses": 0, "pnl": 0.0})
    if won:
        sym_st["wins"] += 1
    else:
        sym_st["losses"] += 1
    sym_st["pnl"] += pnl

    learning["total_trades"]      += 1
    if won:
        learning["profitable_trades"] += 1
    learning["win_rate"] = learning["profitable_trades"] / learning["total_trades"]

    _adapt_parameters()
    save_learning()
    log.info(f"📊 {trade.symbol} {'✅' if won else '❌'} {pnl:+.2f}% | Win%: {learning['win_rate']*100:.1f}%")


def _adapt_parameters():
    """يضبط ATR multipliers بناءً على آخر 30 صفقة"""
    history = learning["trade_history"]
    if len(history) < 15:
        return

    recent    = history[-30:]
    loss_rate = sum(1 for t in recent if not t["won"]) / len(recent)

    if loss_rate > 0.55:
        # كثير من الخسائر → وسّع SL
        learning["atr_sl"] = min(learning["atr_sl"] * 1.08, 4.0)
        log.info(f"🎓 ATR SL ↑ {learning['atr_sl']:.2f} (خسائر {loss_rate*100:.0f}%)")
    elif loss_rate < 0.30:
        # أداء جيد → شدّد SL لتقليل الخسارة
        learning["atr_sl"] = max(learning["atr_sl"] * 0.96, 1.2)
        log.info(f"🎓 ATR SL ↓ {learning['atr_sl']:.2f} (فوز {(1-loss_rate)*100:.0f}%)")

    wins = [t for t in recent if t["won"]]
    if wins:
        avg_win = statistics.mean(t["pnl_pct"] for t in wins)
        if avg_win > 8:
            learning["atr_tp"] = min(learning["atr_tp"] * 1.05, 7.0)
        elif avg_win < 3:
            learning["atr_tp"] = max(learning["atr_tp"] * 0.97, 2.0)


def is_blacklisted(symbol: str) -> bool:
    st = learning["symbol_stats"].get(symbol)
    if not st:
        return False
    total = st["wins"] + st["losses"]
    if total < 6:
        return False
    return (st["wins"] / total) < 0.28   # أقل من 28% فوز → تجنبه


def sym_win_rate(symbol: str) -> float:
    st = learning["symbol_stats"].get(symbol)
    if not st:
        return 0.5
    total = st["wins"] + st["losses"]
    return st["wins"] / total if total else 0.5


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
            timeout=10
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
            return float(p["positionAmt"]), float(p["entryPrice"])
    except Exception as e:
        if "-1022" not in str(e):
            log.warning(f"get_actual_position {symbol}: {e}")
    return 0.0, 0.0


def get_current_price(symbol: str) -> float:
    try:
        return float(client.futures_symbol_ticker(symbol=symbol)["price"])
    except Exception as e:
        log.error(f"get_current_price {symbol}: {e}")
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
        log.info(f"📋 عملات متاحة: {len(symbols)}")
        return symbols
    except Exception as e:
        log.error(f"_get_all_symbols: {e}")
        return []


# ══════════════════════════════════════════════════════════════
#  MARKET ORDER — الطريقة الوحيدة التي تعمل دائماً
# ══════════════════════════════════════════════════════════════

def market_close(symbol: str, qty: float) -> bool:
    """إغلاق وضعية LONG بأمر سوق"""
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
            log.info(f"✅ إغلاق سوق: {symbol} qty={qty}")
            return True
        except Exception as e:
            log.warning(f"market_close {symbol} (محاولة {attempt+1}): {e}")
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


def compute_atr(highs: list, lows: list, closes: list, period=14) -> float:
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


def compute_macd_bull(closes: list, fast=12, slow=26, signal=9) -> bool:
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
    hist    = line[-1] - sig_val
    return line[-1] > sig_val and hist > 0


def compute_bollinger_pct(closes: list, period=20) -> float:
    if len(closes) < period:
        return 0.5
    window = closes[-period:]
    mid    = sum(window) / period
    std    = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    upper  = mid + 2 * std
    lower  = mid - 2 * std
    width  = upper - lower or 1
    return (closes[-1] - lower) / width


def detect_patterns(klines: list) -> list:
    found = []
    if len(klines) < 3:
        return found

    def c(k):
        o, h, l, cl = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        body  = abs(cl - o)
        rng   = h - l or 1e-9
        return o, h, l, cl, body, rng, h - max(o, cl), min(o, cl) - l

    o1,h1,l1,c1,b1,r1,u1,lo1 = c(klines[-3])
    o2,h2,l2,c2,b2,r2,u2,lo2 = c(klines[-2])
    o3,h3,l3,c3,b3,r3,u3,lo3 = c(klines[-1])

    if lo3 > b3 * 2 and u3 < b3 * 0.3 and c3 > o3:
        found.append("hammer")
    if c2 < o2 and c3 > o3 and c3 > o2 and o3 < c2:
        found.append("bullish_engulfing")
    if c1 < o1 and b2 < b1 * 0.3 and c3 > o3 and c3 > (o1+c1)/2:
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

        # ── شموع 15m ────────────────────────────────────
        kl_15 = client.futures_klines(symbol=symbol, interval="15m", limit=210)
        if len(kl_15) < 80:
            return None
        cl15 = [float(k[4]) for k in kl_15]
        hi15 = [float(k[2]) for k in kl_15]
        lo15 = [float(k[3]) for k in kl_15]
        vo15 = [float(k[5]) for k in kl_15]

        # ── شموع 1h ──────────────────────────────────────
        kl_1h = client.futures_klines(symbol=symbol, interval="1h", limit=210)
        cl1h  = [float(k[4]) for k in kl_1h]

        # ── شموع 4h ──────────────────────────────────────
        kl_4h = client.futures_klines(symbol=symbol, interval="4h", limit=100)
        cl4h  = [float(k[4]) for k in kl_4h]

        # ── Ticker ───────────────────────────────────────
        ticker = client.futures_ticker(symbol=symbol)
        vol24  = float(ticker.get("quoteVolume", 0))
        price  = float(ticker["lastPrice"])

        if vol24 < MIN_24H_QUOTE_VOLUME or price <= 0:
            return None

        # ── Indicators ───────────────────────────────────
        rsi_15    = compute_rsi(cl15)
        macd_15   = compute_macd_bull(cl15)
        bb_pct    = compute_bollinger_pct(cl15)
        atr_15    = compute_atr(hi15, lo15, cl15)
        patterns  = detect_patterns(kl_15)

        ema200_1h = ema(cl1h, 200)
        ema50_1h  = ema(cl1h, 50)
        ema20_1h  = ema(cl1h, 20)
        macd_4h   = compute_macd_bull(cl4h)
        ema50_4h  = ema(cl4h, 50)

        cur = cl15[-1]

        # ── حجم ─────────────────────────────────────────
        avg_vol = sum(vo15[-20:]) / 20
        vol_ratio = vo15[-1] / avg_vol if avg_vol > 0 else 1

        # ── Score ────────────────────────────────────────
        score   = 0
        reasons = []

        # اتجاه طويل (1h/4h) — 35 نقطة
        if cur > ema200_1h:
            score += 15; reasons.append("↑EMA200")
        if ema20_1h > ema50_1h:
            score += 10; reasons.append("EMA20>50(1h)")
        if cur > ema50_4h:
            score += 5;  reasons.append("↑EMA50(4h)")
        if macd_4h:
            score += 5;  reasons.append("MACD↑(4h)")

        # RSI — 20 نقطة
        if 40 <= rsi_15 <= 58:
            score += 20; reasons.append(f"RSI✓{rsi_15:.0f}")
        elif 30 <= rsi_15 < 40:
            score += 15; reasons.append(f"RSI-OS{rsi_15:.0f}")
        elif 58 < rsi_15 <= 65:
            score += 8;  reasons.append(f"RSI~{rsi_15:.0f}")
        elif rsi_15 > 70:
            score -= 15  # تشبع شراء → نقاط سالبة

        # MACD 15m — 15 نقطة
        if macd_15:
            score += 15; reasons.append("MACD↑(15m)")

        # Bollinger — 10 نقطة
        if bb_pct < 0.30:
            score += 10; reasons.append("BB-low")
        elif bb_pct > 0.85:
            score -= 5

        # أنماط شمعية — 15 نقطة max
        pattern_scores = {
            "morning_star":       15,
            "bullish_engulfing":  15,
            "three_soldiers":     12,
            "hammer":             10,
            "strong_bull":        8,
        }
        best_pattern_score = max((pattern_scores.get(p, 0) for p in patterns), default=0)
        if best_pattern_score:
            score += best_pattern_score
            reasons.append(f"🕯️{','.join(patterns[:2])}")

        # حجم — 10 نقطة
        if vol_ratio > 1.8:
            score += 10; reasons.append(f"Vol×{vol_ratio:.1f}")
        elif vol_ratio > 1.3:
            score += 5;  reasons.append(f"Vol×{vol_ratio:.1f}")

        # سمعة العملة
        wr = sym_win_rate(symbol)
        if wr > 0.62:
            score += 5; reasons.append(f"WR{wr*100:.0f}%")

        if score < MIN_SCORE:
            return None

        return {
            "symbol":   symbol,
            "score":    score,
            "rsi":      round(rsi_15, 1),
            "price":    price,
            "atr":      atr_15,
            "reasons":  reasons,
            "patterns": patterns,
        }

    except Exception as e:
        if "-1022" not in str(e) and "-1000" not in str(e):
            log.warning(f"score {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  PROTECTION MONITOR — القلب الجديد للبوت
# ══════════════════════════════════════════════════════════════

def protection_monitor():
    """
    يعمل في thread منفصل كل 5 ثواني.
    يُحدّث TradeState لكل صفقة ويتخذ قرار الإغلاق بناءً على السعر الحالي.
    لا يُرسل أوامر STOP_MARKET أو TAKE_PROFIT_MARKET إلى Binance.
    """
    while True:
        try:
            for symbol in list(open_trades.keys()):
                trade = open_trades.get(symbol)
                if trade is None:
                    continue

                # ── تحقق من الوضعية الفعلية ─────────────
                amt, _ = get_actual_position(symbol)
                if abs(amt) < 1e-8:
                    # أُغلقت خارجياً
                    _handle_closed(symbol, trade, reason="external")
                    continue

                current = get_current_price(symbol)
                if current <= 0:
                    continue

                # ── تجاوز الحد الزمني ────────────────────
                if trade.duration_hours() >= MAX_TRADE_HOURS:
                    log.warning(f"⏰ {symbol}: {MAX_TRADE_HOURS}h انتهت — إغلاق")
                    _close_trade(symbol, trade, current, reason="timeout")
                    continue

                # ── تحديث الحماية ────────────────────────
                event = trade.update(current)

                if event == "sl_hit":
                    log.info(f"🔴 {symbol}: SL @ {current:.6f} — إغلاق")
                    _close_trade(symbol, trade, current, reason="sl")

                elif event == "tp_hit":
                    log.info(f"🟢 {symbol}: TP @ {current:.6f} — إغلاق")
                    _close_trade(symbol, trade, current, reason="tp")

                elif event == "breakeven":
                    send_telegram(
                        f"🔒 *Breakeven: {symbol}*\n"
                        f"السعر: `{current:.6f}` | SL نُقل لـ: `{trade.trail_sl:.6f}`\n"
                        f"P&L: `+{trade.pnl_pct(current):.2f}%`"
                    )
                    log.info(f"🔒 {symbol}: Breakeven SL={trade.trail_sl:.6f}")

                elif event == "trailing_move":
                    send_telegram(
                        f"📈 *Trailing: {symbol}*\n"
                        f"سعر: `{current:.6f}` | SL الجديد: `{trade.trail_sl:.6f}`\n"
                        f"P&L: `+{trade.pnl_pct(current):.2f}%`"
                    )
                    log.info(f"📈 {symbol}: Trailing SL={trade.trail_sl:.6f}")

        except Exception as e:
            log.error(f"protection_monitor: {e}")

        time.sleep(5)


def _close_trade(symbol: str, trade: TradeState, current_price: float, reason: str):
    """يُغلق الصفقة بأمر سوق ويُسجّل في التعلم"""
    amt, _ = get_actual_position(symbol)
    if abs(amt) < 1e-8:
        open_trades.pop(symbol, None)
        return

    ok = market_close(symbol, abs(amt))
    if ok:
        open_trades.pop(symbol, None)
        pnl    = trade.pnl_pct(current_price)
        emoji  = "🟢" if pnl >= 0 else "🔴"
        dur    = f"{trade.duration_hours():.1f}h"

        reason_txt = {
            "sl":      "وقف الخسارة ⛔",
            "tp":      "جني الأرباح 💰",
            "timeout": f"انتهاء {MAX_TRADE_HOURS}h ⏰",
            "external":"إغلاق خارجي 🤚",
        }.get(reason, reason)

        record_trade(trade, current_price)

        send_telegram(
            f"{emoji} *مُغلقة: {symbol}*\n"
            f"السبب: {reason_txt}\n"
            f"دخول: `{trade.entry:.6f}` → خروج: `{current_price:.6f}`\n"
            f"P&L: `{pnl:+.2f}%` (رافعة {LEVERAGE}x)\n"
            f"المدة: `{dur}` | أعلى سعر: `{trade.highest_price:.6f}`\n"
            f"📊 Win%: `{learning['win_rate']*100:.1f}%`"
        )
    else:
        send_telegram(f"🚨 *فشل إغلاق {symbol}* — راجع يدوياً!")


def _handle_closed(symbol: str, trade: TradeState, reason: str):
    """يتعامل مع صفقة أُغلقت خارجياً"""
    open_trades.pop(symbol, None)
    current = get_current_price(symbol)
    if current > 0:
        pnl   = trade.pnl_pct(current)
        emoji = "🟢" if pnl >= 0 else "🔴"
        record_trade(trade, current)
        send_telegram(
            f"{emoji} *خارجية: {symbol}*\n"
            f"P&L تقريبي: `{pnl:+.2f}%`\n"
            f"📊 Win%: `{learning['win_rate']*100:.1f}%`"
        )


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

        # حجم الصفقة بناءً على المخاطرة
        risk_usdt   = balance * RISK_PER_TRADE_PCT
        sl_distance = atr * learning["atr_sl"]
        sl_pct      = sl_distance / price if price > 0 else 0.02

        qty_by_risk = risk_usdt / (price * sl_pct)
        qty_by_avail = (avail * 0.9 * LEVERAGE) / price
        raw_qty = min(qty_by_risk, qty_by_avail)
        qty     = round_qty(symbol, raw_qty)

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
                log.warning(f"entry {symbol} (محاولة {attempt+1}): {e}")
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

        # إنشاء TradeState
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

        send_telegram(
            f"🚀 *دخول {symbol}*\n"
            f"سعر: `{actual_entry:.6f}` | كمية: `{actual_qty}`\n"
            f"SL: `{trade.sl_price:.6f}` | TP: `{trade.tp_price:.6f}`\n"
            f"RR: `{(trade.tp_price-actual_entry)/(actual_entry-trade.sl_price):.2f}` | ATR: `{atr:.6f}`\n"
            f"Breakeven عند: `+{BREAKEVEN_TRIGGER_PCT*100:.1f}%`\n"
            f"Trailing عند: `+{TRAILING_START_PCT*100:.1f}%`\n"
            f"📋 {' | '.join(candidate.get('reasons',[])[:4])}\n"
            f"نقاط: `{candidate['score']}` | RSI: `{candidate['rsi']}`"
        )
        log.info(f"✅ {symbol} @ {actual_entry:.6f} SL={trade.sl_price:.6f} TP={trade.tp_price:.6f}")
        return True

    except Exception as e:
        log.error(f"open_long {symbol}: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  ADOPT EXISTING POSITIONS
# ══════════════════════════════════════════════════════════════

def adopt_existing_positions():
    log.info("🔍 جلب الوضعيات...")
    adopted = 0
    try:
        for p in get_all_positions():
            sym   = p["symbol"]
            amt   = float(p["positionAmt"])
            entry = float(p["entryPrice"])

            if abs(amt) < 1e-8 or entry == 0:
                continue
            if sym in open_trades:
                continue

            if amt < 0:
                send_telegram(f"⚠️ SHORT في `{sym}` — راجع يدوياً")
                continue

            try:
                kl = client.futures_klines(symbol=sym, interval="15m", limit=30)
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

            log.info(f"✅ تبنّي: {sym} @ {entry} qty={abs(amt)}")

    except Exception as e:
        log.error(f"adopt_existing_positions: {e}")

    msg = f"🔄 *تبنّي الوضعيات* — {adopted} وضعية\n"
    for sym, t in open_trades.items():
        msg += f"  • `{sym}`: دخول `{t.entry:.6f}` | SL: `{t.sl_price:.6f}`\n"
    if not open_trades:
        msg += "لا توجد وضعيات."
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
        send_telegram(f"✅ يوم جديد | رصيد: `{balance:.2f}` USDT")

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
        d = (daily_start_balance - balance) / daily_start_balance * 100 if daily_start_balance else 0
        t = (bot_start_balance  - balance) / bot_start_balance  * 100 if bot_start_balance  else 0
        msg  = f"📊 *تقرير يومي — {today}*\n"
        msg += f"رصيد: `{balance:.2f}` USDT\n"
        msg += f"اليوم: `{d:.2f}%` | إجمالي: `{t:.2f}%`\n"
        msg += f"Win%: `{learning['win_rate']*100:.1f}%` ({learning['total_trades']} صفقة)\n"
        msg += f"ATR SL×`{learning['atr_sl']:.2f}` TP×`{learning['atr_tp']:.2f}`\n"
        msg += f"مفتوحة: `{len(open_trades)}`\n"
        for sym, t in open_trades.items():
            cp = get_current_price(sym)
            msg += f"  • `{sym}` P&L:`{t.pnl_pct(cp):+.2f}%` Trail:`{'✅' if t.trailing_active else '⏳'}`\n"
        send_telegram(msg)
    except Exception as e:
        log.error(f"daily_report: {e}")


# ══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════

def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date, client

    log.info("🚀 تهيئة البوت v4.0...")
    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
    load_learning()

    # تشغيل Protection Monitor في thread منفصل
    threading.Thread(target=protection_monitor, daemon=True, name="ProtectionMonitor").start()
    log.info("🛡️ Protection Monitor يعمل")

    initial = get_futures_balance()
    bot_start_balance   = initial
    daily_start_balance = initial
    daily_reset_date    = utcnow().date()

    all_symbols = _get_all_symbols()

    send_telegram(
        f"🤖 *بوت التداول الذكي v4.0* ✅\n"
        f"رصيد: `{initial:.2f}` USDT\n"
        f"رافعة: `{LEVERAGE}x` | مخاطرة: `{RISK_PER_TRADE_PCT*100:.0f}%`/صفقة\n"
        f"SL داخلي × ATR`{learning['atr_sl']:.1f}` | TP × ATR`{learning['atr_tp']:.1f}`\n"
        f"Breakeven: `+{BREAKEVEN_TRIGGER_PCT*100:.1f}%` | Trailing: `+{TRAILING_START_PCT*100:.1f}%`\n"
        f"عملات للفحص: `{len(all_symbols)}`\n"
        f"Win% سابق: `{learning['win_rate']*100:.1f}%`"
    )

    adopt_existing_positions()

    cycle = 0
    while True:
        cycle += 1
        try:
            balance = get_futures_balance()
            avail   = get_available_margin()
            log.info(f"══ #{cycle} | رصيد:{balance:.2f} متاح:{avail:.2f} صفقات:{len(open_trades)} ══")

            if not check_protection(balance):
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            if avail < 2.0 or len(open_trades) >= MAX_OPEN_TRADES:
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            # ── تجديد قائمة الرموز كل 100 دورة ─────────
            if cycle % 100 == 0:
                _all_symbols_cache.clear()
                all_symbols = _get_all_symbols()

            # ── فحص وتقييم العملات ───────────────────────
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
                    avail = get_available_margin()
                    if avail < 2.0:
                        break
                    if open_long(c):
                        time.sleep(2)
            else:
                log.info("لا فرص مناسبة.")

            now = utcnow()
            if now.hour == 0 and now.minute < 2:
                send_daily_report(balance)

        except Exception as e:
            log.error(f"main_loop #{cycle}: {e}")
            send_telegram(f"⚠️ خطأ:\n`{e}`")

        time.sleep(SCAN_INTERVAL_SEC)


# ══════════════════════════════════════════════════════════════
#  FLASK
# ══════════════════════════════════════════════════════════════

@app.route("/")
def home():
    lines = [
        "<b>🤖 Bot v4.0</b> | الحماية: داخلية 100%",
        f"Win%: {learning['win_rate']*100:.1f}% ({learning['total_trades']} صفقة)",
        f"ATR SL×{learning['atr_sl']:.2f} TP×{learning['atr_tp']:.2f}",
        "<hr>",
    ]
    for sym, t in open_trades.items():
        cp  = get_current_price(sym)
        pnl = t.pnl_pct(cp)
        lines.append(
            f"• <b>{sym}</b> @ {t.entry:.6f} | "
            f"P&L: {pnl:+.2f}% | "
            f"SL: {t.trail_sl:.6f} | "
            f"{'🔒BE' if t.at_breakeven else ''} {'📈Trail' if t.trailing_active else ''}"
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
            "sl":          round(t.trail_sl, 6),
            "tp":          round(t.tp_price, 6),
            "highest":     round(t.highest_price, 6),
            "breakeven":   t.at_breakeven,
            "trailing":    t.trailing_active,
            "hours":       round(t.duration_hours(), 2),
        }
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
