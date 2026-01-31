"""Message types for OpenClaw Gateway communication."""

from dataclasses import dataclass, field, asdict
from typing import Any, Literal
from datetime import datetime


@dataclass
class GatewayMessage:
    """Base class for gateway messages."""

    message_type: str

    def to_dict(self) -> dict:
        """Convert message to dictionary."""
        return asdict(self)


@dataclass
class CallStartedMessage(GatewayMessage):
    """Message sent when a call is initiated."""

    message_type: str = field(default="call_started", init=False)
    call_id: str = ""
    to_number: str = ""
    prompt: str = ""
    metadata: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class TranscriptMessage(GatewayMessage):
    """Message sent for transcript updates."""

    message_type: str = field(default="transcript", init=False)
    call_id: str = ""
    speaker: str = ""  # "user" or "assistant"
    text: str = ""
    timestamp: str = ""
    is_final: bool = True


@dataclass
class StructuredDataMessage(GatewayMessage):
    """Message sent for structured data extracted from conversation."""

    message_type: str = field(default="structured_data", init=False)
    call_id: str = ""
    intent: str = ""
    entities: dict = field(default_factory=dict)
    summary: str = ""
    confidence: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ActionMessage(GatewayMessage):
    """Message sent when an action should be taken."""

    message_type: str = field(default="action", init=False)
    call_id: str = ""
    action_type: str = ""
    parameters: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class CallEndedMessage(GatewayMessage):
    """Message sent when a call ends."""

    message_type: str = field(default="call_ended", init=False)
    call_id: str = ""
    duration: float = 0.0  # Duration in seconds
    outcome: str = ""  # "completed", "failed", "no-answer", etc.
    full_transcript: str = ""
    summary: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class HeartbeatMessage(GatewayMessage):
    """Heartbeat message for connection keepalive."""

    message_type: str = field(default="heartbeat", init=False)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ErrorMessage(GatewayMessage):
    """Error message for reporting issues."""

    message_type: str = field(default="error", init=False)
    call_id: str = ""
    error_code: str = ""
    error_message: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
