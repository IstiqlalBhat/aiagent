"""Twilio Media Streams WebSocket handler."""

import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

import structlog
from fastapi import WebSocket

logger = structlog.get_logger(__name__)


class MediaStreamEvent(Enum):
    """Twilio Media Stream event types."""

    CONNECTED = "connected"
    START = "start"
    MEDIA = "media"
    STOP = "stop"
    MARK = "mark"


@dataclass
class StreamMetadata:
    """Metadata from Twilio Media Stream start event."""

    stream_sid: str = ""
    call_sid: str = ""
    account_sid: str = ""
    tracks: list[str] = field(default_factory=list)
    custom_parameters: dict = field(default_factory=dict)


class TwilioMediaStreamHandler:
    """Handler for Twilio Media Streams WebSocket protocol.

    Handles incoming events:
    - connected: WebSocket connected
    - start: Stream started with metadata
    - media: Audio data
    - stop: Stream ended
    - mark: Playback position marker

    Sends outgoing messages:
    - media: Audio to play
    - clear: Clear audio buffer (for interrupts)
    - mark: Set playback marker
    """

    def __init__(self, websocket: WebSocket):
        """Initialize the handler.

        Args:
            websocket: FastAPI WebSocket connection
        """
        self.websocket = websocket
        self.metadata = StreamMetadata()
        self._is_connected = False
        self._sequence_number = 0

        # Callbacks
        self._on_audio: Callable[[str], Awaitable[None]] | None = None
        self._on_start: Callable[[StreamMetadata], Awaitable[None]] | None = None
        self._on_stop: Callable[[], Awaitable[None]] | None = None
        self._on_mark: Callable[[str], Awaitable[None]] | None = None

    @property
    def is_connected(self) -> bool:
        """Check if stream is connected."""
        return self._is_connected

    @property
    def stream_sid(self) -> str:
        """Get the stream SID."""
        return self.metadata.stream_sid

    @property
    def call_sid(self) -> str:
        """Get the call SID."""
        return self.metadata.call_sid

    def set_callbacks(
        self,
        on_audio: Callable[[str], Awaitable[None]] | None = None,
        on_start: Callable[[StreamMetadata], Awaitable[None]] | None = None,
        on_stop: Callable[[], Awaitable[None]] | None = None,
        on_mark: Callable[[str], Awaitable[None]] | None = None,
    ):
        """Set event callbacks.

        Args:
            on_audio: Called with base64 mulaw audio payload
            on_start: Called when stream starts with metadata
            on_stop: Called when stream stops
            on_mark: Called when mark event received with mark name
        """
        self._on_audio = on_audio
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_mark = on_mark

    async def accept(self) -> None:
        """Accept the WebSocket connection."""
        await self.websocket.accept()
        logger.info("Twilio WebSocket accepted")

    async def receive_loop(self) -> None:
        """Main loop to receive and process Twilio events."""
        try:
            while True:
                message = await self.websocket.receive_text()
                await self._handle_message(message)
        except Exception as e:
            logger.info("WebSocket connection closed", reason=str(e))
            self._is_connected = False

    async def _handle_message(self, message: str) -> None:
        """Handle a message from Twilio.

        Args:
            message: JSON message string
        """
        try:
            data = json.loads(message)
            event_type = data.get("event")

            if event_type == MediaStreamEvent.CONNECTED.value:
                await self._handle_connected(data)
            elif event_type == MediaStreamEvent.START.value:
                await self._handle_start(data)
            elif event_type == MediaStreamEvent.MEDIA.value:
                await self._handle_media(data)
            elif event_type == MediaStreamEvent.STOP.value:
                await self._handle_stop(data)
            elif event_type == MediaStreamEvent.MARK.value:
                await self._handle_mark(data)
            else:
                logger.debug("Unknown event type", event=event_type)

        except json.JSONDecodeError:
            logger.error("Failed to parse message", message=message[:100])

    async def _handle_connected(self, data: dict) -> None:
        """Handle connected event."""
        logger.info("Twilio stream connected", protocol=data.get("protocol"))
        self._is_connected = True

    async def _handle_start(self, data: dict) -> None:
        """Handle start event with stream metadata."""
        start_data = data.get("start", {})

        self.metadata = StreamMetadata(
            stream_sid=start_data.get("streamSid", ""),
            call_sid=start_data.get("callSid", ""),
            account_sid=start_data.get("accountSid", ""),
            tracks=start_data.get("tracks", []),
            custom_parameters=start_data.get("customParameters", {}),
        )

        logger.info(
            "Stream started",
            stream_sid=self.metadata.stream_sid,
            call_sid=self.metadata.call_sid,
            tracks=self.metadata.tracks,
        )

        if self._on_start:
            await self._on_start(self.metadata)

    async def _handle_media(self, data: dict) -> None:
        """Handle media event with audio data."""
        media_data = data.get("media", {})
        payload = media_data.get("payload", "")

        if payload and self._on_audio:
            await self._on_audio(payload)

    async def _handle_stop(self, data: dict) -> None:
        """Handle stop event."""
        logger.info("Stream stopped", stream_sid=self.metadata.stream_sid)
        self._is_connected = False

        if self._on_stop:
            await self._on_stop()

    async def _handle_mark(self, data: dict) -> None:
        """Handle mark event."""
        mark_data = data.get("mark", {})
        mark_name = mark_data.get("name", "")

        logger.debug("Mark received", name=mark_name)

        if self._on_mark:
            await self._on_mark(mark_name)

    async def send_audio(self, payload: str) -> None:
        """Send audio to Twilio.

        Args:
            payload: Base64-encoded mulaw audio
        """
        if not self._is_connected:
            return

        message = {
            "event": "media",
            "streamSid": self.metadata.stream_sid,
            "media": {"payload": payload},
        }

        await self.websocket.send_text(json.dumps(message))

    async def send_clear(self) -> None:
        """Clear Twilio's audio buffer (for handling interrupts)."""
        if not self._is_connected:
            return

        message = {
            "event": "clear",
            "streamSid": self.metadata.stream_sid,
        }

        await self.websocket.send_text(json.dumps(message))
        logger.debug("Sent clear event")

    async def send_mark(self, name: str) -> None:
        """Send a mark event to track playback position.

        Args:
            name: Mark name for identification
        """
        if not self._is_connected:
            return

        message = {
            "event": "mark",
            "streamSid": self.metadata.stream_sid,
            "mark": {"name": name},
        }

        await self.websocket.send_text(json.dumps(message))

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._is_connected = False
        try:
            await self.websocket.close()
        except Exception:
            pass
