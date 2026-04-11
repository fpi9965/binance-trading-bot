"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    بوت التداول الآلي على Binance                                ║
║                    Binance Automated Trading Bot                               ║
║                                                                            ║
║  المميزات:                                                                  ║
║  ✓ تحليل فني شامل (RSI, MACD, Bollinger Bands)                             ║
║  ✓ اختيار أفضل العملات للشراء                                                ║
║  ✓ شراء تلقائي                                        ✓ وقف خسارة متحرك (Trailing Stop)                                         ║
║  ✓ جني الأرباح عند 10%                                                      ║
║  ✓ إشعارات Telegram فورية                                                  ║
║  ✓ يعمل 24/7 على الخادم                                                     ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import time
import signal
import sys
import os
from datetime import datetime

# استيراد الوحدات
from binance_client import BinanceClient
from technical_analysis import TechnicalAnalysis
from telegram_notifier import TelegramNotifier
from trading_manager import TradingManager


class TradingBot:
    """البوت الرئيسي للتداول الآلي"""

    def __init__(self):
        """تهيئة البوت"""
        print("=" * 60)
        print("🤖 جاري تشغيل بوت التداول الآلي...")
        print("=" * 60)

        # تهيئة المكونات
        self.binance = BinanceClient()
        self.notifier = TelegramNotifier()
        self.trading_manager = TradingManager(self.binance, self.notifier)
        self.analysis = TechnicalAnalysis()

        # متغيرات التشغيل
        self.running = True
        self.cycle_count = 0
        self.last_analysis_time = None

        # إعداد إيقاف البوت
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    def shutdown(self, signum=None, frame=None):
        """إيقاف البوت بشكل آمن"""
        print("\n" + "=" * 60)
        print("🛑 جاري إيقاف البوت...")
        print("=" * 60)

        self.running = False

        if self.trading_manager.current_position:
            print("إغلاق المركز المفتوح...")
            self.trading_manager.close_position(reason="bot_shutdown")

        self.notifier.send_message("🔴 *تم إيقاف البوت*")
        sys.exit(0)

    def check_balance(self):
        """فحص الرصيد"""
        balance = self.binance.get_account_balance()
        if balance:
            usdt_balance = balance.get('USDT', {}).get('free', 0)
            print(f"💰 الرصيد المتاح: ${usdt_balance:.2f} USDT")
            return usdt_balance
        return 0

    def analyze_market(self):
        """تحليل السوق واختيار أفضل العملات"""
        print("\n" + "=" * 60)
        print(f"📊 جاري تحليل السوق... ({datetime.now().strftime('%H:%M:%S')})")
        print("=" * 60)

        symbols = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,MATICUSDT,DOTUSDT,LTCUSDT").split(",")
        analysis_results = {}

        for symbol in symbols:
            symbol = symbol.strip()
            try:
                print(f"🔍 تحليل {symbol}...", end=" ")

                interval = os.getenv("TIMEFRAME", "15m")
                candle_count = int(os.getenv("CANDLE_COUNT", "100"))

                klines = self.binance.get_klines(
                    symbol=symbol,
                    interval=interval,
                    limit=candle_count
                )

                if klines:
                    result = self.analysis.analyze_symbol(klines)

                    if result:
                        analysis_results[symbol] = result
                        print(f"✓ درجة: {result['score']}/100 ({result['recommendation']})")
                    else:
                        print("✗ فشل التحليل")
                else:
                    print("✗ لا توجد بيانات")

                time.sleep(0.5)

            except Exception as e:
                print(f"✗ خطأ: {e}")

        return analysis_results

    def select_best_trade(self, analysis_results):
        """اختيار أفضل صفقة"""
        top_picks = self.analysis.get_top_picks(analysis_results, top_n=1)

        if top_picks:
            symbol, data = top_picks[0]
            current_price = self.binance.get_symbol_price(symbol)

            print(f"\n🏆 أفضل اختيار: {symbol}")
            print(f"   الدرجة: {data['score']}/100")
            print(f"   السعر: ${current_price:.8f}")
            print(f"   RSI: {data.get('rsi', 'N/A'):.2f}")

            self.notifier.send_trade_signal(symbol, data)

            return symbol, current_price

        print("\n❌ لا توجد فرص تداول مناسبة حالياً")
        return None, None

    def execute_trade(self, symbol, price):
        """تنفيذ الصفقة"""
        print(f"\n" + "=" * 60)
        print(f"🟢 جاري تنفيذ أمر الشراء: {symbol}")
        print("=" * 60)

        trade_amount = float(os.getenv("TRADE_AMOUNT_USD", "10.0"))
        quantity = trade_amount / price

        print(f"💵 مبلغ الاستثمار: ${trade_amount}")
        print(f"📦 الكمية: {quantity:.8f} {symbol}")

        success = self.trading_manager.open_position(
            symbol=symbol,
            quantity=quantity,
            entry_price=price
        )

        if success:
            print(f"✅ تم فتح المركز بنجاح!")
            return True
        else:
            print(f"❌ فشل في فتح المركز")
            return False

    def monitor_position(self):
        """مراقبة المركز المفتوح"""
        status = self.trading_manager.get_position_status()

        if not status:
            return False

        print(f"\n📍 حالة المركز الحالي:")
        print(f"   العملة: {status['symbol']}")
        print(f"   الكمية: {status['quantity']:.8f}")
        print(f"   سعر الدخول: ${status['entry_price']:.8f}")
        print(f"   السعر الحالي: ${status['current_price']:.8f}")
        print(f"   الربح/الخسارة: {status['profit_percent']:+.2f}%")
        print(f"   وقف الخسارة: ${status['stop_loss']:.8f}")

        result = self.trading_manager.update_position()

        if result:
            if result['action'] == 'stop_loss':
                print("⚠️ تم تنفيذ وقف الخسارة!")
            elif result['action'] == 'take_profit':
                print("🎯 تم جني الأرباح!")
            elif result['action'] == 'trailing_stop_updated':
                print("📍 تم تحديث وقف الخسارة المتحرك!")

        self.trading_manager.force_close_if_needed()

        return True

    def send_heartbeat(self):
        """إرسال نبضة قلب"""
        status_data = {
            'balance': self.check_balance(),
            'current_position': self.trading_manager.current_position.symbol if self.trading_manager.current_position else 'لا يوجد',
            'trades_today': self.trading_manager.trades_today,
            'total_profit': self.trading_manager.total_profit
        }
        self.notifier.send_status(status_data)

    def run_cycle(self):
        """دورة واحدة من البوت"""
        self.cycle_count += 1
        print(f"\n{'=' * 60}")
        print(f"🔄 الدورة #{self.cycle_count} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        self.check_balance()

        if self.trading_manager.current_position:
            print("\n📍 يوجد مركز مفتوح - جاري المراقبة...")
            self.monitor_position()
        else:
            print("\n📊 لا يوجد مركز مفتوح - جاري البحث عن فرص...")

            analysis_results = self.analyze_market()

            symbol, price = self.select_best_trade(analysis_results)

            if symbol and price:
                self.execute_trade(symbol, price)

        if self.cycle_count % 60 == 0:
            self.send_heartbeat()

    def run(self):
        """تشغيل البوت"""
        print("\n" + "=" * 60)
        print("✅ تم تهيئة البوت بنجاح!")
        print("=" * 60)

        symbols = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,MATICUSDT,DOTUSDT,LTCUSDT")
        self.notifier.send_message("🟢 *تم تشغيل بوت التداول الآلي!*\n\nأزواج المراقبة:\n" + "\n".join([f"• {s.strip()}" for s in symbols.split(",")]))

        while self.running:
            try:
                self.run_cycle()

                cycle_interval = int(os.getenv("CYCLE_INTERVAL", "60"))
                print(f"\n⏰ انتظار {cycle_interval} ثانية للدورة القادمة...")
                time.sleep(cycle_interval)

            except Exception as e:
                print(f"❌ خطأ في الدورة: {e}")
                self.notifier.send_error(f"خطأ في الدورة: {e}")
                time.sleep(60)


def main():
    """نقطة الدخول الرئيسية"""
    print("""
    ╔════════════════════════════════════════════════════════════╗
    ║                                                            ║
    ║          🤖 بوت التداول الآلي على Binance                   ║
    ║                                                            ║
    ║          للتحليل الفني + التداول التلقائي                   ║
    ║                                                            ║
    ╚════════════════════════════════════════════════════════════╝
    """)

    bot = TradingBot()
    bot.run()


# إضافة Flask خفيفة للـ Port
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return 'Trading Bot is running!'

@app.route('/health')
def health():
    return 'OK'

def run_flask():
    port = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    # تشغيل Flask في thread منفصل
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    # تشغيل البوت
    main()
