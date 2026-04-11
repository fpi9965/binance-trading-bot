"""
بوت التداول الآلي - صفقات متعددة + عملات HOLD
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
    
    print("\n" + "=" * 60)
    print("🤖 جاري تشغيل بوت التداول الآلي...")
    print("=" * 60)
    
    print(f"\n📋 الإعدادات:")
    print(f"   صفقات متعددة: {config.MAX_POSITIONS} صفقات كحد أقصى")
    print(f"   مبلغ كل صفقة: ${config.TRADE_AMOUNT_USD}")
    print(f"   SYMBOLS: {', '.join(config.SYMBOLS)}")
    sys.stdout.flush()
    
    if config.TEST_MODE:
        print("\n⚠️ وضع الاختبار مفعل!")
    else:
        print("\n🔴 وضع التداول الحقيقي!")
    
    try:
        binance_client = BinanceClient()
        notifier = TelegramNotifier()
        trading_manager = TradingManager(binance_client, notifier)
        analysis = TechnicalAnalysis()
        
        notifier.send_message("🟢 *تم تشغيل البوت!*\n\n"
                             f"🔢 الحد الأقصى: {config.MAX_POSITIONS} صفقات\n"
                             f"💵 لكل صفقة: ${config.TRADE_AMOUNT_USD}")
        
        cycle_count = 0
        usdt_balance = 0
        
        while True:
            try:
                cycle_count += 1
                print(f"\n{'=' * 60}")
                print(f"🔄 الدورة #{cycle_count} - {time.strftime('%Y-%m-%d %H:%M:%S')}")
                print("=" * 60)
                sys.stdout.flush()
                
                # جلب الرصيد
                balance = binance_client.get_account_balance()
                if balance:
                    for b in balance['balances']:
                        if b['asset'] == 'USDT':
                            usdt_balance = float(b['free'])
                            print(f"💰 الرصيد USDT: ${usdt_balance:.2f}")
                            break
                
                # طباعة الصفقات المفتوحة
                open_count = trading_manager.get_open_positions_count()
                print(f"📊 الصفقات المفتوحة: {open_count}/{config.MAX_POSITIONS}")
                sys.stdout.flush()
                
                # تحديث ومراقبة الصفقات المفتوحة
                if open_count > 0:
                    print("\n📍 جاري مراقبة الصفقات...")
                    trading_manager.update_positions()
                else:
                    # البحث عن فرص جديدة
                    print("\n📊 جاري البحث عن فرص...")
                    sys.stdout.flush()
                    
                    results = {}
                    analyzed_count = 0
                    
                    for symbol in config.SYMBOLS:
                        symbol = symbol.strip()
                        if not symbol:
                            continue
                            
                        try:
                            klines = binance_client.get_klines(
                                symbol=symbol,
                                interval=config.TIMEFRAME,
                                limit=100
                            )
                            
                            if klines:
                                result = analysis.analyze_symbol(klines)
                                if result:
                                    results[symbol] = result
                                    analyzed_count += 1
                                    
                                    status = "🟢" if result['recommendation'] == "BUY" else ("🔴" if result['recommendation'] == "SELL" else "🟡")
                                    print(f"  {status} {symbol}: {result['score']}/100 ({result['recommendation']})")
                                    sys.stdout.flush()
                            
                            time.sleep(0.3)
                            
                        except Exception as e:
                            print(f"  ⚠️ خطأ في {symbol}: {e}")
                            continue
                    
                    print(f"\n📊 تم تحليل {analyzed_count} عملة")
                    sys.stdout.flush()
                    
                    # اختيار الأفضل
                    if results:
                        # أولاً: إرسال إشارات للتليقرام
                        for symbol, data in sorted(results.items(), key=lambda x: x[1]['score'], reverse=True):
                            if data['score'] >= 15:  # أي درجة موجبة
                                notifier.send_trade_signal(symbol, data)
                        
                        # اختيار العملات: BUY أولاً، ثم HOLD
                        all_picks = []
                        
                        # أولاً: عملات BUY
                        buy_picks = analysis.get_top_picks(results, top_n=config.MAX_POSITIONS)
                        all_picks.extend(buy_picks)
                        
                        # ثم عملات HOLD (إذا لم تكفِ عملات BUY)
                        if len(all_picks) < config.MAX_POSITIONS:
                            for symbol, data in sorted(results.items(), key=lambda x: x[1]['score'], reverse=True):
                                if data['recommendation'] == 'HOLD' and data['score'] >= 10:
                                    if not any(s == symbol for s, _ in all_picks):
                                        all_picks.append((symbol, data))
                                        if len(all_picks) >= config.MAX_POSITIONS:
                                            break
                        
                        if all_picks and config.TEST_MODE == False:
                            opened_count = 0
                            
                            for idx, (symbol, data) in enumerate(all_picks):
                                # التحقق من عدد الصفقات
                                if not trading_manager.can_open_position():
                                    print(f"\n⚠️已达到 الحد الأقصى للصفقات ({config.MAX_POSITIONS})")
                                    break
                                
                                # التحقق من الرصيد
                                if usdt_balance < config.TRADE_AMOUNT_USD:
                                    print(f"\n⚠️ رصيد غير كافٍ!")
                                    break
                                
                                print(f"\n🏆 الصفقة {idx+1}: {symbol} (درجة: {data['score']}, {data['recommendation']})")
                                sys.stdout.flush()
                                
                                price = data['current_price']
                                quantity = config.TRADE_AMOUNT_USD / price
                                
                                print(f"💵 مبلغ الصفقة: ${config.TRADE_AMOUNT_USD}")
                                print(f"📊 الكمية: {quantity:.4f} {symbol.replace('USDT', '')}")
                                sys.stdout.flush()
                                
                                success = trading_manager.open_position(symbol, quantity, price)
                                
                                if success:
                                    opened_count += 1
                                    usdt_balance -= config.TRADE_AMOUNT_USD
                                    print(f"✅ تم فتح الصفقة!")
                                else:
                                    print(f"⚠️ فشل في {symbol}، محاولة التالية...")
                                
                                time.sleep(3)  # انتظار أطول بين الصفقات
                            
                            if opened_count > 0:
                                print(f"\n✅ تم فتح {opened_count} صفقة جديدة!")
                            else:
                                print(f"\n⚠️ لم تنجح أي صفقة - سيتم المحاولة لاحقاً")
                                
                        elif config.TEST_MODE:
                            print(f"\n🧪 [TEST MODE]")
                
                # Heartbeat كل ساعة
                if cycle_count % 60 == 0:
                    print("\n❤️ Heartbeat...")
                    sys.stdout.flush()
                    notifier.send_heartbeat()
                
                print(f"\n⏰ انتظار {config.CYCLE_INTERVAL} ثانية...")
                sys.stdout.flush()
                time.sleep(config.CYCLE_INTERVAL)
                
            except Exception as e:
                print(f"\n❌ خطأ: {e}")
                sys.stdout.flush()
                try:
                    notifier.send_error(f"خطأ: {e}")
                except:
                    pass
                time.sleep(60)
                
    except Exception as e:
        print(f"\n❌ خطأFatal: {e}")
        sys.stdout.flush()

if __name__ == "__main__":
    print("🚀 بدء البوت...")
    sys.stdout.flush()
    
    bot_thread = threading.Thread(target=run_bot, daemon=False)
    bot_thread.start()
    
    port = int(os.getenv("PORT", 10000))
    print(f"🌐 تشغيل على المنفذ {port}...")
    sys.stdout.flush()
    app.run(host='0.0.0.0', port=port)
