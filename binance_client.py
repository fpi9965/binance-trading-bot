"""
عميل Binance للتعامل مع API
"""
from binance.client import Client
from binance.exceptions import BinanceAPIException
import config
import time

class BinanceClient:
    def __init__(self):
        self.client = None
        self.test_mode = config.TEST_MODE
        self._connect()
    
    def _connect(self):
        try:
            if not config.BINANCE_API_KEY or not config.BINANCE_API_SECRET:
                raise Exception("API keys غير موجودة! تأكد من إضافة BINANCE_API_KEY و BINANCE_API_SECRET")
            
            self.client = Client(
                config.BINANCE_API_KEY,
                config.BINANCE_API_SECRET
            )
            
            # التحقق من الاتصال
            account = self.client.get_account()
            print(f"✅ تم الاتصال بـ Binance بنجاح!")
            print(f"💰 الرصيد USDT: {self._get_usdt_balance(account)}")
            
            if self.test_mode:
                print("⚠️  وضع الاختبار مفعل - لن يتم تنفيذ صفقات حقيقية!")
            else:
                print("🔴 وضع التداول الحقيقي مفعل - سيتم تنفيذ صفقات!")
                
        except BinanceAPIException as e:
            print(f"❌ خطأ في الاتصال بـ Binance: {e}")
            raise
        except Exception as e:
            print(f"❌ خطأ: {e}")
            raise
    
    def _get_usdt_balance(self, account):
        for balance in account['balances']:
            if balance['asset'] == 'USDT':
                return float(balance['free'])
        return 0
    
    def get_account_balance(self):
        try:
            return self.client.get_account()
        except Exception as e:
            print(f"❌ خطأ في جلب الرصيد: {e}")
            return None
    
    def get_klines(self, symbol, interval, limit=100):
        try:
            klines = self.client.get_klines(
                symbol=symbol,
                interval=interval,
                limit=limit
            )
            return klines
        except Exception as e:
            print(f"❌ خطأ في جلب البيانات لـ {symbol}: {e}")
            return None
    
    def get_symbol_price(self, symbol):
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            return float(ticker['price'])
        except Exception as e:
            print(f"❌ خطأ في جلب السعر لـ {symbol}: {e}")
            return None
    
    def get_symbol_info(self, symbol):
        try:
            return self.client.get_symbol_info(symbol)
        except Exception as e:
            print(f"❌ خطأ في جلب معلومات {symbol}: {e}")
            return None
    
    def buy_symbol(self, symbol, quantity, price):
        """
        تنفيذ أمر شراء
        """
        if self.test_mode:
            print(f"🧪 [TEST MODE] محاكاة شراء: {symbol}")
            print(f"   الكمية: {quantity}")
            print(f"   السعر: {price}")
            return {"orderId": "TEST_ORDER_123", "status": "TEST"}
        
        try:
            # الحصول على دقة الكمية المناسبة
            symbol_info = self.get_symbol_info(symbol)
            step_size = 0.0
            for f in symbol_info['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    step_size = float(f['stepSize'])
                    break
            
            # تقريب الكمية
            quantity = float(quantity)
            quantity = round(quantity - (quantity % step_size), 8)
            
            print(f"🟢 جاري إرسال أمر شراء...")
            print(f"   العملة: {symbol}")
            print(f"   الكمية: {quantity}")
print(f"   السعر: {price}")
            
            order = self.client.order_market_buy(
                symbol=symbol,
                quantity=quantity
            )
            
            print(f"✅ تم تنفيذ أمر الشراء بنجاح!")
            print(f"   رقم الأمر: {order['orderId']}")
            print(f"   الحالة: {order['status']}")
            
            return order
            
        except BinanceAPIException as e:
            print(f"❌ خطأ في أمر الشراء: {e}")
            return None
        except Exception as e:
            print(f"❌ خطأ غير متوقع: {e}")
            return None
    
    def sell_symbol(self, symbol, quantity, price):
        """
        تنفيذ أمر بيع
        """
        if self.test_mode:
            print(f"🧪 [TEST MODE] محاكاة بيع: {symbol}")
            return {"orderId": "TEST_SELL_123", "status": "TEST"}
        
        try:
            # الحصول على دقة الكمية المناسبة
            symbol_info = self.get_symbol_info(symbol)
            step_size = 0.0
            for f in symbol_info['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    step_size = float(f['stepSize'])
                    break
            
            # تقريب الكمية
            quantity = float(quantity)
            quantity = round(quantity - (quantity % step_size), 8)
            
            print(f"🔴 جاري إرسال أمر بيع...")
            print(f"   العملة: {symbol}")
            print(f"   الكمية: {quantity}")
            
            order = self.client.order_market_sell(
                symbol=symbol,
                quantity=quantity
            )
            
            print(f"✅ تم تنفيذ أمر البيع بنجاح!")
            print(f"   رقم الأمر: {order['orderId']}")
            
            return order
            
        except BinanceAPIException as e:
            print(f"❌ خطأ في أمر البيع: {e}")
            return None
        except Exception as e:
            print(f"❌ خطأ غير متوقع: {e}")
            return None
    
    def get_order(self, symbol, order_id):
        try:
            return self.client.get_order(symbol=symbol, orderId=order_id)
        except Exception as e:
            return None
    
    def get_open_orders(self, symbol=None):
        try:
            if symbol:
                return self.client.get_open_orders(symbol=symbol)
            return self.client.get_open_orders()
        except Exception as e:
            return []
