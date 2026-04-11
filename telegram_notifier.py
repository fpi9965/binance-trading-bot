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
            print("⚠️ Telegram غير مفعل")
    
    def send_message(self, text):
        if not self.enabled:
            return False
        try:
            requests.post(self.api_url, data={
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': 'Markdown'
            }, timeout=10)
            return True
        except:
            return False
    
    def send_trade_signal(self, symbol, data):
        if not self.enabled:
            return False
        price = data.get('current_price', 0)
        score = data.get('score', 0)
        rec = data.get('recommendation', 'HOLD')
        emoji = "🟢" if rec == "BUY" else ("🔴" if rec == "SELL" else "🟡")
        return self.send_message(f"{emoji} *إشارة {symbol}*\n💰 السعر: ${price:.4f}\n📊 الدرجة: {score}/100")
    
    def send_position_opened(self, data):
        return self.send_message(f"🟢 *صفقة مفتوحة!*\n📊 {data['symbol']}\n💵 الكمية: {data['quantity']:.4f}\n💰 السعر: ${data['entry_price']:.4f}")
    
    def send_position_update(self, data, price, pnl_pct, pnl_val):
        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        return self.send_message(f"📍 *تحديث {data['symbol']}*\n💰 السعر: ${price:.4f}\n{emoji} الربح: {pnl_pct:+.2f}%")
    
    def send_position_closed(self, data, reason, pnl_pct):
        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        return self.send_message(f"🔴 *صفقة مغلقة!*\n📊 {data['symbol']}\n📋 السبب: {reason}\n{emoji} النتيجة: {pnl_pct:+.2f}%")
    
    def send_heartbeat(self):
        import datetime
        now = datetime.datetime.now().strftime("%H:%M")
        return self.send_message(f"❤️ Heartbeat {now}")
    
    def send_error(self, msg):
        return self.send_message(f"⚠️ خطأ: {msg}")
