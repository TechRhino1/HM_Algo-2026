"""
Tests verifying that remote access strictly requires authentication
while localhost (127.0.0.1:8501) operates with zero-friction automatic admin access.
"""
import json
import pytest
from unittest.mock import MagicMock
from jarvis.api.server import JarvisRequestHandler
from jarvis.api.remote_auth import RemoteAuthEngine


def make_mock_handler(client_ip="203.0.113.195", headers=None, path="/api/telemetry_state"):
    """Creates a mock JarvisRequestHandler with specified headers and client address."""
    handler = object.__new__(JarvisRequestHandler)
    handler.client_address = (client_ip, 54321)
    handler.headers = headers or {}
    handler.path = path
    handler.responses = []
    
    def _send_json(data, status_code=200, cookies=None):
        handler.responses.append((status_code, data))
    
    handler._send_json = _send_json
    return handler


def test_remote_request_without_token_is_blocked_on_telemetry():
    handler = make_mock_handler(
        client_ip="203.0.113.50",
        headers={"Host": "telemetry.trycloudflare.com", "X-Forwarded-For": "203.0.113.50"},
        path="/api/telemetry_state"
    )
    assert handler._is_local_request() is False
    assert handler._check_auth() is False


def test_localhost_request_has_zero_friction_auth():
    handler = make_mock_handler(
        client_ip="127.0.0.1",
        headers={"Host": "127.0.0.1:8501"},
        path="/api/telemetry_state"
    )
    assert handler._is_local_request() is True
    assert handler._check_auth() is True
    user = handler._get_auth_user()
    assert user is not None
    assert user["username"] == "admin"
    assert user["role"] == "ADMIN"


def test_remote_request_with_valid_token_passes_auth():
    RemoteAuthEngine._init_default_users()
    session = RemoteAuthEngine.create_session_token("admin")
    token = session["token"]

    handler = make_mock_handler(
        client_ip="203.0.113.50",
        headers={
            "Host": "telemetry.trycloudflare.com",
            "X-Forwarded-For": "203.0.113.50",
            "Authorization": f"Bearer {token}"
        },
        path="/api/telemetry_state"
    )
    assert handler._is_local_request() is False
    assert handler._check_auth() is True
    user = handler._get_auth_user()
    assert user is not None
    assert user["username"] == "admin"


def test_remote_request_for_admin_endpoint_requires_admin_role():
    # Non-admin user
    RemoteAuthEngine.register_user("gate_trader", "SafePassword123", "Gate Trader")
    RemoteAuthEngine.approve_user("gate_trader", role="TRADER", approved_by="admin")
    session = RemoteAuthEngine.create_session_token("gate_trader")
    token = session["token"]

    handler = make_mock_handler(
        client_ip="203.0.113.50",
        headers={
            "Host": "telemetry.trycloudflare.com",
            "X-Forwarded-For": "203.0.113.50",
            "Authorization": f"Bearer {token}"
        },
        path="/api/admin/users"
    )
    ok, user = handler._require_role("ADMIN")
    assert ok is False
    assert len(handler.responses) == 1
    status_code, data = handler.responses[0]
    assert status_code == 403
    assert "not permitted" in data.get("error", "").lower()

    # Cleanup
    RemoteAuthEngine.delete_user("gate_trader")
