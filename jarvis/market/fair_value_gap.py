import logging
import pandas as pd
import numpy as np

logger = logging.getLogger('JARVIS_FVG')

class FairValueGapEngine:
    """ICT Fair Value Gap (FVG) & Order Block (OB) Detection Engine.
    Detects institutional price imbalance zones, Consequent Encroachment (CE),
    and Optimal Trade Entry (OTE) Fibonacci retracement pockets."""

    def analyze(self, df: pd.DataFrame) -> dict:
        """Analyze dataframe for FVGs, Order Blocks, and OTE zones."""
        result = {
            'bullish_fvgs': [],
            'bearish_fvgs': [],
            'bullish_obs': [],
            'bearish_obs': [],
            'active_bullish_fvg': None,
            'active_bearish_fvg': None,
            'active_bullish_ob': None,
            'active_bearish_ob': None,
            'price_in_fvg': False,
            'price_in_bullish_fvg': False,
            'price_in_bearish_fvg': False,
            'price_in_ob': False,
            'price_in_bullish_ob': False,
            'price_in_bearish_ob': False,
            'ote_zone': {},
            'is_price_in_ote': False,
            'fvg_ob_confluence': False
        }
        
        required_cols = ['open', 'high', 'low', 'close']
        if df is None or len(df) < 10 or not all(col in df.columns for col in required_cols):
            return result
            
        opens = df['open'].values
        highs = df['high'].values
        lows = df['low'].values
        closes = df['close'].values
        n_bars = len(df)
        current_price = float(closes[-1])
        
        # 14 period ATR
        if n_bars >= 15:
            tr1 = highs[-14:] - lows[-14:]
            tr2 = np.abs(highs[-14:] - closes[-15:-1])
            tr3 = np.abs(lows[-14:] - closes[-15:-1])
            tr = np.maximum(tr1, np.maximum(tr2, tr3))
            atr_14 = float(np.mean(tr))
        else:
            atr_14 = float(highs.max() - lows.min()) / max(1, n_bars)
            
        atr_14 = max(atr_14, 1e-5)

        # 1. FVG Detection with Displacement and Consequent Encroachment (Last 50 candles)
        fvg_lookback = min(50, n_bars)
        fvg_start = n_bars - fvg_lookback
        bullish_fvgs = []
        bearish_fvgs = []
        
        min_gap_size = 0.15 * atr_14
        min_disp_body = 0.75 * atr_14
        
        for i in range(fvg_start + 2, n_bars):
            c1_high = float(highs[i - 2])
            c1_low = float(lows[i - 2])
            c2_open = float(opens[i - 1])
            c2_close = float(closes[i - 1])
            c3_high = float(highs[i])
            c3_low = float(lows[i])
            
            c2_body = abs(c2_close - c2_open)
            has_displacement = c2_body >= min_disp_body
            
            # Bullish FVG: c1_high < c3_low with upward displacement on candle 2
            if c1_high < c3_low:
                gap_size = c3_low - c1_high
                if gap_size >= min_gap_size and (has_displacement or c2_close > c2_open):
                    top = c3_low
                    bottom = c1_high
                    ce = (top + bottom) / 2.0  # Consequent Encroachment (50% midpoint)
                    mitigated = bool(np.any(lows[i + 1:] <= bottom)) if i + 1 < n_bars else False
                    tested_ce = bool(np.any(lows[i + 1:] <= ce)) if i + 1 < n_bars else False
                    bullish_fvgs.append({
                        'top': top,
                        'bottom': bottom,
                        'ce': ce,
                        'direction': 'BULLISH',
                        'bar_idx': i - 2,
                        'mitigated': mitigated,
                        'tested_ce': tested_ce
                    })
                
            # Bearish FVG: c1_low > c3_high with downward displacement on candle 2
            elif c1_low > c3_high:
                gap_size = c1_low - c3_high
                if gap_size >= min_gap_size and (has_displacement or c2_close < c2_open):
                    top = c1_low
                    bottom = c3_high
                    ce = (top + bottom) / 2.0  # Consequent Encroachment (50% midpoint)
                    mitigated = bool(np.any(highs[i + 1:] >= top)) if i + 1 < n_bars else False
                    tested_ce = bool(np.any(highs[i + 1:] >= ce)) if i + 1 < n_bars else False
                    bearish_fvgs.append({
                        'top': top,
                        'bottom': bottom,
                        'ce': ce,
                        'direction': 'BEARISH',
                        'bar_idx': i - 2,
                        'mitigated': mitigated,
                        'tested_ce': tested_ce
                    })

        result['bullish_fvgs'] = bullish_fvgs
        result['bearish_fvgs'] = bearish_fvgs
        
        # 2. Institutional Order Block (OB) Detection
        ob_start = max(1, n_bars - 30)
        bullish_obs = []
        bearish_obs = []
        
        for i in range(ob_start + 1, n_bars):
            prev_o, prev_c = float(opens[i - 1]), float(closes[i - 1])
            prev_h, prev_l = float(highs[i - 1]), float(lows[i - 1])
            curr_o, curr_c = float(opens[i]), float(closes[i])
            
            body_size = abs(curr_c - curr_o)
            if body_size > (1.25 * atr_14):
                if curr_c > curr_o and prev_c < prev_o:
                    # Bullish OB
                    top, bottom = prev_h, prev_l
                    mitigated = bool(np.any(lows[i + 1:] <= bottom)) if i + 1 < n_bars else False
                    bullish_obs.append({
                        'top': top,
                        'bottom': bottom,
                        'direction': 'BULLISH',
                        'bar_idx': i - 1,
                        'mitigated': mitigated
                    })
                    
                elif curr_c < curr_o and prev_c > prev_o:
                    # Bearish OB
                    top, bottom = prev_h, prev_l
                    mitigated = bool(np.any(highs[i + 1:] >= top)) if i + 1 < n_bars else False
                    bearish_obs.append({
                        'top': top,
                        'bottom': bottom,
                        'direction': 'BEARISH',
                        'bar_idx': i - 1,
                        'mitigated': mitigated
                    })

        result['bullish_obs'] = bullish_obs
        result['bearish_obs'] = bearish_obs
        
        # Active unmitigated FVGs and OBs with explicit directional flags
        unmitigated_bullish_fvgs = [f for f in bullish_fvgs if not f['mitigated']]
        unmitigated_bearish_fvgs = [f for f in bearish_fvgs if not f['mitigated']]
        
        for f in reversed(unmitigated_bullish_fvgs):
            if f['bottom'] <= current_price <= f['top']:
                result['active_bullish_fvg'] = f
                result['price_in_fvg'] = True
                result['price_in_bullish_fvg'] = True
                break
        else:
            if unmitigated_bullish_fvgs:
                result['active_bullish_fvg'] = unmitigated_bullish_fvgs[-1]
                
        for f in reversed(unmitigated_bearish_fvgs):
            if f['bottom'] <= current_price <= f['top']:
                result['active_bearish_fvg'] = f
                result['price_in_fvg'] = True
                result['price_in_bearish_fvg'] = True
                break
        else:
            if unmitigated_bearish_fvgs:
                result['active_bearish_fvg'] = unmitigated_bearish_fvgs[-1]

        unmitigated_bullish_obs = [ob for ob in bullish_obs if not ob['mitigated']]
        unmitigated_bearish_obs = [ob for ob in bearish_obs if not ob['mitigated']]
        
        for ob in reversed(unmitigated_bullish_obs):
            if ob['bottom'] <= current_price <= ob['top']:
                result['active_bullish_ob'] = ob
                result['price_in_ob'] = True
                result['price_in_bullish_ob'] = True
                break
        else:
            if unmitigated_bullish_obs:
                result['active_bullish_ob'] = unmitigated_bullish_obs[-1]
                
        for ob in reversed(unmitigated_bearish_obs):
            if ob['bottom'] <= current_price <= ob['top']:
                result['active_bearish_ob'] = ob
                result['price_in_ob'] = True
                result['price_in_bearish_ob'] = True
                break
        else:
            if unmitigated_bearish_obs:
                result['active_bearish_ob'] = unmitigated_bearish_obs[-1]

        # 3. Confluence Detection (Active FVG overlaps with Active OB of SAME direction)
        active_fvgs = []
        if result['active_bullish_fvg']: active_fvgs.append(result['active_bullish_fvg'])
        if result['active_bearish_fvg']: active_fvgs.append(result['active_bearish_fvg'])
        
        active_obs = []
        if result['active_bullish_ob']: active_obs.append(result['active_bullish_ob'])
        if result['active_bearish_ob']: active_obs.append(result['active_bearish_ob'])
        
        for fvg in active_fvgs:
            for ob in active_obs:
                if fvg.get('direction') == ob.get('direction') and fvg['bottom'] <= ob['top'] and fvg['top'] >= ob['bottom']:
                    result['fvg_ob_confluence'] = True
                    break

        # 4. True ICT OTE Fibonacci Golden Pocket Zone Calculation (Recent 60 bars)
        pivot_window = 5
        ote_start = max(0, n_bars - 60)
        h_slice = highs[ote_start:]
        l_slice = lows[ote_start:]
        n_slice = len(h_slice)
        
        if n_slice > pivot_window * 2:
            swing_highs = []
            swing_lows = []
            
            for i in range(pivot_window, n_slice - pivot_window):
                if h_slice[i] == np.max(h_slice[i - pivot_window : i + pivot_window + 1]):
                    swing_highs.append((float(h_slice[i]), i))
                if l_slice[i] == np.min(l_slice[i - pivot_window : i + pivot_window + 1]):
                    swing_lows.append((float(l_slice[i]), i))
                    
            if swing_highs and swing_lows:
                recent_sh, sh_idx = swing_highs[-1]
                recent_sl, sl_idx = swing_lows[-1]
                swing_range = recent_sh - recent_sl
                
                if swing_range > 0:
                    # In ICT SMC:
                    # If Swing High is more recent than Swing Low, the impulse was upward.
                    # Price pulling back down into the 61.8%-78.6% discount zone is a BULLISH OTE.
                    is_bullish_leg = (sh_idx >= sl_idx)
                    if is_bullish_leg:
                        ote_62 = recent_sh - 0.618 * swing_range
                        ote_705 = recent_sh - 0.705 * swing_range
                        ote_79 = recent_sh - 0.786 * swing_range
                        in_ote = (ote_79 <= current_price <= ote_62)
                        result['ote_zone'] = {
                            'ote_62': ote_62,
                            'ote_705': ote_705,
                            'ote_79': ote_79,
                            'direction': 'BULLISH',
                            'is_price_in_ote': in_ote
                        }
                        result['is_price_in_ote'] = in_ote
                    else:
                        # If Swing Low is more recent, the impulse was downward.
                        # Price retracing up into the 61.8%-78.6% premium zone is a BEARISH OTE.
                        ote_62 = recent_sl + 0.618 * swing_range
                        ote_705 = recent_sl + 0.705 * swing_range
                        ote_79 = recent_sl + 0.786 * swing_range
                        in_ote = (ote_62 <= current_price <= ote_79)
                        result['ote_zone'] = {
                            'ote_62': ote_62,
                            'ote_705': ote_705,
                            'ote_79': ote_79,
                            'direction': 'BEARISH',
                            'is_price_in_ote': in_ote
                        }
                        result['is_price_in_ote'] = in_ote
                    
        return result
