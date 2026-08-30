# 🤖 Binance Futures Trading Bot — v11

بوت تداول آلي على **Binance USDT-M Futures** (ليس Spot).

> ⚠️ **تحذير:** يتداول بمال حقيقي فور تشغيله. **لا يوجد وضع تجريبي.**
> للتجربة الآمنة استخدم مفاتيح **Binance Futures Testnet**.

---

## الاستراتيجية (كما ينفّذها الكود فعلياً)

| الطبقة | الشرط |
|---|---|
| 1. الاتجاه (1h) | `EMA9 > EMA21 > EMA50` للـ long، والعكس للـ short |
| 2. التأكيد (15m) | `EMA9/EMA21` بنفس الاتجاه + السعر ليس في طرف النطاق |
| 3. RSI (1h) | لا long فوق 78، لا short تحت 22 |
| 4. الحجم | ≥ متوسط آخر 20 شمعة |
| 5. Sentiment | Fear & Greed لا يعاكس الاتجاه |
| 6. اتجاه BTC | سوق صاعد → long فقط، هابط → short فقط |
| 7. النقاط | `score ≥ 60` |

**لا يوجد Breakout في منطق الدخول.** النسخ السابقة ادّعت ذلك في التوثيق بينما الكود يستخدم تأكيد EMA فقط.

## إدارة الصفقة

- `SL = ATR × 1.5` و `TP = ATR × 3.0` → **أمران حقيقيان على Binance** (`closePosition=true`)
- Breakeven عند `+0.8%` ثم Trailing عند `+1.5%` — الـ SL يتحرك على البورصة لا في الذاكرة فقط
- إغلاق إجباري بعد 10 ساعات
- رافعة ثابتة `5x` | 3 صفقات مفتوحة كحد أقصى | 12 صفقة يومياً
- حدود الخسارة: `5%` يومياً و`12%` إجمالياً — **محسوبة على الرصيد شاملاً الخسائر المفتوحة**

## القياس

الربح يُقرأ من `futures_income()` وليس من فرق السعر:
`REALIZED_PNL + COMMISSION + FUNDING_FEE` → **صافي بعد الرسوم**.
صفقة تربح `+0.05%` سعرياً تُسجَّل خسارة إن أكلتها العمولات.
لو تعذّر القياس، تُعلَّم الصفقة بـ `src: "تقديري"` ويزيد عدّاد `unmeasured`.

---

## التشغيل

### 1. مفاتيح Binance
Settings → API Management → أنشئ مفتاحاً مع **Enable Futures**.
قيّد المفتاح بعنوان IP الخادم. **لا تفعّل صلاحية السحب.**

### 2. متغيرات البيئة

| المتغير | إلزامي | الوصف |
|---|---|---|
| `BINANCE_API_KEY` | ✅ | مفتاح Futures |
| `BINANCE_API_SECRET` | ✅ | السر |
| `DATA_DIR` | ⭐ | مسار قرص دائم (`/var/data`). بدونه تُمسح البيانات مع كل deploy |
| `TELEGRAM_TOKEN` | — | من @BotFather |
| `TELEGRAM_CHAT_ID` | — | من @userinfobot |
| `TV_SECRET` | — | بدونه يُعطَّل ويبهوك TradingView |
| `MARKET_FILTER` | — | `false` لتعطيل فلتر اتجاه BTC (افتراضي `true`) |
| `MAX_DAILY_TRADES` | — | افتراضي `12` |
| `PORT` | — | افتراضي `10000` |

### 3. النشر

**استضافة مجانية 24/7:** اتبع [`DEPLOY.md`](DEPLOY.md) — Oracle Cloud (منطقة جدة/الرياض) أو جهاز في البيت.

تثبيت بأمر واحد على خادم Ubuntu نظيف:
```bash
curl -sL https://raw.githubusercontent.com/fpi9965/binance-trading-bot/main/setup.sh | bash
```
ثم `bash check.sh` للتحقق من الحالة.

⚠️ **Binance يحظر عناوين IP الأمريكية** (خطأ 451). اختبر قبل أي تثبيت:
```bash
curl -s https://fapi.binance.com/fapi/v1/ping
```

على Render: Start Command `python main.py` + Persistent Disk. الخطة المجانية تنام بعد 15 دقيقة.
إن استخدمت gunicorn فليكن `--workers 1`، وإلا شُغّل بوت مستقل لكل عامل.

### 4. التحقق بعد أول صفقة

على Binance يجب أن ترى **أمرين معلّقين** لكل صفقة: `STOP_MARKET` + `TAKE_PROFIT_MARKET`.
أمر واحد فقط = خلل، راجع السجلات.

---

## المراقبة

| المسار | المحتوى |
|---|---|
| `/` | الصفقات المفتوحة، الصافي، الرسوم |
| `/trades` | تفاصيل JSON لكل صفقة مفتوحة |
| `/stats` | `net_usdt`, `fees_usdt`, `fees_pct_of_gross`, `avg_net_per_trade`, `unmeasured` |
| `/webhook` | إغلاق يدوي من TradingView (`direction: "close"` فقط) |

**راقب `fees_pct_of_gross`.** تجاوزه 30% يعني أن المشكلة في كثرة الصفقات وقصر مدتها، لا في جودة الإشارات.

الفتح عبر الويبهوك **غير مدعوم عمداً** — يتجاوز كل فلاتر المخاطر.

---

## بعد إعادة التشغيل

`recover_trades()` تعمل عند كل إقلاع. **مصدر الحقيقة هو Binance لا الملف:**
تقرأ الوضعيات المفتوحة فعلياً، تعيد بناء حالة كل صفقة، وتضع SL/TP إن نقصا.

---

## الملفات

```
main.py           البوت كاملاً (ملف واحد)
setup.sh          تثبيت بأمر واحد على Ubuntu
check.sh          فحص حالة البوت على الخادم
DEPLOY.md         دليل الاستضافة المجانية
requirements.txt
Procfile          web: python main.py
runtime.txt       python-3.11.9
Dockerfile
```

ملفات النسخة القديمة (`config.py`, `binance_client.py`, `trading_manager.py`,
`technical_analysis.py`, `telegram_notifier.py`) كانت لبوت **Spot** مختلف تماماً،
ولم يكن `main.py` يستوردها. حُذفت. موجودة في تاريخ git إن احتجتها.

---

## تحذيرات

1. ابدأ بأصغر مبلغ ممكن، وراقب أول 10 صفقات يدوياً
2. فعّل 2FA وقيّد مفتاح API بـ IP
3. لا تتدخّل يدوياً على عملة لديها صفقة مفتوحة — يفسد قياس الربح
4. التداول بالرافعة ينطوي على خسارة كاملة لرأس المال
