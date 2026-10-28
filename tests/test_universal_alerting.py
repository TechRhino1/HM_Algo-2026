"""
Test Suite: Universal Multi-Channel Webhook Alerting (Phase 5).
Verifies that AlertManager auto-formats payloads for Discord ("content"),
Telegram ("chat_id" & "text"), and Slack/generic ("text" & metadata).
"""
import pytest
import os
from jarvis.observability.alerting import Alert, AlertSeverity, AlertManager


def test_discord_payload_formatting():
    alert = Alert(
        title="Obstacle Collision Ahead",
        message="Order canceled: supply ceiling 1.5R away",
        severity=AlertSeverity.WARNING,
        source="DecisionEngine",
    )
    discord_url = "https://discord.com/api/webhooks/123456789/abcdef"
    payload = AlertManager.format_webhook_payload(discord_url, alert)
    
    assert "content" in payload
    assert "**[WARNING] [DecisionEngine] Obstacle Collision Ahead**" in payload["content"]
    assert "supply ceiling" in payload["content"]


def test_telegram_payload_formatting(monkeypatch):
    monkeypatch.setenv("JARVIS_TELEGRAM_CHAT_ID", "-100987654321")
    alert = Alert(
        title="Circuit Breaker Tripped",
        message="5 consecutive loss limit reached across portfolio",
        severity=AlertSeverity.CRITICAL,
        source="CircuitBreaker",
    )
    tg_url = "https://api.telegram.org/bot12345:token/sendMessage"
    payload = AlertManager.format_webhook_payload(tg_url, alert)
    
    assert payload["chat_id"] == "-100987654321"
    assert "text" in payload
    assert "CRITICAL - CircuitBreaker: Circuit Breaker Tripped" in payload["text"]
    assert payload.get("parse_mode") == "Markdown"


def test_slack_and_generic_payload_formatting():
    alert = Alert(
        title="Trade Executed",
        message="BUY 0.10 lots XAUUSD @ 2025.50",
        severity=AlertSeverity.INFO,
        source="ExecutionEngine",
    )
    slack_url = "https://hooks.slack.com/services/T00/B00/X00"
    payload = AlertManager.format_webhook_payload(slack_url, alert)
    
    assert "text" in payload
    assert payload["severity"] == "INFO"
    assert payload["source"] == "ExecutionEngine"
    assert payload["title"] == "Trade Executed"
