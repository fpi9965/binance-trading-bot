"""
=============================================================
  SMART TRADING BOT v6.0 — Claude AI Analyst
  ─────────────────────────────────────────
  ✅ Claude يحلل كل عملة ويقرر الدخول أو الانتظار
  ✅ إصلاح: ORDER_TYPE_STOP_MARKET → "STOP_MARKET"
  ✅ إصلاح: TRAILING_STOP_MARKET endpoint صحيح
  ✅ حماية مزدوجة: داخلية + بايننس
  ✅ Dynamic Risk + Compounding + Pyramid
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
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY",  "YOUR_ANTHROPIC_KEY")

# ─── إعدادات التداول ──────────────────────────────────────────
LEVERAGE           = 10
MAX_OPEN_TRADES    = 4
SCAN_INTERVAL_SEC  = 45

# ─── Dynamic Risk ─────────────────────────────────────────────
BASE_RISK_PCT   = 0.02
MIN_RISK_PCT    = 0.01
MAX_RISK_PCT    = 0.05
RISK_STEP_WIN   = 0.004
RISK_STEP_LOSS  = 0.004

# ─── Pyramid ──────────────────────────────────────────────────
PYRAMID_ENABLED      = True
PYRAMID_TRIGGER_PCT  = 0.025
PYRAMID_MAX_ADDS     = 1
PYRAMID_SIZE_RATIO   = 0.4

# ─── الحماية الداخلية ─────────────────────────────────────────
ATR_SL_MULTIPLIER     = 1.8
ATR_TP_MULTIPLIER     = 3.0
ATR_SL_MAX            = 2.5
MIN_RR_RATIO          = 1.5
BREAKEVEN_TRIGGER_PCT = 0.012
TRAILING_START_PCT    = 0.020
TRAILING_STEP_PCT     = 0.006
MAX_TRADE_HOURS       = 24

# ─── حماية بايننس ─────────────────────────────────────────────
BN_SL_PCT              = 0.02
BN_TRAILING_CALLBACK   = 1.5
BN_TRAILING_ACTIVATION = 0.008

# ─── حماية الرصيد ─────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT  = 0.05
TOTAL_LOSS_LIMIT_PCT  = 0.15

# ─── فلتر السوق ──────────────────────────────────────────────
MARKET_FILTER_SYMBOL = "BTCUSDT"
MARKET_FILTER_EMA    = 50

# ─── فلترة العملات ────────────────────────────────────────────
MIN_24H_QUOTE_VOLUME = 50_000_000
MIN_CLAUDE_CONFIDENCE = 65   # Claude يقول ≥65% ثقة للدخول

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

open_trades:        dict = {}
_filters_cache:     dict = {}
_all_symbols_cache: list = []

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
    "atr_sl":             ATR_SL_MULTIPLIER,
    "atr_tp":             ATR_TP_MULTIPLIER,
    "win_rate":           0.0,
    "total_trades":       0,
    "profitable_trades":  0,
    "current_risk_pct":   BASE_RISK_PCT,
    "consecutive_wins":   0,
    "consecutive_losses": 0,
    "peak_balance":       0.0,
    "compounding_mult":   1.0,
}

PROTECTION_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"}


# ══════════════════════════════════════════════════════════════
#  TradeState
# ══════════════════════════════════════════════════════════════

class TradeState:
    def __init__(self, symbol, entry, qty, atr,
                 score=0, rsi=50, reasons=None, claude_analysis=""):
        self.symbol         = symbol
        self.entry          = entry
        self.qty            = qty
        self.atr            = atr
        self.score          = score
        self.rsi            = rsi
        self.reasons        = reasons or []
        self.claude_analysis = claude_analysis
        self.pyramid_adds   = 0
        self.open_time      = utcnow()

        sl_mult       = learning["atr_sl"]
        tp_mult       = learning["atr_tp"]
        self.sl_price = entry - atr * sl_mult
        self.tp_price = entry + atr * tp_mult

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

        if pnl_pct >= TRAILING_START_PCT:
            new_trail = self.highest_price * (1 - TRAILING_STEP_PCT)
            if new_trail > self.trail_sl:
                self.trail_sl        = new_trail
                self.trailing_active = True
                if (self.last_notif_sl is None or
                        abs(new_trail - self.last_notif_sl) / self.entry > 0.003):
                    self.last_notif_sl = new_trail
                    return "trailing_move"
        elif pnl_pct >= BREAKEVEN_TRIGGER_PCT and not self.at_breakeven:
            self.at_breakeven  = True
            self.trail_sl      = self.entry * 1.001
            self.last_notif_sl = self.trail_sl
            return "breakeven"

        if (PYRAMID_ENABLED
                and self.pyramid_adds < PYRAMID_MAX_ADDS
                and pnl_pct >= PYRAMID_TRIGGER_PCT * (self.pyramid_adds + 1)):
            return "pyramid_trigger"

        return "none"

    def pnl_pct(self, price):
        return (price - self.entry) / self.entry * 100 * LEVERAGE

    def duration_hours(self):
        return (utcnow() - self.open_time).total_seconds() / 3600

    def rr(self):
        risk = self.entry - self.sl_price
        return (self.tp_price - self.entry) / risk if risk > 0 else 0


# ══════════════════════════════════════════════════════════════
#  CLAUDE AI ANALYST
# ══════════════════════════════════════════════════════════════

CLAUDE_PROMPT = """أنت خبير تداول محترف في أسواق العملات الرقمية والعقود الآجلة (Futures).

المطلوب: تحليل زوج {symbol} على الإطار الزمني {timeframe}.

البيانات المتاحة:
- السعر الحالي: {price}
- RSI (15m): {rsi_15m:.1f}
- RSI (1h): {rsi_1h:.1f}
- MACD (15m): {macd_15m}
- MACD (4h): {macd_4h}
- EMA20 > EMA50 (1h): {ema_trend}
- السعر فوق EMA200 (1h): {above_ema200}
- Bollinger Band %: {bb_pct:.2f} (0=أسفل، 1=أعلى)
- نسبة الحجم مقارنة بالمتوسط: {vol_ratio:.2f}x
- نماذج الشموع: {patterns}
- ATR: {atr:.6f}
- حجم التداول 24h: {vol_24h:.0f}M USDT

نفّذ التحليل وفق الخطوات:
1. تحليل الاتجاه (EMA20/50/200)
2. الدعوم والمقاومات من البيانات
3. المؤشرات (RSI/MACD/Volume)
4. نماذج الشموع والسلوك السعري
5. إدارة المخاطر

القواعد الصارمة:
- لا تدخل إذا الإشارات متضاربة
- RSI > 72: لا تدخل LONG
- MACD هابط على 4h و15m معاً: لا تدخل
- لا تخاطر أكثر من 2% من رأس المال
- نسبة RR لا تقل عن 1:2

أخرج النتيجة بهذا الشكل JSON فقط بدون أي نص آخر:
{{
  "direction": "LONG" | "WAIT" | "AVOID",
  "confidence": 0-100,
  "entry": السعر,
  "sl": وقف الخسارة,
  "tp1": هدف 1,
  "tp2": هدف 2,
  "rr": نسبة RR,
  "reason": "سبب القرار في جملة واحدة",
  "summary": "ملخص التحليل في 2-3 جمل"
}}"""


def claude_analyze(symbol: str, data: dict) -> dict | None:
    """
    يرسل البيانات لـ Claude ويحصل على قرار التداول
    يُعيد None إذا فشل الاتصال أو Claude قال WAIT/AVOID
    """
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_KEY":
        log.warning("ANTHROPIC_API_KEY غير مضبوط — تخطي Claude")
        return None

    prompt = CLAUDE_PROMPT.format(
        symbol       = symbol,
        timeframe    = "15m",
        price        = data["price"],
        rsi_15m      = data["rsi_15m"],
        rsi_1h       = data["rsi_1h"],
        macd_15m     = "صاعد ✅" if data["macd_15m"] else "هابط ❌",
        macd_4h      = "صاعد ✅" if data["macd_4h"] else "هابط ❌",
        ema_trend    = "✅ نعم" if data["ema_trend"] else "❌ لا",
        above_ema200 = "✅ نعم" if data["above_ema200"] else "❌ لا",
        bb_pct       = data["bb_pct"],
        vol_ratio    = data["vol_ratio"],
        patterns     = ", ".join(data["patterns"]) if data["patterns"] else "لا يوجد",
        atr          = data["atr"],
        vol_24h      = data["vol_24h"] / 1e6,
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 800,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )

        if resp.status_code != 200:
            log.warning(f"Claude API {resp.status_code}: {resp.text[:200]}")
            return None

        text = resp.json()["content"][0]["text"].strip()

        # تنظيف الـ JSON
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        result = json.loads(text)
        log.info(
            f"🤖 Claude → {symbol}: {result.get('direction')} "
            f"({result.get('confidence')}%) — {result.get('reason', '')}"
        )
        return result

    except json.JSONDecodeError as e:
        log.warning(f"Claude JSON parse error {symbol}: {e} | text: {text[:200]}")
        return None
    except Exception as e:
        log.error(f"Claude API error {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  LEARNING + DYNAMIC RISK
# ══════════════════════════════════════════════════════════════

def load_learning():
    global learning
    try:
        if os.path.exists(LEARNING_FILE):
            with open(LEARNING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                learning.update(data)
            learning["atr_sl"] = min(learning["atr_sl"], ATR_SL_MAX)
            log.info(
                f"📚 تعلم محمّل | صفقات:{learning['total_trades']} "
                f"Win%:{learning['win_rate']*100:.1f}% "
                f"Risk:{learning['current_risk_pct']*100:.1f}%"
            )
    except Exception as e:
        log.error(f"load_learning: {e}")


def save_learning():
    try:
        with open(LEARNING_FILE, "w", encoding="utf-8") as f:
            json.dump(learning, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"save_learning: {e}")


def update_dynamic_risk(won: bool, balance: float):
    risk = learning["current_risk_pct"]
    if won:
        learning["consecutive_wins"]  += 1
        learning["consecutive_losses"] = 0
        risk = min(risk + RISK_STEP_WIN, MAX_RISK_PCT)
        log.info(f"📈 Risk ↑ {risk*100:.2f}%")
    else:
        learning["consecutive_losses"] += 1
        learning["consecutive_wins"]    = 0
        risk = max(risk - RISK_STEP_LOSS, MIN_RISK_PCT)
        log.info(f"📉 Risk ↓ {risk*100:.2f}%")

    if learning["consecutive_wins"] >= 2 and risk < BASE_RISK_PCT:
        risk = BASE_RISK_PCT

    learning["current_risk_pct"] = risk

    if balance > learning["peak_balance"]:
        learning["peak_balance"] = balance
    if learning["peak_balance"] > 0 and bot_start_balance > 0:
        growth = learning["peak_balance"] / bot_start_balance
        learning["compounding_mult"] = max(1.0, min(growth, 2.0))


def record_trade(trade: TradeState, exit_price: float, balance: float):
    won = exit_price > trade.entry
    pnl = trade.pnl_pct(exit_price)

    learning["trade_history"].append({
        "symbol":  trade.symbol,
        "entry":   trade.entry,
        "exit":    exit_price,
        "pnl_pct": round(pnl, 3),
        "won":     won,
        "ts":      utcnow().isoformat(),
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

    learning["total_trades"] += 1
    if won:
        learning["profitable_trades"] += 1
    learning["win_rate"] = learning["profitable_trades"] / learning["total_trades"]

    update_dynamic_risk(won, balance)
    _adapt_atr(won)
    save_learning()
    log.info(f"📊 {trade.symbol} {'✅' if won else '❌'} {pnl:+.2f}%")


def _adapt_atr(won: bool):
    history = learning["trade_history"]
    if len(history) < 15:
        return
    recent    = history[-30:]
    loss_rate = sum(1 for t in recent if not t["won"]) / len(recent)

    if loss_rate > 0.55:
        new_sl = min(learning["atr_sl"] * 1.06, ATR_SL_MAX)
        if new_sl != learning["atr_sl"]:
            learning["atr_sl"] = new_sl
    elif loss_rate < 0.30:
        learning["atr_sl"] = max(learning["atr_sl"] * 0.96, 1.3)

    wins = [t for t in recent if t["won"]]
    if wins:
        avg_win = statistics.mean(t["pnl_pct"] for t in wins)
        if avg_win > 8:
            learning["atr_tp"] = min(learning["atr_tp"] * 1.04, 6.0)
        elif avg_win < 3:
            learning["atr_tp"] = max(learning["atr_tp"] * 0.97, 2.0)


def is_blacklisted(symbol: str) -> bool:
    st = learning["symbol_stats"].get(symbol)
    if not st:
        return False
    total = st["wins"] + st["losses"]
    if total < 5:
        return False
    return (st["wins"] / total) < 0.35


def get_effective_risk() -> float:
    return min(learning["current_risk_pct"] * learning["compounding_mult"], MAX_RISK_PCT)


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
#  ✅ حماية بايننس — إصلاح ORDER_TYPE_STOP_MARKET و Trailing
# ══════════════════════════════════════════════════════════════

def cancel_protection_orders(symbol: str):
    try:
        for o in client.futures_get_open_orders(symbol=symbol):
            if o["type"] in PROTECTION_TYPES:
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                except Exception as e:
                    log.warning(f"إلغاء فشل {symbol}: {e}")
    except Exception as e:
        log.error(f"cancel_protection {symbol}: {e}")


def place_binance_protection(symbol: str, entry: float, qty: float) -> bool:
    """
    ✅ إصلاح كامل:
    - "STOP_MARKET" كنص مباشر بدل ORDER_TYPE_STOP_MARKET
    - TRAILING_STOP_MARKET عبر futures_create_order الصحيح
    """
    if qty <= 0:
        return False

    cancel_protection_orders(symbol)
    time.sleep(0.5)

    ok_sl = ok_tr = False

    # ✅ إصلاح 1: "STOP_MARKET" كنص مباشر
    sl_price = round_price(symbol, entry * (1 - BN_SL_PCT))
    try:
        client.futures_create_order(
            symbol      = symbol,
            side        = SIDE_SELL,
            type        = "STOP_MARKET",      # ✅ نص مباشر
            stopPrice   = sl_price,
            quantity    = qty,
            reduceOnly  = True,
            workingType = "MARK_PRICE"
        )
        ok_sl = True
        log.info(f"✅ BN-SL={sl_price} qty={qty} لـ {symbol}")
    except Exception as e:
        log.error(f"❌ BN-SL فشل {symbol}: {e}")
        send_telegram(f"⚠️ *فشل SL بايننس* `{symbol}`\n`{e}`")

    # ✅ إصلاح 2: TRAILING_STOP_MARKET بـ parameters صحيحة
    activation = round_price(symbol, entry * (1 + BN_TRAILING_ACTIVATION))
    try:
        client.futures_create_order(
            symbol          = symbol,
            side            = SIDE_SELL,
            type            = "TRAILING_STOP_MARKET",   # ✅ نص مباشر
            quantity        = qty,
            callbackRate    = BN_TRAILING_CALLBACK,
            activationPrice = activation,
            reduceOnly      = True,
            workingType     = "CONTRACT_PRICE"          # ✅ تغيير من MARK_PRICE
        )
        ok_tr = True
        log.info(f"✅ BN-Trail={BN_TRAILING_CALLBACK}% @{activation} لـ {symbol}")
    except Exception as e:
        log.error(f"❌ BN-Trail فشل {symbol}: {e}")
        # fallback: بدون activationPrice
        try:
            client.futures_create_order(
                symbol       = symbol,
                side         = SIDE_SELL,
                type         = "TRAILING_STOP_MARKET",
                quantity     = qty,
                callbackRate = BN_TRAILING_CALLBACK,
                reduceOnly   = True,
            )
            ok_tr = True
            log.info(f"✅ BN-Trail (fallback) لـ {symbol}")
        except Exception as e2:
            log.error(f"❌ BN-Trail fallback فشل {symbol}: {e2}")

    return ok_sl or ok_tr


def verify_binance_protection(symbol: str, trade: TradeState):
    """يتحقق كل 5 ثوان ويُعيد الحماية إذا اختفت"""
    try:
        orders    = client.futures_get_open_orders(symbol=symbol)
        has_sl    = any(o["type"] == "STOP_MARKET"          for o in orders)
        has_trail = any(o["type"] == "TRAILING_STOP_MARKET"  for o in orders)

        if not has_sl and not has_trail:
            log.warning(f"⚠️ {symbol}: حماية مفقودة — إعادة")
            amt, _ = get_actual_position(symbol)
            if abs(amt) > 1e-8:
                place_binance_protection(symbol, trade.entry, abs(amt))

        elif not has_sl:
            sl_price = round_price(symbol, trade.entry * (1 - BN_SL_PCT))
            amt, _   = get_actual_position(symbol)
            try:
                client.futures_create_order(
                    symbol=symbol, side=SIDE_SELL, type="STOP_MARKET",
                    stopPrice=sl_price, quantity=abs(amt),
                    reduceOnly=True, workingType="MARK_PRICE"
                )
            except Exception as e:
                log.error(f"إعادة SL {symbol}: {e}")

        elif not has_trail:
            amt, _ = get_actual_position(symbol)
            try:
                client.futures_create_order(
                    symbol=symbol, side=SIDE_SELL, type="TRAILING_STOP_MARKET",
                    quantity=abs(amt), callbackRate=BN_TRAILING_CALLBACK,
                    reduceOnly=True,
                )
            except Exception as e:
                log.error(f"إعادة Trail {symbol}: {e}")

    except Exception as e:
        log.error(f"verify_protection {symbol}: {e}")


# ══════════════════════════════════════════════════════════════
#  MARKET FILTER
# ══════════════════════════════════════════════════════════════

def update_market_filter():
    global _market_is_bull
    try:
        kl  = client.futures_klines(symbol=MARKET_FILTER_SYMBOL, interval="1h", limit=60)
        cls = [float(k[4]) for k in kl]
        k   = 2 / (MARKET_FILTER_EMA + 1)
        v   = sum(cls[:MARKET_FILTER_EMA]) / MARKET_FILTER_EMA
        for c in cls[MARKET_FILTER_EMA:]:
            v = c * k + v * (1 - k)
        ema50           = v
        prev            = _market_is_bull
        _market_is_bull = cls[-1] >= ema50 * 0.98
        if prev != _market_is_bull:
            status = "🟢 صاعد" if _market_is_bull else "🔴 هابط"
            send_telegram(
                f"📡 *تغيير السوق: {status}*\n"
                f"BTC:`{cls[-1]:.2f}` | EMA50:`{ema50:.2f}`"
            )
    except Exception as e:
        log.error(f"market_filter: {e}")


# ══════════════════════════════════════════════════════════════
#  TECHNICAL DATA — جمع البيانات لـ Claude
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


def compute_macd_bull(closes, fast=12, slow=26, signal=9):
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


def compute_bb_pct(closes, period=20):
    if len(closes) < period:
        return 0.5
    window = closes[-period:]
    mid = sum(window) / period
    std = (sum((x-mid)**2 for x in window) / period) ** 0.5
    upper = mid + 2*std
    lower = mid - 2*std
    width = upper - lower or 1
    return (closes[-1] - lower) / width


def detect_patterns(klines):
    found = []
    if len(klines) < 3:
        return found

    def c(k):
        o, h, l, cl = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        body = abs(cl - o)
        rng  = h - l or 1e-9
        return o, h, l, cl, body, rng, h-max(o,cl), min(o,cl)-l

    o1,h1,l1,c1,b1,r1,u1,lo1 = c(klines[-3])
    o2,h2,l2,c2,b2,r2,u2,lo2 = c(klines[-2])
    o3,h3,l3,c3,b3,r3,u3,lo3 = c(klines[-1])

    if lo3 > b3*2 and u3 < b3*0.3 and c3 > o3:
        found.append("hammer")
    if c2 < o2 and c3 > o3 and c3 > o2 and o3 < c2:
        found.append("bullish_engulfing")
    if c1 < o1 and b2 < b1*0.3 and c3 > o3 and c3 > (o1+c1)/2:
        found.append("morning_star")
    if c1 > o1 and c2 > o2 and c3 > o3 and c3 > c2 > c1:
        found.append("three_soldiers")
    if b3/r3 > 0.85 and c3 > o3:
        found.append("strong_bull")
    return found


def gather_technical_data(symbol: str) -> dict | None:
    """يجمع كل البيانات الفنية لرمز واحد"""
    try:
        ticker = client.futures_ticker(symbol=symbol)
        vol24  = float(ticker.get("quoteVolume", 0))
        price  = float(ticker["lastPrice"])

        if vol24 < MIN_24H_QUOTE_VOLUME or price <= 0:
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

        avg_vol   = sum(vo15[-20:]) / 20 or 1
        vol_ratio = vo15[-1] / avg_vol

        return {
            "symbol":       symbol,
            "price":        price,
            "vol_24h":      vol24,
            "vol_ratio":    round(vol_ratio, 2),
            "rsi_15m":      compute_rsi(cl15),
            "rsi_1h":       compute_rsi(cl1h),
            "macd_15m":     compute_macd_bull(cl15),
            "macd_4h":      compute_macd_bull(cl4h),
            "bb_pct":       compute_bb_pct(cl15),
            "atr":          compute_atr(hi15, lo15, cl15),
            "ema_trend":    ema_calc(cl1h, 20) > ema_calc(cl1h, 50),
            "above_ema200": cl1h[-1] > ema_calc(cl1h, 200) * 0.98,
            "patterns":     detect_patterns(kl15),
        }
    except Exception as e:
        if "-1022" not in str(e):
            log.warning(f"gather_data {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  CLOSE
# ══════════════════════════════════════════════════════════════

def market_close(symbol: str, qty: float) -> bool:
    qty = abs(qty)
    if qty <= 0:
        return False
    cancel_protection_orders(symbol)
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

                current = get_current_price(symbol)
                if current <= 0:
                    continue

                if trade.duration_hours() >= MAX_TRADE_HOURS:
                    _execute_close(symbol, trade, current, "timeout")
                    continue

                event = trade.update(current)

                if event == "sl_hit":
                    _execute_close(symbol, trade, current, "sl_internal")
                elif event == "tp_hit":
                    _execute_close(symbol, trade, current, "tp_internal")
                elif event == "breakeven":
                    send_telegram(
                        f"🔒 *Breakeven: {symbol}*\n"
                        f"سعر:`{current:.6f}` SL:`{trade.trail_sl:.6f}`\n"
                        f"P&L:`+{trade.pnl_pct(current):.2f}%`"
                    )
                elif event == "trailing_move":
                    send_telegram(
                        f"📈 *Trailing ↑ {symbol}*\n"
                        f"سعر:`{current:.6f}` SL:`{trade.trail_sl:.6f}`\n"
                        f"P&L:`+{trade.pnl_pct(current):.2f}%`"
                    )
                elif event == "pyramid_trigger":
                    _execute_pyramid(symbol, trade, current)

                # تحقق من حماية بايننس كل 5 ثوان
                verify_binance_protection(symbol, trade)

        except Exception as e:
            log.error(f"protection_monitor: {e}")

        time.sleep(5)


def _execute_close(symbol, trade, current_price, reason):
    amt, _ = get_actual_position(symbol)
    if abs(amt) < 1e-8:
        open_trades.pop(symbol, None)
        return

    ok = market_close(symbol, abs(amt))
    if ok:
        open_trades.pop(symbol, None)
        pnl   = trade.pnl_pct(current_price)
        emoji = "🟢" if pnl >= 0 else "🔴"
        balance = get_futures_balance()
        record_trade(trade, current_price, balance)

        labels = {
            "sl_internal": "SL داخلي ⛔",
            "tp_internal": "TP داخلي 💰",
            "timeout":     f"Timeout {MAX_TRADE_HOURS}h ⏰",
        }
        send_telegram(
            f"{emoji} *مُغلقة: {symbol}*\n"
            f"السبب: {labels.get(reason, reason)}\n"
            f"دخول:`{trade.entry:.6f}` → خروج:`{current_price:.6f}`\n"
            f"P&L:`{pnl:+.2f}%` (×{LEVERAGE})\n"
            f"المدة:`{trade.duration_hours():.1f}h`\n"
            f"─────────────────\n"
            f"Win%:`{learning['win_rate']*100:.1f}%` "
            f"Risk:`{learning['current_risk_pct']*100:.1f}%` "
            f"Comp:`×{learning['compounding_mult']:.2f}`\n"
            f"💰 رصيد:`{balance:.2f}` USDT"
        )
    else:
        send_telegram(f"🚨 *فشل إغلاق {symbol}* — راجع يدوياً!")


def _handle_closed_externally(symbol, trade):
    open_trades.pop(symbol, None)
    current = get_current_price(symbol)
    if current > 0:
        pnl     = trade.pnl_pct(current)
        balance = get_futures_balance()
        record_trade(trade, current, balance)
        emoji = "🟢" if pnl >= 0 else "🔴"
        send_telegram(
            f"{emoji} *مُغلقة (بايننس): {symbol}*\n"
            f"P&L:`{pnl:+.2f}%` | رصيد:`{balance:.2f}`"
        )


def _execute_pyramid(symbol, trade, current_price):
    if trade.pyramid_adds >= PYRAMID_MAX_ADDS:
        return
    avail = get_available_margin()
    if avail < 5.0:
        return

    add_qty = round_qty(symbol, trade.qty * PYRAMID_SIZE_RATIO)
    if add_qty <= 0:
        return
    _, _, min_notional = get_filters(symbol)
    if add_qty * current_price < min_notional:
        return

    try:
        client.futures_create_order(
            symbol=symbol, side=SIDE_BUY,
            type=ORDER_TYPE_MARKET, quantity=add_qty,
        )
        time.sleep(1.0)
        amt, _ = get_actual_position(symbol)
        if abs(amt) < 1e-8:
            return

        if not trade.at_breakeven:
            trade.trail_sl    = trade.entry * 1.001
            trade.at_breakeven = True

        trade.pyramid_adds += 1
        place_binance_protection(symbol, trade.entry, abs(amt))

        send_telegram(
            f"🔺 *Pyramid L{trade.pyramid_adds}: {symbol}*\n"
            f"إضافة:`{add_qty}` @ `{current_price:.6f}`\n"
            f"P&L:`+{trade.pnl_pct(current_price):.2f}%`"
        )
    except Exception as e:
        log.error(f"pyramid {symbol}: {e}")


# ══════════════════════════════════════════════════════════════
#  OPEN POSITION
# ══════════════════════════════════════════════════════════════

def open_long(symbol: str, data: dict, claude_result: dict) -> bool:
    amt, _ = get_actual_position(symbol)
    if abs(amt) > 1e-8 or len(open_trades) >= MAX_OPEN_TRADES:
        return False

    price = data["price"]
    atr   = data["atr"]

    try:
        _, _, min_notional = get_filters(symbol)
        balance = get_futures_balance()
        avail   = get_available_margin()

        effective_risk = get_effective_risk()
        sl_distance    = atr * learning["atr_sl"]
        sl_pct         = sl_distance / price if price > 0 else 0.02

        qty_by_risk  = (balance * effective_risk) / (price * sl_pct)
        qty_by_avail = (avail * 0.85 * LEVERAGE) / price
        raw_qty      = min(qty_by_risk, qty_by_avail)
        qty          = round_qty(symbol, raw_qty)

        if qty <= 0 or qty * price < min_notional:
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
            return False

        actual_qty   = abs(actual_amt)
        actual_entry = actual_entry or price

        trade = TradeState(
            symbol         = symbol,
            entry          = actual_entry,
            qty            = actual_qty,
            atr            = atr,
            rsi            = data["rsi_15m"],
            reasons        = data.get("patterns", []),
            claude_analysis = claude_result.get("summary", ""),
        )
        open_trades[symbol] = trade

        bn_ok = place_binance_protection(symbol, actual_entry, actual_qty)
        conf  = claude_result.get("confidence", 0)

        send_telegram(
            f"🚀 *دخول {symbol}* (Claude {conf}%)\n"
            f"سعر:`{actual_entry:.6f}` | كمية:`{actual_qty}`\n"
            f"─── داخلي ───\n"
            f"SL:`{trade.sl_price:.6f}` | TP:`{trade.tp_price:.6f}`\n"
            f"─── بايننس ───\n"
            f"SL:`{round_price(symbol, actual_entry*(1-BN_SL_PCT))}` | "
            f"Trail:`{BN_TRAILING_CALLBACK}%`\n"
            f"{'✅ حماية بايننس' if bn_ok else '⚠️ راجع بايننس!'}\n"
            f"─────────────────\n"
            f"🤖 *Claude:* {claude_result.get('reason','')}\n"
            f"📊 {claude_result.get('summary','')}\n"
            f"─────────────────\n"
            f"⚙️ Risk:`{effective_risk*100:.1f}%` | Comp:`×{learning['compounding_mult']:.2f}`"
        )
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
            place_binance_protection(sym, entry, abs(amt))
            adopted += 1

    except Exception as e:
        log.error(f"adopt: {e}")

    msg = f"🔄 *تبنّي — {adopted} وضعية*\n"
    for sym, t in open_trades.items():
        msg += f"  • `{sym}` @ `{t.entry:.6f}`\n"
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
        cancel_protection_orders(sym)
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
        msg += f"مفتوحة:`{len(open_trades)}`\n"
        for sym, tr in open_trades.items():
            cp  = get_current_price(sym)
            pnl = tr.pnl_pct(cp)
            msg += f"  • `{sym}` P&L:`{pnl:+.2f}%`\n"
        send_telegram(msg)
    except Exception as e:
        log.error(f"daily_report: {e}")


# ══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════

def main_loop():
    global bot_start_balance, daily_start_balance, daily_reset_date, client

    log.info("🚀 بوت v6.0 — Claude AI Analyst")
    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
    load_learning()

    initial = get_futures_balance()
    if learning["peak_balance"] == 0:
        learning["peak_balance"] = initial

    bot_start_balance   = initial
    daily_start_balance = initial
    daily_reset_date    = utcnow().date()

    all_symbols = _get_all_symbols()

    threading.Thread(target=protection_monitor, daemon=True, name="ProtMon").start()

    send_telegram(
        f"🤖 *بوت v6.0 — Claude AI* ✅\n"
        f"رصيد:`{initial:.2f}` USDT\n"
        f"─── الحماية ───\n"
        f"داخلي: SL×`{learning['atr_sl']:.2f}` | BE`+{BREAKEVEN_TRIGGER_PCT*100:.1f}%`\n"
        f"بايننس: SL`{BN_SL_PCT*100:.0f}%` | Trail`{BN_TRAILING_CALLBACK}%`\n"
        f"─── الفلاتر ───\n"
        f"Vol_min:`{MIN_24H_QUOTE_VOLUME/1e6:.0f}M` | Claude_min:`{MIN_CLAUDE_CONFIDENCE}%`\n"
        f"عملات:`{len(all_symbols)}`"
    )

    adopt_existing_positions()
    update_market_filter()

    cycle = market_check_c = 0

    while True:
        cycle          += 1
        market_check_c += 1

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

            if market_check_c >= 20:
                update_market_filter()
                market_check_c = 0

            if not check_protection(balance):
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            if not _market_is_bull:
                log.info("🔴 هابط — توقف")
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            if avail < 3.0 or len(open_trades) >= MAX_OPEN_TRADES:
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            if cycle % 100 == 0:
                _all_symbols_cache.clear()
                all_symbols = _get_all_symbols()

            # ── جمع البيانات + تحليل Claude ───────────────
            for sym in all_symbols:
                if len(open_trades) >= MAX_OPEN_TRADES:
                    break
                if get_available_margin() < 3.0:
                    break
                if sym in open_trades or is_blacklisted(sym):
                    continue

                # جمع البيانات الفنية
                data = gather_technical_data(sym)
                if data is None:
                    continue

                # فلتر أولي قبل إرسال لـ Claude (يوفر API calls)
                if data["rsi_15m"] > 75:
                    continue
                if not data["macd_15m"] and not data["macd_4h"]:
                    continue

                # Claude يحلل ويقرر
                claude_result = claude_analyze(sym, data)

                if claude_result is None:
                    continue
                if claude_result.get("direction") != "LONG":
                    log.info(f"🤖 Claude → {sym}: {claude_result.get('direction')} — تخطي")
                    continue
                if claude_result.get("confidence", 0) < MIN_CLAUDE_CONFIDENCE:
                    log.info(
                        f"🤖 Claude → {sym}: ثقة {claude_result.get('confidence')}% < {MIN_CLAUDE_CONFIDENCE}% — تخطي"
                    )
                    continue

                log.info(
                    f"🤖 Claude ✅ {sym}: {claude_result.get('confidence')}% — "
                    f"{claude_result.get('reason','')}"
                )
                if open_long(sym, data, claude_result):
                    time.sleep(2)

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
    bull = "🟢 صاعد" if _market_is_bull else "🔴 هابط"
    lines = [
        f"<b>🤖 Bot v6.0 — Claude AI</b> | {bull}",
        f"رصيد: {bal:.2f} USDT | مفتوحة: {len(open_trades)}/{MAX_OPEN_TRADES}",
        f"Win%: {learning['win_rate']*100:.1f}% ({learning['total_trades']} صفقة)",
        f"Risk: {learning['current_risk_pct']*100:.1f}% | Comp: ×{learning['compounding_mult']:.2f}",
        f"حماية: داخلية + بايننس (SL {BN_SL_PCT*100:.0f}% | Trail {BN_TRAILING_CALLBACK}%)",
        "<hr>",
    ]
    for sym, t in open_trades.items():
        cp  = get_current_price(sym)
        pnl = t.pnl_pct(cp)
        flags = []
        if t.at_breakeven:    flags.append("🔒BE")
        if t.trailing_active: flags.append("📈Trail")
        if t.pyramid_adds:    flags.append(f"🔺×{t.pyramid_adds}")
        lines.append(
            f"• <b>{sym}</b> @ {t.entry:.6f} | P&L: {pnl:+.2f}% | "
            f"SL: {t.trail_sl:.6f} | {' '.join(flags)}"
        )
        if t.claude_analysis:
            lines.append(f"  🤖 {t.claude_analysis}")
    return "<br>".join(lines)


@app.route("/trades")
def trades_route():
    result = {}
    for sym, t in open_trades.items():
        cp = get_current_price(sym)
        result[sym] = {
            "entry":         t.entry,
            "current":       cp,
            "pnl_pct":       round(t.pnl_pct(cp), 2),
            "sl_internal":   round(t.trail_sl, 8),
            "tp_internal":   round(t.tp_price, 8),
            "breakeven":     t.at_breakeven,
            "trailing":      t.trailing_active,
            "pyramid_adds":  t.pyramid_adds,
            "hours":         round(t.duration_hours(), 2),
            "claude":        t.claude_analysis,
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
