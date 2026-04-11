"""
بوت التداول الآلي - النهائي
- يجرب جميع العملات المتاحة
- يتجاوز العملات المغلفة
- يفتح عدة صفقات
"""
import time
import threading
import os
import sys
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
ta = TechnicalAnalysis(binance)
trading_manager = TradingManager(binance, ta, telegram)

print("=" * 50)
print("بوت التداول الآلي - التشغيل")
print("=" * 50)
print(f"وضع الاختبار: {'نعم' if TEST_MODE else 'لا'}")
print(f"إشعارات تيليجرام: {'مفعلة' if TELEGRAM_ENABLED else 'معطلة'}")
print(f"الحد الأقصى من الصفقات: {config.MAX_POSITIONS}")
print("=" * 50)

TARGET_SYMBOLS = ["MATICUSDT", "ADAUSDT", "DOGEUSDT", "SHIBUSDT", "1000SHIBUSDT"]

def get_open_symbols():
    try:
        positions = binance.get_open_positions()
        return [p['symbol'] for p in positions] if positions else []
    except:
        return []

def scan_and_trade():
    global TARGET_SYMBOLS
    while True:
        try:
            print("\n" + "=" * 50)
            print("🔍 جاري المسح...")

            open_symbols = get_open_symbols()
            print(f"الصفقات المفتوحة حالياً: {len(open_symbols)}")

            closed_symbols = trading_manager.get_recently_traded_symbols()
            excluded_symbols = set(open_symbols + closed_symbols + trading_manager.get_failed_symbols())

            for symbol in TARGET_SYMBOLS:
                if symbol in excluded_symbols:
                    print(f"⏭️ {symbol} - تم تخطيه")
                    continue

                print(f"\n📊 تحليل {symbol}...")

                market_open, _ = binance.check_market_status(symbol)
                if not market_open:
                    print(f"⚠️ {symbol} - السوق مغلق")
                    trading_manager.add_failed_symbol(symbol)
                    continue

                position = trading_manager.get_position(symbol)
                if position:
                    print(f"📈 {symbol} - متابعة الصفقة المفتوحة")
                    trading_manager.monitor_position(position)
                else:
                    signal = ta.analyze(symbol)
                    if signal and signal['action'] == 'buy':
                        print(f"✅ إشارة شراء قوية في {symbol}!")
                        success = trading_manager.open_position(symbol)
                        if success:
                            print(f"🎉 تم فتح صفقة في {symbol}")
                            excluded_symbols.add(symbol)
                    elif signal:
                        print(f"❌ لا يوجد إشارة شراء - {symbol}")
                    else:
                        print(f"⚠️ فشل في تحليل {symbol}")
                        trading_manager.add_failed_symbol(symbol)

            print("\n⏰ انتظار 60 ثانية...")
            time.sleep(60)

        except Exception as e:
            print(f"❌ خطأ في المسح: {e}")
            time.sleep(30)

@app.route('/')
def home():
    positions = trading_manager.get_all_positions()
    return {'status': 'running', 'active_positions': len(positions), 'test_mode': TEST_MODE}

@app.route('/health')
def health():
    return {'status': 'healthy', 'test_mode': TEST_MODE}

if __name__ == "__main__":
    print("🚀 بدء تشغيل البوت...")

    if not TEST_MODE:
        telegram.send_message("🚀 تم تشغيل بوت التداول الآلي!")

    scan_thread = threading.Thread(target=scan_and_trade, daemon=True)
    scan_thread.start()

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
