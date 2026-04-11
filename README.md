# 🤖 بوت التداول الآلي على Binance

## 📋 الفهرس
1. [المميزات](#-المميزات)
2. [قبل البدء - تحذير مهم](#-تحذير-مهم)
3. [الخطوة 1: إنشاء API على Binance](#الخطوة-1-إنشاء-api-على-binance)
4. [الخطوة 2: إنشاء بوت Telegram](#الخطوة-2-إنشاء-بوت-telegram)
5. [الخطوة 3: إعداد GitHub](#الخطوة-3-إعداد-github)
6. [الخطوة 4: النشر على Railway (مجاني)](#الخطوة-4-النشر-على-railway-مجاني)
7. [الخطوة 5: إعداد المتغيرات البيئية](#الخطوة-5-إعداد-المتغيرات-البيئية)
8. [التحقق من التشغيل](#التحقق-من-التشغيل)
9. [الإعدادات المتقدمة](#-الإعدادات-المتقدمة)
10. [حل المشاكل](#-حل-المشاكل)

---

## 🎯 المميزات

| الميزة | الوصف |
|--------|-------|
| 📊 **تحليل فني شامل** | RSI, MACD, Bollinger Bands, المتوسطات المتحركة |
| 🎯 **اختيار ذكي** | يختار أفضل العملات بناءً على المؤشرات |
| 🛒 **شراء تلقائي** | يشتري العملة الواعدة فوراً |
| 📈 **وقف خسارة متحرك** | Trailing Stop يحمي أرباحك |
| 💰 **جني الأرباح** | يبيع عند ربح 10% |
| 📱 **إشعارات فورية** | تنبيهات Telegram لكل عملية |
| ⚡ **يعمل 24/7** | على خادم سحابي بدون توقف |

---

## ⚠️ تحذير مهم

> **⚠️ تحذير: هذا البوت للتعليم والاختبار فقط!**
>
> - التداول ينطوي على مخاطر عالية
> - قد تخسر كل رأس المال
> - **لا تستخدم أموال حقيقية** في البداية
> - اختبر البوت لمدة شهر على الأقل في وضع الاختبار
> - استثمر فقط ما يمكنك تحمل خسارته

---

## الخطوة 1: إنشاء API على Binance

### 1.1 إنشاء API Key

1. **سجل دخول** على [Binance.com](https://www.binance.com)
2. **اذهب إلى** Dashboard → API Management
3. **اضغط** "Create API"
4. **اختر** "System generated"
5. **امنح اسماً** للبوت (مثال: `TradingBot`)

### 1.2 إعداد الصلاحيات

بعد إنشاء الـ API:

| الصلاحية | التفعيل | السبب |
|----------|---------|-------|
| Enable Spot & Margin Trading | ✅ نعم | للشراء والبيع |
| Enable Futures | ❌ لا | لا تحتاجه |
| Enable Withdrawal | ❌ لا | **خطر أمني!** |

### 1.3 نسخ المفاتيح

ستحصل على:
```
API Key:    XXXXXXXXXXXXXXXXXXXXXXXX
Secret Key: XXXXXXXXXXXXXXXXXXXXXXXX
```

**⚠️ احفظهم في مكان آمن!**

---

## الخطوة 2: إنشاء بوت Telegram

### 2.1 إنشاء البوت

1. **افتح Telegram** وابحث عن **@BotFather**
2. **أرسل** `/newbot`
3. **اختر اسماً** للبوت (مثال: `My Trading Bot`)
4. **اختر username** (ينتهي بـ bot، مثال: `MyTradingBot_alerts`)
5. **احفظ الـ Token** الذي ستحصل عليه

### 2.2 الحصول على Chat ID

1. **ابحث عن** @userinfobot على Telegram
2. **أرسل** أي رسالة
3. **احصل على** Chat ID الخاص بك

---

## الخطوة 3: إعداد GitHub

### 3.1 إنشاء Repository

1. **اذهب إلى** [GitHub.com](https://github.com)
2. **اضغط** "New repository"
3. **الاسم:** `binance-trading-bot`
4. **الخصوصية:** Public
5. **اضغط** "Create repository"

### 3.2 رفع الملفات

**الطريقة 1: من المتصفح**
1. اذهب إلى Repository الجديد
2. اضغط "Add file" → "Upload files"
3. ارفع **جميع ملفات** المشروع

**الطريقة 2: من GitHub Desktop**
```bash
# نسخ المشروع
git clone https://github.com/YOUR_USERNAME/binance-trading-bot.git
cd binance-trading-bot
# (انسخ ملفات البوت هنا)
git add .
git commit -m "Initial commit"
git push
```

---

## الخطوة 4: النشر على Railway (مجاني)

Railway يوفر **$5 شهرياً مجاناً** يكفي لتشغيل البوت.

### 4.1 إنشاء حساب

1. اذهب إلى [Railway.app](https://railway.app)
2. **سجل دخول** بـ GitHub
3. ستحصل على **$5 مجاناً**

### 4.2 نشر المشروع

1. **اضغط** "New Project"
2. **اختر** "Deploy from GitHub repo"
3. **اختر** repository البوت
4. Railway سيكتشف أنه Python تلقائياً

### 4.3 تشغيل البوت

1. اذهب إلى **Settings** → **Start Command**
2. اكتب:
   ```
   python main.py
   ```
3. **اضغط** "Deploy"

---

## الخطوة 5: إعداد المتغيرات البيئية

### 5.1 في Railway

1. اذهب إلى **Variables** في مشروعك
2. **أضف المتغيرات التالية:**

| المتغير | القيمة |
|---------|--------|
| `BINANCE_API_KEY` | مفتاح API من Binance |
| `BINANCE_API_SECRET` | مفتاح السر من Binance |
| `TELEGRAM_BOT_TOKEN` | Token بوت Telegram |
| `TELEGRAM_CHAT_ID` | Chat ID الخاص بك |
| `TEST_MODE` | `False` (لتداول حقيقي) |

### 5.2 تحديث الكود لاستخدام المتغيرات

عدّل ملف `config.py`:

```python
import os

# API Binance
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY_HERE")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_BINANCE_API_SECRET_HERE")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

# وضع الاختبار
TEST_MODE = os.getenv("TEST_MODE", "True").lower() == "true"
```

---

## التحقق من التشغيل

### من Telegram
- ستصلك رسالة: "🟢 تم تشغيل بوت التداول الآلي!"
- كل ساعة ستصلك نبضة قلب تؤكد أن البوت يعمل

### من Railway
1. اذهب إلى مشروعك
2. اضغط على **Deployments**
3. سترى البوت يعمل

### من Logs
1. في Railway، اذهب إلى **Logs**
2. سترى كل العمليات:
   ```
   🔄 الدورة #1 - 2024-01-15 10:00:00
   📊 جاري تحليل السوق...
   🏆 أفضل اختيار: BTCUSDT
   ✅ تم فتح المركز!
   ```

---

## 📊 الإعدادات المتقدمة

### ملف `config.py`

```python
# ═══════════════════════════════════════════════════════
# إعدادات التداول
# ═══════════════════════════════════════════════════════

# مبلغ الاستثمار لكل صفقة (بالدولار)
TRADE_AMOUNT_USD = 10.0

# وقف الخسارة (%)
STOP_LOSS_PERCENT = 2.0

# جني الأرباح (%)
TAKE_PROFIT_PERCENT = 10.0

# وقف الخسارة المتحرك
TRAILING_STOP_ENABLED = True
TRAILING_STOP_PERCENT = 1.5

# الأزواج المراد تحليلها
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "MATICUSDT", "DOTUSDT", "LTCUSDT"
]

# ═══════════════════════════════════════════════════════
# إعدادات التحليل الفني
# ═══════════════════════════════════════════════════════

# الإطار الزمني: 1m, 3m, 5m, 15m, 1h, 4h, 1d
TIMEFRAME = "15m"

# RSI
RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_OVERBROUGHT = 70

# MACD
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
```

---

## 🔧 حل المشاكل

### ❌ خطأ "Invalid API Key"
- تأكد من صحة API Key
- تأكد من تفعيل صلاحيات التداول

### ❌ خطأ "Connection Error"
- تأكد من اتصال الإنترنت
- Railway قد يحتاج إعادة تشغيل

### ❌ لا تأتي إشعارات Telegram
- تأكد من صحة Bot Token
- تأكد من صحة Chat ID
- أرسل رسالة للبوت أولاً

### ❌ البوت لا يعمل على Railway
1. تحقق من Logs
2. تأكد من متغيرات البيئة
3. أعد النشر

---

## 📞 التواصل والدعم

- **قناة التحديثات:** تابع البوت على Railway للتحديثات
- **النسخ الاحتياطي:** احفظ نسخة من الإعدادات

---

## 📄 الملفات في المشروع

```
binance-trading-bot/
├── config.py              # الإعدادات
├── main.py                # الملف الرئيسي
├── binance_client.py      # اتصال Binance
├── technical_analysis.py  # التحليل الفني
├── trading_manager.py     # إدارة الصفقات
├── telegram_notifier.py   # إشعارات Telegram
├── requirements.txt       # المتطلبات
├── Dockerfile             # لتشغيل Docker
├── Procfile               # لـ Railway
└── README.md              # هذا الملف
```

---

## ⚖️ تنبيه قانوني

هذا البوت **لأغراض تعليمية فقط**. المؤلف غير مسؤول عن أي خسارة مالية. استخدمه على مسؤوليتك الخاصة.

---

**🚀 نجاح!** الآن لديك بوت تداول يعمل 24/7
