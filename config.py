"""
إعدادات البوت - عدّل هذه القيم حسب احتياجاتك
"""
import os

# Binance API
# احصل على المفاتيح من: https://www.binance.com/en/my/settings/api-management
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# Telegram Bot
# أنشئ بوت من: https://t.me/BotFather
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# إعدادات التداول
TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", "10"))  # مبلغ كل صفقة بالدولار
TIMEFRAME = os.getenv("TIMEFRAME", "15m")  # الإطار الزمني
SYMBOLS = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,MATICUSDT,DOTUSDT,LTCUSDT").split(",")
CYCLE_INTERVAL = int(os.getenv("CYCLE_INTERVAL", "60"))  # الفاصل بين كل دورة (بالثواني)

# إعدادات المؤشرات الفنية
MIN_RSI = 30  # الحد الأدنى لـ RSI للشراء
MAX_RSI = 70  # الحد الأعلى لـ RSI للبيع
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 20

# إعدادات Stop Loss و Trailing Stop
STOP_LOSS_PERCENT = 2.0  # نسبة Stop Loss (2%)
TAKE_PROFIT_PERCENT = 10.0  # نسبة جني الأرباح (10%)
TRAILING_STOP_PERCENT = 1.5  # نسبة Trailing Stop (1.5%)

# إعدادات عامة
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"  # وضع الاختبار - تأكد أنه False للتداول الحقيقي
