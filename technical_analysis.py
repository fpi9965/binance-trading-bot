"""Technical Analysis - محسن للفرص"""
import config


class TechnicalAnalysis:
    def __init__(self, binance_client=None):
        self.binance = binance_client

    def analyze(self, symbol):
        try:
            klines = self.binance.get_klines(symbol, '15m', 50)
            if not klines or len(klines) < 20:
                return None

            closes = [float(k[4]) for k in klines]
            current_price = closes[-1]
            
            # RSI
            rsi = self._calculate_rsi(closes, 14)
            
            # MACD
            macd = self._calculate_macd(closes)
            
            # Bollinger Bands
            bb = self._calculate_bollinger(closes)
            
            score = 0
            reasons = []
            
            # RSI - ذروة البيع أفضل فرصة
            if rsi < 30:
                score += 45
                reasons.append(f"RSI={rsi:.1f} ذروة بيع")
            elif rsi < 40:
                score += 25
                reasons.append(f"RSI={rsi:.1f} منطقة شراء")
            elif rsi < 50:
                score += 10
                reasons.append(f"RSI={rsi:.1f} محايد")
            
            # MACD - إيجابي يعني صعود
            if macd['histogram'] > 0:
                score += 35
                reasons.append("MACD إيجابي")
            elif macd['histogram'] > -0.3:
                score += 15
                reasons.append("MACD صاعد")
            
            # Bollinger Bands - السعر عند الحد السفلي فرصته عالية
            if bb and current_price < bb['lower']:
                score += 25
                reasons.append("عند BB السفلي")
            elif bb and current_price < bb['middle']:
                score += 12
                reasons.append("أسفل منتصف BB")
            
            # فرصة ذهبية - كل المؤشرات مع بعضها
            if rsi < 35 and macd['histogram'] > 0:
                score += 20
                reasons.append("فرصة ذهبية!")
            
            return {
                'action': 'buy' if score >= 50 else 'hold',
                'score': score,
                'rsi': rsi,
                'macd_hist': macd['histogram'],
                'current_price': current_price,
                'reasons': reasons
            }

        except Exception as e:
            return None

    def _calculate_rsi(self, closes, period=14):
        if len(closes) < period + 1:
            return 50
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]
        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _calculate_macd(self, closes, fast=12, slow=26, signal=9):
        if len(closes) < slow + signal:
            return {'macd': 0, 'signal': 0, 'histogram': 0}
        ema_fast = self._ema(closes, fast)
        ema_slow = self._ema(closes, slow)
        macd_line = ema_fast - ema_slow
        signal_line = macd_line * 0.9
        return {'macd': macd_line, 'signal': signal_line, 'histogram': macd_line - signal_line}

    def _calculate_bollinger(self, closes, period=20, std_dev=2):
        if len(closes) < period:
            return None
        sma = sum(closes[-period:]) / period
        variance = sum((c - sma) ** 2 for c in closes[-period:]) / period
        std = variance ** 0.5
        return {'upper': sma + std_dev * std, 'middle': sma, 'lower': sma - std_dev * std}

    def _ema(self, data, period):
        if len(data) < period:
            return data[-1] if data else 0
        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period
        for price in data[period:]:
            ema = (price - ema) * multiplier + ema
        return ema
