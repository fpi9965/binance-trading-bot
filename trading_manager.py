"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    وحدة إدارة الصفقات                                           ║
║                    Trading Position Manager                                     ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import time
import os
from datetime import datetime


class Position:
    """فئة للمركز (الصفقة المفتوحة)"""

    def __init__(self, symbol, quantity, entry_price, stop_loss_price, timestamp=None):
        self.symbol = symbol
        self.quantity = quantity
        self.entry_price = entry_price
        self.current_stop_loss = stop_loss_price
        self.initial_stop_loss = stop_loss_price
        self.highest_price = entry_price
        self.timestamp = timestamp or time.time()
        self.is_active = True

    def update_stop_loss(self, new_price):
        """تحديث وقف الخسارة (Trailing Stop)"""
        if new_price > self.highest_price:
            stop_loss_percent = float(os.getenv("TRAILING_STOP_PERCENT", "1.5"))
            trailing_distance = self.highest_price * (stop_loss_percent / 100)
            new_stop = new_price - trailing_distance

            if new_stop > self.current_stop_loss:
                self.current_stop_loss = new_stop
                self.highest_price = new_price
                return True
        return False

    def check_stop_loss(self, current_price):
        """فحص إذا تم ضرب وقف الخسارة"""
        return current_price <= self.current_stop_loss

    def check_take_profit(self, current_price):
        """فحص إذا تم الوصول لهدف الربح"""
        take_profit = float(os.getenv("TAKE_PROFIT_PERCENT", "10.0"))
        profit_percent = ((current_price - self.entry_price) / self.entry_price) * 100
        return profit_percent >= take_profit

    def get_profit_percent(self, current_price):
        """حساب نسبة الربح الحالية"""
        return ((current_price - self.entry_price) / self.entry_price) * 100

    def get_stop_loss_percent(self, current_price):
        """حساب المسافة لوقف الخسارة"""
        return ((self.current_stop_loss - current_price) / current_price) * 100

    def to_dict(self):
        """تحويل إلى dictionary"""
        return {
            'symbol': self.symbol,
            'quantity': self.quantity,
            'entry_price': self.entry_price,
            'current_stop_loss': self.current_stop_loss,
            'highest_price': self.highest_price,
            'timestamp': self.timestamp,
            'is_active': self.is_active
        }


class TradingManager:
    """فئة لإدارة الصفقات"""

    def __init__(self, binance_client, telegram_notifier):
        self.binance = binance_client
        self.notifier = telegram_notifier
        self.current_position = None
        self.trade_history = []
        self.trades_today = 0
        self.total_profit = 0.0

    def open_position(self, symbol, quantity, entry_price):
        """فتح مركز جديد (شراء)"""
        if self.current_position and self.current_position.is_active:
            self.notifier.send_error(f"يوجد مركز مفتوح بالفعل: {self.current_position.symbol}")
            return False

        try:
            stop_loss_percent = float(os.getenv("STOP_LOSS_PERCENT", "2.0"))
            stop_loss_price = entry_price * (1 - stop_loss_percent / 100)

            order = self.binance.buy_symbol(symbol, quantity)

            if order:
                self.current_position = Position(
                    symbol=symbol,
                    quantity=quantity,
                    entry_price=entry_price,
                    stop_loss_price=stop_loss_price
                )

                self.notifier.send_buy_notification(
                    symbol=symbol,
                    quantity=quantity,
                    price=entry_price,
                    order_id=order.get('orderId', 'TEST')
                )

                print(f"✅ تم فتح مركز: {symbol} بسعر {entry_price}")
                return True

        except Exception as e:
            self.notifier.send_error(f"فشل في فتح المركز: {e}")
            print(f"❌ خطأ في فتح المركز: {e}")
            return False

    def close_position(self, reason="manual"):
        """إغلاق المركز الحالي (بيع)"""
        if not self.current_position or not self.current_position.is_active:
            print("لا يوجد مركز مفتوح")
            return False

        try:
            position = self.current_position
            current_price = self.binance.get_symbol_price(position.symbol)

            if not current_price:
                return False

            profit_percent = position.get_profit_percent(current_price)
            self.total_profit += profit_percent

            order = self.binance.sell_symbol(position.symbol, position.quantity)

            if order:
                trade_record = {
                    'symbol': position.symbol,
                    'entry_price': position.entry_price,
                    'exit_price': current_price,
                    'quantity': position.quantity,
                    'profit_percent': profit_percent,
                    'reason': reason,
                    'timestamp': datetime.now().isoformat()
                }
                self.trade_history.append(trade_record)
                self.trades_today += 1

                self.notifier.send_sell_notification(
                    symbol=position.symbol,
                    quantity=position.quantity,
                    price=current_price,
                    profit_percent=profit_percent,
                    order_id=order.get('orderId', 'TEST')
                )

                position.is_active = False
                self.current_position = None

                print(f"✅ تم إغلاق المركز: {position.symbol} بربح {profit_percent:.2f}%")
                return True

        except Exception as e:
            self.notifier.send_error(f"فشل في إغلاق المركز: {e}")
            print(f"❌ خطأ في إغلاق المركز: {e}")
            return False

    def update_position(self):
        """تحديث المركز - فحص وقف الخسارة والهدف"""
        if not self.current_position or not self.current_position.is_active:
            return None

        try:
            position = self.current_position
            current_price = self.binance.get_symbol_price(position.symbol)

            if not current_price:
                return None

            result = {
                'action': None,
                'current_price': current_price,
                'profit_percent': position.get_profit_percent(current_price),
                'stop_loss': position.current_stop_loss
            }

            if position.check_stop_loss(current_price):
                loss_percent = position.get_profit_percent(current_price)
                self.notifier.send_stop_loss_alert(
                    symbol=position.symbol,
                    current_price=current_price,
                    entry_price=position.entry_price,
                    loss_percent=loss_percent
                )
                self.close_position(reason="stop_loss")
                result['action'] = 'stop_loss'

            elif position.check_take_profit(current_price):
                target_price = position.entry_price * (1 + float(os.getenv("TAKE_PROFIT_PERCENT", "10.0")) / 100)
                self.notifier.send_profit_target_alert(
                    symbol=position.symbol,
                    current_price=current_price,
                    target_price=target_price,
                    profit_percent=result['profit_percent']
                )
                self.close_position(reason="take_profit")
                result['action'] = 'take_profit'

            elif os.getenv("TRAILING_STOP_ENABLED", "True").lower() == "true":
                if position.update_stop_loss(current_price):
                    self.notifier.send_trailing_stop_update(
                        symbol=position.symbol,
                        current_price=current_price,
                        stop_price=position.current_stop_loss,
                        profit_percent=result['profit_percent']
                    )
                    result['action'] = 'trailing_stop_updated'

            return result

        except Exception as e:
            print(f"❌ خطأ في تحديث المركز: {e}")
            return None

    def get_position_status(self):
        """الحصول على حالة المركز الحالي"""
        if not self.current_position:
            return None

        position = self.current_position
        current_price = self.binance.get_symbol_price(position.symbol)

        if not current_price:
            return None

        return {
            'symbol': position.symbol,
            'quantity': position.quantity,
            'entry_price': position.entry_price,
            'current_price': current_price,
            'profit_percent': position.get_profit_percent(current_price),
            'stop_loss': position.current_stop_loss,
            'highest_price': position.highest_price,
            'age_hours': (time.time() - position.timestamp) / 3600
        }

    def force_close_if_needed(self):
        """إغلاق إجباري إذا تجاوز عمر المركز الحد المسموح"""
        if not self.current_position:
            return False

        position = self.current_position
        age_hours = (time.time() - position.timestamp) / 3600
        max_hours = float(os.getenv("MAX_POSITION_HOURS", "24"))

        if age_hours >= max_hours:
            print(f"⚠️ تم تجاوز عمر المركز المسموح ({age_hours:.1f} ساعة)")
            self.notifier.send_message(f"⚠️ إيقاف إجباري للمركز بعد {age_hours:.1f} ساعة")
            self.close_position(reason="max_age")
            return True

        return False
