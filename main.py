"""
بوت التداول الآلي على Binance
===============================
هذا البوت يقوم بـ:
- التحليل الفني للعملات
- تنفيذ أوامر الشراء والبيع تلقائياً
- إدارة Stop Loss و Trailing Stop
- إرسال إشعارات على Telegram
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

# Flask للتوافق مع Render
app = Flask(__name__)

# المتغيرات العامة
binance_client = None
notifier = None
trading_manager = None
analysis = None
bot_started = False

@app.route('/')
def home():
    return 'Trading Bot is running!'

@app.route('/health')
def health():
    return 'OK'

def run_bot():
    """
    الحلقة الرئيسية للبوت
    """
    global binance_client, notifier, trading_manager, analysis, bot_started
    
    print("\n" + "=" * 60)
    print("🤖 جاري تشغيل بوت التداول الآلي...")
    print("=" * 60)
    sys.stdout.flush()
    
    # طباعة إعدادات مهمة
    print(f"\n📋 الإعدادات:")
    print(f"   TEST_MODE: {config.TEST_MODE}")
    print(f"   TRADE_AMOUNT_USD: ${config.TRADE_AMOUNT_USD}")
    print(f"   SYMBOLS: {', '.join(config.SYMBOLS)}")
    print(f"   CYCLE_INTERVAL: {config.CYCLE_INTERVAL} ثانية")
    print(f"   STOP_LOSS: {config.STOP_LOSS_PERCENT}%")
    print(f"   TAKE_PROFIT: {config.TAKE_PROFIT_PERCENT}%")
    print(f"   TRAILING_STOP: {config.TRAILING_STOP_PERCENT}%")
    sys.stdout.flush()
    
    if config.TEST_MODE:
        print("\n⚠️  ⚠️  ⚠️  وضع الاختبار مفعل! لن يتم تنفيذ صفقات حقيقية!  ⚠️  ⚠️  ⚠️")
    else:
        print("\n🔴 🔴 🔴  وضع التداول الحقيقي! سيتم تنفيذ صفقات! 🔴 🔴 🔴")
    
    print("=" * 60 + "\n")
    sys.stdout.flush()
    
    try:
        # تهيئة المكونات
        print("🔗 جاري الاتصال بـ Binance...")
        sys.stdout.flush()
        binance_client = BinanceClient()
        
        print("📱 جاري تهيئة Telegram...")
        sys.stdout.flush()
        notifier = TelegramNotifier()
        
        print("📊 جاري تهيئة نظام التداول...")
        sys.stdout.flush()
        trading_manager = TradingManager(binance_client, notifier)
        
        print("📈 جاري تهيئة التحليل الفني...\n")
        sys.stdout.flush()
        analysis = TechnicalAnalysis()
        
        # إرسال رسالة تشغيل
        notifier.send_message("🟢 *تم تشغيل بوت التداول الآلي!*\n\n"
                             f"🔄 وضع الاختبار: {'نعم' if config.TEST_MODE else 'لا'}\n"
                             f"💵 مبلغ كل صفقة: ${config.TRADE_AMOUNT_USD}")
        
        bot_started = True
        cycle_count = 0
        usdt_balance = 0
        
        # الحلقة الرئيسية
        while True:
            try:
                cycle_count += 1
                print(f"\n{'=' * 60}")
                print(f"🔄 الدورة #{cycle_count} - {time.strftime('%Y-%m-%d %H:%M:%S')}")
                print("=" * 60)
                sys.stdout.flush()
                
                # التحقق من الرصيد
                balance = binance_client.get_account_balance()
                if balance:
                    for b in balance['balances']:
                        if b['asset'] == 'USDT':
                            usdt_balance = float(b['free'])
                            print(f"💰 الرصيد USDT: ${usdt_balance:.2f}")
                            break
                sys.stdout.flush()
                
                # إذا كانت هناك صفقة مفتوحة
                if trading_manager.current_position:
                    print("\n📍 جاري مراقبة الصفقة المفتوحة...")
                    sys.stdout.flush()
                    trading_manager.update_position()
                else:
                    # البحث عن فرص جديدة
                    print("\n📊 جاري البحث عن فرص تداول...")
                    sys.stdout.flush()
                    
                    results = {}
                    analyzed_count = 0
                    
                    for symbol in config.SYMBOLS:
                        symbol = symbol.strip()
                        if not symbol:
                            continue
                            
                        try:
                            # جلب البيانات
                            klines = binance_client.get_klines(
                                symbol=symbol,
                                interval=config.TIMEFRAME,
                                limit=100
                            )
                            
                            if klines:
                                # تحليل
                                result = analysis.analyze_symbol(klines)
                                if result:
                                    results[symbol] = result
                                    analyzed_count += 1
                                    
                                    # طباعة النتيجة
                                    status = "🟢" if result['recommendation'] == "BUY" else ("🔴" if result['recommendation'] == "SELL" else "🟡")
                                    print(f"  {status} {symbol}: {result['score']}/100 ({result['recommendation']})")
                                    sys.stdout.flush()
                            
                            # انتظار قصير لتجنب Rate Limit
                            time.sleep(0.5)
                            
                        except Exception as e:
                            print(f"  ⚠️ خطأ في تحليل {symbol}: {e}")
                            sys.stdout.flush()
                            continue
                    
                    print(f"\n📊 تم تحليل {analyzed_count} عملة")
                    sys.stdout.flush()
                    
                    # اختيار الأفضل
                    if results:
                        # أولاً: إرسال جميع الإشارات للتليقرام
                        for symbol, data in sorted(results.items(), key=lambda x: x[1]['score'], reverse=True):
                            if data['recommendation'] == 'BUY' and data['score'] >= 40:
                                notifier.send_trade_signal(symbol, data)
                        
                        # ثانياً: اختيار الأفضل وتنفيذ الصفقة
                        top_picks = analysis.get_top_picks(results, top_n=1)
                        
                        if top_picks and config.TEST_MODE == False:
                            symbol, data = top_picks[0]
                            
                            # التحقق من الرصيد الكافي
                            if usdt_balance >= config.TRADE_AMOUNT_USD:
                                print(f"\n🏆 أفضل اختيار: {symbol} (درجة: {data['score']})")
                                sys.stdout.flush()
                                
                                # حساب الكمية
                                price = data['current_price']
                                quantity = config.TRADE_AMOUNT_USD / price
                                
                                print(f"💵 مبلغ الصفقة: ${config.TRADE_AMOUNT_USD}")
                                print(f"📊 الكمية: {quantity} {symbol.replace('USDT', '')}")
                                sys.stdout.flush()
                                
                                # فتح الصفقة
                                success = trading_manager.open_position(symbol, quantity, price)
                                
                                if success:
                                    print(f"✅ تم فتح الصفقة بنجاح!")
                                    sys.stdout.flush()
                                else:
                                    print(f"❌ فشل في فتح الصفقة!")
                                    sys.stdout.flush()
                            else:
                                print(f"\n⚠️ رصيد USDT غير كافٍ! الرصيد: ${usdt_balance:.2f}, المطلوب: ${config.TRADE_AMOUNT_USD}")
                                sys.stdout.flush()
                        elif config.TEST_MODE:
                            print(f"\n🧪 [TEST MODE] لن يتم تنفيذ أي صفقات حقيقية")
                            sys.stdout.flush()
                
                # إشعار Heartbeat كل ساعة
                if cycle_count % 60 == 0:
                    print("\n❤️ إرسال Heartbeat...")
                    sys.stdout.flush()
                    notifier.send_heartbeat()
                
                # انتظار للدورة القادمة
                print(f"\n⏰ انتظار {config.CYCLE_INTERVAL} ثانية للدورة القادمة...")
                sys.stdout.flush()
                time.sleep(config.CYCLE_INTERVAL)
                
            except Exception as e:
                print(f"\n❌ خطأ في الدورة: {e}")
                sys.stdout.flush()
                try:
                    notifier.send_error(f"خطأ في الدورة: {e}")
                except:
                    pass
                time.sleep(60)
                
    except KeyboardInterrupt:
        print("\n\n⚠️ تم إيقاف البوت بواسطة المستخدم")
        sys.stdout.flush()
        if notifier:
            notifier.send_message("🔴 *تم إيقاف البوت*")
    except Exception as e:
        print(f"\n❌ خطأFatal: {e}")
        sys.stdout.flush()
        try:
            notifier.send_error(f"خطأFatal: {e}")
        except:
            pass

# تشغيل البوت عند بدء التطبيق
def start_background_bot():
    bot_thread = threading.Thread(target=run_bot, daemon=False)
    bot_thread.start()

# نقطة الدخول
if __name__ == "__main__":
    print("🚀 بدء تشغيل البوت...")
    sys.stdout.flush()
    
    # تشغيل البوت في Thread منفصل
    start_background_bot()
    
    # تشغيل Flask
    port = int(os.getenv("PORT", 10000))
    print(f"🌐 تشغيل خادم الويب على المنفذ {port}...")
    sys.stdout.flush()
    app.run(host='0.0.0.0', port=port)
