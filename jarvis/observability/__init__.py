"""Observability for the JARVIS platform: structured logs and scraped metrics.

Two halves, deliberately kept independent so a caller can take one without the
other:

* :mod:`jarvis.observability.context` / :mod:`jarvis.observability.logging_setup`
  — a fixed vocabulary of fields, bound per unit of work and merged into every
  log record. Opt in by calling :func:`configure_logging`.
* :mod:`jarvis.observability.metrics` — counters, gauges and histograms with a
  Prometheus-compatible text exposition format and no third-party dependency.
  :mod:`jarvis.observability.instruments` is the catalogue of what is published.

Neither half imports anything from `jarvis.*` outside this package, so any
layer (market, data, execution, intelligence, api) can use both without
creating an import cycle.
"""
from __future__ import annotations

from .context import (
    CANONICAL_KEYS,
    COMPONENT,
    DURATION_MS,
    ERROR_TYPE,
    EVENT,
    OUTCOME,
    REQUEST_ID,
    ROUTE,
    SOURCE,
    SYMBOL,
    bind,
    bound,
    bound_scope,
    clear,
    current_request_id,
    iter_bound,
    new_request_id,
    unbind,
)
from .logging_setup import (
    JsonFormatter,
    KeyValueFormatter,
    configure_logging,
    log_event,
    log_exception,
    record_fields,
)
from .metrics import (
    DEFAULT_MAX_SERIES,
    Counter,
    Gauge,
    Histogram,
    MetricsError,
    Registry,
    Timer,
    counter,
    gauge,
    histogram,
)

from .alerting import (
    ALERT_MANAGER,
    Alert,
    AlertManager,
    AlertSeverity,
    dispatch_alert,
)

__all__ = [
    # alerting
    "ALERT_MANAGER", "Alert", "AlertManager", "AlertSeverity", "dispatch_alert",
    # context
    "CANONICAL_KEYS", "COMPONENT", "DURATION_MS", "ERROR_TYPE", "EVENT",
    "OUTCOME", "REQUEST_ID", "ROUTE", "SOURCE", "SYMBOL",
    "bind", "bound", "bound_scope", "clear", "current_request_id",
    "iter_bound", "new_request_id", "unbind",
    # logging
    "JsonFormatter", "KeyValueFormatter", "configure_logging", "log_event",
    "log_exception", "record_fields",
    # metrics
    "DEFAULT_MAX_SERIES", "Counter", "Gauge", "Histogram", "MetricsError",
    "Registry", "Timer", "counter", "gauge", "histogram",
]
