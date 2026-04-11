"""
بوت التداول الآلي على Binance
"""
import time
import threading
from flask import Flask
from binance_client import BinanceClient
from technical_analysis import TechnicalAnalysis
from telegram_notifier import TelegramNotifier
from trading_manager import TradingManager
import os

app = Flask(__name__)

binance_client = None
notifier = None
trading_manager = None
analysis = None

@app.route('/')
def home():
    return 'Trading Bot is running!'

@app.route('/health')
def health():
    return 'OK'

def run_bot():
    global binance_client, notifier, trading_manager, analysis
    
    print("=" * 60)
    print("🤖 جاري تشغيل بوت التداول الآلي...")
    print("=" * 60)

    try:
        binance_client = BinanceClient()
        notifier = TelegramNotifier()
        trading_manager = TradingManager(binance_client, notifier)
        analysis = TechnicalAnalysis()
        
        symbols = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,MATICUSDT,DOTUSDT,LTCUSDT")
        notifier.send_message("🟢 *تم تشغيل بوت التداول الآلي!*")
        
        cycle_count = 0
        
        while True:
            try:
                cycle_count += 1
                print(f"\n{'=' * 60}")
                print(f"🔄 الدورة #{cycle_count}")
                print("=" * 60)
                
                balance = binance_client.get_account_balance()
                if balance:
                    usdt_balance = balance.get('USDT', {}).get('free', 0)
                    print(f"💰 الرصيد: ${usdt_balance:.2f}")
                
                if trading_manager.current_position:
                    print("📍 جاري المراقبة...")
                    trading_manager.update_position()
                else:
                    print("📊 جاري البحث عن فرص...")
                    syms = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,MATICUSDT,DOTUSDT,LTCUSDT").split(",")
                    results = {}
                    for s in syms:
                        s = s.strip()
                        try:
                            klines = binance_client.get_klines(symbol=s, interval=os.getenv("TIMEFRAME", "15m"), limit=100)
                            if klines:
                                r = analysis.analyze_symbol(klines)
                                if r:
                                    results[s] = r
                                    print(f"🔍 {s}: {r['score']}/100 ({r['recommendation']})")
                            time.sleep(0.3)
                        except:
                            pass
                    
                    top = analysis.get_top_picks(results, top_n=1)
                    if top:
                        sym, data = top[0]
                        price = binance_client.get_symbol_price(sym)
                        print(f"🏆 {sym} بسعر ${price}")
                        notifier.send_trade_signal(sym, data)
                        qty = float(os.getenv("TRADE_AMOUNT_USD", "10")) / price
                        trading_manager.open_position(sym, qty, price)
                
                if cycle_count % 60 == 0:
                    notifier.send_heartbeat()
                
                time.sleep(int(os.getenv("CYCLE_INTERVAL", "60")))
                
            except Exception as e:
                print(f"❌ خطأ: {e}")
                time.sleep(60)
                
    except Exception as e:
        print(f"❌ خطأ: {e}")

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    port = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
