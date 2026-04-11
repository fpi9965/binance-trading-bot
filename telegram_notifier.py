"""
إشعارات Telegram
"""
import requests
import config
import time

class TelegramNotifier:
    def __init__(self):
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        
        if not self.bot_token or not self.chat_id:
            print("⚠️ Telegram غير مفعل")
        else:
            print("✅ Telegram جاهز")
    
    def send_message(self, text):
        if not self.bot_token or not self.chat_id:
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
        if not self.bot_token:
            return False
        
        price = data.get('current_price', 0)
        score = data.get('score', 0)
        rec = data.get('recommendation', 'HOLD')
        rsi = data.get('rsi', 0)
        
        emoji = "🟢" if rec == "BUY" else ("🔴" if rec == "SELL" else "🟡")
        
        return self.send_message(f"""
{emoji} *إشارة {symbol}*

💰 السعر: ${price:.4f}
📊 الدرجة: {score}/100
🎯 التوصية: {rec}
📉 RSI: {rsi:.1f}
""")
    
    def send_position_opened(self, data):
        return self.send_message(f"""
🟢 *صفقة مفتوحة!*

📊 {data['symbol']}
💵 الكمية: {data['quantity']:.4f}
💰 السعر: ${data['entry_price']:.4f}
🛑 SL: ${data['stop_loss']:.4f}
🎯 TP: ${data['take_profit']:.4f}
""")
    
    def send_position_update(self, data, price, pnl_pct, pnl_val):
        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        return self.send_message(f"""
📍 *تحديث {data['symbol']}*
💰 السعر: ${price:.4f}
{emoji} الربح: {pnl_pct:+.2f}%
""")
    
    def send_position_closed(self, data, reason, pnl_pct):
        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        return self.send_message(f"""
🔴 *صفقة مغلقة!*

📊 {data['symbol']}
📋 السبب: {reason}
{emoji} النتيجة: {pnl_pct:+.2f}%
""")
    
    def send_heartbeat(self):
        import datetime
        now = datetime.datetime.now().strftime("%H:%M")
        return self.send_message(f"❤️ Heartbeat {now}")
    
    def send_error(self, msg):
        return self.send_message(f"⚠️ خطأ: {msg}")
