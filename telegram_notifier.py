"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    وحدة إشعارات Telegram                                        ║
║                    Telegram Notifications Module                                ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import requests
import os


class TelegramNotifier:
    """فئة لإرسال الإشعارات عبر Telegram"""

    def __init__(self):
        """تهيئة الإشعارات"""
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None

    def send_message(self, message, parse_mode='Markdown'):
        """إرسال رسالة نصية"""
        if not self.bot_token or not self.chat_id:
            print(f"[إشعارات] Telegram غير مفعل: {message}")
            return False

        try:
            url = f"{self.api_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': parse_mode
            }
            response = requests.post(url, json=payload, timeout=10)

            if response.status_code == 200:
                return True
            else:
                print(f"خطأ في إرسال الرسالة: {response.text}")
                return False

        except Exception as e:
            print(f"خطأ في إرسال إشعار Telegram: {e}")
            return False

    def send_alert(self, title, message):
        """إرسال تنبيه منسق"""
        full_message = f"🔔 *{title}*\n\n{message}"
        return self.send_message(full_message)

    def send_trade_signal(self, symbol, analysis_data):
        """إرسال إشارة تداول"""
        message = f"""
📊 *إشارة تداول جديدة*

🔹 *العملة:* `{symbol}`
🔹 *السعر الحالي:* ${analysis_data['current_price']:.8f}
🔹 *درجة الجاذبية:* {analysis_data['score']}/100
🔹 *توصية:* {analysis_data['recommendation']}

📈 *المؤشرات:*
• RSI: {analysis_data.get('rsi', 'N/A'):.2f}
• MACD: {analysis_data.get('macd', 'N/A'):.8f}

📝 *الإشارات:*
"""
        for signal in analysis_data.get('signals', []):
            message += f"• {signal}\n"

        return self.send_message(message)

    def send_buy_notification(self, symbol, quantity, price, order_id):
        """إشعار تنفيذ الشراء"""
        message = f"""
🟢 *تم تنفيذ أمر شراء*

🔹 *العملة:* `{symbol}`
🔹 *الكمية:* {quantity}
🔹 *سعر الشراء:* ${price:.8f}
🔹 *رقم الأمر:* `{order_id}`
"""
        return self.send_message(message)

    def send_sell_notification(self, symbol, quantity, price, profit_percent, order_id):
        """إشعار تنفيذ البيع"""
        emoji = "💰" if profit_percent >= 0 else "📉"
        profit_emoji = "📈" if profit_percent >= 0 else "📊"

        message = f"""
{red_emoji} *تم تنفيذ أمر بيع*

🔹 *العملة:* `{symbol}`
🔹 *الكمية:* {quantity}
🔹 *سعر البيع:* ${price:.8f}
🔹 *الربح/الخسارة:* {profit_emoji} {profit_percent:+.2f}%
🔹 *رقم الأمر:* `{order_id}`
"""
        return self.send_message(message)

    def send_stop_loss_alert(self, symbol, current_price, entry_price, loss_percent):
        """تنبيه وقف الخسارة"""
        message = f"""
⚠️ *تنبيه وقف الخسارة*

🔹 *العملة:* `{symbol}`
🔹 *سعر الدخول:* ${entry_price:.8f}
🔹 *السعر الحالي:* ${current_price:.8f}
🔹 *الخسارة:* 📉 {loss_percent:.2f}%

⏰ جاري تنفيذ وقف الخسارة...
"""
        return self.send_message(message)

    def send_trailing_stop_update(self, symbol, current_price, stop_price, profit_percent):
        """تحديث وقف الخسارة المتحرك"""
        message = f"""
📍 *تحديث وقف الخسارة المتحرك*

🔹 *العملة:* `{symbol}`
🔹 *السعر الحالي:* ${current_price:.8f}
🔹 *وقف الخسارة الجديد:* ${stop_price:.8f}
🔹 *الربح الحالي:* 📈 {profit_percent:.2f}%

🔒 تم رفع وقف الخسارة!
"""
        return self.send_message(message)

    def send_profit_target_alert(self, symbol, current_price, target_price, profit_percent):
        """تنبيه الوصول لهدف الربح"""
        message = f"""
🎯 *تم الوصول لهدف الربح!*

🔹 *العملة:* `{symbol}`
🔹 *السعر الحالي:* ${current_price:.8f}
🔹 *الربح:* 💰 {profit_percent:.2f}%

⏰ جاري جني الأرباح...
"""
        return self.send_message(message)

    def send_error(self, error_message):
        """إشعار خطأ"""
        message = f"""
❌ *خطأ في البوت*

{error_message}

⏰ سيتم إعادة المحاولة...
"""
        return self.send_message(message)

    def send_status(self, status_data):
        """إرسال حالة البوت"""
        message = f"""
📋 *حالة البوت*

🔹 *الرصيد:* ${status_data.get('balance', 'N/A')}
🔹 *المركز الحالي:* {status_data.get('current_position', 'لا يوجد')}
🔹 *الصفقات اليوم:* {status_data.get('trades_today', 0)}
🔹 *إجمالي الربح:* {status_data.get('total_profit', 0):+.2f}%
"""
        return self.send_message(message)

    def send_heartbeat(self):
        """إشعار استمرار البوت"""
        message = "✅ *البوت يعمل بشكل طبيعي*\n\n⏰ تم فحص الحالة"
        return self.send_message(message)
