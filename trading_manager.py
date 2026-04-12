"""
Trading Manager - ذكي ويتعلم من الأخطاء
"""
import os
import time
import config

class TradingManager:
    def __init__(self, binance_client, telegram_notifier):
        self.binance = binance_client
        self.telegram = telegram_notifier
        self.positions = {}
        self.failed_symbols = {}
        self.max_positions = getattr(config, 'MAX_POSITIONS', 3)
        self.trade_amount = getattr(config, 'TRADE_AMOUNT_USD', 20)
        self.stop_loss_pct = getattr(config, 'STOP_LOSS_PERCENT', 2.0)
        self.take_profit_pct = getattr(config, 'TAKE_PROFIT_PERCENT', 10.0)
        
        print("✅ Trading Manager جاهز")
    
    def get_open_positions_count(self):
        return len(self.positions)
    
    def can_open_position(self):
        return len(self.positions) < self.max_positions
    
    def get_position(self, symbol):
        return self.positions.get(symbol)
    
    def get_all_positions(self):
        return list(self.positions.values())
    
    def get_failed_symbols(self):
        return list(self.failed_symbols.keys())
    
    def add_failed_symbol(self, symbol, reason=""):
        self.failed_symbols[symbol] = {
            'reason': reason,
            'attempts': self.failed_symbols.get(symbol, {}).get('attempts', 0) + 1,
            'last_attempt': time.time()
        }
    
    def clear_failed_symbols(self):
        cleared = []
        now = time.time()
        for symbol in list(self.failed_symbols.keys()):
            data = self.failed_symbols[symbol]
            if now - data['last_attempt'] > 3600:
                del self.failed_symbols[symbol]
                cleared.append(symbol)
        if cleared:
            print(f"   🟢 تم مسح: {cleared}")
    
    def open_position(self, symbol, retry_count=0):
        if not self.can_open_position():
            return False
        
        try:
            market_open, price = self.binance.check_market_status(symbol)
            if not market_open:
                self.add_failed_symbol(symbol, "السوق مغلق")
                return False
            
            quantity = self.trade_amount / price
            
            print(f"🟢 جاري فتح صفقة: {symbol}")
            print(f"   السعر: ${price:.6f}")
            print(f"   الكمية: {quantity:.4f}")
            
            result = self.binance.buy_symbol(symbol, quantity, price)
            
            if result:
                self.positions[symbol] = {
                    'symbol': symbol,
                    'quantity': quantity,
                    'entry_price': price,
                    'stop_loss': price * (1 - self.stop_loss_pct / 100),
                    'take_profit': price * (1 + self.take_profit_pct / 100),
                    'highest_price': price,
                    'opened_at': time.time(),
                    'trailing_stop': price * (1 - 1.5 / 100)
                }
                
                print(f"✅ تم فتح صفقة في {symbol}!")
                self.telegram.send_message(f"✅ *صفقة مفتوحة!*\n📊 {symbol}\n💰 السعر: ${price:.6f}\n🎯 الهدف: {self.take_profit_pct}%")
                return True
            else:
                self.add_failed_symbol(symbol, "فشل في الشراء")
                return False
                
        except Exception as e:
            print(f"❌ خطأ: {e}")
            self.add_failed_symbol(symbol, str(e))
            return False
    
    def monitor_position(self, position):
        try:
            symbol = position['symbol']
            current_price = self.binance.get_symbol_price(symbol)
            
            if not current_price:
                return
            
            entry = position['entry_price']
            pnl_pct = ((current_price - entry) / entry) * 100
            pnl_value = (current_price - entry) * position['quantity']
            
            print(f"\n   📊 {symbol}")
            print(f"   💰 سعر الدخول: ${entry:.6f}")
            print(f"   📈 السعر الحالي: ${current_price:.6f}")
            print(f"   📊 الربح/الخسارة: {pnl_pct:+.2f}%")
            
            if current_price > position['highest_price']:
                position['highest_price'] = current_price
                new_trailing = position['highest_price'] * (1 - 1.5 / 100)
                position['trailing_stop'] = max(position['trailing_stop'], new_trailing)
                print(f"   🆕 وقف متحرك: ${position['trailing_stop']:.6f}")
            
            if current_price <= position['trailing_stop']:
                print(f"🛑 وقف متحرك!")
                self.close_position(symbol, "Trailing Stop", pnl_pct)
            elif current_price <= position['stop_loss']:
                print(f"🛑 وقف خسارة!")
                self.close_position(symbol, "Stop Loss", pnl_pct)
            elif current_price >= position['take_profit']:
                print(f"🎯 هدف محقق!")
                self.close_position(symbol, "Take Profit", pnl_pct)
                
        except Exception as e:
            print(f"خطأ: {e}")
    
    def close_position(self, symbol, reason, pnl_pct):
        try:
            position = self.positions.get(symbol)
            if not position:
                return
            
            result = self.binance.sell_symbol(symbol, position['quantity'], position['entry_price'])
            
            if result:
                emoji = "🟢" if pnl_pct >= 0 else "🔴"
                print(f"✅ تم إغلاق: {reason} ({pnl_pct:+.2f}%)")
                self.telegram.send_message(f"{emoji} *صفقة مغلقة!*\n📊 {symbol}\n📋 {reason}\n💰 {pnl_pct:+.2f}%")
                del self.positions[symbol]
            else:
                print(f"❌ فشل في الإغلاق")
                
        except Exception as e:
            print(f"خطأ: {e}")
