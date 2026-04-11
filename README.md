# 🤖 بوت التداول الآلي على Binance

بوت تداول تلقائي متطور يعمل على مدار الساعة، يقوم بتحليل العملات المشفرة وتنفيذ الصفقات تلقائياً.

## ✨ المميزات

- 📊 **تحليل فني متقدم**: RSI, MACD, Bollinger Bands, SMAs
- 🟢 **توصيات ذكية**: يختار أفضل العملات للشراء
- ⚡ **تنفيذ تلقائي**: يفتح ويغلق الصفقات تلقائياً
- 🛑 **إدارة المخاطر**:
  - Stop Loss تلقائي
  - Trailing Stop متحرك
  - جني الأرباح عند 10%
- 📱 **إشعارات Telegram**: تنبيهات فورية لكل العمليات
- 🔄 **يعمل 24/7**: على Render أو أي استضافة سحابية

---

## 📋 المتطلبات

1. **حساب Binance** مع:
   - API Key (مع صلاحية التداول)
   - API Secret

2. **بوت Telegram** مع:
   - Bot Token (من @BotFather)
   - Chat ID

3. **استضافة** (أحد الخيارات):
   - **Render.com** (موصى به) - لهاش paid plan
   - Railway
   - Heroku
   - أي VPS

---

## 🚀 خطوات التشغيل

### الخطوة 1: تحميل الملفات

حمّل جميع ملفات المشروع:
- `main.py`
- `config.py`
- `binance_client.py`
- `technical_analysis.py`
- `trading_manager.py`
- `telegram_notifier.py`
- `requirements.txt`

### الخطوة 2: إنشاء حساب Binance

1. اذهب إلى [Binance](https://www.binance.com)
2. سجل دخول → Settings → API Management
3. أنشئ API Key جديد
4. **مهم**: فعّل "Enable Spot & Margin Trading"
5. انسخ API Key و API Secret

### الخطوة 3: إنشاء بوت Telegram

1. افتح Telegram → ابحث عن @BotFather
2. أرسل `/newbot`
3. اختر اسم للبوت
4. احصل على Bot Token (شكله: `123456789:ABCdef...`)
5. الآن ابحث عن @userinfobot أو @getidsbot
6. أرسل أي رسالة واحصل على Chat ID (رقم مثل: `123456789`)

### الخطوة 4: رفع الملفات على GitHub

```bash
# 1. أنشئ مجلد جديد
mkdir crypto-trading-bot
cd crypto-trading-bot

# 2. أنشئ الملفات (أنشئ كل ملف بالكود المناسب)

# 3.初始化 Git
git init
git add .
git commit -m "Initial commit"

# 4. أنشئ مستودع على GitHub
# اذهب إلى github.com وأنشئ مستودع جديد

# 5. ارفع الكود
git remote add origin https://github.com/YOUR_USERNAME/crypto-trading-bot.git
git push -u origin main
```

### الخطوة 5: النشر على Render

#### 5.1: إنشاء Account على Render

1. اذهب إلى [Render.com](https://render.com)
2. سجّل باستخدام GitHub
3. **مهم**: اختر خطة **paid** (Eagle أو выше)
   - الخطة المجانية تنام بعد 15 دقيقة!

#### 5.2: إنشاء Web Service

1. اضغط **New +** → **Web Service**
2. اربط مستودع GitHub
3.填写 الإعدادات:
   - **Name**: `crypto-trading-bot`
   - **Region**: Singapore (الأقرب لك)
   - **Branch**: main
   - **Root Directory**: (اتركها فارغة)
   - **Runtime**: Python
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main.py`

#### 5.3: إضافة Environment Variables

اضغط **Environment** وأضف:

| المتغير | القيمة | ملاحظة |
|---------|--------|--------|
| `BINANCE_API_KEY` | مفتاح Binance API | من الخطوة 2 |
| `BINANCE_API_SECRET` | سر Binance API | من الخطوة 2 |
| `TELEGRAM_BOT_TOKEN` | رمز بوت Telegram | من الخطوة 3 |
| `TELEGRAM_CHAT_ID` | معرف المحادثة | من الخطوة 3 |
| `TEST_MODE` | `false` | **هام!** التعليق false للتداول الحقيقي |
| `TRADE_AMOUNT_USD` | `10` | مبلغ كل صفقة |
| `PORT` | `10000` | المنفذ المطلوب |
| `PYTHON_VERSION` | `3.11` | إصدار Python |

#### 5.4: النشر

اضغط **Create Web Service**

**مهم جداً**: تأكد من `TEST_MODE = false`!

---

## ⚠️ إعدادات مهمة

### ملف config.py

```python
# إعدادات التداول
TRADE_AMOUNT_USD = 10  # مبلغ كل صفقة بالدولار
TIMEFRAME = "15m"      # الإطار الزمني

# العملات المراد تحليلها
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
    "XRPUSDT", "ADAUSDT", "DOGEUSDT", "MATICUSDT"
]

# إدارة المخاطر
STOP_LOSS_PERCENT = 2.0      # 2% Stop Loss
TAKE_PROFIT_PERCENT = 10.0  # 10%جني الأرباح
TRAILING_STOP_PERCENT = 1.5  # 1.5% Trailing Stop
```

### متغير TEST_MODE

**هذا هو السبب الأكثر شيوعاً لعدم تنفيذ الصفقات!**

- `TEST_MODE = true` → فقط يرسل إشارات، لا ينفذ صفقات
- `TEST_MODE = false` → ينفذ صفقات حقيقية

**تأكد من:**
1. في `config.py`: `TEST_MODE = False` (حرف F كبير)
2. في متغيرات Render: `TEST_MODE = false` (حرف f صغير)

---

## 🔧oubleshooting (حل المشاكل)

### المشكلة: البوت يرسل إشارات لكن لا ينفذ صفقات

1. **تأكد من TEST_MODE = false** في Render
2. تأكد أن API Keys صحيحة
3. تأكد أن الرصيد كافي (USDT)
4. تحقق من logs على Render

### المشكلة: خطأ في API

```
BinanceAPIException: APIError(code=-2015): Invalid API-key...
```

- المفتاح غير صحيح
- أو المفتاح لا يملك صلاحية التداول

### المشكلة: Service keeps sleeping

- تحتاج **خطة مدفوعة** على Render
- الخطة المجانية تنام بعد 15 دقيقة

### المشكلة: لا توجد إشعارات Telegram

- تأكد من Bot Token الصحيح
- تأكد من Chat ID الصحيح
- جرب إرسال رسالة من BotFather للبوت

---

## 📊 كيف يعمل البوت

```
كل 60 ثانية:
1. فحص الرصيد
2. إذا توجد صفقة مفتوحة → مراقبة وتحديث Stop Loss
3. إذا لا توجد صفقة:
   a. تحليل جميع العملات
   b. إرسال إشارات BUY للتليقرام
   c. اختيار أفضل عملة
   d. فتح صفقة شراء
   e. انتظار حتى تتحقق الشروط
```

### شروط الشراء
- RSI < 45
- MACD إيجابي (MACD > Signal)
- السعر فوق المتوسطات المتحركة
- الدرجة ≥ 40/100

### شروط البيع
- الوصول لـ 10% ربح
- أو Stop Loss (2%)
- أو Trailing Stop (1.5%)

---

## 🛡️ تحذيرات أمان

⚠️ **مهم جداً**:
1. لا تشارك API Keys مع أحد
2. ابدأ بمبلغ صغير (مثل $10)
3. فعّل 2FA على حساب Binance
4. راقب البوت في الأيام الأولى
5. افهم المخاطر - التداول ينطوي على خسارة

---

## 📝 ملاحظات

- البوت يعمل على Binance Spot (ليس Futures)
- لا يستخدم Multi-Assets Mode
- يتطلب رصيد USDT للتداول

---

## 📞 المساعدة

إذا واجهت مشكلة:
1. تحقق من logs على Render
2. تأكد من جميع المتغيرات
3. تأكد أن TEST_MODE = false
4. تأكد من صحة API Keys

---

**صُنع بـ ❤️ للتداول الآلي**
