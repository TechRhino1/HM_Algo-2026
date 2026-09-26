"""
HM Algo 2.0 — Chronological Event-Driven Backtesting Engine.
Executes historical simulation without lookahead bias, incorporating realistic spreads, commissions, and slippage.
"""
import pandas as pd
from typing import Dict, List, Any, Optional, ClassVar

from jarvis.market.market_context import MarketContextEngine
from jarvis.intelligence.regime_engine import MarketRegimeClassifier
from jarvis.analysts.parallel_runner import ParallelAnalystCluster
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.risk.risk_engine import RiskEngine
from jarvis.data.schemas import AccountSnapshot
from jarvis.data.symbol_registry import resolve as resolve_symbol
from jarvis.backtesting.metrics import PerformanceMetricsCalculator
from jarvis.risk.loss_cooldown import LossCooldownManager
from jarvis.historical.historical_engine import HISTORICAL_DATA_ENGINE
from jarvis.intelligence.symbol_profile_config import get_symbol_profile_config
from jarvis.execution.exit_policy import ExitPolicy, evaluate_exit
from jarvis.backtesting.exit_geometry import build_exit_geometry
from jarvis.backtesting.fills import entry_fill
from jarvis.execution.entry_policy import evaluate_entry
from jarvis.market.data_feed import style_timeframes, normalise_style

class BacktestEngine:
    def __init__(
        self,
        initial_balance: float = 10000.0,
        risk_per_trade_pct: float = 0.5,
        commission_per_lot: float = 5.0,
        slippage_pips: float = 0.5
    ):
        self.initial_balance = initial_balance
        self.risk_per_trade_pct = risk_per_trade_pct
        self.commission_per_lot = commission_per_lot
        self.slippage_pips = slippage_pips

        self.context_engine = MarketContextEngine()
        self.regime_classifier = MarketRegimeClassifier()
        self.analyst_cluster = ParallelAnalystCluster(parallel=False)
        # AI8: a simulation must not be decided by weights learned from live trading.
        #
        # `DecisionEngine` owns every component on the decision path that is stateful
        # against disk — `OnlineMLPredictor` (learned weights), `MetaLabeler` and
        # `ConfidenceCalibrationEngine` (fitted models), `SelfLearningEngine` (reads
        # the live trade journal) and `RealtimeOptimizer` (shifts the gate thresholds
        # from realised P&L) — and every one of them loads EAGERLY, in its own
        # constructor. So entering offline_mode() only around run_backtest() is too
        # late: the live state is already in memory by then.
        #
        # Measured on this machine: the predictor woke up with 199 live training
        # steps and weights [0.363, 0.312, 0.162, 0.227] where the neutral prior is
        # [0.35, 0.25, 0.15, 0.20] / 10 steps; and SelfLearningEngine reported a
        # 0.9 regime multiplier and a 25-sample 0.39 win rate from today's journal
        # instead of the neutral 1.0 / 0 / 0.50. A backtest of June was therefore
        # being decided by trades that happened in September.
        #
        # The other four collaborators are deliberately NOT wrapped: they were
        # checked and read no disk at all.
        from jarvis.config.runtime import offline_mode

        with offline_mode():
            self.decision_engine = DecisionEngine()
        self.risk_engine = RiskEngine(max_risk_per_trade_pct=risk_per_trade_pct, is_backtest=True)

    def _calc_commission(self, symbol: str, lots: float, price: float = 0.0) -> float:
        cfg = get_symbol_profile_config(symbol)
        comm_per_lot = getattr(cfg, "commission_per_lot", 0.0)
        if not comm_per_lot:
            # Every shipped SymbolProfileConfig carries commission_per_lot = 0.0,
            # which silently overrode the constructor argument and made every
            # backtest commission-free. Fall back to the engine's own figure so
            # a caller passing commission_per_lot=5.0 is actually charged.
            comm_per_lot = float(self.commission_per_lot or 0.0)
        return round(lots * comm_per_lot, 4)

    # How many bars of each role the context builder gets. Matches the legacy
    # resample path (primary 300, context 100, macro 50) so the two paths feed
    # the engines comparable amounts of history.
    _ROLE_LOOKBACK: ClassVar[Dict[str, int]] = {"primary": 300, "context": 100, "macro": 50, "setup": 100, "timing": 100}

    @staticmethod
    def _prepare_mtf(
        mtf_source: Optional[Dict[str, pd.DataFrame]],
        role_tf: Dict[str, str],
    ) -> Dict[str, Any]:
        """Pre-index real per-timeframe frames for O(log n) slicing inside the loop.

        A boolean-mask slice per bar would be O(bars x rows) — quadratic, and
        fatal at M5 scale (35k bars). Each frame is instead reduced to its
        close-time array once, so the loop only ever does a binary search.

        "Close time" is ``bar_start + timeframe_duration``; a bar is only visible
        to a decision once that moment has passed. Using the bar's *start* would
        let a partially-formed H4/D1 bar contribute its future close.
        """
        if not mtf_source:
            return {}
        try:
            from jarvis.data.mt5_history import TIMEFRAME_MINUTES
        except Exception:  # pragma: no cover - MetaTrader5 optional
            TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30,
                                 "H1": 60, "H4": 240, "D1": 1440}
        prepared: Dict[str, Any] = {}
        for tf in set(role_tf.values()):
            df = mtf_source.get(tf)
            if df is None or len(df) == 0 or "time" not in df.columns:
                continue
            d = df.copy()
            starts = pd.to_datetime(d["time"])
            done = (starts + pd.Timedelta(minutes=TIMEFRAME_MINUTES.get(tf, 60)))
            d["_done_ns"] = done.values.astype("datetime64[ns]")
            d = d.sort_values("_done_ns").reset_index(drop=True)
            prepared[tf] = (d, d["_done_ns"].values)
        return prepared

    def _slice_mtf(
        self,
        prepared: Dict[str, Any],
        role_tf: Dict[str, str],
        bar_time: Any,
    ) -> Dict[str, pd.DataFrame]:
        """Role→frame dict of completed bars at ``bar_time``, from real data."""
        try:
            cutoff = pd.Timestamp(bar_time).tz_localize(None) if pd.Timestamp(bar_time).tzinfo else pd.Timestamp(bar_time)
        except Exception:
            return {}
        cutoff = cutoff.to_datetime64().astype("datetime64[ns]")
        out: Dict[str, pd.DataFrame] = {}
        for role, tf in role_tf.items():
            entry = prepared.get(tf)
            if entry is None:
                continue
            d, done_ns = entry
            # side="right" -> every bar whose close time is <= bar_time
            cut = int(done_ns.searchsorted(cutoff, side="right"))
            if cut <= 0:
                continue
            look = self._ROLE_LOOKBACK.get(role, 100)
            lo = max(0, cut - look)
            sl = d.iloc[lo:cut]
            if len(sl):
                out[role] = sl.drop(columns=["_done_ns"], errors="ignore")
        return out

    def run_backtest(self, *args, **kwargs) -> Dict[str, Any]:
        """Public entry point — the parameters are on `_run_backtest_impl`.

        AI8: the entire simulation executes inside `offline_mode()`.

        Constructing the engine hermetically (`__init__`) stops live state being
        loaded, but the stateful components also consult `is_offline()` at CALL
        time — `SelfLearningEngine.get_pattern_win_rate_and_ev` and
        `get_regime_multiplier` hit the live trade journal unless the flag is set
        while they run. Wrapping the run closes that half, and also guarantees a
        backtest cannot WRITE: today it writes nothing, but any future learning
        call inside the loop would otherwise persist to the live store.

        Only seven components honour the flag (meta_labeler, realtime_optimizer,
        self_learning, online_ml_predictor, strategy_bandit, circuit_breaker,
        drawdown). Historical data loading is NOT one of them, so a backtest
        still gets its bars.
        """
        from jarvis.config.runtime import offline_mode

        with offline_mode():
            return self._run_backtest_impl(*args, **kwargs)

    def _run_backtest_impl(
        self,
        df_h1: Optional[pd.DataFrame] = None,
        symbol: str = "XAUUSD",
        spread_pips: float = 2.0,
        slippage_delta: float = 0.05,
        start_bar_idx: int = 50,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        timeframe: str = "H1",
        wr_profile: Optional[Any] = None,
        trade_style: Optional[str] = None,
        mtf_source: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> Dict[str, Any]:
        """Run a chronological backtest.

        ``wr_profile`` (a ``jarvis.intelligence.winrate_targeting.WRTargetProfile``)
        switches entry selection and exit geometry to the calibrated per-symbol
        configuration:

          * entry selection becomes capital-protection gates + the calibrated
            score threshold + the learned regime policy, instead of the legacy
            29-check stack (see ``jarvis.execution.entry_policy``);
          * the take-profit distance becomes ``tp_r`` R-multiples of the realised
            risk distance, instead of the engine's own target;
          * the exit policy and time stop come from the calibrated geometry.

        ``wr_profile=None`` preserves the previous behaviour exactly.

        ``trade_style`` (``SWING`` / ``DAY_TRADING`` / ``SCALP``) reproduces the
        live decision path, where the style selects the role→timeframe map and is
        forwarded to the context builder and the decision engine. ``mtf_source``
        supplies *real* per-timeframe frames keyed by timeframe name
        (``{"M15": df, "M1": df, ...}``); each role is sliced from it at the
        current bar, instead of the engine resampling the primary series. When
        either is omitted the previous behaviour is unchanged: MTF context is
        built by resampling the primary series to H4/D1.
        """
        balance = self.initial_balance
        trades: List[Dict[str, Any]] = []
        open_trade: Optional[Dict[str, Any]] = None

        if df_h1 is None or (isinstance(df_h1, pd.DataFrame) and df_h1.empty):
            df_h1 = HISTORICAL_DATA_ENGINE.get_market_data(
                symbol=symbol,
                timeframe=timeframe,
                start=start_date,
                end=end_date,
                auto_download=True
            )

        total_bars = len(df_h1) if df_h1 is not None else 0
        if total_bars < 20:
            return {"symbol": symbol, "final_balance": balance, "metrics": PerformanceMetricsCalculator.calculate_metrics([], balance), "trades": [], "dataset_version": 1}

        effective_start = max(20, min(total_bars - 2, start_bar_idx))
        spec = resolve_symbol(symbol)
        actual_slippage_delta = self.slippage_pips * spec.pip_size
        cooldown_mgr = LossCooldownManager()
        skipped_min_lot = 0
        
        sym_upper = symbol.upper()
        is_crypto = spec.is_crypto or ("BTC" in sym_upper)
        rejection_stats = {}

        # Pre-compute Full Multi-Timeframe (H4 & D1) Resamplings Once Upfront (Before the Bar Loop)
        full_df_indexed = df_h1.copy()
        if "time" in full_df_indexed.columns:
            if not isinstance(full_df_indexed["time"].iloc[0], pd.Timestamp):
                full_df_indexed["time"] = pd.to_datetime(full_df_indexed["time"])
                df_h1 = df_h1.copy()
                df_h1["time"] = full_df_indexed["time"]
            if not isinstance(full_df_indexed.index, pd.DatetimeIndex):
                full_df_indexed.set_index("time", inplace=True)
            
            agg_dict = {"open": "first", "high": "max", "low": "min", "close": "last"}
            if "volume" in full_df_indexed.columns:
                agg_dict["volume"] = "sum"
            elif "tick_volume" in full_df_indexed.columns:
                agg_dict["tick_volume"] = "sum"

            # label="right" stamps each bucket with its CLOSE time, so the
            # `time <= bar_time` filter can only admit a bucket that has already
            # finished. With pandas' default label="left" the bucket is stamped
            # with its open, and an in-progress H4/D1 bar - which in a
            # full-series resample already contains its future bars - was
            # visible to the decision. _prepare_mtf/_slice_mtf do the equivalent
            # on the real-MTF path with an explicit close time.
            full_df_h4 = full_df_indexed.resample("4h", closed="left", label="right").agg(agg_dict).dropna().reset_index()
            full_df_d1 = full_df_indexed.resample("1D", closed="left", label="right").agg(agg_dict).dropna().reset_index()
            if "index" in full_df_h4.columns and "time" not in full_df_h4.columns:
                full_df_h4.rename(columns={"index": "time"}, inplace=True)
            if "index" in full_df_d1.columns and "time" not in full_df_d1.columns:
                full_df_d1.rename(columns={"index": "time"}, inplace=True)
        elif isinstance(full_df_indexed.index, pd.DatetimeIndex):
            agg_dict = {"open": "first", "high": "max", "low": "min", "close": "last"}
            if "volume" in full_df_indexed.columns:
                agg_dict["volume"] = "sum"
            elif "tick_volume" in full_df_indexed.columns:
                agg_dict["tick_volume"] = "sum"

            # label="right" stamps each bucket with its CLOSE time, so the
            # `time <= bar_time` filter can only admit a bucket that has already
            # finished. With pandas' default label="left" the bucket is stamped
            # with its open, and an in-progress H4/D1 bar - which in a
            # full-series resample already contains its future bars - was
            # visible to the decision. _prepare_mtf/_slice_mtf do the equivalent
            # on the real-MTF path with an explicit close time.
            full_df_h4 = full_df_indexed.resample("4h", closed="left", label="right").agg(agg_dict).dropna().reset_index()
            full_df_d1 = full_df_indexed.resample("1D", closed="left", label="right").agg(agg_dict).dropna().reset_index()
            if "index" in full_df_h4.columns and "time" not in full_df_h4.columns:
                full_df_h4.rename(columns={"index": "time"}, inplace=True)
            if "index" in full_df_d1.columns and "time" not in full_df_d1.columns:
                full_df_d1.rename(columns={"index": "time"}, inplace=True)
        else:
            full_df_h4 = None
            full_df_d1 = None

        # ── Style-aware real MTF context ────────────────────────────────────
        # When a trade style is supplied the engine reproduces the live path:
        # the style picks the role→timeframe map and each role is sliced from
        # real per-timeframe bars. Slices use COMPLETED bars only (a bar is
        # visible once its own close time has passed), so no partially-formed
        # higher-timeframe bar leaks future prices into the decision.
        active_style = normalise_style(trade_style) if trade_style else None
        role_tf = style_timeframes(active_style) if active_style else None
        mtf_prepared = self._prepare_mtf(mtf_source, role_tf) if role_tf else {}

        for i in range(effective_start, total_bars - 1):
            window_start = max(0, i - 300)
            history_slice = df_h1.iloc[window_start:i]
            current_bar = df_h1.iloc[i]
            next_bar = df_h1.iloc[i + 1]
            
            bar_time = current_bar.get("time") if "time" in current_bar else None
            b_date = None
            if bar_time is not None:
                b_date = bar_time.date() if hasattr(bar_time, "date") else None
                if b_date is not None and b_date != cooldown_mgr.current_date:
                    cooldown_mgr.reset_daily(b_date)
                # Advance the risk clock to BAR time. Circuit-breaker pauses
                # (45/60 min) and risk-reservation TTLs were otherwise measured
                # against real CPU seconds, so whether a pause had expired
                # depended on machine speed and backtest trade counts varied run
                # to run (USDJPY 53 vs 63, XAUUSD 97 vs 84 on identical inputs).
                try:
                    _bar_ts = float(pd.Timestamp(bar_time).timestamp())
                except Exception:
                    _bar_ts = None
                if _bar_ts is not None:
                    self.risk_engine.set_clock(lambda t=_bar_ts: t)
            cooldown_mgr.tick_bar()

            # 1. Manage existing open trade with institutional partial TP & dynamic trailing
            if open_trade:
                high = float(current_bar["high"])
                low = float(current_bar["low"])
                atr = float(current_bar.get("atr", current_bar.get("ATR", (high - low) if (high - low) > 0 else 1.0)))

                # Increment bar holding counter
                open_trade["bars_held"] = open_trade.get("bars_held", 0) + 1

                # Track MFE / MAE
                if open_trade["type"] == "BUY":
                    favorable = high - open_trade["entry"]
                    adverse = open_trade["entry"] - low
                else:
                    favorable = open_trade["entry"] - low
                    adverse = high - open_trade["entry"]

                open_trade["mfe"] = max(open_trade.get("mfe", 0.0), favorable)
                open_trade["mae"] = max(open_trade.get("mae", 0.0), adverse)

                risk_dist = open_trade.get("risk_dist", abs(open_trade["entry"] - open_trade["sl"]))
                if risk_dist <= 0:
                    risk_dist = max(0.001, abs(open_trade["entry"] - open_trade["sl"]))

                # Master-Trader Stagnation Time Stop: Dynamic regime-aware.
                # When a calibrated profile is in use the time stop is part of
                # the calibrated geometry (it bounds how many trades fit in the
                # sample), so the profile's value takes precedence.
                if open_trade.get("max_bars"):
                    stag_limit = int(open_trade["max_bars"])
                else:
                    regime_str = str(open_trade.get("regime", "")).upper()
                    base_stag = 24 if is_crypto else 16
                    if any(r in regime_str for r in ["RANGE", "CONSOLIDATION", "COMPRESSION"]):
                        stag_limit = max(8, base_stag // 2)
                    elif any(r in regime_str for r in ["TREND", "BREAKOUT"]):
                        stag_limit = int(base_stag * 1.25)
                    else:
                        stag_limit = base_stag

                if open_trade["bars_held"] >= stag_limit and open_trade["mfe"] < (risk_dist * 0.25):
                    # A stagnation exit is a market order, so it takes adverse
                    # slippage. Only stop exits used to be charged, which made
                    # the time stop look free.
                    exit_price = float(current_bar["close"])
                    exit_price -= actual_slippage_delta if open_trade["type"] == "BUY" else -actual_slippage_delta
                    pips = ((exit_price - open_trade["entry"]) if open_trade["type"] == "BUY" else (open_trade["entry"] - exit_price)) / spec.pip_size
                    pnl_raw = pips * spec.pip_value_per_lot * open_trade["lots"]
                    comm = self._calc_commission(symbol, open_trade["lots"], exit_price)
                    pnl_remaining = pnl_raw - comm
                    pnl_net = pnl_remaining + open_trade.get("realized_pnl", 0.0)
                    balance += pnl_remaining
                    is_win = pnl_net > 0
                    cooldown_mgr.record_trade_result(pnl=pnl_net, is_win=is_win, symbol=symbol, current_date=b_date)
                    trades.append({
                        "symbol": symbol, "type": open_trade["type"],
                        "open_time": open_trade.get("open_time"),
                        "exit_time": bar_time,
                        "bars_held": open_trade.get("bars_held", stag_limit),
                        "entry": open_trade["entry"], "exit": exit_price,
                        "sl": open_trade["sl"], "tp": open_trade["tp"],
                        # Immutable initial stop and risk distance. The
                        # "sl" above is the TRAILED stop, so computing an
                        # R-multiple from it divides by a shrinking
                        # denominator and reports absurd R values.
                        "initial_sl": open_trade.get("initial_sl", open_trade["sl"]),
                        "risk_dist": open_trade.get("risk_dist", 0.0),
                        "lots": open_trade.get("initial_lots", open_trade["lots"]),
                        "pnl": round(pnl_net, 2), "result": f"STAGNATION_TIME_STOP_{stag_limit}BAR",
                        "strategy": open_trade["strategy"], "regime": open_trade["regime"],
                        "score": open_trade["score"],
                        "planned_rr": open_trade.get("planned_rr", 0.0),
                        "master_score": open_trade.get("master_score", 0.0),
                        "mfe": round(open_trade["mfe"], 4), "mae": round(open_trade["mae"], 4),
                        "is_win": is_win
                    })
                    open_trade = None
                    continue

                # ── Exit management (canonical policy) ──────────────────────
                # All stop/partial/trail arithmetic now lives in
                # jarvis.execution.exit_policy so the backtest and the live
                # position monitor can never drift apart again.
                #
                # Previously this block hardcoded `be_trigger_r = 1.00` for gold,
                # locked breakeven after only +1R, and then used a 2.2-2.6x ATR
                # trail that sat *behind* the initial stop and could never ratchet.
                # Net effect: trades that ran +20R were closed near +0.9R.
                if "exit_policy" not in open_trade:
                    open_trade["exit_policy"] = ExitPolicy.for_symbol(symbol, spec)

                price_for_exit = float(current_bar["close"])
                # Favourable excursion measured on the excursion high/low, but the
                # stop arithmetic must reference a *tradable* price. Using the bar
                # close avoids the lookahead of assuming we exit at the extreme.
                exit_dec = evaluate_exit(
                    side=open_trade["type"],
                    entry=float(open_trade["entry"]),
                    initial_sl=float(open_trade["initial_sl"]),
                    current_sl=float(open_trade["sl"]),
                    # Trail-only geometries carry tp=None; evaluate_exit needs a
                # real number, and 0.0 is safe because it is unused there.
                tp=float(open_trade["tp"]) if open_trade["tp"] is not None else 0.0,
                    price=price_for_exit,
                    favorable_dist=float(open_trade["mfe"]),
                    atr=float(atr or 0.0),
                    policy=open_trade["exit_policy"],
                    partial_already_taken=bool(open_trade.get("partial_closed", False) or open_trade.get("partial_unsupported", False)),
                    be_already_locked=bool(open_trade.get("be_locked", False)),
                    # No structural reference is passed here: this loop only has bar
                    # OHLC, and inventing a swing level from the same bar would
                    # introduce lookahead. The ATR trail and R-milestones already
                    # provide progressive locking.
                    struct_level=None,
                )

                # Apply the partial scale-out (only if the lots can actually split)
                if exit_dec.partial_close_pct > 0.0 and not (open_trade.get("partial_closed", False) or open_trade.get("partial_unsupported", False)):
                    partial_ratio = exit_dec.partial_close_pct
                    partial_lots = round(open_trade["lots"] * partial_ratio, 2)
                    if partial_lots >= 0.01 and (open_trade["lots"] - partial_lots) >= 0.01:
                        partial_exit_p = exit_dec.partial_price
                        pips_p = ((partial_exit_p - open_trade["entry"]) if open_trade["type"] == "BUY"
                                  else (open_trade["entry"] - partial_exit_p)) / spec.pip_size
                        comm_p = self._calc_commission(symbol, partial_lots, partial_exit_p)
                        pnl_p = (pips_p * spec.pip_value_per_lot * partial_lots) - comm_p
                        balance += pnl_p
                        open_trade["realized_pnl"] = open_trade.get("realized_pnl", 0.0) + pnl_p
                        open_trade["lots"] = round(open_trade["lots"] - partial_lots, 2)
                        open_trade["partial_closed"] = True
                        open_trade["partial_close_bar"] = open_trade.get("bars_held", 0)
                    else:
                        # Cannot split lots (micro size): do not fake a partial close.
                        # Mark partial_unsupported so evaluate_exit doesn't re-trigger partials,
                        # and allow the trade to breathe to its full be_trigger_r or TP.
                        open_trade["partial_unsupported"] = True
                        open_trade["partial_close_bar"] = open_trade.get("bars_held", 0)

                # Ratchet the stop (one-way only; evaluate_exit enforces this)
                if exit_dec.new_sl and exit_dec.new_sl != open_trade["sl"]:
                    if open_trade["type"] == "BUY":
                        open_trade["sl"] = max(open_trade["sl"], exit_dec.new_sl)
                    else:
                        open_trade["sl"] = min(open_trade["sl"], exit_dec.new_sl)

                if exit_dec.be_locked:
                    open_trade["be_locked"] = True

                # NOTE: the previous "Stage 2" block (ATR trail + 1.25R/2R/3R
                # milestone locks) was removed. It duplicated the policy logic with
                # different constants and, critically, its trail width (2.2-2.6 ATR)
                # was wider than the initial stop so it could never ratchet.
                # evaluate_exit() above now owns trailing and milestones.

                # 5.1 scale-out rungs: close every ladder rung whose R level is
                # reached, with the harness's conservative rule - if the bar
                # also spans the stop, the stop fills first and no rung fills.
                closed = False
                exit_price = 0.0
                result = ""

                _legs = open_trade.get("legs") or []
                if _legs and float(open_trade.get("risk_dist") or 0.0) > 0:
                    _rd = float(open_trade["risk_dist"])
                    _dir = 1.0 if open_trade["type"] == "BUY" else -1.0
                    _r_now = float(open_trade["mfe"]) / _rd
                    _stop_hit = ((low <= open_trade["sl"]) if open_trade["type"] == "BUY"
                                 else (high >= open_trade["sl"]))
                    if not _stop_hit:
                        for _lg in _legs:
                            if _lg["done"] or _lg["r"] is None:
                                continue
                            if _r_now < float(_lg["r"]):
                                continue
                            _planned_lots = round(open_trade.get("initial_lots", open_trade["lots"]) * float(_lg["pct"]), 2)
                            _lots = min(open_trade["lots"], _planned_lots)
                            if _lots < 0.01:
                                _lg["done"] = True
                                continue
                            _px = open_trade["entry"] + _dir * float(_lg["r"]) * _rd
                            _pips = (((_px - open_trade["entry"]) if open_trade["type"] == "BUY"
                                      else (open_trade["entry"] - _px)) / spec.pip_size)
                            _comm = self._calc_commission(symbol, _lots, _px)
                            _pnl = (_pips * spec.pip_value_per_lot * _lots) - _comm
                            balance += _pnl
                            open_trade["realized_pnl"] = open_trade.get("realized_pnl", 0.0) + _pnl
                            open_trade["lots"] = round(open_trade["lots"] - _lots, 2)
                            _lg["done"] = True
                            open_trade["partial_closed"] = True
                            if open_trade["lots"] <= 0.001:
                                closed = True
                                exit_price = _px
                                result = "LADDER_TP"
                                break

                # Stage 4: Check SL/TP exit for remaining position
                if not closed:
                    if open_trade["type"] == "BUY":
                        sl_hit = low <= open_trade["sl"]
                        tp_hit = (open_trade["tp"] is not None) and (high >= open_trade["tp"])
                        if sl_hit and tp_hit:
                            # Conservative intrabar ordering (matches trade_simulator):
                            # the stop resting at the start of the bar is tested first,
                            # so a bar that spans both is booked as a stop-out loss.
                            # Resolving it as a TP on the open-price heuristic inflated
                            # win rate exactly where the target lives.
                            exit_price = open_trade["sl"] - actual_slippage_delta
                            result = "BE/TRAIL_SL" if (open_trade.get("partial_closed") or open_trade.get("be_locked")) else "SL"
                            closed = True
                        elif sl_hit:
                            exit_price = open_trade["sl"] - actual_slippage_delta
                            result = "BE/TRAIL_SL" if (open_trade.get("partial_closed") or open_trade.get("be_locked")) else "SL"
                            closed = True
                        elif tp_hit:
                            exit_price = open_trade["tp"]
                            result = "TP"
                            closed = True
                    elif open_trade["type"] == "SELL":
                        sl_hit = high >= open_trade["sl"]
                        tp_hit = (open_trade["tp"] is not None) and (low <= open_trade["tp"])
                        if sl_hit and tp_hit:
                            # Conservative intrabar ordering (matches trade_simulator).
                            exit_price = open_trade["sl"] + actual_slippage_delta
                            result = "BE/TRAIL_SL" if (open_trade.get("partial_closed") or open_trade.get("be_locked")) else "SL"
                            closed = True
                        elif sl_hit:
                            exit_price = open_trade["sl"] + actual_slippage_delta
                            result = "BE/TRAIL_SL" if (open_trade.get("partial_closed") or open_trade.get("be_locked")) else "SL"
                            closed = True
                        elif tp_hit:
                            exit_price = open_trade["tp"]
                            result = "TP"
                            closed = True

                if closed:
                    pips = ((exit_price - open_trade["entry"]) if open_trade["type"] == "BUY" else (open_trade["entry"] - exit_price)) / spec.pip_size
                    pnl_raw = pips * spec.pip_value_per_lot * open_trade["lots"]
                    comm = self._calc_commission(symbol, open_trade["lots"], exit_price)
                    pnl_remaining = pnl_raw - comm
                    pnl_net = pnl_remaining + open_trade.get("realized_pnl", 0.0)
                    balance += pnl_remaining

                    is_win = pnl_net > 0
                    cooldown_mgr.record_trade_result(pnl=pnl_net, is_win=is_win, symbol=symbol, current_date=b_date)

                    trades.append({
                        "symbol": symbol,
                        "type": open_trade["type"],
                        "open_time": open_trade.get("open_time"),
                        "exit_time": bar_time,
                        "bars_held": open_trade.get("bars_held", 1),
                        "entry": open_trade["entry"],
                        "exit": exit_price,
                        "sl": open_trade["sl"],
                        "initial_sl": open_trade.get("initial_sl", open_trade["sl"]),
                        "risk_dist": open_trade.get("risk_dist", 0.0),
                        "tp": open_trade["tp"],
                        "lots": open_trade.get("initial_lots", open_trade["lots"]),
                        "pnl": round(pnl_net, 2),
                        "result": result,
                        "strategy": open_trade["strategy"],
                        "regime": open_trade["regime"],
                        "score": open_trade["score"],
                        "planned_rr": open_trade.get("planned_rr", 0.0),
                        "master_score": open_trade.get("master_score", 0.0),
                        "mfe": round(open_trade["mfe"], 4),
                        "mae": round(open_trade["mae"], 4),
                        "is_win": is_win
                    })
                    open_trade = None

            # 2. Check new trade entry if flat
            if open_trade is None:
                skip_trade, skip_reason = cooldown_mgr.should_skip_trade(symbol)
                if skip_trade:
                    rejection_stats[skip_reason] = rejection_stats.get(skip_reason, 0) + 1
                    continue

                if mtf_prepared and bar_time is not None:
                    mtf_dict = self._slice_mtf(mtf_prepared, role_tf, bar_time)
                    if mtf_dict.get("primary") is None or mtf_dict["primary"].empty:
                        mtf_dict["primary"] = history_slice
                elif full_df_h4 is not None and bar_time is not None:
                    h4_slice = full_df_h4[full_df_h4["time"] <= bar_time].iloc[-100:]
                    d1_slice = full_df_d1[full_df_d1["time"] <= bar_time].iloc[-50:]
                    mtf_dict = {"primary": history_slice, "context": h4_slice, "macro": d1_slice}
                else:
                    mtf_dict = {"primary": history_slice}

                context = self.context_engine.build_context(
                    symbol, mtf_dict,
                    current_spread_pips=spread_pips,
                    max_allowed_spread_pips=spec.max_spread_pips,
                    trade_style=active_style or "SWING",
                )
                regime = self.regime_classifier.classify_regime(context)

                # Parallel analysts with dynamic directional hypothesis
                tentative_bias = "BUY" if context.structure.bias == "BULLISH" else ("SELL" if context.structure.bias == "BEARISH" else ("SELL" if getattr(context.momentum, "trend_score", 0.0) < 0 else "BUY"))
                analyst_reports, devil_report = self.analyst_cluster.run_all_parallel(context, regime, tentative_bias)
                
                # Fractional Kelly dynamic position sizing from trade history
                planned_risk_pct = self.risk_per_trade_pct
                if len(trades) >= 5:
                    recent_trades = trades[-20:]
                    wins = [t for t in recent_trades if t.get("is_win", False)]
                    wr = len(wins) / len(recent_trades) if recent_trades else 0.50
                    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
                    losses = [t for t in recent_trades if not t.get("is_win", False)]
                    avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 1.0
                    payoff = (avg_win / avg_loss) if avg_loss > 0 else 1.5
                    size_mult = cooldown_mgr.get_position_size_multiplier(
                        planned_risk_pct, balance,
                        win_rate=max(0.35, min(0.75, wr)),
                        payoff_ratio=max(1.0, min(3.0, payoff))
                    )
                else:
                    size_mult = cooldown_mgr.get_position_size_multiplier(planned_risk_pct, balance, win_rate=0.50, payoff_ratio=1.5)
                effective_risk_pct = max(0.20, planned_risk_pct * size_mult)

                decision = self.decision_engine.evaluate(
                    context, regime, analyst_reports, devil_report, account_balance=balance, risk_per_trade_pct=effective_risk_pct, mtf_data=mtf_dict, trade_style=active_style or "SWING"
                )

                regime_name = (
                    regime.primary_regime.value
                    if hasattr(regime.primary_regime, "value")
                    else str(regime.primary_regime)
                )

                # ── Entry selection ─────────────────────────────────────────
                # With a calibrated profile, selection is the capital-protection
                # gates plus the out-of-sample-calibrated score threshold and the
                # learned regime policy. Without one, the legacy 29-check stack
                # decides, exactly as before.
                if wr_profile is not None:
                    entry_dec = evaluate_entry(
                        quality_gate=decision.quality_gate,
                        score=float(getattr(decision, "model_confidence", 0.0) or 0.0),
                        regime=regime_name,
                        profile=wr_profile,
                    )
                    entry_ok = bool(entry_dec.allowed and decision.bias in ("BUY", "SELL"))
                    if not entry_ok:
                        # The entry policy does not see the bias, so a bar whose
                        # gates and score all pass but which carries no direction
                        # would otherwise be recorded under the policy's SUCCESS
                        # reason ("calibrated edge filter passed") — a label that
                        # says the opposite of what happened.
                        if entry_dec.allowed and decision.bias not in ("BUY", "SELL"):
                            reason = f"no directional bias ({decision.bias or 'HOLD'})"
                        else:
                            reason = entry_dec.reason
                        rejection_stats[reason] = rejection_stats.get(reason, 0) + 1
                else:
                    entry_dec = None
                    entry_ok = decision.decision == "EXECUTE" and decision.bias in ("BUY", "SELL")

                if entry_ok:
                    account_snap = AccountSnapshot(
                        login=1, server="Backtest", balance=balance, equity=balance, margin=0, free_margin=balance, margin_level=0, leverage=100
                    )
                    spec = resolve_symbol(symbol)
                    sym_info = {
                        "name": symbol,
                        "trade_contract_size": spec.contract_size,
                        "volume_min": 0.01,
                        "volume_max": 100.0,
                        "volume_step": 0.01
                    }
                    auth_res = self.risk_engine.authorize_execution(
                        decision, account_snap, [], sym_info, spread_pips,
                        # In calibrated mode entry selection is owned by
                        # entry_policy, which deliberately trades setups the
                        # legacy gate stack rejected. Without this the risk
                        # guard would reimpose the legacy veto and block every
                        # calibrated trade.
                        entry_authorized_override=(True if wr_profile is not None else None),
                    )

                    if auth_res["authorized"]:
                        # One spread, charged in the correct direction: a long
                        # pays the ask, a short is filled at the bid. This used
                        # to be inline here and duplicated (differently) in
                        # `tools/scan_signals.py`, where the short leg was free.
                        # Both now share `entry_fill`.
                        entry_price = entry_fill(
                            float(next_bar["open"]), decision.bias, spread_pips, spec.pip_size
                        )
                        price_shift = entry_price - decision.entry_price
                        sl_price = decision.stop_loss + price_shift
                        
                        actual_risk_dist = abs(entry_price - sl_price)
                        if actual_risk_dist <= 0:
                            actual_risk_dist = max(spec.pip_size * 10, decision.sl_distance)

                        # Calibrated target: a multiple of the REALISED risk
                        # distance (not the engine's planned target), so the
                        # geometry means the same thing on every symbol.
                        exit_geom = None
                        if wr_profile is not None:
                            # Execute the VALIDATED base geometry. The
                            # calibrator's out-of-sample expectancy is measured
                            # on this geometry; per-regime overrides in
                            # ``geometry_for`` were never validated as an
                            # ensemble and silently destroyed expectancy (e.g.
                            # TREND_BULL tp_r=1.5 vs base tp_r=0.25). Using the
                            # validated geometry is what makes the engine's
                            # realized expectancy match the calibrated OOS.
                            geom = wr_profile.geometry
                            direction_sign = 1.0 if decision.bias == "BUY" else -1.0
                            geom_tp = float(geom.tp_r) if (geom and geom.tp_r is not None) else 1.0
                            # ``trail_atr = None`` means "no runner trail". That is
                            # the same convention the calibration simulator uses:
                            # ``Geometry.to_policy`` pushes ``trail_activation_r`` to
                            # 1e9 when ``trail_atr`` is None, which disables the trail
                            # entirely. The previous ``or 1.5`` coerced None into a
                            # live 1.5xATR trail, so the engine traded a DIFFERENT
                            # exit schedule from the one the calibrator measured its
                            # out-of-sample expectancy on — precisely what the
                            # comment above is trying to prevent.
                            #
                            # The divergence is invisible below tp_r = 2.0, because
                            # the trail only engages at ``trail_activation_r`` (2.0)
                            # and a trade targeting 1.5R exits before it gets there.
                            # At and above 2.0 it is severe: on NAS100 the calibrator
                            # predicted 44.2% WR / +0.125R while the engine realised
                            # 23.3% WR / -0.246R. Preserve None so the two agree.
                            geom_trail = getattr(geom, "trail_atr", None)
                            geom_be = getattr(geom, "be_trigger_r", None)
                            geom_trail_act = getattr(geom, "trail_activation_r", 2.0) or 2.0
                            geom_max_bars = int(geom.max_bars) if geom else 48
                            exit_geom = build_exit_geometry(
                                getattr(wr_profile, "geometry_mode", "A_fixed_tp"),
                                tp_r=geom_tp,
                                trail_atr=None if geom_trail is None else float(geom_trail),
                                be_trigger_r=geom_be,
                                trail_activation_r=float(geom_trail_act),
                                max_bars=geom_max_bars,
                            )
                            if exit_geom.tp_r is None:
                                tp_price = None      # trail-only / ladder runner
                            else:
                                tp_price = entry_price + direction_sign * float(exit_geom.tp_r) * actual_risk_dist
                            exit_policy_for_trade = exit_geom.to_policy(symbol, spec)
                            trade_max_bars = geom_max_bars
                        else:
                            tp_price = decision.take_profit + price_shift
                            exit_policy_for_trade = ExitPolicy.for_symbol(symbol, spec)
                            trade_max_bars = None

                        # Enforce hard dollar risk cap based on filled entry & SL
                        planned_risk_dollars = balance * (effective_risk_pct / 100.0)
                        from jarvis.data.symbol_registry import get_dollar_risk_per_price_unit
                        unit_risk = get_dollar_risk_per_price_unit(symbol, sym_info)
                        dollar_risk_per_lot = actual_risk_dist * unit_risk
                        
                        if dollar_risk_per_lot > 0:
                            raw_lots = planned_risk_dollars / dollar_risk_per_lot
                            lots = min(auth_res["lots"], round(raw_lots, 2))
                            if lots < sym_info["volume_min"]:
                                # The planned risk cannot be expressed at the
                                # minimum lot. The floor used to be applied
                                # AFTER the risk cap, so the trade silently
                                # opened at up to 2x the intended risk (and up
                                # to 10x once MT5 re-quantizes to the broker's
                                # real minimum) with no check at all.
                                skipped_min_lot += 1
                                continue
                        else:
                            lots = auth_res["lots"]

                        open_time_val = next_bar.get("time") if "time" in next_bar else bar_time

                        open_trade = {
                            "type": decision.bias,
                            "open_time": open_time_val,
                            "bars_held": 0,
                            "entry": entry_price,
                            "sl": sl_price,
                            # Immutable copy of the original stop. This defines 1R for
                            # the entire life of the trade and must never be mutated,
                            # otherwise R-multiples silently drift as the stop trails.
                            "initial_sl": sl_price,
                            "tp": tp_price,
                            "lots": lots,
                            "initial_lots": lots,
                            "risk_dist": actual_risk_dist,
                            "exit_geom": exit_geom,
                            # Rungs ONLY when the schedule has a runner: mode A
                            # is a single 100% target, and giving it a rung
                            # would close it here AND again in the TP check.
                            "legs": ([
                                {"pct": float(lg.pct), "r": lg.r, "done": False}
                                for lg in exit_geom.legs if lg.r is not None
                            ] if (exit_geom is not None
                                  and any(l.r is None for l in exit_geom.legs)) else []),
                            "realized_pnl": 0.0,
                            "partial_closed": False,
                            "partial_close_bar": -1,
                            "strategy": decision.strategy,
                            "regime": regime_name,
                            "score": decision.model_confidence,
                            "planned_rr": decision.risk_reward_ratio,
                            "master_score": getattr(decision, "master_confluence_score", 0.0),
                            "mfe": 0.0,
                            "mae": 0.0,
                            "first_target_price": getattr(decision, "first_target_price", None),
                            "first_target_volume_pct": getattr(decision, "first_target_volume_pct", 0.50),
                            # Resolved once here and consumed by evaluate_exit(). Previously
                            # this key was written but never read by any exit code path.
                            # With a calibrated profile this is the calibrated geometry
                            # rather than the symbol default.
                            "exit_policy": exit_policy_for_trade,
                            # None => legacy regime-aware stagnation stop.
                            "max_bars": trade_max_bars,
                        }
                    else:
                        # The risk engine reports a LIST of reasons under
                        # "reasons". Reading "reason" (singular) discarded them
                        # all and collapsed every veto into one opaque label.
                        auth_reasons = auth_res.get("reasons") or []
                        if not auth_reasons:
                            auth_reasons = [auth_res.get("reason", "Risk Engine Auth Failed")]
                        for r in auth_reasons:
                            rejection_stats[r] = rejection_stats.get(r, 0) + 1
                elif wr_profile is not None:
                    # Calibrated mode: the reason was already recorded above.
                    pass
                else:
                    for r in getattr(decision, "rejection_reasons", []):
                        rejection_stats[r] = rejection_stats.get(r, 0) + 1
                    if not getattr(decision, "rejection_reasons", []):
                        for r in getattr(decision, "waiting_reasons", []):
                            rejection_stats[r] = rejection_stats.get(r, 0) + 1

        # Mark-to-market close of any remaining open position on final bar
        if open_trade is not None:
            final_bar = df_h1.iloc[-1]
            exit_price = float(final_bar["close"])
            exit_price -= actual_slippage_delta if open_trade["type"] == "BUY" else -actual_slippage_delta
            spec = resolve_symbol(symbol)
            pips = ((exit_price - open_trade["entry"]) if open_trade["type"] == "BUY" else (open_trade["entry"] - exit_price)) / spec.pip_size
            pnl_raw = pips * spec.pip_value_per_lot * open_trade["lots"]
            comm = self._calc_commission(symbol, open_trade["lots"], exit_price)
            pnl_net = pnl_raw - comm + open_trade.get("realized_pnl", 0.0)
            balance += (pnl_raw - comm)

            trades.append({
                "symbol": symbol,
                "type": open_trade["type"],
                "open_time": open_trade.get("open_time"),
                "exit_time": final_bar.get("time") if "time" in final_bar else None,
                "bars_held": open_trade.get("bars_held", 1),
                "entry": open_trade["entry"],
                "exit": exit_price,
                "sl": open_trade["sl"],
                "initial_sl": open_trade.get("initial_sl", open_trade["sl"]),
                "risk_dist": open_trade.get("risk_dist", 0.0),
                "tp": open_trade["tp"],
                "lots": open_trade.get("initial_lots", open_trade["lots"]),
                "pnl": round(pnl_net, 2),
                "result": "CLOSE_AT_END",
                "strategy": open_trade["strategy"],
                "regime": open_trade["regime"],
                "score": open_trade["score"],
                "planned_rr": open_trade.get("planned_rr", 0.0),
                "master_score": open_trade.get("master_score", 0.0),
                "mfe": round(open_trade["mfe"], 4),
                "mae": round(open_trade["mae"], 4),
                "is_win": pnl_net > 0
            })
            open_trade = None

        metrics = PerformanceMetricsCalculator.calculate_metrics(trades, self.initial_balance)
        ver = HISTORICAL_DATA_ENGINE.get_dataset_version(symbol, timeframe=timeframe) or 1
        return {
            "symbol": symbol,
            "metrics": metrics,
            "trades": trades,
            "final_balance": round(balance, 2),
            "rejection_stats": rejection_stats,
            "dataset_version": ver
        }
