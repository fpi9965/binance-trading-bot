"""
Binance Client
"""
from binance.client import Client
from binance.exceptions import BinanceAPIException
import os

class BinanceClient:
    def __init__(self, api_key=None, api_secret=None, testnet=False):
        self.api_key = api_key or os.getenv("BINANCE_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET", "")
        self.test_mode = testnet or os.getenv("TEST_MODE", "false").lower() == "true"
        
        if not self.api_key or not self.api_secret:
            print("⚠️ API keys غير موجودة!")
        
        self.client = Client(self.api_key, self.api_secret)
        self._verify_connection()
    
    def _verify_connection(self):
        try:
            self.client.get_account()
            print("✅ تم الاتصال بـ Binance بنجاح!")
        except Exception as e:
            print(f"⚠️ خطأ: {e}")
    
    def get_account_balance(self):
        try:
            return self.client.get_account()
        except:
            return None
    
    def get_klines(self, symbol, interval, limit=100):
        try:
            return self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
        except:
            return None
    
    def get_symbol_price(self, symbol):
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            return float(ticker['price'])
        except:
            return None
    
    def get_symbol_info(self, symbol):
        try:
            return self.client.get_symbol_info(symbol)
        except:
            return None
    
    def check_market_status(self, symbol):
        try:
            price = self.get_symbol_price(symbol)
            if price and price > 0:
                return True, price
            return False, None
        except:
            return False, None
    
    def buy_symbol(self, symbol, quantity, price):
        if self.test_mode:
            print(f"🧪 [TEST] شراء: {symbol}")
            return {"orderId": "TEST", "status": "NEW"}
        
        try:
            order = self.client.order_market_buy(symbol=symbol, quantity=quantity)
            print(f"✅ تم الشراء: {symbol}")
            return order
        except BinanceAPIException as e:
            if "Market is closed" in str(e):
                print(f"⚠️ {symbol}: السوق مغلق")
            else:
                print(f"خطأ Binance: {e}")
            return None
    
    def sell_symbol(self, symbol, quantity, price):
        if self.test_mode:
            print(f"🧪 [TEST] بيع: {symbol}")
            return {"orderId": "TEST", "status": "NEW"}
        
        try:
            order = self.client.order_market_sell(symbol=symbol, quantity=quantity)
            print(f"✅ تم البيع: {symbol}")
            return order
        except BinanceAPIException as e:
            print(f"خطأ: {e}")
            return None
    
    def get_open_orders(self, symbol=None):
        try:
            if symbol:
                return self.client.get_open_orders(symbol=symbol)
            return self.client.get_open_orders()
        except:
            return []
    
    def get_open_positions(self):
        try:
            orders = self.get_open_orders()
            return [{"symbol": o['symbol'], "orderId": o['orderId']} for o in orders]
        except:
            return []
