"""
Broker symbol resolution.

WHY THIS MODULE EXISTS
----------------------
The project works in canonical names (XAUUSD, NAS100, GER40, BTCUSD...), but
brokers label the same instruments differently. On XMGlobal-MT5 for example:

    XAUUSD  -> GOLD.i#   (there is no "XAUUSD" at all)
    NAS100  -> US100Cash#
    GER40   -> GER40Cash#
    BTCUSD  -> BTCUSD#

The canonical name simply is not in the Market Watch, so
``copy_rates_from_pos("XAUUSD", ...)`` returns None with
``(-1, 'Terminal: Call failed')`` - which reads like a terminal fault rather
than the naming mismatch it actually is. That is the bug that made a live data
pull (and therefore the backtest on fresh data) fail.

Resolution order:
  1. the canonical name, if the broker has it
  2. each configured alias in order
  3. a fuzzy scan of the broker's symbol list, cached for the session

Call :func:`resolve_broker_symbol` instead of passing a canonical name straight
to ``mt5.copy_rates_*``.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger("JARVIS_BrokerSymbols")

__all__ = [
    "BROKER_ALIASES",
    "resolve_broker_symbol",
    "probe_symbol",
    "ensure_mt5_terminal",
    "terminal_ready",
    "terminal_live",
    "reset_cache",
]

# Canonical -> ordered list of broker aliases for this account.
# Add a symbol here when a new broker uses a different label; no other code
# needs to change.
BROKER_ALIASES: Dict[str, List[str]] = {
    "XAUUSD": ["GOLD.i#", "GOLD24-7.i#", "XAUUSD#", "XAUUSD"],
    "XAGUSD": ["SILVER.i#", "XAGUSD#", "XAGUSD"],
    "EURUSD": ["EURUSD#", "EURUSD.i#", "EURUSD.m", "EURUSD"],
    "GBPUSD": ["GBPUSD#", "GBPUSD.i#", "GBPUSD.m", "GBPUSD"],
    "USDJPY": ["USDJPY#", "USDJPY.i#", "USDJPY.m", "USDJPY"],
    "AUDUSD": ["AUDUSD#", "AUDUSD.i#", "AUDUSD.m", "AUDUSD"],
    "USDCAD": ["USDCAD#", "USDCAD.i#", "USDCAD.m", "USDCAD"],
    "USDCHF": ["USDCHF#", "USDCHF.i#", "USDCHF.m", "USDCHF"],
    "NZDUSD": ["NZDUSD#", "NZDUSD.i#", "NZDUSD.m", "NZDUSD"],
    "EURJPY": ["EURJPY#", "EURJPY.i#", "EURJPY.m", "EURJPY"],
    "GBPJPY": ["GBPJPY#", "GBPJPY.i#", "GBPJPY.m", "GBPJPY"],
    "WTI":    ["OILCash#", "USOILCash#", "USOIL#", "OIL#", "WTI#", "USOIL", "OIL"],
    "NAS100": ["US100Cash#", "US100-SEP26", "NAS100Cash#", "US100", "NAS100"],
    "US30":   ["US30Cash#", "US30-SEP26", "US30"],
    "GER40":  ["GER40Cash#", "GER40-SEP26", "DE40Cash#", "GER40"],
    "UK100":  ["UK100Cash#", "UK100-SEP26", "UK100"],
    "US500":  ["US500Cash#", "SPXUSD", "US500"],
    "GOLD.I#": ["GOLD.i#"],
    "SILVER.I#": ["SILVER.i#"],
    "BTCUSD": ["BTCUSD#", "BTCUSD"],
    "ETHUSD": ["ETHUSD#", "ETHUSD"],
    "SOLUSD": ["SOLUSD#", "SOLUSD"],
    "ETHBTC": ["ETHBTC#", "ETHBTC"],
}

# Alternate user/input names that map directly to a known canonical symbol.
# Kept separate from BROKER_ALIASES so alias tables remain one-way canonical -> broker name.
CANONICAL_SYNONYMS: Dict[str, str] = {
    "GOLD": "XAUUSD",
    "GOLD#": "XAUUSD",
    "SILVER": "XAGUSD",
    "SILVER#": "XAGUSD",
    "EURUSD#": "EURUSD",
    "GBPUSD#": "GBPUSD",
    "USDJPY#": "USDJPY",
    "AUDUSD#": "AUDUSD",
    "USDCAD#": "USDCAD",
    "USDCHF#": "USDCHF",
    "NZDUSD#": "NZDUSD",
    "EURJPY#": "EURJPY",
    "GBPJPY#": "GBPJPY",
    "BTCUSD#": "BTCUSD",
    "ETHUSD#": "ETHUSD",
    "SOLUSD#": "SOLUSD",
    "ETHBTC#": "ETHBTC",
    "USOIL": "WTI",
    "OIL": "WTI",
}

# cache: canonical -> broker name confirmed to exist
_CACHE: Dict[str, str] = {}
# cache: canonical -> False when nothing resolved (avoid rescanning)
_FAILED: Dict[str, bool] = {}


def _mt5():
    import MetaTrader5 as mt5  # imported lazily so backtests without MT5 still work
    return mt5


# ── Terminal initialization ────────────────────────────────────────────────
# Market data needs an initialized terminal, and EXECUTION MODE MUST NOT GATE
# THAT. `MT5Client.init_connection()` returns early for paper mode (it simulates
# fills, so it needs no broker link) -- and that used to starve the *data* path
# too, because nothing else ever called `mt5.initialize()`:
#
#   paper mode -> resolve_broker_symbol() -> None  (every probe fails)
#              -> copy_rates_from_pos("XAUUSD")    (canonical name, not the
#                                                   broker's "GOLD.i#")
#              -> 0 rows -> SYNTHETIC_FALLBACK
#
# The platform then read as "broker offline / no data" with a live terminal
# sitting right there. Reading bars and placing orders are separate
# capabilities: establish the first regardless of the second.
_TERMINAL_READY = False
_LAST_INIT_ATTEMPT = 0.0
_INIT_RETRY_SEC = 30.0
_init_lock = threading.Lock()
# The gate is dialled from the scan loop, so a refusal re-fires once per
# `_INIT_RETRY_SEC`. Measured on `HM_start.py live` with no terminal: 6 lines in
# 2.5 minutes (~2,900/day) of the same sentence, which is how a real error gets
# missed. Warn once per outage; the retry itself is unaffected. Cleared when the
# gate next succeeds so a *later* outage still warns.
_NO_TERMINAL_WARNED = False

# `initialize()` with no terminal answering waits for the IPC pipe for a
# DEFAULT OF 60 SECONDS, and it does so inside a native call while HOLDING THE
# GIL. Measured on a live `HM_start.py live` with the terminal down: six workers
# inside that call starved the main thread so completely that `run_web_server`
# never reached `ThreadingHTTPServer` — the process stayed up for 40+ minutes,
# started both tunnels, and never listened on :8501. A Python-side timeout
# cannot interrupt it (the guard thread cannot even be scheduled while the GIL
# is held), so the only lever is the C call's own timeout.
#
# HONEST LIMIT, measured: this bounds the ATTACH path only. When there is no
# terminal to attach to, `initialize()` tries to launch one and the argument is
# ignored — `initialize(timeout=8000)` had still not returned after 100s. That
# is why the process check in `ensure_mt5_terminal` exists; this constant is the
# second line of defence, not the first.
# Override with JARVIS_MT5_INIT_TIMEOUT_MS.
_INIT_TIMEOUT_MS = int(os.environ.get("JARVIS_MT5_INIT_TIMEOUT_MS", "10000"))

_TERMINAL_EXE_NAMES = ("terminal64.exe", "terminal.exe")


def _terminal_process_running() -> Optional[bool]:
    """Is a MetaTrader 5 terminal process running? ``None`` when we cannot tell.

    Cheap and non-blocking on purpose: it is the gate that keeps a 60-100s
    GIL-holding native call out of the process. ``None`` means "no opinion" and
    the caller proceeds as it did before this check existed, so a machine
    without ``psutil`` is no worse off.
    """
    try:
        import psutil
    except Exception:
        return None
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in _TERMINAL_EXE_NAMES:
                return True
        return False
    except Exception:
        return None


def _is_real_mt5_package(mt5) -> bool:
    """True only for the imported ``MetaTrader5`` package, not a stand-in.

    The process check below asks the OS a question about THIS machine, which is
    only meaningful when we are about to talk to the real package. A stand-in
    injected through ``sys.modules`` (the test fakes) has no ``__file__``, and
    gating it on whether a real terminal happens to be running would make every
    test's outcome depend on the developer's desktop — the same class of
    mistake as letting a stand-in latch ``_TERMINAL_READY``.
    """
    return isinstance(getattr(mt5, "__file__", None), str)


def ensure_mt5_terminal(mt5_module=None, allow_launch: bool = False) -> bool:
    """Initialise the MT5 terminal once per process so bars are readable.

    Independent of execution mode on purpose. Cached on success; a failure is
    retried at most once every :data:`_INIT_RETRY_SEC` so a 1 Hz poll loop
    cannot hammer ``initialize()`` (which attaches to the terminal and is not
    cheap). Returns True only when the terminal is genuinely up.

    `allow_launch` is the BOOT-TIME switch and defaults to False. With no
    terminal running, ``initialize()`` does not merely attach — it LAUNCHES
    ``terminal64.exe`` and waits 60-100s inside native code holding the GIL.
    That is tolerable once, at startup, before anything needs to be served; it
    is fatal on a request thread, which is why every read path leaves this
    False. `HM_start.py` is the one caller that passes True, because launching
    the terminal is exactly what starting the platform is supposed to do — it
    used to happen as a side effect of `initialize()` and gating it out
    everywhere left `HM_start.bat live` with no terminal and synthetic bars.
    ``JARVIS_MT5_ALLOW_LAUNCH=1`` forces the same behaviour process-wide.

    `mt5_module` is honoured INSTEAD of the global package. A caller that
    supplies its own terminal owns its lifecycle, and reaching past it to the
    real ``MetaTrader5`` is both wrong and — with no terminal running — fatal:
    ``initialize()`` blocks inside the native call while HOLDING THE GIL, so no
    Python-side timeout can interrupt it and not even faulthandler can dump.
    Measured: `broker_time._derive_offset` ignored its injected module this way
    and hung the whole suite for 13 minutes.

    An injected module is never latched into :data:`_TERMINAL_READY`: that flag
    is a statement about THIS process's real terminal, and letting a stand-in
    set it would make every later caller believe the terminal is up.
    """
    global _TERMINAL_READY, _LAST_INIT_ATTEMPT

    if _TERMINAL_READY:
        return True
    if os.environ.get("JARVIS_BACKTEST_MODE") == "1":
        # Backtests replay stored bars; never drag a terminal into that path.
        return False

    if mt5_module is not None:
        # Ask before attaching: `initialize()` is the expensive, blocking call,
        # and a module that is already up must not pay for it on every poll.
        try:
            if mt5_module.terminal_info() is not None:
                return True
        except Exception as exc:
            logger.debug("injected MT5 module has no readable terminal_info: %s", exc)
            return False
        try:
            return bool(mt5_module.initialize(timeout=_INIT_TIMEOUT_MS))
        except TypeError:
            # A stand-in that predates the timeout argument. Still bounded by
            # the caller's own guard; do not lose the attempt over a signature.
            try:
                return bool(mt5_module.initialize())
            except Exception as exc:
                logger.debug("injected MT5 module could not be initialized: %s", exc)
                return False
        except Exception as exc:
            logger.debug("injected MT5 module could not be initialized: %s", exc)
            return False

    with _init_lock:
        if _TERMINAL_READY:
            return True
        try:
            mt5 = _mt5()
        except Exception:
            return False

        real_package = _is_real_mt5_package(mt5)

        # The retry throttle bounds the expensive `initialize()` below. It is
        # consulted here but only CONSUMED after the cheap checks have passed,
        # because a refusal that costs nothing must not spend the window.
        now = time.time()
        if now - _LAST_INIT_ATTEMPT < _INIT_RETRY_SEC:
            return False

        # Do NOT call initialize() when there is nothing to attach to.
        #
        # With no terminal running, `initialize()` tries to LAUNCH one and then
        # waits for its IPC pipe. That wait is 60-100s, it happens inside native
        # code HOLDING THE GIL, and — measured — the `timeout=` argument does not
        # bound it: `initialize(timeout=8000)` had still not returned after 100s.
        # A Python-side guard cannot rescue it either, because no other thread
        # can be scheduled to run the guard while the GIL is held.
        #
        # Consequence on `HM_start.py live`: the process binds :8501 and then
        # never answers a single request, because the accept loop cannot get the
        # GIL. `netstat` shows a listener and `curl` times out — which reads as a
        # networking problem and is not one.
        #
        # Asking the OS whether the terminal is running costs microseconds and
        # cannot block, so it is the right gate. Set JARVIS_MT5_ALLOW_LAUNCH=1 to
        # restore the old launch-and-wait behaviour.
        #
        # NOTE the return below deliberately does NOT set `_LAST_INIT_ATTEMPT`.
        # Doing so made one "no terminal" answer refuse *every* caller for the
        # next 30s, including callers that cannot block — the suite drives this
        # path with a stand-in module. Measured as 4 order-dependent failures (3
        # in `test_position_id_join`, 1 in `test_fill_origin`) that each passed
        # in isolation. Only an actual initialize() attempt spends the window.
        launch_allowed = allow_launch or os.environ.get("JARVIS_MT5_ALLOW_LAUNCH") == "1"
        if real_package and not launch_allowed:
            if _terminal_process_running() is False:
                global _NO_TERMINAL_WARNED
                if not _NO_TERMINAL_WARNED:
                    _NO_TERMINAL_WARNED = True
                    logger.warning(
                        "No MetaTrader 5 terminal process is running. Refusing to call "
                        "initialize(): it would try to launch one and block this whole "
                        "process for 60-100s holding the GIL, which stops the web server "
                        "answering. Start the MetaTrader 5 terminal and retry. Market "
                        "data falls back to synthetic bars until then; this is logged "
                        "once per outage."
                    )
                return False

        # Committing to the call that can block: this is what the window is for.
        _LAST_INIT_ATTEMPT = now

        try:
            if not mt5.initialize(timeout=_INIT_TIMEOUT_MS):
                logger.warning("MT5 initialize() failed for market data: %s", mt5.last_error())
                return False
            if mt5.terminal_info() is None:
                return False
            _TERMINAL_READY = True
            if _NO_TERMINAL_WARNED:
                # Re-arm the warning: the next outage must be reported, not
                # silenced by the fact that we already complained once.
                _NO_TERMINAL_WARNED = False
                logger.info(
                    "MetaTrader 5 terminal is now available for market data; "
                    "the no-terminal warning is re-armed."
                )
            logger.info("MT5 terminal initialized for market data.")
            return True
        except Exception as exc:
            logger.warning("MT5 initialize() raised for market data: %s", exc)
            return False


def terminal_ready() -> bool:
    """True when the terminal has been initialized by this process.

    Read-only: does not attempt an initialization, so a health check cannot
    block on attaching to the terminal.

    Note this latches on success and never re-checks: if the terminal dies
    later this still reports True. That is deliberate — every consumer uses it
    to decide whether an EMPTY frame means "broker does not offer this symbol",
    and staying True keeps that check firing (fail closed) rather than
    suspending it. It is not a liveness probe.
    """
    return _TERMINAL_READY


def terminal_live() -> bool:
    """`terminal_ready()` AND the terminal process is still running.

    `terminal_ready()` latches on success and never re-checks, which is right
    for the job it documents. It is the WRONG primitive for "is the broker link
    up right now", and that is how all three of its consumers were using it.

    The failure mode is silent and total. Once the latch is True, a terminal
    that dies mid-session leaves it True, so every frame that falls back to
    synthetic bars is read as evidence that the broker does not offer that
    symbol. `run_cycle_for_symbol` then refuses to decide for every symbol, the
    radar empties, and nothing is ever traded -- while the only log line blames
    the symbol ("this broker does not appear to offer X"), which is the opposite
    of the truth.

    Measured in the suite: with `_TERMINAL_READY` latched by an earlier test,
    `test_multi_style_radar` goes from 6 opportunities to 0 (`0 != 6`) with
    exactly that warning, while the same test passes in isolation. Two more
    tests fail the same way. That is the same defect an operator hits after
    restarting the terminal mid-session.

    Costs one psutil process scan -- microseconds, cannot block, and it is the
    same check the init gate already relies on. An unanswerable check (no
    psutil) falls back to the latch rather than inventing a dead terminal.
    """
    if not _TERMINAL_READY:
        return False
    running = _terminal_process_running()
    return True if running is None else running


def reset_cache() -> None:
    """Forget every cached resolution and the terminal state.

    Mirrors `broker_time.reset_cache()`. Needed after a terminal restart or a
    server/account change, since both caches are otherwise process-lifetime:
    a symbol resolved against one broker stays resolved after switching to
    another, and `_TERMINAL_READY` would suppress re-initialization.
    """
    global _TERMINAL_READY, _LAST_INIT_ATTEMPT, _NO_TERMINAL_WARNED
    _CACHE.clear()
    _FAILED.clear()
    _TERMINAL_READY = False
    _LAST_INIT_ATTEMPT = 0.0
    _NO_TERMINAL_WARNED = False


def probe_symbol(name: str, mt5_module=None, require_tradable: bool = False) -> bool:
    """True if ``name`` exists at the broker and returns at least one H1 bar.

    `mt5_module` is used instead of the global package when supplied, so a
    caller driving this offline cannot end up attaching to the real terminal
    behind its own back.
    """
    try:
        mt5 = mt5_module or _mt5()
        info = mt5.symbol_info(name)
        if info is None:
            return False
        if require_tradable and getattr(info, "trade_mode", 4) == 0:
            return False
        mt5.symbol_select(name, True)
        rates = mt5.copy_rates_from_pos(name, mt5.TIMEFRAME_H1, 0, 5)
        return rates is not None and len(rates) > 0
    except Exception:
        return False


def resolve_broker_symbol(symbol: str, verbose: bool = False, mt5_module=None) -> Optional[str]:
    """Return the broker's name for a canonical symbol, or None if unresolvable.

    None means "this instrument is not available from this broker" - callers
    should skip the symbol, not crash and not silently trade the wrong series.

    `mt5_module` is threaded through to the terminal ensure and to every probe
    rather than stopping at this function. It has to be: `broker_time` derives
    the broker clock offset from a caller-supplied module, and resolving
    "XAUUSD" -> "GOLD.i#" then reaching for the global package anyway meant an
    offline caller still called `MetaTrader5.initialize()` — which, with no
    terminal installed, blocks forever holding the GIL.
    """
    sym = str(symbol or "").upper()
    if not sym:
        return None
    if sym in _CACHE:
        return _CACHE[sym]
    if _FAILED.get(sym):
        return None

    # An uninitialized terminal makes every probe fail, and a failed probe is
    # indistinguishable from "this broker has no such symbol". Ask for the
    # terminal FIRST, and when it is not up report UNKNOWN *without* writing the
    # failure cache -- otherwise one call before the terminal is ready pins every
    # symbol to None for the life of the process, long after the terminal came up.
    if not ensure_mt5_terminal(mt5_module=mt5_module):
        return None

    canonical_key = CANONICAL_SYNONYMS.get(sym, sym)
    candidates = [sym] + list(BROKER_ALIASES.get(sym, []))
    if canonical_key != sym:
        for c in BROKER_ALIASES.get(canonical_key, []):
            if c not in candidates:
                candidates.append(c)

    # Pass 1: find candidate with active trading enabled (trade_mode > 0)
    for cand in candidates:
        if probe_symbol(cand, mt5_module=mt5_module, require_tradable=True):
            _CACHE[sym] = cand
            if verbose and cand != sym:
                print(f"  [broker-symbol] {sym} -> {cand}")
            return cand

    # Pass 2: fallback if trade_mode is not enforced (e.g. test mock or weekend)
    for cand in candidates:
        if probe_symbol(cand, mt5_module=mt5_module, require_tradable=False):
            _CACHE[sym] = cand
            if verbose and cand != sym:
                print(f"  [broker-symbol] {sym} -> {cand}")
            return cand

    # Nothing configured worked: try a fuzzy scan of the broker's own list.
    # PREFIX ONLY, and logged loudly. A bare substring scan resolved COPPER to
    # `SouthernCopper` -- Southern Copper Corp, an EQUITY -- and handed it back
    # as the copper commodity. Nothing downstream could tell: the frame was
    # stamped LIVE_MT5 and the freshness gate certified it FRESH, so the
    # platform would have analysed (and could have traded) the wrong instrument
    # with a clean bill of health. A wrong instrument that looks healthy is far
    # worse than an unresolved symbol, which callers already handle.
    try:
        mt5 = mt5_module or _mt5()
        for info in (mt5.symbols_get() or []):
            n = str(info.name)
            if len(sym) >= 3 and n.upper().startswith(sym) and probe_symbol(n, mt5_module=mt5_module):
                _CACHE[sym] = n
                logger.warning(
                    "Fuzzy broker-symbol match %s -> %s (not in BROKER_ALIASES). "
                    "Confirm this is the intended instrument; if it is, add it to "
                    "BROKER_ALIASES so the match is explicit.",
                    sym, n,
                )
                if verbose:
                    print(f"  [broker-symbol] {sym} -> {n} (discovered)")
                return n
    except Exception as e:
        logger.warning("Fuzzy broker-symbol scan failed for %s: %s", sym, e)

    _FAILED[sym] = True
    if verbose:
        print(f"  [broker-symbol] {sym} -> NOT AVAILABLE at this broker")
    return None
