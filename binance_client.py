"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    وحدة اتصال Binance                                         ║
║                    Binance Connection Module                                  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os
from binance.client import Client
from binance.exceptions import BinanceAPIException


class BinanceClient:
    """فئة للتعامل مع API Binance"""

    def __init__(self):
        """تهيئة اتصال Binance"""
        api_key = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_API_SECRET", "")
        self.client = Client(api_key, api_secret)
        self.test_mode = os.getenv("TEST_MODE", "True").lower() == "true"

    def get_account_balance(self):
        """الحصول على رصيد الحساب"""
        try:
            if self.test_mode:
                return {"USDT": {"free": 100.0, "locked": 0.0}}
            account = self.client.get_account()
            balances = {}
            for asset in account['balances']:
                if float(asset['free']) > 0 or float(asset['locked']) > 0:
                    balances[asset['asset']] = {
                        'free': float(asset['free']),
                        'locked': float(asset['locked'])
                    }
            return balances
        except BinanceAPIException as e:
            print(f"خطأ في جلب الرصيد: {e}")
            return None

    def get_symbol_price(self, symbol):
        """الحصول على السعر الحالي لزوج معين"""
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            return float(ticker['price'])
        except BinanceAPIException as e:
            print(f"خطأ في جلب السعر: {e}")
            return None

    def get_klines(self, symbol, interval, limit=100):
        """الحصول على بيانات الشموع"""
        try:
            klines = self.client.get_klines(
                symbol=symbol,
                interval=interval,
                limit=limit
            )
            return klines
        except BinanceAPIException as e:
            print(f"خطأ في جلب الشموع: {e}")
            return None

    def get_symbol_info(self, symbol):
        """الحصول على معلومات الزوج"""
        try:
            info = self.client.get_symbol_info(symbol=symbol)
            return info
        except BinanceAPIException as e:
            print(f"خطأ في جلب معلومات الزوج: {e}")
            return None

    def buy_symbol(self, symbol, quantity):
        """تنفيذ أمر شراء"""
        try:
            if self.test_mode:
                print(f"[اختبار] تم محاكاة شراء {quantity} من {symbol}")
                return {
                    'orderId': 123456,
                    'symbol': symbol,
                    'price': self.get_symbol_price(symbol),
                    'qty': quantity,
                    'status': 'FILLED'
                }
            symbol_info = self.get_symbol_info(symbol)
            step_size = 0.0
            for filter in symbol_info['filters']:
                if filter['filterType'] == 'LOT_SIZE':
                    step_size = float(filter['stepSize'])
                    break
            quantity = float(quantity)
            quantity = round(quantity - (quantity % step_size), 8)
            order = self.client.order_market_buy(symbol=symbol, quantity=quantity)
            return order
        except BinanceAPIException as e:
            print(f"خطأ في تنفيذ أمر الشراء: {e}")
            return None

    def sell_symbol(self, symbol, quantity):
        """تنفيذ أمر بيع"""
        try:
            if self.test_mode:
                print(f"[اختبار] تم محاكاة بيع {quantity} من {symbol}")
                return {
                    'orderId': 789012,
                    'symbol': symbol,
                    'price': self.get_symbol_price(symbol),
                    'qty': quantity,
                    'status': 'FILLED'
                }
            symbol_info = self.get_symbol_info(symbol)
            step_size = 0.0
            for filter in symbol_info['filters']:
                if filter['filterType'] == 'LOT_SIZE':
                    step_size = float(filter['stepSize'])
                    break
            quantity = float(quantity)
            quantity = round(quantity - (quantity % step_size), 8)
            order = self.client.order_market_sell(symbol=symbol, quantity=quantity)
            return order
        except BinanceAPIException as e:
            print(f"خطأ في تنفيذ أمر البيع: {e}")
            return None

    def get_open_orders(self, symbol=None):
        """الحصول على الأوامر المفتوحة"""
        try:
            if symbol:
                return self.client.get_open_orders(symbol=symbol)
            return self.client.get_open_orders()
        except BinanceAPIException as e:
            print(f"خطأ في جلب الأوامر المفتوحة: {e}")
            return []

    def cancel_order(self, symbol, order_id):
        """إلغاء أمر"""
        try:
            return self.client.cancel_order(symbol=symbol, orderId=order_id)
        except BinanceAPIException as e:
            print(f"خطأ في إلغاء الأمر: {e}")
            return None

    def get_all_tickers(self):
        """الحصول على جميع أسعار العملات"""
        try:
            return self.client.get_all_tickers()
        except BinanceAPIException as e:
            print(f"خطأ في جلب الأسعار: {e}")
            return None
