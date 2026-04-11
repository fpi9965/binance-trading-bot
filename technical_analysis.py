"""
Technical Analysis
"""
import config

class TechnicalAnalysis:
    def __init__(self):
        self.rsi_period = getattr(config, 'RSI_PERIOD', 14)
        self.macd_fast = getattr(config, 'MACD_FAST', 12)
        self.macd_slow = getattr(config, 'MACD_SLOW', 26)
        self.macd_signal = getattr(config, 'MACD_SIGNAL', 9)
    
    def analyze(self, symbol):
        """تحليل العملة وإرجاع الإشارة"""
        try:
            from binance_client import BinanceClient
            
            client = BinanceClient()
            klines = client.get_klines(symbol, '15m', 100)
            
            if not klines:
                return None
            
            closes = [float(k[4]) for k in klines]
            
            rsi = self._calculate_rsi(closes)
            macd = self._calculate_macd(closes)
            
            score = 0
            if rsi < 30:
                score += 40
            elif rsi > 70:
                score -= 20
            
            if macd['macd'] > macd['signal']:
                score += 30
            else:
                score -= 10
            
            if score >= 60:
                return {
                    'action': 'buy',
                    'score': score,
                    'rsi': rsi,
                    'macd': macd,
                    'current_price': closes[-1]
                }
            elif score <= 20:
                return {
                    'action': 'sell',
                    'score': score,
                    'rsi': rsi,
                    'macd': macd,
                    'current_price': closes[-1]
                }
            
            return {'action': 'hold', 'score': score, 'rsi': rsi}
            
        except Exception as e:
            print(f"خطأ في التحليل: {e}")
            return None
    
    def _calculate_rsi(self, closes, period=14):
        if len(closes) < period + 1:
            return 50
        
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]
        
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        
        if avg_loss == 0:
            return 100
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def _calculate_macd(self, closes, fast=12, slow=26, signal=9):
        if len(closes) < slow + signal:
            return {'macd': 0, 'signal': 0, 'histogram': 0}
        
        ema_fast = self._ema(closes, fast)
        ema_slow = self._ema(closes, slow)
        macd_line = ema_fast - ema_slow
        
        macd_values = []
        for i in range(len(closes) - slow + 1):
            e_f = self._ema(closes[:slow+i], fast)
            e_s = self._ema(closes[:slow+i], slow)
            macd_values.append(e_f - e_s)
        
        signal_line = self._ema(macd_values, signal) if len(macd_values) >= signal else macd_values[-1]
        
        return {
            'macd': macd_line,
            'signal': signal_line,
            'histogram': macd_line - signal_line
        }
    
    def _ema(self, data, period):
        if len(data) < period:
            return data[-1] if data else 0
        
        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period
        
        for price in data[period:]:
            ema = (price - ema) * multiplier + ema
        
        return ema
