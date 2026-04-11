"""
Technical Analysis Module
"""
import numpy as np
import os


class TechnicalAnalysis:
    """فئة للتحليل الفني للعملات"""

    @staticmethod
    def calculate_rsi(prices, period=14):
        """حساب مؤشر RSI"""
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

    @staticmethod
    def _ema(prices, period):
        """حساب المتوسط المتحرك الأسي"""
        prices = np.array(prices)
        multiplier = 2 / (period + 1)
        ema = [prices[0]]
        for i in range(1, len(prices)):
            ema.append((prices[i] - ema[-1]) * multiplier + ema[-1])
        return np.array(ema)

    @staticmethod
    def calculate_macd(prices, fast=12, slow=26, signal=9):
        """حساب MACD"""
        if len(prices) < slow + signal:
            return None, None, None
        ema_fast = TechnicalAnalysis._ema(prices, fast)
        ema_slow = TechnicalAnalysis._ema(prices, slow)
        macd_line = ema_fast - ema_slow
        signal_line = TechnicalAnalysis._ema(macd_line, signal)
        histogram = macd_line - signal_line
        return macd_line[-1], signal_line[-1], histogram[-1]

    @staticmethod
    def calculate_sma(prices, period):
        """حساب المتوسط المتحرك البسيط"""
        if len(prices) < period:
            return None
        return np.mean(prices[-period:])

    @staticmethod
    def analyze_symbol(klines):
        """تحليل شامل للزوج"""
        if not klines or len(klines) < 50:
            return None
        try:
            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            volumes = [float(k[5]) for k in klines]
            prices = np.array(closes)
            current_price = prices[-1]
            rsi_period = int(os.getenv("RSI_PERIOD", "14"))
            rsi = TechnicalAnalysis.calculate_rsi(closes, rsi_period)
            macd, signal, histogram = TechnicalAnalysis.calculate_macd(closes)
            sma_20 = TechnicalAnalysis.calculate_sma(closes, 20)
            avg_volume = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
            score = 0
            signals = []
            if rsi:
                rsi_oversold = float(os.getenv("RSI_OVERSOLD", "35"))
                rsi_overbought = float(os.getenv("RSI_OVERBROUGHT", "70"))
                if rsi < rsi_oversold:
                    score += 25
                    signals.append(f"RSI في ذروة البيع: {rsi:.2f}")
                elif rsi < 45:
                    score += 15
                    signals.append(f"RSI منخفض: {rsi:.2f}")
                elif rsi > rsi_overbought:
                    score -= 10
                    signals.append(f"RSI في ذروة الشراء: {rsi:.2f}")
            if macd and signal:
                if macd > signal and histogram > 0:
                    score += 20
                    signals.append("MACD إيجابي")
                elif macd < signal and histogram < 0:
                    score -= 10
                    signals.append("MACD سلبي")
            if sma_20 and current_price > sma_20:
                score += 20
                signals.append("السعر فوق المتوسط")
            if avg_volume:
                recent_volume = np.mean(volumes[-5:])
                if recent_volume > avg_volume * 1.5:
                    score += 10
                    signals.append("حجم تداول عالي")
            if score >= 40:
                recommendation = "BUY"
            elif score <= 20:
                recommendation = "SELL"
            else:
                recommendation = "HOLD"
            return {
                'score': score,
                'signals': signals,
                'recommendation': recommendation,
                'rsi': rsi,
                'macd': macd,
                'current_price': current_price
            }
        except Exception as e:
            print(f"خطأ في التحليل: {e}")
            return None

    @staticmethod
    def get_top_picks(analysis_results, top_n=3):
        """اختيار أفضل العملات"""
        valid_picks = [
            (symbol, data) for symbol, data in analysis_results.items()
            if data and data['recommendation'] == 'BUY'
        ]
        valid_picks.sort(key=lambda x: x[1]['score'], reverse=True)
        return valid_picks[:top_n]
