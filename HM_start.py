"""
HM Algo 2.0 — Primary Autonomous System Launcher (HM_start.py).
Launches:
 1. Autonomous Multi-Asset Trading Engine & Quality Gate Decision Matrix
 2. Remote Access Web Terminal & REST API Server (Port 8501)
 3. Automatic Authenticated HTTPS Mobile Access Tunnel (localhost.run / serveo)
 4. Permanent Local Wi-Fi & Global Cloud Access
All in one single command!
"""
import os
import sys
import time
import re
import socket
import shutil
import threading
import subprocess
import logging

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from jarvis.application.orchestrator import JarvisOrchestrator
from jarvis.api.server import run_web_server
# Admin credentials are resolved from environment at runtime (see jarvis.api.remote_auth)

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("HM_START")

_TUNNEL_STATE = {
    "serveo_url": "https://hm2026.serveousercontent.com",
    "serveo_status": "CONNECTING",
    "cloudflare_url": "establishing...",
    "cloudflare_status": "STARTING",
    "url": "https://hm2026.serveousercontent.com",
    "status": "STARTING",
    "provider": "Serveo (Custom: hm2026) + Cloudflare Edge",
    "serveo_proc": None,
    "cloudflare_proc": None
}

def get_local_wifi_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"

def find_cloudflared_binary():
    """Locates the Cloudflare Tunnel executable on Windows / Linux."""
    cand = shutil.which("cloudflared")
    if cand and os.path.exists(cand):
        return cand
    for p in [
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
        r"C:\cloudflared\cloudflared.exe",
        os.path.expanduser("~\\cloudflared.exe")
    ]:
        if os.path.exists(p):
            return p
    return None

def _save_active_tunnel_url(url: str, provider: str = ""):
    try:
        target = os.path.join(BASE_DIR, "active_tunnel_url.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write(url.strip())
    except Exception:
        pass

def _serveo_worker(port: int = 8501, custom_subdomain: str = "hm2026"):
    """Dedicated persistent worker for https://hm2026.serveousercontent.com with auto-reconnect."""
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "TCPKeepAlive=yes",
        "-R", f"{custom_subdomain}:80:127.0.0.1:{port}",
        "serveo.net"
    ]
    for candidate_key in [
        os.path.expanduser("~/.ssh/id_ed25519"),
        os.path.expanduser("~/.ssh/id_hm2026"),
        os.path.expanduser("~/.ssh/id_rsa")
    ]:
        if os.path.exists(candidate_key):
            cmd = [cmd[0], "-i", candidate_key] + cmd[1:]
            break

    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/IM", "ssh.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["pkill", "-f", "serveo.net"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
    except Exception:
        pass

    while True:
        try:
            logger.info(f"Connecting Custom Subdomain HTTPS via serveo.net ({custom_subdomain})...")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace"
            )
            _TUNNEL_STATE["serveo_proc"] = proc
            rate_limited = False
            for _ in range(60):
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue
                if "Forwarding HTTP traffic from" in line or f"{custom_subdomain}.serveousercontent.com" in line:
                    _TUNNEL_STATE["serveo_url"] = f"https://{custom_subdomain}.serveousercontent.com"
                    _TUNNEL_STATE["serveo_status"] = "CONNECTED"
                    # Only take the primary slot when Cloudflare is unavailable — see
                    # _cloudflare_worker for why serveo is not dependable as the main link.
                    if _TUNNEL_STATE.get("cloudflare_status") != "CONNECTED":
                        _TUNNEL_STATE["url"] = _TUNNEL_STATE["serveo_url"]
                        _save_active_tunnel_url(_TUNNEL_STATE["serveo_url"], "serveo")
                        print(f"\n[HM_START] CUSTOM SUBDOMAIN ACTIVE (MOBILE LINK): {_TUNNEL_STATE['serveo_url']}\n", flush=True)
                    logger.info(f"Custom Subdomain Active: {_TUNNEL_STATE['serveo_url']}")
                    break
                if "Free users are limited" in line or "failed for listen port" in line:
                    rate_limited = True

            # Keep alive while process is running. stdout MUST be drained: ssh blocks
            # forever once the ~64KB pipe buffer fills, and a blocked ssh stops sending
            # keepalives — so the tunnel silently dies without the process ever exiting.
            def _drain_serveo():
                try:
                    for _ in proc.stdout:
                        pass
                except Exception:
                    pass

            threading.Thread(target=_drain_serveo, daemon=True,
                             name="hm_serveo_stdout_drain").start()

            while proc.poll() is None:
                time.sleep(2.0)

            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            backoff = 60 if rate_limited else 5
            logger.warning(f"Serveo custom tunnel closed. Auto-reconnecting in {backoff}s...")
            _TUNNEL_STATE["serveo_status"] = "RECONNECTING"
            time.sleep(backoff)
        except Exception as e:
            logger.error(f"Serveo worker error: {e}. Reconnecting in 10s...")
            time.sleep(10)

def _cloudflare_worker(port: int = 8501):
    """Dedicated worker for Cloudflare Edge Tunnel — primary mobile link when available."""
    cloudflared_bin = find_cloudflared_binary()
    if not cloudflared_bin:
        logger.warning("cloudflared binary not found; skipping secondary edge tunnel.")
        return

    cmd = [cloudflared_bin, "tunnel", "--url", f"http://127.0.0.1:{port}"]
    while True:
        try:
            logger.info("Connecting High-Speed Cloudflare Edge Tunnel...")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace"
            )
            _TUNNEL_STATE["cloudflare_proc"] = proc
            rate_limited = False
            for _ in range(60):
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue
                if "429 Too Many Requests" in line or "1015" in line:
                    rate_limited = True
                # Must run on EVERY line, not just rate-limited ones: the tunnel URL
                # only ever appears on a non-429 line. Keeping this scoped to the
                # branch above left `m` unbound and raised UnboundLocalError.
                m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if m:
                    url = m.group(0)
                    if "api.trycloudflare.com" in url:
                        continue
                    _TUNNEL_STATE["cloudflare_url"] = url
                    _TUNNEL_STATE["cloudflare_status"] = "CONNECTED"
                    # Cloudflare is the PRIMARY mobile link. serveo.net tears the SSH session
                    # down on a ~12-minute cycle (measured 12m09s +/- 1s over 6 consecutive
                    # drops), so the custom subdomain is up but 502s between reconnects.
                    # Cloudflare's subdomain is random per restart but stays up.
                    _TUNNEL_STATE["url"] = url
                    _save_active_tunnel_url(url, "cloudflare")
                    logger.info(f"Cloudflare Edge Tunnel Active (PRIMARY): {url}")
                    print(f"\n[HM_START] MOBILE LINK (CLOUDFLARE, PRIMARY): {url}\n", flush=True)
                    break
            while proc.poll() is None:
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                time.sleep(1.0)
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            backoff = 30 if rate_limited else 3
            logger.warning(f"Cloudflare edge tunnel closed. Auto-reconnecting in {backoff}s...")
            _TUNNEL_STATE["cloudflare_status"] = "RECONNECTING"
            time.sleep(backoff)
        except Exception as e:
            logger.error(f"Cloudflare worker error: {e}. Reconnecting in 5s...")
            time.sleep(5)

def _start_background_tunnel(port: int = 8501):
    """Launches Cloudflare Edge as the primary mobile link, with Serveo (hm2026) as fallback.

    Serveo keeps the stable custom subdomain but drops the SSH session every ~12 minutes,
    so it is demoted to backup. Both run in parallel whichever connects first wins the
    primary slot (see the promotion guards in each worker)."""
    t_serveo = threading.Thread(target=_serveo_worker, args=(port, "hm2026"), daemon=True, name="hm_tunnel_serveo")
    t_serveo.start()

    t_cf = threading.Thread(target=_cloudflare_worker, args=(port,), daemon=True, name="hm_tunnel_cf")
    t_cf.start()

def hm_start(mode: str = "live", port: int = 8501, host: str = "127.0.0.1", trade_style: str = "ALL"):
    local_ip = get_local_wifi_ip()

    # Remote tunnels start automatically by default for mobile phone access (disable with JARVIS_ENABLE_TUNNEL=0 if needed)
    enable_tunnel = os.environ.get("JARVIS_ENABLE_TUNNEL", "1").lower() not in {"0", "false", "no", "off"}
    if enable_tunnel:
        tunnel_thread = threading.Thread(target=_start_background_tunnel, args=(port,), daemon=True, name="hm_mobile_tunnel")
        tunnel_thread.start()

    # Warm the intelligence API import BEFORE any worker thread exists.
    # `run_web_server` imports it lazily inside `configure_orchestrator`, and it
    # is heavy (`jarvis.backtesting.optimizer`). Doing it there means it runs
    # after the engine threads are already inside `mt5.initialize()` — which,
    # with no terminal answering, holds the GIL for 60-100s per call — so the
    # main thread never finished the import and `ThreadingHTTPServer` was never
    # constructed: measured 40+ minutes with both tunnels healthy and no
    # listener on :8501. Importing while the process is single-threaded makes
    # the cost deterministic and removes the window entirely.
    try:
        import jarvis.api.intelligence_api  # noqa: F401
    except Exception as exc:
        logger.warning("Could not pre-import the intelligence API: %s", exc)

    # 2. Start Autonomous Orchestrator (defaults to LIVE mode; allows paper for testing)
    orchestrator = JarvisOrchestrator(mode=mode, trade_style=trade_style)
    actual_mode = orchestrator.mode.upper()

    print("=" * 95, flush=True)
    print("                 HM Algo 2.0 — INSTITUTIONAL QUANTITATIVE TRADING PLATFORM", flush=True)
    print("=" * 95, flush=True)
    print(f" -> Execution Mode             : {actual_mode}", flush=True)
    print(f" -> Trade Style                : {trade_style.upper()}", flush=True)
    print(f" -> Local Terminal             : http://{host}:{port}", flush=True)
    print(f" -> Local Network Address      : {local_ip}", flush=True)
    print(f" -> Mobile Access Link         : https://hm2026.serveousercontent.com", flush=True)
    print(f" -> Remote Tunnel              : {'enabled' if enable_tunnel else 'disabled'}", flush=True)
    print(f" -> Admin Login                : admin / hm2026admin", flush=True)
    print("=" * 95, flush=True)

    # Attach to the MetaTrader terminal BEFORE anything is served.
    #
    # This is the one call site allowed to LAUNCH the terminal (`allow_launch`).
    # With no terminal running, `initialize()` starts `terminal64.exe` and can
    # block 60-100s inside native code while HOLDING THE GIL. At boot that is
    # acceptable — nothing is listening yet — and on a request thread it is
    # fatal, which is why every read path leaves the flag at its default and
    # refuses. Skipping the launch entirely is not an option: starting the
    # terminal used to be a side effect of `initialize()` on a worker thread,
    # so gating it out everywhere left `HM_start.bat live` booting, serving,
    # and quietly reporting SYNTHETIC bars with no way to trade.
    try:
        from jarvis.data.broker_symbols import ensure_mt5_terminal

        if ensure_mt5_terminal(allow_launch=True):
            logger.info("MetaTrader 5 terminal attached; live bars and execution are available.")
        else:
            logger.warning(
                "Could not attach to (or launch) the MetaTrader 5 terminal. The "
                "platform will serve, but market data is SYNTHETIC and orders "
                "cannot be sent. Start the terminal manually "
                "(C:\\Program Files\\MetaTrader 5\\terminal64.exe), log in to the "
                "account, then restart HM_start."
            )
    except Exception as exc:
        logger.warning("Terminal bring-up failed: %s", exc)

    orch_thread = threading.Thread(target=orchestrator.start, daemon=True, name="hm_orchestrator")
    orch_thread.start()
    logger.info(f"Autonomous Multi-Asset Trading Engine active ({actual_mode} mode, style {trade_style.upper()}).")

    # 3. Start Remote Access Web Terminal & REST API Server with auto-recovery
    logger.info(f"Starting Remote Access Web Terminal at http://{host}:{port}...")
    while True:
        try:
            run_web_server(port=port, host=host, mt5_client=orchestrator.mt5_client,
                           orchestrator=orchestrator)
        except KeyboardInterrupt:
            logger.info("Shutting down HM Algo 2.0 trading platform...")
            orchestrator.stop()
            for proc_key in ("serveo_proc", "cloudflare_proc"):
                proc = _TUNNEL_STATE.get(proc_key)
                if proc:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            print("\n[SHUTDOWN] HM Algo 2.0 stopped cleanly.", flush=True)
            break
        except Exception as e:
            logger.error(f"Web server encountered error: {e}. Auto-restarting in 3s...", exc_info=True)
            time.sleep(3)

def main():
    # Execution mode resolution with broker account auto-detection
    mode = "live"
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        raw = sys.argv[1].lower()
        if raw in {"paper", "test", "sim", "backtest"}:
            mode = "paper"
        elif raw in {"demo", "broker_demo"}:
            mode = "demo"
        else:
            mode = "live"
    else:
        # Auto-detect connected MT5 terminal trade_mode to prevent safety lockout
        try:
            import MetaTrader5 as mt5
            if mt5.initialize():
                acc = mt5.account_info()
                if acc:
                    # trade_mode: 0 = DEMO, 1 = CONTEST, 2 = REAL
                    if getattr(acc, "trade_mode", 0) in (0, 1):
                        mode = "demo"
                        logger.info(f"Auto-detected DEMO MT5 account #{acc.login} ({acc.server}). Setting mode='demo'.")
                    else:
                        mode = "live"
                        logger.info(f"Auto-detected REAL MT5 account #{acc.login} ({acc.server}). Setting mode='live'.")
        except Exception as ex:
            logger.debug(f"Account auto-detect exception: {ex}")
            mode = "live"
    hm_start(mode=mode)

if __name__ == "__main__":
    main()

