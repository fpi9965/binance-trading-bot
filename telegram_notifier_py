"""
إشعارات Telegram
"""
import requests
import os

class TelegramNotifier:
    def __init__(self, bot_token=None, chat_id=None, enabled=True):
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "") or bot_token or ""
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "") or chat_id or ""
        self.enabled = bool(self.bot_token and self.chat_id)
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        
        if self.enabled:
            print("✅ Telegram جاهز")
        else:
            print("⚠️ Telegram غير مفعل")
    
    def send_message(self, text):
        if not self.enabled:
            return False
        try:
            response = requests.post(self.api_url, data={
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': 'Markdown'
            }, timeout=10)
            return response.status_code == 200
        except:
            return False
    
    def send_position_opened(self, data):
        symbol = data.get('symbol', 'N/A')
        quantity = data.get('quantity', 0)
        entry = data.get('entry_price', 0)
        return self.send_message(f"🟢 *صفقة مفتوحة!*\n📊 {symbol}\n💵 الكمية: {quantity:.4f}\n💰 السعر: ${entry:.4f}")
    
    def send_position_closed(self, data, reason, pnl_pct):
        symbol = data.get('symbol', 'N/A')
        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        return self.send_message(f"🔴 *صفقة مغلقة!*\n📊 {symbol}\n📋 {reason}\n{emoji} النتيجة: {pnl_pct:+.2f}%")
    
    def send_error(self, msg):
        return self.send_message(f"⚠️ خطأ: {msg}")
