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
            print("⚠️  إعدادات Telegram غير موجودة - لن يتم إرسال إشعارات")
        else:
            print("✅ تم تهيئة إشعارات Telegram")
    
    def send_message(self, text):
        """إرسال رسالة نصية"""
        if not self.bot_token or not self.chat_id:
            return False
        
        try:
            data = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': 'Markdown'
            }
            response = requests.post(self.api_url, data=data, timeout=10)
            return response.status_code == 200
        except Exception as e:
            print(f"❌ خطأ في إرسال الرسالة: {e}")
            return False
    
    def send_trade_signal(self, symbol, analysis_data):
        """إرسال إشارة تداول"""
        if not self.bot_token or not self.chat_id:
            print(f"📊 إشارة تداول: {symbol} - {analysis_data['recommendation']}")
            return False
        
        price = analysis_data.get('current_price', 0)
        score = analysis_data.get('score', 0)
        recommendation = analysis_data.get('recommendation', 'HOLD')
        rsi = analysis_data.get('rsi', 0)
        macd = analysis_data.get('macd', 0)
        signals = analysis_data.get('signals', [])
        
        # اختيار الإيموجي حسب التوصية
        emoji = "🟢" if recommendation == "BUY" else ("🔴" if recommendation == "SELL" else "🟡")
        
        text = f"""
{emoji} *إشارة تداول جديدة*

📊 العملة: *{symbol}*
💰 السعر الحالي: ${price:.2f}
📈 الدرجة: {score}/100
🎯 التوصية: *{recommendation}*

📉 المؤشرات:
• RSI: {rsi:.2f}
• MACD: {macd:.4f}

📋 الإشارات:
"""
        for signal in signals:
            text += f"• {signal}\n"
        
        return self.send_message(text)
    
    def send_position_opened(self, data):
        """إشعار بفتح صفقة"""
        symbol = data['symbol']
        quantity = data['quantity']
        entry_price = data['entry_price']
        stop_loss = data['stop_loss']
        take_profit = data['take_profit']
        
        text = f"""
🟢 *تم فتح صفقة!*

📊 العملة: *{symbol}*
💵 الكمية: {quantity}
💰 سعر الدخول: ${entry_price:.2f}

🛑 Stop Loss: ${stop_loss:.2f}
🎯 جني الأرباح: ${take_profit:.2f}
"""
        return self.send_message(text)
    
    def send_position_update(self, data, current_price, pnl_percent, pnl_value):
        """إشعار بتحديث الصفقة"""
        symbol = data['symbol']
        
        emoji = "🟢" if pnl_percent >= 0 else "🔴"
        
        text = f"""
📍 *تحديث الصفقة*

📊 {symbol}
💰 السعر الحالي: ${current_price:.2f}
{emoji} الربح/الخسارة: {pnl_percent:+.2f}% (${pnl_value:+.2f})
"""
        return self.send_message(text)
    
    def send_position_closed(self, data, reason, pnl_percent):
        """إشعار بإغلاق الصفقة"""
        symbol = data['symbol']
        entry_price = data['entry_price']
        
        reason_text = {
            'take_profit': '🎯 جني الأرباح',
            'stop_loss': '🛑 Stop Loss',
            'trailing_stop': '🦶 Trailing Stop',
            'manual': '✋ يدوي'
        }.get(reason, reason)
        
        emoji = "🟢" if pnl_percent >= 0 else "🔴"

        text = f"""
🔴 *تم إغلاق الصفقة!*

📊 العملة: {symbol}
💰 سعر الدخول: ${entry_price:.2f}
📋 السبب: {reason_text}
{emoji} النتيجة: {pnl_percent:+.2f}%
"""
        return self.send_message(text)
    
    def send_heartbeat(self):
        """إشعار دوري (heartbeat)"""
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        text = f"""
❤️ *Heartbeat*

⏰ الوقت: {now}
🤖 البوت يعمل بشكل طبيعي
"""
        return self.send_message(text)
    
    def send_error(self, error_msg):
        """إشعار خطأ"""
        text = f"""
⚠️ *خطأ!*

{error_msg}
"""
        return self.send_message(text)
