"""Live end-to-end check of the redesigned dashboard and the API surface.

Starts the real HTTP server on a scratch port with no broker attached, then
exercises every route the browser would call.

Usage (from the repo root):

    python tools/verify_ui_live.py

Exit code is 0 when every check passes, 1 otherwise, so it can gate a release.

Three things this script deliberately proves rather than assumes:

* ``/api/action/auto-select`` stays a dry run even when asked to go live. The
  HTTP surface must have no path that can open a trade.
* With no orchestrator attached, auto-selection reports ``UNAVAILABLE`` with a
  reason instead of returning an empty-but-successful selection. Failing safe
  and saying so is the correct behaviour; a 200 with no decisions would let a
  caller mistake "engine not wired" for "nothing to trade".
* Every route that returns modelled, fixed or placeholder values says so in its
  own payload. A UI cannot label what the backend does not describe, so this is
  checked at the HTTP boundary rather than trusted from the render layer.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

# Run from anywhere: put the repo root on the path explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.api.server import start_server

PORT = 8599
BASE = f"http://127.0.0.1:{PORT}"

results = []


# Decimal literals that look like a market price or a monetary amount rather
# than a layout constant. Used to prove the dashboard does not bake values into
# its markup: a price rendered server-side is stale the moment it is sent.
_PRICE_RE = re.compile(r">\s*[-+]?\d{1,3}(?:,\d{3})*(?:\.\d{2,5})?\s*<")


def _static_market_values(html: str) -> list[str]:
    """Market-looking values sitting in element text rather than bound at runtime."""
    hits = []
    for m in _PRICE_RE.finditer(html or ""):
        literal = m.group(0).strip().strip("<>").strip()
        # Counters that start at zero are legitimate initial state, not data.
        if literal in {"0", "1", "100"}:
            continue
        hits.append(literal)
    return hits


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


# Statuses a provider-backed route may legitimately answer. Anything else is a
# failure, including the synthetic status 0 that request() returns when the
# client gives up: a route that never answers pins a server thread, which is
# worse than one that errors.
PROVIDER_OK = (200, 503, 504)


def request(path, payload=None, timeout=60):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (TimeoutError, urllib.error.URLError) as exc:
        # A route that never answers is a result, not a crash. Status 0 is
        # deliberately not a real HTTP code so a caller can tell "hung" apart
        # from "answered 500" — the two need different fixes.
        return 0, json.dumps({"error": f"no response within {timeout}s: {exc}"})


def provider_verdict(status, ok_when_200, detail_when_200, timeout):
    """One shared verdict for every provider-backed route.

    200 is judged by the caller, because only the caller knows which fields
    that particular payload must carry. 503/504 is an honest "the upstream is
    unavailable" and passes. 0 means the route never answered inside the
    client budget — a failure, and the message says which failure it is.
    404 and 500 are failures for the obvious reasons.
    """
    if status == 200:
        return ok_when_200, detail_when_200
    if status in (503, 504):
        return True, f"status={status} (provider unavailable, route dispatched)"
    if status == 0:
        return False, (
            f"no response within {timeout}s — the route has no effective "
            f"timeout of its own and pins a server thread"
        )
    return False, f"status={status} (unwired or the handler raised)"


def main():
    server = start_server(host="127.0.0.1", port=PORT, mt5_client=None, orchestrator=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(1.5)

    try:
        # ── 1. Page routing ────────────────────────────────────────────────
        status, body = request("/")
        record(
            "GET / serves the advanced trading dashboard",
            status == 200 and "tt-app" in body and "dashboard.js" in body,
            f"status={status} bytes={len(body)}",
        )

        status, body = request("/dashboard.html")
        record(
            "GET /dashboard.html serves the same dashboard",
            status == 200 and "tt-app" in body,
            f"status={status} bytes={len(body)}",
        )

        # The dashboard must not bake market values into the markup. A price
        # rendered server-side would be stale the moment it left the process.
        status, body = request("/")
        leaks = _static_market_values(body)
        record(
            "dashboard HTML contains no hard-coded market values",
            status == 200 and not leaks,
            f"status={status} leaks={leaks[:6]}" if leaks else f"status={status} clean",
        )

        # Every template on disk, not only the ones served above. A fabricated
        # value in a page nobody checked is exactly how the legacy terminal came
        # to display 4380.00 for gold while the feed was down — the value was in
        # the markup, so it rendered before any request had been made.
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tdir = os.path.join(repo_root, "jarvis", "ui", "templates")
        leaks_by_file = {}
        try:
            for name in sorted(os.listdir(tdir)):
                if not name.endswith(".html"):
                    continue
                with open(os.path.join(tdir, name), encoding="utf-8") as fh:
                    found = _static_market_values(fh.read())
                if found:
                    leaks_by_file[name] = found[:4]
        except OSError as exc:
            leaks_by_file = {"<unreadable>": [str(exc)]}
        record(
            "no template hard-codes a market value",
            not leaks_by_file,
            f"leaks={leaks_by_file}" if leaks_by_file else "all templates clean",
        )

        # The dashboard's timeframe selector was inert for a while: it sent
        # `timeframe=` while the handler read `tf=`, so every request silently
        # came back as H1 whatever the user picked. Assert that both spellings
        # are honoured AND that two different timeframes really do return
        # different series — accepting the parameter and then ignoring it would
        # pass a weaker check.
        series = {}
        for label, qs in (
            ("tf", "symbol=XAUUSD&tf=H1"),
            ("timeframe", "symbol=XAUUSD&timeframe=H1"),
            ("M5", "symbol=XAUUSD&tf=M5"),
        ):
            status, body = request("/api/candles?" + qs)
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
            candles = payload.get("candles") or []
            series[label] = {
                "status": status,
                "tf": payload.get("timeframe"),
                "n": len(candles),
                "first": candles[0]["time"] if candles else None,
            }

        record(
            "GET /api/candles accepts both tf and timeframe",
            series["tf"]["tf"] == "H1" and series["timeframe"]["tf"] == "H1",
            f"tf={series['tf']['tf']} timeframe={series['timeframe']['tf']}",
        )
        record(
            "GET /api/candles honours the requested timeframe",
            series["M5"]["tf"] == "M5" and series["M5"]["first"] != series["tf"]["first"],
            f"H1 first={series['tf']['first']} M5 first={series['M5']['first']}",
        )

        # The chart surface is created at runtime, so a missing container is
        # invisible to every HTTP check above: the panel simply never draws.
        status, body = request("/dashboard")
        chart_ids = [
            "chart-live-price", "chart-legend", "chart-hud",
            "chart-tooltip", "chart-levels", "chart-timeframe",
        ]
        missing_ids = [i for i in chart_ids if f'id="{i}"' not in body]
        record(
            "the dashboard ships the full chart surface",
            status == 200 and not missing_ids,
            f"missing={missing_ids}" if missing_ids else "all present",
        )

        try:
            with open(os.path.join(repo_root, "jarvis", "ui", "static", "js", "dashboard.js"),
                      encoding="utf-8") as fh:
                dash_js = fh.read()
        except OSError as exc:
            dash_js = ""
            js_note = str(exc)
        else:
            js_note = ""
        record(
            "dashboard.js requests candles with the parameter the handler reads",
            "/api/candles?symbol=" in dash_js and "&tf=" in dash_js and "&timeframe=" not in dash_js,
            js_note or ("ok" if "&tf=" in dash_js else "no &tf= in the candles call"),
        )

        status, body = request("/api/pending_orders")
        record(
            "GET /api/pending_orders dispatches",
            status in (200, 503),
            f"status={status}",
        )

        status, body = request("/console")
        record(
            "GET /console serves the previous console",
            status == 200 and "cx-topbar" in body,
            f"status={status} bytes={len(body)}",
        )

        status, body = request("/classic")
        record(
            "GET /classic still serves the legacy terminal (rollback path)",
            status == 200 and "terminal.js" in body,
            f"status={status} bytes={len(body)}",
        )

        status, body = request("/classic.html")
        record(
            "GET /classic.html serves the same legacy terminal",
            status == 200 and "terminal.js" in body,
            f"status={status} bytes={len(body)}",
        )

        # The retired UI must be genuinely gone, not merely unrouted.
        status, _ = request("/static/js/app_shell.js")
        record(
            "retired app_shell.js is no longer served",
            status == 404,
            f"status={status}",
        )

        # ── 2. Assets ──────────────────────────────────────────────────────
        for asset, needle in (
            ("/static/css/theme_terminal.css", "tt-panel"),
            ("/static/css/console.css", "cx-topbar"),
            ("/static/js/dashboard.js", "HM Algo 2.0"),
            # console.js is an IIFE and deliberately exports no global, so match
            # a distinctive string from the file body instead.
            ("/static/js/console.js", "HM Algo 2.0 Console"),
        ):
            status, body = request(asset)
            record(
                f"GET {asset}",
                status == 200 and needle in body,
                f"status={status} bytes={len(body)}",
            )

        # ── 3. Intelligence GET endpoints ──────────────────────────────────
        status, body = request("/api/intelligence/meta")
        ok = status == 200
        payload = {}
        if ok:
            try:
                payload = json.loads(body)
            except ValueError:
                ok = False
        record(
            "GET /api/intelligence/meta",
            ok,
            f"status={status} keys={sorted(payload)[:6]}",
        )

        status, body = request("/api/intelligence/reliability")
        ok = status == 200
        payload = {}
        if ok:
            try:
                payload = json.loads(body)
            except ValueError:
                ok = False
        # `styles` is a list of per-style records, not a mapping.
        styles = sorted(
            row.get("style") for row in (payload.get("styles") or []) if isinstance(row, dict)
        )
        weights = {
            row.get("style"): row.get("weight")
            for row in (payload.get("styles") or [])
            if isinstance(row, dict)
        }
        record(
            "GET /api/intelligence/reliability",
            ok and styles == ["DAY_TRADING", "SCALP", "SWING"],
            f"status={status} weights={weights}",
        )

        status, body = request("/api/intelligence/auto-selection")
        payload = {}
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        # With no engine attached the endpoint must fail safe and say so, rather
        # than returning an empty-but-successful selection.
        record(
            "GET /api/intelligence/auto-selection fails safe without an engine",
            status == 503
            and payload.get("status") == "UNAVAILABLE"
            and bool(payload.get("error")),
            f"status={status} status_field={payload.get('status')!r}",
        )

        # ── 4. Backtest endpoints ──────────────────────────────────────────
        status, body = request("/api/backtest/meta")
        ok = status == 200
        payload = {}
        if ok:
            try:
                payload = json.loads(body)
            except ValueError:
                ok = False
        record(
            "GET /api/backtest/meta",
            ok,
            f"status={status} keys={sorted(payload)[:8]}",
        )

        status, body = request("/api/backtest/jobs")
        ok = status == 200
        record("GET /api/backtest/jobs", ok, f"status={status}")

        # ── Regime policy ──────────────────────────────────────────────────
        # Either answer is correct here and both are asserted: 200 with a policy
        # table once tools/optimise_regime.py has run, or 503 UNAVAILABLE with a
        # reason when it has not. What must never happen is a 404 (route not
        # wired) or a 500 (handler raised) — and a bare 200 with no policy would
        # let a caller mistake "never run" for "nothing tradeable".
        status, body = request("/api/backtest/regime-policy")
        payload = {}
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        if status == 200:
            ok = payload.get("status") == "OK" and isinstance(payload.get("policy"), dict)
            detail = f"status={status} modes={sorted(payload.get('policy', {}))}"
        else:
            ok = status == 503 and payload.get("status") == "UNAVAILABLE" and bool(
                payload.get("error")
            )
            detail = f"status={status} status_field={payload.get('status')!r}"
        record("GET /api/backtest/regime-policy dispatches", ok, detail)

        # The style filter must not blow up on a missing parameter, and must not
        # return modes that were not asked for.
        status, body = request("/api/backtest/regime-policy?styles=SCALP")
        payload = {}
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        returned = set((payload.get("policy") or {}).keys())
        record(
            "GET /api/backtest/regime-policy honours ?styles=",
            status in (200, 503) and returned <= {"SCALP"},
            f"status={status} returned={sorted(returned)}",
        )

        # ── 5. POST auto-select (dry run is hard-wired) ────────────────────
        status, body = request("/api/action/auto-select", {"dry_run": True})
        payload = {}
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        # Same fail-safe as the GET: no engine, no selection, said out loud.
        record(
            "POST /api/action/auto-select fails safe without an engine",
            status == 503 and payload.get("status") == "UNAVAILABLE",
            f"status={status} status_field={payload.get('status')!r}",
        )

        # ── 6. Submit a real backtest job ──────────────────────────────────
        status, body = request(
            "/api/backtest/run",
            {
                "label": "live-check",
                "modes": ["SWING"],
                "symbols": ["EURUSD"],
                "objective": "expectancy_r",
                "min_trades": 10,
                "passes": 1,
                "max_evaluations": 12,
            },
            timeout=60,
        )
        payload = {}
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        # A queued job is 202 Accepted, not 200.
        job_id = payload.get("job_id") or (payload.get("job") or {}).get("job_id")
        record(
            "POST /api/backtest/run queues a job (202 Accepted)",
            status == 202 and bool(job_id),
            f"status={status} job_id={job_id}",
        )

        if job_id:
            final = None
            for _ in range(60):
                status, body = request(f"/api/backtest/jobs/{job_id}")
                try:
                    final = json.loads(body)
                except ValueError:
                    final = {}
                state = (final.get("job") or final).get("status")
                if state in ("DONE", "FAILED", "CANCELLED"):
                    break
                time.sleep(2)
            state = (final.get("job") or final).get("status")
            record(
                "the job reaches a terminal state",
                state in ("DONE", "FAILED", "CANCELLED"),
                f"status={state}",
            )

            status, body = request(f"/api/backtest/jobs/{job_id}/result")
            ok = status in (200, 404)
            record(
                "GET /api/backtest/jobs/<id>/result responds",
                ok,
                f"status={status} bytes={len(body)}",
            )

        # ── 7. The redesigned dashboard's data panels ──────────────────────
        # Every panel below is fed by a route that either sits behind an
        # external provider (and can block for many seconds on a cold cache) or
        # returns modelled values. Two invariants matter more than the numbers:
        #
        #   * the route must DISPATCH. A 404 means the tab is inert: its shell
        #     renders, the click does nothing, and nothing raises.
        #   * every response must describe its own PROVENANCE. A UI cannot label
        #     what the backend does not describe, and a plausible number shown
        #     as a live quote is worse than no number at all.
        #
        # Provider routes are allowed to answer 503/504 — that is an honest
        # "provider unavailable". They are not allowed to answer 404 (unwired)
        # or 500 (handler raised).

        # 7a. The served markup must ship the panels themselves. A missing
        # section id is invisible to every check above: the rail tab exists, so
        # the UI looks complete, but selecting it renders an empty box.
        status, body = request("/dashboard")
        panel_ids = [
            # news calendar
            "view-news", "news-count", "news-live-chip", "news-impact",
            "news-currency", "news-refresh", "news-body", "news-updated",
            "news-hero", "news-detail-impact", "news-detail",
            # devil's advocate + quality gate
            "view-analyst", "da-symbol", "da-verdict", "da-metrics", "da-bull",
            "da-bear", "da-threats", "da-invalidation", "da-objections",
            "gate-count", "gate-verdict", "gate-body",
            # global + Indian markets
            "view-markets", "eq-count", "eq-body", "eq-heatmap", "in-indices",
            "in-fii", "in-oc-symbol", "in-optionchain",
            # provenance chips
            "eq-prov", "in-index-prov", "in-fii-prov", "in-oc-prov",
            # the chart-source switch and the external chart's container
            "chart-src-native", "chart-src-tv", "chart-tv",
        ]
        missing = [i for i in panel_ids if f'id="{i}"' not in body]
        record(
            "the dashboard ships every new panel, chip and the chart switch",
            status == 200 and not missing,
            f"missing={missing}" if missing else f"{len(panel_ids)} ids present",
        )

        # 7b. The rail tab must exist for each view, or the panel above is
        # unreachable. Both halves are asserted because either one alone leaves
        # a dead tab.
        rail_missing = [
            v for v in ("news", "analyst", "markets")
            if f'data-view-btn="{v}"' not in body or f'data-view-panel="{v}"' not in body
        ]
        record(
            "each new view has both a rail tab and a panel",
            status == 200 and not rail_missing,
            f"missing={rail_missing}" if rail_missing else "3 views wired",
        )

        # 7c. The served stylesheet must carry the new class families. Perfect
        # markup is still invisible if the CSS that is served does not define it.
        status, body = request("/static/css/theme_terminal.css")
        css_classes = [
            "tt-news", "tt-heatgrid", "tt-gauge", "tt-gate", "tt-chart__tv",
            "tt-prov-note", "tt-eq", "tt-oc",
        ]
        css_missing = [c for c in css_classes if c not in body]
        record(
            "the served stylesheet defines the new panel classes",
            status == 200 and not css_missing,
            f"missing={css_missing}" if css_missing else f"{len(css_classes)} families present",
        )

        # 7d. /api/news — the countdown's anchor. The client corrects for clock
        # skew against the envelope's own generation time, so that value must be
        # a real ISO instant rather than a display string.
        status, body = request("/api/news", timeout=90)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        items = payload.get("news") or []
        generated = payload.get("timestamp")
        envelope_dt = None
        try:
            envelope_dt = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
            envelope_ok = envelope_dt.tzinfo is not None
        except (TypeError, ValueError):
            envelope_ok = False
        news_fields = {"timestamp_iso", "diff_seconds", "status_badge", "event",
                       "currency", "impact"}
        thin = [i for i, ev in enumerate(items) if not news_fields <= set(ev)]
        record(
            "GET /api/news carries the fields the countdown is built from",
            status == 200 and bool(items) and not thin and envelope_ok,
            f"status={status} events={len(items)} envelope={generated!r} thin={thin[:3]}",
        )

        # 7e. The sign of diff_seconds must agree with timestamp_iso measured
        # against the envelope's own generation time. If the two disagree, the
        # client's locally-derived countdown contradicts the server's and a row
        # flips phase at the wrong moment — which is exactly why deriving the
        # countdown client-side is only safe when this holds.
        disagree = []
        if envelope_dt is not None:
            for ev in items[:12]:
                iso, diff = ev.get("timestamp_iso"), ev.get("diff_seconds")
                if iso is None or diff is None:
                    continue
                try:
                    event_dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
                except ValueError:
                    disagree.append((iso, "unparseable"))
                    continue
                expected = (event_dt - envelope_dt).total_seconds()
                # Same side of zero, and within the couple of seconds the
                # payload takes to travel.
                if (expected > 0) != (float(diff) > 0) or abs(expected - float(diff)) > 120:
                    disagree.append((ev.get("event"), diff, round(expected, 1)))
        record(
            "the server's diff_seconds agrees with its own timestamp_iso",
            envelope_ok and not disagree,
            f"disagreements={disagree[:3]}" if disagree
            else f"{min(len(items), 12)} events consistent",
        )

        # 7f. /api/india/fii_dii must declare itself a sample. It previously
        # returned fixed constants under a docstring claiming a real-time proxy,
        # which is indistinguishable from live institutional flow.
        status, body = request("/api/india/fii_dii", timeout=90)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        record(
            "GET /api/india/fii_dii declares its provenance",
            status in (200, 503)
            and payload.get("data_source") == "sample"
            and bool(payload.get("data_source_note")),
            f"status={status} data_source={payload.get('data_source')!r}",
        )

        # The provider-backed routes below are given 60s. The dashboard's own
        # per-request budget for these is 30s (TIMEOUT.provider in dashboard.js),
        # so a route that cannot answer in twice that is already unusable to the
        # UI. A tighter client bound also keeps a hung route from turning this
        # script into a 20-minute wait.
        PROVIDER_BUDGET = 60

        # 7g. /api/stocks/screener must flag placeholder rows at both levels:
        # per row so the renderer can suppress a fabricated setup, and at the
        # envelope so a caller can tell a fully analysed scan from a partly
        # failed one without walking the universe.
        status, body = request(
            "/api/stocks/screener?sort_by=probability&sort_dir=desc&limit=40",
            timeout=PROVIDER_BUDGET,
        )
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        rows = payload.get("stocks") or []
        unlabelled = [r.get("symbol") for r in rows if not r.get("analysis_source")]
        bad_fallback = [
            r.get("symbol") for r in rows
            if r.get("analysis_source") == "fallback"
            and r.get("data_source") != "profile_reference"
        ]
        ok, detail = provider_verdict(
            status,
            isinstance(payload.get("fallback_count"), int)
            and isinstance(payload.get("provenance"), dict)
            and not unlabelled
            and not bad_fallback,
            f"status=200 rows={len(rows)} "
            f"fallback_count={payload.get('fallback_count')} "
            f"unlabelled={unlabelled[:3]} bad_fallback={bad_fallback[:3]}",
            PROVIDER_BUDGET,
        )
        record("GET /api/stocks/screener labels every row's provenance", ok, detail)

        # 7h. The two heatmaps. The India one is additionally asserted NOT to
        # carry avg_cmf: the India engine computes no money-flow index, so a
        # tile showing one would be invented. It does carry avg_probability, and
        # the US tiles carry avg_cmf, so the two are deliberately not symmetric.
        status, body = request("/api/stocks/heatmap", timeout=PROVIDER_BUDGET)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        us_sectors = payload.get("sectors") or []
        ok, detail = provider_verdict(
            status,
            isinstance(us_sectors, list),
            f"status=200 sectors={len(us_sectors)}",
            PROVIDER_BUDGET,
        )
        record("GET /api/stocks/heatmap dispatches", ok, detail)

        status, body = request("/api/india/heatmap", timeout=PROVIDER_BUDGET)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        in_sectors = payload.get("sectors") or []
        invented = [s.get("sector") for s in in_sectors if "avg_cmf" in s]
        in_unflagged = [s.get("sector") for s in in_sectors if not s.get("data_source")]
        ok, detail = provider_verdict(
            status,
            bool(in_sectors) and not invented and not in_unflagged,
            f"status=200 sectors={len(in_sectors)} "
            f"invented_cmf={invented[:3]} unflagged={in_unflagged[:3]}",
            PROVIDER_BUDGET,
        )
        record("GET /api/india/heatmap reports only what it computes", ok, detail)

        # 7i. /api/india/indices — every row must name its own source, because a
        # static profile reference price sits alongside live quotes and the panel
        # reports the weakest one.
        status, body = request("/api/india/indices", timeout=PROVIDER_BUDGET)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        idx_rows = payload.get("indices") or []
        idx_unflagged = [r.get("symbol") for r in idx_rows if not r.get("data_source")]
        ok, detail = provider_verdict(
            status,
            bool(idx_rows) and not idx_unflagged,
            f"status=200 rows={len(idx_rows)} unflagged={idx_unflagged[:3]}",
            PROVIDER_BUDGET,
        )
        record("GET /api/india/indices labels every row's source", ok, detail)

        # 7j. /api/india/option_chain — the chain is modelled, so it must say so,
        # and it must expose the ATM strike the panel centres its window on.
        status, body = request(
            "/api/india/option_chain?symbol=NIFTY", timeout=PROVIDER_BUDGET
        )
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        chain = payload.get("chain") or []
        ok, detail = provider_verdict(
            status,
            bool(payload.get("data_source"))
            and payload.get("atm_strike") is not None
            and bool(chain),
            f"status=200 source={payload.get('data_source')!r} "
            f"atm={payload.get('atm_strike')} strikes={len(chain)}",
            PROVIDER_BUDGET,
        )
        record("GET /api/india/option_chain declares itself modelled", ok, detail)

        # ── 8. Attach a real orchestrator and re-check the live path ───────
        try:
            from jarvis.api.server import JarvisRequestHandler
            from jarvis.application.orchestrator import JarvisOrchestrator

            orchestrator = JarvisOrchestrator(
                symbols=["EURUSD"], mode="paper", trade_style="SWING"
            )
            JarvisRequestHandler.configure_orchestrator(orchestrator)

            status, body = request("/api/intelligence/meta")
            meta = json.loads(body) if status == 200 else {}
            record(
                "configure_orchestrator reaches the intelligence service",
                status == 200 and meta.get("orchestrator_attached") is True,
                f"status={status} attached={meta.get('orchestrator_attached')}",
            )

            status, body = request(
                "/api/intelligence/auto-selection?symbols=EURUSD&styles=SWING",
                timeout=240,
            )
            payload = {}
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
            record(
                "GET /api/intelligence/auto-selection with an engine attached",
                status == 200 and payload.get("status") == "OK",
                f"status={status} field={payload.get('status')!r} "
                f"decisions={len(payload.get('decisions') or [])} "
                f"error={str(payload.get('error'))[:80]!r}",
            )

            # The HTTP path must never be able to open a trade.
            if status == 200:
                record(
                    "auto-selection is hard-wired to dry_run",
                    payload.get("dry_run") is True,
                    f"dry_run={payload.get('dry_run')!r}",
                )

            status, body = request(
                "/api/action/auto-select",
                {"dry_run": False, "symbols": ["EURUSD"], "styles": ["SWING"]},
                timeout=240,
            )
            payload = {}
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
            record(
                "POST /api/action/auto-select ignores dry_run=False",
                status in (200, 503) and payload.get("dry_run") is True,
                f"status={status} dry_run={payload.get('dry_run')!r} "
                f"ignored={payload.get('requested_dry_run_ignored')!r}",
            )
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            record(
                "attach a real orchestrator and re-check the live path",
                False,
                f"{type(exc).__name__}: {exc}",
            )

        # ── 9. Copilot: the route answers, and dispatches by intent ────────
        # This endpoint had no probe and its module had no tests, so a routing
        # regression reached the trader unchallenged. A plain answer and the
        # intent dispatch are both checked, because the failure this endpoint
        # has actually shipped was a wrong-but-plausible answer rather than an
        # error: "how are my trades doing?" returned one instrument's position
        # status.
        status, body = request("/api/copilot/ask", {"query": ""})
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        answer = payload.get("response") or ""
        record(
            "POST /api/copilot/ask answers with a response body",
            status == 200 and len(answer) > 40,
            f"status={status} chars={len(answer)}",
        )

        status, body = request(
            "/api/copilot/ask",
            {"query": "how are my trades doing?", "context": {"symbol": "XAUUSD"}},
        )
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        answer = payload.get("response") or ""
        record(
            "the copilot answers a book-level question about the book",
            status == 200 and "How you stand" in answer and "no open position" not in answer,
            f"status={status} book_answer={'How you stand' in answer}",
        )

        # ── 10. Unknown route still 404s ───────────────────────────────────
        status, _ = request("/api/backtest/nonsense")
        record("unknown backtest route 404s", status == 404, f"status={status}")

    finally:
        server.shutdown()
        server.server_close()

    failures = [r for r in results if not r[1]]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
