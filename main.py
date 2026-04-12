"""
بوت التداول الآلي الذكي v3
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

# إعدادات Flask
app = Flask(__name__)
app.logger.disabled = True
import logging
log = logging.getLogger('werkzeug')
log.disabled = True

TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

binance = BinanceClient(API_KEY, API_SECRET, testnet=TEST_MODE)
telegram = TelegramNotifier()
ta = TechnicalAnalysis(binance)
trading_manager = TradingManager(binance, telegram)

print("=" * 60)
print("🤖 بوت التداول الذكي v3")
print("=" * 60)
sys.stdout.flush()

TARGET_SYMBOLS = ["ADAUSDT", "DOGEUSDT", "SHIBUSDT", "1000SHIBUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "LTCUSDT", "ETHUSDT", "BTCUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT", "LINKUSDT", "ATOMUSDT", "UNIUSDT"]

def scan_and_trade():
    cycle = 0
    while True:
        cycle += 1
        print(f"\n🔄 الدورة #{cycle}")
        sys.stdout.flush()
        
        try:
            positions = trading_manager.get_all_positions()
            if positions:
                for pos in positions:
                    trading_manager.monitor_position(pos)
            else:
                print("🔍 البحث عن فرص...")
                best = None
                best_score = 0
                
                for symbol in TARGET_SYMBOLS:
                    try:
                        market_open, price = binance.check_market_status(symbol)
                        if not market_open:
                            continue
                        signal = ta.analyze(symbol)
                        if signal and signal['action'] == 'buy':
                            score = signal.get('score', 0)
                            print(f"  ✅ {symbol}: {score}")
                            if score > best_score:
                                best_score = score
                                best = (symbol, signal)
                        time.sleep(0.3)
                    except:
                        continue
                
                if best and best_score >= 40:
                    symbol, _ = best
                    print(f"🏆 أفضل فرصة: {symbol}")
                    trading_manager.open_position(symbol)
            
            if cycle % 5 == 0:
                trading_manager.clear_failed_symbols()
            
            time.sleep(60)
        except Exception as e:
            print(f"خطأ: {e}")
            sys.stdout.flush()
            time.sleep(30)

@app.route('/')
def home():
    return {'status': 'running', 'bot': 'Smart v3'}

@app.route('/health')
def health():
    return {'status': 'healthy'}

if __name__ == "__main__":
    print("🚀 بدء التشغيل...")
    sys.stdout.flush()
    
    t = threading.Thread(target=scan_and_trade, daemon=True)
    t.start()
    
    time.sleep(2)
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False, threaded=True)
