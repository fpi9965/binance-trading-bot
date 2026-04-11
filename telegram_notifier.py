"""
إشعارات Telegram
"""
import requests
import os

class TelegramNotifier:
    def __init__(self, bot_token=None, chat_id=None, enabled=True):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled and bool(self.bot_token and self.chat_id)
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        
        if self.enabled:
            print("✅ Telegram جاهز")
        else:
            print("⚠️ Telegram غير مفعل - لا توجد إعدادات")
    
    def send_message(self, text):
        if not self.enabled:
            print(f"📱 Telegram معطل: {text[:50]}...")
            return False
        try:
            response = requests.post(self.api_url, data={
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': 'Markdown'
            }, timeout=10)
            if response.status_code == 200:
                print(f"📱 تم الإرسال: {text[:50]}...")
                return True
            else:
                print(f"❌ فشل الإرسال: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ خطأ في الإرسال: {e}")
            return False
    
    def send_trade_signal(self, symbol, data):
        price = data.get('current_price', 0)
        score = data.get('score', 0)
        rec = data.get('recommendation', 'HOLD')
        emoji = "🟢" if rec == "BUY" else ("🔴" if rec == "SELL" else "🟡")
        return self.send_message(f"{emoji} *إشارة {symbol}*\n💰 السعر: ${price:.4f}\n📊 الدرجة: {score}/100\n🎯 التوصية: {rec}")
    
    def send_position_opened(self, data):
        symbol = data.get('symbol', 'N/A')
        quantity = data.get('quantity', 0)
        entry = data.get('entry_price', 0)
        return self.send_message(f"🟢 *صفقة مفتوحة!*\n📊 العملة: {symbol}\n💵 الكمية: {quantity:.4f}\n💰 السعر: ${entry:.4f}")
    
    def send_position_update(self, data, price, pnl_pct, pnl_val):
        symbol = data.get('symbol', 'N/A')
        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        return self.send_message(f"📍 *تحديث {symbol}*\n💰 السعر: ${price:.4f}\n{emoji} الربح: {pnl_pct:+.2f}%")
    
    def send_position_closed(self, data, reason, pnl_pct):
        symbol = data.get('symbol', 'N/A')
        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        return self.send_message(f"🔴 *صفقة مغلقة!*\n📊 العملة: {symbol}\n📋 السبب: {reason}\n{emoji} النتيجة: {pnl_pct:+.2f}%")
    
    def send_heartbeat(self):
        import datetime
        now = datetime.datetime.now().strftime("%H:%M")
        return self.send_message(f"❤️ *Heartbeat*\n⏰ الوقت: {now}")
    
    def send_error(self, msg):
        return self.send_message(f"⚠️ *خطأ!*\n{msg}")
