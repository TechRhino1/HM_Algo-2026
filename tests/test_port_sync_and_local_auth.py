"""
Regression tests for local auth fallback, dual-port synchronization, and allowed symbols.
"""
import pytest
from jarvis.config.settings import SETTINGS
from jarvis.api.server import JarvisRequestHandler
from jarvis.api.remote_auth import RemoteAuthEngine


def test_allowed_symbols_includes_gold_and_crypto():
    """Verify that institutional symbols including Gold and Crypto are active."""
    symbols = set(SETTINGS.trading.symbols)
    assert "XAUUSD" in symbols or "GOLD" in symbols
    assert "BTCUSD" in symbols
    assert "ETHUSD" in symbols
    assert "SOLUSD" in symbols
    assert "USDCAD" in symbols


def test_local_request_precedence_over_invalid_token():
    """Verify that an invalid or stale token does not lock out a local request."""
    handler = JarvisRequestHandler.__new__(JarvisRequestHandler)
    handler.client_address = ("127.0.0.1", 12345)
    handler.headers = {
        "Authorization": "Bearer completely_bogus_or_expired_token_12345",
        "Host": "127.0.0.1:8501"
    }
    
    assert handler._is_local_request() is True
    user = handler._get_auth_user()
    assert user is not None
    assert user.get("role") == "ADMIN"
    assert user.get("username") == "admin"


def test_remote_request_with_invalid_token_is_rejected():
    """Verify that remote callers with invalid tokens are still rejected."""
    handler = JarvisRequestHandler.__new__(JarvisRequestHandler)
    handler.client_address = ("192.168.1.50", 12345)
    handler.headers = {
        "Authorization": "Bearer completely_bogus_token",
        "Host": "192.168.1.10:8501"
    }
    
    assert handler._is_local_request() is False
    user = handler._get_auth_user()
    assert user is None
