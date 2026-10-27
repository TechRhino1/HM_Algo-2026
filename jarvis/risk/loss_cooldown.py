import logging
from typing import Tuple, Dict

logger = logging.getLogger("JARVIS_LossCooldown")

class LossCooldownManager:
    """Professional consecutive loss cooldown and Fractional Kelly dynamic position sizing.
    
    Research shows:
    - Quarter-Kelly achieves 87% of full Kelly growth with max DD reduced from 64% to 12%
    - Circuit breaker after 3 consecutive losses prevents tilt spirals
    - Dynamic drawdown deleveraging prevents catastrophic failure
    """

    def __init__(self):
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.consecutive_losses_by_symbol: Dict[str, int] = {}
        self.cooldown_expiry_by_symbol: Dict[str, float] = {}
        self.cooldown_bars_by_symbol: Dict[str, int] = {}
        # Wall-clock equivalent for live trading. The bar counter is only
        # decremented by the backtester's tick_bar(); using it live bricked the
        # whole book after three consecutive losses until a process restart.
        self.cooldown_expiry_global: float = 0.0
        self.daily_pnl = 0.0
        self.cooldown_bars_remaining = 0
        self.trades_today = 0
        self.trades_today_by_symbol: Dict[str, int] = {}
        self.max_trades_per_day_per_symbol: int = 8
        self.peak_equity = 0.0
        self.current_date = None

    def record_trade_result(self, pnl: float, is_win: bool, symbol: str = "", current_date=None) -> None:
        """Record trade outcome and update streak tracking."""
        import time
        if current_date is not None and current_date != self.current_date:
            self.reset_daily(current_date)
            
        self.daily_pnl += pnl
        self.trades_today += 1
        sym_key = str(symbol).strip().upper() if symbol else ""
        if sym_key:
            try:
                from jarvis.data.symbol_registry import resolve

                sym_key = resolve(sym_key).canonical.upper()
            except Exception:
                pass
            self.trades_today_by_symbol[sym_key] = self.trades_today_by_symbol.get(sym_key, 0) + 1
            
        if is_win:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
            if sym_key:
                self.consecutive_losses_by_symbol[sym_key] = 0
                self.cooldown_expiry_by_symbol.pop(sym_key, None)
                self.cooldown_bars_by_symbol.pop(sym_key, None)
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
            if sym_key:
                cur_sym_losses = self.consecutive_losses_by_symbol.get(sym_key, 0) + 1
                self.consecutive_losses_by_symbol[sym_key] = cur_sym_losses
                if cur_sym_losses == 3:
                    self.cooldown_expiry_by_symbol[sym_key] = time.time() + 900.0  # 15 minutes cooldown
                    self.cooldown_bars_by_symbol[sym_key] = 2
                    logger.info(f"LossCooldownManager: 3 consecutive losses for {sym_key}. Pausing {sym_key} for 15 minutes / 2 bars.")
                elif cur_sym_losses >= 5:
                    self.cooldown_expiry_by_symbol[sym_key] = time.time() + 1800.0  # 30 minutes cooldown
                    self.cooldown_bars_by_symbol[sym_key] = 4
                    logger.info(f"LossCooldownManager: 5 consecutive losses for {sym_key}. Pausing {sym_key} for 30 minutes / 4 bars.")
            
        # Backtest bar tracking (only applies when tick_bar is actively called)
        if self.consecutive_losses == 3:
            self.cooldown_bars_remaining = 2
            self.cooldown_expiry_global = time.time() + 900.0
        elif self.consecutive_losses >= 5:
            self.cooldown_bars_remaining = 4
            self.cooldown_expiry_global = time.time() + 1800.0

    def should_skip_trade(self, symbol: str = "") -> Tuple[bool, str]:
        """Check if we should skip the next trade.
        Returns (should_skip, reason)."""
        import time
        from datetime import datetime, timezone
        from jarvis.config.runtime import is_offline
        sym_key = str(symbol).strip().upper() if symbol else ""
        if sym_key:
            try:
                from jarvis.data.symbol_registry import resolve

                sym_key = resolve(sym_key).canonical.upper()
            except Exception:
                pass

        live = not is_offline()
        if live:
            today = datetime.now(timezone.utc).date().isoformat()
            if self.current_date is None:
                self.current_date = today
            elif self.current_date != today:
                self.reset_daily(today)

        # 0. Global cooldown. Live uses wall-clock expiry because nothing calls
        # tick_bar() outside the backtester; the old bar counter never reached
        # zero and silently blocked every symbol until process restart.
        if live:
            if self.cooldown_expiry_global > time.time():
                remaining_sec = int(self.cooldown_expiry_global - time.time())
                return True, f"Cooldown active: {remaining_sec}s remaining."
            self.cooldown_bars_remaining = 0
        elif self.cooldown_bars_remaining > 0:
            return True, f"Cooldown active: {self.cooldown_bars_remaining} bars remaining."

        # 1. Per-symbol cooldown:
        # In offline/backtest mode, time.time() does not advance with historical bars,
        # so we MUST use bar-based cooldown (2-4 bars). In live trading, use 15-30m wall clock.
        if sym_key:
            if is_offline():
                bars_left = self.cooldown_bars_by_symbol.get(sym_key, 0)
                if bars_left > 0:
                    return True, f"Symbol {sym_key} loss cooldown active ({bars_left} bars remaining)."
            else:
                expiry = self.cooldown_expiry_by_symbol.get(sym_key, 0.0)
                if time.time() < expiry:
                    remaining_sec = int(expiry - time.time())
                    return True, f"Symbol {sym_key} loss cooldown active ({remaining_sec}s remaining)."
        
        # 2. Daily Drawdown safe guard (Max 4% daily drawdown)
        if self.peak_equity > 0:
            daily_dd = self.daily_pnl / self.peak_equity
            if daily_dd <= -0.04:
                return True, "Max daily drawdown (4%) reached."
                
        # 3. Max trades per day per symbol
        max_daily = getattr(self, "max_trades_per_day_per_symbol", 8)
        if sym_key and self.trades_today_by_symbol.get(sym_key, 0) >= max_daily:
            return True, f"Max {max_daily} trades per day reached for symbol {sym_key}."
            
        return False, ""

    def get_position_size_multiplier(self, base_risk_pct: float, account_equity: float, 
                                       win_rate: float = 0.55, payoff_ratio: float = 1.5) -> float:
        """Calculate Fractional Kelly position size multiplier.
        
        Formula:
        1. Full Kelly: f* = (b*p - q) / b where p=win_rate, q=1-p, b=payoff_ratio
        2. Fractional Kelly: f_trade = 0.25 * f*  (Quarter-Kelly)
        3. Drawdown scaling: multiply by max(0, 1 - current_dd/max_dd)
        4. Loss streak scaling: multiply by 0.5^max(0, consecutive_losses - 2)
        5. Cap at base_risk_pct
        
        Returns multiplier (0.0 to 1.0) to apply to base risk percentage.
        """
        if account_equity <= 0:
            logger.warning("Account equity is zero or negative. Multiplier is 0.0")
            return 0.0
            
        if payoff_ratio <= 0:
            logger.warning("Payoff ratio is zero or negative. Multiplier is 0.0")
            return 0.0
            
        # Track peak equity for drawdown calculations
        if account_equity > self.peak_equity:
            self.peak_equity = account_equity
            
        p = win_rate
        q = 1.0 - p
        b = payoff_ratio
        
        # 1. Full Kelly
        f_star = (b * p - q) / b
        if f_star <= 0:
            return 0.0
            
        # 2. Fractional Kelly (Quarter-Kelly)
        f_trade = 0.25 * f_star
        # Convert f_trade fraction to percentage to compare with base_risk_pct
        f_trade_pct = f_trade * 100.0
        
        # 3. Drawdown scaling
        current_drawdown = 0.0
        if self.peak_equity > 0:
            current_drawdown = max(0.0, (self.peak_equity - account_equity) / self.peak_equity)
            
        # 0.15 is 15% max drawdown threshold
        drawdown_scaler = max(0.0, 1.0 - (current_drawdown / 0.15))
        
        # 4. Smooth loss streak scaling (bounded so recovery trades are not artificially impaired)
        loss_scaler = 1.0
        if self.consecutive_losses >= 3:
            loss_scaler = max(0.75, 1.0 - (0.08 * (self.consecutive_losses - 2)))
            
        # Final risk calculation
        final_risk_pct = min(base_risk_pct, f_trade_pct * drawdown_scaler * loss_scaler)
        
        if base_risk_pct > 0:
            multiplier = final_risk_pct / base_risk_pct
        else:
            multiplier = 0.0
            
        # Cooldown rule: After 2 consecutive losses, reduce size by 50%
        if self.consecutive_losses == 2:
            multiplier = min(multiplier, 0.5)
            
        return max(0.0, min(1.0, multiplier))

    def tick_bar(self, symbol: str = "") -> None:
        """Called each bar to decrement cooldown timer."""
        if self.cooldown_bars_remaining > 0:
            self.cooldown_bars_remaining -= 1
            if self.cooldown_bars_remaining == 0:
                logger.info("LossCooldownManager: Cooldown finished. Resuming trading.")

        sym_key = str(symbol).strip().upper() if symbol else ""
        if sym_key:
            if sym_key in self.cooldown_bars_by_symbol and self.cooldown_bars_by_symbol[sym_key] > 0:
                self.cooldown_bars_by_symbol[sym_key] -= 1
        else:
            for k in list(self.cooldown_bars_by_symbol.keys()):
                if self.cooldown_bars_by_symbol[k] > 0:
                    self.cooldown_bars_by_symbol[k] -= 1

    def reset_daily(self, current_date=None) -> None:
        """Reset daily counters."""
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.trades_today_by_symbol.clear()
        self.current_date = current_date
        logger.info(f"LossCooldownManager: Daily counters reset for {current_date}")
