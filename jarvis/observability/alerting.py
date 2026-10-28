"""Active alerting subsystem for the JARVIS quantitative platform.

Provides real-time multi-channel notifications (Logger, Ring Buffer, Webhook)
for operational anomalies, circuit breaker halts, risk limit breaches, and
system errors with thread-safe debouncing and rate limiting.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, List, Optional
from urllib import request as urllib_request

logger = logging.getLogger("JARVIS_Alerting")


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    FATAL = "FATAL"


@dataclass
class Alert:
    title: str
    message: str
    severity: AlertSeverity = AlertSeverity.INFO
    source: str = "JARVIS"
    timestamp: float = field(default_factory=time.time)
    details: Dict[str, Any] = field(default_factory=dict)
    event_key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value if hasattr(self.severity, "value") else str(self.severity)
        return d


class AlertManager:
    """Thread-safe active alert dispatcher with debouncing and webhook integration."""

    _instance: Optional[AlertManager] = None
    _lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> AlertManager:
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, max_history: int = 100, webhook_url: Optional[str] = None):
        if getattr(self, "_initialized", False):
            return
        self._max_history = max_history
        self._webhook_url = webhook_url or os.environ.get("JARVIS_ALERT_WEBHOOK_URL", "").strip()
        self._history: Deque[Alert] = deque(maxlen=max_history)
        self._last_alert_times: Dict[str, float] = {}
        self._history_lock = threading.Lock()
        self._initialized = True
        logger.info(
            "AlertManager initialized (webhook=%s, history_cap=%d)",
            "ENABLED" if self._webhook_url else "DISABLED (set JARVIS_ALERT_WEBHOOK_URL)",
            max_history,
        )

    def dispatch(
        self,
        title: str,
        message: str,
        severity: AlertSeverity = AlertSeverity.INFO,
        source: str = "JARVIS",
        details: Optional[Dict[str, Any]] = None,
        event_key: str = "",
        debounce_seconds: float = 60.0,
    ) -> bool:
        """Dispatches an alert across all active channels if not debounced.

        Returns True if the alert was dispatched; False if suppressed by debouncing.
        """
        now = time.time()
        key = event_key or f"{source}:{title}"

        # 1. Debounce check
        with self._history_lock:
            last_time = self._last_alert_times.get(key, 0.0)
            if debounce_seconds > 0 and (now - last_time) < debounce_seconds:
                logger.debug("Alert '%s' suppressed by debouncing (cooldown: %.1fs)", key, debounce_seconds)
                return False
            self._last_alert_times[key] = now

        alert = Alert(
            title=title,
            message=message,
            severity=severity,
            source=source,
            timestamp=now,
            details=details or {},
            event_key=key,
        )

        # 2. Append to ring buffer
        with self._history_lock:
            self._history.append(alert)

        # 3. Structured log dispatch
        log_msg = f"🔔 [{alert.severity.value}] [{alert.source}] {alert.title}: {alert.message}"
        if alert.severity in (AlertSeverity.FATAL, AlertSeverity.CRITICAL):
            logger.critical(log_msg)
        elif alert.severity == AlertSeverity.WARNING:
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

        # 4. Asynchronous Webhook Dispatch
        if self._webhook_url:
            threading.Thread(
                target=self._send_webhook_payload,
                args=(alert,),
                daemon=True,
                name="alert-webhook-worker",
            ).start()

        return True

    def _send_webhook_payload(self, alert: Alert) -> None:
        """Sends HTTP POST webhook payload without blocking the caller."""
        if not self._webhook_url:
            return
        payload = {
            "text": f"*{alert.severity.value} Alert - {alert.source}*\n*{alert.title}*\n{alert.message}",
            "severity": alert.severity.value,
            "title": alert.title,
            "source": alert.source,
            "timestamp": alert.timestamp,
            "details": alert.details,
        }
        try:
            req = urllib_request.Request(
                self._webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "HM-Algo-Alerting/2.0"},
                method="POST",
            )
            with urllib_request.urlopen(req, timeout=5.0) as resp:
                if resp.status >= 400:
                    logger.warning("Webhook alert failed with HTTP status %d", resp.status)
        except Exception as e:
            logger.warning("Webhook dispatch error to %s: %s", self._webhook_url, e)

    def get_recent_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns the most recent alerts in chronological reverse (newest first)."""
        with self._history_lock:
            items = list(self._history)
        items.sort(key=lambda a: a.timestamp, reverse=True)
        return [a.to_dict() for a in items[:limit]]

    def clear(self) -> None:
        """Clears alert history and debouncing state (primarily for tests)."""
        with self._history_lock:
            self._history.clear()
            self._last_alert_times.clear()


# Global singleton instance
ALERT_MANAGER = AlertManager()


def dispatch_alert(
    title: str,
    message: str,
    severity: AlertSeverity = AlertSeverity.INFO,
    source: str = "JARVIS",
    details: Optional[Dict[str, Any]] = None,
    event_key: str = "",
    debounce_seconds: float = 60.0,
) -> bool:
    """Convenience helper to dispatch an alert through the global AlertManager."""
    return ALERT_MANAGER.dispatch(
        title=title,
        message=message,
        severity=severity,
        source=source,
        details=details,
        event_key=event_key,
        debounce_seconds=debounce_seconds,
    )
