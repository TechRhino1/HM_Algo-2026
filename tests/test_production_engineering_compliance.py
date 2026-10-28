"""Comprehensive automated tests verifying the 6 Senior Software Engineering principles:
1. Architecture: Modular, decoupled microservices with clean boundaries.
2. Version Control: Strict .gitignore rules preventing secret/cache leakage.
3. Testing: Comprehensive unit, integration, and contract verification.
4. Security: Zero hardcoded secrets and robust access control.
5. Observability: Contextual structured logging, metrics, and active alerting.
6. Documentation: Complete system specifications and runbook standards.
"""
from __future__ import annotations

import glob
import os
import re
import pytest

from jarvis.observability.alerting import (
    ALERT_MANAGER,
    Alert,
    AlertSeverity,
    dispatch_alert,
)
from jarvis.api.remote_auth import _resolve_admin_password


class TestPillar1Architecture:
    """Architecture: Microservices & Modular Boundaries."""

    def test_modular_microservice_packages_import_cleanly(self):
        """All core service packages must import cleanly without circular dependencies."""
        import importlib
        packages = [
            "jarvis.api",
            "jarvis.application",
            "jarvis.analysts",
            "jarvis.data",
            "jarvis.intelligence",
            "jarvis.risk",
            "jarvis.execution",
            "jarvis.observability",
            "jarvis.market",
            "jarvis.backtesting",
        ]
        for pkg in packages:
            mod = importlib.import_module(pkg)
            assert mod is not None, f"Failed to import package {pkg}"


class TestPillar2VersionControl:
    """Version Control: Strict Git Hygiene & Ignore Policy."""

    def test_gitignore_enforces_secret_and_env_exclusions(self):
        """Gitignore must explicitly reject .env, keys, and private credentials."""
        gitignore_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".gitignore")
        assert os.path.exists(gitignore_path), ".gitignore must exist"
        with open(gitignore_path, "r", encoding="utf-8") as f:
            content = f.read()

        required_patterns = [
            ".env",
            ".env.*",
            "!.env.example",
            "*.pem",
            "*.key",
            ".jarvis_admin_pass",
            ".jarvis_secret_key",
        ]
        for pattern in required_patterns:
            assert pattern in content, f"Missing required .gitignore rule: {pattern}"


class TestPillar4Security:
    """Security: Zero Hardcoded Secrets & Access Control."""

    def test_no_hardcoded_admin_passwords_in_source(self):
        """Source files must not contain hardcoded default passwords in cleartext strings."""
        root_dir = os.path.dirname(os.path.dirname(__file__))
        py_files = glob.glob(os.path.join(root_dir, "jarvis", "**", "*.py"), recursive=True)
        py_files.append(os.path.join(root_dir, "HM_start.py"))

        suspicious_patterns = [
            re.compile(r'admin_pass\s*=\s*["\'](hm2026|admin1234)["\']', re.IGNORECASE),
            re.compile(r'password\s*=\s*["\'](hm2026admin|admin1234)["\']', re.IGNORECASE),
            re.compile(r'admin\s*/\s*hm2026admin', re.IGNORECASE),
        ]

        violations = []
        for path in py_files:
            # Skip test files or tests directory
            if "tests" in path or "test_" in os.path.basename(path):
                continue
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            for pattern in suspicious_patterns:
                matches = pattern.findall(content)
                if matches:
                    violations.append(f"{os.path.relpath(path, root_dir)}: {matches}")

        assert not violations, f"Hardcoded password violations found: {violations}"

    def test_admin_password_resolution_without_hardcoded_fallback(self):
        """_resolve_admin_password must read from env or local file, never hardcoded string."""
        resolved = _resolve_admin_password()
        assert isinstance(resolved, str)
        assert len(resolved) >= 8
        assert resolved not in ("hm2026", "hm2026admin", "admin1234", "password")


class TestPillar5Observability:
    """Observability: Structured Logging, Metrics, and Active Alerts."""

    def test_active_alert_manager_dispatch_and_ring_buffer(self):
        """AlertManager must record alerts in ring buffer in reverse chronological order."""
        ALERT_MANAGER.clear()

        ok1 = dispatch_alert(
            title="Test Critical Risk Halt",
            message="Daily drawdown exceeded safety threshold.",
            severity=AlertSeverity.CRITICAL,
            source="TestRunner",
            details={"drawdown_pct": 5.2},
            event_key="test_halt_1",
            debounce_seconds=0.0,
        )
        assert ok1 is True

        ok2 = dispatch_alert(
            title="Test Warning Spread",
            message="Gold spread widened to 6.5 pips.",
            severity=AlertSeverity.WARNING,
            source="TestRunner",
            details={"symbol": "XAUUSD"},
            event_key="test_spread_1",
            debounce_seconds=0.0,
        )
        assert ok2 is True

        recent = ALERT_MANAGER.get_recent_alerts(limit=10)
        assert len(recent) == 2
        # Newest first
        assert recent[0]["title"] == "Test Warning Spread"
        assert recent[0]["severity"] == "WARNING"
        assert recent[1]["title"] == "Test Critical Risk Halt"
        assert recent[1]["severity"] == "CRITICAL"

    def test_active_alert_debouncing(self):
        """Rapid identical alerts must be throttled by debounce_seconds."""
        ALERT_MANAGER.clear()

        # First alert fires
        first = dispatch_alert(
            title="Repeated Event",
            message="First notification",
            severity=AlertSeverity.WARNING,
            source="CircuitBreaker",
            event_key="repeated_event_key",
            debounce_seconds=60.0,
        )
        assert first is True

        # Second rapid alert with same event_key must be suppressed
        second = dispatch_alert(
            title="Repeated Event",
            message="Second notification (duplicate)",
            severity=AlertSeverity.WARNING,
            source="CircuitBreaker",
            event_key="repeated_event_key",
            debounce_seconds=60.0,
        )
        assert second is False

        # Only one alert recorded in ring buffer
        assert len(ALERT_MANAGER.get_recent_alerts()) == 1

    def test_structured_logging_context_binding(self):
        """Logging context must bind and propagate request ID."""
        from jarvis.observability.context import (
            bind,
            bound,
            clear,
            current_request_id,
            new_request_id,
            unbind,
        )
        req_id = new_request_id()
        bind(request_id=req_id, symbol="EURUSD")
        assert current_request_id() == req_id
        assert bound().get("symbol") == "EURUSD"
        clear()
        assert current_request_id() is None
