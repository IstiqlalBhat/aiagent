"""Tests for gateway message types."""

import pytest
from datetime import datetime

from agenticai.gateway.messages import (
    CallStartedMessage,
    TranscriptMessage,
    StructuredDataMessage,
    ActionMessage,
    CallEndedMessage,
    HeartbeatMessage,
    ErrorMessage,
)


class TestGatewayMessages:
    """Tests for gateway message types."""

    def test_call_started_message(self):
        """Test CallStartedMessage creation and serialization."""
        msg = CallStartedMessage(
            call_id="test-123",
            to_number="+15551234567",
            prompt="Test prompt",
            metadata={"key": "value"},
        )

        assert msg.message_type == "call_started"
        assert msg.call_id == "test-123"
        assert msg.to_number == "+15551234567"

        data = msg.to_dict()
        assert data["message_type"] == "call_started"
        assert data["call_id"] == "test-123"
        assert "timestamp" in data

    def test_transcript_message(self):
        """Test TranscriptMessage creation and serialization."""
        msg = TranscriptMessage(
            call_id="test-123",
            speaker="assistant",
            text="Hello, how can I help you?",
            timestamp="2024-01-15T10:30:00",
            is_final=True,
        )

        assert msg.message_type == "transcript"
        assert msg.speaker == "assistant"
        assert msg.is_final is True

        data = msg.to_dict()
        assert data["message_type"] == "transcript"
        assert data["text"] == "Hello, how can I help you?"

    def test_structured_data_message(self):
        """Test StructuredDataMessage creation and serialization."""
        msg = StructuredDataMessage(
            call_id="test-123",
            intent="schedule_appointment",
            entities={"date": "2024-01-20", "time": "14:00"},
            summary="User wants to schedule an appointment",
            confidence=0.95,
        )

        assert msg.message_type == "structured_data"
        assert msg.intent == "schedule_appointment"
        assert msg.confidence == 0.95

        data = msg.to_dict()
        assert data["entities"]["date"] == "2024-01-20"

    def test_action_message(self):
        """Test ActionMessage creation and serialization."""
        msg = ActionMessage(
            call_id="test-123",
            action_type="send_email",
            parameters={"to": "user@example.com", "subject": "Confirmation"},
        )

        assert msg.message_type == "action"
        assert msg.action_type == "send_email"

        data = msg.to_dict()
        assert data["parameters"]["to"] == "user@example.com"

    def test_call_ended_message(self):
        """Test CallEndedMessage creation and serialization."""
        msg = CallEndedMessage(
            call_id="test-123",
            duration=125.5,
            outcome="completed",
            full_transcript="User: Hi\nAssistant: Hello!",
            summary="Brief greeting call",
        )

        assert msg.message_type == "call_ended"
        assert msg.duration == 125.5
        assert msg.outcome == "completed"

        data = msg.to_dict()
        assert data["duration"] == 125.5

    def test_heartbeat_message(self):
        """Test HeartbeatMessage creation and serialization."""
        msg = HeartbeatMessage()

        assert msg.message_type == "heartbeat"
        assert "timestamp" in msg.to_dict()

    def test_error_message(self):
        """Test ErrorMessage creation and serialization."""
        msg = ErrorMessage(
            call_id="test-123",
            error_code="CONNECTION_FAILED",
            error_message="Failed to connect to Gemini API",
        )

        assert msg.message_type == "error"
        assert msg.error_code == "CONNECTION_FAILED"

        data = msg.to_dict()
        assert data["error_message"] == "Failed to connect to Gemini API"

    def test_message_has_timestamp(self):
        """Test that messages automatically get timestamps."""
        msg = CallStartedMessage(
            call_id="test-123",
            to_number="+15551234567",
            prompt="Test",
        )

        assert msg.timestamp is not None
        assert len(msg.timestamp) > 0

    def test_message_with_empty_metadata(self):
        """Test message with empty metadata dict."""
        msg = CallStartedMessage(
            call_id="test-123",
            to_number="+15551234567",
            prompt="Test",
        )

        assert msg.metadata == {}
        data = msg.to_dict()
        assert data["metadata"] == {}
