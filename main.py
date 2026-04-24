"""
=============================================================
  SMART TRADING BOT v8.0  — SCALPING EDITION
  ─────────────────────────────────────────
  ✅ إطار زمني: 5m أساسي + 1m تأكيد
  ✅ EMA 9/21 + RSI + Volume + Support/Resistance
  ✅ Long & Short
  ✅ TP: 0.8%–1.5% | SL: 0.5%–0.8%
  ✅ رافعة 5x–10x (حسب قوة الصفقة)
  ✅ إصلاح BN-SL: STOP_MARKET مع fallback
  ✅ Anti-loop: لا إعادة SL بعد فشل متكرر
  ✅ Market Filter: رفض السوق العرضي
  ✅ Max 2 صفقات متزامنة
  ✅ Breakeven عند +0.5% | Trailing عند +0.7%
=============================================================
"""

import os, time, math, logging, threading, json, requests
from datetime import datetime, timezone

from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
from flask import Flask

# ─── CREDENTIALS ─────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_API_SECRET")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

# ─── عملات السكالبينج (سيولة عالية فقط) ──────────────────────
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

# ─── إعدادات التداول ──────────────────────────────────────────
MAX_OPEN_TRADES   = 2
SCAN_INTERVAL_SEC = 15          # كل 15 ثانية للسكالبينج

# ─── الرافعة (ديناميكية حسب قوة الصفقة) ─────────────────────
LEVERAGE_STRONG = 10            # score >= 75
LEVERAGE_NORMAL = 7             # score 60–74
LEVERAGE_WEAK   = 5             # score 55–59

# ─── أهداف السكالبينج ─────────────────────────────────────────
TP_PCT         = 0.010          # 1.0%
SL_PCT         = 0.006          # 0.6%
TP_PCT_STRONG  = 0.015          # 1.5% للصفقات القوية
SL_PCT_STRONG  = 0.008          # 0.8%
MIN_RR         = 1.8            # نسبة مخاطرة مقبولة

# ─── الحماية الداخلية ─────────────────────────────────────────
BREAKEVEN_PCT       = 0.005     # +0.5% → SL لنقطة الدخول
TRAILING_START_PCT  = 0.007     # +0.7% → يبدأ Trailing
TRAILING_STEP_PCT   = 0.003     # خطوة Trailing
MAX_TRADE_MINUTES   = 30        # أقصى مدة صفقة سكالبينج

# ─── إدارة المخاطر ────────────────────────────────────────────
BASE_RISK_PCT  = 0.03           # 3% من رأس المال
MIN_RISK_PCT   = 0.015
MAX_RISK_PCT   = 0.05
RISK_STEP_WIN  = 0.003
RISK_STEP_LOSS = 0.006

# ─── حماية الرصيد ─────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT = 0.06     # 6% يومي
TOTAL_LOSS_LIMIT_PCT = 0.15     # 15% إجمالي
MAX_DAILY_TRADES     = 8        # أقصى صفقات يومياً
CONSECUTIVE_LOSS_STOP = 2       # توقف بعد خسارتين متتاليتين

# ─── شروط الدخول ─────────────────────────────────────────────
MIN_SCORE      = 55
MIN_VOLUME_RATIO = 1.3          # فوليوم أعلى من المعدل بـ 30%

# ─── SL بايننس — قائمة العملات التي تدعم STOP_MARKET ─────────
SL_SUPPORTED_SYMBOLS = set()    # يُملأ تلقائياً عند البدء

LEARNING_FILE = "bot_learning_v8.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot_v8.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

app    = Flask(__name__)
client: Client = None

open_trades:    dict = {}
_filters_cache: dict = {}

# ─── SL failure cache: لا نعيد المحاولة إذا فشل 3 مرات ───────
_sl_fail_count: dict = {}
MAX_SL_FAIL = 3

bot_start_balance:   float = 0.0
daily_start_balance: float = 0.0
daily_reset_date           = None
bot_halted_total           = False
bot_halted_daily           = False
_last_report_date          = None
_market_is_bull            = True
_daily_trade_count         = 0

learning = {
    "trade_history":      [],
    "symbol_stats":       {},
    "win_rate":           0.0,
    "total_trades":       0,
    "profitable_trades":  0,
    "current_risk_pct":   BASE_RISK_PCT,
    "consecutive_wins":   0,
    "consecutive_losses": 0,
    "peak_balance":       0.0,
    "compounding_mult":   1.0,
    "daily_trades":       0,
    "best_hour_stats":    {},    # إحصاء أفضل ساعات التداول
}


# ══════════════════════════════════════════════════════════════
#  TradeState
# ══════════════════════════════════════════════════════════════

class TradeState:
    def __init__(self, symbol, entry, qty, direction, tp, sl, score, reasons=None):
        self.symbol    = symbol
        self.entry     = entry
        self.qty       = qty
        self.direction = direction  # "long" or "short"
        self.tp_price  = tp
        self.sl_price  = sl
        self.score     = score
        self.reasons   = reasons or []
        self.open_time = utcnow()

        self.highest_price   = entry
        self.lowest_price    = entry
        self.at_breakeven    = False
        self.trailing_active = False
        self.trail_sl        = sl
        self.last_notif_sl   = None

    def update(self, price: float) -> str:
        is_long = self.direction == "long"

        if is_long:
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
                            abs(new_trail - self.last_notif_sl) / self.entry > 0.002):
                        self.last_notif_sl = new_trail
                        return "trailing_move"
            elif pnl >= BREAKEVEN_PCT and not self.at_breakeven:
                self.at_breakeven  = True
                self.trail_sl      = self.entry * 1.0003
                self.last_notif_sl = self.trail_sl
                return "breakeven"
        else:
            # Short
            if price < self.lowest_price:
                self.lowest_price = price
            pnl = (self.entry - price) / self.entry

            if price <= self.tp_price:
                return "tp_hit"
            if price >= self.trail_sl:
                return "sl_hit"

            if pnl >= TRAILING_START_PCT:
                new_trail = self.lowest_price * (1 + TRAILING_STEP_PCT)
                if new_trail < self.trail_sl:
                    self.trail_sl        = new_trail
                    self.trailing_active = True
                    if (self.last_notif_sl is None or
                            abs(new_trail - self.last_notif_sl) / self.entry > 0.002):
                        self.last_notif_sl = new_trail
                        return "trailing_move"
            elif pnl >= BREAKEVEN_PCT and not self.at_breakeven:
                self.at_breakeven  = True
                self.trail_sl      = self.entry * 0.9997
                self.last_notif_sl = self.trail_sl
                return "breakeven"

        return "none"

    def pnl_pct(self, price):
        leverage = self._get_leverage()
        if self.direction == "long":
            return (price - self.entry) / self.entry * 100 * leverage
        else:
            return (self.entry - price) / self.entry * 100 * leverage

    def _get_leverage(self):
        if self.score >= 75:
            return LEVERAGE_STRONG
        elif self.score >= 60:
            return LEVERAGE_NORMAL
        return LEVERAGE_WEAK

    def duration_minutes(self):
        return (utcnow() - self.open_time).total_seconds() / 60

    def rr(self):
        if self.direction == "long":
            risk   = self.entry - self.sl_price
            reward = self.tp_price - self.entry
        else:
            risk   = self.sl_price - self.entry
            reward = self.entry - self.tp_price
        return reward / risk if risk > 0 else 0


# ══════════════════════════════════════════════════════════════
#  UTILS
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

    learning["current_risk_pct"] = risk

    if balance > learning["peak_balance"]:
        learning["peak_balance"] = balance
    if learning["peak_balance"] > 0 and bot_start_balance > 0:
        growth = learning["peak_balance"] / bot_start_balance
        learning["compounding_mult"] = max(1.0, min(growth, 1.5))


def record_trade(trade: TradeState, exit_price: float, balance: float):
    is_long = trade.direction == "long"
    won     = (exit_price > trade.entry) if is_long else (exit_price < trade.entry)
    pnl     = trade.pnl_pct(exit_price)

    # إحصاء أفضل الساعات
    hour_key = str(trade.open_time.hour)
    hs = learning["best_hour_stats"].setdefault(hour_key, {"wins": 0, "losses": 0})
    if won:
        hs["wins"] += 1
    else:
        hs["losses"] += 1

    learning["trade_history"].append({
        "symbol":    trade.symbol,
        "direction": trade.direction,
        "entry":     trade.entry,
        "exit":      exit_price,
        "pnl_pct":   round(pnl, 2),
        "won":       won,
        "minutes":   round(trade.duration_minutes(), 1),
        "score":     trade.score,
        "ts":        utcnow().isoformat(),
    })
    if len(learning["trade_history"]) > 500:
        learning["trade_history"] = learning["trade_history"][-500:]

    st = learning["symbol_stats"].setdefault(trade.symbol, {"wins": 0, "losses": 0, "pnl": 0.0, "long_wins": 0, "short_wins": 0})
    if won:
        st["wins"] += 1
        if is_long:
            st["long_wins"] += 1
        else:
            st["short_wins"] += 1
    else:
        st["losses"] += 1
    st["pnl"] += pnl

    learning["total_trades"] += 1
    if won:
        learning["profitable_trades"] += 1
    learning["win_rate"] = learning["profitable_trades"] / learning["total_trades"]

    update_risk(won, balance)
    save_learning()
    log.info(f"📊 {trade.symbol} {trade.direction} {'✅' if won else '❌'} {pnl:+.2f}% | Win%:{learning['win_rate']*100:.1f}%")


def get_effective_risk():
    base = learning["current_risk_pct"]
    # عقوبة الخسائر المتتالية
    consec_loss = learning["consecutive_losses"]
    if consec_loss >= 2:
        base = MIN_RISK_PCT
    mult = learning["compounding_mult"]
    return min(base * mult, MAX_RISK_PCT)


def sym_win_rate(symbol: str) -> float:
    st = learning["symbol_stats"].get(symbol)
    if not st:
        return 0.5
    total = st["wins"] + st["losses"]
    return st["wins"] / total if total else 0.5


def is_bad_hour() -> bool:
    """تجنب الساعات ذات معدل خسارة مرتفع تاريخياً"""
    hour_key = str(utcnow().hour)
    hs = learning["best_hour_stats"].get(hour_key)
    if not hs:
        return False
    total = hs["wins"] + hs["losses"]
    if total < 5:
        return False
    wr = hs["wins"] / total
    return wr < 0.35  # ساعة خاسرة تاريخياً


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


def probe_sl_support(symbol: str) -> bool:
    """اختبار إذا كان الرمز يدعم STOP_MARKET"""
    try:
        price = get_current_price(symbol)
        if price <= 0:
            return False
        # محاولة أمر وهمي بكمية 0 — لكشف الدعم
        test_price = round_price(symbol, price * 0.80)
        client.futures_create_order(
            symbol      = symbol,
            side        = SIDE_SELL,
            type        = "STOP_MARKET",
            stopPrice   = test_price,
            quantity    = round_qty(symbol, 0),  # كمية 0 → سيرفض لكن يكشف نوع الخطأ
            reduceOnly  = True,
            workingType = "MARK_PRICE"
        )
        return True
    except Exception as e:
        code = str(e)
        if "-4120" in code:
            return False
        # أي خطأ آخر = مدعوم (الكمية 0 سترفض لكن بكود آخر)
        return "-4120" not in code


def detect_sl_supported_symbols():
    """يُشغَّل مرة عند البدء لمعرفة العملات الداعمة لـ STOP_MARKET"""
    global SL_SUPPORTED_SYMBOLS
    log.info("🔍 كشف دعم STOP_MARKET...")
    for sym in SYMBOLS:
        supported = probe_sl_support(sym)
        if supported:
            SL_SUPPORTED_SYMBOLS.add(sym)
        log.info(f"  {sym}: {'✅ مدعوم' if supported else '⚠️ غير مدعوم (Algo)'}")
    log.info(f"STOP_MARKET مدعوم لـ: {SL_SUPPORTED_SYMBOLS or 'لا شيء — سنعتمد على الحماية الداخلية'}")


# ══════════════════════════════════════════════════════════════
#  SL على بايننس — مع fallback ذكي
# ══════════════════════════════════════════════════════════════

def place_binance_sl(symbol: str, entry: float, qty: float, direction: str) -> bool:
    """
    ضع SL على بايننس — مع fallback إذا كان الرمز لا يدعم STOP_MARKET.
    يراعي: Long → SELL STOP | Short → BUY STOP
    لا يعيد المحاولة إذا وصل عدد الإخفاقات لـ MAX_SL_FAIL
    """
    if qty <= 0:
        return False

    fail_key = f"{symbol}_{direction}"
    if _sl_fail_count.get(fail_key, 0) >= MAX_SL_FAIL:
        log.debug(f"⏭️ BN-SL {symbol}: تجاوز حد المحاولات — الحماية الداخلية تعمل")
        return False

    is_long = direction == "long"
    sl_price = round_price(
        symbol,
        entry * (1 - 0.025) if is_long else entry * (1 + 0.025)
    )
    side = SIDE_SELL if is_long else SIDE_BUY

    # أولاً: إلغاء الأوامر القديمة
    try:
        for o in client.futures_get_open_orders(symbol=symbol):
            if "STOP" in o.get("type", ""):
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                except Exception:
                    pass
        time.sleep(0.3)
    except Exception:
        pass

    # محاولة STOP_MARKET
    if symbol in SL_SUPPORTED_SYMBOLS or not SL_SUPPORTED_SYMBOLS:
        try:
            client.futures_create_order(
                symbol      = symbol,
                side        = side,
                type        = "STOP_MARKET",
                stopPrice   = sl_price,
                quantity    = qty,
                reduceOnly  = True,
                workingType = "MARK_PRICE"
            )
            _sl_fail_count[fail_key] = 0
            log.info(f"✅ BN-SL={sl_price} {symbol}")
            return True
        except Exception as e:
            code = str(e)
            if "-4120" in code:
                # هذا الرمز لا يدعم STOP_MARKET
                SL_SUPPORTED_SYMBOLS.discard(symbol)
                log.warning(f"⚠️ {symbol}: لا يدعم STOP_MARKET — تفعيل الحماية الداخلية فقط")
            else:
                log.error(f"❌ BN-SL {symbol}: {e}")
            _sl_fail_count[fail_key] = _sl_fail_count.get(fail_key, 0) + 1
            return False

    # إذا كان مدعوماً فقط بـ Algo — سجل ولا تحاول
    log.info(f"ℹ️ {symbol}: SL محلي فقط (لا Algo API)")
    return False


def check_and_restore_sl(symbol: str, trade: TradeState):
    """إعادة SL فقط إذا لم يتجاوز حد الإخفاقات"""
    fail_key = f"{symbol}_{trade.direction}"
    if _sl_fail_count.get(fail_key, 0) >= MAX_SL_FAIL:
        return  # لا تعيد المحاولة

    try:
        orders = client.futures_get_open_orders(symbol=symbol)
        has_sl = any("STOP" in o.get("type", "") for o in orders)
        if not has_sl:
            amt, _ = get_actual_position(symbol)
            if abs(amt) > 1e-8:
                log.warning(f"⚠️ {symbol}: SL مفقود — إعادة")
                place_binance_sl(symbol, trade.entry, abs(amt), trade.direction)
    except Exception as e:
        log.error(f"check_sl {symbol}: {e}")


# ══════════════════════════════════════════════════════════════
#  TECHNICAL ANALYSIS — SCALPING (5m + 1m)
# ══════════════════════════════════════════════════════════════

def ema_calc(values, period):
    if len(values) < period:
        return values[-1] if values else 0
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
    rs = ag / al
    return 100 - 100 / (1 + rs)


def find_support_resistance(highs, lows, closes, lookback=20):
    """إيجاد أقرب دعم ومقاومة"""
    recent_highs = highs[-lookback:]
    recent_lows  = lows[-lookback:]
    price        = closes[-1]

    resistance = max(recent_highs)
    support    = min(recent_lows)

    # أقرب مستوى
    closer_resistance = min((h for h in recent_highs if h > price), default=resistance)
    closer_support    = max((l for l in recent_lows  if l < price), default=support)

    return closer_support, closer_resistance


def detect_market_structure(closes_5m, highs_5m, lows_5m):
    """
    تحديد هيكل السوق:
    - TRENDING_UP: سلسلة قيعان وقمم متصاعدة
    - TRENDING_DOWN: سلسلة قيعان وقمم متراجعة
    - RANGING: سوق عرضي → لا ندخل
    """
    if len(closes_5m) < 20:
        return "UNKNOWN"

    # نقارن آخر 3 قيعان محلية و3 قمم
    recent = closes_5m[-20:]
    highs  = highs_5m[-20:]
    lows   = lows_5m[-20:]

    # اتجاه EMA
    ema9  = ema_calc(closes_5m, 9)
    ema21 = ema_calc(closes_5m, 21)
    ema_diff_pct = abs(ema9 - ema21) / closes_5m[-1]

    if ema_diff_pct < 0.001:  # EMA متلاصقة جداً → سوق عرضي
        return "RANGING"

    # تذبذب السعر
    high_range = max(highs) - min(lows)
    price_range_pct = high_range / closes_5m[-1]

    if price_range_pct < 0.004:  # أقل من 0.4% → سوق عرضي
        return "RANGING"

    if ema9 > ema21 and closes_5m[-1] > ema21:
        return "TRENDING_UP"
    elif ema9 < ema21 and closes_5m[-1] < ema21:
        return "TRENDING_DOWN"

    return "RANGING"


def analyze_symbol_scalp(symbol: str) -> dict | None:
    """
    تحليل سكالبينج — 5m أساسي + 1m تأكيد
    يُرجع dict للفرصة أو None
    """
    try:
        # ── 5m البيانات ────────────────────────────────────────
        kl5  = client.futures_klines(symbol=symbol, interval="5m", limit=100)
        cl5  = [float(k[4]) for k in kl5]
        hi5  = [float(k[2]) for k in kl5]
        lo5  = [float(k[3]) for k in kl5]
        vo5  = [float(k[5]) for k in kl5]

        # ── 1m التأكيد ─────────────────────────────────────────
        kl1  = client.futures_klines(symbol=symbol, interval="1m", limit=30)
        cl1  = [float(k[4]) for k in kl1]

        price = cl5[-1]
        if price <= 0:
            return None

        # ── المؤشرات ───────────────────────────────────────────
        ema9_5m  = ema_calc(cl5, 9)
        ema21_5m = ema_calc(cl5, 21)
        ema9_1m  = ema_calc(cl1, 9)
        ema21_1m = ema_calc(cl1, 21)

        rsi_5m = compute_rsi(cl5)
        rsi_1m = compute_rsi(cl1)

        avg_vol   = sum(vo5[-20:]) / 20 or 1
        vol_ratio = vo5[-1] / avg_vol

        support, resistance = find_support_resistance(hi5, lo5, cl5)
        structure           = detect_market_structure(cl5, hi5, lo5)

        # ── رفض فوري ──────────────────────────────────────────
        if structure == "RANGING":
            log.debug(f"{symbol}: سوق عرضي — رفض")
            return None
        if vol_ratio < MIN_VOLUME_RATIO:
            log.debug(f"{symbol}: فوليوم منخفض {vol_ratio:.2f} — رفض")
            return None

        # ── قرار الاتجاه ──────────────────────────────────────
        # هل هو تقاطع EMA 9/21 جديد؟
        prev_ema9_5m  = ema_calc(cl5[:-1], 9)
        prev_ema21_5m = ema_calc(cl5[:-1], 21)
        ema_cross_up   = (prev_ema9_5m <= prev_ema21_5m) and (ema9_5m > ema21_5m)
        ema_cross_down = (prev_ema9_5m >= prev_ema21_5m) and (ema9_5m < ema21_5m)
        ema_above      = ema9_5m > ema21_5m
        ema_below      = ema9_5m < ema21_5m

        # قرب الدعم/المقاومة
        dist_to_support    = (price - support)    / price if support    > 0 else 1
        dist_to_resistance = (resistance - price) / price if resistance > 0 else 1

        # ── تحديد الاتجاه: Long أو Short ─────────────────────
        direction = None
        score     = 0
        reasons   = []

        # === LONG ===
        if structure == "TRENDING_UP" and ema_above:
            direction = "long"
            score += 20; reasons.append("ترند↑")

            if ema_cross_up:
                score += 25; reasons.append("تقاطع↑جديد")
            elif ema_above:
                score += 10; reasons.append("EMA9>21")

            # RSI 5m
            if 45 <= rsi_5m <= 65:
                score += 15; reasons.append(f"RSI5m✓{rsi_5m:.0f}")
            elif 35 <= rsi_5m < 45:
                score += 20; reasons.append(f"RSI5m-OS{rsi_5m:.0f}")
            elif rsi_5m > 70:
                score -= 25; reasons.append(f"RSI-OB{rsi_5m:.0f}")
            elif rsi_5m < 35:
                score += 10; reasons.append(f"RSI-Deep{rsi_5m:.0f}")

            # RSI 1m تأكيد
            if 40 <= rsi_1m <= 65:
                score += 10; reasons.append(f"RSI1m✓{rsi_1m:.0f}")
            elif rsi_1m > 70:
                score -= 10

            # 1m ترند تأكيد
            if ema9_1m > ema21_1m:
                score += 10; reasons.append("1m↑تأكيد")

            # قرب الدعم
            if dist_to_support < 0.005:
                score += 15; reasons.append("دعم✓")

            # ابتعاد عن المقاومة
            if dist_to_resistance < 0.005:
                score -= 15; reasons.append("قرب-مقاومة⚠️")

        # === SHORT ===
        elif structure == "TRENDING_DOWN" and ema_below:
            direction = "short"
            score += 20; reasons.append("ترند↓")

            if ema_cross_down:
                score += 25; reasons.append("تقاطع↓جديد")
            elif ema_below:
                score += 10; reasons.append("EMA9<21")

            # RSI
            if 35 <= rsi_5m <= 55:
                score += 15; reasons.append(f"RSI5m✓{rsi_5m:.0f}")
            elif rsi_5m > 65:
                score += 20; reasons.append(f"RSI5m-OB{rsi_5m:.0f}")
            elif rsi_5m < 30:
                score -= 25; reasons.append(f"RSI-OS{rsi_5m:.0f}")

            if 35 <= rsi_1m <= 60:
                score += 10; reasons.append(f"RSI1m✓{rsi_1m:.0f}")
            elif rsi_1m < 30:
                score -= 10

            if ema9_1m < ema21_1m:
                score += 10; reasons.append("1m↓تأكيد")

            if dist_to_resistance < 0.005:
                score += 15; reasons.append("مقاومة✓")
            if dist_to_support < 0.005:
                score -= 15; reasons.append("قرب-دعم⚠️")
        else:
            return None  # لا اتجاه واضح

        # فوليوم
        if vol_ratio > 2.5:
            score += 15; reasons.append(f"Vol×{vol_ratio:.1f}🔥")
        elif vol_ratio > 1.5:
            score += 8;  reasons.append(f"Vol×{vol_ratio:.1f}")

        # سمعة الرمز
        wr = sym_win_rate(symbol)
        if wr > 0.60:
            score += 8;  reasons.append(f"WR{wr*100:.0f}%")
        elif wr < 0.35:
            score -= 10

        if score < MIN_SCORE:
            log.info(f"{symbol} {direction}: score={score} < {MIN_SCORE} — رفض")
            return None

        # ── حساب SL / TP ───────────────────────────────────────
        is_strong = score >= 75
        tp_pct    = TP_PCT_STRONG if is_strong else TP_PCT
        sl_pct    = SL_PCT_STRONG if is_strong else SL_PCT

        if direction == "long":
            tp_price = price * (1 + tp_pct)
            sl_price = price * (1 - sl_pct)
        else:
            tp_price = price * (1 - tp_pct)
            sl_price = price * (1 + sl_pct)

        # فحص RR
        if direction == "long":
            rr = (tp_price - price) / (price - sl_price) if (price - sl_price) > 0 else 0
        else:
            rr = (price - tp_price) / (sl_price - price) if (sl_price - price) > 0 else 0

        if rr < MIN_RR:
            log.info(f"{symbol}: RR={rr:.2f} < {MIN_RR} — رفض")
            return None

        return {
            "symbol":    symbol,
            "direction": direction,
            "score":     score,
            "rsi_5m":    round(rsi_5m, 1),
            "rsi_1m":    round(rsi_1m, 1),
            "price":     price,
            "tp_price":  round_price(symbol, tp_price),
            "sl_price":  round_price(symbol, sl_price),
            "rr":        round(rr, 2),
            "vol_ratio": round(vol_ratio, 2),
            "structure": structure,
            "reasons":   reasons,
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
        kl  = client.futures_klines(symbol="BTCUSDT", interval="15m", limit=50)
        cls = [float(k[4]) for k in kl]
        ema21 = ema_calc(cls, 21)
        prev  = _market_is_bull
        _market_is_bull = cls[-1] >= ema21 * 0.98
        if prev != _market_is_bull:
            s = "🟢 صاعد" if _market_is_bull else "🔴 هابط"
            send_telegram(f"📡 *تغيير السوق: {s}*\nBTC:`{cls[-1]:.2f}` | EMA21:`{ema21:.2f}`")
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


def market_close(symbol: str, qty: float, direction: str) -> bool:
    qty  = abs(qty)
    if qty <= 0:
        return False
    cancel_sl_orders(symbol)
    side = SIDE_SELL if direction == "long" else SIDE_BUY
    for attempt in range(3):
        try:
            client.futures_create_order(
                symbol=symbol, side=side,
                type=ORDER_TYPE_MARKET, quantity=qty, reduceOnly=True,
            )
            log.info(f"✅ إغلاق: {symbol} {direction} qty={qty}")
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

                # timeout أقصر للسكالبينج
                if trade.duration_minutes() >= MAX_TRADE_MINUTES:
                    _execute_close(symbol, trade, price, "timeout")
                    continue

                event = trade.update(price)

                if event == "sl_hit":
                    _execute_close(symbol, trade, price, "sl_internal")
                elif event == "tp_hit":
                    _execute_close(symbol, trade, price, "tp_internal")
                elif event == "breakeven":
                    send_telegram(
                        f"🔒 *Breakeven: {symbol}* {'🟢L' if trade.direction=='long' else '🔴S'}\n"
                        f"P&L:`+{trade.pnl_pct(price):.2f}%`"
                    )
                elif event == "trailing_move":
                    send_telegram(
                        f"📈 *Trailing ↑ {symbol}*\n"
                        f"SL:`{trade.trail_sl:.4f}` | P&L:`+{trade.pnl_pct(price):.2f}%`"
                    )

                check_and_restore_sl(symbol, trade)

        except Exception as e:
            log.error(f"protection_monitor: {e}")

        time.sleep(3)  # أسرع للسكالبينج


def _execute_close(symbol, trade, price, reason):
    amt, _ = get_actual_position(symbol)
    if abs(amt) < 1e-8:
        open_trades.pop(symbol, None)
        return
    ok = market_close(symbol, abs(amt), trade.direction)
    if ok:
        open_trades.pop(symbol, None)
        pnl     = trade.pnl_pct(price)
        emoji   = "🟢" if pnl >= 0 else "🔴"
        balance = get_futures_balance()
        record_trade(trade, price, balance)
        dir_emoji = "📈L" if trade.direction == "long" else "📉S"
        labels  = {
            "sl_internal": "وقف الخسارة ⛔",
            "tp_internal": "جني الأرباح 💰",
            "timeout":     f"Timeout {MAX_TRADE_MINUTES}m ⏰",
        }
        send_telegram(
            f"{emoji} *{dir_emoji} مُغلقة: {symbol}*\n"
            f"السبب: {labels.get(reason, reason)}\n"
            f"دخول:`{trade.entry:.4f}` → خروج:`{price:.4f}`\n"
            f"P&L:`{pnl:+.2f}%` | مدة:`{trade.duration_minutes():.0f}m`\n"
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

def open_position(candidate: dict) -> bool:
    global _daily_trade_count
    symbol    = candidate["symbol"]
    price     = candidate["price"]
    direction = candidate["direction"]
    score     = candidate["score"]

    # الرافعة حسب قوة الصفقة
    if score >= 75:
        leverage = LEVERAGE_STRONG
    elif score >= 60:
        leverage = LEVERAGE_NORMAL
    else:
        leverage = LEVERAGE_WEAK

    amt, _ = get_actual_position(symbol)
    if abs(amt) > 1e-8 or symbol in open_trades:
        return False
    if len(open_trades) >= MAX_OPEN_TRADES:
        return False
    if _daily_trade_count >= MAX_DAILY_TRADES:
        log.info(f"وصل الحد اليومي {MAX_DAILY_TRADES} صفقة")
        return False

    try:
        _, _, min_notional = get_filters(symbol)
        balance = get_futures_balance()
        avail   = get_available_margin()

        effective_risk = get_effective_risk()
        sl_pct         = SL_PCT_STRONG if score >= 75 else SL_PCT

        qty_by_risk  = (balance * effective_risk) / (price * sl_pct)
        qty_by_avail = (avail * 0.80 * leverage) / price
        raw_qty      = min(qty_by_risk, qty_by_avail)
        qty          = round_qty(symbol, raw_qty)

        if qty <= 0 or qty * price < min_notional:
            log.info(f"{symbol}: qty={qty:.4f} — تخطي")
            return False

        try:
            client.futures_change_leverage(symbol=symbol, leverage=leverage)
        except Exception as e:
            log.warning(f"leverage {symbol}: {e}")

        side = SIDE_BUY if direction == "long" else SIDE_SELL

        for attempt in range(3):
            try:
                client.futures_create_order(
                    symbol=symbol, side=side,
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

        # أعد احتساب SL/TP بناءً على سعر الدخول الفعلي
        tp_pct = TP_PCT_STRONG if score >= 75 else TP_PCT
        if direction == "long":
            tp = round_price(symbol, actual_entry * (1 + tp_pct))
            sl = round_price(symbol, actual_entry * (1 - sl_pct))
        else:
            tp = round_price(symbol, actual_entry * (1 - tp_pct))
            sl = round_price(symbol, actual_entry * (1 + sl_pct))

        trade = TradeState(
            symbol    = symbol,
            entry     = actual_entry,
            qty       = actual_qty,
            direction = direction,
            tp        = tp,
            sl        = sl,
            score     = score,
            reasons   = candidate.get("reasons", []),
        )
        open_trades[symbol]    = trade
        _daily_trade_count    += 1

        # SL على بايننس (مع ignore إذا لم يدعمه الرمز)
        fail_key = f"{symbol}_{direction}"
        _sl_fail_count[fail_key] = 0  # إعادة العداد للوضعية الجديدة
        bn_ok = place_binance_sl(symbol, actual_entry, actual_qty, direction)

        dir_label = "📈 Long" if direction == "long" else "📉 Short"
        send_telegram(
            f"🚀 *دخول {dir_label}: {symbol}*\n"
            f"سعر:`{actual_entry:.4f}` | رافعة:`{leverage}x`\n"
            f"TP:`{tp:.4f}` (+{tp_pct*100:.1f}%)\n"
            f"SL:`{sl:.4f}` (-{sl_pct*100:.1f}%)\n"
            f"RR:`{trade.rr():.2f}` | BE`+{BREAKEVEN_PCT*100:.1f}%`\n"
            f"SL-BN: {'✅' if bn_ok else 'ℹ️ محلي فقط'}\n"
            f"─────────────────\n"
            f"Score:`{score}` | RSI5m:`{candidate['rsi_5m']:.0f}` RSI1m:`{candidate['rsi_1m']:.0f}`\n"
            f"Vol:`×{candidate['vol_ratio']:.1f}` | Risk:`{effective_risk*100:.1f}%`\n"
            f"📋 {' | '.join(candidate.get('reasons', [])[:5])}"
        )
        log.info(f"✅ {direction} {symbol} @ {actual_entry:.4f} ×{leverage} score={score}")
        return True

    except Exception as e:
        log.error(f"open_position {symbol}: {e}")
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
            if sym not in SYMBOLS:
                continue

            direction = "long" if amt > 0 else "short"
            tp_pct    = TP_PCT
            sl_pct    = SL_PCT
            if direction == "long":
                tp = round_price(sym, entry * (1 + tp_pct))
                sl = round_price(sym, entry * (1 - sl_pct))
            else:
                tp = round_price(sym, entry * (1 - tp_pct))
                sl = round_price(sym, entry * (1 + sl_pct))

            trade = TradeState(sym, entry, abs(amt), direction, tp, sl, score=60, reasons=["موروثة"])
            open_trades[sym] = trade
            fail_key = f"{sym}_{direction}"
            _sl_fail_count[fail_key] = 0
            place_binance_sl(sym, entry, abs(amt), direction)
            adopted += 1

    except Exception as e:
        log.error(f"adopt: {e}")

    msg = f"🔄 *تبنّي — {adopted} وضعية*\n"
    for sym, t in open_trades.items():
        msg += f"  • `{sym}` {t.direction} @ `{t.entry:.4f}`\n"
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
    global _daily_trade_count

    if bot_halted_total:
        return False

    today = utcnow().date()
    if daily_reset_date != today:
        daily_start_balance  = balance
        daily_reset_date     = today
        bot_halted_daily     = False
        _daily_trade_count   = 0
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

    # وقف بعد خسارتين متتاليتين
    if learning["consecutive_losses"] >= CONSECUTIVE_LOSS_STOP:
        if not open_trades:
            log.info(f"⛔ {CONSECUTIVE_LOSS_STOP} خسائر متتالية — انتظار 15 دقيقة")
            send_telegram(f"⏸️ *{CONSECUTIVE_LOSS_STOP} خسائر متتالية — توقف 15 دق*")
            time.sleep(900)
            learning["consecutive_losses"] = 0
        return False if not open_trades else True

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
        msg += f"صفقات اليوم:`{_daily_trade_count}`\n"
        msg += f"─────────────────\n"
        # أفضل الساعات
        best_hours = sorted(
            [(h, s) for h, s in learning["best_hour_stats"].items()
             if (s["wins"]+s["losses"]) >= 3],
            key=lambda x: x[1]["wins"]/(x[1]["wins"]+x[1]["losses"]),
            reverse=True
        )[:3]
        if best_hours:
            msg += "⏰ أفضل ساعات:\n"
            for h, s in best_hours:
                tot = s["wins"] + s["losses"]
                msg += f"  {h}:00 — WR:{s['wins']/tot*100:.0f}% ({tot} صفقة)\n"
        send_telegram(msg)
    except Exception as e:
        log.error(f"daily_report: {e}")


# ══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════

def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date, client

    log.info("🚀 بوت v8.0 SCALPING — BTC ETH SOL XRP | 5m+1m")
    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
    load_learning()

    for sym in SYMBOLS:
        get_filters(sym)

    # كشف دعم STOP_MARKET لكل رمز
    detect_sl_supported_symbols()

    initial = get_futures_balance()
    if learning["peak_balance"] == 0:
        learning["peak_balance"] = initial

    bot_start_balance   = initial
    daily_start_balance = initial
    daily_reset_date    = utcnow().date()

    threading.Thread(target=protection_monitor, daemon=True, name="ProtMon").start()

    lev_str = f"L:{LEVERAGE_STRONG}x/N:{LEVERAGE_NORMAL}x/W:{LEVERAGE_WEAK}x"
    send_telegram(
        f"🤖 *بوت v8.0 SCALPING* ✅\n"
        f"رصيد:`{initial:.2f}` USDT\n"
        f"عملات: BTC ETH SOL XRP\n"
        f"إطار: 5m+1m | Long & Short\n"
        f"─── الأهداف ───\n"
        f"TP:`0.8-1.5%` SL:`0.5-0.8%` RR≥`{MIN_RR}`\n"
        f"─── الرافعة ───\n"
        f"{lev_str}\n"
        f"─── الحماية ───\n"
        f"BE`+{BREAKEVEN_PCT*100:.1f}%` Trail`+{TRAILING_START_PCT*100:.1f}%`\n"
        f"Max:`{MAX_TRADE_MINUTES}m` | يومي:`{DAILY_LOSS_LIMIT_PCT*100:.0f}%`\n"
        f"─── SL بايننس ───\n"
        f"مدعوم: {', '.join(SL_SUPPORTED_SYMBOLS) or 'لا شيء (حماية داخلية)'}"
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
                f"{'🟢' if _market_is_bull else '🔴'} "
                f"Win%:{learning['win_rate']*100:.1f}% ══"
            )

            if mf_cycle >= 20:  # كل 5 دقائق (20×15s)
                update_market_filter()
                mf_cycle = 0

            if not check_protection(balance):
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            if avail < 2.0 or len(open_trades) >= MAX_OPEN_TRADES:
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            if is_bad_hour():
                log.info("⏰ ساعة ضعيفة تاريخياً — انتظار")
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            # ── مسح العملات ───────────────────────────────────
            candidates = []
            for sym in SYMBOLS:
                if sym in open_trades:
                    continue
                amt, _ = get_actual_position(sym)
                if abs(amt) > 1e-8:
                    continue

                # Long فقط في سوق صاعد، Short فقط في هابط
                result = analyze_symbol_scalp(sym)
                if result:
                    dir_ok = (result["direction"] == "long" and _market_is_bull) or \
                             (result["direction"] == "short" and not _market_is_bull)
                    # لكن نسمح بالاتجاهين إذا كان السكور عالياً جداً (>80)
                    if dir_ok or result["score"] >= 80:
                        candidates.append(result)
                        log.info(
                            f"✅ {sym} {result['direction']}: {result['score']}pts "
                            f"RSI5m={result['rsi_5m']:.0f} Vol×{result['vol_ratio']:.1f}"
                        )

            if candidates:
                # ترتيب: أعلى سكور، ثم RR
                candidates.sort(key=lambda x: (-x["score"], -x["rr"]))
                for c in candidates:
                    if len(open_trades) >= MAX_OPEN_TRADES:
                        break
                    if get_available_margin() < 2.0:
                        break
                    if open_position(c):
                        time.sleep(2)
            else:
                log.info("لا فرص الآن.")

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
        f"<b>🤖 Bot v8.0 SCALPING</b> | {bull}",
        f"رصيد: <b>{bal:.2f} USDT</b> | مفتوحة: {len(open_trades)}/{MAX_OPEN_TRADES}",
        f"Win%: {learning['win_rate']*100:.1f}% ({learning['total_trades']} صفقة) | اليوم: {_daily_trade_count}/{MAX_DAILY_TRADES}",
        f"Risk: {learning['current_risk_pct']*100:.1f}% | Comp: ×{learning['compounding_mult']:.2f}",
        f"SL مدعوم: {', '.join(SL_SUPPORTED_SYMBOLS) or 'لا شيء'}",
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
            f"• <b>{sym}</b> {t.direction} @ {t.entry:.4f} | "
            f"<span style='color:{color}'>{pnl:+.2f}%</span> | "
            f"SL:{t.trail_sl:.4f} TP:{t.tp_price:.4f} | "
            f"RR:{t.rr():.2f} | {t.duration_minutes():.0f}m | "
            f"{' '.join(flags)}"
        )
    return "<br>".join(lines)


@app.route("/trades")
def trades_route():
    result = {}
    for sym, t in open_trades.items():
        cp = get_current_price(sym)
        result[sym] = {
            "direction": t.direction,
            "entry":     t.entry,
            "current":   cp,
            "pnl_pct":   round(t.pnl_pct(cp), 2),
            "sl":        round(t.trail_sl, 6),
            "tp":        round(t.tp_price, 6),
            "rr":        round(t.rr(), 2),
            "breakeven": t.at_breakeven,
            "trailing":  t.trailing_active,
            "minutes":   round(t.duration_minutes(), 1),
            "score":     t.score,
        }
    return json.dumps(result, ensure_ascii=False, indent=2)


@app.route("/stats")
def stats_route():
    result = {}
    for sym in SYMBOLS:
        st    = learning["symbol_stats"].get(sym, {"wins": 0, "losses": 0, "pnl": 0.0})
        total = st["wins"] + st["losses"]
        result[sym] = {
            "wins":       st["wins"],
            "losses":     st["losses"],
            "win_rate":   round(st["wins"]/total*100, 1) if total else 0,
            "pnl":        round(st["pnl"], 2),
            "long_wins":  st.get("long_wins", 0),
            "short_wins": st.get("short_wins", 0),
        }
    return json.dumps(result, ensure_ascii=False, indent=2)


@app.route("/learning")
def learning_route():
    return json.dumps({
        k: learning[k] for k in [
            "win_rate", "total_trades", "current_risk_pct",
            "compounding_mult", "consecutive_wins", "consecutive_losses",
            "peak_balance", "best_hour_stats",
        ]
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
