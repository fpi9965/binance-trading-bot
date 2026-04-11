"""
مدير التداول - إدارة الصفقات المفتوحة
"""
import time
import config

class TradingManager:
    def __init__(self, binance_client, notifier):
        self.binance = binance_client
        self.notifier = notifier
        self.current_position = None
        self.position_data = {}
    
    def open_position(self, symbol, quantity, entry_price):
        """
        فتح صفقة جديدة
        """
        try:
            print(f"\n{'='*50}")
            print(f"🟢 جاري فتح صفقة جديدة...")
            print(f"   العملة: {symbol}")
            print(f"   الكمية: {quantity}")
            print(f"   سعر الدخول: ${entry_price}")
            print(f"{'='*50}")
            
            # تنفيذ أمر الشراء
            order = self.binance.buy_symbol(symbol, quantity, entry_price)
            
            if order:
                self.current_position = symbol
                self.position_data = {
                    'symbol': symbol,
                    'quantity': quantity,
                    'entry_price': entry_price,
                    'stop_loss': entry_price * (1 - config.STOP_LOSS_PERCENT / 100),
                    'take_profit': entry_price * (1 + config.TAKE_PROFIT_PERCENT / 100),
                    'trailing_stop': entry_price * (1 - config.TRAILING_STOP_PERCENT / 100),
                    'highest_price': entry_price,
                    'order_id': order.get('orderId'),
                    'opened_at': time.time()
                }
                
                self.notifier.send_position_opened(self.position_data)
                print(f"✅ تم فتح الصفقة بنجاح!")
                return True
            else:
                print(f"❌ فشل في فتح الصفقة!")
                self.notifier.send_message(f"❌ *فشل في فتح صفقة {symbol}*")
                return False
                
        except Exception as e:
            print(f"❌ خطأ في فتح الصفقة: {e}")
            return False
    
    def update_position(self):
        """
        تحديث ومراقبة الصفقة المفتوحة
        """
        if not self.current_position:
            return
        
        try:
            symbol = self.current_position
            data = self.position_data
            
            # الحصول على السعر الحالي
            current_price = self.binance.get_symbol_price(symbol)
            if not current_price:
                return
            
            # حساب نسبة الربح/الخسارة
            pnl_percent = ((current_price - data['entry_price']) / data['entry_price']) * 100
            pnl_value = (current_price - data['entry_price']) * data['quantity']
            
            # تحديث أعلى سعر (للت trailing stop)
            if current_price > data['highest_price']:
                data['highest_price'] = current_price
                # تحديث trailing stop بناءً على أعلى سعر
                data['trailing_stop'] = current_price * (1 - config.TRAILING_STOP_PERCENT / 100)
                print(f"📈 تم تحديث Trailing Stop: ${data['trailing_stop']:.2f}")
            
            # طباعة الحالة
            print(f"\n📍 مراقبة الصفقة: {symbol}")
            print(f"   سعر الدخول: ${data['entry_price']:.2f}")
            print(f"   السعر الحالي: ${current_price:.2f}")
            print(f"   الربح/الخسارة: {pnl_percent:+.2f}% (${pnl_value:+.2f})")
            print(f"   Stop Loss: ${data['stop_loss']:.2f}")
            print(f"   Trailing Stop: ${data['trailing_stop']:.2f}")
            print(f"   جني الأرباح: ${data['take_profit']:.2f}")
            
            # التحقق من الشروط
            
# 1. تحقق من جني الأرباح
            if current_price >= data['take_profit']:
                print(f"\n🎯 تم الوصول لسعر جني الأرباح!")
                self.close_position(reason="take_profit", pnl_percent=pnl_percent)
                return
            
            # 2. تحقق من Stop Loss
            if current_price <= data['stop_loss']:
                print(f"\n🛑 تم الوصول لحد Stop Loss!")
                self.close_position(reason="stop_loss", pnl_percent=pnl_percent)
                return
            
            # 3. تحقق من Trailing Stop
            if current_price <= data['trailing_stop']:
                print(f"\n🦶 Trailing Stop تم تفعيل!")
                self.close_position(reason="trailing_stop", pnl_percent=pnl_percent)
                return
            
            # إرسال تحديث كل 5 دقائق
            if time.time() - data.get('last_update', 0) > 300:
                self.notifier.send_position_update(data, current_price, pnl_percent, pnl_value)
                data['last_update'] = time.time()
                
        except Exception as e:
            print(f"❌ خطأ في تحديث الصفقة: {e}")
    
    def close_position(self, reason, pnl_percent):
        """
        إغلاق الصفقة
        """
        try:
            symbol = self.current_position
            data = self.position_data
            
            print(f"\n{'='*50}")
            print(f"🔴 جاري إغلاق الصفقة...")
            print(f"   السبب: {reason}")
            print(f"   الربح/الخسارة: {pnl_percent:.2f}%")
            print(f"{'='*50}")
            
            # تنفيذ أمر البيع
            order = self.binance.sell_symbol(
                symbol,
                data['quantity'],
                self.binance.get_symbol_price(symbol)
            )
            
            if order:
                self.notifier.send_position_closed(data, reason, pnl_percent)
                print(f"✅ تم إغلاق الصفقة بنجاح!")
            else:
                self.notifier.send_message(f"❌ *فشل في إغلاق صفقة {symbol}*")
                print(f"❌ فشل في إغلاق الصفقة!")
            
            # إعادة تعيين الحالة
            self.current_position = None
            self.position_data = {}
            
        except Exception as e:
            print(f"❌ خطأ في إغلاق الصفقة: {e}")
    
    def get_current_position(self):
        """الحصول على الصفقة الحالية"""
        return self.current_position, self.position_data
