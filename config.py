"""
إعدادات البوت
"""
import os

# Binance API
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# Telegram Bot
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# إعدادات التداول
TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", "20"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))

# إعدادات المؤشرات
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Stop Loss و Trailing Stop
STOP_LOSS_PERCENT = 2.0
TAKE_PROFIT_PERCENT = 10.0
TRAILING_STOP_PERCENT = 1.5

# عامة
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
