"""
HM Algo 2.0 — MT5 Client & Execution Gateway.
Provides a thread-safe, timeout-guarded connection to MetaTrader 5 with automatic symbol resolution and retry mechanisms.
"""
import os
import time
import logging
import math
from typing import Dict, List, Optional, Any, ClassVar

from jarvis.common.timeout_guard import TimeoutGuard
from jarvis.data.broker_time import broker_utc_offset
from jarvis.data.schemas import AccountSnapshot, PositionSnapshot
from jarvis.execution.broker_lock import DEFAULT_WAIT_SEC, TrackedRLock

logger = logging.getLogger("JARVIS_MT5Client")

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None
    MT5_AVAILABLE = False

class MT5Client:
    _shared_paper_positions: ClassVar[Dict[int, PositionSnapshot]] = {}
    _shared_paper_pending_orders: ClassVar[Dict[int, Dict[str, Any]]] = {}
    # P3: NOT a plain RLock. This lock is held ACROSS native broker calls, and a
    # native call that never returns cannot be interrupted — Python cannot kill a
    # thread blocked in C. A plain RLock therefore turned one hung call into a
    # permanent, silent wedge: every later call blocked on acquire until its own
    # timeout fired and it quietly returned its default. `TrackedRLock` keeps the
    # serialisation (the MT5 bindings are not thread-safe, so removing it would be
    # a correctness bug) but bounds how long a waiter waits and records who holds
    # it, so the wedge is reported instead of absorbed.
    _shared_lock = TrackedRLock("mt5_shared", default_wait_sec=DEFAULT_WAIT_SEC)

    def __init__(self, magic_number: int = 888999, mode: str = "live", timeout_sec: float = 4.0,
                 auto_init: bool = True):
        """`auto_init=False` defers `mt5.initialize()` to first use.

        Connecting in a constructor is only safe if the constructor itself is
        safe to run at an arbitrary moment. It is not: `server.py` builds one in
        the CLASS BODY, so importing the module initialised MT5. With no terminal
        running `mt5.initialize()` blocks inside a native call while HOLDING THE
        GIL, which means `Thread.start()` can never complete — so the import
        never returns and the process is dead (measured: pytest could not even
        collect, 13min+ with no output). `TimeoutGuard` cannot rescue this: a
        timeout cannot interrupt a thread, and no new thread can be created
        while the GIL is held.

        Nothing needs the eager call: every operation already goes through
        `_reconnect_if_needed()`, which connects on first use. Deferring it is
        therefore behaviour-preserving for anything that actually talks to the
        broker, and simply stops module import from being able to hang.
        """
        self.magic_number = magic_number
        self.mode = mode.lower()  # "live", "paper", "demo"
        self.timeout_sec = timeout_sec
        self.is_connected = False
        #: De-duplicates the "cannot reach the broker" log line. See `_init()`.
        self._last_init_error = None
        self.symbol_alias_cache: Dict[str, str] = {}
        self._paper_positions = MT5Client._shared_paper_positions
        self._paper_pending_orders = MT5Client._shared_paper_pending_orders
        self._lock = MT5Client._shared_lock
        if auto_init:
            self.init_connection()


    def init_connection(self) -> bool:
        if self.mode in ("paper", "backtest", "offline") or os.environ.get("JARVIS_BACKTEST_MODE") == "1":
            self.is_connected = True
            logger.info("MT5Client running in simulated/offline execution mode.")
            return True

        if not MT5_AVAILABLE or mt5 is None:
            if self.mode in ("live", "demo"):
                logger.error(f"FAIL-CLOSED: MetaTrader5 package unavailable in {self.mode.upper()} mode. Refusing paper fallback.")
                self.is_connected = False
                return False
            logger.warning("MetaTrader5 python package not available. Falling back to PAPER mode.")
            self.mode = "paper"
            self.is_connected = True
            return True

        import random
        delays = [1.0, 2.0, 4.0, 8.0, 16.0]
        
        for attempt in range(len(delays) + 1):
            def _init():
                with self._lock:
                    from jarvis.data.broker_symbols import ensure_mt5_terminal

                    if not ensure_mt5_terminal():
                        err = mt5.last_error()
                        if err != self._last_init_error:
                            self._last_init_error = err
                            logger.warning(
                                "MT5 not available (%s). Market data and execution "
                                "report as unavailable until a terminal is running.", err,
                            )
                        return False
                    self.is_connected = True
                    self._last_init_error = None
                    acc = mt5.account_info()
                    if acc:
                        trade_mode = getattr(acc, "trade_mode", 0)
                        mode_str = "DEMO" if trade_mode == 0 else ("REAL" if trade_mode == 2 else "CONTEST")
                        logger.info(f"Connected to MT5 Server: {acc.server} | Login: #{acc.login} | TradeMode: {mode_str} | Equity: ${acc.equity:.2f}")
                        if self.mode == "demo" and trade_mode == 2:
                            logger.critical(
                                f"SAFETY WARNING: Execution mode is DEMO, but connected to REAL MT5 account #{acc.login}! "
                                "Live order dispatch will be blocked to protect real capital."
                            )
                        elif self.mode == "live" and trade_mode == 0:
                            logger.critical(
                                f"SAFETY WARNING: Execution mode is LIVE, but connected to DEMO MT5 account #{acc.login}! "
                                "Live order dispatch will be blocked due to account mismatch."
                            )
                    return True

            res = TimeoutGuard.run_sync(_init, timeout_sec=self.timeout_sec, default=False, task_name="MT5_Init")
            self.is_connected = bool(res)
            
            if self.is_connected:
                return True
                
            if attempt < len(delays):
                delay = delays[attempt]
                jitter = delay * 0.2 * (random.random() * 2 - 1)
                time.sleep(delay + jitter)
                
        return False

    def _reconnect_if_needed(self):
        if not self.is_connected:
            self.init_connection()

    def resolve_symbol_name(self, symbol: str) -> str:
        if symbol in self.symbol_alias_cache:
            return self.symbol_alias_cache[symbol]

        if self.mode == "paper" or not MT5_AVAILABLE or not self.is_connected:
            return symbol

        def _resolve():
            all_syms = mt5.symbols_get()
            if not all_syms:
                return symbol

            base_u = symbol.upper()
            candidates = []
            for s in all_syms:
                s_name_u = s.name.upper()
                if base_u in ["XAUUSD", "GOLD"]:
                    if any(k in s_name_u for k in ["GOLD.I#", "GOLD#", "GOLD.M", "XAUUSD.I#", "XAUUSD#", "GOLD", "XAUUSD"]):
                        candidates.append(s)
                elif base_u in ["BTCUSD", "BTC"]:
                    if any(k in s_name_u for k in ["BTCUSD#", "BTCUSD", "BITCOIN"]):
                        candidates.append(s)
                elif base_u in ["ETHUSD", "ETH"]:
                    if any(k in s_name_u for k in ["ETHUSD#", "ETHUSD", "ETHEREUM"]):
                        candidates.append(s)
                elif base_u in ["SOLUSD", "SOL"]:
                    if any(k in s_name_u for k in ["SOLUSD#", "SOLUSD", "SOLANA"]):
                        candidates.append(s)
                elif base_u in ["NAS100", "US100", "USTEC", "NDX"]:
                    if any(k in s_name_u for k in ["US100CASH#", "US100#", "NAS100#", "USTECH#", "US100"]):
                        candidates.append(s)
                elif base_u in ["US500", "SPX500", "SP500"]:
                    if any(k in s_name_u for k in ["US500CASH#", "US500#", "SPX500#"]):
                        candidates.append(s)
                elif base_u in ["US30", "DJ30", "DOW"]:
                    if any(k in s_name_u for k in ["US30CASH#", "US30#", "DJ30#"]):
                        candidates.append(s)
                elif base_u in ["WTI", "USOIL", "OIL", "CRUDE"]:
                    if any(k in s_name_u for k in ["OILCASH#", "BRENTCASH#", "USOIL#", "OIL#", "WTI#"]):
                        candidates.append(s)
                elif base_u in s_name_u:
                    candidates.append(s)

            if candidates:
                candidates.sort(key=lambda s: (
                    0 if getattr(s, "trade_mode", 0) == getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4) else 1,
                    0 if "#" in s.name else 1,
                    len(s.name)
                ))
                best = candidates[0].name
                mt5.symbol_select(best, True)
                self.symbol_alias_cache[symbol] = best
                return best
            return symbol

        res = TimeoutGuard.run_sync(_resolve, timeout_sec=2.0, default=symbol, task_name=f"ResolveSymbol_{symbol}")
        self.symbol_alias_cache[symbol] = res
        return res

    def get_symbol_trading_spec(self, symbol: str) -> Dict[str, Any]:
        """Fetches live broker trading specifications for a symbol with fallback to symbol registry."""
        resolved = self.resolve_symbol_name(symbol)
        spec_dict = {
            "symbol": resolved,
            "canonical": symbol,
            "trade_contract_size": 100000.0,
            "trade_tick_value": 1.0,
            "trade_tick_size": 0.0001,
            "trade_stops_level": 0,
            "trade_freeze_level": 0,
            "point": 0.0001,
            "digits": 5,
            "spread": 0,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }
        if not MT5_AVAILABLE or mt5 is None or not self.is_connected or self.mode == "paper":
            from jarvis.data.symbol_registry import resolve as resolve_sym
            s = resolve_sym(symbol)
            spec_dict.update({
                "trade_contract_size": s.contract_size,
                "trade_tick_value": s.pip_value_per_lot,
                "trade_tick_size": s.pip_size,
                "point": s.pip_size,
                "digits": s.digits,
                "spread": int(s.typical_spread_pips * 10),
            })
            return spec_dict

        try:
            with self._lock:
                sym_info = mt5.symbol_info(resolved)
                if sym_info:
                    spec_dict.update({
                        "trade_contract_size": float(getattr(sym_info, "trade_contract_size", 100000.0) or 100000.0),
                        "trade_tick_value": float(getattr(sym_info, "trade_tick_value", 1.0) or 1.0),
                        "trade_tick_size": float(getattr(sym_info, "trade_tick_size", 0.0001) or 0.0001),
                        "trade_stops_level": int(getattr(sym_info, "trade_stops_level", 0) or 0),
                        "trade_freeze_level": int(getattr(sym_info, "trade_freeze_level", 0) or 0),
                        "point": float(getattr(sym_info, "point", 0.0001) or 0.0001),
                        "digits": int(getattr(sym_info, "digits", 5) or 5),
                        "spread": int(getattr(sym_info, "spread", 0) or 0),
                        "volume_min": float(getattr(sym_info, "volume_min", 0.01) or 0.01),
                        "volume_max": float(getattr(sym_info, "volume_max", 100.0) or 100.0),
                        "volume_step": float(getattr(sym_info, "volume_step", 0.01) or 0.01),
                    })
        except Exception as e:
            logger.warning(f"Failed to fetch MT5 symbol info for {resolved}: {e}")
        return spec_dict

    def get_account_snapshot(self) -> AccountSnapshot:
        # `_reconnect_if_needed()` first: it is what rewrites self.mode to
        # "paper" when the MT5 package is absent, and that rewrite has to be
        # visible to the check below.
        self._reconnect_if_needed()

        # Paper mode must never size against the broker.
        #
        # `init_connection()` does NOT call mt5.initialize() for paper -- it just
        # sets is_connected=True -- but the DATA path does initialise a terminal
        # to fetch real bars, so `mt5.account_info()` answers anyway, with the
        # LIVE account. Checking the mode only *after* that call meant a paper
        # run sized every position against the real balance: measured 762.51
        # against a simulated book of 10,000. `MT5StateSynchronizer` then caches
        # it into state_manager.account, so every later risk and sizing decision
        # inherits it too.
        if self.mode == "paper":
            with self._lock:
                paper_pnl = sum(getattr(p, "profit", 0.0) for p in self._paper_positions.values())
            return AccountSnapshot(
                login=999999,
                server="HM Algo 2.0-PAPER",
                balance=10000.0,
                equity=10000.0 + paper_pnl,
                margin=0.0,
                free_margin=10000.0 + paper_pnl,
                margin_level=0.0,
                leverage=100,
                profit=paper_pnl,
                name="Paper Account",
                company="HM Algo 2.0 Simulator",
                currency="USD",
                trade_allowed=True,
            )

        if MT5_AVAILABLE and mt5 is not None:
            try:
                with self._lock:
                    acc = mt5.account_info()
                    if acc is not None:
                        return AccountSnapshot(
                            login=int(acc.login),
                            server=str(acc.server),
                            balance=float(acc.balance),
                            equity=float(acc.equity),
                            margin=float(acc.margin),
                            free_margin=float(acc.margin_free),
                            margin_level=float(getattr(acc, "margin_level", 0.0)),
                            leverage=int(acc.leverage),
                            profit=float(getattr(acc, "profit", 0.0)),
                            name=str(getattr(acc, "name", "Trader")),
                            company=str(getattr(acc, "company", "XM Global")),
                            currency=str(acc.currency),
                            trade_allowed=bool(acc.trade_allowed)
                        )
            except Exception as e:
                logger.error(f"Failed to fetch live MT5 account snapshot: {e}")

        # Paper is handled above, before any broker call is made.

        # Dynamic Offline / Disconnected State (Zero Hardcoded Mock Data)
        return AccountSnapshot(
            login=0,
            server="DISCONNECTED",
            balance=0.0,
            equity=0.0,
            margin=0.0,
            free_margin=0.0,
            margin_level=0.0,
            leverage=0,
            profit=0.0,
            name="Offline Terminal",
            company="DISCONNECTED",
            currency="USD",
            trade_allowed=False
        )


    def get_open_positions(self, symbol: Optional[str] = None) -> List[PositionSnapshot]:

        self._reconnect_if_needed()

        if self.mode == "paper":
            with self._lock:
                if symbol:
                    resolved = self.resolve_symbol_name(symbol)
                    return [p for p in self._paper_positions.values() if p.symbol == resolved or p.symbol == symbol]
                return list(self._paper_positions.values())

        if not self.is_connected or not MT5_AVAILABLE:
            # A live/demo session with no broker link has UNKNOWN positions - not
            # the paper book. `_paper_positions` is aliased to a CLASS-level dict
            # (see `_shared_paper_positions`) that is never cleared, so returning
            # it here put a stale simulated GOLD entry from an earlier paper run
            # on the dashboard beside the real XM account: "Positions 1", a 2400.0
            # entry against a 4328.88 market, and a BUY whose stop-loss sat ABOVE
            # its entry. The MT5 service flag already reports DISCONNECTED, so an
            # empty list is the honest answer.
            with self._lock:
                withheld = len(self._paper_positions)
            if withheld:
                logger.warning(
                    "get_open_positions: MT5 disconnected in %s mode; withholding %d stale "
                    "paper position(s) instead of reporting them as live.",
                    self.mode, withheld,
                )
            return []

        try:
            with self._lock:
                resolved = self.resolve_symbol_name(symbol) if symbol else None
                positions = mt5.positions_get(symbol=resolved) if resolved else mt5.positions_get()
                if not positions:
                    return []

                # `p.time` is BROKER SERVER time, not UTC (XM: GMT+2/+3). Derive the
                # offset from these positions' own symbols - they are guaranteed to be
                # in Market Watch, so their ticks are available - and stamp open_time
                # in true UTC. Downstream code (position_monitor's duration, and every
                # stagnation/time-decay exit built on it) subtracts this from
                # datetime.now(timezone.utc), so a 2-3h mislabel silently shortens every
                # holding time and, after the max(0, ...) clamp, zeroes it entirely for
                # the first three hours of a position's life.
                pos_symbols = [str(p.symbol) for p in positions]
                offset = broker_utc_offset(mt5_module=mt5, symbols=pos_symbols)

                results = []
                for p in positions:
                    results.append(PositionSnapshot(
                        ticket=int(p.ticket),
                        symbol=str(p.symbol),
                        type="BUY" if p.type == getattr(mt5, "POSITION_TYPE_BUY", 0) else "SELL",
                        volume=float(p.volume),
                        open_price=float(p.price_open),
                        current_price=float(p.price_current),
                        sl=float(p.sl),
                        tp=float(p.tp),
                        profit=float(p.profit),
                        swap=float(p.swap),
                        commission=float(getattr(p, "commission", 0.0)),
                        open_time=time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.gmtime(int(p.time) - offset)
                        ),
                        magic=int(p.magic),
                        comment=str(p.comment)
                    ))
                return results
        except Exception as e:
            logger.error(f"MT5 get_open_positions failed: {e}")
            return []

    def _paper_fill_price(self, symbol: str, reference_price: float) -> Optional[float]:
        """A real price for a paper fill, or None when we genuinely have none.

        This used to be a hardcoded table (XAU 2400.0, EUR 1.0850, BTC 65000.0,
        JPY 155.0, else 1.2700). That recorded a fabricated entry - GOLD filled
        at 2400.0 while the market was at 4328.88 - and because `current_price`
        was set to the same constant, `profit` was structurally 0.0 forever, so
        the dashboard showed "OPEN P&L 0.00" on a position plainly in profit.
        Worse, the caller had computed SL/TP from the REAL price, so the ticket
        carried a BUY whose stop-loss (4294.29) sat ABOVE its entry (2400.0) -
        a structurally impossible order whose displayed R:R was meaningless.

        Callers that already know the price must pass it. Only when they do not
        do we pay for a quote lookup; if that fails too we return None and the
        order is refused rather than filled at an invented number.
        """
        if isinstance(reference_price, (int, float)) and math.isfinite(reference_price) and reference_price > 0:
            return float(reference_price)
        try:
            from jarvis.data.tradingview_provider import TRADINGVIEW_PROVIDER
            quotes = TRADINGVIEW_PROVIDER.fetch_quotes([symbol]) or {}
            for key in (symbol, symbol.upper(), self.resolve_symbol_name(symbol)):
                q = quotes.get(str(key).strip().upper())
                if not q:
                    continue
                # A `profile_reference` quote is a static baseline, not a market
                # price. Filling from it would put the same fabricated entry back
                # on the book that this function exists to prevent.
                if q.get("is_fallback") or q.get("source") != "tradingview":
                    logger.warning(
                        f"[PAPER] {symbol}: provider returned a {q.get('source')!r} reference "
                        f"rather than a live quote; refusing to fill from it."
                    )
                    continue
                px = q.get("price")
                if isinstance(px, (int, float)) and math.isfinite(px) and px > 0:
                    logger.info(f"[PAPER] {symbol}: no reference_price given, using quote {px}")
                    return float(px)
        except Exception as e:
            logger.warning(f"Paper fill price lookup failed for {symbol}: {e}")
        return None

    @staticmethod
    def _level_error(order_type: str, price: float, sl: float, tp: float) -> Optional[str]:
        """Stop-loss and take-profit must straddle the fill price.

        Returns a message when they do not, else None. This is the check whose
        absence let a BUY with SL above its entry reach the book.
        """
        if not (isinstance(sl, (int, float)) and math.isfinite(sl) and sl > 0):
            return "stop-loss must be a positive finite number"
        if not (isinstance(tp, (int, float)) and math.isfinite(tp) and tp > 0):
            return "take-profit must be a positive finite number"
        if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
            return "fill price must be a positive finite number"
        if order_type == "BUY":
            if sl >= price:
                return f"BUY stop-loss {sl} is not below the fill price {price}"
            if tp <= price:
                return f"BUY take-profit {tp} is not above the fill price {price}"
        else:
            if sl <= price:
                return f"SELL stop-loss {sl} is not above the fill price {price}"
            if tp >= price:
                return f"SELL take-profit {tp} is not below the fill price {price}"
        return None

    def mark_paper_positions(self, prices: Dict[str, float]) -> int:
        """Mark the paper book to market; returns how many positions moved.

        Paper positions were never re-priced, so `profit` stayed at the 0.0 it
        was born with and the dashboard reported "OPEN P&L 0.00" no matter where
        the market went. Positions we cannot price are left alone, and the UI
        shows their P&L as unknown rather than as a flat zero.
        """
        if not prices:
            return 0
        from jarvis.data.symbol_registry import resolve, is_registered
        moved = 0
        with self._lock:
            for pos in self._paper_positions.values():
                px = prices.get(pos.symbol)
                if px is None:
                    px = prices.get(str(pos.symbol).strip().upper())
                if not (isinstance(px, (int, float)) and math.isfinite(px) and px > 0):
                    continue
                # An UNregistered symbol resolves to a generic FX fallback spec,
                # whose contract_size is 1000x too big for gold - a wrong P&L is
                # worse than none, so skip rather than mark with a guessed size.
                if not is_registered(pos.symbol):
                    continue
                contract = getattr(resolve(pos.symbol), "contract_size", 0.0) or 0.0
                if contract <= 0:
                    continue
                direction = 1.0 if str(pos.type).upper() == "BUY" else -1.0
                pos.current_price = float(px)
                pos.profit = round(direction * (float(px) - float(pos.open_price)) * contract * float(pos.volume), 2)
                moved += 1
        return moved

    def send_market_order(
        self,
        symbol: str,
        order_type: str,
        volume: float,
        sl_price: float,
        tp_price: float,
        comment: str = "HMAlgo2",
        reference_price: float = 0.0
    ) -> Dict[str, Any]:
        if self.mode not in {"live", "demo", "paper"}:
            return {"status": "BLOCKED", "reason": f"Execution is disabled (mode={self.mode})"}
        if order_type not in {"BUY", "SELL"}:
            return {"status": "FAILED", "reason": "order_type must be BUY or SELL"}
        if not isinstance(volume, (int, float)) or not math.isfinite(volume) or volume <= 0:
            return {"status": "FAILED", "reason": "volume must be a positive finite number"}

        # Capture BEFORE `_reconnect_if_needed()`. With no terminal installed,
        # `init_connection()` rewrites `self.mode` to "paper" and returns True,
        # so by the time the branch below runs the client no longer remembers
        # that a live/demo order was asked for. That silent downgrade erases
        # the only evidence that real money was booked against a price no
        # broker ever quoted — so read the requested mode now and report it as
        # `is_fallback` on the fill (D1).
        requested_mode = str(self.mode or "").lower()
        self._reconnect_if_needed()
        resolved = self.resolve_symbol_name(symbol)

        if requested_mode in ("live", "demo"):
            if not MT5_AVAILABLE or not self.is_connected or mt5 is None:
                logger.error(
                    f"FAIL-CLOSED: MT5 is disconnected/unavailable in {requested_mode.upper()} mode. "
                    "Refusing paper fallback. Order blocked."
                )
                return {
                    "status": "FAILED",
                    "reason": f"FAIL_CLOSED: MT5 broker disconnected in {requested_mode.upper()} mode"
                }
            acc = mt5.account_info()
            if acc:
                trade_mode = getattr(acc, "trade_mode", 0)
                if requested_mode == "demo" and trade_mode == 2:
                    logger.critical(f"SAFETY BLOCK: Refusing order in DEMO mode because connected MT5 account #{acc.login} is REAL!")
                    return {"status": "BLOCKED", "reason": f"SAFETY_BLOCK: Connected to REAL MT5 account #{acc.login} while bot in DEMO mode"}
                elif requested_mode == "live" and trade_mode == 0:
                    logger.critical(f"SAFETY BLOCK: Refusing order in LIVE mode because connected MT5 account #{acc.login} is DEMO!")
                    return {"status": "BLOCKED", "reason": f"SAFETY_BLOCK: Connected to DEMO MT5 account #{acc.login} while bot in LIVE mode"}

        if self.mode == "paper" or not MT5_AVAILABLE:
            # A simulated fill is what paper mode ASKED for, so the row is
            # honest as `paper`.
            is_fallback = requested_mode != "paper"
            price = self._paper_fill_price(symbol, reference_price)
            if price is None:
                return {
                    "status": "FAILED",
                    "reason": (
                        f"No reference price available for {symbol}; refusing to fill at a "
                        f"placeholder. Pass reference_price from the caller's own quote."
                    ),
                }
            incoherent = self._level_error(order_type, price, sl_price, tp_price)
            if incoherent:
                return {"status": "FAILED", "reason": incoherent}

            with self._lock:
                ticket = int(time.time() * 1000) % 100000000
                pos = PositionSnapshot(
                    ticket=ticket,
                    symbol=resolved,
                    type=order_type,
                    volume=volume,
                    open_price=price,
                    current_price=price,
                    sl=float(sl_price),
                    tp=float(tp_price),
                    profit=0.0,
                    swap=0.0,
                    commission=0.0,
                    open_time=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                    magic=self.magic_number,
                    comment=f"[PAPER] {comment}"
                )
                self._paper_positions[ticket] = pos
                logger.info(f"[PAPER] Order FILLED: #{ticket} {order_type} {volume} {resolved} @ {price}")
                return {
                    "status": "FILLED",
                    "ticket": ticket,
                    # Paper has one synthetic id for both: there is no separate
                    # order ticket and position id to confuse.
                    "position_id": ticket,
                    "symbol": resolved,
                    "type": order_type,
                    "volume": volume,
                    "price": price,
                    "is_fallback": is_fallback,
                    "comment": f"[PAPER] {comment}"
                }

        def _send():
            with self._lock:
                sym_info = mt5.symbol_info(resolved)
                tick = mt5.symbol_info_tick(resolved)
                if not tick or not sym_info:
                    return {"status": "FAILED", "reason": f"Tick or symbol metadata unavailable for {resolved}"}

                vol_min = getattr(sym_info, "volume_min", 0.01)
                vol_max = getattr(sym_info, "volume_max", 100.0)
                vol_step = getattr(sym_info, "volume_step", 0.01)

                if vol_step > 0:
                    steps = round((volume - vol_min) / vol_step)
                    quantized_vol = vol_min + steps * vol_step
                else:
                    quantized_vol = volume

                quantized_vol = max(vol_min, min(vol_max, quantized_vol))
                vol_decimals = 2 if vol_step <= 0.01 else (1 if vol_step <= 0.1 else 0)
                final_volume = round(quantized_vol, vol_decimals)

                price = tick.ask if order_type == "BUY" else tick.bid
                type_op = getattr(mt5, "ORDER_TYPE_BUY", 0) if order_type == "BUY" else getattr(mt5, "ORDER_TYPE_SELL", 1)
                digits = sym_info.digits
                point = sym_info.point or (10 ** -digits)
                spread_dist = abs(tick.ask - tick.bid)
                min_stop_dist = max(
                    getattr(sym_info, "trade_stops_level", 0) * point,
                    getattr(sym_info, "trade_freeze_level", 0) * point,
                    spread_dist * 1.25,
                    10 * point
                )

                final_sl = float(sl_price)
                final_tp = float(tp_price)
                if order_type == "BUY":
                    # BUY: SL must be strictly below current Bid; TP strictly above current Ask
                    if final_sl > 0 and final_sl >= (tick.bid - min_stop_dist):
                        final_sl = tick.bid - min_stop_dist
                    if final_tp > 0 and final_tp <= (tick.ask + min_stop_dist):
                        final_tp = tick.ask + min_stop_dist
                else:
                    # SELL: SL must be strictly ABOVE current Ask; TP strictly BELOW current Bid
                    if final_sl > 0 and final_sl <= (tick.ask + min_stop_dist):
                        final_sl = tick.ask + min_stop_dist
                    if final_tp > 0 and final_tp >= (tick.bid - min_stop_dist):
                        final_tp = tick.bid - min_stop_dist

                request = {
                    "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
                    "symbol": resolved,
                    "volume": final_volume,
                    "type": type_op,
                    "price": round(price, digits),
                    "sl": round(final_sl, digits) if final_sl > 0 else 0.0,
                    "tp": round(final_tp, digits) if final_tp > 0 else 0.0,
                    "deviation": 50,
                    "magic": self.magic_number,
                    "comment": comment,
                    "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
                    "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 1)
                }

                # Direct fast execution with instant filling fallback & requote retry loop
                max_retries = 3
                for attempt in range(max_retries):
                    result = mt5.order_send(request)
                    if result is not None and result.retcode in [getattr(mt5, "TRADE_RETCODE_DONE", 10009), getattr(mt5, "TRADE_RETCODE_PLACED", 10008)]:
                        break

                    # If filling type rejected (10030 / 10031), retry filling modes
                    if result and result.retcode in [10030, 10031]:
                        request["type_filling"] = getattr(mt5, "ORDER_FILLING_FOK", 0)
                        result = mt5.order_send(request)
                        if result and result.retcode in [10030, 10031]:
                            request["type_filling"] = getattr(mt5, "ORDER_FILLING_RETURN", 2)
                            result = mt5.order_send(request)

                    # Requote error (10004) -> refresh tick price and retry after small backoff
                    if result and result.retcode == 10004 and attempt < (max_retries - 1):
                        time.sleep(0.1 * (2 ** attempt))
                        fresh_tick = mt5.symbol_info_tick(resolved)
                        if fresh_tick:
                            request["price"] = round(fresh_tick.ask if order_type == "BUY" else fresh_tick.bid, digits)
                            logger.info(f"Requote retry #{attempt+1} for {resolved}: updated price -> {request['price']}")
                        continue

                if result is None or result.retcode not in [getattr(mt5, "TRADE_RETCODE_DONE", 10009), getattr(mt5, "TRADE_RETCODE_PLACED", 10008)]:
                    err_msg = result.comment if result else str(mt5.last_error())
                    logger.error(f"MT5 Order Send Failed for {resolved}: {err_msg}")
                    return {"status": "FAILED", "reason": err_msg}


                logger.info(f"⚡ ULTRA-FAST ORDER FILLED: Ticket={result.order} {order_type} {volume} {resolved} @ {result.price}")
                # `result.order` is the ORDER ticket. The position it opened has a
                # DIFFERENT identifier, and that identifier — `deal.position_id` —
                # is the only key the exit deal will later be reported under.
                # Returning only the order ticket meant the journal row written at
                # entry could never be matched to its exit (D2), so the trade
                # stayed open forever with realized_pnl 0.0.
                position_id = None
                try:
                    deal_ticket = int(getattr(result, "deal", 0) or 0)
                    if deal_ticket > 0:
                        deals = mt5.history_deals_get(ticket=deal_ticket)
                        if deals:
                            position_id = int(getattr(deals[0], "position_id", 0)) or None
                    if position_id is None:
                        # Netting accounts and some brokers report no deal on the
                        # result; the position itself is the fallback.
                        opened = mt5.positions_get(ticket=int(getattr(result, "order", 0) or 0))
                        if opened:
                            position_id = int(getattr(opened[0], "ticket", 0)) or None
                except Exception as e:
                    logger.warning(
                        "Could not resolve the position id for order %s (%s); the "
                        "journal row will only be matchable by order ticket.",
                        getattr(result, "order", "?"), e,
                    )
                return {
                    "status": "FILLED",
                    "ticket": result.order,
                    "position_id": position_id,
                    "volume": result.volume,
                    "price": result.price,
                    # Spelled out rather than left absent: a reader that does
                    # `res.get("is_fallback")` cannot tell "False" from "never
                    # sent", and an absent key is how a frontend renders the
                    # empty state on success.
                    "is_fallback": False,
                    "comment": result.comment
                }

        # "UNKNOWN", not "FAILED": a timed-out order_send may well have filled at
        # the broker. Reporting FAILED made the caller release the risk
        # reservation and skip the cooldown, so the next sweep could re-send the
        # same order and DOUBLE the position. An indeterminate outcome is its own
        # category and must be reconciled against the broker before any retry.
        return TimeoutGuard.run_sync(
            _send,
            timeout_sec=5.0,
            default={"status": "UNKNOWN", "reason": "Timeout"},
            task_name=f"MT5_SendOrder_{symbol}",
        )

    def place_pending_order(
        self,
        symbol: str,
        order_type: str,
        price: float,
        volume: float,
        sl_price: float = 0.0,
        tp_price: float = 0.0,
        comment: str = "JARVIS_LIMIT"
    ) -> Dict[str, Any]:
        """Places a pending limit/stop order on MT5 (BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP)."""
        if self.mode not in {"live", "demo", "paper"}:
            return {"status": "BLOCKED", "reason": f"Execution is disabled (mode={self.mode})"}
        valid_types = {"BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"}
        if order_type not in valid_types:
            return {"status": "FAILED", "reason": f"order_type must be one of {valid_types}"}

        self._reconnect_if_needed()
        resolved = self.resolve_symbol_name(symbol)
        requested_mode = str(self.mode or "").lower()

        if requested_mode in ("live", "demo"):
            if not MT5_AVAILABLE or not self.is_connected or mt5 is None:
                logger.error(
                    f"FAIL-CLOSED: MT5 is disconnected/unavailable in {requested_mode.upper()} mode. "
                    "Refusing paper fallback for pending order. Order blocked."
                )
                return {
                    "status": "FAILED",
                    "reason": f"FAIL_CLOSED: MT5 broker disconnected in {requested_mode.upper()} mode"
                }
            acc = mt5.account_info()
            if acc:
                trade_mode = getattr(acc, "trade_mode", 0)
                if requested_mode == "demo" and trade_mode == 2:
                    logger.critical(f"SAFETY BLOCK: Refusing pending order in DEMO mode because connected MT5 account #{acc.login} is REAL!")
                    return {"status": "BLOCKED", "reason": f"SAFETY_BLOCK: Connected to REAL MT5 account #{acc.login} while bot in DEMO mode"}
                elif requested_mode == "live" and trade_mode == 0:
                    logger.critical(f"SAFETY BLOCK: Refusing pending order in LIVE mode because connected MT5 account #{acc.login} is DEMO!")
                    return {"status": "BLOCKED", "reason": f"SAFETY_BLOCK: Connected to DEMO MT5 account #{acc.login} while bot in LIVE mode"}

        if self.mode == "paper" or not MT5_AVAILABLE:
            ticket = int(time.time() * 1000) % 100000000
            order_info = {
                "status": "PLACED",
                "ticket": ticket,
                "symbol": resolved,
                "type": order_type,
                "volume": volume,
                "price": price,
                "sl": sl_price,
                "tp": tp_price,
                "comment": f"[PAPER] {comment}",
                "time_setup": int(time.time())
            }
            with self._lock:
                self._paper_pending_orders[ticket] = order_info
            logger.info(f"[PAPER] Pending order PLACED: #{ticket} {order_type} {volume} {resolved} @ {price}")
            return order_info

        def _send_pending():
            with self._lock:
                sym_info = mt5.symbol_info(resolved)
                if not sym_info:
                    return {"status": "FAILED", "reason": f"Symbol metadata unavailable for {resolved}"}

                digits = sym_info.digits
                type_map = {
                    "BUY_LIMIT": getattr(mt5, "ORDER_TYPE_BUY_LIMIT", 2),
                    "SELL_LIMIT": getattr(mt5, "ORDER_TYPE_SELL_LIMIT", 3),
                    "BUY_STOP": getattr(mt5, "ORDER_TYPE_BUY_STOP", 4),
                    "SELL_STOP": getattr(mt5, "ORDER_TYPE_SELL_STOP", 5),
                }

                request = {
                    "action": getattr(mt5, "TRADE_ACTION_PENDING", 5),
                    "symbol": resolved,
                    "volume": volume,
                    "type": type_map[order_type],
                    "price": round(price, digits),
                    "sl": round(sl_price, digits) if sl_price > 0 else 0.0,
                    "tp": round(tp_price, digits) if tp_price > 0 else 0.0,
                    "magic": self.magic_number,
                    "comment": comment,
                    "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
                    "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 1)
                }

                result = mt5.order_send(request)
                if result and result.retcode in [10030, 10031]:
                    request["type_filling"] = getattr(mt5, "ORDER_FILLING_FOK", 0)
                    result = mt5.order_send(request)
                    if result and result.retcode in [10030, 10031]:
                        request["type_filling"] = getattr(mt5, "ORDER_FILLING_RETURN", 2)
                        result = mt5.order_send(request)

                if result is None or result.retcode not in [getattr(mt5, "TRADE_RETCODE_DONE", 10009), getattr(mt5, "TRADE_RETCODE_PLACED", 10008)]:
                    err_msg = result.comment if result else str(mt5.last_error())
                    logger.error(f"MT5 Pending Order Failed for {resolved}: {err_msg}")
                    return {"status": "FAILED", "reason": err_msg}

                logger.info(f"⚡ PENDING ORDER PLACED: Ticket={result.order} {order_type} {volume} {resolved} @ {price}")
                return {
                    "status": "PLACED",
                    "ticket": result.order,
                    "symbol": resolved,
                    "type": order_type,
                    "volume": volume,
                    "price": price,
                    "comment": result.comment
                }

        return TimeoutGuard.run_sync(_send_pending, timeout_sec=5.0, default={"status": "FAILED", "reason": "Timeout"}, task_name=f"MT5_Pending_{symbol}")

    def get_pending_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns active pending orders from MT5."""
        if self.mode == "paper" or not MT5_AVAILABLE or not self.is_connected:
            with self._lock:
                resolved = self.resolve_symbol_name(symbol) if symbol else None
                if resolved:
                    return [o for o in self._paper_pending_orders.values() if o.get("symbol") == resolved or o.get("symbol") == symbol]
                return list(self._paper_pending_orders.values())
        resolved = self.resolve_symbol_name(symbol) if symbol else None
        orders = mt5.orders_get(symbol=resolved) if resolved else mt5.orders_get()
        if not orders:
            return []
        res = []
        for o in orders:
            res.append({
                "ticket": o.ticket,
                "symbol": o.symbol,
                "type": o.type,
                "volume": o.volume_initial,
                "price": o.price_open,
                "sl": o.sl,
                "tp": o.tp,
                "comment": o.comment,
                "time_setup": o.time_setup
            })
        return res

    def cancel_pending_order(self, ticket: int) -> Dict[str, Any]:
        """Cancels a pending order by ticket."""
        if self.mode == "paper" or not MT5_AVAILABLE or not self.is_connected:
            with self._lock:
                self._paper_pending_orders.pop(ticket, None)
            return {"status": "CANCELLED", "ticket": ticket}
        request = {
            "action": getattr(mt5, "TRADE_ACTION_REMOVE", 8),
            "order": ticket
        }
        result = mt5.order_send(request)
        if result and result.retcode in [getattr(mt5, "TRADE_RETCODE_DONE", 10009), getattr(mt5, "TRADE_RETCODE_PLACED", 10008)]:
            logger.info(f"⚡ PENDING ORDER CANCELLED: Ticket={ticket}")
            return {"status": "CANCELLED", "ticket": ticket}
        err_msg = result.comment if result else str(mt5.last_error())
        return {"status": "FAILED", "reason": err_msg}

    def cancel_all_pending_orders(self) -> List[Dict[str, Any]]:
        """Cancels all currently active pending orders."""
        orders = self.get_pending_orders()
        results = []
        for o in orders:
            t = o.get("ticket")
            if t:
                results.append(self.cancel_pending_order(int(t)))
        return results

    def modify_pending_order(
        self,
        ticket: int,
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Modifies the price / SL / TP of a working pending order.

        ``TRADE_ACTION_MODIFY`` replaces the whole order definition, so a field
        the caller leaves as ``None`` is re-sent at its *current broker value*.
        Sending 0.0 would silently delete the stop rather than preserve it,
        which is the failure mode this signature exists to prevent. Pass an
        explicit ``0`` to clear a level.
        """
        if self.mode not in {"live", "demo", "paper"}:
            return {"status": "BLOCKED", "reason": f"Execution is disabled (mode={self.mode})"}
        if price is None and sl is None and tp is None:
            return {"status": "FAILED", "reason": "nothing to modify — supply price, sl or tp"}

        if self.mode == "paper" or not MT5_AVAILABLE or not self.is_connected:
            with self._lock:
                order = self._paper_pending_orders.get(int(ticket))
                if order is None:
                    return {"status": "FAILED", "reason": f"Order #{ticket} not found in paper orders"}
                if price is not None:
                    order["price"] = float(price)
                if sl is not None:
                    order["sl"] = float(sl)
                if tp is not None:
                    order["tp"] = float(tp)
                logger.info(
                    "[PAPER] Pending order MODIFIED: #%s -> price=%s sl=%s tp=%s",
                    ticket, order["price"], order["sl"], order["tp"],
                )
                return {
                    "status": "MODIFIED", "ticket": int(ticket),
                    "price": order["price"], "sl": order["sl"], "tp": order["tp"],
                }

        def _modify_pending():
            with self._lock:
                orders = mt5.orders_get(ticket=int(ticket))
                if not orders:
                    return {"status": "FAILED", "reason": f"Order #{ticket} not found on MT5"}
                pending = orders[0]

                sym_info = mt5.symbol_info(pending.symbol)
                if not sym_info:
                    return {"status": "FAILED", "reason": f"Symbol metadata unavailable for {pending.symbol}"}
                digits = sym_info.digits

                new_price = float(price) if price is not None else float(pending.price_open)
                new_sl = float(sl) if sl is not None else float(pending.sl)
                new_tp = float(tp) if tp is not None else float(pending.tp)

                request = {
                    "action": getattr(mt5, "TRADE_ACTION_MODIFY", 7),
                    "order": int(ticket),
                    "symbol": pending.symbol,
                    "volume": float(pending.volume_initial),
                    "type": int(pending.type),
                    "price": round(new_price, digits),
                    "sl": round(new_sl, digits) if new_sl > 0 else 0.0,
                    "tp": round(new_tp, digits) if new_tp > 0 else 0.0,
                    "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
                    "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 1),
                }

                result = mt5.order_send(request)
                # 10030 = unsupported filling, 10031 = no quotes to process.
                # Brokers disagree about which mode a pending order accepts, so
                # walk the three modes rather than failing on the first.
                for fallback in ("ORDER_FILLING_FOK", "ORDER_FILLING_RETURN"):
                    if not (result and result.retcode in (10030, 10031)):
                        break
                    request["type_filling"] = getattr(mt5, fallback, 0)
                    result = mt5.order_send(request)

                done = getattr(mt5, "TRADE_RETCODE_DONE", 10009)
                placed = getattr(mt5, "TRADE_RETCODE_PLACED", 10008)
                if result is None or result.retcode not in (done, placed):
                    err_msg = result.comment if result else str(mt5.last_error())
                    logger.error("MT5 Pending Modify Failed for #%s: %s", ticket, err_msg)
                    return {"status": "FAILED", "reason": err_msg}

                logger.info(
                    "PENDING ORDER MODIFIED: #%s -> price=%s sl=%s tp=%s",
                    ticket, request["price"], request["sl"], request["tp"],
                )
                return {
                    "status": "MODIFIED", "ticket": int(ticket),
                    "price": request["price"], "sl": request["sl"], "tp": request["tp"],
                }

        return TimeoutGuard.run_sync(
            _modify_pending, timeout_sec=5.0,
            default={"status": "FAILED", "reason": "Timeout"},
            task_name=f"MT5_ModifyPending_{ticket}",
        )

    def close_position(self, ticket: int, volume: Optional[float] = None) -> Dict[str, Any]:
        """Closes a specific open MT5 position (full or partial) by ticket."""
        if self.mode == "paper" or not MT5_AVAILABLE or not self.is_connected:
            with self._lock:
                if ticket in self._paper_positions:
                    pos = self._paper_positions[ticket]
                    if volume is not None and 0 < volume < pos.volume:
                        pos.volume = round(pos.volume - volume, 2)
                        pnl = round(pos.profit * (volume / (pos.volume + volume)), 2)
                        logger.info(f"[PAPER] Partially closed position #{ticket} ({pos.symbol}) by {volume} lots. Remaining: {pos.volume}")
                        return {
                            "status": "PARTIALLY_CLOSED",
                            "ticket": ticket,
                            "closed_volume": volume,
                            "remaining_volume": pos.volume,
                            "pnl": pnl,
                            "price": pos.current_price
                        }
                    else:
                        pos = self._paper_positions.pop(ticket)
                        logger.info(f"[PAPER] Closed simulated position #{ticket} ({pos.symbol})")
                        return {"status": "CLOSED", "ticket": ticket, "pnl": pos.profit, "price": pos.current_price}
                logger.info(f"[PAPER] Position #{ticket} not found or already closed")
                return {"status": "CLOSED", "ticket": ticket, "pnl": 0.0, "price": 0.0}

        def _close():
            with self._lock:
                positions = mt5.positions_get(ticket=ticket)
                if not positions or len(positions) == 0:
                    return {"status": "FAILED", "reason": f"Position #{ticket} not found on MT5"}

                p = positions[0]
                symbol = p.symbol
                tick = mt5.symbol_info_tick(symbol)
                sym_info = mt5.symbol_info(symbol)
                if not tick or not sym_info:
                    return {"status": "FAILED", "reason": f"Tick info unavailable for {symbol}"}

                # Calculate volume to close
                close_volume = float(volume) if (volume is not None and 0 < volume < p.volume) else float(p.volume)
                is_partial = close_volume < p.volume

                # Opposite order type
                is_buy = p.type == getattr(mt5, "POSITION_TYPE_BUY", 0)
                order_type = getattr(mt5, "ORDER_TYPE_SELL", 1) if is_buy else getattr(mt5, "ORDER_TYPE_BUY", 0)
                price = tick.bid if is_buy else tick.ask

                request = {
                    "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
                    "position": ticket,
                    "symbol": symbol,
                    "volume": close_volume,
                    "type": order_type,
                    "price": round(price, sym_info.digits),
                    "deviation": 50,
                    "magic": self.magic_number,
                    "comment": f"Partial #{ticket}" if is_partial else f"Close #{ticket}",
                    "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
                    "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 1)
                }

                result = mt5.order_send(request)
                if result is None or result.retcode != getattr(mt5, "TRADE_RETCODE_DONE", 10009):
                    err_msg = result.comment if result else str(mt5.last_error())
                    logger.error(f"MT5 Close Order Failed for #{ticket}: {err_msg}")
                    return {"status": "FAILED", "reason": err_msg}

                status_code = "PARTIALLY_CLOSED" if is_partial else "CLOSED"
                logger.info(f"LIVE POSITION {status_code}: Ticket=#{ticket} {symbol} closed {close_volume} lots @ {result.price}")
                return {
                    "status": status_code,
                    "ticket": ticket,
                    "closed_volume": close_volume,
                    "remaining_volume": round(p.volume - close_volume, 2) if is_partial else 0.0,
                    "price": result.price
                }

        return TimeoutGuard.run_sync(_close, timeout_sec=5.0, default={"status": "FAILED", "reason": "Timeout"}, task_name=f"MT5_Close_{ticket}")

    def partial_close(self, ticket: int, volume: float) -> Dict[str, Any]:
        """Convenience method to partially close an open position by ticket and volume."""
        return self.close_position(ticket, volume=volume)

    def modify_position(self, ticket: int, sl: float, tp: float) -> Dict[str, Any]:
        """Modifies Stop Loss and Take Profit of an open MT5 position."""
        if self.mode == "paper" or not MT5_AVAILABLE or not self.is_connected:
            with self._lock:
                if ticket in self._paper_positions:
                    self._paper_positions[ticket].sl = float(sl)
                    self._paper_positions[ticket].tp = float(tp)
                    logger.info(f"[PAPER] Modified position #{ticket} -> SL: {sl}, TP: {tp}")
                    return {"status": "MODIFIED", "ticket": ticket, "sl": sl, "tp": tp}
                return {"status": "FAILED", "reason": f"Position #{ticket} not found in paper positions"}

        def _modify():
            with self._lock:
                positions = mt5.positions_get(ticket=ticket)
                if not positions or len(positions) == 0:
                    return {"status": "FAILED", "reason": f"Position #{ticket} not found on MT5"}

                p = positions[0]
                symbol = p.symbol
                sym_info = mt5.symbol_info(symbol)
                if not sym_info:
                    return {"status": "FAILED", "reason": f"Symbol info unavailable for {symbol}"}

                digits = sym_info.digits
                point = sym_info.point or (10 ** -digits)
                min_stop_dist = max(getattr(sym_info, "trade_stops_level", 0), getattr(sym_info, "freeze_level", 0), 5) * point

                tick = mt5.symbol_info_tick(symbol)
                cur_price = (tick.bid if p.type == getattr(mt5, "POSITION_TYPE_BUY", 0) else tick.ask) if tick else p.price_current

                final_sl = float(sl)
                final_tp = float(tp)
                is_buy = (p.type == getattr(mt5, "POSITION_TYPE_BUY", 0))

                if is_buy:
                    if final_sl > 0 and (cur_price - final_sl) < min_stop_dist:
                        final_sl = cur_price - min_stop_dist
                    if final_tp > 0 and (final_tp - cur_price) < min_stop_dist:
                        final_tp = cur_price + min_stop_dist
                else:
                    if final_sl > 0 and (final_sl - cur_price) < min_stop_dist:
                        final_sl = cur_price + min_stop_dist
                    if final_tp > 0 and (cur_price - final_tp) < min_stop_dist:
                        final_tp = cur_price - min_stop_dist

                request = {
                    "action": getattr(mt5, "TRADE_ACTION_SLTP", 6),
                    "position": ticket,
                    "symbol": symbol,
                    "sl": round(final_sl, digits) if final_sl > 0 else 0.0,
                    "tp": round(final_tp, digits) if final_tp > 0 else 0.0,
                    "magic": self.magic_number,
                    "comment": "JARVIS_SLTP_MODIFY"
                }

                result = mt5.order_send(request)
                if result is None or result.retcode not in [getattr(mt5, "TRADE_RETCODE_DONE", 10009), getattr(mt5, "TRADE_RETCODE_PLACED", 10008)]:
                    err_msg = result.comment if result else str(mt5.last_error())
                    logger.error(f"MT5 SL/TP Modify Failed for #{ticket}: {err_msg}")
                    return {"status": "FAILED", "reason": err_msg}

                logger.info(f"⚡ LIVE SL/TP MODIFIED: Ticket=#{ticket} {symbol} -> New SL={round(final_sl, digits)}, TP={round(final_tp, digits)}")
                return {"status": "MODIFIED", "ticket": ticket, "sl": round(final_sl, digits), "tp": round(final_tp, digits)}

        return TimeoutGuard.run_sync(_modify, timeout_sec=5.0, default={"status": "FAILED", "reason": "Timeout"}, task_name=f"MT5_Modify_{ticket}")

    def close_all_positions(self) -> List[Dict[str, Any]]:
        """Closes all currently open MT5 positions."""
        positions = self.get_open_positions()
        results = []
        for p in positions:
            res = self.close_position(p.ticket)
            results.append(res)
        return results

    def broker_lock_health(self) -> Dict[str, Any]:
        """Who currently holds the broker lock, and for how long (P3).

        Every broker call in the process serialises on one lock, and that lock is
        held across native calls that Python cannot interrupt. When one of them
        hangs, the symptom used to be that each later call quietly timed out in
        isolation — indistinguishable from a slow market. This names the holder
        instead. Report it wherever `TimeoutGuard.health()` is reported.
        """
        return self._lock.health()

    def shutdown(self):
        if MT5_AVAILABLE and mt5 and self.is_connected and self.mode != "paper":
            try:
                mt5.shutdown()
            except Exception:
                pass
        self.is_connected = False
