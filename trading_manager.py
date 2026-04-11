"""
مدير التداول - صفقات متعددة
"""
import time
import config

class TradingManager:
    def __init__(self, binance_client, notifier):
        self.binance = binance_client
        self.notifier = notifier
        self.positions = {}  # {symbol: position_data}
        self.max_positions = getattr(config, 'MAX_POSITIONS', 3)
    
    def get_open_positions_count(self):
        return len(self.positions)
    
    def can_open_position(self):
        return len(self.positions) < self.max_positions
    
    def open_position(self, symbol, quantity, entry_price):
        if not self.can_open_position():
            print(f"⚠️已达到最大仓位数量 ({self.max_positions})")
            return False
        
        if symbol in self.positions:
            print(f"⚠️{symbol} موجود بالفعل")
            return False
        
        try:
            print(f"\n{'='*50}")
            print(f"🟢 جاري فتح صفقة جديدة...")
            print(f"   العملة: {symbol}")
            print(f"   الكمية: {quantity}")
            print(f"   سعر الدخول: ${entry_price}")
            print(f"{'='*50}")
            
            order = self.binance.buy_symbol(symbol, quantity, entry_price)
            
            if order:
                self.positions[symbol] = {
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
                
                self.notifier.send_position_opened(self.positions[symbol])
                print(f"✅ تم فتح الصفقة! الصفقات المفتوحة: {self.get_open_positions_count()}/{self.max_positions}")
                return True
            else:
                print(f"❌ فشل في فتح الصفقة!")
                self.notifier.send_message(f"❌ *فشل في فتح صفقة {symbol}*")
                return False
                
        except Exception as e:
            print(f"❌ خطأ في فتح الصفقة: {e}")
            return False
    
    def update_positions(self):
        if not self.positions:
            return
        
        symbols_to_close = []
        
        for symbol, data in list(self.positions.items()):
            try:
                current_price = self.binance.get_symbol_price(symbol)
                if not current_price:
                    continue
                
                pnl_percent = ((current_price - data['entry_price']) / data['entry_price']) * 100
                pnl_value = (current_price - data['entry_price']) * data['quantity']
                
                # تحديث Trailing Stop
                if current_price > data['highest_price']:
                    data['highest_price'] = current_price
                    data['trailing_stop'] = current_price * (1 - config.TRAILING_STOP_PERCENT / 100)
                    print(f"📈 {symbol} تم تحديث Trailing Stop: ${data['trailing_stop']:.2f}")
                
                print(f"\n📍 الصفقة: {symbol}")
                print(f"   سعر الدخول: ${data['entry_price']:.2f}")
                print(f"   السعر الحالي: ${current_price:.2f}")
                print(f"   الربح/الخسارة: {pnl_percent:+.2f}% (${pnl_value:+.2f})")
                print(f"   Stop Loss: ${data['stop_loss']:.2f}")
                print(f"   Trailing Stop: ${data['trailing_stop']:.2f}")
                print(f"   جني الأرباح: ${data['take_profit']:.2f}")
                
                # تحقق من جني الأرباح
                if current_price >= data['take_profit']:
                    print(f"\n🎯 تم الوصول لجني الأرباح!")
                    symbols_to_close.append((symbol, "take_profit", pnl_percent))
                    continue
                
                # تحقق من Stop Loss
                if current_price <= data['stop_loss']:
                    print(f"\n🛑 تم الوصول لStop Loss!")
                    symbols_to_close.append((symbol, "stop_loss", pnl_percent))
                    continue
                
                # تحقق من Trailing Stop
                if current_price <= data['trailing_stop']:
                    print(f"\n🦶 تم تفعيل Trailing Stop!")
                    symbols_to_close.append((symbol, "trailing_stop", pnl_percent))
                    continue
                
                # تحديث كل 5 دقائق
                if time.time() - data.get('last_update', 0) > 300:
                    self.notifier.send_position_update(data, current_price, pnl_percent, pnl_value)
                    data['last_update'] = time.time()
                    
            except Exception as e:
                print(f"❌ خطأ في تحديث {symbol}: {e}")
        
        # إغلاق الصفقات
        for symbol, reason, pnl in symbols_to_close:
            self.close_position(symbol, reason, pnl)
    
    def close_position(self, symbol, reason, pnl_percent):
        if symbol not in self.positions:
            return
        
        try:
            data = self.positions[symbol]
            
            print(f"\n{'='*50}")
            print(f"🔴 جاري إغلاق الصفقة...")
            print(f"   العملة: {symbol}")
            print(f"   السبب: {reason}")
            print(f"   الربح/الخسارة: {pnl_percent:.2f}%")
            print(f"{'='*50}")
            
            order = self.binance.sell_symbol(
                symbol,
                data['quantity'],
                self.binance.get_symbol_price(symbol)
            )
            
            if order:
                self.notifier.send_position_closed(data, reason, pnl_percent)
                print(f"✅ تم إغلاق الصفقة!")
            else:
                self.notifier.send_message(f"❌ *فشل في إغلاق صفقة {symbol}*")
                print(f"❌ فشل في إغلاق الصفقة!")
            
            del self.positions[symbol]
            print(f"📊 الصفقات المفتوحة: {self.get_open_positions_count()}/{self.max_positions}")
            
        except Exception as e:
            print(f"❌ خطأ في إغلاق الصفقة: {e}")
    
    def get_all_positions(self):
        return self.positions
    
    @property
    def current_position(self):
        return list(self.positions.keys())[0] if self.positions else None
    
    @property
    def position_data(self):
        return list(self.positions.values())[0] if self.positions else {}
