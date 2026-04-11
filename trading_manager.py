"""
Trading Manager
"""
import os
import config

class TradingManager:
    def __init__(self, binance_client, telegram_notifier):
        self.binance = binance_client
        self.telegram = telegram_notifier
        self.positions = {}
        self.failed_symbols = set()
        self.recently_traded = set()
        self.max_positions = getattr(config, 'MAX_POSITIONS', 3)
        self.trade_amount = getattr(config, 'TRADE_AMOUNT_USD', 20)
        self.stop_loss_pct = getattr(config, 'STOP_LOSS_PERCENT', 2.0)
        self.take_profit_pct = getattr(config, 'TAKE_PROFIT_PERCENT', 10.0)
    
    def get_open_positions_count(self):
        return len(self.positions)
    
    def can_open_position(self):
        return len(self.positions) < self.max_positions
    
    def get_position(self, symbol):
        return self.positions.get(symbol)
    
    def get_all_positions(self):
        return list(self.positions.values())
    
    def get_failed_symbols(self):
        return list(self.failed_symbols)
    
    def get_recently_traded_symbols(self):
        return list(self.recently_traded)
    
    def add_failed_symbol(self, symbol):
        self.failed_symbols.add(symbol)
    
    def open_position(self, symbol):
        if not self.can_open_position():
            print(f"⚠️已达到 الحد الأقصى للصفقات ({self.max_positions})")
            return False
        
        try:
            market_open, price = self.binance.check_market_status(symbol)
            if not market_open:
                print(f"⚠️ {symbol}: السوق مغلق")
                return False
            
            quantity = self.trade_amount / price
            
            result = self.binance.buy_symbol(symbol, quantity, price)
            
            if result:
                self.positions[symbol] = {
                    'symbol': symbol,
                    'quantity': quantity,
                    'entry_price': price,
                    'stop_loss': price * (1 - self.stop_loss_pct / 100),
                    'take_profit': price * (1 + self.take_profit_pct / 100),
                    'highest_price': price
                }
                print(f"✅ تم فتح صفقة في {symbol}!")
                self.telegram.send_position_opened(self.positions[symbol])
                return True
            
            return False
            
        except Exception as e:
            print(f"❌ خطأ في فتح الصفقة: {e}")
            return False
    
    def monitor_position(self, position):
        try:
            symbol = position['symbol']
            current_price = self.binance.get_symbol_price(symbol)
            
            if not current_price:
                return
            
            entry = position['entry_price']
            pnl_pct = ((current_price - entry) / entry) * 100
            pnl_val = (current_price - entry) * position['quantity']
            
            print(f"   سعر الدخول: ${entry:.4f}")
            print(f"   السعر الحالي: ${current_price:.4f}")
            print(f"   الربح/الخسارة: {pnl_pct:+.2f}%")
            
            if current_price > position['highest_price']:
                position['highest_price'] = current_price
                new_sl = position['highest_price'] * (1 - 1.5 / 100)
                position['stop_loss'] = max(position['stop_loss'], new_sl)
            
            if current_price <= position['stop_loss']:
                print(f"🛑 تم تفعيل وقف الخسارة!")
                self.close_position(symbol, "Stop Loss", pnl_pct)
            elif current_price >= position['take_profit']:
                print(f"🎯 تم الوصول لهدف الربح!")
                self.close_position(symbol, "Take Profit", pnl_pct)
                
        except Exception as e:
            print(f"خطأ في المراقبة: {e}")
    
    def close_position(self, symbol, reason, pnl_pct):
        try:
            position = self.positions.get(symbol)
            if not position:
                return
            
            result = self.binance.sell_symbol(symbol, position['quantity'], position['entry_price'])
            
            if result:
                print(f"✅ تم إغلاق الصفقة: {reason}")
                self.telegram.send_position_closed(position, reason, pnl_pct)
                del self.positions[symbol]
                self.recently_traded.add(symbol)
                
        except Exception as e:
            print(f"خطأ في إغلاق الصفقة: {e}")
