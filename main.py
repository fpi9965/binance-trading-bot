"""
=============================================================
  SMART TRADING BOT v7.0
  ─────────────────────────────────────────
  ✅ رافعة 20x
  ✅ أقصى 4 صفقات متزامنة
  ✅ تحليل متعدد الـ Timeframes (15m + 1h + 4h)
  ✅ حماية داخلية كاملة (Breakeven + Trailing كل 5 ثوان)
  ✅ SL على بايننس كشبكة أمان (2.5%)
  ✅ Dynamic Risk + Compounding
  ✅ RR لا تقل عن 1:2
=============================================================
"""

import os, time, math, logging, threading, json, statistics, requests
from datetime import datetime, timezone

from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
from flask import Flask

# ─── CREDENTIALS ─────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_API_SECRET")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

# ─── 8 عملات كبيرة فقط ───────────────────────────────────────
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "BNBUSDT", "LINKUSDT", "LTCUSDT",
]

# ─── إعدادات التداول ──────────────────────────────────────────
LEVERAGE          = 20
MAX_OPEN_TRADES   = 2
SCAN_INTERVAL_SEC = 60

# ─── إدارة المخاطر ────────────────────────────────────────────
BASE_RISK_PCT  = 0.02
MIN_RISK_PCT   = 0.01
MAX_RISK_PCT   = 0.04
RISK_STEP_WIN  = 0.003
RISK_STEP_LOSS = 0.005

# ─── الحماية الداخلية ─────────────────────────────────────────
ATR_SL_MULT        = 1.5
ATR_TP_MULT        = 3.0
ATR_SL_MAX         = 2.0
MIN_RR             = 2.0
BREAKEVEN_PCT      = 0.008
TRAILING_START_PCT = 0.015
TRAILING_STEP_PCT  = 0.005
MAX_TRADE_HOURS    = 18

# ─── SL بايننس (شبكة أمان) ────────────────────────────────────
BN_SL_PCT = 0.025

# ─── حماية الرصيد ─────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT = 0.04
TOTAL_LOSS_LIMIT_PCT = 0.12

# ─── شروط الدخول ─────────────────────────────────────────────
MIN_SCORE = 55

LEARNING_FILE = "bot_learning.json"

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

open_trades:    dict = {}
_filters_cache: dict = {}

bot_start_balance:   float = 0.0
daily_start_balance: float = 0.0
daily_reset_date           = None
bot_halted_total           = False
bot_halted_daily           = False
_last_report_date          = None
_market_is_bull            = True

learning = {
    "trade_history":      [],
    "symbol_stats":       {},
    "atr_sl":             ATR_SL_MULT,
    "atr_tp":             ATR_TP_MULT,
    "win_rate":           0.0,
    "total_trades":       0,
    "profitable_trades":  0,
    "current_risk_pct":   BASE_RISK_PCT,
    "consecutive_wins":   0,
    "consecutive_losses": 0,
    "peak_balance":       0.0,
    "compounding_mult":   1.0,
}


# ══════════════════════════════════════════════════════════════
#  TradeState
# ══════════════════════════════════════════════════════════════

class TradeState:
    def __init__(self, symbol, entry, qty, atr, rsi=50, reasons=None):
        self.symbol    = symbol
        self.entry     = entry
        self.qty       = qty
        self.atr       = atr
        self.rsi       = rsi
        self.reasons   = reasons or []
        self.open_time = utcnow()

        sl_mult       = learning["atr_sl"]
        tp_mult       = learning["atr_tp"]
        self.sl_price = entry - atr * sl_mult
        self.tp_price = entry + atr * tp_mult

        risk   = entry - self.sl_price
        reward = self.tp_price - entry
        if risk > 0 and reward / risk < MIN_RR:
            self.tp_price = entry + risk * MIN_RR

        self.highest_price   = entry
        self.at_breakeven    = False
        self.trailing_active = False
        self.trail_sl        = self.sl_price
        self.last_notif_sl   = None

    def update(self, price: float) -> str:
        if price > self.highest_price:
            self.highest_price = price
        pnl = (price - self.entry) / self.entry

        if price >= self.tp_price:
            return "tp_hit"
        if price <= self.trail_sl:
            return "sl_hit"

        if pnl >= TRAILING_START_PCT:
            new_trail = self.highest_price * (1 - TRAILING_STEP_PCT)
            if new_trail > self.trail_sl:
                self.trail_sl        = new_trail
                self.trailing_active = True
                if (self.last_notif_sl is None or
                        abs(new_trail - self.last_notif_sl) / self.entry > 0.004):
                    self.last_notif_sl = new_trail
                    return "trailing_move"
        elif pnl >= BREAKEVEN_PCT and not self.at_breakeven:
            self.at_breakeven  = True
            self.trail_sl      = self.entry * 1.0005
            self.last_notif_sl = self.trail_sl
            return "breakeven"

        return "none"

    def pnl_pct(self, price):
        return (price - self.entry) / self.entry * 100 * LEVERAGE

    def duration_hours(self):
        return (utcnow() - self.open_time).total_seconds() / 3600

    def rr(self):
        risk = self.entry - self.sl_price
        return (self.tp_price - self.entry) / risk if risk > 0 else 0


# ══════════════════════════════════════════════════════════════
#  LEARNING
# ══════════════════════════════════════════════════════════════

def utcnow():
    return datetime.now(timezone.utc)


def load_learning():
    global learning
    try:
        if os.path.exists(LEARNING_FILE):
            with open(LEARNING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                learning.update(data)
            learning["atr_sl"] = min(learning["atr_sl"], ATR_SL_MAX)
            log.info(f"📚 تعلم | صفقات:{learning['total_trades']} Win%:{learning['win_rate']*100:.1f}%")
    except Exception as e:
        log.error(f"load_learning: {e}")


def save_learning():
    try:
        with open(LEARNING_FILE, "w", encoding="utf-8") as f:
            json.dump(learning, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_learning: {e}")


def update_risk(won: bool, balance: float):
    risk = learning["current_risk_pct"]
    if won:
        learning["consecutive_wins"]  += 1
        learning["consecutive_losses"] = 0
        risk = min(risk + RISK_STEP_WIN, MAX_RISK_PCT)
    else:
        learning["consecutive_losses"] += 1
        learning["consecutive_wins"]    = 0
        risk = max(risk - RISK_STEP_LOSS, MIN_RISK_PCT)

    if learning["consecutive_wins"] >= 2 and risk < BASE_RISK_PCT:
        risk = BASE_RISK_PCT
    learning["current_risk_pct"] = risk

    if balance > learning["peak_balance"]:
        learning["peak_balance"] = balance
    if learning["peak_balance"] > 0 and bot_start_balance > 0:
        growth = learning["peak_balance"] / bot_start_balance
        learning["compounding_mult"] = max(1.0, min(growth, 1.5))


def record_trade(trade: TradeState, exit_price: float, balance: float):
    won = exit_price > trade.entry
    pnl = trade.pnl_pct(exit_price)

    learning["trade_history"].append({
        "symbol":  trade.symbol,
        "entry":   trade.entry,
        "exit":    exit_price,
        "pnl_pct": round(pnl, 2),
        "won":     won,
        "hours":   round(trade.duration_hours(), 1),
        "ts":      utcnow().isoformat(),
    })
    if len(learning["trade_history"]) > 300:
        learning["trade_history"] = learning["trade_history"][-300:]

    st = learning["symbol_stats"].setdefault(trade.symbol, {"wins": 0, "losses": 0, "pnl": 0.0})
    if won:
        st["wins"] += 1
    else:
        st["losses"] += 1
    st["pnl"] += pnl

    learning["total_trades"] += 1
    if won:
        learning["profitable_trades"] += 1
    learning["win_rate"] = learning["profitable_trades"] / learning["total_trades"]

    update_risk(won, balance)
    _adapt_atr(won)
    save_learning()
    log.info(f"📊 {trade.symbol} {'✅' if won else '❌'} {pnl:+.2f}% | Win%:{learning['win_rate']*100:.1f}%")


def _adapt_atr(won: bool):
    h = learning["trade_history"]
    if len(h) < 10:
        return
    recent    = h[-20:]
    loss_rate = sum(1 for t in recent if not t["won"]) / len(recent)
    if loss_rate > 0.55:
        learning["atr_sl"] = min(learning["atr_sl"] * 1.05, ATR_SL_MAX)
    elif loss_rate < 0.25:
        learning["atr_sl"] = max(learning["atr_sl"] * 0.97, 1.2)


def get_effective_risk():
    return min(learning["current_risk_pct"] * learning["compounding_mult"], MAX_RISK_PCT)


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
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.error(f"Telegram: {e}")


# ══════════════════════════════════════════════════════════════
#  BINANCE HELPERS
# ══════════════════════════════════════════════════════════════

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
            log.warning(f"pos {symbol}: {e}")
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
    try:
        for s in client.futures_exchange_info()["symbols"]:
            if s["symbol"] == symbol:
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
                    _filters_cache[symbol] = (lot, tick, notional)
                    return _filters_cache[symbol]
    except Exception as e:
        log.error(f"filters {symbol}: {e}")
    return (0.001, 0.01, 5.0)


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


# ══════════════════════════════════════════════════════════════
#  SL على بايننس
# ══════════════════════════════════════════════════════════════

def place_binance_sl(symbol: str, entry: float, qty: float) -> bool:
    if qty <= 0:
        return False
    sl_price = round_price(symbol, entry * (1 - BN_SL_PCT))
    try:
        for o in client.futures_get_open_orders(symbol=symbol):
            if "STOP" in o.get("type", ""):
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                except Exception:
                    pass
        time.sleep(0.3)

        client.futures_create_order(
            symbol      = symbol,
            side        = SIDE_SELL,
            type        = "STOP_MARKET",
            stopPrice   = sl_price,
            quantity    = qty,
            reduceOnly  = True,
            workingType = "MARK_PRICE"
        )
        log.info(f"✅ BN-SL={sl_price} {symbol}")
        return True
    except Exception as e:
        log.error(f"❌ BN-SL {symbol}: {e}")
        return False


def check_and_restore_sl(symbol: str, trade: TradeState):
    try:
        orders = client.futures_get_open_orders(symbol=symbol)
        has_sl = any("STOP" in o.get("type", "") for o in orders)
        if not has_sl:
            amt, _ = get_actual_position(symbol)
            if abs(amt) > 1e-8:
                log.warning(f"⚠️ {symbol}: SL مفقود — إعادة")
                place_binance_sl(symbol, trade.entry, abs(amt))
    except Exception as e:
        log.error(f"check_sl {symbol}: {e}")


# ══════════════════════════════════════════════════════════════
#  TECHNICAL ANALYSIS
# ══════════════════════════════════════════════════════════════

def ema_calc(values, period):
    if len(values) < period:
        return sum(values) / len(values)
    k = 2 / (period + 1)
    v = sum(values[:period]) / period
    for x in values[period:]:
        v = x * k + v * (1 - k)
    return v


def compute_rsi(closes, period=14):
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


def compute_atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    return sum(trs[-period:]) / min(period, len(trs)) if trs else closes[-1] * 0.01


def compute_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return False, 0
    kf, ks = 2/(fast+1), 2/(slow+1)
    ef = es = closes[0]
    line = []
    for c in closes:
        ef = c*kf + ef*(1-kf)
        es = c*ks + es*(1-ks)
        line.append(ef - es)
    sig = ema_calc(line, signal)
    hist     = line[-1] - sig
    hist_prv = line[-2] - ema_calc(line[:-1], signal) if len(line) > signal else 0
    bullish  = line[-1] > sig and hist > hist_prv
    return bullish, hist


def compute_bb_pct(closes, period=20):
    if len(closes) < period:
        return 0.5
    window = closes[-period:]
    mid = sum(window) / period
    std = (sum((x-mid)**2 for x in window) / period) ** 0.5
    upper = mid + 2*std
    lower = mid - 2*std
    width = upper - lower or 1e-9
    return (closes[-1] - lower) / width


def detect_patterns(klines) -> list:
    found = []
    if len(klines) < 4:
        return found

    def c(k):
        o, h, l, cl = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        body = abs(cl - o)
        rng  = h - l or 1e-9
        return o, h, l, cl, body, rng, h-max(o,cl), min(o,cl)-l

    o2,h2,l2,c2,b2,r2,u2,lo2 = c(klines[-3])
    o3,h3,l3,c3,b3,r3,u3,lo3 = c(klines[-2])
    o4,h4,l4,c4,b4,r4,u4,lo4 = c(klines[-1])

    if lo4 > b4*2 and u4 < b4*0.5 and c4 > o4:
        found.append("hammer")
    if c3 < o3 and c4 > o4 and c4 > o3 and o4 < c3:
        found.append("engulfing")
    if c2 < o2 and b3 < b2*0.3 and c4 > o4 and c4 > (o2+c2)/2:
        found.append("morning_star")
    if b4/r4 > 0.80 and c4 > o4 and lo4 < b4*0.3:
        found.append("strong_bull")
    return found


def analyze_symbol(symbol: str) -> dict | None:
    """تحليل متعدد الـ Timeframes — 15m + 1h + 4h"""
    try:
        kl1h = client.futures_klines(symbol=symbol, interval="1h", limit=250)
        cl1h = [float(k[4]) for k in kl1h]
        hi1h = [float(k[2]) for k in kl1h]
        lo1h = [float(k[3]) for k in kl1h]
        vo1h = [float(k[5]) for k in kl1h]

        kl4h = client.futures_klines(symbol=symbol, interval="4h", limit=100)
        cl4h = [float(k[4]) for k in kl4h]

        kl15 = client.futures_klines(symbol=symbol, interval="15m", limit=150)
        cl15 = [float(k[4]) for k in kl15]

        ticker = client.futures_ticker(symbol=symbol)
        price  = float(ticker["lastPrice"])
        if price <= 0:
            return None

        # المؤشرات
        ema20_1h  = ema_calc(cl1h, 20)
        ema50_1h  = ema_calc(cl1h, 50)
        ema200_1h = ema_calc(cl1h, 200)
        ema50_4h  = ema_calc(cl4h, 50)
        ema200_4h = ema_calc(cl4h, 200)

        rsi_1h  = compute_rsi(cl1h)
        rsi_15m = compute_rsi(cl15)

        macd_bull_4h,  hist_4h  = compute_macd(cl4h)
        macd_bull_1h,  hist_1h  = compute_macd(cl1h)
        macd_bull_15m, hist_15m = compute_macd(cl15)

        bb_pct = compute_bb_pct(cl1h)
        atr_1h = compute_atr(hi1h, lo1h, cl1h)

        avg_vol   = sum(vo1h[-20:]) / 20 or 1
        vol_ratio = vo1h[-1] / avg_vol

        patterns = detect_patterns(kl1h[-4:])

        # ── قيود صارمة — رفض فوري ─────────────────────────
        if not macd_bull_4h and not macd_bull_1h:
            return None
        if rsi_1h > 72 or rsi_15m > 75:
            return None
        if price < ema200_1h * 0.97:
            return None

        # ── نظام النقاط ────────────────────────────────────
        score   = 0
        reasons = []

        # الاتجاه (35)
        if price > ema200_1h:
            score += 12; reasons.append("↑EMA200")
        if ema20_1h > ema50_1h:
            score += 8;  reasons.append("EMA20>50")
        if price > ema50_4h:
            score += 8;  reasons.append("↑4h-EMA50")
        if price > ema200_4h:
            score += 7;  reasons.append("↑4h-EMA200")

        # RSI (25)
        if 35 <= rsi_1h <= 55:
            score += 15; reasons.append(f"RSI1h✓{rsi_1h:.0f}")
        elif 25 <= rsi_1h < 35:
            score += 20; reasons.append(f"RSI1h-OS{rsi_1h:.0f}")
        elif 55 < rsi_1h <= 65:
            score += 8;  reasons.append(f"RSI1h~{rsi_1h:.0f}")
        elif rsi_1h > 70:
            score -= 20

        if 35 <= rsi_15m <= 60:
            score += 10; reasons.append(f"RSI15✓{rsi_15m:.0f}")
        elif rsi_15m > 70:
            score -= 10

        # MACD (25)
        if macd_bull_4h:
            score += 15; reasons.append("MACD4h↑")
        if macd_bull_1h:
            score += 8;  reasons.append("MACD1h↑")
        if macd_bull_15m:
            score += 2;  reasons.append("MACD15↑")

        # Bollinger (10)
        if bb_pct < 0.25:
            score += 10; reasons.append("BB-low")
        elif bb_pct > 0.85:
            score -= 8

        # أنماط (15)
        pts = {"morning_star": 15, "engulfing": 10, "hammer": 8, "strong_bull": 6}
        best = max((pts.get(p, 0) for p in patterns), default=0)
        if best:
            score += best; reasons.append(f"🕯️{patterns[0]}")

        # الحجم (10)
        if vol_ratio > 2.0:
            score += 10; reasons.append(f"Vol×{vol_ratio:.1f}")
        elif vol_ratio > 1.5:
            score += 6;  reasons.append(f"Vol×{vol_ratio:.1f}")
        elif vol_ratio < 0.5:
            score -= 5

        # سمعة الرمز
        wr = sym_win_rate(symbol)
        if wr > 0.60:
            score += 5;  reasons.append(f"WR{wr*100:.0f}%")
        elif wr < 0.35:
            score -= 8

        if score < MIN_SCORE:
            log.info(f"{symbol}: score={score} < {MIN_SCORE} — تخطي")
            return None

        return {
            "symbol":   symbol,
            "score":    score,
            "rsi_1h":   round(rsi_1h, 1),
            "rsi_15m":  round(rsi_15m, 1),
            "price":    price,
            "atr":      atr_1h,
            "reasons":  reasons,
            "patterns": patterns,
        }

    except Exception as e:
        if "-1022" not in str(e):
            log.warning(f"analyze {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  MARKET FILTER
# ══════════════════════════════════════════════════════════════

def update_market_filter():
    global _market_is_bull
    try:
        kl    = client.futures_klines(symbol="BTCUSDT", interval="1h", limit=60)
        cls   = [float(k[4]) for k in kl]
        ema50 = ema_calc(cls, 50)
        prev  = _market_is_bull
        _market_is_bull = cls[-1] >= ema50 * 0.97
        if prev != _market_is_bull:
            s = "🟢 صاعد" if _market_is_bull else "🔴 هابط"
            send_telegram(f"📡 *تغيير السوق: {s}*\nBTC:`{cls[-1]:.0f}` | EMA50:`{ema50:.0f}`")
    except Exception as e:
        log.error(f"market_filter: {e}")


# ══════════════════════════════════════════════════════════════
#  CLOSE
# ══════════════════════════════════════════════════════════════

def cancel_sl_orders(symbol: str):
    try:
        for o in client.futures_get_open_orders(symbol=symbol):
            if "STOP" in o.get("type", ""):
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                except Exception:
                    pass
    except Exception as e:
        log.error(f"cancel_sl {symbol}: {e}")


def market_close(symbol: str, qty: float) -> bool:
    qty = abs(qty)
    if qty <= 0:
        return False
    cancel_sl_orders(symbol)
    for attempt in range(3):
        try:
            client.futures_create_order(
                symbol=symbol, side=SIDE_SELL,
                type=ORDER_TYPE_MARKET, quantity=qty, reduceOnly=True,
            )
            log.info(f"✅ إغلاق: {symbol} qty={qty}")
            return True
        except Exception as e:
            log.warning(f"close {symbol} #{attempt+1}: {e}")
            time.sleep(1)
    return False


# ══════════════════════════════════════════════════════════════
#  PROTECTION MONITOR
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

                price = get_current_price(symbol)
                if price <= 0:
                    continue

                if trade.duration_hours() >= MAX_TRADE_HOURS:
                    _execute_close(symbol, trade, price, "timeout")
                    continue

                event = trade.update(price)

                if event == "sl_hit":
                    _execute_close(symbol, trade, price, "sl_internal")
                elif event == "tp_hit":
                    _execute_close(symbol, trade, price, "tp_internal")
                elif event == "breakeven":
                    send_telegram(
                        f"🔒 *Breakeven: {symbol}*\n"
                        f"سعر:`{price:.4f}` SL:`{trade.trail_sl:.4f}`\n"
                        f"P&L:`+{trade.pnl_pct(price):.2f}%`"
                    )
                elif event == "trailing_move":
                    send_telegram(
                        f"📈 *Trailing ↑ {symbol}*\n"
                        f"سعر:`{price:.4f}` | SL:`{trade.trail_sl:.4f}`\n"
                        f"P&L:`+{trade.pnl_pct(price):.2f}%`"
                    )

                check_and_restore_sl(symbol, trade)

        except Exception as e:
            log.error(f"protection_monitor: {e}")

        time.sleep(5)


def _execute_close(symbol, trade, price, reason):
    amt, _ = get_actual_position(symbol)
    if abs(amt) < 1e-8:
        open_trades.pop(symbol, None)
        return
    ok = market_close(symbol, abs(amt))
    if ok:
        open_trades.pop(symbol, None)
        pnl     = trade.pnl_pct(price)
        emoji   = "🟢" if pnl >= 0 else "🔴"
        balance = get_futures_balance()
        record_trade(trade, price, balance)
        labels  = {
            "sl_internal": "وقف الخسارة ⛔",
            "tp_internal": "جني الأرباح 💰",
            "timeout":     f"Timeout {MAX_TRADE_HOURS}h ⏰",
        }
        send_telegram(
            f"{emoji} *مُغلقة: {symbol}*\n"
            f"السبب: {labels.get(reason, reason)}\n"
            f"دخول:`{trade.entry:.4f}` → خروج:`{price:.4f}`\n"
            f"P&L:`{pnl:+.2f}%` (×{LEVERAGE})\n"
            f"المدة:`{trade.duration_hours():.1f}h` | أعلى:`{trade.highest_price:.4f}`\n"
            f"─────────────────\n"
            f"Win%:`{learning['win_rate']*100:.1f}%` Risk:`{learning['current_risk_pct']*100:.1f}%`\n"
            f"💰 رصيد:`{balance:.2f}` USDT"
        )
    else:
        send_telegram(f"🚨 *فشل إغلاق {symbol}* — راجع يدوياً!")


def _handle_closed_externally(symbol, trade):
    open_trades.pop(symbol, None)
    price = get_current_price(symbol)
    if price > 0:
        pnl     = trade.pnl_pct(price)
        balance = get_futures_balance()
        record_trade(trade, price, balance)
        emoji = "🟢" if pnl >= 0 else "🔴"
        send_telegram(
            f"{emoji} *مُغلقة (بايننس): {symbol}*\n"
            f"P&L:`{pnl:+.2f}%` | رصيد:`{balance:.2f}` USDT"
        )


# ══════════════════════════════════════════════════════════════
#  OPEN POSITION
# ══════════════════════════════════════════════════════════════

def open_long(candidate: dict) -> bool:
    symbol = candidate["symbol"]
    price  = candidate["price"]
    atr    = candidate["atr"]

    amt, _ = get_actual_position(symbol)
    if abs(amt) > 1e-8 or symbol in open_trades:
        return False
    if len(open_trades) >= MAX_OPEN_TRADES:
        return False

    try:
        _, _, min_notional = get_filters(symbol)
        balance = get_futures_balance()
        avail   = get_available_margin()

        effective_risk = get_effective_risk()
        sl_distance    = atr * learning["atr_sl"]
        sl_pct         = sl_distance / price if price > 0 else 0.025

        qty_by_risk  = (balance * effective_risk) / (price * sl_pct)
        qty_by_avail = (avail * 0.80 * LEVERAGE) / price
        raw_qty      = min(qty_by_risk, qty_by_avail)
        qty          = round_qty(symbol, raw_qty)

        if qty <= 0 or qty * price < min_notional:
            log.info(f"{symbol}: qty={qty:.4f} أقل من الحد — تخطي")
            return False

        try:
            client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        except Exception as e:
            log.warning(f"leverage {symbol}: {e}")

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
            log.error(f"❌ {symbol}: لا وضعية بعد الأمر!")
            return False

        actual_qty   = abs(actual_amt)
        actual_entry = actual_entry or price

        trade = TradeState(
            symbol  = symbol,
            entry   = actual_entry,
            qty     = actual_qty,
            atr     = atr,
            rsi     = candidate["rsi_1h"],
            reasons = candidate.get("reasons", []),
        )
        open_trades[symbol] = trade

        bn_ok = place_binance_sl(symbol, actual_entry, actual_qty)

        send_telegram(
            f"🚀 *دخول {symbol}*\n"
            f"سعر:`{actual_entry:.4f}` | كمية:`{actual_qty}`\n"
            f"─── الحماية الداخلية ───\n"
            f"SL:`{trade.sl_price:.4f}` | TP:`{trade.tp_price:.4f}`\n"
            f"RR:`{trade.rr():.2f}` | BE`+{BREAKEVEN_PCT*100:.1f}%` | Trail`+{TRAILING_START_PCT*100:.1f}%`\n"
            f"─── بايننس (شبكة أمان) ───\n"
            f"SL:`{round_price(symbol, actual_entry*(1-BN_SL_PCT))}`\n"
            f"{'✅ SL وُضع' if bn_ok else '⚠️ SL فشل!'}\n"
            f"─────────────────\n"
            f"Score:`{candidate['score']}` RSI1h:`{candidate['rsi_1h']:.0f}` RSI15:`{candidate['rsi_15m']:.0f}`\n"
            f"Risk:`{effective_risk*100:.1f}%` Comp:`×{learning['compounding_mult']:.2f}`\n"
            f"📋 {' | '.join(candidate.get('reasons', [])[:5])}"
        )
        log.info(f"✅ {symbol} @ {actual_entry:.4f} qty={actual_qty} Risk={effective_risk*100:.1f}%")
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
                kl  = client.futures_klines(symbol=sym, interval="1h", limit=30)
                atr = compute_atr(
                    [float(k[2]) for k in kl],
                    [float(k[3]) for k in kl],
                    [float(k[4]) for k in kl],
                )
            except Exception:
                atr = entry * 0.015

            trade = TradeState(sym, entry, abs(amt), atr, reasons=["موروثة"])
            open_trades[sym] = trade
            place_binance_sl(sym, entry, abs(amt))
            adopted += 1

    except Exception as e:
        log.error(f"adopt: {e}")

    msg = f"🔄 *تبنّي — {adopted} وضعية*\n"
    for sym, t in open_trades.items():
        msg += f"  • `{sym}` @ `{t.entry:.4f}`\n"
    if not open_trades:
        msg += "لا وضعيات مفتوحة."
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
        cancel_sl_orders(sym)
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
        send_telegram(f"✅ يوم جديد | رصيد:`{balance:.2f}` USDT")

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
        t = (bot_start_balance   - balance) / bot_start_balance   * 100 if bot_start_balance   else 0
        msg  = f"📊 *تقرير يومي — {today}*\n"
        msg += f"رصيد:`{balance:.2f}` USDT\n"
        msg += f"اليوم:`{d:.2f}%` | إجمالي:`{t:.2f}%`\n"
        msg += f"Win%:`{learning['win_rate']*100:.1f}%` ({learning['total_trades']} صفقة)\n"
        msg += f"Risk:`{learning['current_risk_pct']*100:.1f}%` Comp:`×{learning['compounding_mult']:.2f}`\n"
        msg += f"─────────────────\n"
        for sym in SYMBOLS:
            st = learning["symbol_stats"].get(sym)
            if st:
                tot = st["wins"] + st["losses"]
                wr  = st["wins"]/tot*100 if tot else 0
                msg += f"`{sym.replace('USDT','')}`: {st['wins']}✅/{st['losses']}❌ WR:{wr:.0f}%\n"
        send_telegram(msg)
    except Exception as e:
        log.error(f"daily_report: {e}")


# ══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════

def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date, client

    log.info("🚀 بوت v7.0 — 8 عملات | 20x | 2 صفقات")
    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
    load_learning()

    for sym in SYMBOLS:
        get_filters(sym)

    initial = get_futures_balance()
    if learning["peak_balance"] == 0:
        learning["peak_balance"] = initial

    bot_start_balance   = initial
    daily_start_balance = initial
    daily_reset_date    = utcnow().date()

    threading.Thread(target=protection_monitor, daemon=True, name="ProtMon").start()

    send_telegram(
        f"🤖 *بوت v7.0* ✅\n"
        f"رصيد:`{initial:.2f}` USDT\n"
        f"عملات: BTC ETH SOL XRP DOGE BNB LINK LTC\n"
        f"رافعة:`{LEVERAGE}x` | أقصى:`{MAX_OPEN_TRADES}` صفقات\n"
        f"─── الحماية ───\n"
        f"SL داخلي×`{learning['atr_sl']:.2f}` | BE`+{BREAKEVEN_PCT*100:.1f}%` | Trail`+{TRAILING_START_PCT*100:.1f}%`\n"
        f"SL بايننس:`{BN_SL_PCT*100:.1f}%`\n"
        f"─── المخاطرة ───\n"
        f"Risk:`{learning['current_risk_pct']*100:.1f}%` | RR_min:`1:{MIN_RR:.0f}`\n"
        f"يومي:`{DAILY_LOSS_LIMIT_PCT*100:.0f}%` | إجمالي:`{TOTAL_LOSS_LIMIT_PCT*100:.0f}%`"
    )

    adopt_existing_positions()
    update_market_filter()

    cycle = mf_cycle = 0

    while True:
        cycle    += 1
        mf_cycle += 1

        try:
            balance = get_futures_balance()
            avail   = get_available_margin()

            log.info(
                f"══ #{cycle} | رصيد:{balance:.2f} متاح:{avail:.2f} "
                f"صفقات:{len(open_trades)}/{MAX_OPEN_TRADES} | "
                f"Risk:{learning['current_risk_pct']*100:.1f}% "
                f"Comp:×{learning['compounding_mult']:.2f} | "
                f"{'🟢BULL' if _market_is_bull else '🔴BEAR'} ══"
            )

            if mf_cycle >= 15:
                update_market_filter()
                mf_cycle = 0

            if not check_protection(balance):
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            if not _market_is_bull:
                log.info("🔴 سوق هابط — انتظار")
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            if avail < 2.0 or len(open_trades) >= MAX_OPEN_TRADES:
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            # ── تحليل الـ 8 عملات ────────────────────────────
            candidates = []
            for sym in SYMBOLS:
                if sym in open_trades:
                    continue
                amt, _ = get_actual_position(sym)
                if abs(amt) > 1e-8:
                    continue
                result = analyze_symbol(sym)
                if result:
                    candidates.append(result)
                    log.info(
                        f"✅ {sym}: {result['score']}pts "
                        f"RSI1h={result['rsi_1h']:.0f} "
                        f"RSI15={result['rsi_15m']:.0f}"
                    )

            if candidates:
                candidates.sort(key=lambda x: (-x["score"], x["rsi_1h"]))
                log.info(f"مرشحون: {[(c['symbol'], c['score']) for c in candidates]}")
                for c in candidates:
                    if len(open_trades) >= MAX_OPEN_TRADES:
                        break
                    if get_available_margin() < 2.0:
                        break
                    if open_long(c):
                        time.sleep(3)
            else:
                log.info("لا فرص الآن — انتظار شروط أفضل.")

            now = utcnow()
            if now.hour == 0 and now.minute < 1:
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
    bull = "🟢 صاعد" if _market_is_bull else "🔴 هابط"
    lines = [
        f"<b>🤖 Bot v7.0</b> | {bull}",
        f"رصيد: <b>{bal:.2f} USDT</b> | مفتوحة: {len(open_trades)}/{MAX_OPEN_TRADES}",
        f"Win%: {learning['win_rate']*100:.1f}% ({learning['total_trades']} صفقة)",
        f"Risk: {learning['current_risk_pct']*100:.1f}% | Comp: ×{learning['compounding_mult']:.2f}",
        "<hr>",
    ]
    for sym, t in open_trades.items():
        cp    = get_current_price(sym)
        pnl   = t.pnl_pct(cp)
        color = "green" if pnl >= 0 else "red"
        flags = []
        if t.at_breakeven:    flags.append("🔒BE")
        if t.trailing_active: flags.append("📈Trail")
        lines.append(
            f"• <b>{sym}</b> @ {t.entry:.4f} | "
            f"<span style='color:{color}'>{pnl:+.2f}%</span> | "
            f"SL:{t.trail_sl:.4f} TP:{t.tp_price:.4f} | "
            f"RR:{t.rr():.2f} | {' '.join(flags)}"
        )
    return "<br>".join(lines)


@app.route("/trades")
def trades_route():
    result = {}
    for sym, t in open_trades.items():
        cp = get_current_price(sym)
        result[sym] = {
            "entry":    t.entry,
            "current":  cp,
            "pnl_pct":  round(t.pnl_pct(cp), 2),
            "sl":       round(t.trail_sl, 6),
            "tp":       round(t.tp_price, 6),
            "rr":       round(t.rr(), 2),
            "breakeven": t.at_breakeven,
            "trailing":  t.trailing_active,
            "hours":    round(t.duration_hours(), 2),
        }
    return json.dumps(result, ensure_ascii=False, indent=2)


@app.route("/stats")
def stats_route():
    result = {}
    for sym in SYMBOLS:
        st    = learning["symbol_stats"].get(sym, {"wins": 0, "losses": 0, "pnl": 0.0})
        total = st["wins"] + st["losses"]
        result[sym] = {
            "wins":    st["wins"],
            "losses":  st["losses"],
            "win_rate": round(st["wins"]/total*100, 1) if total else 0,
            "pnl":     round(st["pnl"], 2),
        }
    return json.dumps(result, ensure_ascii=False, indent=2)


@app.route("/learning")
def learning_route():
    return json.dumps({
        k: learning[k] for k in [
            "win_rate", "total_trades", "current_risk_pct",
            "compounding_mult", "consecutive_wins", "consecutive_losses",
            "atr_sl", "atr_tp", "peak_balance",
        ]
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
