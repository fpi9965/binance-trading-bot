"""
مدير التداول - صفقات متعددة
"""
import time
import config

class TradingManager:
    def __init__(self, binance_client, notifier):
        self.binance = binance_client
        self.notifier = notifier
        self.positions = {}
        self.max_positions = getattr(config, 'MAX_POSITIONS', 3)
    
    def get_open_positions_count(self):
        return len(self.positions)
    
    def can_open_position(self):
        return len(self.positions) < self.max_positions
    
    def open_position(self, symbol, quantity, entry_price):
        if not self.can_open_position():
            return False
        
        if symbol in self.positions:
            return False
        
        try:
            print(f"\n{'='*50}")
            print(f"🟢 جاري فتح صفقة...")
            print(f"   {symbol}")
            print(f"   الكمية: {quantity}")
            print(f"   السعر: ${entry_price}")
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
                print(f"✅ تم! الصفقات: {self.get_open_positions_count()}/{self.max_positions}")
                return True
            else:
                print(f"❌ فشل!")
                return False
                
        except Exception as e:
            print(f"❌ خطأ: {e}")
            return False
    
    def update_positions(self):
        if not self.positions:
            return
        
        to_close = []
        
        for symbol, data in list(self.positions.items()):
            try:
                current_price = self.binance.get_symbol_price(symbol)
                if not current_price:
                    continue
                
                pnl_pct = ((current_price - data['entry_price']) / data['entry_price']) * 100
                pnl_val = (current_price - data['entry_price']) * data['quantity']
                
                # تحديث Trailing Stop
                if current_price > data['highest_price']:
                    data['highest_price'] = current_price
                    data['trailing_stop'] = current_price * (1 - config.TRAILING_STOP_PERCENT / 100)
                    print(f"📈 {symbol} Trailing: ${data['trailing_stop']:.4f}")
                
                print(f"\n📍 {symbol}")
                print(f"   دخول: ${data['entry_price']:.4f} | الآن: ${current_price:.4f}")
                print(f"   ربح: {pnl_pct:+.2f}% (${pnl_val:+.2f})")
                
                # فحص الشروط
                if current_price >= data['take_profit']:
                    print(f"🎯 جني أرباح!")
                    to_close.append((symbol, "take_profit", pnl_pct))
                elif current_price <= data['stop_loss']:
                    print(f"🛑 Stop Loss!")
                    to_close.append((symbol, "stop_loss", pnl_pct))
                elif current_price <= data['trailing_stop']:
                    print(f"🦶 Trailing Stop!")
                    to_close.append((symbol, "trailing_stop", pnl_pct))
                    
            except Exception as e:
                print(f"❌ خطأ {symbol}: {e}")
        
        for symbol, reason, pnl in to_close:
            self.close_position(symbol, reason, pnl)
    
    def close_position(self, symbol, reason, pnl_pct):
        if symbol not in self.positions:
            return
        
        try:
            data = self.positions[symbol]
            
            print(f"\n{'='*50}")
            print(f"🔴 إغلاق {symbol} | السبب: {reason} | الربح: {pnl_pct:.2f}%")
            print(f"{'='*50}")
            
            order = self.binance.sell_symbol(
                symbol,
                data['quantity'],
                self.binance.get_symbol_price(symbol)
            )
            
            if order:
                self.notifier.send_position_closed(data, reason, pnl_pct)
                print(f"✅ تم!")
            
            del self.positions[symbol]
            print(f"📊 الصفقات: {self.get_open_positions_count()}/{self.max_positions}")
            
        except Exception as e:
            print(f"❌ خطأ: {e}")
    
    def get_all_positions(self):
        return self.positions
    
    @property
    def current_position(self):
        return list(self.positions.keys())[0] if self.positions else None
    
    @property
    def position_data(self):
        return list(self.positions.values())[0] if self.positions else {}
