"""
=============================================================
  SMART TRADING BOT v11.0  — Binance USDT-M Futures
  ─────────────────────────────────────────────────
  الاستراتيجية الفعلية (كما ينفّذها الكود، لا كما نتمناه):

  1) اتجاه على 1h  → EMA9 > EMA21 > EMA50 (long) أو العكس (short)
  2) تأكيد على 15m → EMA9/EMA21 بنفس الاتجاه + السعر ليس في طرف النطاق
  3) فلاتر         → RSI، حجم التداول، Fear&Greed، اتجاه BTC العام
  4) نقاط (score)  → لا دخول تحت MIN_SCORE

  ⚠️ لا يوجد Breakout في منطق الدخول. الوصف القديم كان خاطئاً.

  إدارة الصفقة:
  • SL = ATR × 1.5  و TP = ATR × 3.0  → أمران حقيقيان على Binance
  • Breakeven عند +0.8% ثم Trailing عند +1.5% (يتحرك SL على البورصة)
  • استعادة كاملة للصفقات بعد إعادة التشغيل (مصدر الحقيقة = Binance)
  • قياس الربح من futures_income: صافي بعد العمولات والتمويل
  • رافعة ثابتة 5x | حد أقصى 3 صفقات مفتوحة
=============================================================
"""

import os, time, math, logging, threading, json, requests
from datetime import datetime, timezone
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
from flask import Flask, request as flask_request


# ══════════════════════════════════════════════════════════════
#  تحميل ملف .env (بلا مكتبات إضافية)
#  ويندوز لا يمرّر متغيرات البيئة من ملف تلقائياً مثل systemd،
#  فنقرأ .env بأنفسنا قبل أي os.getenv أدناه.
#  متغيرات النظام الموجودة مسبقاً لها الأولوية ولا تُستبدل.
# ══════════════════════════════════════════════════════════════
def _load_env(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return 0
    n = 0
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                # أزل علامات الاقتباس إن وُجدت
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                if k and k not in os.environ:
                    os.environ[k] = v
                    n += 1
    except Exception as e:
        print(f"تحذير: تعذّرت قراءة .env — {e}")
    return n

_ENV_LOADED = _load_env()

# ─── CREDENTIALS ─────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "YOUR_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_SECRET")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "YOUR_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT")
# لا مفتاح افتراضي — المفتاح "my_secret_123" كان معروفاً لأي شخص يرى الكود
TV_SECRET          = os.getenv("TV_SECRET", "")

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
# ملاحظة: هذه القيم كانت معرّفة هنا لكن الكود يتجاهلها ويستخدم أرقاماً
# مكتوبة داخل analyze(). الآن هي المصدر الوحيد — والقيم مطابقة
# للسلوك الفعلي السابق حتى لا يتغيّر التداول بلا قصد منك.
MIN_VOL_RATIO     = 1.0           # الحد الأدنى لنسبة الحجم (كان 1.5 معطّلاً / 1.0 فعلياً)
RSI_LONG_MAX      = 78            # فوقه: ذروة شراء → لا long
RSI_SHORT_MIN     = 22            # تحته: ذروة بيع  → لا short
RANGE_LOOKBACK    = 20            # عدد الشموع لحساب الدعم/المقاومة على 15m
POS_LONG_MAX      = 0.85          # لا long إذا كان السعر أعلى من 85% من النطاق
POS_SHORT_MIN     = 0.15          # لا short إذا كان السعر أدنى من 15% من النطاق
MIN_SCORE         = 60            # كان مدفوناً داخل analyze()

# ─── فلتر اتجاه السوق العام (BTC) ─────────────────────────────
# ⚠️ يغيّر سلوك التداول: كان مكتوباً لكنه لا يعمل إطلاقاً (شرط مستحيل).
# مفعّلاً: سوق BTC صاعد → long فقط | هابط → short فقط.
# لتعطيله: MARKET_FILTER=false في متغيرات البيئة.
MARKET_FILTER     = os.getenv("MARKET_FILTER", "true").lower() == "true"

# ─── حد الصفقات اليومي ────────────────────────────────────────
MAX_DAILY_TRADES  = int(os.getenv("MAX_DAILY_TRADES", "12"))

# ─── Fear & Greed Sentiment ───────────────────────────────────
SENTIMENT_ENABLED     = True
SENTIMENT_CACHE_MIN   = 30        # تحديث كل 30 دقيقة
FEAR_BLOCK_LONG       = 25        # لو الخوف < 25 → لا long
GREED_BLOCK_SHORT     = 75        # لو الطمع > 75 → لا short

# ─── ساعات راحة (UTC) ─────────────────────────────────────────
NO_TRADE_HOURS = {2, 3, 4}

# ─── تخزين ─────────────────────────────────────────────────────
# DATA_DIR: على Render اربط Persistent Disk وحط المسار هنا (مثلاً /var/data)
# بدونه الملفات تُمسح مع كل deploy
DATA_DIR      = os.getenv("DATA_DIR", ".")
LEARNING_FILE = os.path.join(DATA_DIR, "bot_v10_data.json")
STATE_FILE    = os.path.join(DATA_DIR, "bot_v10_state.json")   # الصفقات المفتوحة
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DATA_DIR, "bot_v10.log"), encoding="utf-8"),
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
    "total_pnl":   0.0,      # نسبة تراكمية (للعرض فقط — غير قابلة للجمع منطقياً)
    "net_usdt":    0.0,      # صافي الربح الفعلي بالدولار بعد الرسوم
    "fees_usdt":   0.0,      # إجمالي العمولات + التمويل
    "gross_usdt":  0.0,      # الربح قبل الرسوم
    "unmeasured":  0,        # صفقات تعذّر قياسها من Binance
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

    # ── حفظ/استعادة ──────────────────────────────────────────
    def to_dict(self):
        return {
            "symbol": self.symbol, "entry": self.entry, "qty": self.qty,
            "direction": self.direction, "tp_price": self.tp_price,
            "sl_price": self.sl_price, "atr": self.atr, "reasons": self.reasons,
            "open_time": self.open_time.isoformat(),
            "highest": self.highest, "lowest": self.lowest,
            "breakeven": self.breakeven, "trailing": self.trailing,
            "trail_sl": self.trail_sl, "notified": self.notified,
        }

    @classmethod
    def from_dict(cls, d, entry=None, qty=None):
        t = cls(
            d["symbol"], entry if entry else d["entry"], qty if qty else d["qty"],
            d["direction"], d["tp_price"], d["sl_price"], d.get("atr", 0),
            d.get("reasons", []),
        )
        try:
            t.open_time = datetime.fromisoformat(d["open_time"])
        except Exception:
            pass
        t.highest   = d.get("highest",  t.entry)
        t.lowest    = d.get("lowest",   t.entry)
        t.breakeven = d.get("breakeven", False)
        t.trailing  = d.get("trailing",  False)
        t.trail_sl  = d.get("trail_sl",  t.sl_price)
        t.notified  = d.get("notified",  False)
        return t

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

def fetch_trade_income(symbol, start_ms, tries=4):
    """
    يجلب الأرقام الحقيقية من Binance بدل التقدير:
    REALIZED_PNL + COMMISSION + FUNDING_FEE.
    قيم COMMISSION و FUNDING تأتي سالبة من Binance، فالجمع صحيح.
    """
    for attempt in range(tries):
        try:
            rows = client.futures_income(symbol=symbol, startTime=start_ms, limit=1000)
        except Exception as e:
            log.warning(f"income {symbol} #{attempt+1}: {e}")
            time.sleep(1.5)
            continue

        realized = commission = funding = 0.0
        for r in rows:
            t = r.get("incomeType")
            try: v = float(r.get("income", 0))
            except: continue
            if   t == "REALIZED_PNL": realized   += v
            elif t == "COMMISSION":   commission += v
            elif t == "FUNDING_FEE":  funding    += v

        # الربح المحقق يظهر بعد الإغلاق بثوانٍ — ننتظر ظهوره
        if realized != 0.0:
            return {
                "ok": True, "realized": realized,
                "commission": commission, "funding": funding,
                "net": realized + commission + funding,
            }
        time.sleep(1.5)

    log.warning(f"⚠️ {symbol}: تعذّر قياس الربح من Binance — سيُسجَّل تقديرياً")
    return {"ok": False, "realized": 0.0, "commission": 0.0, "funding": 0.0, "net": 0.0}


def record_trade(trade, exit_price):
    """
    يسجّل الصفقة بالأرقام الفعلية من Binance (صافي بعد الرسوم).
    يرجع (فوز, نسبة على الهامش, تفاصيل).
    """
    # نبدأ من دقيقة قبل الفتح لالتقاط عمولة الدخول
    start_ms = int((trade.open_time.timestamp() - 60) * 1000)
    inc      = fetch_trade_income(trade.symbol, start_ms)
    margin   = (trade.entry * trade.qty / LEVERAGE) if LEVERAGE else 0.0

    if inc["ok"]:
        net     = inc["net"]
        fees    = inc["commission"] + inc["funding"]
        gross   = inc["realized"]
        pnl_pct = (net / margin * 100) if margin > 0 else 0.0
        won     = net > 0
        source  = "binance"
    else:
        # احتياطي: التقدير القديم من السعر (بدون رسوم — متفائل)
        pnl_pct = trade.pnl_pct(exit_price)
        gross   = pnl_pct / 100 * margin
        fees    = 0.0
        net     = gross
        won     = gross > 0
        source  = "تقديري"
        data["unmeasured"] += 1

    data["total_pnl"]   += pnl_pct
    data["net_usdt"]    += net
    data["fees_usdt"]   += fees
    data["gross_usdt"]  += gross
    data["daily_count"] += 1
    if won: data["wins"]   += 1
    else:   data["losses"] += 1

    data["trades"].append({
        "sym": trade.symbol, "dir": trade.direction,
        "entry": trade.entry, "exit": exit_price,
        "qty": trade.qty, "margin": round(margin, 2),
        "gross": round(gross, 4), "fees": round(fees, 4),
        "net": round(net, 4), "pnl": round(pnl_pct, 2),
        "won": won, "src": source,
        "hrs": round(trade.duration_hrs(), 1),
        "ts":  utcnow().isoformat(),
    })
    if len(data["trades"]) > 200:
        data["trades"] = data["trades"][-200:]
    save_data()

    total = data["wins"] + data["losses"]
    wr    = data["wins"] / total * 100 if total else 0
    log.info(
        f"📊 {trade.symbol} {'✅' if won else '❌'} "
        f"صافي:{net:+.4f}$ (خام:{gross:+.4f} رسوم:{fees:+.4f}) "
        f"{pnl_pct:+.2f}% [{source}] | WR:{wr:.0f}% ({total} صفقة)"
    )
    inc["net"], inc["fees"], inc["gross"], inc["source"] = net, fees, gross, source
    return won, pnl_pct, inc


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

def equity():
    """الرصيد شاملاً الأرباح/الخسائر غير المحققة — هذا ما يجب أن تُقاس عليه الحدود."""
    try:
        return float(client.futures_account()["totalMarginBalance"])
    except Exception as e:
        log.error(f"equity: {e}")
        return balance()

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
    # لا نخترع قيماً — كمية خاطئة أخطر من تفويت صفقة
    log.error(f"❌ {symbol}: تعذّر جلب الفلاتر — لن يُتداول على هذه العملة")
    return None

def _decimals(x):
    """عدد الخانات العشرية الفعلية لـ stepSize/tickSize (0.005 → 3)."""
    s = f"{x:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0

def rqty(symbol, qty):
    """
    يقصّ الكمية لأقرب مضاعف أدنى لـ stepSize.
    الطريقة القديمة f"{qty:.Nf}" تقرّب لأعلى أحياناً وتفترض أن stepSize
    قوة عشرة — فتنتج كمية أكبر من المتاح أو غير مقبولة.
    """
    f = get_filters(symbol)
    if not f: return 0.0
    lot = f[0]
    if lot <= 0: return round(qty, 3)
    steps = math.floor(qty / lot + 1e-9)
    return round(steps * lot, _decimals(lot))

def rprice(symbol, price):
    """يحاذي السعر لمضاعف tickSize."""
    f = get_filters(symbol)
    if not f: return round(price, 4)
    tick = f[1]
    if tick <= 0: return round(price, 4)
    steps = round(price / tick)
    return round(steps * tick, _decimals(tick))

def cancel_stops(symbol):
    """يلغي كل أوامر الحماية (STOP و TAKE_PROFIT) المعلّقة على العملة."""
    try:
        for o in client.futures_get_open_orders(symbol=symbol):
            t = o.get("type", "")
            if "STOP" in t or "TAKE_PROFIT" in t:
                try: client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                except: pass
    except: pass


def has_protection(symbol):
    """يرجع (فيه SL؟, فيه TP؟) حسب الأوامر المعلّقة فعلياً على Binance."""
    try:
        orders = client.futures_get_open_orders(symbol=symbol)
    except Exception as e:
        log.warning(f"open_orders {symbol}: {e}")
        return None, None                       # None = غير معروف، لا تتصرف
    sl = any("STOP" in o.get("type", "") for o in orders)
    tp = any("TAKE_PROFIT" in o.get("type", "") for o in orders)
    return sl, tp


def _valid_stop(direction, kind, price, mark):
    """يتأكد أن السعر على الجهة الصحيحة من السعر الحالي حتى لا يرفضه Binance (-2021)."""
    if mark <= 0 or price <= 0: return False
    if direction == "long":
        return price < mark if kind == "sl" else price > mark
    return price > mark if kind == "sl" else price < mark


def place_protection(symbol, sl_price, tp_price, direction, want_tp=True):
    """
    يضع SL و TP حقيقيين على Binance بمستويات الاستراتيجية الفعلية (ATR)،
    باستخدام closePosition=True فيتبعان حجم الصفقة تلقائياً.
    يرجع (ok_sl, ok_tp).
    """
    is_long = direction == "long"
    side    = SIDE_SELL if is_long else SIDE_BUY
    mark    = cur_price(symbol)
    ok_sl = ok_tp = False

    cancel_stops(symbol)
    time.sleep(0.3)

    # ── Stop Loss ────────────────────────────────────────────
    if symbol not in _sl_no_support and sl_price:
        sl_p = rprice(symbol, sl_price)
        if not _valid_stop(direction, "sl", sl_p, mark):
            log.warning(f"⚠️ {symbol}: SL={sl_p} على الجهة الخاطئة من {mark} — لم يُوضع")
        else:
            for wt in ("MARK_PRICE", "CONTRACT_PRICE"):
                try:
                    client.futures_create_order(
                        symbol=symbol, side=side, type="STOP_MARKET",
                        stopPrice=sl_p, closePosition=True, workingType=wt
                    )
                    log.info(f"✅ SL {symbol}={sl_p} [{wt}]")
                    ok_sl = True
                    break
                except Exception as e:
                    if "-4120" in str(e) or "does not support" in str(e).lower():
                        continue
                    log.error(f"SL {symbol} [{wt}]: {e}")
                    break
            else:
                _sl_no_support.add(symbol)
                log.warning(f"⚠️ {symbol}: لا يدعم SL على Binance")

    # ── Take Profit ──────────────────────────────────────────
    if want_tp and tp_price:
        tp_p = rprice(symbol, tp_price)
        if not _valid_stop(direction, "tp", tp_p, mark):
            log.warning(f"⚠️ {symbol}: TP={tp_p} على الجهة الخاطئة من {mark} — لم يُوضع")
        else:
            for wt in ("MARK_PRICE", "CONTRACT_PRICE"):
                try:
                    client.futures_create_order(
                        symbol=symbol, side=side, type="TAKE_PROFIT_MARKET",
                        stopPrice=tp_p, closePosition=True, workingType=wt
                    )
                    log.info(f"✅ TP {symbol}={tp_p} [{wt}]")
                    ok_tp = True
                    break
                except Exception as e:
                    if "-4120" in str(e) or "does not support" in str(e).lower():
                        continue
                    log.error(f"TP {symbol} [{wt}]: {e}")
                    break

    return ok_sl, ok_tp


def sync_protection(trade, reason=""):
    """يحرّك SL على Binance ليطابق trail_sl بعد Breakeven / Trailing."""
    ok_sl, ok_tp = place_protection(
        trade.symbol, trade.trail_sl, trade.tp_price, trade.direction
    )
    log.info(f"🔄 حماية {trade.symbol} {reason}: SL={'✅' if ok_sl else '❌'} TP={'✅' if ok_tp else '❌'}")
    return ok_sl


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
    if len(closes) < slow + sig: return 0, False, False
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
        # كان: (direction=="short" and not ema_bear) — مستحيل التحقق،
        # لأن short لا تحدث إلا مع ema_bear. الفلتر لم يعمل ولا مرة.
        if MARKET_FILTER:
            if _market_bull and direction == "short":
                log.info(f"🔕 {symbol}: short في سوق BTC صاعد — رفض")
                return None
            if not _market_bull and direction == "long":
                log.info(f"🔕 {symbol}: long في سوق BTC هابط — رفض")
                return None

        # ── RSI Filter — أكثر مرونة ──────────────────────────
        if direction == "long"  and rsi_1h > RSI_LONG_MAX:
            log.info(f"🔕 {symbol}: RSI ذروة شراء {rsi_1h:.0f}")
            return None
        if direction == "short" and rsi_1h < RSI_SHORT_MIN:
            log.info(f"🔕 {symbol}: RSI ذروة بيع {rsi_1h:.0f}")
            return None

        # ── طبقة 2: تأكيد الدخول (بسيط وفعّال) ─────────────────
        # نستخدم EMA على 15m + موقع السعر من النطاق
        e9_15  = ema(cl15, 9)
        e21_15 = ema(cl15, 21)
        rsi_15 = rsi(cl15)

        # حساب النطاق من آخر 20 شمعة
        resist = max(hi15[-(RANGE_LOOKBACK+1):-1])
        support= min(lo15[-(RANGE_LOOKBACK+1):-1])
        rng    = resist - support or 1e-9
        pos    = (price - support) / rng   # 0=دعم, 1=مقاومة

        if direction == "long":
            # الدخول عندما EMA15 صاعدة والسعر ليس في ذروة
            entry_ok = e9_15 > e21_15 and pos < POS_LONG_MAX
            if not entry_ok:
                log.info(f"🔕 {symbol}: 15m لا يؤكد long (EMA15={'↑' if e9_15>e21_15 else '↓'} pos={pos:.2f})")
                return None
        else:
            entry_ok = e9_15 < e21_15 and pos > POS_SHORT_MIN
            if not entry_ok:
                log.info(f"🔕 {symbol}: 15m لا يؤكد short (EMA15={'↓' if e9_15<e21_15 else '↑'} pos={pos:.2f})")
                return None

        b_strength = 1.0 - abs(pos - 0.5) * 2   # أعلى نقطة عند المنتصف
        entry_type = "ema_confirm"

        # ── طبقة 3: حجم ───────────────────────────────────────
        avg_vol = sum(vo15[-(RANGE_LOOKBACK+1):-1]) / RANGE_LOOKBACK or 1
        vol_r   = vo15[-2] / avg_vol
        if vol_r < MIN_VOL_RATIO:
            log.info(f"🔕 {symbol}: حجم ضعيف جداً {vol_r:.2f}")
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

        # تأكيد EMA 15m
        if entry_type == "breakout":
            score += int(b_strength * 25)
            reasons.append(f"Break↑×{b_strength:.1f}")
        elif entry_type == "pullback":
            score += int(b_strength * 15)
            reasons.append(f"Pull↑×{b_strength:.1f}")
        else:
            score += 15
            reasons.append(f"EMA15✅")

        # موقع السعر في النطاق
        if direction == "long"  and pos < 0.40: score += 10; reasons.append(f"Near-Support")
        if direction == "short" and pos > 0.60: score += 10; reasons.append(f"Near-Resist")

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

        # ملاحظة: بما أن SL و TP كلاهما من ATR بمضاعفات ثابتة، فإن
        # RR = ATR_TP_MULT/ATR_SL_MULT دائماً. الفحص هنا لا يُفعّل عملياً،
        # والتحقق الحقيقي انتقل إلى بدء التشغيل (validate_config).
        if rr < MIN_RR:
            log.warning(f"🔕 {symbol}: RR={rr:.2f} < {MIN_RR} — راجع مضاعفات ATR")
            return None

        log.info(
            f"🎯 {symbol} {direction} [{entry_type}] score={score} RR={rr:.2f} "
            f"RSI={rsi_1h:.0f} Vol×{vol_r:.1f}"
        )

        return {
            "symbol":     symbol,
            "direction":  direction,
            "score":      score,
            "price":      price,
            "tp":         rprice(symbol, tp_p),
            "sl":         rprice(symbol, sl_p),
            "atr":        atr_1h,
            "rr":         round(rr, 2),
            "rsi_1h":     round(rsi_1h, 1),
            "vol_r":      round(vol_r, 1),
            "resist":     round(resist, 4),
            "support":    round(support, 4),
            "b_str":      round(b_strength, 2),
            "entry_type": entry_type,
            "reasons":    reasons,
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
#  STATE PERSISTENCE + RECOVERY
# ══════════════════════════════════════════════════════════════
def save_state():
    """يحفظ الصفقات المفتوحة — تُستدعى عند كل تغيير مهم."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {s: t.to_dict() for s, t in open_trades.items()},
                f, ensure_ascii=False, indent=2
            )
    except Exception as e:
        log.error(f"save_state: {e}")


def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.error(f"load_state: {e}")
    return {}


def current_atr(symbol):
    """ATR على 1h — يُستخدم لإعادة بناء SL/TP لصفقة فقدنا بياناتها."""
    try:
        k = client.futures_klines(symbol=symbol, interval="1h", limit=100)
        return atr_calc([float(x[2]) for x in k],
                        [float(x[3]) for x in k],
                        [float(x[4]) for x in k])
    except Exception as e:
        log.error(f"atr {symbol}: {e}")
        return 0.0


def recover_trades():
    """
    مصدر الحقيقة هو Binance، لا الملف.
    لكل وضعية مفتوحة فعلياً: نعيد بناء TradeState (من الملف إن توفّر، وإلا من ATR)
    ونتأكد أن SL/TP موجودان على البورصة.
    """
    saved = load_state()
    found = []

    for p in all_positions():
        try:
            amt = float(p["positionAmt"])
        except Exception:
            continue
        if abs(amt) < 1e-8:
            continue

        sym       = p["symbol"]
        entry     = float(p.get("entryPrice") or 0)
        direction = "long" if amt > 0 else "short"
        qty       = abs(amt)

        if entry <= 0:
            entry = cur_price(sym)
        if entry <= 0:
            log.error(f"❌ {sym}: تعذّر تحديد سعر الدخول — تخطّي")
            continue

        s = saved.get(sym)
        if s and s.get("direction") == direction:
            trade  = TradeState.from_dict(s, entry=entry, qty=qty)
            origin = "الملف"
        else:
            a = current_atr(sym)
            if a <= 0:
                a = entry * 0.01                       # احتياطي 1%
            if direction == "long":
                sl = entry - a * ATR_SL_MULT
                tp = entry + a * ATR_TP_MULT
            else:
                sl = entry + a * ATR_SL_MULT
                tp = entry - a * ATR_TP_MULT
            trade  = TradeState(sym, entry, qty, direction, tp, sl, a, ["recovered"])
            origin = "ATR"

        open_trades[sym] = trade

        sl_ok, tp_ok = has_protection(sym)
        if sl_ok is None:                              # لم نستطع القراءة — لا نعبث
            log.warning(f"⚠️ {sym}: تعذّرت قراءة الأوامر — الحماية غير مؤكدة")
        elif not (sl_ok and tp_ok):
            place_protection(sym, trade.trail_sl, trade.tp_price, direction)

        found.append(f"{sym} {direction} @{entry:.4f} ({origin})")
        log.info(f"♻️ استُعيدت: {sym} {direction} qty={qty} entry={entry:.4f} [{origin}]")

    # صفقات في الملف لم تعد موجودة على Binance → أُغلقت أثناء التوقف
    for sym in saved:
        if sym not in open_trades:
            log.info(f"🗑️ {sym}: في الملف لكن لا وضعية على Binance — أُهملت")

    save_state()

    if found:
        tg("♻️ *استعادة بعد إعادة التشغيل*\n" + "\n".join(f"• {x}" for x in found))
    else:
        log.info("♻️ لا صفقات مفتوحة للاستعادة")
    return len(found)


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
        f = get_filters(sym)
        if not f:
            return False
        min_n = f[2]
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
        save_state()
        bn, tp_ok = place_protection(sym, cand["sl"], cand["tp"], dire)

        dl = "📈 Long" if dire == "long" else "📉 Short"
        tg(
            f"🚀 *{dl}: {sym}*\n"
            f"سعر:`{re:.4f}` | رافعة:`{LEVERAGE}x`\n"
            f"TP:`{cand['tp']:.4f}` | SL:`{cand['sl']:.4f}`\n"
            f"RR:`{trade.rr():.2f}` | BE`+{BE_TRIGGER*100:.1f}%`\n"
            f"SL-BN:{'✅' if bn else '⚠️محلي'} | TP-BN:{'✅' if tp_ok else '⚠️محلي'}\n"
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
        open_trades.pop(symbol, None); save_state(); return
    ok = mkt_close(symbol, abs(amt), trade.direction)
    if ok:
        open_trades.pop(symbol, None)
        save_state()
        won, pnl, inc = record_trade(trade, price)
        bal = balance()
        em  = "🟢" if won else "🔴"
        de  = "📈L" if trade.direction == "long" else "📉S"
        total = data["wins"] + data["losses"]
        wr    = data["wins"] / total * 100 if total else 0
        src_tag = "" if inc["source"] == "binance" else " ⚠️تقديري"
        tg(
            f"{em} *{de} {symbol}*\n"
            f"{reason}\n"
            f"دخول:`{trade.entry:.4f}` → خروج:`{price:.4f}`\n"
            f"صافي:`{inc['net']:+.4f}` USDT{src_tag}\n"
            f"خام:`{inc['gross']:+.4f}` | رسوم:`{inc['fees']:+.4f}`\n"
            f"على الهامش:`{pnl:+.2f}%` | مدة:`{trade.duration_hrs():.1f}h`\n"
            f"WR:`{wr:.0f}%` ({total} صفقة) | تراكمي:`{data['net_usdt']:+.2f}` USDT\n"
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
            save_state()
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
                    save_state()
                    p = cur_price(sym)
                    if p > 0:
                        won, pnl, inc = record_trade(tr, p)
                        tg(
                            f"{'🟢' if won else '🔴'} *مُغلقة على Binance: {sym}*\n"
                            f"صافي:`{inc['net']:+.4f}` USDT | رسوم:`{inc['fees']:+.4f}`\n"
                            f"على الهامش:`{pnl:+.2f}%`"
                        )
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
                    save_state()
                    sync_protection(tr, "BE")
                    tg(f"🔒 *BE {sym}* SL→`{tr.trail_sl:.4f}` P&L:`+{tr.pnl_pct(p):.2f}%`")
                elif ev == "trail":
                    save_state()
                    sync_protection(tr, "Trail")
                    tg(f"📈 *Trail {sym}* SL:`{tr.trail_sl:.4f}` P&L:`+{tr.pnl_pct(p):.2f}%`")

                # تجديد الحماية لو اختفت من Binance
                sl_ok, tp_ok = has_protection(sym)
                if sl_ok is not None and not (sl_ok and tp_ok):
                    log.warning(f"⚠️ {sym}: حماية ناقصة (SL:{sl_ok} TP:{tp_ok}) — إعادة وضع")
                    place_protection(sym, tr.trail_sl, tr.tp_price, tr.direction)

        except Exception as e:
            log.error(f"prot_mon: {e}")
        time.sleep(5)


# ══════════════════════════════════════════════════════════════
#  PROTECTION CHECK
# ══════════════════════════════════════════════════════════════
def check_protection(bal):
    global halted, daily_start_bal, daily_reset_dt, _daily_trades
    if halted: return False
    eq = equity()
    if eq > 0: bal = eq          # احسب الحدود على الرصيد + الخسائر المفتوحة

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

    if _daily_trades >= MAX_DAILY_TRADES:
        log.info(f"⏸️ بلغ حد الصفقات اليومي ({MAX_DAILY_TRADES})")
        return False

    if utcnow().hour in NO_TRADE_HOURS:
        return False

    return True


# ══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════
def validate_config():
    """يفحص التناقضات في الإعدادات قبل أي تداول — يمنع الإقلاع لو كانت قاتلة."""
    fatal, warn = [], []

    if not BINANCE_API_KEY or BINANCE_API_KEY == "YOUR_KEY":
        fatal.append("BINANCE_API_KEY غير مضبوط")
    if not BINANCE_API_SECRET or BINANCE_API_SECRET == "YOUR_SECRET":
        fatal.append("BINANCE_API_SECRET غير مضبوط")

    rr_struct = ATR_TP_MULT / ATR_SL_MULT if ATR_SL_MULT else 0
    if rr_struct < MIN_RR:
        fatal.append(f"RR البنيوي {rr_struct:.2f} < MIN_RR {MIN_RR} — عدّل مضاعفات ATR")

    if BE_TRIGGER >= TRAIL_TRIGGER:
        warn.append(f"BE_TRIGGER({BE_TRIGGER}) ≥ TRAIL_TRIGGER({TRAIL_TRIGGER}) — Trailing لن يعمل")
    if MAX_DAILY_LOSS >= MAX_TOTAL_LOSS:
        warn.append("MAX_DAILY_LOSS ≥ MAX_TOTAL_LOSS — الحد الإجمالي بلا معنى")
    if DATA_DIR == ".":
        warn.append("DATA_DIR=. → الملفات تُمسح مع كل deploy. اربط Persistent Disk")
    if not TV_SECRET:
        warn.append("TV_SECRET فارغ — ويبهوك TradingView معطّل")
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "YOUR_TOKEN":
        warn.append("TELEGRAM_TOKEN غير مضبوط — لا إشعارات")

    for w in warn:  log.warning(f"⚠️ إعدادات: {w}")
    for e in fatal: log.error(f"❌ إعدادات: {e}")

    log.info(
        f"⚙️ رافعة:{LEVERAGE}x | RR:{rr_struct:.2f} | MIN_SCORE:{MIN_SCORE} | "
        f"فلتر السوق:{'مفعّل' if MARKET_FILTER else 'معطّل'} | "
        f"حد يومي:{MAX_DAILY_TRADES} صفقة"
    )
    if fatal:
        raise SystemExit("توقف: " + " | ".join(fatal))
    return warn


def main_loop():
    global bot_start_bal, daily_start_bal, daily_reset_dt, client

    log.info("🚀 Bot v11.0 — EMA(1h) + تأكيد(15m) + Sentiment")
    warns = validate_config()
    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

    load_data()

    # تحميل filters مسبقاً
    for sym in SYMBOLS:
        get_filters(sym)

    update_market()
    update_sentiment()
    recover_trades()

    ini = balance()
    data["peak_bal"]   = max(data.get("peak_bal", 0), ini)
    bot_start_bal      = ini
    daily_start_bal    = ini
    daily_reset_dt     = utcnow().date()

    threading.Thread(target=protection_monitor, daemon=True).start()

    total = data["wins"] + data["losses"]
    wr    = data["wins"] / total * 100 if total else 0
    tg(
        f"🤖 *Bot v11.0* ✅\n"
        f"رصيد:`{ini:.2f}` USDT\n"
        f"─── الاستراتيجية ───\n"
        f"EMA Crossover + Breakout + Sentiment\n"
        f"رافعة:`{LEVERAGE}x` ثابتة\n"
        f"SL:`ATR×{ATR_SL_MULT}` | TP:`ATR×{ATR_TP_MULT}`\n"
        f"─── السجل ───\n"
        f"WR:`{wr:.0f}%` ({total} صفقة)\n"
        f"صافي تراكمي:`{data['net_usdt']:+.2f}` USDT | رسوم:`{data['fees_usdt']:+.2f}`\n"
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
            # كان يصفّر العدّاد فقط دون مسح الكاش — أي لا إعادة تحميل إطلاقاً
            if sc >= 600:
                _filters_cache.clear()
                log.info("🔄 أُعيد تحميل فلاتر العملات")
                sc = 0

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
        f"<b>🤖 Bot v11.0 — EMA(1h) + تأكيد(15m) + Sentiment</b> | {bull}",
        f"رصيد:<b>{bal:.2f} USDT</b> | مفتوحة:{len(open_trades)}/{MAX_OPEN_TRADES}",
        f"WR:<b>{wr:.0f}%</b> ({total} صفقة) | "
        f"صافي:<b>{data['net_usdt']:+.2f} USDT</b> | رسوم:{data['fees_usdt']:+.2f}",
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
    net, fees, gross = data["net_usdt"], data["fees_usdt"], data["gross_usdt"]
    return json.dumps({
        "wins":         data["wins"],
        "losses":       data["losses"],
        "wr":           round(data["wins"]/total*100, 1) if total else 0,
        "net_usdt":     round(net, 4),
        "gross_usdt":   round(gross, 4),
        "fees_usdt":    round(fees, 4),
        "fees_pct_of_gross": round(abs(fees)/abs(gross)*100, 1) if gross else 0,
        "avg_net_per_trade": round(net/total, 4) if total else 0,
        "unmeasured":   data["unmeasured"],
        "total_pnl":    round(data["total_pnl"], 2),
        "sentiment": {"value": _sentiment["value"], "label": _sentiment["label"]},
        "market":    "bull" if _market_bull else "bear",
    }, ensure_ascii=False, indent=2)

@app.route("/webhook", methods=["POST"])
def webhook():
    """إشارات TradingView يدوية"""
    try:
        if not TV_SECRET:
            return {"status": "disabled", "msg": "اضبط TV_SECRET لتفعيل الويبهوك"}, 403
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
        # الفتح عبر الويبهوك غير مدعوم عمداً: يتجاوز كل فلاتر الدخول
        # وإدارة المخاطر. المدعوم فقط: direction="close".
        return {
            "status": "open_not_supported",
            "msg": "الويبهوك يدعم الإغلاق فقط — الفتح يتجاوز فلاتر المخاطر",
            "symbol": sym, "direction": dire,
        }
    except Exception as e:
        return {"status": "error", "msg": str(e)}, 500

# ══════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ══════════════════════════════════════════════════════════════
# ⚠️ الـ Procfile كان: gunicorn main:app
#    مع gunicorn لا يُنفَّذ __main__ إطلاقاً → خيط التداول لا يبدأ،
#    والبوت يعرض صفحة ويب فقط ولا يتداول. لذلك نبدأ الخيط عند الاستيراد.
#    وإذا استُخدم gunicorn فيجب أن يكون --workers 1 وإلا شُغّل بوت لكل عامل.
_bot_started = threading.Lock()
_started     = False

def start_bot_once():
    global _started
    with _bot_started:
        if _started:
            return
        _started = True
        threading.Thread(target=main_loop, daemon=True).start()
        log.info("🧵 خيط التداول بدأ")

start_bot_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
