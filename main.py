"""
بوت التداول الآلي
"""
import time
import threading
import os
from flask import Flask
from binance_client import BinanceClient
from technical_analysis import TechnicalAnalysis
from telegram_notifier import TelegramNotifier
from trading_manager import TradingManager
import config

app = Flask(__name__)

TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

binance = BinanceClient(API_KEY, API_SECRET, testnet=TEST_MODE)
telegram = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, enabled=TELEGRAM_ENABLED)
ta = TechnicalAnalysis()
trading_manager = TradingManager(binance, telegram)

print("=" * 50)
print("بوت التداول الآلي")
print(f"وضع الاختبار: {'نعم' if TEST_MODE else 'لا'}")
print("=" * 50)

TARGET_SYMBOLS = ["ADAUSDT", "DOGEUSDT", "SHIBUSDT", "1000SHIBUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "LTCUSDT", "ETHUSDT", "BTCUSDT"]

def scan_and_trade():
    while True:
        try:
            print("\n🔍 جاري المسح...")
            for symbol in TARGET_SYMBOLS:
                print(f"\n📊 تحليل {symbol}...")
                market_open, _ = binance.check_market_status(symbol)
                if not market_open:
                    print(f"⚠️ {symbol} - السوق مغلق")
                    continue
                signal = ta.analyze(symbol)
                if signal and signal['action'] == 'buy':
                    print(f"✅ إشارة شراء في {symbol}!")
                    trading_manager.open_position(symbol)
            time.sleep(60)
        except Exception as e:
            print(f"خطأ: {e}")
            time.sleep(30)

@app.route('/')
def home():
    return {'status': 'running'}

@app.route('/health')
def health():
    return {'status': 'healthy'}

if __name__ == "__main__":
    print("🚀 بدء التشغيل...")
    t = threading.Thread(target=scan_and_trade, daemon=True)
    t.start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
