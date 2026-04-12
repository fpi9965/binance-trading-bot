"""بوت التداول الآلي الذكي v4 - محسن للفرص"""
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
app.logger.disabled = True

import logging
logging.getLogger('werkzeug').disabled = True

TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

binance = BinanceClient(API_KEY, API_SECRET, testnet=TEST_MODE)
telegram = TelegramNotifier()
ta = TechnicalAnalysis(binance)
trading_manager = TradingManager(binance, telegram)

print("=" * 60)
print("🤖 بوت التداول الذكي v4 - محسن!")
print("=" * 60)
sys.stdout.flush()

TARGET_SYMBOLS = [
    "ADAUSDT", "DOGEUSDT", "SHIBUSDT", "1000SHIBUSDT", "BNBUSDT", 
    "XRPUSDT", "SOLUSDT", "LTCUSDT", "ETHUSDT", "BTCUSDT", 
    "AVAXUSDT", "DOTUSDT", "MATICUSDT", "LINKUSDT", "ATOMUSDT", "UNIUSDT"
]


def scan_and_trade():
    cycle = 0
    while True:
        cycle += 1
        print(f"\n🔄 الدورة #{cycle}")
        sys.stdout.flush()
        
        try:
            positions = trading_manager.get_all_positions()
            
            if positions:
                print(f"📊 مراقبة {len(positions)} صفقة...")
                for pos in positions:
                    trading_manager.monitor_position(pos)
            else:
                print("🔍 البحث عن فرص شراء...")
                sys.stdout.flush()
                
                best_symbol = None
                best_signal = None
                best_score = 0
                
                for symbol in TARGET_SYMBOLS:
                    try:
                        market_open, price = binance.check_market_status(symbol)
                        if not market_open:
                            continue
                        
                        signal = ta.analyze(symbol)
                        
                        if signal and signal['score'] > best_score:
                            best_score = signal['score']
                            best_symbol = symbol
                            best_signal = signal
                            print(f"  📈 {symbol}: {signal['score']} نقطة", end="")
                            if 'reasons' in signal and signal['reasons']:
                                print(f" - {' | '.join(signal['reasons'][:2])}")
                            else:
                                print()
                        else:
                            score = signal['score'] if signal else 0
                            print(f"  ⚪ {symbol}: {score} نقطة")
                        
                        time.sleep(0.5)
                        
                    except:
                        continue
                
                # فتح صفقة إذا كانت النقاط >= 50
                if best_symbol and best_score >= 50:
                    print(f"\n🎯 فرصة: {best_symbol} ({best_score} نقطة)")
                    success = trading_manager.open_position(best_symbol)
                    if success:
                        print(f"✅ تم فتح الصفقة!")
                elif best_symbol:
                    print(f"\n📊 {best_symbol}: {best_score} نقطة - غير كافي")
                else:
                    print("\n❌ لا توجد فرص")
            
            if cycle % 5 == 0:
                trading_manager.clear_failed_symbols()
            
            time.sleep(60)

        except Exception as e:
            print(f"❌ خطأ: {e}")
            sys.stdout.flush()
            time.sleep(30)


@app.route('/')
def home():
    return {'status': 'running', 'bot': 'Smart v4'}


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
