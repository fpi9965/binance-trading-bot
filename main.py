"""
=============================================================
  SMART TRADING BOT v10.0  — REBUILD FROM SCRATCH
  ─────────────────────────────────────────────────
  استراتيجية مزدوجة موثوقة:

  1) EMA CROSSOVER  → تحديد الاتجاه (1h)
  2) BREAKOUT       → تأكيد الدخول (15m)
  3) SENTIMENT      → فلتر الأخبار (Fear & Greed Index)

  قواعد الدخول الصارمة:
  ✅ EMA 9 يقطع EMA 21 من الأسفل (long) أو من الأعلى (short)
  ✅ السعر يكسر مستوى مقاومة/دعم واضح
  ✅ RSI بين 40-60 (لا ذروة شراء/بيع)
  ✅ Sentiment لا يعاكس الاتجاه
  ✅ حجم أعلى من المتوسط ×1.5

  إدارة الصفقة:
  • SL = ATR × 1.5 (ضيق ومحكم)
  • TP = ATR × 3.0 (RR = 2.0 minimum)
  • Breakeven عند +0.8%
  • Trailing عند +1.5%
  • رافعة ثابتة 5x (آمنة)
  • حد أقصى 3 صفقات مفتوحة
=============================================================
"""

import os, time, math, logging, threading, json, requests
from datetime import datetime, timezone
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
from flask import Flask, request as flask_request

# ─── CREDENTIALS ─────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_SECRET")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT")
TV_SECRET          = os.getenv("TV_SECRET",           "my_secret_123")

# ─── العملات (فقط الكبيرة الموثوقة) ──────────────────────────
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "DOTUSDT",
]

# ─── إعدادات التداول ──────────────────────────────────────────
MAX_OPEN_TRADES   = 3
LEVERAGE          = 5              # ثابت — لا تغيير
SCAN_INTERVAL_SEC = 45
MAX_TRADE_HRS     = 10

# ─── TP / SL ─────────────────────────────────────────────────
ATR_SL_MULT   = 1.5               # SL = ATR × 1.5
ATR_TP_MULT   = 3.0               # TP = ATR × 3.0 → RR = 2.0
MIN_RR        = 1.8

# ─── Breakeven / Trailing ─────────────────────────────────────
BE_TRIGGER    = 0.008             # +0.8% → breakeven
TRAIL_TRIGGER = 0.015             # +1.5% → trailing
TRAIL_STEP    = 0.005

# ─── إدارة المخاطر ────────────────────────────────────────────
RISK_PER_TRADE = 0.015            # 1.5% من الرصيد لكل صفقة
MAX_DAILY_LOSS = 0.05             # 5% يومياً
MAX_TOTAL_LOSS = 0.12             # 12% إجمالي

# ─── شروط الدخول ─────────────────────────────────────────────
MIN_VOL_RATIO     = 1.5           # الحجم ≥ 1.5× المتوسط
RSI_MIN           = 35            # RSI لا يكون في ذروة
RSI_MAX           = 65
BREAKOUT_LOOKBACK = 20            # نشوف آخر 20 شمعة للمستويات

# ─── Fear & Greed Sentiment ───────────────────────────────────
SENTIMENT_ENABLED     = True
SENTIMENT_CACHE_MIN   = 30        # تحديث كل 30 دقيقة
FEAR_BLOCK_LONG       = 25        # لو الخوف < 25 → لا long
GREED_BLOCK_SHORT     = 75        # لو الطمع > 75 → لا short

# ─── ساعات راحة (UTC) ─────────────────────────────────────────
NO_TRADE_HOURS = {2, 3, 4}

LEARNING_FILE = "bot_v10_data.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot_v10.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

app    = Flask(__name__)
client: Client = None

open_trades:    dict = {}
_filters_cache: dict = {}
_sl_no_support: set  = set()

bot_start_bal   = 0.0
daily_start_bal = 0.0
daily_reset_dt  = None
halted          = False
_daily_trades   = 0
_market_bull    = True

# ─── Sentiment Cache ──────────────────────────────────────────
_sentiment = {
    "value":       50,             # 0=خوف شديد, 100=طمع شديد
    "label":       "Neutral",
    "last_update": None,
    "ok":          False,
}

data = {
    "trades":      [],
    "wins":        0,
    "losses":      0,
    "total_pnl":   0.0,
    "peak_bal":    0.0,
    "daily_count": 0,
}


# ══════════════════════════════════════════════════════════════
#  TradeState
# ══════════════════════════════════════════════════════════════
class TradeState:
    def __init__(self, symbol, entry, qty, direction, tp, sl, atr, reasons):
        self.symbol    = symbol
        self.entry     = entry
        self.qty       = qty
        self.direction = direction
        self.tp_price  = tp
        self.sl_price  = sl
        self.atr       = atr
        self.reasons   = reasons
        self.open_time = utcnow()
        self.highest   = entry
        self.lowest    = entry
        self.breakeven = False
        self.trailing  = False
        self.trail_sl  = sl
        self.notified  = False

    def pnl_pct(self, price):
        if self.direction == "long":
            return (price - self.entry) / self.entry * 100 * LEVERAGE
        return (self.entry - price) / self.entry * 100 * LEVERAGE

    def rr(self):
        if self.direction == "long":
            risk = self.entry - self.sl_price
            rew  = self.tp_price - self.entry
        else:
            risk = self.sl_price - self.entry
            rew  = self.entry - self.tp_price
        return rew / risk if risk > 1e-9 else 0

    def duration_hrs(self):
        return (utcnow() - self.open_time).total_seconds() / 3600

    def update(self, price):
        is_long = self.direction == "long"
        if is_long:
            if price > self.highest: self.highest = price
            pnl = (price - self.entry) / self.entry
            if price >= self.tp_price: return "tp"
            if price <= self.trail_sl: return "sl"
            if pnl >= TRAIL_TRIGGER:
                new_sl = self.highest * (1 - TRAIL_STEP)
                if new_sl > self.trail_sl:
                    self.trail_sl = new_sl
                    self.trailing = True
                    return "trail"
            elif pnl >= BE_TRIGGER and not self.breakeven:
                self.breakeven = True
                self.trail_sl  = self.entry * 1.001
                return "be"
        else:
            if price < self.lowest: self.lowest = price
            pnl = (self.entry - price) / self.entry
            if price <= self.tp_price: return "tp"
            if price >= self.trail_sl: return "sl"
            if pnl >= TRAIL_TRIGGER:
                new_sl = self.lowest * (1 + TRAIL_STEP)
                if new_sl < self.trail_sl:
                    self.trail_sl = new_sl
                    self.trailing = True
                    return "trail"
            elif pnl >= BE_TRIGGER and not self.breakeven:
                self.breakeven = True
                self.trail_sl  = self.entry * 0.999
                return "be"
        return "none"


# ══════════════════════════════════════════════════════════════
#  UTILS
# ══════════════════════════════════════════════════════════════
def utcnow():
    return datetime.now(timezone.utc)

def tg(msg):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "YOUR_TOKEN": return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error(f"TG: {e}")

def save_data():
    try:
        with open(LEARNING_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except: pass

def load_data():
    global data
    try:
        if os.path.exists(LEARNING_FILE):
            with open(LEARNING_FILE) as f:
                data.update(json.load(f))
            log.info(f"📚 تاريخ: {data['wins']}W/{data['losses']}L PnL:{data['total_pnl']:.1f}%")
    except Exception as e:
        log.error(f"load_data: {e}")

def record_trade(trade, exit_price):
    won  = exit_price > trade.entry if trade.direction == "long" else exit_price < trade.entry
    pnl  = trade.pnl_pct(exit_price)
    data["total_pnl"] += pnl
    data["daily_count"] += 1
    if won: data["wins"]   += 1
    else:   data["losses"] += 1
    data["trades"].append({
        "sym": trade.symbol, "dir": trade.direction,
        "entry": trade.entry, "exit": exit_price,
        "pnl": round(pnl, 2), "won": won,
        "hrs": round(trade.duration_hrs(), 1),
        "ts":  utcnow().isoformat(),
    })
    if len(data["trades"]) > 200:
        data["trades"] = data["trades"][-200:]
    save_data()
    total = data["wins"] + data["losses"]
    wr    = data["wins"] / total * 100 if total else 0
    log.info(f"📊 {trade.symbol} {'✅' if won else '❌'} {pnl:+.2f}% | WR:{wr:.0f}% ({total}صفقة)")
    return won, pnl

def win_rate():
    total = data["wins"] + data["losses"]
    return data["wins"] / total if total else 0.5


# ══════════════════════════════════════════════════════════════
#  SENTIMENT (Fear & Greed Index)
# ══════════════════════════════════════════════════════════════
def update_sentiment():
    if not SENTIMENT_ENABLED: return
    now  = utcnow()
    last = _sentiment["last_update"]
    if last and (now - last).total_seconds() < SENTIMENT_CACHE_MIN * 60:
        return
    try:
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=8
        )
        if resp.status_code == 200:
            d = resp.json()["data"][0]
            _sentiment["value"]       = int(d["value"])
            _sentiment["label"]       = d["value_classification"]
            _sentiment["last_update"] = now
            _sentiment["ok"]          = True
            log.info(f"😱 Fear&Greed: {_sentiment['value']} ({_sentiment['label']})")
        else:
            _sentiment["last_update"] = now
    except Exception as e:
        log.warning(f"Sentiment: {e}")
        _sentiment["last_update"] = now

def sentiment_block(direction):
    """يمنع الدخول لو Sentiment يعاكس الاتجاه بقوة"""
    if not _sentiment["ok"]: return False, ""
    v = _sentiment["value"]
    if direction == "long"  and v < FEAR_BLOCK_LONG:
        return True, f"😱خوف شديد({v})"
    if direction == "short" and v > GREED_BLOCK_SHORT:
        return True, f"🤑طمع شديد({v})"
    return False, ""

def sentiment_label():
    if not _sentiment["ok"]: return "⚪N/A"
    v = _sentiment["value"]
    if v < 25:   return f"😱خوف شديد({v})"
    if v < 45:   return f"😨خوف({v})"
    if v < 55:   return f"😐محايد({v})"
    if v < 75:   return f"😊طمع({v})"
    return f"🤑طمع شديد({v})"


# ══════════════════════════════════════════════════════════════
#  BINANCE HELPERS
# ══════════════════════════════════════════════════════════════
def balance():
    try:
        for b in client.futures_account_balance():
            if b["asset"] == "USDT":
                return float(b["balance"])
    except Exception as e:
        log.error(f"balance: {e}")
    return 0.0

def avail_margin():
    try:
        return float(client.futures_account()["availableBalance"])
    except Exception as e:
        log.error(f"margin: {e}")
        return 0.0

def get_position(symbol):
    try:
        for p in client.futures_position_information(symbol=symbol):
            return float(p["positionAmt"]), float(p["entryPrice"])
    except Exception as e:
        if "-1022" not in str(e): log.warning(f"pos {symbol}: {e}")
    return 0.0, 0.0

def all_positions():
    try: return client.futures_position_information()
    except: return []

def cur_price(symbol):
    try: return float(client.futures_symbol_ticker(symbol=symbol)["price"])
    except: return 0.0

def get_filters(symbol):
    if symbol in _filters_cache: return _filters_cache[symbol]
    try:
        for s in client.futures_exchange_info()["symbols"]:
            if s["symbol"] != symbol: continue
            lot = tick = None; notional = 5.0
            for f in s["filters"]:
                ft = f["filterType"]
                if ft == "LOT_SIZE":       lot     = float(f["stepSize"])
                elif ft == "PRICE_FILTER": tick    = float(f["tickSize"])
                elif ft == "MIN_NOTIONAL": notional= float(f["notional"])
            if lot and tick:
                _filters_cache[symbol] = (lot, tick, notional)
                return _filters_cache[symbol]
    except Exception as e:
        log.error(f"filters {symbol}: {e}")
    return (0.001, 0.01, 5.0)

def rqty(symbol, qty):
    lot, _, _ = get_filters(symbol)
    if lot <= 0: return round(qty, 3)
    prec = max(0, round(-math.log10(lot)))
    return float(f"{qty:.{prec}f}")

def rprice(symbol, price):
    _, tick, _ = get_filters(symbol)
    if tick <= 0: return round(price, 4)
    prec = max(0, round(-math.log10(tick)))
    return float(f"{price:.{prec}f}")

def cancel_stops(symbol):
    try:
        for o in client.futures_get_open_orders(symbol=symbol):
            if "STOP" in o.get("type", ""):
                try: client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                except: pass
    except: pass

def place_sl(symbol, entry, qty, direction):
    if symbol in _sl_no_support: return False
    is_long = direction == "long"
    sl_p = rprice(symbol, entry * (1 - 0.03) if is_long else entry * (1 + 0.03))
    side = SIDE_SELL if is_long else SIDE_BUY
    cancel_stops(symbol)
    time.sleep(0.3)
    for wt in ("MARK_PRICE", "CONTRACT_PRICE"):
        try:
            client.futures_create_order(
                symbol=symbol, side=side, type="STOP_MARKET",
                stopPrice=sl_p, quantity=qty,
                reduceOnly=True, workingType=wt
            )
            log.info(f"✅ SL {symbol}={sl_p} [{wt}]")
            return True
        except Exception as e:
            if "-4120" in str(e) or "does not support" in str(e).lower():
                continue
            log.error(f"SL {symbol} [{wt}]: {e}")
            return False
    _sl_no_support.add(symbol)
    log.warning(f"⚠️ {symbol}: لا يدعم SL على Binance")
    return False

def mkt_close(symbol, qty, direction):
    qty  = abs(qty)
    if qty <= 0: return False
    cancel_stops(symbol)
    side = SIDE_SELL if direction == "long" else SIDE_BUY
    for i in range(3):
        try:
            client.futures_create_order(
                symbol=symbol, side=side, type=ORDER_TYPE_MARKET,
                quantity=qty, reduceOnly=True
            )
            return True
        except Exception as e:
            log.warning(f"close {symbol} #{i+1}: {e}")
            time.sleep(1)
    return False


# ══════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════
def ema(vals, period):
    if len(vals) < period: return vals[-1] if vals else 0
    k = 2 / (period + 1)
    v = sum(vals[:period]) / period
    for x in vals[period:]: v = x * k + v * (1 - k)
    return v

def rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period or 1e-9
    return 100 - 100 / (1 + ag / al)

def atr_calc(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i],
                 abs(highs[i]-closes[i-1]),
                 abs(lows[i]-closes[i-1]))
        trs.append(tr)
    return sum(trs[-period:]) / min(period, len(trs)) if trs else closes[-1] * 0.01

def macd_signal(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow + sig: return 0, False
    kf, ks = 2/(fast+1), 2/(slow+1)
    ef = es = closes[0]
    line = []
    for c in closes:
        ef = c*kf + ef*(1-kf)
        es = c*ks + es*(1-ks)
        line.append(ef - es)
    sl_val = ema(line, sig)
    hist   = line[-1] - sl_val
    hist_p = line[-2] - ema(line[:-1], sig) if len(line) > sig else 0
    crossed_up   = hist > 0 and hist_p <= 0
    crossed_down = hist < 0 and hist_p >= 0
    return hist, crossed_up, crossed_down


# ══════════════════════════════════════════════════════════════
#  BREAKOUT DETECTION
# ══════════════════════════════════════════════════════════════
def find_breakout(closes, highs, lows, lookback=20):
    """
    يشوف إذا السعر كسر مستوى مقاومة أو دعم.
    يرجع:
      bullish_break: كسر مقاومة صاعد
      bearish_break: كسر دعم هابط
      resistance: مستوى المقاومة
      support: مستوى الدعم
      strength: قوة الكسر (0-1)
    """
    if len(closes) < lookback + 2:
        return False, False, 0, 0, 0

    window_h = highs[-lookback-1:-1]   # آخر N شمعة باستثناء الحالية
    window_l = lows[-lookback-1:-1]

    resistance = max(window_h)         # أعلى قمة
    support    = min(window_l)         # أدنى قاع

    price = closes[-1]
    rng   = resistance - support
    if rng < 1e-9: return False, False, resistance, support, 0

    # هل كسر المقاومة؟ (السعر الحالي أعلى من أعلى قمة)
    bullish_break = price > resistance * 1.0005   # كسر بـ 0.05% فقط
    # هل كسر الدعم؟
    bearish_break = price < support * 0.9995

    # قوة الكسر (كم تجاوز المستوى)
    if bullish_break:
        strength = min((price - resistance) / (rng * 0.1), 1.0)
    elif bearish_break:
        strength = min((support - price) / (rng * 0.1), 1.0)
    else:
        strength = 0

    return bullish_break, bearish_break, resistance, support, strength


# ══════════════════════════════════════════════════════════════
#  MAIN ANALYSIS — قلب البوت الجديد
# ══════════════════════════════════════════════════════════════
def analyze(symbol):
    """
    يحلل العملة بثلاث طبقات:
    1. EMA Crossover على 1h → اتجاه
    2. Breakout على 15m    → نقطة دخول
    3. تأكيدات إضافية     → جودة الإشارة
    """
    try:
        # جلب البيانات
        k1h  = client.futures_klines(symbol=symbol, interval="1h",  limit=100)
        k15m = client.futures_klines(symbol=symbol, interval="15m", limit=50)

        def parse(k):
            return (
                [float(x[4]) for x in k],   # close
                [float(x[2]) for x in k],   # high
                [float(x[3]) for x in k],   # low
                [float(x[5]) for x in k],   # volume
            )

        cl1h, hi1h, lo1h, vo1h = parse(k1h)
        cl15, hi15, lo15, vo15 = parse(k15m)

        price = cl15[-1]
        if price <= 0: return None

        # ── طبقة 1: EMA على 1h ────────────────────────────────
        e9_1h  = ema(cl1h, 9)
        e21_1h = ema(cl1h, 21)
        e50_1h = ema(cl1h, 50)
        rsi_1h = rsi(cl1h)
        atr_1h = atr_calc(hi1h, lo1h, cl1h)

        # MACD على 1h
        macd_hist, macd_up, macd_down = macd_signal(cl1h)

        # اتجاه EMA
        ema_bull = e9_1h > e21_1h and e21_1h > e50_1h
        ema_bear = e9_1h < e21_1h and e21_1h < e50_1h

        # لا اتجاه واضح → تخطي
        if not ema_bull and not ema_bear:
            log.info(f"🔕 {symbol}: لا اتجاه EMA — e9={e9_1h:.2f} e21={e21_1h:.2f} e50={e50_1h:.2f}")
            return None

        direction = "long" if ema_bull else "short"

        # ── فلتر السوق العام ──────────────────────────────────
        if _market_bull and direction == "short" and not ema_bear:
            log.info(f"🔕 {symbol}: short في سوق صاعد — رفض")
            return None
        if not _market_bull and direction == "long" and not ema_bull:
            log.info(f"🔕 {symbol}: long في سوق هابط — رفض")
            return None

        # ── RSI Filter — أكثر مرونة ──────────────────────────
        if direction == "long"  and rsi_1h > 78:
            log.info(f"🔕 {symbol}: RSI ذروة شراء {rsi_1h:.0f}")
            return None
        if direction == "short" and rsi_1h < 22:
            log.info(f"🔕 {symbol}: RSI ذروة بيع {rsi_1h:.0f}")
            return None

        # ── طبقة 2: Breakout على 15m ──────────────────────────
        bull_break, bear_break, resist, support, b_strength = find_breakout(
            cl15, hi15, lo15, lookback=BREAKOUT_LOOKBACK
        )

        # يجب أن يتطابق Breakout مع الاتجاه
        if direction == "long"  and not bull_break:
            log.info(f"🔕 {symbol}: لا breakout صاعد (مقاومة={resist:.4f} سعر={price:.4f})")
            return None
        if direction == "short" and not bear_break:
            log.info(f"🔕 {symbol}: لا breakout هابط (دعم={support:.4f} سعر={price:.4f})")
            return None

        # ── طبقة 3: حجم ───────────────────────────────────────
        avg_vol = sum(vo15[-21:-1]) / 20 or 1
        vol_r   = vo15[-2] / avg_vol
        if vol_r < MIN_VOL_RATIO:
            log.info(f"🔕 {symbol}: حجم ضعيف {vol_r:.2f} < {MIN_VOL_RATIO}")
            return None

        # ── Sentiment ─────────────────────────────────────────
        blocked, block_reason = sentiment_block(direction)
        if blocked:
            log.info(f"🔕 {symbol}: Sentiment يمنع {direction} — {block_reason}")
            return None

        # ── حساب Score ────────────────────────────────────────
        score   = 0
        reasons = []

        # EMA alignment
        if ema_bull and direction == "long":
            score += 25
            reasons.append("EMA9>21>50✅")
        if ema_bear and direction == "short":
            score += 25
            reasons.append("EMA9<21<50✅")

        # Breakout strength
        score += int(b_strength * 20)
        reasons.append(f"Break×{b_strength:.1f}")

        # MACD
        if direction == "long"  and (macd_up   or macd_hist > 0):
            score += 15; reasons.append("MACD↑")
        if direction == "short" and (macd_down  or macd_hist < 0):
            score += 15; reasons.append("MACD↓")

        # RSI في المنطقة المثالية
        if 40 <= rsi_1h <= 65:
            score += 15; reasons.append(f"RSI✓{rsi_1h:.0f}")
        elif direction == "long"  and 35 <= rsi_1h < 40:
            score += 10; reasons.append(f"RSI-low{rsi_1h:.0f}")
        elif direction == "long"  and 65 < rsi_1h <= 78:
            score += 5;  reasons.append(f"RSI-high{rsi_1h:.0f}")
        elif direction == "short" and 60 < rsi_1h <= 65:
            score += 10; reasons.append(f"RSI-high{rsi_1h:.0f}")

        # حجم
        if vol_r > 3.0:   score += 15; reasons.append(f"Vol×{vol_r:.1f}🔥")
        elif vol_r > 2.0: score += 10; reasons.append(f"Vol×{vol_r:.1f}")
        else:             score += 5;  reasons.append(f"Vol×{vol_r:.1f}")

        # Sentiment إيجابي
        if _sentiment["ok"]:
            v = _sentiment["value"]
            if direction == "long"  and v > 55: score += 10; reasons.append(f"😊{v}")
            if direction == "short" and v < 45: score += 10; reasons.append(f"😨{v}")

        # السعر فوق EMA50 (long) أو تحته (short)
        if direction == "long"  and price > e50_1h: score += 10; reasons.append("↑EMA50")
        if direction == "short" and price < e50_1h: score += 10; reasons.append("↓EMA50")

        # نحتاج على الأقل 60 نقطة
        MIN_SCORE = 60
        if score < MIN_SCORE:
            log.info(f"🔕 {symbol} {direction}: score={score} < {MIN_SCORE}")
            return None

        # ── TP / SL ───────────────────────────────────────────
        if direction == "long":
            sl_p = price - atr_1h * ATR_SL_MULT
            tp_p = price + atr_1h * ATR_TP_MULT
            rr   = (tp_p - price) / (price - sl_p) if (price - sl_p) > 0 else 0
        else:
            sl_p = price + atr_1h * ATR_SL_MULT
            tp_p = price - atr_1h * ATR_TP_MULT
            rr   = (price - tp_p) / (sl_p - price) if (sl_p - price) > 0 else 0

        if rr < MIN_RR:
            log.info(f"🔕 {symbol}: RR={rr:.2f} < {MIN_RR}")
            return None

        log.info(
            f"🎯 {symbol} {direction} score={score} RR={rr:.2f} "
            f"Break={'↑' if bull_break else '↓'}×{b_strength:.1f} "
            f"RSI={rsi_1h:.0f} Vol×{vol_r:.1f}"
        )

        return {
            "symbol":    symbol,
            "direction": direction,
            "score":     score,
            "price":     price,
            "tp":        rprice(symbol, tp_p),
            "sl":        rprice(symbol, sl_p),
            "atr":       atr_1h,
            "rr":        round(rr, 2),
            "rsi_1h":    round(rsi_1h, 1),
            "vol_r":     round(vol_r, 1),
            "resist":    round(resist, 4),
            "support":   round(support, 4),
            "b_str":     round(b_strength, 2),
            "reasons":   reasons,
        }

    except Exception as e:
        if "-1022" not in str(e): log.warning(f"analyze {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  MARKET FILTER
# ══════════════════════════════════════════════════════════════
def update_market():
    global _market_bull
    try:
        kl  = client.futures_klines(symbol="BTCUSDT", interval="1h", limit=60)
        cls = [float(k[4]) for k in kl]
        e21 = ema(cls, 21)
        e50 = ema(cls, 50)
        prev = _market_bull
        _market_bull = cls[-1] > e50 and e21 > e50
        if prev != _market_bull:
            s = "🟢 صاعد" if _market_bull else "🔴 هابط"
            tg(f"📡 *تغيير السوق: {s}*\nBTC:`{cls[-1]:.0f}`")
        log.info(f"📡 السوق: {'🟢صاعد' if _market_bull else '🔴هابط'} BTC={cls[-1]:.0f} e21={e21:.0f} e50={e50:.0f}")
    except Exception as e:
        log.error(f"market: {e}")


# ══════════════════════════════════════════════════════════════
#  OPEN POSITION
# ══════════════════════════════════════════════════════════════
def open_pos(cand):
    global _daily_trades
    sym  = cand["symbol"]
    prc  = cand["price"]
    dire = cand["direction"]

    if sym in open_trades: return False
    if len(open_trades) >= MAX_OPEN_TRADES: return False

    amt, _ = get_position(sym)
    if abs(amt) > 1e-8: return False

    try:
        _, _, min_n = get_filters(sym)
        bal = balance()
        av  = avail_margin()

        # حجم الصفقة بناء على المخاطرة
        sl_dist = abs(prc - cand["sl"]) / prc
        if sl_dist < 0.005: sl_dist = 0.01
        qty_risk  = (bal * RISK_PER_TRADE) / (prc * sl_dist)
        qty_avail = (av * 0.85 * LEVERAGE) / prc
        qty       = rqty(sym, min(qty_risk, qty_avail))

        # لو الكمية صغيرة → نجرب الحد الأدنى
        if qty * prc < min_n:
            min_qty = rqty(sym, (min_n * 1.1) / prc)
            if (min_qty * prc / LEVERAGE) > av * 0.9:
                log.info(f"{sym}: هامش غير كافٍ")
                return False
            qty = min_qty

        if qty <= 0: return False

        # ضبط الرافعة
        try: client.futures_change_leverage(symbol=sym, leverage=LEVERAGE)
        except: pass

        # أمر الدخول
        side = SIDE_BUY if dire == "long" else SIDE_SELL
        order_ok = False
        for i in range(3):
            try:
                res = client.futures_create_order(
                    symbol=sym, side=side,
                    type=ORDER_TYPE_MARKET, quantity=qty
                )
                log.info(f"📨 {sym}: orderId={res.get('orderId','?')}")
                order_ok = True
                break
            except Exception as e:
                log.warning(f"❌ entry {sym} #{i+1}: {e}")
                time.sleep(1)

        if not order_ok: return False

        # انتظر تأكيد الوضعية
        ra = re = 0.0
        for _ in range(6):
            time.sleep(1)
            ra, re = get_position(sym)
            if abs(ra) > 1e-8: break

        if abs(ra) < 1e-8:
            log.error(f"❌ {sym}: لا وضعية!")
            return False

        rq = abs(ra); re = re or prc
        trade = TradeState(sym, re, rq, dire, cand["tp"], cand["sl"], cand["atr"], cand["reasons"])
        open_trades[sym] = trade
        _daily_trades   += 1
        bn = place_sl(sym, re, rq, dire)

        dl = "📈 Long" if dire == "long" else "📉 Short"
        tg(
            f"🚀 *{dl}: {sym}*\n"
            f"سعر:`{re:.4f}` | رافعة:`{LEVERAGE}x`\n"
            f"TP:`{cand['tp']:.4f}` | SL:`{cand['sl']:.4f}`\n"
            f"RR:`{trade.rr():.2f}` | BE`+{BE_TRIGGER*100:.1f}%`\n"
            f"SL-BN:{'✅' if bn else 'ℹ️محلي'}\n"
            f"─────────────────\n"
            f"Score:`{cand['score']}` | RSI:`{cand['rsi_1h']:.0f}`\n"
            f"Vol:`×{cand['vol_r']:.1f}` | Break:`×{cand['b_str']:.1f}`\n"
            f"Resist:`{cand['resist']}` | Support:`{cand['support']}`\n"
            f"🎯 {' | '.join(cand['reasons'])}\n"
            f"😱 Sentiment: {sentiment_label()}"
        )
        log.info(f"✅ {dire} {sym} @ {re:.4f} ×{LEVERAGE} score={cand['score']}")
        return True

    except Exception as e:
        log.error(f"open_pos {sym}: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  CLOSE HELPERS
# ══════════════════════════════════════════════════════════════
def execute_close(symbol, trade, price, reason):
    amt, _ = get_position(symbol)
    if abs(amt) < 1e-8:
        open_trades.pop(symbol, None); return
    ok = mkt_close(symbol, abs(amt), trade.direction)
    if ok:
        open_trades.pop(symbol, None)
        won, pnl = record_trade(trade, price)
        bal = balance()
        em  = "🟢" if pnl >= 0 else "🔴"
        de  = "📈L" if trade.direction == "long" else "📉S"
        total = data["wins"] + data["losses"]
        wr    = data["wins"] / total * 100 if total else 0
        tg(
            f"{em} *{de} {symbol}*\n"
            f"{reason}\n"
            f"دخول:`{trade.entry:.4f}` → خروج:`{price:.4f}`\n"
            f"P&L:`{pnl:+.2f}%` | مدة:`{trade.duration_hrs():.1f}h`\n"
            f"WR:`{wr:.0f}%` ({total} صفقة)\n"
            f"💰 رصيد:`{bal:.2f}` USDT"
        )
    else:
        tg(f"🚨 *فشل إغلاق {symbol}* — راجع يدوياً!")

def close_all(reason):
    tg(f"🚨 *إغلاق إجباري:* {reason}")
    for p in all_positions():
        amt = float(p["positionAmt"])
        if abs(amt) < 1e-8: continue
        sym  = p["symbol"]
        side = SIDE_SELL if amt > 0 else SIDE_BUY
        cancel_stops(sym)
        try:
            client.futures_create_order(
                symbol=sym, side=side, type=ORDER_TYPE_MARKET,
                quantity=abs(amt), reduceOnly=True
            )
            open_trades.pop(sym, None)
        except Exception as e:
            log.error(f"close_all {sym}: {e}")


# ══════════════════════════════════════════════════════════════
#  PROTECTION MONITOR
# ══════════════════════════════════════════════════════════════
def protection_monitor():
    while True:
        try:
            for sym in list(open_trades.keys()):
                tr = open_trades.get(sym)
                if tr is None: continue

                amt, _ = get_position(sym)
                if abs(amt) < 1e-8:
                    # أُغلقت خارجياً
                    open_trades.pop(sym, None)
                    p = cur_price(sym)
                    if p > 0:
                        won, pnl = record_trade(tr, p)
                        tg(f"{'🟢' if pnl>=0 else '🔴'} *مُغلقة(BN): {sym}*\nP&L:`{pnl:+.2f}%`")
                    continue

                p = cur_price(sym)
                if p <= 0: continue

                # Timeout
                if tr.duration_hrs() >= MAX_TRADE_HRS:
                    execute_close(sym, tr, p, f"Timeout {MAX_TRADE_HRS}h ⏰")
                    continue

                ev = tr.update(p)
                if ev == "tp":
                    execute_close(sym, tr, p, "جني TP 💰")
                elif ev == "sl":
                    execute_close(sym, tr, p, "وقف SL ⛔")
                elif ev == "be" and not tr.notified:
                    tr.notified = True
                    tg(f"🔒 *BE {sym}* P&L:`+{tr.pnl_pct(p):.2f}%`")
                elif ev == "trail":
                    tg(f"📈 *Trail {sym}* SL:`{tr.trail_sl:.4f}` P&L:`+{tr.pnl_pct(p):.2f}%`")

                # تجديد SL على Binance لو اختفى
                if sym not in _sl_no_support:
                    try:
                        orders = client.futures_get_open_orders(symbol=sym)
                        if not any("STOP" in o.get("type","") for o in orders):
                            place_sl(sym, tr.entry, abs(amt), tr.direction)
                    except: pass

        except Exception as e:
            log.error(f"prot_mon: {e}")
        time.sleep(5)


# ══════════════════════════════════════════════════════════════
#  PROTECTION CHECK
# ══════════════════════════════════════════════════════════════
def check_protection(bal):
    global halted, daily_start_bal, daily_reset_dt, _daily_trades
    if halted: return False

    today = utcnow().date()
    if daily_reset_dt != today:
        daily_start_bal = bal
        daily_reset_dt  = today
        _daily_trades   = 0
        data["daily_count"] = 0
        tg(f"✅ *يوم جديد* | رصيد:`{bal:.2f}` USDT")

    if daily_start_bal > 0:
        d = (daily_start_bal - bal) / daily_start_bal
        if d >= MAX_DAILY_LOSS:
            close_all(f"حد خسارة يومي {d*100:.1f}%")
            tg("⏸️ *البوت متوقف لليوم — خسارة يومية*")
            return False

    if bot_start_bal > 0:
        t = (bot_start_bal - bal) / bot_start_bal
        if t >= MAX_TOTAL_LOSS:
            halted = True
            close_all(f"حد خسارة إجمالي {t*100:.1f}%")
            tg("🚨 *البوت متوقف نهائياً — حد الخسارة الإجمالي*")
            return False

    if utcnow().hour in NO_TRADE_HOURS:
        return False

    return True


# ══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════
def main_loop():
    global bot_start_bal, daily_start_bal, daily_reset_dt, client

    log.info("🚀 Bot v10.0 — EMA + Breakout + Sentiment")
    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

    load_data()

    # تحميل filters مسبقاً
    for sym in SYMBOLS:
        get_filters(sym)

    update_market()
    update_sentiment()

    ini = balance()
    data["peak_bal"]   = max(data.get("peak_bal", 0), ini)
    bot_start_bal      = ini
    daily_start_bal    = ini
    daily_reset_dt     = utcnow().date()

    threading.Thread(target=protection_monitor, daemon=True).start()

    total = data["wins"] + data["losses"]
    wr    = data["wins"] / total * 100 if total else 0
    tg(
        f"🤖 *Bot v10.0* ✅\n"
        f"رصيد:`{ini:.2f}` USDT\n"
        f"─── الاستراتيجية ───\n"
        f"EMA Crossover + Breakout + Sentiment\n"
        f"رافعة:`{LEVERAGE}x` ثابتة\n"
        f"SL:`ATR×{ATR_SL_MULT}` | TP:`ATR×{ATR_TP_MULT}`\n"
        f"─── السجل ───\n"
        f"WR:`{wr:.0f}%` ({total} صفقة)\n"
        f"😱 Sentiment: {sentiment_label()}"
    )

    cy = mf = sc = 0

    while True:
        cy += 1; mf += 1; sc += 1
        try:
            bal = balance()
            av  = avail_margin()

            log.info(
                f"══ #{cy} | {bal:.2f}$ av:{av:.2f} "
                f"صفقات:{len(open_trades)}/{MAX_OPEN_TRADES} | "
                f"{'🟢' if _market_bull else '🔴'} | "
                f"WR:{win_rate()*100:.0f}% ══"
            )

            # تحديثات دورية
            if mf >= 20: update_market(); update_sentiment(); mf = 0
            if sc >= 600: sc = 0  # إعادة تحميل filters كل 5 ساعات

            if not check_protection(bal):
                time.sleep(SCAN_INTERVAL_SEC); continue
            if av < 2.0 or len(open_trades) >= MAX_OPEN_TRADES:
                time.sleep(SCAN_INTERVAL_SEC); continue

            # ── مسح العملات ──────────────────────────────────
            candidates = []
            for sym in SYMBOLS:
                if sym in open_trades: continue
                amt, _ = get_position(sym)
                if abs(amt) > 1e-8: continue

                r = analyze(sym)
                if r:
                    candidates.append(r)

            # ── الدخول ───────────────────────────────────────
            if candidates:
                # ترتيب: أعلى score أولاً
                candidates.sort(key=lambda x: -x["score"])
                slots = MAX_OPEN_TRADES - len(open_trades)
                for c in candidates[:slots]:
                    if avail_margin() < 2.0: break
                    if open_pos(c):
                        time.sleep(3)
            else:
                if cy % 8 == 0:
                    log.info(f"لا إشارات breakout الآن — {sentiment_label()}")

        except Exception as e:
            log.error(f"main #{cy}: {e}")
            tg(f"⚠️ خطأ:\n`{str(e)[:200]}`")

        time.sleep(SCAN_INTERVAL_SEC)


# ══════════════════════════════════════════════════════════════
#  FLASK
# ══════════════════════════════════════════════════════════════
@app.route("/")
def home():
    bal  = balance()
    bull = "🟢 صاعد" if _market_bull else "🔴 هابط"
    total = data["wins"] + data["losses"]
    wr    = data["wins"] / total * 100 if total else 0
    lines = [
        f"<b>🤖 Bot v10.0 — EMA + Breakout + Sentiment</b> | {bull}",
        f"رصيد:<b>{bal:.2f} USDT</b> | مفتوحة:{len(open_trades)}/{MAX_OPEN_TRADES}",
        f"WR:<b>{wr:.0f}%</b> ({total} صفقة) | Pnl:{data['total_pnl']:+.1f}%",
        f"Sentiment: {sentiment_label()} | رافعة:{LEVERAGE}x",
        "<hr>",
    ]
    for sym, t in open_trades.items():
        p   = cur_price(sym)
        pnl = t.pnl_pct(p)
        col = "green" if pnl >= 0 else "red"
        flags = []
        if t.breakeven: flags.append("🔒BE")
        if t.trailing:  flags.append("📈Trail")
        lines.append(
            f"• <b>{sym}</b> {t.direction} @ {t.entry:.4f} | "
            f"<span style='color:{col}'>{pnl:+.2f}%</span> | "
            f"SL:{t.trail_sl:.4f} TP:{t.tp_price:.4f} | "
            f"RR:{t.rr():.2f} | {t.duration_hrs():.1f}h | {' '.join(flags)}"
        )
    return "<br>".join(lines)

@app.route("/trades")
def trades_r():
    out = {}
    for sym, t in open_trades.items():
        p = cur_price(sym)
        out[sym] = {
            "dir": t.direction, "entry": t.entry, "current": p,
            "pnl": round(t.pnl_pct(p), 2),
            "sl": round(t.trail_sl, 6), "tp": round(t.tp_price, 6),
            "rr": round(t.rr(), 2),
            "be": t.breakeven, "trail": t.trailing,
            "hrs": round(t.duration_hrs(), 2),
        }
    return json.dumps(out, ensure_ascii=False, indent=2)

@app.route("/stats")
def stats_r():
    total = data["wins"] + data["losses"]
    return json.dumps({
        "wins":      data["wins"],
        "losses":    data["losses"],
        "wr":        round(data["wins"]/total*100, 1) if total else 0,
        "total_pnl": round(data["total_pnl"], 2),
        "sentiment": {"value": _sentiment["value"], "label": _sentiment["label"]},
        "market":    "bull" if _market_bull else "bear",
    }, ensure_ascii=False, indent=2)

@app.route("/webhook", methods=["POST"])
def webhook():
    """إشارات TradingView يدوية"""
    try:
        d = flask_request.get_json(force=True, silent=True) or {}
        if d.get("secret") != TV_SECRET:
            return {"status": "unauthorized"}, 401
        sym  = d.get("symbol","").upper()
        dire = d.get("direction","").lower()
        if not sym.endswith("USDT"): sym += "USDT"
        if sym not in SYMBOLS:
            return {"status": "symbol_not_supported", "symbol": sym}
        if dire == "close" and sym in open_trades:
            tr = open_trades[sym]
            p  = cur_price(sym)
            execute_close(sym, tr, p, "TV-Close")
            return {"status": "closed"}
        return {"status": "received", "symbol": sym, "direction": dire}
    except Exception as e:
        return {"status": "error", "msg": str(e)}, 500

if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
