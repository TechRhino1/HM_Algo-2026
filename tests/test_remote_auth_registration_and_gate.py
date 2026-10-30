"""
Integration and regression tests for User Self-Registration, Admin Approvals,
and Remote Authentication Gating in HM Algo 2.0.
"""
import os
import time
import pytest
from jarvis.api.remote_auth import RemoteAuthEngine, ADMIN_USERNAME
from jarvis.api.server import JarvisRequestHandler

TEST_PASSWORD = "SuperSecretPassword123"

@pytest.fixture(autouse=True)
def clean_users():
    """Ensure clean user state for each test."""
    RemoteAuthEngine._init_default_users()
    yield
    # Clean up any test users created
    for u in ["test_alice", "test_bob", "test_charlie", "test_david"]:
        try:
            RemoteAuthEngine.delete_user(u)
        except Exception:
            pass


def test_user_self_registration_creates_pending_approval():
    ok, msg = RemoteAuthEngine.register_user("test_alice", TEST_PASSWORD, "Alice Trader")
    assert ok is True
    assert "pending administrator approval" in msg.lower()

    users = RemoteAuthEngine.get_all_users()
    alice = next((u for u in users if u["username"] == "test_alice"), None)
    assert alice is not None
    assert alice["status"] == "PENDING_APPROVAL"
    assert alice["full_name"] == "Alice Trader"
    assert "password_hash" not in alice


def test_pending_user_is_blocked_from_logging_in():
    RemoteAuthEngine.register_user("test_bob", TEST_PASSWORD, "Bob Trader")
    user, err = RemoteAuthEngine.verify_credentials("test_bob", TEST_PASSWORD)
    assert user is None
    assert "pending administrator approval" in err.lower()


def test_admin_approval_enables_login_and_session():
    RemoteAuthEngine.register_user("test_charlie", TEST_PASSWORD, "Charlie Trader")
    
    # Approve as TRADER
    ok, msg = RemoteAuthEngine.approve_user("test_charlie", role="TRADER", approved_by="admin")
    assert ok is True
    assert "approved as TRADER" in msg

    # Now login must succeed
    user, err = RemoteAuthEngine.verify_credentials("test_charlie", TEST_PASSWORD)
    assert err == ""
    assert user is not None
    assert user["username"] == "test_charlie"
    assert user["role"] == "TRADER"
    assert user["status"] == "ACTIVE"

    # Create and validate session token
    session = RemoteAuthEngine.create_session_token("test_charlie")
    assert session["token"] is not None
    assert session["role"] == "TRADER"

    validated = RemoteAuthEngine.validate_token(session["token"])
    assert validated is not None
    assert validated["valid"] is True
    assert validated["username"] == "test_charlie"


def test_admin_rejection_blocks_login():
    RemoteAuthEngine.register_user("test_david", TEST_PASSWORD, "David Trader")
    ok, msg = RemoteAuthEngine.reject_user("test_david", rejected_by="admin")
    assert ok is True
    assert "rejected" in msg.lower()

    user, err = RemoteAuthEngine.verify_credentials("test_david", TEST_PASSWORD)
    assert user is None
    assert "declined" in err.lower()


def test_primary_admin_cannot_be_rejected_or_deleted():
    ok, msg = RemoteAuthEngine.reject_user(ADMIN_USERNAME)
    assert ok is False
    assert "cannot reject" in msg.lower()

    ok2, msg2 = RemoteAuthEngine.delete_user(ADMIN_USERNAME)
    assert ok2 is False
    assert "cannot delete" in msg2.lower()


def test_registration_validation_rules():
    # Username too short
    ok, msg = RemoteAuthEngine.register_user("ab", TEST_PASSWORD)
    assert ok is False
    assert "between 3 and 30" in msg

    # Password too short
    ok, msg = RemoteAuthEngine.register_user("valid_user", "short")
    assert ok is False
    assert "at least 8" in msg

    # Reserved username
    ok, msg = RemoteAuthEngine.register_user("admin", TEST_PASSWORD)
    assert ok is False
    assert "reserved" in msg.lower()
