"""
=============================================================
  SMART TRADING BOT v9.3  — SPOT + FUTURES EDITION
  ─────────────────────────────────────────────
  ✅ Futures Trading مع رافعة ذكية
  ✅ Spot Trading (شراء فوري بدون رافعة)
  ✅ تحليل محسّن مرتبط بحالة السوق العام
  ✅ قائمة سوداء للعملات المتذبذبة
  ✅ Polymarket + Fibonacci + Multi-TF
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

# ─── TradingView Webhook ──────────────────────────────────────
TV_SECRET         = os.getenv("TV_SECRET", "my_secret_123")
TV_SIGNAL_TTL_SEC = 180

TV_MODE           = "hybrid"
TV_FALLBACK_MIN   = 10
_tv_last_signal_t = None

# ─── Spot Trading ─────────────────────────────────────────────
SPOT_ENABLED      = True         # ← تفعيل Spot
SPOT_BUDGET_PCT   = 0.30         # 30% من الرصيد للـ Spot
SPOT_MIN_SCORE    = 70           # score أعلى للـ Spot (أكثر أماناً)
SPOT_MAX_TRADES   = 2            # صفقتان Spot كحد أقصى
spot_trades: dict = {}           # {symbol: {"entry":x,"qty":y,"tp":z}}

# ─── عملات ───────────────────────────────────────────────────
SYMBOLS: list = []
MIN_VOLUME_24H   = 300_000_000
MIN_TRADES_24H   = 80_000
MAX_SYMBOLS      = 25
EXCLUDE_SYMBOLS  = {
    "USDCUSDT","BUSDUSDT","TUSDUSDT","USDTUSDT","DAIUSDT",
    "FDUSDUSDT","XAUUSDT","XAGUSDT","BTCDOMUSDT","DEFIUSDT",
}
GUARANTEED = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","BNBUSDT",
    "DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT",
]

# ─── إعدادات التداول ──────────────────────────────────────────
MAX_OPEN_TRADES   = 4
SCAN_INTERVAL_SEC = 30

# ─── الأطر الزمنية ────────────────────────────────────────────
TF_TREND  = "1h"
TF_ENTRY  = "15m"
TF_CONFIRM= "5m"

# ─── الرافعة ─────────────────────────────────────────────────
LEVERAGE_STRONG = 10             # score >= 80  ← آمن مع عملات صغيرة
LEVERAGE_NORMAL = 7              # score 65-79
LEVERAGE_WEAK   = 5              # score 55-64

# ─── TP / SL ─────────────────────────────────────────────────
ATR_TP_MULT    = 2.5
ATR_SL_MULT    = 1.0             # ← ضيّقنا SL لحماية أسرع
MIN_RR         = 2.0             # ← رفعنا RR لجودة أعلى
MAX_TRADE_HRS  = 8               # ← أقصر مدة للصفقة

# ─── Breakeven / Trailing ─────────────────────────────────────
BE_PCT         = 0.005           # ← breakeven أسرع
TRAIL_START    = 0.008
TRAIL_STEP     = 0.003

# ─── إدارة المخاطر ────────────────────────────────────────────
BASE_RISK      = 0.010           # 1% لكل صفقة
MIN_RISK       = 0.006
MAX_RISK       = 0.015
RISK_WIN_STEP  = 0.001
RISK_LOSS_STEP = 0.003

# ─── حماية الرصيد ─────────────────────────────────────────────
DAILY_LOSS_LIM = 0.05            # 5%
TOTAL_LOSS_LIM = 0.12            # 12%
MAX_DAILY_TR   = 10
CONSEC_LOSS_ST = 2               # ← راحة بعد خسارتين
PAUSE_AFTER_LOSS_MIN = 30        # ← راحة أطول

# ─── شروط الدخول ─────────────────────────────────────────────
MIN_SCORE      = 65              # ← رُفع لجودة أعلى

# ──────────────────────────────────────────────────────────────
# 🔧 إصلاح #1: Polymarket — منطق آمن مع fallback
# ──────────────────────────────────────────────────────────────
POLY_ENABLED        = True
POLY_BEAR_THRESHOLD = 0.60
POLY_BULL_THRESHOLD = 0.60
POLY_CACHE_MIN      = 15
POLY_URL            = "https://clob.polymarket.com/markets"

# ─── ساعات راحة (UTC) ─────────────────────────────────────────
NO_TRADE_HOURS = {2, 3, 4}

# ─── عملات محظورة من التداول (صغيرة ومتذبذبة) ──────────────────
BLACKLIST_SYMBOLS = {
    "FILUSDT", "CLUSDT", "LABUSDT", "SKYAIUSDT",
    "BZUSDT", "RAVEUSDT", "BSBUSDT", "TAOUSDT",
    "IOUSDT", "WIFUSDT", "DOGSUSDT", "TSTUSDT",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot_v9.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

app    = Flask(__name__)
client: Client = None

open_trades:    dict = {}
_filters_cache: dict = {}
_sl_fail_count: dict = {}
MAX_SL_FAIL = 3

bot_start_bal   = 0.0
daily_start_bal = 0.0
daily_reset_dt  = None
halted_total    = False
halted_daily    = False
_last_report_dt = None
_market_bull    = True
_daily_trades   = 0

# ──────────────────────────────────────────────────────────────
# 🔧 إصلاح #1: Polymarket cache — قيم افتراضية محايدة 50/50
#    بدل 40/60 اللي كانت تتسبب في قراءة خاطئة
# ──────────────────────────────────────────────────────────────
_poly_cache = {
    "btc_bear_prob": 0.50,   # ← محايد افتراضياً
    "btc_bull_prob": 0.50,   # ← محايد افتراضياً
    "last_update":   None,
    "fetch_ok":      False,  # هل نجح آخر جلب؟
}

_tv_signals: dict = {}

learning = {
    "trade_history":     [],
    "symbol_stats":      {},
    "win_rate":          0.0,
    "total_trades":      0,
    "profitable_trades": 0,
    "current_risk":      BASE_RISK,
    "consec_wins":       0,
    "consec_losses":     0,
    "peak_balance":      0.0,
    "comp_mult":         1.0,
    "hour_stats":        {},
    "atr_sl_mult":       ATR_SL_MULT,
}


# ══════════════════════════════════════════════════════════════
#  TradeState
# ══════════════════════════════════════════════════════════════
class TradeState:
    def __init__(self, symbol, entry, qty, direction, tp, sl, atr, score, reasons=None):
        self.symbol    = symbol
        self.entry     = entry
        self.qty       = qty
        self.direction = direction
        self.tp_price  = tp
        self.sl_price  = sl
        self.atr       = atr
        self.score     = score
        self.reasons   = reasons or []
        self.open_time = utcnow()
        self.highest   = entry
        self.lowest    = entry
        self.breakeven = False
        self.trailing  = False
        self.trail_sl  = sl
        self.notif_sl  = None

    def leverage(self):
        if self.score >= 80: return LEVERAGE_STRONG
        if self.score >= 65: return LEVERAGE_NORMAL
        return LEVERAGE_WEAK

    def pnl_pct(self, price):
        lev = self.leverage()
        if self.direction == "long":
            return (price - self.entry) / self.entry * 100 * lev
        return (self.entry - price) / self.entry * 100 * lev

    def duration_hrs(self):
        return (utcnow() - self.open_time).total_seconds() / 3600

    def rr(self):
        if self.direction == "long":
            risk = self.entry - self.sl_price
            rew  = self.tp_price - self.entry
        else:
            risk = self.sl_price - self.entry
            rew  = self.entry - self.tp_price
        return rew / risk if risk > 0 else 0

    def update(self, price):
        is_long = self.direction == "long"
        if is_long:
            if price > self.highest: self.highest = price
            pnl = (price - self.entry) / self.entry
            if price >= self.tp_price: return "tp_hit"
            if price <= self.trail_sl: return "sl_hit"
            if pnl >= TRAIL_START:
                nt = self.highest * (1 - TRAIL_STEP)
                if nt > self.trail_sl:
                    self.trail_sl = nt
                    self.trailing = True
                    if self.notif_sl is None or abs(nt - self.notif_sl) / self.entry > 0.003:
                        self.notif_sl = nt
                        return "trailing"
            elif pnl >= BE_PCT and not self.breakeven:
                self.breakeven = True
                self.trail_sl  = self.entry * 1.0003
                return "breakeven"
        else:
            if price < self.lowest: self.lowest = price
            pnl = (self.entry - price) / self.entry
            if price <= self.tp_price: return "tp_hit"
            if price >= self.trail_sl: return "sl_hit"
            if pnl >= TRAIL_START:
                nt = self.lowest * (1 + TRAIL_STEP)
                if nt < self.trail_sl:
                    self.trail_sl = nt
                    self.trailing = True
                    if self.notif_sl is None or abs(nt - self.notif_sl) / self.entry > 0.003:
                        self.notif_sl = nt
                        return "trailing"
            elif pnl >= BE_PCT and not self.breakeven:
                self.breakeven = True
                self.trail_sl  = self.entry * 0.9997
                return "breakeven"
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

def load_learning():
    global learning
    try:
        if os.path.exists(LEARNING_FILE):
            with open(LEARNING_FILE) as f:
                learning.update(json.load(f))
            log.info(f"📚 صفقات:{learning['total_trades']} WR:{learning['win_rate']*100:.1f}%")
    except Exception as e:
        log.error(f"load_learning: {e}")

def save_learning():
    try:
        with open(LEARNING_FILE, "w") as f:
            json.dump(learning, f, ensure_ascii=False, indent=2)
    except: pass

def record_trade(trade, exit_price, balance):
    is_long = trade.direction == "long"
    won  = (exit_price > trade.entry) if is_long else (exit_price < trade.entry)
    pnl  = trade.pnl_pct(exit_price)
    hr   = str(trade.open_time.hour)
    hs   = learning["hour_stats"].setdefault(hr, {"w":0,"l":0})
    if won: hs["w"] += 1
    else:   hs["l"] += 1
    learning["trade_history"].append({
        "sym": trade.symbol, "dir": trade.direction,
        "entry": trade.entry, "exit": exit_price,
        "pnl": round(pnl,2), "won": won,
        "hrs": round(trade.duration_hrs(),1),
        "score": trade.score, "ts": utcnow().isoformat()
    })
    if len(learning["trade_history"]) > 500:
        learning["trade_history"] = learning["trade_history"][-500:]
    st = learning["symbol_stats"].setdefault(trade.symbol, {"w":0,"l":0,"pnl":0.0})
    if won: st["w"] += 1
    else:   st["l"] += 1
    st["pnl"] += pnl
    learning["total_trades"] += 1
    if won: learning["profitable_trades"] += 1
    learning["win_rate"] = learning["profitable_trades"] / learning["total_trades"]
    r = learning["current_risk"]
    if won:
        learning["consec_wins"]   += 1
        learning["consec_losses"]  = 0
        r = min(r + RISK_WIN_STEP, MAX_RISK)
    else:
        learning["consec_losses"] += 1
        learning["consec_wins"]    = 0
        r = max(r - RISK_LOSS_STEP, MIN_RISK)
    learning["current_risk"] = r
    if balance > learning["peak_balance"]: learning["peak_balance"] = balance
    if learning["peak_balance"] > 0 and bot_start_bal > 0:
        g = learning["peak_balance"] / bot_start_bal
        learning["comp_mult"] = max(1.0, min(g, 1.4))
    recent = learning["trade_history"][-20:]
    if len(recent) >= 10:
        lr = sum(1 for t in recent if not t["won"]) / len(recent)
        if lr > 0.55:
            learning["atr_sl_mult"] = min(learning["atr_sl_mult"] * 1.05, 2.0)
        elif lr < 0.30:
            learning["atr_sl_mult"] = max(learning["atr_sl_mult"] * 0.97, 0.8)
    save_learning()
    log.info(f"📊 {trade.symbol} {'✅' if won else '❌'} {pnl:+.2f}% WR:{learning['win_rate']*100:.1f}%")

def effective_risk():
    r = learning["current_risk"]
    if learning["consec_losses"] >= 2: r = MIN_RISK
    return min(r * learning["comp_mult"], MAX_RISK)

def sym_wr(symbol):
    st = learning["symbol_stats"].get(symbol)
    if not st: return 0.5
    tot = st["w"] + st["l"]
    return st["w"] / tot if tot else 0.5

def bad_hour():
    hr = str(utcnow().hour)
    if utcnow().hour in NO_TRADE_HOURS: return True
    hs = learning["hour_stats"].get(hr)
    if not hs: return False
    tot = hs["w"] + hs["l"]
    if tot < 5: return False
    return hs["w"] / tot < 0.35


# ══════════════════════════════════════════════════════════════
#  🔧 POLYMARKET — إصلاح شامل
# ══════════════════════════════════════════════════════════════
def update_polymarket():
    """
    يجلب احتمالية BTC من Polymarket.
    🔧 إصلاح: لو فشل API → يبقى على 50/50 (محايد) بدل Bear=100%
    """
    if not POLY_ENABLED: return
    now  = utcnow()
    last = _poly_cache["last_update"]
    if last and (now - last).total_seconds() < POLY_CACHE_MIN * 60:
        return

    try:
        resp = requests.get(
            POLY_URL,
            params={"tag": "crypto", "active": "true"},
            timeout=8
        )
        if resp.status_code != 200:
            log.warning(f"Polymarket HTTP {resp.status_code} — الإبقاء على القيم الحالية")
            _poly_cache["last_update"] = now  # لا نعيد المحاولة فوراً
            return

        markets = resp.json().get("data", [])
        btc_markets = [
            m for m in markets
            if "bitcoin" in m.get("question","").lower() or "btc" in m.get("question","").lower()
        ]
        if not btc_markets:
            log.warning("Polymarket: لا يوجد market BTC — إبقاء 50/50")
            _poly_cache["last_update"] = now
            return

        bull_prob = None
        for m in btc_markets[:5]:
            tokens = m.get("tokens", [])
            for t in tokens:
                outcome = t.get("outcome","").lower()
                raw_price = t.get("price")
                if raw_price is None: continue
                price = float(raw_price)
                # تجاهل قيم غير منطقية (0 أو 1 تماماً = بيانات خاطئة)
                if price <= 0.01 or price >= 0.99: continue
                if "yes" in outcome or "above" in outcome or "up" in outcome:
                    bull_prob = price
                    break
            if bull_prob is not None: break

        if bull_prob is None:
            log.warning("Polymarket: قيم متطرفة (0% أو 100%) — إبقاء 50/50")
            _poly_cache["last_update"] = now
            _poly_cache["fetch_ok"]    = False
            return

        _poly_cache["btc_bull_prob"] = bull_prob
        _poly_cache["btc_bear_prob"] = 1 - bull_prob
        _poly_cache["last_update"]   = now
        _poly_cache["fetch_ok"]      = True
        log.info(f"🎯 Polymarket: BTC Bull={bull_prob*100:.0f}% Bear={(1-bull_prob)*100:.0f}%")

    except Exception as e:
        log.warning(f"Polymarket error: {e} — إبقاء القيم الحالية")
        _poly_cache["last_update"] = now  # لا نعيد المحاولة فوراً


def poly_score_bonus(direction):
    """
    🔧 إصلاح: لو fetch_ok=False → لا نعطي bonus ولا penalty
    """
    if not _poly_cache["fetch_ok"]:
        return 0, "Poly⚪N/A"

    bull = _poly_cache["btc_bull_prob"]
    bear = _poly_cache["btc_bear_prob"]
    if direction == "long":
        if bull > POLY_BULL_THRESHOLD: return +10, f"Poly🟢{bull*100:.0f}%"
        if bear > POLY_BEAR_THRESHOLD: return -15, f"Poly🔴{bear*100:.0f}%"
    else:
        if bear > POLY_BEAR_THRESHOLD: return +10, f"Poly🔴{bear*100:.0f}%"
        if bull > POLY_BULL_THRESHOLD: return -15, f"Poly🟢{bull*100:.0f}%"
    return 0, ""


def poly_hard_block(direction):
    """
    🔧 إصلاح: لو fetch_ok=False → لا نحجب أي صفقة
    """
    if not _poly_cache["fetch_ok"]:
        return False  # ← لا تحجب لو البيانات غير موثوقة

    bull = _poly_cache["btc_bull_prob"]
    bear = _poly_cache["btc_bear_prob"]
    if direction == "long"  and bear > 0.70: return True
    if direction == "short" and bull > 0.70: return True
    return False


# ══════════════════════════════════════════════════════════════
#  BINANCE HELPERS
# ══════════════════════════════════════════════════════════════
def balance():
    try:
        for b in client.futures_account_balance():
            if b["asset"] == "USDT": return float(b["balance"])
    except Exception as e:
        log.error(f"balance: {e}")
    return 0.0

def avail_margin():
    try: return float(client.futures_account()["availableBalance"])
    except Exception as e:
        log.error(f"margin: {e}")
        return 0.0

def all_positions():
    try: return client.futures_position_information()
    except: return []

def get_position(symbol):
    try:
        for p in client.futures_position_information(symbol=symbol):
            return float(p["positionAmt"]), float(p["entryPrice"])
    except Exception as e:
        if "-1022" not in str(e): log.warning(f"pos {symbol}: {e}")
    return 0.0, 0.0

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
    lot,_,_ = get_filters(symbol)
    if lot <= 0: return round(qty,3)
    prec = max(0, round(-math.log10(lot)))
    return float(f"{qty:.{prec}f}")

def rprice(symbol, price):
    _,tick,_ = get_filters(symbol)
    if tick <= 0: return round(price,4)
    prec = max(0, round(-math.log10(tick)))
    return float(f"{price:.{prec}f}")

def cancel_stops(symbol):
    try:
        for o in client.futures_get_open_orders(symbol=symbol):
            if "STOP" in o.get("type",""):
                try: client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                except: pass
    except: pass

_sl_no_support: set = set()   # عملات لا تدعم STOP_MARKET — لا نحاول مجدداً

def place_sl(symbol, entry, qty, direction):
    # لو العملة في القائمة السوداء → تخطي بصمت
    if symbol in _sl_no_support:
        return False

    fail_key = f"{symbol}_{direction}"
    if _sl_fail_count.get(fail_key,0) >= MAX_SL_FAIL:
        return False

    is_long = direction == "long"
    sl_p = rprice(symbol, entry*(1-0.025) if is_long else entry*(1+0.025))
    side = SIDE_SELL if is_long else SIDE_BUY
    cancel_stops(symbol)
    time.sleep(0.3)

    for working_type in ("MARK_PRICE", "CONTRACT_PRICE"):
        try:
            client.futures_create_order(
                symbol=symbol, side=side, type="STOP_MARKET",
                stopPrice=sl_p, quantity=qty, reduceOnly=True,
                workingType=working_type
            )
            _sl_fail_count[fail_key] = 0
            log.info(f"✅ BN-SL {symbol}={sl_p} [{working_type}]")
            return True
        except Exception as e:
            code = str(e)
            if "-4120" in code or "does not support" in code.lower():
                continue
            else:
                log.error(f"❌ BN-SL {symbol} [{working_type}]: {e}")
                _sl_fail_count[fail_key] = _sl_fail_count.get(fail_key,0) + 1
                return False

    # كلا النوعين فشلا → قائمة سوداء، لا نكرر
    _sl_no_support.add(symbol)
    log.warning(f"⚠️ {symbol}: لا يدعم BN-SL — حماية داخلية فقط (مُضاف للقائمة السوداء)")
    return False

def mkt_close(symbol, qty, direction):
    qty = abs(qty)
    if qty <= 0: return False
    cancel_stops(symbol)
    side = SIDE_SELL if direction == "long" else SIDE_BUY
    for i in range(3):
        try:
            client.futures_create_order(
                symbol=symbol, side=side, type=ORDER_TYPE_MARKET,
                quantity=qty, reduceOnly=True
            )
            log.info(f"✅ إغلاق {symbol} {direction} qty={qty}")
            return True
        except Exception as e:
            log.warning(f"close {symbol} #{i+1}: {e}")
            time.sleep(1)
    return False


# ══════════════════════════════════════════════════════════════
#  TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════
def ema(vals, period):
    if len(vals) < period: return vals[-1] if vals else 0
    k = 2/(period+1)
    v = sum(vals[:period])/period
    for x in vals[period:]: v = x*k + v*(1-k)
    return v

def rsi(closes, period=14):
    if len(closes) < period+1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag = sum(gains[-period:])/period
    al = sum(losses[-period:])/period or 1e-9
    return 100 - 100/(1+ag/al)

def macd(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow+sig: return False, 0, 0
    kf,ks = 2/(fast+1), 2/(slow+1)
    ef=es=closes[0]
    line=[]
    for c in closes:
        ef=c*kf+ef*(1-kf); es=c*ks+es*(1-ks)
        line.append(ef-es)
    sl = ema(line, sig)
    hist = line[-1]-sl
    hist_prev = line[-2]-ema(line[:-1],sig) if len(line)>sig else 0
    bull = line[-1]>sl and hist>hist_prev
    return bull, hist, sl

def atr(highs, lows, closes, period=14):
    trs=[]
    for i in range(1,len(closes)):
        tr=max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    return sum(trs[-period:])/min(period,len(trs)) if trs else closes[-1]*0.01

def bollinger(closes, period=20):
    if len(closes)<period: return 0.5, False, False
    w=closes[-period:]
    mid=sum(w)/period
    std=(sum((x-mid)**2 for x in w)/period)**0.5
    up=mid+2*std; lo=mid-2*std
    width=up-lo or 1e-9
    pct=(closes[-1]-lo)/width
    return pct, closes[-1]<lo*1.005, closes[-1]>up*0.995

def supertrend(highs, lows, closes, period=10, mult=3.0):
    if len(closes)<period+1: return True
    atr_val = atr(highs, lows, closes, period)
    mid = (highs[-1]+lows[-1])/2
    lower = mid - mult*atr_val
    return closes[-1] > lower

def detect_structure(closes, highs, lows):
    if len(closes)<30: return "RANGING"
    e9  = ema(closes,9)
    e21 = ema(closes,21)
    e50 = ema(closes,50)
    diff = abs(e9-e21)/closes[-1]
    if diff < 0.0001: return "RANGING"
    rng = max(highs[-20:])-min(lows[-20:])
    if rng/closes[-1] < 0.002: return "RANGING"
    if e9>e21 and e21>e50 and closes[-1]>e21: return "TRENDING_UP"
    if e9<e21 and e21<e50 and closes[-1]<e21: return "TRENDING_DOWN"
    return "RANGING"


# ══════════════════════════════════════════════════════════════
#  FIBONACCI
# ══════════════════════════════════════════════════════════════
def fibonacci_levels(highs, lows, closes, lookback=50):
    if len(closes) < lookback:
        return {}, False, False, (0.5, 0.0)
    window_h = highs[-lookback:]
    window_l = lows[-lookback:]
    price    = closes[-1]
    swing_high = max(window_h)
    swing_low  = min(window_l)
    rng        = swing_high - swing_low
    if rng < 1e-9:
        return {}, False, False, (0.5, 0.0)
    ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    is_uptrend = closes[-1] > (swing_high + swing_low) / 2
    levels = {}
    if is_uptrend:
        for r in ratios: levels[r] = swing_high - r * rng
    else:
        for r in ratios: levels[r] = swing_low + r * rng
    nearest_ratio = min(ratios, key=lambda r: abs(levels[r] - price))
    nearest_dist  = abs(levels[nearest_ratio] - price) / price
    TOLERANCE = 0.004
    near_any  = nearest_dist < TOLERANCE
    support_ratios    = {0.382, 0.5, 0.618}
    resistance_ratios = {0.236, 0.382, 0.5}
    near_support    = near_any and nearest_ratio in support_ratios    and is_uptrend
    near_resistance = near_any and nearest_ratio in resistance_ratios and not is_uptrend
    return levels, near_support, near_resistance, (nearest_ratio, nearest_dist)


# ══════════════════════════════════════════════════════════════
#  MAIN ANALYSIS
# ══════════════════════════════════════════════════════════════
def analyze(symbol):
    try:
        k1h  = client.futures_klines(symbol=symbol, interval=TF_TREND,   limit=150)
        k15m = client.futures_klines(symbol=symbol, interval=TF_ENTRY,   limit=100)
        k5m  = client.futures_klines(symbol=symbol, interval=TF_CONFIRM, limit=60)

        def parse(k):
            return (
                [float(x[4]) for x in k],
                [float(x[2]) for x in k],
                [float(x[3]) for x in k],
                [float(x[5]) for x in k],
            )

        cl1h,hi1h,lo1h,vo1h = parse(k1h)
        cl15,hi15,lo15,vo15 = parse(k15m)
        cl5, hi5, lo5, vo5  = parse(k5m)

        price = cl15[-1]
        if price <= 0: return None

        e9_1h   = ema(cl1h, 9)
        e21_1h  = ema(cl1h, 21)
        e50_1h  = ema(cl1h, 50)
        e200_1h = ema(cl1h, 200)
        rsi_1h  = rsi(cl1h)
        macd_bull_1h, hist_1h, _ = macd(cl1h)
        atr_1h  = atr(hi1h, lo1h, cl1h)
        bb_pct, bb_low, bb_high  = bollinger(cl1h)
        st_bull_1h = supertrend(hi1h, lo1h, cl1h)
        struct_1h  = detect_structure(cl1h, hi1h, lo1h)

        fib_lvls, fib_sup, fib_res, (fib_ratio, fib_dist) = fibonacci_levels(hi1h, lo1h, cl1h, lookback=60)
        fib_near = fib_dist < 0.004

        e9_15   = ema(cl15, 9)
        e21_15  = ema(cl15, 21)
        rsi_15  = rsi(cl15)
        macd_bull_15, hist_15, _ = macd(cl15)
        atr_15  = atr(hi15, lo15, cl15)
        struct_15 = detect_structure(cl15, hi15, lo15)

        avg_vol = sum(vo15[-21:-1])/20 or 1
        vol_r   = vo15[-2]/avg_vol

        rsi_5   = rsi(cl5)
        e9_5    = ema(cl5, 9)
        e21_5   = ema(cl5, 21)

        if struct_1h == "RANGING" and struct_15 == "RANGING":
            log.info(f"🔕 {symbol}: سوق عرضي — رفض")
            return None
        # 🔧 العملات الكبيرة المضمونة تحصل على تخفيف فلتر الفوليوم
        vol_threshold = 0.4 if symbol in set(GUARANTEED) else 0.6
        if vol_r < vol_threshold:
            log.info(f"🔕 {symbol}: فوليوم {vol_r:.2f} < {vol_threshold} — رفض")
            return None

        direction = None; score = 0; reasons = []

        # 🔧 ربط التحليل بحالة السوق العام
        # لو السوق صاعد → نبحث عن long فقط (إلا لو score short أعلى بكثير)
        # لو السوق هابط → نبحث عن short فقط
        long_ok = (
            struct_1h in ("TRENDING_UP", "RANGING") and
            e9_1h > e21_1h and
            price > e50_1h
        )
        short_ok = (
            struct_1h in ("TRENDING_DOWN", "RANGING") and
            e9_1h < e21_1h and
            price < e50_1h
        )

        # فلتر السوق العام: لو السوق صاعد → لا short إلا في حالة اتجاه هبوطي واضح
        if _market_bull and short_ok and struct_1h != "TRENDING_DOWN":
            short_ok = False   # ← منع short في سوق صاعد عرضي
        if not _market_bull and long_ok and struct_1h != "TRENDING_UP":
            long_ok = False    # ← منع long في سوق هابط عرضي

        if long_ok and not short_ok:      direction = "long"
        elif short_ok and not long_ok:    direction = "short"
        elif long_ok and short_ok:        direction = "long" if rsi_1h < 50 else "short"
        else:
            log.info(f"🔕 {symbol}: لا اتجاه واضح")
            return None

        if direction == "long":
            if struct_1h == "TRENDING_UP":     score+=15; reasons.append("1h↑")
            if e9_1h>e21_1h:                   score+=8;  reasons.append("EMA9>21_1h")
            if price>e200_1h:                  score+=10; reasons.append("↑EMA200")
            if st_bull_1h:                     score+=10; reasons.append("ST🟢")
            if macd_bull_1h:                   score+=8;  reasons.append("MACD1h↑")
            if 40<=rsi_1h<=60:                 score+=12; reasons.append(f"RSI1h✓{rsi_1h:.0f}")
            elif 30<=rsi_1h<40:                score+=18; reasons.append(f"RSI1h-OS{rsi_1h:.0f}")
            elif rsi_1h>70:                    score-=20
            if bb_low:                         score+=12; reasons.append("BB-Low🎯")
            elif bb_pct<0.30:                  score+=6;  reasons.append("BB-low")
            if bb_high:                        score-=10
            if fib_sup:                        score+=15; reasons.append(f"Fib-دعم{fib_ratio*100:.0f}%🟡")
            elif fib_near and fib_ratio in (0.382, 0.5, 0.618): score+=8; reasons.append(f"Fib{fib_ratio*100:.0f}%")
            if fib_res:                        score-=10; reasons.append("Fib-مقاومة⚠️")
            if struct_15=="TRENDING_UP":       score+=10; reasons.append("15m↑")
            if e9_15>e21_15:                   score+=6;  reasons.append("EMA_15↑")
            if macd_bull_15:                   score+=5;  reasons.append("MACD15↑")
            if 40<=rsi_15<=60:                 score+=8;  reasons.append(f"RSI15✓{rsi_15:.0f}")
            elif rsi_15>70:                    score-=10
            if e9_5>e21_5:                     score+=5;  reasons.append("5m↑تأكيد")
            if 40<=rsi_5<=65:                  score+=5;  reasons.append(f"RSI5✓{rsi_5:.0f}")
        else:
            if struct_1h == "TRENDING_DOWN":   score+=15; reasons.append("1h↓")
            if e9_1h<e21_1h:                   score+=8;  reasons.append("EMA9<21_1h")
            if price<e200_1h:                  score+=10; reasons.append("↓EMA200")
            if not st_bull_1h:                 score+=10; reasons.append("ST🔴")
            if not macd_bull_1h:               score+=8;  reasons.append("MACD1h↓")
            if 40<=rsi_1h<=60:                 score+=12; reasons.append(f"RSI1h✓{rsi_1h:.0f}")
            elif rsi_1h>65:                    score+=18; reasons.append(f"RSI1h-OB{rsi_1h:.0f}")
            elif rsi_1h<30:                    score-=20
            if bb_high:                        score+=12; reasons.append("BB-High🎯")
            elif bb_pct>0.70:                  score+=6;  reasons.append("BB-high")
            if bb_low:                         score-=10
            if fib_res:                        score+=15; reasons.append(f"Fib-مقاومة{fib_ratio*100:.0f}%🟡")
            elif fib_near and fib_ratio in (0.236, 0.382, 0.5): score+=8; reasons.append(f"Fib{fib_ratio*100:.0f}%")
            if fib_sup:                        score-=10; reasons.append("Fib-دعم⚠️")
            if struct_15=="TRENDING_DOWN":     score+=10; reasons.append("15m↓")
            if e9_15<e21_15:                   score+=6;  reasons.append("EMA_15↓")
            if not macd_bull_15:               score+=5;  reasons.append("MACD15↓")
            if 40<=rsi_15<=60:                 score+=8;  reasons.append(f"RSI15✓{rsi_15:.0f}")
            elif rsi_15<30:                    score-=10
            if e9_5<e21_5:                     score+=5;  reasons.append("5m↓تأكيد")
            if 35<=rsi_5<=60:                  score+=5;  reasons.append(f"RSI5✓{rsi_5:.0f}")

        if vol_r>2.5:   score+=12; reasons.append(f"Vol×{vol_r:.1f}🔥")
        elif vol_r>1.5: score+=6;  reasons.append(f"Vol×{vol_r:.1f}")

        wr = sym_wr(symbol)
        if wr>0.60:   score+=8;  reasons.append(f"WR{wr*100:.0f}%")
        elif wr<0.35: score-=8

        if POLY_ENABLED:
            bonus, label = poly_score_bonus(direction)
            if bonus != 0:
                score += bonus
                if label: reasons.append(label)
            if poly_hard_block(direction):
                log.info(f"🚫 {symbol}: Polymarket يعاكس {direction} — رفض")
                return None

        if score < MIN_SCORE:
            log.info(f"🔕 {symbol} {direction}: score={score} < {MIN_SCORE}")
            return None

        sl_mult = learning["atr_sl_mult"]
        if direction == "long":
            sl_p = price - atr_1h * sl_mult
            tp_p = price + atr_1h * ATR_TP_MULT
        else:
            sl_p = price + atr_1h * sl_mult
            tp_p = price - atr_1h * ATR_TP_MULT

        if direction == "long":
            rr = (tp_p-price)/(price-sl_p) if (price-sl_p)>0 else 0
        else:
            rr = (price-tp_p)/(sl_p-price) if (sl_p-price)>0 else 0

        if rr < MIN_RR:
            log.info(f"🔕 {symbol}: RR={rr:.2f} < {MIN_RR}")
            return None

        return {
            "symbol":    symbol,
            "direction": direction,
            "score":     score,
            "price":     price,
            "tp":        rprice(symbol, tp_p),
            "sl":        rprice(symbol, sl_p),
            "atr":       atr_1h,
            "rr":        round(rr,2),
            "vol_r":     round(vol_r,2),
            "rsi_1h":    round(rsi_1h,1),
            "rsi_15":    round(rsi_15,1),
            "struct":    struct_1h,
            "reasons":   reasons,
            "fib_ratio": fib_ratio,
            "fib_dist":  round(fib_dist*100,2),
            "fib_near":  fib_near,
            "fib_key_levels": {
                f"{int(r*100)}%": round(v,4)
                for r,v in fib_lvls.items()
                if r in (0.0,0.236,0.382,0.5,0.618,0.786,1.0)
            },
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
        e50 = ema(cls,50)
        prev = _market_bull
        _market_bull = cls[-1] >= e50*0.98
        if prev != _market_bull:
            s = "🟢 صاعد" if _market_bull else "🔴 هابط"
            tg(f"📡 *تغيير السوق: {s}*\nBTC:`{cls[-1]:.0f}` EMA50:`{e50:.0f}`")
    except Exception as e:
        log.error(f"market: {e}")


# ══════════════════════════════════════════════════════════════
#  SYMBOLS LOADER
# ══════════════════════════════════════════════════════════════
def load_symbols():
    global SYMBOLS
    try:
        tickers = client.futures_ticker()
        fil = []
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT"): continue
            if sym in EXCLUDE_SYMBOLS: continue
            base = sym.replace("USDT","")
            if any(x in base for x in ["UP","DOWN","BULL","BEAR","LONG","SHORT"]): continue
            vol = float(t.get("quoteVolume",0))
            cnt = int(t.get("count",0))
            prc = float(t.get("lastPrice",0))
            if vol < MIN_VOLUME_24H: continue
            if cnt < MIN_TRADES_24H: continue
            if prc <= 0: continue
            fil.append((sym,vol))
        fil.sort(key=lambda x:-x[1])
        syms = [s for s,_ in fil[:MAX_SYMBOLS]]
        for g in GUARANTEED:
            if g not in syms: syms.insert(0,g)
        SYMBOLS = syms[:MAX_SYMBOLS]
        log.info(f"📊 عملات ({len(SYMBOLS)}): {' '.join(SYMBOLS[:8])}...")
        for s in SYMBOLS:
            if s not in _filters_cache: get_filters(s)
    except Exception as e:
        log.error(f"load_symbols: {e}")
        if not SYMBOLS: SYMBOLS = GUARANTEED[:10]


# ══════════════════════════════════════════════════════════════
#  OPEN POSITION
# ══════════════════════════════════════════════════════════════
def open_pos(cand):
    global _daily_trades
    sym  = cand["symbol"]
    prc  = cand["price"]
    dire = cand["direction"]
    sc   = cand["score"]
    lev  = LEVERAGE_STRONG if sc>=80 else (LEVERAGE_NORMAL if sc>=65 else LEVERAGE_WEAK)

    amt,_ = get_position(sym)
    if abs(amt)>1e-8 or sym in open_trades: return False
    if len(open_trades)>=MAX_OPEN_TRADES: return False
    if _daily_trades>=MAX_DAILY_TR: return False

    try:
        _,_,min_n = get_filters(sym)
        bal = balance(); av = avail_margin()
        risk = effective_risk()
        sl_dist = abs(prc - cand["sl"]) / prc
        if sl_dist < 0.001: sl_dist = 0.01

        q_risk  = (bal * risk) / (prc * sl_dist)
        q_avail = (av * 0.90 * lev) / prc   # ← رفعنا من 80% إلى 90%

        qty = rqty(sym, min(q_risk, q_avail))

        # 🔧 لو الكمية أقل من الحد الأدنى → نستخدم الحد الأدنى مباشرة
        if qty * prc < min_n:
            min_qty = rqty(sym, (min_n * 1.05) / prc)
            log.info(f"{sym}: qty صغير {qty:.4f} → نجرب الحد الأدنى {min_qty:.4f}")
            # نتحقق أن الهامش يكفي
            needed_margin = (min_qty * prc) / lev
            if needed_margin > av * 0.95:
                log.info(f"{sym}: هامش غير كافٍ ({needed_margin:.2f} > {av*0.95:.2f}) — تخطي")
                return False
            qty = min_qty

        if qty <= 0:
            log.info(f"{sym}: qty=0 — تخطي")
            return False

        log.info(f"📋 {sym}: bal={bal:.2f} av={av:.2f} lev={lev}x qty={qty} price={prc} min_n={min_n}")

        try:
            client.futures_change_leverage(symbol=sym, leverage=lev)
            log.info(f"⚙️ {sym}: رافعة {lev}x مضبوطة")
        except Exception as e:
            log.warning(f"⚙️ {sym}: فشل ضبط الرافعة: {e}")

        side = SIDE_BUY if dire=="long" else SIDE_SELL
        order_ok = False
        for i in range(3):
            try:
                res = client.futures_create_order(
                    symbol=sym, side=side, type=ORDER_TYPE_MARKET, quantity=qty
                )
                log.info(f"📨 {sym}: أمر مُرسل orderId={res.get('orderId','?')}")
                order_ok = True
                break
            except Exception as e:
                log.warning(f"❌ entry {sym} #{i+1}: {e}")
                time.sleep(1)
        if not order_ok:
            log.error(f"❌ {sym}: فشل إرسال الأمر بعد 3 محاولات")
            tg(f"❌ *فشل دخول {sym}*\nراجع السجلات")
            return False

        # انتظر حتى تظهر الوضعية — Binance تحتاج وقتاً أحياناً
        ra, re = 0.0, 0.0
        for attempt in range(6):   # نحاول 6 مرات × 1 ثانية = 6 ثواني
            time.sleep(1.0)
            ra, re = get_position(sym)
            log.info(f"🔍 {sym}: محاولة {attempt+1}/6 amt={ra:.6f} entry={re:.4f}")
            if abs(ra) > 1e-8:
                break
        if abs(ra) < 1e-8:
            log.error(f"❌ {sym}: الأمر نُفّذ (orderId) لكن الوضعية لا تظهر بعد 6 ثواني!")
            tg(f"⚠️ *{sym}*: أمر مُرسل لكن الوضعية غير مؤكدة — تحقق يدوياً")
            return False
        rq = abs(ra); re = re or prc
        trade = TradeState(sym,re,rq,dire,cand["tp"],cand["sl"],cand["atr"],sc,cand["reasons"])
        open_trades[sym] = trade
        _daily_trades   += 1
        _sl_fail_count[f"{sym}_{dire}"] = 0
        bn = place_sl(sym,re,rq,dire)
        dl = "📈 Long" if dire=="long" else "📉 Short"
        fib_str = ""
        if cand.get("fib_near"):
            fib_str = f"Fib `{cand['fib_ratio']*100:.0f}%` (±`{cand['fib_dist']:.2f}%`)\n"
        poly_status = f"Poly:{'✅' if _poly_cache['fetch_ok'] else '⚪N/A'} Bull:{_poly_cache['btc_bull_prob']*100:.0f}%"
        tg(
            f"🚀 *{dl}: {sym}*\n"
            f"سعر:`{re:.4f}` | رافعة:`{lev}x`\n"
            f"TP:`{cand['tp']:.4f}` | SL:`{cand['sl']:.4f}`\n"
            f"RR:`{trade.rr():.2f}` | BE`+{BE_PCT*100:.1f}%`\n"
            f"SL-BN:{'✅' if bn else 'ℹ️ محلي'}\n"
            f"─────────────────\n"
            f"Score:`{sc}` RSI1h:`{cand['rsi_1h']:.0f}` RSI15:`{cand['rsi_15']:.0f}`\n"
            f"Vol:`×{cand['vol_r']:.1f}` | Struct:`{cand['struct']}`\n"
            f"{fib_str}"
            f"🎯 {' | '.join(cand['reasons'][:6])}\n"
            f"💰 {poly_status}"
        )
        log.info(f"✅ {dire} {sym} @ {re:.4f} ×{lev} score={sc}")
        return True
    except Exception as e:
        log.error(f"open_pos {sym}: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  CLOSE HELPERS
# ══════════════════════════════════════════════════════════════
def execute_close(symbol, trade, price, reason):
    amt,_ = get_position(symbol)
    if abs(amt)<1e-8:
        open_trades.pop(symbol,None); return
    ok = mkt_close(symbol,abs(amt),trade.direction)
    if ok:
        open_trades.pop(symbol,None)
        pnl = trade.pnl_pct(price)
        bal = balance()
        record_trade(trade,price,bal)
        em  = "🟢" if pnl>=0 else "🔴"
        de  = "📈L" if trade.direction=="long" else "📉S"
        lb  = {"sl_internal":"وقف ⛔","tp_internal":"جني 💰","timeout":f"Timeout {MAX_TRADE_HRS}h ⏰"}
        tg(
            f"{em} *{de} {symbol}*\n"
            f"{lb.get(reason,reason)}\n"
            f"دخول:`{trade.entry:.4f}` → خروج:`{price:.4f}`\n"
            f"P&L:`{pnl:+.2f}%` | مدة:`{trade.duration_hrs():.1f}h`\n"
            f"Win%:`{learning['win_rate']*100:.1f}%` Risk:`{learning['current_risk']*100:.1f}%`\n"
            f"💰 رصيد:`{bal:.2f}` USDT"
        )
    else:
        tg(f"🚨 *فشل إغلاق {symbol}* — راجع يدوياً!")

def handle_closed_ext(symbol, trade):
    open_trades.pop(symbol,None)
    p = cur_price(symbol)
    if p>0:
        pnl = trade.pnl_pct(p)
        bal = balance()
        record_trade(trade,p,bal)
        em = "🟢" if pnl>=0 else "🔴"
        tg(f"{em} *مُغلقة (BN): {symbol}*\nP&L:`{pnl:+.2f}%` رصيد:`{bal:.2f}`")

def close_all(reason):
    tg(f"🚨 *إغلاق إجباري:* {reason}")
    for p in all_positions():
        amt = float(p["positionAmt"])
        if abs(amt)<1e-8: continue
        sym  = p["symbol"]
        side = SIDE_SELL if amt>0 else SIDE_BUY
        cancel_stops(sym)
        try:
            client.futures_create_order(symbol=sym,side=side,type=ORDER_TYPE_MARKET,quantity=abs(amt),reduceOnly=True)
            open_trades.pop(sym,None)
        except Exception as e:
            log.error(f"close_all {sym}: {e}")

def close_external():
    try:
        for p in all_positions():
            sym = p["symbol"]; amt = float(p["positionAmt"])
            if abs(amt)<1e-8: continue
            if sym in SYMBOLS or sym in open_trades: continue
            side = SIDE_SELL if amt>0 else SIDE_BUY
            cancel_stops(sym)
            try:
                client.futures_create_order(symbol=sym,side=side,type=ORDER_TYPE_MARKET,quantity=abs(amt),reduceOnly=True)
                log.warning(f"🔄 خارجية مُغلقة: {sym}")
                tg(f"🔄 *خارجية مُغلقة: {sym}* qty:`{abs(amt):.4f}`")
            except Exception as e:
                log.error(f"close_ext {sym}: {e}")
    except Exception as e:
        log.error(f"close_external: {e}")


# ══════════════════════════════════════════════════════════════
#  SPOT TRADING
# ══════════════════════════════════════════════════════════════
def spot_balance_usdt():
    try:
        info = client.get_account()
        for b in info["balances"]:
            if b["asset"] == "USDT":
                return float(b["free"])
    except Exception as e:
        log.error(f"spot_balance: {e}")
    return 0.0

def spot_open(cand):
    if not SPOT_ENABLED: return False
    if cand["direction"] != "long": return False
    if cand["score"] < SPOT_MIN_SCORE: return False
    if len(spot_trades) >= SPOT_MAX_TRADES: return False
    sym = cand["symbol"]
    if sym in spot_trades: return False
    try:
        s_bal  = spot_balance_usdt()
        slots  = max(1, SPOT_MAX_TRADES - len(spot_trades))
        budget = (s_bal * SPOT_BUDGET_PCT) / slots
        if budget < 5:
            log.info(f"Spot {sym}: رصيد Spot غير كافٍ ({s_bal:.2f}$)")
            return False
        prc = cand["price"]
        qty_raw = budget / prc
        try:
            info = client.get_symbol_info(sym)
            step = float(next(f["stepSize"] for f in info["filters"] if f["filterType"]=="LOT_SIZE"))
            prec = max(0, round(-math.log10(step)))
            qty  = float(f"{qty_raw:.{prec}f}")
        except:
            qty = round(qty_raw, 4)
        if qty <= 0: return False
        client.order_market_buy(symbol=sym, quantity=qty)
        tp_p = prc * (1 + 0.05)   # TP 5%
        sl_p = prc * (1 - 0.025)  # SL 2.5%
        spot_trades[sym] = {
            "entry": prc, "qty": qty,
            "tp": tp_p, "sl": sl_p,
            "open_time": utcnow(), "score": cand["score"],
        }
        tg(
            f"🛒 *Spot شراء: {sym}*\n"
            f"سعر:`{prc:.4f}` | كمية:`{qty}`\n"
            f"TP:`{tp_p:.4f}` (+5%) | SL:`{sl_p:.4f}` (-2.5%)\n"
            f"Score:`{cand['score']}` | ميزانية:`{budget:.2f}$`"
        )
        log.info(f"🛒 Spot {sym} @ {prc:.4f} qty={qty}")
        return True
    except Exception as e:
        log.error(f"spot_open {sym}: {e}")
        return False

def spot_monitor():
    while True:
        try:
            for sym in list(spot_trades.keys()):
                tr = spot_trades[sym]
                try:
                    price = float(client.get_symbol_ticker(symbol=sym)["price"])
                except:
                    continue
                pnl = (price - tr["entry"]) / tr["entry"] * 100
                hrs = (utcnow() - tr["open_time"]).total_seconds() / 3600
                reason = None
                if price >= tr["tp"]:   reason = "TP جني 💰"
                elif price <= tr["sl"]: reason = "SL وقف ⛔"
                elif hrs >= MAX_TRADE_HRS: reason = f"Timeout ⏰"
                if reason:
                    try:
                        asset = sym.replace("USDT","")
                        ab = client.get_asset_balance(asset=asset)
                        sell_qty = float(ab["free"]) if ab else tr["qty"]
                        if sell_qty > 0:
                            client.order_market_sell(symbol=sym, quantity=sell_qty)
                        spot_trades.pop(sym, None)
                        em = "🟢" if pnl >= 0 else "🔴"
                        tg(f"{em} *Spot {sym}* {reason}\nدخول:`{tr['entry']:.4f}` → خروج:`{price:.4f}`\nP&L:`{pnl:+.2f}%`")
                        log.info(f"🛒 Spot إغلاق {sym} {pnl:+.2f}% [{reason}]")
                    except Exception as e:
                        log.error(f"spot_close {sym}: {e}")
        except Exception as e:
            log.error(f"spot_monitor: {e}")
        time.sleep(10)


# ══════════════════════════════════════════════════════════════
#  PROTECTION MONITOR
# ══════════════════════════════════════════════════════════════
def protection_monitor():
    while True:
        try:
            for sym in list(open_trades.keys()):
                tr = open_trades.get(sym)
                if tr is None: continue
                amt,_ = get_position(sym)
                if abs(amt)<1e-8:
                    handle_closed_ext(sym,tr); continue
                p = cur_price(sym)
                if p<=0: continue
                if tr.duration_hrs()>=MAX_TRADE_HRS:
                    execute_close(sym,tr,p,"timeout"); continue
                ev = tr.update(p)
                if ev=="sl_hit":   execute_close(sym,tr,p,"sl_internal")
                elif ev=="tp_hit": execute_close(sym,tr,p,"tp_internal")
                elif ev=="breakeven":
                    tg(f"🔒 *BE {sym}* {'🟢L' if tr.direction=='long' else '🔴S'}\nP&L:`+{tr.pnl_pct(p):.2f}%`")
                elif ev=="trailing":
                    tg(f"📈 *Trail {sym}*\nSL:`{tr.trail_sl:.4f}` P&L:`+{tr.pnl_pct(p):.2f}%`")
                fail_key=f"{sym}_{tr.direction}"
                # لا نعيد محاولة SL للعملات في القائمة السوداء أو التي تجاوزت الحد
                if sym not in _sl_no_support and _sl_fail_count.get(fail_key,0)<MAX_SL_FAIL:
                    try:
                        orders=client.futures_get_open_orders(symbol=sym)
                        if not any("STOP" in o.get("type","") for o in orders):
                            place_sl(sym,tr.entry,abs(amt),tr.direction)
                    except: pass
        except Exception as e:
            log.error(f"prot_mon: {e}")
        time.sleep(4)


# ══════════════════════════════════════════════════════════════
#  PROTECTION CHECK
# ══════════════════════════════════════════════════════════════
def check_protection(bal):
    global halted_total,halted_daily,daily_start_bal,daily_reset_dt,_daily_trades
    if halted_total: return False
    today = utcnow().date()
    if daily_reset_dt!=today:
        daily_start_bal=bal; daily_reset_dt=today
        halted_daily=False; _daily_trades=0
        tg(f"✅ يوم جديد | رصيد:`{bal:.2f}` USDT")
    if daily_start_bal>0:
        d=(daily_start_bal-bal)/daily_start_bal
        if d>=DAILY_LOSS_LIM:
            if not halted_daily:
                halted_daily=True; close_all(f"خسارة يومية {d*100:.1f}%")
            return False
    if bot_start_bal>0:
        t=(bot_start_bal-bal)/bot_start_bal
        if t>=TOTAL_LOSS_LIM:
            halted_total=True; close_all(f"خسارة إجمالية {t*100:.1f}%")
            tg("🚨 *البوت متوقف نهائياً*"); return False
    if learning["consec_losses"]>=CONSEC_LOSS_ST and not open_trades:
        log.info(f"⛔ خسارتان — راحة {PAUSE_AFTER_LOSS_MIN} دقيقة")
        tg(f"⏸️ *خسارتان متتاليتان — راحة {PAUSE_AFTER_LOSS_MIN} دق*")
        time.sleep(PAUSE_AFTER_LOSS_MIN*60)
        learning["consec_losses"]=0
        return False
    return True

def daily_report(bal):
    global _last_report_dt
    today=utcnow().date()
    if _last_report_dt==today: return
    _last_report_dt=today
    try:
        d=(daily_start_bal-bal)/daily_start_bal*100 if daily_start_bal else 0
        t=(bot_start_bal-bal)/bot_start_bal*100 if bot_start_bal else 0
        poly_st = f"✅ Bull:{_poly_cache['btc_bull_prob']*100:.0f}%" if _poly_cache["fetch_ok"] else "⚪ N/A (محايد)"
        msg  = f"📊 *تقرير {today}*\n"
        msg += f"رصيد:`{bal:.2f}` | اليوم:`{d:.2f}%` | إجمالي:`{t:.2f}%`\n"
        msg += f"Win%:`{learning['win_rate']*100:.1f}%` ({learning['total_trades']} صفقة)\n"
        msg += f"صفقات اليوم:`{_daily_trades}` | Risk:`{learning['current_risk']*100:.1f}%`\n"
        msg += f"🎯 Poly {poly_st}\n"
        bh = sorted(
            [(h,s) for h,s in learning["hour_stats"].items() if s["w"]+s["l"]>=3],
            key=lambda x:x[1]["w"]/(x[1]["w"]+x[1]["l"]), reverse=True
        )[:3]
        if bh:
            msg+="─── أفضل ساعات ───\n"
            for h,s in bh:
                tot=s["w"]+s["l"]
                msg+=f"  {h}:00 WR:{s['w']/tot*100:.0f}% ({tot} صفقة)\n"
        tg(msg)
    except Exception as e:
        log.error(f"report: {e}")


# ══════════════════════════════════════════════════════════════
#  ADOPT EXISTING
# ══════════════════════════════════════════════════════════════
def adopt_existing():
    adopted=0; closed=0
    try:
        for p in all_positions():
            sym=p["symbol"]; amt=float(p["positionAmt"]); entry=float(p["entryPrice"])
            if abs(amt)<1e-8 or entry==0 or sym in open_trades: continue
            dire="long" if amt>0 else "short"
            if sym not in SYMBOLS:
                log.warning(f"⚠️ خارجية: {sym} — إغلاق")
                side=SIDE_SELL if amt>0 else SIDE_BUY
                cancel_stops(sym)
                try:
                    client.futures_create_order(symbol=sym,side=side,type=ORDER_TYPE_MARKET,quantity=abs(amt),reduceOnly=True)
                    closed+=1
                    tg(f"🔄 *خارجية مُغلقة: {sym}* qty:`{abs(amt):.4f}`")
                except Exception as e:
                    log.error(f"close ext {sym}: {e}")
                continue
            atr_v = entry*0.015
            if dire=="long":
                tp=rprice(sym,entry*(1+ATR_TP_MULT*0.01))
                sl=rprice(sym,entry*(1-ATR_SL_MULT*0.01))
            else:
                tp=rprice(sym,entry*(1-ATR_TP_MULT*0.01))
                sl=rprice(sym,entry*(1+ATR_SL_MULT*0.01))
            tr=TradeState(sym,entry,abs(amt),dire,tp,sl,atr_v,65,["موروثة"])
            open_trades[sym]=tr
            _sl_fail_count[f"{sym}_{dire}"]=0
            place_sl(sym,entry,abs(amt),dire)
            adopted+=1
    except Exception as e:
        log.error(f"adopt: {e}")
    msg=f"🔄 *فحص الوضعيات*\nتبنّي:{adopted} | مُغلقة خارجية:{closed}\n"
    for sym,t in open_trades.items():
        msg+=f"  • `{sym}` {t.direction} @ `{t.entry:.4f}`\n"
    if not open_trades and closed==0: msg+="لا وضعيات — جاهز ✅"
    tg(msg)


# ══════════════════════════════════════════════════════════════
#  🔧 MAIN LOOP — وضع هجين
# ══════════════════════════════════════════════════════════════
def main_loop():
    global bot_start_bal,daily_start_bal,daily_reset_dt,client,_market_bull,_tv_last_signal_t

    log.info("🚀 Bot v9.1 — Hybrid TV + Polymarket Fixed")
    client=Client(BINANCE_API_KEY,BINANCE_API_SECRET)
    load_learning()
    load_symbols()
    update_polymarket()

    ini=balance()
    if learning["peak_balance"]==0: learning["peak_balance"]=ini
    bot_start_bal=ini; daily_start_bal=ini; daily_reset_dt=utcnow().date()

    threading.Thread(target=protection_monitor,daemon=True).start()
    if SPOT_ENABLED:
        threading.Thread(target=spot_monitor,daemon=True).start()
        log.info(f"🛒 Spot Trading مُفعّل | ميزانية:{SPOT_BUDGET_PCT*100:.0f}% | Score>{SPOT_MIN_SCORE}")

    poly_st = f"Bull:{_poly_cache['btc_bull_prob']*100:.0f}%" if _poly_cache["fetch_ok"] else "⚪ N/A"
    tg(
        f"🤖 *Bot v9.2* ✅\n"
        f"رصيد:`{ini:.2f}` USDT\n"
        f"عملات:{len(SYMBOLS)} | Swing 1h+15m+5m\n"
        f"─── الوضع ───\n"
        f"TV Mode:`{TV_MODE}` | Fallback:`{TV_FALLBACK_MIN} دق`\n"
        f"─── الرافعة ───\n"
        f"Strong:{LEVERAGE_STRONG}x | Normal:{LEVERAGE_NORMAL}x | Weak:{LEVERAGE_WEAK}x\n"
        f"─── الصفقات ───\n"
        f"حد أقصى:`{MAX_OPEN_TRADES}` | Score:`{MIN_SCORE}+`\n"
        f"─── الحماية ───\n"
        f"BE`+{BE_PCT*100:.1f}%` Trail`+{TRAIL_START*100:.1f}%`\n"
        f"يومي:{DAILY_LOSS_LIM*100:.0f}% | إجمالي:{TOTAL_LOSS_LIM*100:.0f}%\n"
        f"─── Polymarket ───\n"
        f"{poly_st}"
    )

    update_market()
    adopt_existing()

    cy=mf=sc=ec=pc=0

    while True:
        cy+=1; mf+=1; sc+=1; ec+=1; pc+=1
        try:
            bal=balance(); av=avail_margin()
            log.info(
                f"══ #{cy} | {bal:.2f}$ متاح:{av:.2f} "
                f"صفقات:{len(open_trades)}/{MAX_OPEN_TRADES} | "
                f"{'🟢' if _market_bull else '🔴'} "
                f"WR:{learning['win_rate']*100:.1f}% "
                f"Risk:{learning['current_risk']*100:.1f}% ══"
            )
            if mf>=20: update_market(); mf=0
            if sc>=720: load_symbols(); sc=0
            if ec>=8:  close_external(); ec=0
            if pc>=30: update_polymarket(); pc=0

            if not check_protection(bal):
                time.sleep(SCAN_INTERVAL_SEC); continue
            if av<2.0 or len(open_trades)>=MAX_OPEN_TRADES:
                time.sleep(SCAN_INTERVAL_SEC); continue
            if bad_hour():
                if cy%20==0: log.info(f"🌙 ساعة راحة UTC={utcnow().hour}")
                time.sleep(SCAN_INTERVAL_SEC); continue

            # ── تنظيف الإشارات المنتهية ───────────────────────
            now_t = utcnow()
            expired = [s for s,sig in _tv_signals.items()
                       if (now_t-sig["ts"]).total_seconds() > TV_SIGNAL_TTL_SEC]
            for s in expired:
                _tv_signals.pop(s, None)
                log.info(f"⌛ TV Signal {s}: انتهت صلاحيتها")

            # ══ منطق TV_MODE الهجين ════════════════════════════
            now_t = utcnow()
            has_tv_signals = bool(_tv_signals)

            # حساب الوقت منذ آخر إشارة TV
            mins_since_tv = 9999
            if _tv_last_signal_t:
                mins_since_tv = (now_t - _tv_last_signal_t).total_seconds() / 60

            if TV_MODE == "strict":
                # الوضع القديم: ننتظر TV فقط
                if not has_tv_signals:
                    if cy % 20 == 0:
                        log.info("⏳ Strict: لا إشارات TV — ننتظر")
                    time.sleep(SCAN_INTERVAL_SEC); continue
                scan_list  = list(_tv_signals.keys())
                tv_boost   = True   # TV يعزز النقاط لو وُجد

            elif TV_MODE == "hybrid":
                # الوضع الهجين: TV أولوية، بعد X دقيقة → تحليل داخلي
                if has_tv_signals:
                    scan_list = list(_tv_signals.keys())
                    tv_boost  = True
                    log.info(f"📡 Hybrid: إشارات TV {list(_tv_signals.keys())}")
                elif mins_since_tv < TV_FALLBACK_MIN:
                    remaining = TV_FALLBACK_MIN - mins_since_tv
                    if cy % 10 == 0:
                        log.info(f"⏳ Hybrid: انتظار TV fallback — {remaining:.0f} دق متبقية")
                    time.sleep(SCAN_INTERVAL_SEC); continue
                else:
                    # fallback → تحليل داخلي
                    scan_list = SYMBOLS
                    tv_boost  = False
                    if cy % 10 == 0:
                        log.info("🔍 Hybrid-Fallback: تحليل داخلي (لا إشارات TV)")

            else:  # off
                scan_list = SYMBOLS
                tv_boost  = False

            # ── مسح ──────────────────────────────────────────
            candidates = []
            for sym in scan_list:
                if sym not in SYMBOLS: continue
                if sym in open_trades: continue
                if sym in BLACKLIST_SYMBOLS:
                    continue   # عملة محظورة
                amt,_ = get_position(sym)
                if abs(amt) > 1e-8: continue

                r = analyze(sym)
                if not r:
                    if tv_boost and sym in _tv_signals:
                        log.info(f"🔕 {sym}: TV أشار لكن التحليل رفض")
                    continue

                # تأكيد اتجاه TV لو موجود
                if tv_boost and sym in _tv_signals:
                    tv_dir = _tv_signals[sym]["direction"]
                    if r["direction"] != tv_dir:
                        log.info(f"⚡ {sym}: TV={tv_dir} vs تحليل={r['direction']} — تعارض")
                        tg(f"⚡ *تعارض {sym}*\nTV:`{tv_dir}` ↔ تحليل:`{r['direction']}` — رُفض")
                        continue
                    r["score"] += 10
                    r["reasons"].insert(0, f"TV✅{_tv_signals[sym]['tf']}")
                    log.info(f"✅ TV+تحليل: {sym} {tv_dir} score={r['score']}")

                dok = (r["direction"]=="long"  and _market_bull) or \
                      (r["direction"]=="short" and not _market_bull)
                if dok or r["score"] >= 85:
                    candidates.append(r)
                    log.info(f"🎯 {sym} {r['direction']} score={r['score']} RR={r['rr']}")
                else:
                    log.info(f"🚫 {sym}: عكس السوق ({r['direction']})")

            if candidates:
                candidates.sort(key=lambda x: (-x["score"], -x["rr"]))
                log.info(f"🏆 أفضل مرشح: {candidates[0]['symbol']} score={candidates[0]['score']} RR={candidates[0]['rr']}")

                slots = MAX_OPEN_TRADES - len(open_trades)
                if slots <= 0:
                    log.info(f"⛔ وصلنا الحد الأقصى ({MAX_OPEN_TRADES}) — لا دخول")
                elif avail_margin() < 2.0:
                    log.info(f"⛔ هامش غير كافٍ — لا دخول")
                else:
                    opened = 0
                    for c in candidates:
                        if opened >= slots: break
                        if len(open_trades) >= MAX_OPEN_TRADES: break
                        if avail_margin() < 2.0: break
                        if c["symbol"] in open_trades: continue
                        log.info(f"▶️ محاولة فتح {c['symbol']} {c['direction']} score={c['score']}")
                        if open_pos(c):
                            _tv_signals.pop(c["symbol"], None)
                            opened += 1
                            time.sleep(3)
            else:
                if cy % 10 == 0: log.info("لا فرص الآن.")

            # ── Spot Trading ──────────────────────────────────
            if SPOT_ENABLED and _market_bull and len(spot_trades) < SPOT_MAX_TRADES:
                # نأخذ أفضل long candidate لـ Spot
                spot_candidates = [c for c in candidates
                                   if c["direction"] == "long"
                                   and c["score"] >= SPOT_MIN_SCORE
                                   and c["symbol"] not in spot_trades]
                if spot_candidates:
                    best_spot = spot_candidates[0]
                    if spot_open(best_spot):
                        log.info(f"🛒 Spot مفتوح: {best_spot['symbol']}")

            now=utcnow()
            if now.hour==0 and now.minute<1: daily_report(bal)

        except Exception as e:
            log.error(f"main #{cy}: {e}")
            tg(f"⚠️ خطأ:\n`{e}`")
        time.sleep(SCAN_INTERVAL_SEC)


# ══════════════════════════════════════════════════════════════
#  FLASK
# ══════════════════════════════════════════════════════════════
@app.route("/webhook", methods=["POST"])
def tv_webhook():
    """
    ══════════════════════════════════════════════
    🔧 رسالة Webhook الصحيحة في TradingView:
    ══════════════════════════════════════════════
    {
        "secret":    "my_secret_123",
        "symbol":    "{{ticker}}",
        "direction": "long",
        "price":     "{{close}}",
        "tf":        "{{interval}}"
    }

    للـ Short:
    {
        "secret":    "my_secret_123",
        "symbol":    "{{ticker}}",
        "direction": "short",
        "price":     "{{close}}",
        "tf":        "{{interval}}"
    }

    للإغلاق:
    {
        "secret":    "my_secret_123",
        "symbol":    "{{ticker}}",
        "direction": "close",
        "price":     "{{close}}",
        "tf":        "{{interval}}"
    }
    ══════════════════════════════════════════════
    """
    global _tv_last_signal_t
    try:
        data = flask_request.get_json(force=True, silent=True) or {}

        if data.get("secret") != TV_SECRET:
            log.warning(f"⚠️ Webhook: سر خاطئ من {flask_request.remote_addr}")
            return {"status": "unauthorized"}, 401

        sym  = data.get("symbol", "").upper().strip()
        dire = data.get("direction", "").lower().strip()
        tf   = data.get("tf", "?")
        prc  = float(data.get("price", 0) or 0)

        if not sym or dire not in ("long", "short", "close"):
            return {"status": "invalid", "msg": "direction must be long/short/close"}, 400

        if not sym.endswith("USDT"):
            sym += "USDT"

        now = utcnow()
        _tv_last_signal_t = now  # تحديث وقت آخر إشارة

        if dire == "close":
            _tv_signals.pop(sym, None)
            if sym in open_trades:
                trade = open_trades[sym]
                cp    = cur_price(sym)
                execute_close(sym, trade, cp, f"TV-Close({tf})")
                return {"status": "closed", "symbol": sym}
            return {"status": "no_position", "symbol": sym}

        _tv_signals[sym] = {
            "direction": dire,
            "ts":        now,
            "price":     prc,
            "tf":        tf,
        }
        log.info(f"📡 TV Signal: {sym} {dire} @ {prc} TF={tf}")
        tg(f"📡 *TV Signal: {sym}*\n{dire.upper()} @ `{prc}` TF:`{tf}`\n⏳ تحليل...")
        return {"status": "received", "symbol": sym, "direction": dire}

    except Exception as e:
        log.error(f"webhook error: {e}")
        return {"status": "error", "msg": str(e)}, 500


@app.route("/signals")
def signals_r():
    now = utcnow()
    out = {}
    for sym, sig in _tv_signals.items():
        age = (now - sig["ts"]).total_seconds()
        out[sym] = {
            "direction": sig["direction"],
            "price":     sig["price"],
            "tf":        sig["tf"],
            "age_sec":   round(age),
            "valid":     age < TV_SIGNAL_TTL_SEC,
        }
    return json.dumps(out, ensure_ascii=False, indent=2)


@app.route("/")
def home():
    bal  = balance()
    bull = "🟢 صاعد" if _market_bull else "🔴 هابط"
    poly_st = f"✅ Bull:{_poly_cache['btc_bull_prob']*100:.0f}%" if _poly_cache["fetch_ok"] else "⚪ N/A (محايد)"
    lines=[
        f"<b>🤖 Bot v9.1 — Hybrid TV + Poly Fixed</b> | {bull}",
        f"رصيد:<b>{bal:.2f} USDT</b> | مفتوحة:{len(open_trades)}/{MAX_OPEN_TRADES}",
        f"Win%:{learning['win_rate']*100:.1f}% ({learning['total_trades']} صفقة) | يوم:{_daily_trades}/{MAX_DAILY_TR}",
        f"Risk:{learning['current_risk']*100:.1f}% | Comp:×{learning['comp_mult']:.2f}",
        f"TV Mode:<b>{TV_MODE}</b> | Fallback:{TV_FALLBACK_MIN} دق | Poly: {poly_st}",
        f"<b>📡 إشارات TV: {len(_tv_signals)}</b>",
        "<hr>",
    ]
    now_t = utcnow()
    for sym, sig in _tv_signals.items():
        age    = int((now_t - sig["ts"]).total_seconds())
        valid  = age < TV_SIGNAL_TTL_SEC
        status = "🟢 فعالة" if valid else "🔴 منتهية"
        lines.append(f"📡 <b>{sym}</b> {sig['direction'].upper()} TF:{sig['tf']} | {age}s | {status}")
    if _tv_signals: lines.append("<hr>")
    for sym,t in open_trades.items():
        p=cur_price(sym); pnl=t.pnl_pct(p)
        col="green" if pnl>=0 else "red"
        flags=[]
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
    out={}
    for sym,t in open_trades.items():
        p=cur_price(sym)
        out[sym]={"dir":t.direction,"entry":t.entry,"current":p,
                  "pnl":round(t.pnl_pct(p),2),"sl":round(t.trail_sl,6),
                  "tp":round(t.tp_price,6),"rr":round(t.rr(),2),
                  "be":t.breakeven,"trail":t.trailing,"hrs":round(t.duration_hrs(),2)}
    return json.dumps(out,ensure_ascii=False,indent=2)

@app.route("/stats")
def stats_r():
    out={}
    for sym in SYMBOLS:
        st=learning["symbol_stats"].get(sym,{"w":0,"l":0,"pnl":0.0})
        tot=st["w"]+st["l"]
        out[sym]={"wins":st["w"],"losses":st["l"],"wr":round(st["w"]/tot*100,1) if tot else 0,"pnl":round(st["pnl"],2)}
    return json.dumps(out,ensure_ascii=False,indent=2)

@app.route("/fib/<symbol>")
def fib_r(symbol):
    try:
        sym = symbol.upper()
        if not sym.endswith("USDT"): sym += "USDT"
        kl  = client.futures_klines(symbol=sym, interval="1h", limit=100)
        cl  = [float(k[4]) for k in kl]
        hi  = [float(k[2]) for k in kl]
        lo  = [float(k[3]) for k in kl]
        price = cl[-1]
        lvls, sup, res, (ratio, dist) = fibonacci_levels(hi, lo, cl, lookback=60)
        out = {
            "symbol":  sym,
            "price":   price,
            "nearest_fib": f"{ratio*100:.1f}%",
            "distance_pct": round(dist*100, 3),
            "near_support":    sup,
            "near_resistance": res,
            "levels": {f"{int(r*100)}%": round(v,4) for r,v in lvls.items()},
        }
        return json.dumps(out, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})

@app.route("/poly")
def poly_r():
    return json.dumps({**_poly_cache, "last_update": str(_poly_cache["last_update"])},
                      ensure_ascii=False, indent=2)

@app.route("/learning")
def learn_r():
    return json.dumps(
        {k:learning[k] for k in ["win_rate","total_trades","current_risk",
                                  "comp_mult","consec_wins","consec_losses",
                                  "atr_sl_mult","peak_balance"]},
        ensure_ascii=False, indent=2
    )

if __name__=="__main__":
    threading.Thread(target=main_loop,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
