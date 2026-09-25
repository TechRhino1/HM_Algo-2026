"""
HM Algo 2.0 — High-Performance Telemetry, Trading & Web Terminal Server.
Provides REST, JSON streaming, manual trading execution, position management, news feed, and static assets.
"""
import os
import json
import hashlib
import logging
import mimetypes
import math
import socketserver
import sqlite3
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from typing import Any, Optional, Dict, Tuple, ClassVar

from jarvis.application.state_manager import StateManager, GLOBAL_STATE
from jarvis.application.radar_sort import radar_sort_key
import threading
import time
from jarvis.market.data_feed import DataFeedEngine, first_untrusted_frame, first_unusable_frame
from jarvis.data.broker_symbols import terminal_live
from jarvis.api.copilot import JarvisCopilot
from jarvis.execution.mt5_client import MT5Client
from jarvis.data.schemas import ExecutionMode
from jarvis.data.symbol_registry import resolve as resolve_symbol
from jarvis.market.sessions import SessionEngine
from jarvis.api.remote_auth import RemoteAuthEngine
from jarvis.config.settings import SETTINGS
from jarvis.common.bounded_cache import BoundedTTLCache
from jarvis.observability import bind, new_request_id, unbind
from jarvis.observability.instruments import HTTP_LATENCY, HTTP_REQUESTS
from jarvis.observability.metrics import REGISTRY

logger = logging.getLogger("JARVIS_WebServer")


def _status_class(status_code: int) -> str:
    """`2xx` / `4xx` / `5xx`. A status code is not a usable metric label: one
    per code would be unbounded and unreadable, and the interesting question is
    almost always "did this fail, and was it our fault"."""
    try:
        return f"{int(status_code) // 100}xx"
    except (TypeError, ValueError):
        return "unk"


def _bound_route() -> str:
    """The route bound by `do_GET`/`do_POST`, or `unbound` off the web path."""
    from jarvis.observability.context import ROUTE, bound

    return str(bound().get(ROUTE) or "unbound")


class JarvisRequestHandler(BaseHTTPRequestHandler):
    state_manager: StateManager = GLOBAL_STATE
    # P5: `auto_init=False`. This line runs at IMPORT time, and an eager
    # `mt5.initialize()` here blocks in a native call holding the GIL when no
    # terminal is running — so `import jarvis.api.server` never returns and even
    # `pytest --collect-only` hangs forever. Connection now happens on first use
    # via `MT5Client._reconnect_if_needed()`.
    mt5_client: MT5Client = MT5Client(mode="live", auto_init=False)
    data_feed: DataFeedEngine = DataFeedEngine(mt5_client=mt5_client)
    # The copilot answers questions about working orders, which only the broker
    # knows, so it is handed the same client rather than opening its own.
    copilot: JarvisCopilot = JarvisCopilot(GLOBAL_STATE, mt5_client=mt5_client)
    # The live orchestrator, attached by whichever entry point owns it.
    #
    # This was the root of a real bug: the handler only ever received the broker
    # client, so the trade-style selector updated a global that the running
    # orchestrator never read — it silently changed nothing. ``None`` is a valid
    # state (API-only mode) and every consumer checks for it.
    orchestrator: Optional[Any] = None
    _bg_thread_started: bool = False
    _bg_lock = threading.Lock()
    # Bounded. The key is `f"{sym}_{tf}"` and `sym` is read straight off the
    # query string, so the old plain dict grew one entry per distinct symbol a
    # caller asked for and never removed one. 256 entries covers every
    # symbol/timeframe pair the UI can render at once, many times over.
    _CANDLES_CACHE: ClassVar[BoundedTTLCache] = BoundedTTLCache(
        max_entries=256, ttl_sec=1.0, name="server.candles"
    )
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root_dir = os.path.dirname(base_dir)

    @classmethod
    def configure_broker(cls, mt5_client: MT5Client):
        """Use the application's single broker client for API and orchestration work."""
        cls.mt5_client = mt5_client
        cls.data_feed = DataFeedEngine(mt5_client=mt5_client)

    @classmethod
    def configure_orchestrator(cls, orchestrator: Any):
        """Attach the running orchestrator so the API can drive the real engine.

        Propagates to the intelligence router as well, because auto-selection
        must call the same ``scan_all_modes`` the live loop uses — otherwise the
        preview would describe a different engine than the one trading.
        """
        cls.orchestrator = orchestrator
        try:
            from jarvis.api.intelligence_api import INTELLIGENCE
            INTELLIGENCE.configure_orchestrator(orchestrator)
        except Exception as exc:
            logger.warning(f"Could not attach orchestrator to the intelligence API: {exc}")

    def _extract_token(self) -> str:
        auth_header = self.headers.get("Authorization", "")
        cookie_header = self.headers.get("Cookie", "")
        token = ""

        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif "jarvis_auth_token=" in cookie_header:
            try:
                for c in cookie_header.split(";"):
                    c = c.strip()
                    if c.startswith("jarvis_auth_token="):
                        token = c.split("=", 1)[1].strip()
                        break
            except Exception:
                pass

        return token

    @staticmethod
    def _allowed_cors_origin(request_origin: str = "") -> str:
        configured = os.environ.get("JARVIS_CORS_ORIGIN", SETTINGS.server.cors_origin).strip()
        return configured if configured and request_origin == configured else ""

    def _session_cookie(self, token: str, max_age: int) -> str:
        attrs = [f"jarvis_auth_token={token}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Strict"]
        if os.environ.get("JARVIS_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}:
            attrs.append("Secure")
        return "; ".join(attrs)

    def _is_local_request(self) -> bool:
        """Check if request originates locally on the host machine without reverse proxying."""
        client_ip = self.client_address[0] if hasattr(self, "client_address") and self.client_address else ""
        if client_ip in ("127.0.0.1", "::1", "localhost"):
            forwarded = self.headers.get("X-Forwarded-For") or self.headers.get("X-Forwarded-Host")
            if not forwarded:
                host = self.headers.get("Host", "").split(":")[0]
                if host in ("127.0.0.1", "localhost", "::1"):
                    return True
        return False

    def _get_auth_user(self) -> Optional[Dict[str, Any]]:
        token = self._extract_token()
        if token:
            return RemoteAuthEngine.validate_token(token)
        if self._is_local_request():
            return {"username": "admin", "role": "ADMIN", "full_name": "Local Administrator"}
        return None

    def _copilot_session_id(self) -> Optional[str]:
        """A stable key for this caller's conversation thread.

        The token when there is one, so two browsers signed into the same
        account keep separate threads; otherwise the client address for a local
        request. Returns ``None`` when neither exists, which leaves the call
        stateless — exactly the behaviour before memory existed.

        The token is hashed rather than used directly. The store is in memory
        and never logs its keys, but a session id is the kind of string that
        ends up in a debug print, and a credential must not be.
        """
        token = self._extract_token()
        if token:
            return "tok:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        if self._is_local_request():
            addr = getattr(self, "client_address", None)
            ip = addr[0] if addr else "local"
            return f"local:{ip}"
        return None

    def _check_auth(self) -> bool:
        return self._get_auth_user() is not None

    def _require_role(self, *allowed_roles: str):
        user = self._get_auth_user()
        if not user:
            self._send_json({"status": "UNAUTHORIZED", "error": "Authentication required"}, status_code=401)
            return False, None
        if user.get("role") not in allowed_roles:
            self._send_json({"status": "FORBIDDEN", "error": f"Role '{user.get('role')}' is not permitted to perform this action"}, status_code=403)
            return False, None
        return True, user


    @classmethod
    def start_background_syncer(cls):
        with cls._bg_lock:
            if cls._bg_thread_started:
                return
            cls._bg_thread_started = True

        def _bg_loop():
            from jarvis.market.market_context import MarketContextEngine
            from jarvis.intelligence.regime_engine import MarketRegimeClassifier
            from jarvis.analysts.parallel_runner import ParallelAnalystCluster
            from jarvis.intelligence.decision_engine import DecisionEngine

            # Third hardcoded universe. `trading.allowed_symbols` is the single
            # source of truth; this fallback matches it so the telemetry sweep
            # and the trading engine cannot disagree about what we cover.
            try:
                symbols = [str(s).strip().upper()
                           for s in (SETTINGS.trading.symbols or []) if str(s).strip()]
            except Exception:
                symbols = []
            if not symbols:
                symbols = ["XAUUSD", "EURUSD", "GBPUSD", "BTCUSD", "USDJPY"]
            ce = MarketContextEngine()
            rc = MarketRegimeClassifier()
            ac = ParallelAnalystCluster(parallel=False)
            de = DecisionEngine()

            while True:
                try:
                    # 1. Sync Account & Positions
                    acc = cls.mt5_client.get_account_snapshot()
                    pos = cls.mt5_client.get_open_positions()
                    cls.state_manager.sync_broker_state(acc, pos)

                    # If the main Orchestrator is actively running and radar is populated, let it drive
                    if cls.state_manager.is_orchestrator_active() and cls.state_manager.radar_opportunities:
                        time.sleep(2.0)
                        continue

                    # 2. Standalone Fallback: Sweep Multi-Asset Radar across all 3 active styles
                    radar_results = []
                    account = cls.state_manager.account or acc
                    active_styles = ["SWING", "DAY_TRADING", "SCALP"]

                    for t_style in active_styles:
                        for sym in symbols:
                            try:
                                mtf = cls.data_feed.fetch_multi_timeframe(sym, trade_style=t_style)

                                # A frame that claims to be live but cannot be
                                # shown to be current must not become a signal.
                                # This path builds its decision directly rather
                                # than going through the orchestrator's cycle,
                                # so the guard has to be applied here too -
                                # enforcing it in one entry point and not the
                                # other is how the same defect comes back. Both
                                # STALE and unverifiable-age-on-a-live-frame
                                # block; see first_untrusted_frame.
                                _untrusted_role, _untrusted_reason, _untrusted_age = \
                                    first_untrusted_frame(mtf)
                                if _untrusted_role:
                                    if _untrusted_reason == "stale":
                                        logger.warning(
                                            "Skipping %s (%s) in auto-selection: %s frame is "
                                            "STALE (%.0fs old).",
                                            sym, t_style, _untrusted_role, _untrusted_age,
                                        )
                                    else:
                                        logger.warning(
                                            "Skipping %s (%s) in auto-selection: %s frame is "
                                            "LIVE_MT5 but its age cannot be verified.",
                                            sym, t_style, _untrusted_role,
                                        )
                                    continue

                                # Fabricated bars must not be selected on, for the
                                # same reason and with the same
                                # only-when-the-broker-is-reachable scope as the
                                # orchestrator's gate.
                                if terminal_live():
                                    _bad_role, _bad_source = first_unusable_frame(mtf)
                                    if _bad_role:
                                        logger.warning(
                                            "Skipping %s (%s) in auto-selection: %s frame "
                                            "is %s, not real market data.",
                                            sym, t_style, _bad_role, _bad_source,
                                        )
                                        continue

                                spec = resolve_symbol(sym)
                                ctx = ce.build_context(sym, mtf, current_spread_pips=spec.typical_spread_pips, max_allowed_spread_pips=spec.max_spread_pips, trade_style=t_style)
                                cls.state_manager.update_market_context(sym, ctx)
                                regime = rc.classify_regime(ctx)
                                tentative_bias = "BUY" if ctx.structure.bias == "BULLISH" else ("SELL" if ctx.structure.bias == "BEARISH" else ("SELL" if getattr(ctx.momentum, "trend_score", 0.0) < 0 else "BUY"))
                                reports, devil = ac.run_all_parallel(ctx, regime, tentative_bias)
                                d = de.evaluate(ctx, regime, reports, devil, account_balance=account.equity if account else 10000.0, mtf_data=mtf, trade_style=t_style)
                                
                                active_state_style = getattr(cls.state_manager, "trade_style", "SWING")
                                if t_style.upper() == active_state_style.upper() or (t_style == "DAY_TRADING" and active_state_style in ("DAY", "INTRADAY")):
                                    cls.state_manager.record_decision(sym, d)

                                mkt_status = SessionEngine.get_market_trading_status(sym)
                                is_mkt_open = mkt_status.get("is_open", True)

                                win_p = d.probabilities.get(d.bias.lower(), d.model_confidence) if (d.probabilities and d.bias in ["BUY", "SELL"]) else d.model_confidence
                                if not is_mkt_open:
                                    status_label = "MARKET CLOSED"
                                elif d.decision == "EXECUTE":
                                    status_label = f"{d.bias} READY"
                                elif d.decision == "WAIT" and d.bias in ["BUY", "SELL"]:
                                    status_label = f"WAIT: {d.bias}"
                                elif d.decision == "NO_TRADE":
                                    if d.quality_gate and not d.quality_gate.passed and any("Invalid" in r or "Devil" in r or "Adversarial" in r for r in d.quality_gate.failing_reasons):
                                        status_label = f"INVALID: {d.bias}" if d.bias in ["BUY", "SELL"] else "TRADE INVALIDATED"
                                    elif d.bias in ["BUY", "SELL"]:
                                        status_label = f"NO TRADE: {d.bias}"
                                    else:
                                        status_label = "NO SETUP"
                                elif d.bias in ["BUY", "SELL"]:
                                    status_label = f"WAIT: {d.bias}"
                                else:
                                    status_label = "NO SETUP"

                                tf_str = "D1/H4/H1" if t_style == "SWING" else ("H1/M15/M5" if t_style in ("DAY_TRADING", "INTRADAY", "DAY") else "M15/M5/M1")

                                curr_price = d.entry_price
                                if ctx and hasattr(ctx, "current_price") and ctx.current_price is not None:
                                    try:
                                        curr_price = round(float(ctx.current_price), getattr(spec, "digits", 2 if "XAU" in sym or "BTC" in sym else 5))
                                    except Exception:
                                        curr_price = d.entry_price

                                radar_results.append({
                                    "symbol": sym,
                                    "trade_style": t_style,
                                    "timeframe": tf_str,
                                    "current_price": curr_price,
                                    "entry_price": d.entry_price,
                                    "stop_loss": d.stop_loss,
                                    "take_profit": d.take_profit,
                                    "risk_reward_ratio": round(d.risk_reward_ratio, 2) if d.risk_reward_ratio else 2.50,
                                    "ev": round(d.expected_value, 2) if d.expected_value else 0.0,
                                    "bias": d.bias,
                                    "action": status_label,
                                    "status_label": status_label,
                                    "decision": d.decision,
                                    "score": round(win_p * 100.0, 0),
                                    "win_prob": round(win_p * 100.0, 0),
                                    "confluence_score": getattr(d, "master_confluence_score", 0.0),
                                    "confluence_tier": getattr(d, "master_confluence_tier", "MODERATE"),
                                    "regime": d.regime.primary_regime.value if (d.regime and hasattr(d.regime, "primary_regime")) else "UNKNOWN",
                                    "strategy": d.strategy or "STRUCTURE",
                                    "gate_passed": d.quality_gate.passed if d.quality_gate else False,
                                    "failing_reasons": d.quality_gate.failing_reasons if d.quality_gate else [],
                                    "checks": d.quality_gate.checks if d.quality_gate else {},
                                    "not_evaluated": list(d.quality_gate.not_evaluated) if d.quality_gate else [],
                                    "waiting_reasons": getattr(d, "waiting_reasons", []),
                                    "rejection_reasons": getattr(d, "rejection_reasons", []),
                                    "risk_factors": d.risk_factors or [],
                                    "adversarial_penalty": round(d.adversarial_penalty, 1) if d.adversarial_penalty else 0.0,
                                    "invalidation_levels": d.invalidation_levels or [],
                                    "mtf_alignment": getattr(ctx, "mtf_alignment", {}),
                                    "mtf_confluence": getattr(ctx, "mtf_confluence_score", 0.0),
                                })
                            except Exception as e_sym:
                                logger.error(f"Radar sweep error for {sym} ({t_style}): {e_sym}", exc_info=True)

                    if radar_results:
                        # A1: one canonical radar sort key, shared with
                        # orchestrator.py — see jarvis/application/radar_sort.py.
                        radar_results.sort(key=radar_sort_key, reverse=True)
                        cls.state_manager.update_radar(radar_results)

                except Exception as e:
                    logger.error(f"Background telemetry sync error: {e}", exc_info=True)

                time.sleep(3.0)

        t = threading.Thread(target=_bg_loop, daemon=True, name="web_bg_telemetry_syncer")
        t.start()

    def log_message(self, format, *args):
        logger.debug(f"{self.address_string()} - {format % args}")

    @staticmethod
    def _normalize_route(path: str) -> str:
        """Collapse a request path into a bounded set of metric label values.

        The path arrives from the client and is therefore unvalidated input.
        Using it directly as a metric label lets any caller mint a new time
        series per request, which is a memory leak with a metrics-shaped hat.
        Non-API paths collapse to a coarse bucket; an over-long or non-ASCII
        path collapses to `other`.
        """
        if not isinstance(path, str) or not path:
            return "empty"
        if not path.startswith("/api/") or len(path) > 64:
            return "page" if path in ("/", "/index.html") else "other"
        try:
            path.encode("ascii")
        except UnicodeEncodeError:
            return "other"
        # One dynamic id in the middle of a path is still unbounded, so keep the
        # prefix (the route) and drop the tail (the parameter).
        head = path.split("?")[0]
        return head if head.count("/") <= 4 else "/".join(head.split("/")[:5]) + "/*"

    def do_GET(self):
        JarvisRequestHandler.start_background_syncer()
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # One correlation id per request. Bound here rather than in a wrapper so
        # every log line this request causes — including ones emitted deep in the
        # market or execution layer — carries the same id and can be recovered
        # with a single grep.
        _route = self._normalize_route(path)
        _request_timer = HTTP_LATENCY.time(route=_route)
        _bind_token = bind(request_id=new_request_id(), route=_route)

        try:
            public_get_endpoints = {
                "/", "/index.html", "/dashboard", "/dashboard.html",
                "/console", "/console.html",
                "/classic", "/classic.html",
                "/stocks", "/stocks.html", "/screener",
                "/india", "/india.html", "/india/stocks", "/nse", "/bse",
                "/options", "/options.html", "/india/options", "/india-options", "/fno",
                "/positions", "/positions.html",
                "/api/telemetry_state", "/api/telemetry", "/api/candles", "/api/rates",
                "/api/radar", "/api/market-status", "/api/news", "/api/history",
                "/api/tunnel_info", "/api/pending_orders", "/api/risk_status",
                "/api/stream/telemetry", "/api/auth/me", "/api/auth/verify",
                # Probes: no session, and no reliance on the loopback bypass.
                "/health", "/ready"
            }
            # NOTE: /api/diagnostics is deliberately NOT public. It returns the
            # account snapshot, so it now requires a session — the loopback
            # bypass still serves local tooling, but a remote caller cannot read
            # balances unauthenticated.
            # NOTE: /api/intelligence/* and /api/backtest/* are deliberately NOT
            # listed here. Page shells must load before auth, but the intelligence
            # surface exposes trading intent and can start CPU-heavy jobs, so it
            # stays behind authentication.
            is_public_get = (
                path in public_get_endpoints
                or path.startswith("/static/")
                or path.startswith("/api/historical/")
                or path.startswith("/api/stocks/")
                or path.startswith("/api/india/")
            )

            if path.startswith("/api/") and not is_public_get and not self._check_auth():
                self._send_json({"status": "UNAUTHORIZED", "error": "Authentication required"}, status_code=401)
                return

            if path in ("/health", "/ready"):
                self._send_health()
            elif path in ("/", "/index.html", "/dashboard", "/dashboard.html"):
                self._serve_dashboard_ui()
            elif path in ("/console", "/console.html"):
                self._serve_console_ui()
            elif path in ("/classic", "/classic.html"):
                self._serve_terminal_ui()
            elif path in ["/stocks", "/stocks.html", "/screener"]:
                self._serve_stocks_ui()
            elif path in ["/india", "/india.html", "/india/stocks", "/nse", "/bse"]:
                self._serve_india_ui()
            elif path in ["/options", "/options.html", "/india/options", "/india-options", "/fno"]:
                self._serve_options_ui()
            elif path in ["/positions", "/positions.html"]:
                self._serve_positions_ui()
            elif path.startswith("/static/"):
                self._serve_static_file(path)
            elif path.startswith("/api/stocks/"):
                from jarvis.stocks.stock_service import STOCK_SERVICE
                if not STOCK_SERVICE.handle_request(path, query, self):
                    self.send_error(404, f"Stock API {path} not found")
            elif path.startswith("/api/india/"):
                from jarvis.india.india_service import INDIA_SERVICE
                if not INDIA_SERVICE.handle_request(path, query, self):
                    self.send_error(404, f"India API {path} not found")
            elif path.startswith("/api/intelligence/") or path.startswith("/api/backtest/"):
                from jarvis.api.intelligence_api import INTELLIGENCE
                if not INTELLIGENCE.handle_get(path, query, self):
                    self.send_error(404, f"Intelligence API {path} not found")
            elif path in ("/api/telemetry_state", "/api/telemetry"):
                snap = self.state_manager.get_state_snapshot()
                acc_dict = snap.get("account")
                if not acc_dict or acc_dict.get("balance", 0) == 0:
                    # Only refresh from the broker when a connection is ALREADY
                    # held. `get_account_snapshot()` connects on first use, and
                    # `init_connection` retries with 1+2+4+8+16s backoff — so
                    # calling it here parked a request thread for ~31s whenever
                    # the terminal was down. Measured on the live server:
                    # `/api/telemetry_state` timed out at 15s while every other
                    # endpoint answered in ~0.15s, which reads as "this endpoint
                    # is broken" rather than "a read is paying for a connect".
                    # Connecting is the background synchroniser's job; a read
                    # reports what is known, and reports nothing as nothing.
                    if getattr(self.mt5_client, "is_connected", False):
                        acc = self.mt5_client.get_account_snapshot()
                        pos = self.mt5_client.get_open_positions()
                        if acc and acc.login > 0:
                            self.state_manager.sync_broker_state(acc, pos)
                        snap = self.state_manager.get_state_snapshot()
                
                from jarvis.market.sessions import SessionEngine
                sym = query.get("symbol", ["XAUUSD"])[0]
                snap["active_market_status"] = SessionEngine.get_market_trading_status(sym)
                snap["market_statuses"] = {
                    s: SessionEngine.get_market_trading_status(s)
                    for s in ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD", "ETHUSD"]
                }
                self._send_json(snap)
            elif path == "/api/market-status":
                from jarvis.market.sessions import SessionEngine
                sym = query.get("symbol", ["XAUUSD"])[0]
                status = SessionEngine.get_market_trading_status(sym)
                self._send_json(status)
            elif path == "/api/historical/manifest":
                from jarvis.historical.historical_engine import HISTORICAL_DATA_ENGINE
                datasets = HISTORICAL_DATA_ENGINE.list_datasets()
                stats = HISTORICAL_DATA_ENGINE.get_engine_stats()
                self._send_json({"datasets": datasets, "stats": stats})
            elif path == "/api/historical/status":
                from jarvis.historical.historical_engine import HISTORICAL_DATA_ENGINE
                self._send_json(HISTORICAL_DATA_ENGINE.get_engine_stats())
            elif path == "/api/historical/data":
                from jarvis.historical.historical_engine import HISTORICAL_DATA_ENGINE
                sym = query.get("symbol", ["XAUUSD"])[0]
                tf = query.get("timeframe", query.get("tf", ["H1"]))[0]
                bars = max(1, min(5000, int(query.get("num_bars", query.get("bars", [200]))[0])))
                with_reg = query.get("with_regimes", ["false"])[0].lower() in ("true", "1")
                df = HISTORICAL_DATA_ENGINE.get_market_data(sym, tf, num_bars=bars, with_regimes=with_reg)
                records = df.to_dict(orient="records") if not df.empty else []
                for r in records:
                    if "time" in r:
                        r["time"] = str(r["time"])
                self._send_json({"symbol": sym, "timeframe": tf, "count": len(records), "data": records})
            elif path == "/api/tunnel_info":
                tunnel_url = ""
                provider = "None"
                status = "DISCONNECTED"
                tunnel_file = os.path.join(self.root_dir, "active_tunnel_url.txt")
                if os.path.exists(tunnel_file):
                    try:
                        with open(tunnel_file, "r", encoding="utf-8") as f:
                            tunnel_url = f.read().strip()
                        if tunnel_url.startswith("http"):
                            status = "CONNECTED"
                            if "trycloudflare" in tunnel_url:
                                provider = "Cloudflare Tunnel"
                            elif "lhr.life" in tunnel_url:
                                provider = "localhost.run"
                            elif "pinggy" in tunnel_url:
                                provider = "pinggy.io"
                            elif "serveo" in tunnel_url:
                                provider = "serveo.net"
                    except Exception as e:
                        logger.warning(f"Could not read tunnel URL file {tunnel_file}: {e}")
                from HM_start import get_local_wifi_ip
                local_ip = get_local_wifi_ip()
                self._send_json({
                    "url": tunnel_url,
                    "status": status,
                    "provider": provider,
                    "local_ip": local_ip,
                    "port": 8501
                })
            elif path == "/api/candles":
                sym = query.get("symbol", ["XAUUSD"])[0]
                # Accept `timeframe` as well as `tf`, matching /api/rates and
                # /api/historical/data below. A caller that sends the longer name
                # used to get a silent H1 fallback rather than an error.
                tf = query.get("tf", query.get("timeframe", ["H1"]))[0]
                cache_key = f"{sym}_{tf}"
                now = time.time()
                cached = self._CANDLES_CACHE.get(cache_key)
                if cached and (now - cached[1] < 1.0):
                    self._send_json(cached[0])
                    return

                df = self.data_feed.fetch_rates(sym, timeframe=tf, num_bars=150, include_current_bar=True)
                spec = resolve_symbol(sym)
                digits = getattr(spec, "digits", 2 if "XAU" in sym or "BTC" in sym else 5)
                candles = []
                if df is not None and not df.empty:
                    records = df.to_dict(orient="records")
                    for r in records:
                        t_val = r["time"]
                        if hasattr(t_val, "tzinfo") and t_val.tzinfo is None:
                            t_val = t_val.tz_localize("UTC")
                        ts = int(t_val.timestamp()) if hasattr(t_val, "timestamp") else int(t_val)
                        candles.append({
                            "time": ts,
                            "open": round(float(r["open"]), digits),
                            "high": round(float(r["high"]), digits),
                            "low": round(float(r["low"]), digits),
                            "close": round(float(r["close"]), digits),
                            "volume": float(r.get("volume", 0.0))
                        })
                payload = {"symbol": sym, "timeframe": tf, "candles": candles}
                self._CANDLES_CACHE[cache_key] = (payload, now)
                self._send_json(payload)
            elif path == "/api/rates":
                sym = query.get("symbol", ["XAUUSD"])[0]
                tf = query.get("tf", query.get("timeframe", ["H1"]))[0]
                trade_style = query.get("trade_style", [None])[0]
                bars = max(1, min(5000, int(query.get("num_bars", query.get("bars", [150]))[0])))
                if trade_style:
                    mtf = self.data_feed.fetch_multi_timeframe(sym, trade_style=trade_style, num_bars=bars)
                    res = {}
                    for role, df in mtf.items():
                        res[role] = df.tail(bars).to_dict(orient="records")
                    self._send_json({"symbol": sym, "trade_style": trade_style, "rates": res})
                else:
                    df = self.data_feed.fetch_rates(sym, timeframe=tf, num_bars=bars, include_current_bar=True)
                    self._send_json({"symbol": sym, "timeframe": tf, "rates": df.to_dict(orient="records")})
            elif path == "/api/radar":
                style_filter = query.get("trade_style", query.get("style", [None]))[0]
                opps = list(self.state_manager.radar_opportunities)

                # A1: one canonical radar sort key, shared with orchestrator.py —
                # see jarvis/application/radar_sort.py. (Used to be a drifted copy
                # here, the only one that also fell back to `status_label`.)
                if style_filter and style_filter.strip().upper() not in ("ALL", "", "NONE"):
                    s_norm = style_filter.strip().upper()
                    if s_norm in ("DAY", "DAY_TRADING", "INTRADAY"):
                        target_styles = {"DAY_TRADING", "DAY", "INTRADAY"}
                    elif s_norm in ("SCALP", "SCALPING"):
                        target_styles = {"SCALP", "SCALPING"}
                    elif s_norm == "SWING":
                        target_styles = {"SWING"}
                    else:
                        target_styles = {s_norm}
                    opps = [o for o in opps if str(o.get("trade_style", "")).upper() in target_styles]

                opps.sort(key=radar_sort_key, reverse=True)
                self._send_json({"opportunities": opps})
            elif path == "/api/history":
                try:
                    from jarvis.data.database import TRADE_DB

                    # The route answers with a bare array — both existing
                    # consumers (the classic terminal and the console) read it
                    # that way — so query params narrow the result rather than
                    # wrapping it. Callers already send `limit` and `symbol`;
                    # until now both were ignored and the cap was hardcoded.
                    try:
                        limit = int((query.get("limit") or ["200"])[0])
                    except (TypeError, ValueError):
                        limit = 200
                    limit = max(1, min(limit, 2000))

                    try:
                        days = int((query.get("days") or ["60"])[0])
                    except (TypeError, ValueError):
                        days = 60
                    days = max(1, min(days, 3650))

                    # `days` has to reach the journal query as well as the MT5
                    # fetch below. It used to reach only MT5, so the Window
                    # filter left every journal row unfiltered — a "1 day"
                    # window answered with a month of trades.
                    # D1: `origin` is how a caller asks for real money only.
                    # Absent, it changes nothing; present but unrecognised, it
                    # selects nothing rather than quietly widening to
                    # everything — a filter that matches everything is
                    # indistinguishable from no filter, and far more dangerous
                    # because the caller believes it filtered.
                    raw_origin = str((query.get("origin") or [""])[0]).strip()
                    origin_filter = [p.strip() for p in raw_origin.split(",") if p.strip()] or None

                    trades = TRADE_DB.fetch_recent_trades(
                        limit=limit, days=days, origin=origin_filter) or []

                    # Also fetch live closed deals from MT5 broker account
                    # Deals merged in from MT5 are broker rows, so they only
                    # belong in an answer that is allowed to contain broker
                    # rows. Appending them under `origin=paper` would hand back
                    # real money inside a result the caller asked to be
                    # simulated — the exact mix D1 exists to prevent.
                    if (hasattr(self, "mt5_client") and self.mt5_client
                            and getattr(self.mt5_client, "is_connected", False)
                            and (origin_filter is None or "broker" in origin_filter)):
                        import MetaTrader5 as _mt5
                        mt5_deals = _mt5.history_deals_get(datetime.now() - timedelta(days=days), datetime.now())
                        if mt5_deals:
                            existing_tickets = {int(t.get("ticket", 0)) for t in trades if t.get("ticket")}
                            for d in reversed(list(mt5_deals)):
                                if getattr(d, "entry", 0) == 1 and getattr(d, "symbol", ""):
                                    if int(d.ticket) not in existing_tickets:
                                        sym_clean = str(d.symbol).replace("#", "").replace(".m", "").replace("m", "")
                                        comm = str(getattr(d, "comment", "") or "")
                                        if "[sl" in comm.lower():
                                            exec_tag = "SL EXIT"
                                        elif "[tp" in comm.lower():
                                            exec_tag = "TP EXIT"
                                        elif "jarvis" in comm.lower():
                                            exec_tag = "BOT (AI)"
                                        else:
                                            exec_tag = "MT5 BROKER"
                                            
                                        # In MT5: entry 1 with type 1 (SELL) closed a BUY position; type 0 (BUY) closed a SELL position
                                        orig_action = "BUY" if d.type == 1 else ("SELL" if d.type == 0 else "CLOSE")
                                        trades.append({
                                            "id": int(d.ticket),
                                            "ticket": int(d.ticket),
                                            "symbol": sym_clean,
                                            "action": orig_action,
                                            "type": orig_action,
                                            "entry_price": float(d.price),
                                            "volume": float(d.volume),
                                            # The merge only keeps out-deals
                                            # (entry == 1), so this moment
                                            # really is the close — unlike a
                                            # journal row, whose `timestamp` is
                                            # the time it was logged.
                                            "timestamp": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                                            "closed_at": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                                            "executor": exec_tag,
                                            # D1: these rows are broker deals
                                            # merged in from MT5, so their
                                            # origin is `broker` by definition.
                                            # Without the key a client filtering
                                            # on `origin` matches nothing and
                                            # renders the empty state on
                                            # success — the exact failure of a
                                            # frontend reading a field the
                                            # server never sends.
                                            "origin": "broker",
                                            # The closing deal's own fill price.
                                            # `d.price` on an out-deal is the
                                            # EXIT price, so the row carries the
                                            # outcome a client needs to draw an
                                            # exit marker — without it the API
                                            # sent a `realized_pnl` with no price
                                            # it could have come from.
                                            "exit_price": float(d.price),
                                            "realized_pnl": round(float(d.profit), 2),
                                            "profit": round(float(d.profit), 2)
                                        })
                    # Sort newest first
                    trades.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)

                    symbol_filter = str((query.get("symbol") or [""])[0]).strip().upper()
                    if symbol_filter:
                        trades = [t for t in trades if str(t.get("symbol", "")).upper() == symbol_filter]

                    self._send_json(trades[:limit])
                except Exception as e:
                    logger.error(f"Error fetching trade history: {e}")
                    self._send_json({"error": str(e)})
            elif path == "/api/news":
                # Institutional Macro News & Economic Calendar. The banner used
                # to claim "Real-Time" unconditionally while the engine served a
                # hardcoded calendar whenever both feeds failed -- which is every
                # time in this environment (FairEconomy answers 429 "Rate
                # Limited", MyFxBook 403 behind a Cloudflare challenge that
                # urllib can never pass, since it runs no JavaScript). The
                # response now carries provenance so the UI can say which it is.
                from jarvis.market.news import GLOBAL_NEWS_ENGINE
                news_items = GLOBAL_NEWS_ENGINE.get_news_calendar()
                n_fab = sum(1 for it in news_items if it.get("is_fallback"))
                if news_items and n_fab == len(news_items):
                    source = "synthetic_calendar"
                elif n_fab:
                    source = "mixed"
                else:
                    source = "live_feed"
                self._send_json({
                    "news": news_items,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "synthetic_count": n_fab,
                    "is_fallback": bool(news_items) and n_fab == len(news_items),
                    "source": source,
                })
            elif path == "/api/pending_orders":
                pending = self.mt5_client.get_pending_orders()
                self._send_json(pending)
            elif path in ["/api/auth/me", "/api/auth/verify"]:
                token = self._extract_token()
                user = RemoteAuthEngine.validate_token(token) if token else None
                if user:
                    self._send_json({"status": "AUTHENTICATED", "valid": True, "user": user})
                else:
                    self._send_json({"status": "UNAUTHORIZED", "valid": False, "error": "Not authenticated"}, status_code=401)
            elif path == "/api/stream/telemetry":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                cors_origin = self._allowed_cors_origin(self.headers.get("Origin", ""))
                if cors_origin:
                    self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.end_headers()

                # Stream initial state DIGEST, not the full snapshot.
                # A10: the full one measured 63 KB and was re-sent on every state
                # change, up to once a second (~63 KB/s per client). The digest is
                # ~30% of that and names the endpoint that still has the detail.
                snap = self.state_manager.get_state_digest()
                init_msg = f"event: telemetry\ndata: {json.dumps(snap, default=str)}\n\n"
                try:
                    self.wfile.write(init_msg.encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    return

                # SSE Event Loop
                last_ver = self.state_manager.get_state_version()
                for _ in range(60): # 60 iterations (approx 1-2 mins before clean client reconnect)
                    time.sleep(1.0)
                    cur_ver = self.state_manager.get_state_version()
                    try:
                        if cur_ver != last_ver:
                            last_ver = cur_ver
                            cur_snap = self.state_manager.get_state_digest()
                            msg = f"event: telemetry\ndata: {json.dumps(cur_snap, default=str)}\n\n"
                            self.wfile.write(msg.encode("utf-8"))
                            self.wfile.flush()
                        else:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                    except Exception:
                        break
                return
            elif path == "/api/diagnostics":
                snap = self.state_manager.get_state_snapshot()
                self._send_json({
                    "status": "SAFE_MODE" if snap["safe_mode"] else "OPERATIONAL",
                    "services": snap["services"],
                    "account": snap["account"],
                    "timestamp": snap["timestamp"]
                })
            elif path == "/api/risk_status":
                try:
                    acc = self.mt5_client.get_account_snapshot() if (hasattr(self, "mt5_client") and self.mt5_client) else None
                    equity = getattr(acc, "equity", 10000.0) or 10000.0
                    balance = getattr(acc, "balance", 10000.0) or 10000.0
                    if hasattr(self, "orchestrator") and self.orchestrator and hasattr(self.orchestrator, "risk_engine"):
                        r_status = self.orchestrator.risk_engine.get_risk_status(equity, balance)
                    else:
                        from jarvis.risk.risk_engine import RiskEngine
                        re = RiskEngine()
                        r_status = re.get_risk_status(equity, balance)
                    self._send_json(r_status)
                except Exception as e:
                    logger.error(f"Error fetching risk status: {e}")
                    self._send_json({"error": str(e)}, status_code=500)
            elif path == "/api/metrics":
                self._send_metrics(query)
            else:
                self.send_error(404, "Endpoint not found")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass
        except Exception as e:
            logger.error(f"Error handling GET {path}: {e}", exc_info=True)
            try:
                self._send_json({"error": str(e)}, status_code=500)
            except Exception:
                pass
        finally:
            # Unbind even on the SSE path, which holds this thread for ~60s and
            # would otherwise hand its request id to whatever runs next here.
            unbind(_bind_token)
            _request_timer.stop()

    def do_OPTIONS(self):
        try:
            self.send_response(200)
            cors_origin = self._allowed_cors_origin(self.headers.get("Origin", ""))
            if cors_origin:
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Max-Age", "86400")
            self.end_headers()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        _route = self._normalize_route(path)
        _request_timer = HTTP_LATENCY.time(route=_route)
        _bind_token = bind(request_id=new_request_id(), route=_route)

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length < 0 or content_length > 1_048_576:
                self._send_json({"status": "FAILED", "error": "Request body is too large"}, status_code=413)
                return
            body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
            try:
                data = json.loads(body)
            except Exception:
                data = {}

            if path == "/api/auth/login":
                username = data.get("username", "").strip()
                password = data.get("password", "").strip()
                client_ip = self.client_address[0] if hasattr(self, "client_address") and self.client_address else ""
                user_info, err_msg = RemoteAuthEngine.verify_credentials(username, password, client_ip=client_ip)
                if user_info:
                    session_info = RemoteAuthEngine.create_session_token(username)
                    token = session_info["token"]
                    cookie_header = self._session_cookie(token, int(RemoteAuthEngine._token_ttl))
                    public_session = {k: v for k, v in session_info.items() if k != "token"}
                    self._send_json(public_session, cookies=[cookie_header])
                else:
                    status_code = 429 if "locked" in (err_msg or "").lower() else 401
                    self._send_json({"status": "UNAUTHORIZED" if status_code == 401 else "LOCKED", "error": err_msg or "Invalid username or password"}, status_code=status_code)
                return
            elif path == "/api/auth/logout":
                token = self._extract_token()
                RemoteAuthEngine.revoke_token(token)
                logout_cookie = self._session_cookie("", 0) + "; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
                self._send_json({"status": "LOGGED_OUT", "message": "Session terminated successfully"}, cookies=[logout_cookie])
                return
            elif path == "/api/auth/verify":
                token = self._extract_token()
                user = RemoteAuthEngine.validate_token(token) if token else None
                if not user and self._is_local_request():
                    try:
                        session_info = RemoteAuthEngine.create_session_token("admin")
                        token = session_info.get("token")
                        user = {"username": "admin", "role": "ADMIN", "full_name": "System Administrator"}
                    except Exception:
                        user = {"username": "admin", "role": "ADMIN", "full_name": "System Administrator"}
                if user:
                    refresh_cookie = self._session_cookie(token, int(RemoteAuthEngine._token_ttl)) if token else ""
                    public_user = {k: v for k, v in user.items() if k != "token"}
                    cookies = [refresh_cookie] if refresh_cookie else []
                    self._send_json({"status": "AUTHENTICATED", "valid": True, "user": public_user}, cookies=cookies)
                else:
                    self._send_json({"status": "UNAUTHORIZED", "valid": False, "error": "Invalid or expired session"}, status_code=401)
                return
            elif path == "/api/auth/change_password":
                user = self._get_auth_user()
                if not user:
                    self._send_json({"status": "UNAUTHORIZED", "error": "Authentication required"}, status_code=401)
                    return
                old_pwd = data.get("old_password", "")
                new_pwd = data.get("new_password", "")
                success, msg = RemoteAuthEngine.change_password(user["username"], old_pwd, new_pwd)
                if success:
                    self._send_json({"status": "SUCCESS", "message": msg})
                else:
                    self._send_json({"status": "FAILED", "error": msg}, status_code=400)
                return

            # Protected Action Endpoints — Require Authentication + appropriate role
            if (path.startswith("/api/action/")
                    or path.startswith("/api/historical/")
                    or path.startswith("/api/backtest/")
                    or path.startswith("/api/intelligence/")):
                ok, _ = self._require_role("ADMIN", "TRADER")
                if not ok:
                    return
            if path == "/api/action/set_mode":
                ok, _ = self._require_role("ADMIN")
                if not ok:
                    return
            elif path.startswith("/api/copilot/"):
                if not self._check_auth():
                    self._send_json({"status": "UNAUTHORIZED", "error": "Authentication required"}, status_code=401)
                    return

            if path == "/api/copilot/ask":
                query = data.get("query", "")
                # The page tells us which instrument the trader is looking at so
                # "why?" is answered about the chart on screen. Extra keys are
                # ignored rather than rejected — this endpoint must stay usable
                # from a one-line curl.
                context = data.get("context") if isinstance(data.get("context"), dict) else None
                # Conversation memory is per caller. A curl with no token and no
                # local origin still gets the old stateless behaviour.
                session_id = self._copilot_session_id()
                response_text = self.copilot.ask(query, context=context,
                                                 session_id=session_id)
                # `session` and `provider` are additive: both front ends read
                # `response` and nothing else. They exist so a caller can tell a
                # stateless answer from a continued one without guessing.
                self._send_json({
                    "query": query,
                    "response": response_text,
                    "session": bool(session_id),
                    "provider": bool(self.copilot.provider.available),
                })
            elif path == "/api/action/auto-select" or path.startswith("/api/backtest/") or path.startswith("/api/intelligence/"):
                from jarvis.api.intelligence_api import INTELLIGENCE
                if not INTELLIGENCE.handle_post(path, data, self):
                    self.send_error(404, f"Intelligence API {path} not found")
            elif path == "/api/action/toggle_safe_mode":
                is_safe = self.state_manager.toggle_safe_mode()
                self._send_json({"safe_mode": is_safe})
            elif path == "/api/action/set_trade_style":
                style = data.get("trade_style", "SWING").upper()
                self.state_manager.set_trade_style(style)
                # Propagate to the LIVE orchestrator. This is the fix for a real
                # bug: the old check was `hasattr(self, "orchestrator")` against
                # an attribute that was never set, so it was always False and the
                # selector silently changed nothing. The response now states
                # whether the engine actually picked the change up.
                applied = False
                orch = self.orchestrator
                if orch is not None:
                    try:
                        orch.trade_style = style
                        applied = True
                    except Exception as exc:
                        logger.warning(f"Could not apply trade_style to orchestrator: {exc}")
                self._send_json({
                    "status": "SUCCESS",
                    "trade_style": style,
                    "applied_to_orchestrator": applied,
                    "orchestrator_attached": orch is not None,
                })
            elif path == "/api/historical/download":
                from jarvis.historical.historical_engine import HISTORICAL_DATA_ENGINE
                sym = data.get("symbol", "XAUUSD").upper()
                tf = data.get("timeframe", "H1").upper()
                months = int(data.get("months", 6))
                from datetime import datetime, timezone, timedelta
                end = datetime.now(timezone.utc)
                start = end - timedelta(days=months * 30)
                res = HISTORICAL_DATA_ENGINE.download(sym, tf, start=start, end=end, force=data.get("force", False))
                self._send_json(res)
            elif path == "/api/historical/replay":
                from jarvis.historical.historical_engine import HISTORICAL_DATA_ENGINE
                from jarvis.historical.replay_engine import MarketReplayEngine, RealisticExecutionSimulator
                sym = data.get("symbol", "XAUUSD").upper()
                tf = data.get("timeframe", "H1").upper()
                bars = int(data.get("bars", 150))
                df = HISTORICAL_DATA_ENGINE.get_market_data(sym, tf, num_bars=bars)
                sim = RealisticExecutionSimulator()
                engine = MarketReplayEngine(df, symbol=sym, timeframe=tf, simulator=sim)
                def dummy_strat(b, h, s):
                    pass
                res = engine.run_replay(dummy_strat, start_idx=20)
                self._send_json(res)
            elif path == "/api/action/set_mode":
                mode_str = data.get("mode", "PAPER").upper()
                try:
                    mode = ExecutionMode(mode_str)
                    self.state_manager.set_execution_mode(mode)
                    self.mt5_client.mode = mode.value.lower()
                    self._send_json({"status": "SUCCESS", "mode": mode.value})
                except Exception as e:
                    self._send_json({"status": "FAILED", "error": str(e)}, status_code=400)
            elif path == "/api/action/close_position":
                ticket = int(data.get("ticket", 0))
                if ticket <= 0:
                    self._send_json({"status": "FAILED", "error": "Invalid ticket number"}, status_code=400)
                    return
                res = self.mt5_client.close_position(ticket)
                # Re-sync positions in state manager immediately
                fresh_pos = self.mt5_client.get_open_positions()
                fresh_acc = self.mt5_client.get_account_snapshot()
                self.state_manager.sync_broker_state(fresh_acc, fresh_pos)
                self._send_json(res)
            elif path == "/api/action/close_all_positions":
                results = self.mt5_client.close_all_positions()
                fresh_pos = self.mt5_client.get_open_positions()
                fresh_acc = self.mt5_client.get_account_snapshot()
                self.state_manager.sync_broker_state(fresh_acc, fresh_pos)
                self._send_json({"status": "SUCCESS", "closed_count": len(results), "details": results})
            elif path == "/api/action/emergency_stop":
                reason = str(data.get("reason") or "OPERATOR_EMERGENCY_STOP: Trading manually halted by user")
                close_pos = bool(data.get("close_positions", False))
                if hasattr(self, "orchestrator") and self.orchestrator:
                    res = self.orchestrator.emergency_stop(reason=reason, close_positions=close_pos)
                else:
                    from jarvis.risk.circuit_breaker import CircuitBreaker
                    cb = CircuitBreaker()
                    cb.trip(reason)
                    cancelled = self.mt5_client.cancel_all_pending_orders() if (hasattr(self, "mt5_client") and self.mt5_client) else []
                    closed = self.mt5_client.close_all_positions() if (close_pos and hasattr(self, "mt5_client") and self.mt5_client) else []
                    res = {"status": "SUCCESS", "circuit_breaker": "TRIPPED", "reason": reason, "orders_cancelled": len(cancelled), "positions_closed": len(closed)}
                if close_pos and hasattr(self, "mt5_client") and self.mt5_client:
                    fresh_pos = self.mt5_client.get_open_positions()
                    fresh_acc = self.mt5_client.get_account_snapshot()
                    self.state_manager.sync_broker_state(fresh_acc, fresh_pos)
                self._send_json(res)
            elif path == "/api/action/resume_trading":
                if hasattr(self, "orchestrator") and self.orchestrator:
                    res = self.orchestrator.resume_trading()
                else:
                    from jarvis.risk.circuit_breaker import CircuitBreaker
                    cb = CircuitBreaker()
                    cb.reset()
                    res = {"status": "SUCCESS", "circuit_breaker": "RESET"}
                self._send_json(res)
            elif path == "/api/action/cancel_pending_order":
                ticket = int(data.get("ticket", 0))
                if ticket <= 0:
                    self._send_json({"status": "FAILED", "error": "Invalid ticket number"}, status_code=400)
                    return
                res = self.mt5_client.cancel_pending_order(ticket)
                self._send_json(res)
            elif path == "/api/action/place_pending_order":
                sym = str(data.get("symbol", "") or "").strip().upper()
                otype = str(data.get("order_type", data.get("type", "")) or "").strip().upper()
                valid_types = {"BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"}
                if otype not in valid_types:
                    self._send_json({"status": "FAILED", "error": f"order_type must be one of {sorted(valid_types)}"}, status_code=400)
                    return
                if not sym:
                    self._send_json({"status": "FAILED", "error": "symbol is required"}, status_code=400)
                    return
                try:
                    price = float(data.get("price", 0.0))
                    lots = float(data.get("lots", data.get("volume", 0.01)))
                except (TypeError, ValueError):
                    self._send_json({"status": "FAILED", "error": "price and volume must be numbers"}, status_code=400)
                    return
                if not math.isfinite(price) or price <= 0:
                    self._send_json({"status": "FAILED", "error": "a pending order needs a positive price"}, status_code=400)
                    return
                if not math.isfinite(lots) or lots <= 0:
                    self._send_json({"status": "FAILED", "error": "volume must be a positive finite number"}, status_code=400)
                    return
                # A pending order without a stop is legal — unlike a market
                # order, which the desk requires to carry one — so 0 here means
                # "none" rather than "invalid".
                sl = float(data.get("sl", data.get("sl_price", 0.0)) or 0.0)
                tp = float(data.get("tp", data.get("tp_price", 0.0)) or 0.0)
                res = self.mt5_client.place_pending_order(
                    symbol=sym,
                    order_type=otype,
                    price=price,
                    volume=lots,
                    sl_price=sl if math.isfinite(sl) and sl > 0 else 0.0,
                    tp_price=tp if math.isfinite(tp) and tp > 0 else 0.0,
                    comment=str(data.get("comment", "HMAlgo2_Pending"))[:26] or "HMAlgo2_Pending",
                )
                self._send_json(res)
            elif path == "/api/action/modify_pending_order":
                ticket = int(data.get("ticket", 0) or 0)
                if ticket <= 0:
                    self._send_json({"status": "FAILED", "error": "Invalid ticket number"}, status_code=400)
                    return

                def _level(key: str, *aliases: str) -> Optional[float]:
                    """None = leave alone; a number = set it (0 clears it)."""
                    for k in (key,) + aliases:
                        if k in data and data[k] not in (None, "", "null"):
                            try:
                                v = float(data[k])
                            except (TypeError, ValueError):
                                return None
                            return v if math.isfinite(v) and v >= 0 else None
                    return None

                price = _level("price")
                sl = _level("sl", "sl_price")
                tp = _level("tp", "tp_price")
                if price is None and sl is None and tp is None:
                    self._send_json({"status": "FAILED", "error": "supply at least one of price, sl, tp"}, status_code=400)
                    return
                res = self.mt5_client.modify_pending_order(ticket, price=price, sl=sl, tp=tp)
                self._send_json(res)
            elif path in ("/api/action/manual_trade", "/api/action/place_order"):
                sym = data.get("symbol", "XAUUSD")
                action = data.get("action", data.get("order_type", data.get("side", "BUY"))).upper()
                lots = float(data.get("lots", data.get("volume", 0.01)))
                sl = float(data.get("sl", data.get("sl_price", 0.0)))
                tp = float(data.get("tp", data.get("tp_price", 0.0)))
                comment = data.get("comment", "HMAlgo2_ManualDesk")
                current_price = float(data.get("price", data.get("current_price", 0.0)))
                if action not in {"BUY", "SELL"}:
                    self._send_json({"status": "FAILED", "error": "action must be BUY or SELL"}, status_code=400)
                    return
                if not math.isfinite(lots) or lots <= 0:
                    self._send_json({"status": "FAILED", "error": "lots must be a positive finite number"}, status_code=400)
                    return

                # If sl <= 0 or tp <= 0, compute AI structural levels
                if sl <= 0 or tp <= 0:
                    try:
                        from jarvis.intelligence.dynamic_levels import DYNAMIC_LEVELS_ENGINE
                        ai_levels = DYNAMIC_LEVELS_ENGINE.calculate_manual_trade_levels(
                            symbol=sym,
                            action=action,
                            current_price=current_price if current_price > 0 else None
                        )
                        if sl <= 0:
                            sl = float(ai_levels.get("sl", 0.0))
                        if tp <= 0:
                            tp = float(ai_levels.get("tp", 0.0))
                    except Exception as ex:
                        logger.error(f"Error computing AI structural levels for {sym}: {ex}")
                if not all(math.isfinite(v) and v > 0 for v in (sl, tp)):
                    self._send_json({"status": "FAILED", "error": "Valid stop-loss and take-profit are required"}, status_code=400)
                    return

                res = self.mt5_client.send_market_order(
                    symbol=sym,
                    order_type=action,
                    volume=lots,
                    sl_price=sl,
                    tp_price=tp,
                    comment=comment,
                    reference_price=current_price
                )
                if res and res.get("status") == "FILLED":
                    try:
                        from jarvis.data.database import TRADE_DB
                        TRADE_DB.log_trade(
                            ticket=res.get("ticket", 0),
                            symbol=sym,
                            action=action,
                            entry=float(res.get("price", 0.0)),
                            sl=sl,
                            tp=tp,
                            volume=lots,
                            score=100.0,
                            regime="MANUAL_EXECUTION",
                            ev=0.0,
                            executor="MANUAL_AI_ASSISTED",
                            # D2: so the exit deal can find this row again.
                            position_id=res.get("position_id"),
                            # D1: the manual desk trades real money through the
                            # same client; a fallback price must still be marked.
                            origin=("synthetic" if res.get("is_fallback")
                                    else ("paper" if self.mt5_client.mode == "paper"
                                          else "broker")),
                            # D1 (completed): the mode is a separate question
                            # from the price. A fallback price on a live client
                            # is still a live trade.
                            execution_mode=(
                                str(getattr(self.mt5_client, "mode", "") or "").lower()
                                if str(getattr(self.mt5_client, "mode", "") or "").lower()
                                in ("live", "paper", "demo") else "unknown"
                            ),
                        )
                    except Exception as ex:
                        logger.error(f"Error logging manual trade to DB: {ex}")

                fresh_pos = self.mt5_client.get_open_positions()
                fresh_acc = self.mt5_client.get_account_snapshot()
                self.state_manager.sync_broker_state(fresh_acc, fresh_pos)
                self._send_json(res)
            else:
                self.send_error(404, "Endpoint not found")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass
        except Exception as e:
            logger.error(f"Error handling POST {path}: {e}", exc_info=True)
            try:
                self._send_json({"error": str(e)}, status_code=500)
            except Exception:
                pass
        finally:
            unbind(_bind_token)
            _request_timer.stop()

    # Response headers applied to every JSON, HTML and static response.
    #
    # CSP is deliberately wider than `default-src 'self'`. The pages load Google
    # Fonts, jsDelivr (Bootstrap / Chart.js / lightweight-charts) and, on demand,
    # TradingView's tv.js; the favicons are `data:` URIs. A bare `default-src
    # 'self'` blocks all of those and would regress every page, so the directive
    # names exactly the origins the UI actually uses and nothing else. Inline
    # styles and scripts are allowed because the dashboard uses both.
    _SECURITY_HEADERS = (
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Content-Security-Policy",
         "default-src 'self'; "
         "img-src 'self' data:; "
         "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
         "font-src 'self' data: https://fonts.gstatic.com; "
         "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://s3.tradingview.com; "
         "frame-src 'self' https://*.tradingview.com; "
         "connect-src 'self' https://cdn.jsdelivr.net https://*.tradingview.com wss://*.tradingview.com"),
    )

    def _send_security_headers(self):
        for name, value in self._SECURITY_HEADERS:
            self.send_header(name, value)

    #: A broker lock held longer than this is not a slow call, it is a wedged one:
    #: `BrokerLock` gives up waiting after 10s (broker_lock.DEFAULT_WAIT_SEC), so
    #: past that point callers are already failing. Kept in step with that module.
    _HEALTH_LOCK_STALE_SEC = 10.0

    @classmethod
    def _history_db_path(cls) -> str:
        """The canonical history DB (``data/jarvis_history.db``), taken from the
        single resolver in ``jarvis.data.database`` — not a second copy of it."""
        from jarvis.data.database import TRADE_DB
        return TRADE_DB.db_path

    def _send_health(self):
        """Liveness/readiness probe for ``/health`` and ``/ready``.

        Never raises: a probe that 500s tells a monitor nothing, so each
        dependency is isolated and a failure degrades the answer instead of
        propagating. 200 only when all three checks pass, else 503.
        """
        try:
            broker_lock = self.mt5_client.broker_lock_health()
        except Exception as exc:
            broker_lock = {"error": str(exc)}
        try:
            # Phase 3 relocated this module (jarvis.application -> jarvis.common).
            # Accept either location so the probe is correct whether the move is
            # present or not.
            try:
                from jarvis.common.timeout_guard import TimeoutGuard
            except ImportError:
                from jarvis.application.timeout_guard import TimeoutGuard
            guard = TimeoutGuard.health()
        except Exception as exc:
            guard = {"error": str(exc)}
        try:
            conn = sqlite3.connect(self._history_db_path())
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
            db_ok = True
        except Exception:
            db_ok = False

        # A probe that raised carries no verdict keys, so reading it back with
        # .get("held") / .get("wedged") silently yields "healthy". Treat an
        # unprobed subsystem as degraded: a monitor must never be told "ok" by an
        # endpoint that could not actually look.
        lock_failed = "error" in broker_lock or broker_lock.get("held") is None
        guard_failed = "error" in guard or guard.get("wedged") is None

        lock_stale = bool(broker_lock.get("held")) and \
            float(broker_lock.get("age_sec") or 0.0) > self._HEALTH_LOCK_STALE_SEC
        ok = (db_ok and not lock_stale and not bool(guard.get("wedged"))
              and not lock_failed and not guard_failed)
        self._send_json({
            "status": "ok" if ok else "degraded",
            "broker_lock": broker_lock,
            "guard": guard,
            "db": db_ok,
            "ts": datetime.now(timezone.utc).isoformat(),
        }, status_code=200 if ok else 503)

    def _send_metrics(self, query: Dict[str, list]) -> None:
        """The scrape surface. Deliberately NOT in `public_get_endpoints`.

        `/health` is public because a proxy has to be able to reach it, but a
        metrics page enumerates the platform's internals — how many orders were
        refused, how often the news calendar is synthetic — and that is not
        information an anonymous caller needs. It is reachable from loopback by
        the existing bypass, which is what a local scraper wants.

        `?format=prometheus` returns text exposition; the default is JSON.
        """
        fmt = (query.get("format", ["json"])[0] or "json").strip().lower()
        if fmt in ("prometheus", "text", "txt"):
            body = REGISTRY.render_prometheus().encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._send_security_headers()
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                pass
            return
        self._send_json({
            "metrics": REGISTRY.collect(),
            "uptime_seconds": REGISTRY.uptime_seconds(),
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def _send_json(self, data: Any, status_code: int = 200, cookies: Optional[list] = None):
        try:
            payload = json.dumps(data, default=str).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self._send_security_headers()
            cors_origin = self._allowed_cors_origin(self.headers.get("Origin", ""))
            if cors_origin:
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            if cookies:
                for c in cookies:
                    if c:
                        self.send_header("Set-Cookie", c)
            self.end_headers()
            self.wfile.write(payload)
            HTTP_REQUESTS.inc(route=str(_bound_route()),
                              status_class=_status_class(status_code))
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass
        except Exception as e:
            logger.debug(f"Socket write exception (non-fatal): {e}")

    _STATIC_CACHE: ClassVar[Dict[str, Tuple[bytes, str, float]]] = {}

    def _serve_static_file(self, req_path: str):
        now = time.time()
        is_vendor = "vendor" in req_path
        cache_ttl = 3600.0 if is_vendor else 5.0

        if req_path in self._STATIC_CACHE:
            content, mime_type, ts = self._STATIC_CACHE[req_path]
            if now - ts < cache_ttl:
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", mime_type)
                    self.send_header("Content-Length", str(len(content)))
                    self._send_security_headers()
                    if is_vendor:
                        self.send_header("Cache-Control", "public, max-age=604800, immutable")
                    else:
                        self.send_header("Cache-Control", "no-cache, must-revalidate")
                    self.end_headers()
                    self.wfile.write(content)
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    pass
                return

        static_dir = os.path.abspath(os.path.join(self.base_dir, "ui", "static"))
        rel = req_path.lstrip("/").replace("static/", "", 1)
        target_path = os.path.abspath(os.path.join(static_dir, rel))
        found_path = None
        if target_path.startswith(static_dir) and os.path.exists(target_path) and os.path.isfile(target_path):
            found_path = target_path

        if found_path:
            mime_type, _ = mimetypes.guess_type(found_path)
            if not mime_type:
                mime_type = "text/plain"
            if found_path.endswith(".css"):
                mime_type = "text/css"
            elif found_path.endswith(".js"):
                mime_type = "application/javascript"

            with open(found_path, "rb") as f:
                content = f.read()

            self._STATIC_CACHE[req_path] = (content, mime_type, now)

            try:
                self.send_response(200)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(len(content)))
                self._send_security_headers()
                if is_vendor:
                    self.send_header("Cache-Control", "public, max-age=604800, immutable")
                else:
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(content)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                pass
        else:
            self.send_error(404, f"Static file {req_path} not found")

    def _serve_template(self, template_name: str):
        templates_dir = os.path.abspath(os.path.join(self.base_dir, "ui", "templates"))
        clean_name = os.path.basename(template_name)
        ui_path = os.path.abspath(os.path.join(templates_dir, clean_name))
        if ui_path.startswith(templates_dir) and os.path.exists(ui_path) and os.path.isfile(ui_path):
            with open(ui_path, "r", encoding="utf-8") as f:
                content = f.read()
            content_bytes = content.encode("utf-8")

            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content_bytes)))
                self._send_security_headers()
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(content_bytes)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                pass
        else:
            self.send_error(404, f"Template {template_name} not found")

    def _serve_dashboard_ui(self):
        """Primary surface: the advanced trading terminal (dashboard.html).

        Built on theme_terminal.css and bound entirely to live API data — no
        market value is hard-coded in the template or its controller.
        """
        self._serve_template("dashboard.html")

    def _serve_console_ui(self):
        """Previous desk console, retained at /console."""
        self._serve_template("console.html")

    def _serve_terminal_ui(self):
        """Legacy terminal UI, retained at /classic as a rollback path."""
        self._serve_template("index.html")

    def _serve_stocks_ui(self):
        self._serve_template("stocks.html")

    def _serve_india_ui(self):
        self._serve_template("india.html")

    def _serve_options_ui(self):
        self._serve_template("india_options.html")

    def _serve_positions_ui(self):
        """iOS-style positions surface — Bootstrap-based responsive layout with
        a full iOS design language for mobile and a consistent look at desktop.
        Served at /positions for both /positions and /positions.html."""
        self._serve_template("positions.html")

class _NoReverseDNSHTTPServer(ThreadingHTTPServer):
    """`ThreadingHTTPServer` without the blocking reverse-DNS lookup on bind.

    `http.server.HTTPServer.server_bind` ends with
    `self.server_name = socket.getfqdn(host)`. `getfqdn` is a REVERSE DNS
    lookup, and where that lookup does not answer it blocks for as long as the
    resolver takes. Measured here on `HM_start.py live`: `py-spy dump` put
    MainThread in `server_bind -> getfqdn (socket.py:811)` while the port was
    never opened, so the platform looked like it had hung at startup even
    though nothing about the broker was involved. Every other symptom of a
    wedged start (tunnels up, workers busy, banner printed) was present.

    `server_name` only fills the `Server:` response header, so the literal host
    is a correct substitute and costs nothing.
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def _build_server(host: str, port: int) -> ThreadingHTTPServer:
    """One place that constructs the listener, so both entry points agree."""
    _NoReverseDNSHTTPServer.allow_reuse_address = True
    return _NoReverseDNSHTTPServer((host, port), JarvisRequestHandler)


def start_server(host: str = "127.0.0.1", port: int = 8501, mt5_client: Optional[MT5Client] = None,
                 orchestrator: Optional[Any] = None) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"} and os.environ.get("JARVIS_COOKIE_SECURE", "").lower() not in {"1", "true", "yes"}:
        raise ValueError("Remote binding requires HTTPS session cookies: set JARVIS_COOKIE_SECURE=1 behind TLS.")
    if mt5_client:
        JarvisRequestHandler.configure_broker(mt5_client)
    if orchestrator is not None:
        JarvisRequestHandler.configure_orchestrator(orchestrator)
    ThreadingHTTPServer.allow_reuse_address = True
    server = _build_server(host, port)
    JarvisRequestHandler.start_background_syncer()
    logger.info(f"HM Algo 2.0 Web Terminal Server running at http://{host}:{port}")
    return server

def run_web_server(port: int = 8501, host: str = "127.0.0.1", mt5_client: Optional[MT5Client] = None,
                   orchestrator: Optional[Any] = None):
    if host not in {"127.0.0.1", "::1", "localhost"} and os.environ.get("JARVIS_COOKIE_SECURE", "").lower() not in {"1", "true", "yes"}:
        raise ValueError("Remote binding requires HTTPS session cookies: set JARVIS_COOKIE_SECURE=1 behind TLS.")
    if mt5_client:
        JarvisRequestHandler.configure_broker(mt5_client)
    if orchestrator is not None:
        JarvisRequestHandler.configure_orchestrator(orchestrator)
    ThreadingHTTPServer.allow_reuse_address = True
    server = _build_server(host, port)
    JarvisRequestHandler.start_background_syncer()
    logger.info(f"HM Algo 2.0 Web Terminal Server running at http://{host}:{port}")
    try:
        server.serve_forever()
    except Exception as e:
        logger.error(f"Web server serve_forever error: {e}", exc_info=True)

