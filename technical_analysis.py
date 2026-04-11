"""
التحليل الفني - حساب المؤشرات الفنية
"""
import numpy as np
import config

class TechnicalAnalysis:
    def __init__(self):
        self.rsi_period = config.RSI_PERIOD
        self.macd_fast = config.MACD_FAST
        self.macd_slow = config.MACD_SLOW
        self.macd_signal = config.MACD_SIGNAL
        self.bb_period = config.BB_PERIOD
    
    def calculate_rsi(self, prices, period=14):
        """حساب RSI"""
        if len(prices) < period + 1:
            return None
        
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        
        if avg_loss == 0:
            return 100
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def calculate_macd(self, prices):
        """حساب MACD"""
        if len(prices) < self.macd_slow:
            return None, None, None
        
        ema_fast = self._ema(prices, self.macd_fast)
        ema_slow = self._ema(prices, self.macd_slow)
        
        macd_line = ema_fast - ema_slow
        signal_line = self._ema(np.array([macd_line] if isinstance(mACD_line := macd_line, (int, float)) else macd_line), self.macd_signal)
        histogram = macd_line - signal_line
        
        return macd_line, signal_line, histogram
    
    def _ema(self, prices, period):
        """حساب المتوسط المتحرك الأسي"""
        prices = np.array(prices)
        multiplier = 2 / (period + 1)
        ema = [prices[0]]
        
        for price in prices[1:]:
            ema.append((price - ema[-1]) * multiplier + ema[-1])
        
        return ema[-1]
    
    def calculate_bollinger_bands(self, prices, period=20):
        """حساب Bollinger Bands"""
        if len(prices) < period:
            return None, None, None
        
        prices = np.array(prices[-period:])
        sma = np.mean(prices)
        std = np.std(prices)
        
        upper_band = sma + (2 * std)
        lower_band = sma - (2 * std)
        
        return upper_band, sma, lower_band
    
    def calculate_sma(self, prices, period):
        """حساب المتوسط المتحرك البسيط"""
        if len(prices) < period:
            return None
        return np.mean(prices[-period:])
    
    def analyze_symbol(self, klines):
        """
        تحليل شامل للرمز
        Returns: dict مع النتيجة والتوصية
        """
        if not klines or len(klines) < 50:
            return None
        
        try:
            # استخراج أسعار الإغلاق
            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            
            current_price = closes[-1]
            
            # حساب المؤشرات
            rsi = self.calculate_rsi(closes, self.rsi_period)
            macd, signal, histogram = self.calculate_macd(closes)
            bb_upper, bb_middle, bb_lower = self.calculate_bollinger_bands(closes, self.bb_period)
            sma_20 = self.calculate_sma(closes, 20)
            sma_50 = self.calculate_sma(closes, 50) if len(closes) >= 50 else None
            
            # تحليل الإشارات
            signals = []
            score = 0
            
            # RSI
            if rsi:
                if rsi < 35:
                    signals.append("RSI في منطقة ذروة البيع")
                    score += 25
                elif rsi < 45:
                    signals.append("RSI إيجابي")
                    score += 15
                elif rsi > 70:
                    signals.append("RSI في منطقة ذروة الشراء")
                    score -= 10
            
            # MACD
            if macd and signal:
                if macd > signal and histogram > 0:
                    signals.append("MACD إيجابي")
                    score += 20
                elif macd < signal:
                    signals.append("MACD سلبي")
                    score -= 15
            
            # Bollinger Bands
            if bb_upper and bb_lower:
                position = (current_price - bb_lower) / (bb_upper - bb_lower)
                if position < 0.3:
                    signals.append("السعر قريب من الحد السفلي")
                    score += 15
                elif position > 0.7:
                    signals.append("السعر قريب من الحد العلوي")
                    score -= 5
            
            # السعر مقابل المتوسطات
            if sma_20 and current_price > sma_20:
                signals.append("السعر فوق المتوسط 20")
                score += 10
            if sma_20 and current_price < sma_20:
                signals.append("السعر تحت المتوسط 20")
                score -= 5
            
            if sma_50 and current_price > sma_50:
                signals.append("السعر فوق المتوسط 50")
                score += 10
            if sma_50 and current_price < sma_50:
                signals.append("السعر تحت المتوسط 50")
                score -= 5
            
            # تحديد التوصية
            if score >= 20:
                recommendation = "BUY"
            elif score <= 5:
                recommendation = "SELL"
            else:
                recommendation = "HOLD"
            
            return {
                'score': score,
                'recommendation': recommendation,
                'rsi': rsi,
                'macd': macd,
                'signal': signal,
                'histogram': histogram,
                'bb_upper': bb_upper,
                'bb_middle': bb_middle,
                'bb_lower': bb_lower,
                'sma_20': sma_20,
                'sma_50': sma_50,
                'current_price': current_price,
                'signals': signals
            }
            
        except Exception as e:
            print(f"❌ خطأ في التحليل: {e}")
            return None
    
    def get_top_picks(self, results, top_n=3):
        """اختيار أفضل العملات"""
        if not results:
            return []
        
        # ترتيب حسب الدرجة
        sorted_results = sorted(
            [(sym, data) for sym, data in results.items() if data['recommendation'] == 'BUY'],
            key=lambda x: x[1]['score'],
            reverse=True
        )
        
        return sorted_results[:top_n]
