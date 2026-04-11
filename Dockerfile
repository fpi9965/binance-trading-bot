# ═══════════════════════════════════════════════════════════════════════════════
#                    Dockerfile لتشغيل البوت على الخادم
# ═══════════════════════════════════════════════════════════════════════════════

FROM python:3.11-slim

# تعيين مجلد العمل
WORKDIR /app

# تحديث النظام
RUN apt-get update && apt-get upgrade -y

# تثبيت المتطلبات
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ ملفات البوت
COPY . .

# تشغيل البوت
CMD ["python", "main.py"]
