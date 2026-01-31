"""Call lifecycle manager for Agentic AI."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

import structlog

from .config import Config
from .audio_bridge import AudioBridge, TranscriptEntry
from ..twilio.client import TwilioClient
from ..twilio.websocket import TwilioMediaStreamHandler
from ..gemini.handler import GeminiLiveHandler
from ..gateway.client import GatewayClient
from ..gateway.messages import (
    CallStartedMessage,
    TranscriptMessage,
    CallEndedMessage,
)

logger = structlog.get_logger(__name__)


@dataclass
class CallSession:
    """Active call session."""

    call_id: str
    call_sid: str
    to_number: str
    prompt: str
    metadata: dict = field(default_factory=dict)
    start_time: datetime = field(default_factory=datetime.now)
    bridge: AudioBridge | None = None
    status: str = "initiating"


class CallManager:
    """Manages call lifecycle and active sessions.

    Orchestrates:
    - Call initiation via Twilio REST API
    - Media stream handling
    - Gemini Live API connections
    - Gateway communication
    """

    def __init__(self, config: Config):
        """Initialize the call manager.

        Args:
            config: Application configuration
        """
        self.config = config
        self._twilio_client: TwilioClient | None = None
        self._gateway_client: GatewayClient | None = None
        self._active_sessions: dict[str, CallSession] = {}
        self._pending_calls: dict[str, dict] = {}  # call_sid -> call info
        self._is_running = False

    @property
    def active_sessions(self) -> dict[str, CallSession]:
        """Get active call sessions."""
        return self._active_sessions

    async def start(self) -> None:
        """Start the call manager."""
        logger.info("Starting call manager")

        # Initialize Twilio client
        self._twilio_client = TwilioClient(
            account_sid=self.config.twilio.account_sid,
            auth_token=self.config.twilio.auth_token,
            from_number=self.config.twilio.from_number,
        )

        # Initialize and connect gateway client
        self._gateway_client = GatewayClient(
            url=self.config.gateway.url,
            max_reconnect_attempts=self.config.gateway.reconnect_max_attempts,
            reconnect_base_delay=self.config.gateway.reconnect_base_delay,
            reconnect_max_delay=self.config.gateway.reconnect_max_delay,
        )
        await self._gateway_client.connect()

        self._is_running = True
        logger.info("Call manager started")

    async def stop(self) -> None:
        """Stop the call manager and clean up all sessions."""
        logger.info("Stopping call manager")
        self._is_running = False

        # Stop all active sessions
        for session in list(self._active_sessions.values()):
            await self._end_session(session)

        # Disconnect gateway
        if self._gateway_client:
            await self._gateway_client.disconnect()

        logger.info("Call manager stopped")

    async def initiate_call(
        self,
        to_number: str,
        prompt: str,
        webhook_base_url: str,
        metadata: dict | None = None,
    ) -> str:
        """Initiate an outbound call.

        Args:
            to_number: Phone number to call
            prompt: System prompt for the AI
            webhook_base_url: Base URL for webhooks (e.g., https://example.com)
            metadata: Optional metadata for the call

        Returns:
            Call ID
        """
        call_id = str(uuid4())

        logger.info(
            "Initiating call",
            call_id=call_id,
            to_number=to_number,
        )

        # Create session
        session = CallSession(
            call_id=call_id,
            call_sid="",  # Will be set after Twilio call
            to_number=to_number,
            prompt=prompt,
            metadata=metadata or {},
        )

        # Construct webhook URLs
        webhook_url = f"{webhook_base_url}{self.config.server.webhook_path}"
        status_url = f"{webhook_base_url}/twilio/status"

        # Initiate Twilio call
        call_sid = self._twilio_client.initiate_call(
            to_number=to_number,
            webhook_url=webhook_url,
            status_callback_url=status_url,
        )

        session.call_sid = call_sid
        session.status = "ringing"

        # Store pending call info for webhook
        self._pending_calls[call_sid] = {
            "call_id": call_id,
            "prompt": prompt,
            "metadata": metadata or {},
        }

        self._active_sessions[call_id] = session

        # Notify gateway
        await self._send_call_started(session)

        return call_id

    def get_pending_call_info(self, call_sid: str) -> dict | None:
        """Get pending call info by call SID.

        Args:
            call_sid: Twilio call SID

        Returns:
            Call info dict or None
        """
        return self._pending_calls.get(call_sid)

    async def handle_call_status(self, call_sid: str, status: str) -> None:
        """Handle call status update from Twilio.

        Args:
            call_sid: Twilio call SID
            status: New call status
        """
        # Find session by call_sid
        session = self._find_session_by_call_sid(call_sid)
        if not session:
            logger.warning("No session found for call status", call_sid=call_sid)
            return

        old_status = session.status
        session.status = status

        logger.info(
            "Call status changed",
            call_id=session.call_id,
            old_status=old_status,
            new_status=status,
        )

        # Handle terminal states
        if status in ("completed", "failed", "busy", "no-answer", "canceled"):
            await self._end_session(session)

    async def handle_media_stream(self, twilio_handler: TwilioMediaStreamHandler) -> None:
        """Handle a new media stream connection.

        Args:
            twilio_handler: Twilio WebSocket handler
        """
        # Wait for start event to get call info
        await asyncio.sleep(0.1)  # Brief wait for start event

        # Create a temporary receive loop to get metadata
        start_received = asyncio.Event()
        session: CallSession | None = None

        async def on_start(metadata):
            nonlocal session
            call_info = self._pending_calls.get(metadata.call_sid)
            if call_info:
                session = self._find_session_by_call_id(call_info["call_id"])
            start_received.set()

        twilio_handler.set_callbacks(on_start=on_start)

        # Start receiving to get the start event
        receive_task = asyncio.create_task(twilio_handler.receive_loop())

        try:
            # Wait for start event with timeout
            await asyncio.wait_for(start_received.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.error("Timeout waiting for stream start")
            receive_task.cancel()
            return

        if not session:
            logger.error("No session found for media stream")
            receive_task.cancel()
            return

        # Cancel the temporary receive task
        receive_task.cancel()
        try:
            await receive_task
        except asyncio.CancelledError:
            pass

        # Create Gemini handler
        gemini_handler = GeminiLiveHandler(
            api_key=self.config.gemini.api_key,
            model=self.config.gemini.model,
            voice=self.config.gemini.voice,
            system_instruction=self.config.gemini.system_instruction,
        )

        # Create audio bridge
        bridge = AudioBridge(
            twilio_handler=twilio_handler,
            gemini_handler=gemini_handler,
            initial_prompt=session.prompt,
        )

        # Set up bridge callbacks
        bridge.set_callbacks(
            on_transcript=lambda t: self._handle_transcript(session, t),
        )

        session.bridge = bridge
        session.status = "in-progress"

        logger.info("Starting audio bridge", call_id=session.call_id)

        try:
            # Start the bridge
            await bridge.start()

            # Wait for bridge to complete (stream closed or error)
            while bridge.is_running:
                await asyncio.sleep(0.5)

        except Exception as e:
            logger.error("Error in audio bridge", error=str(e))
        finally:
            await bridge.stop()

    async def end_call(self, call_id: str) -> None:
        """End an active call.

        Args:
            call_id: Call ID to end
        """
        session = self._active_sessions.get(call_id)
        if not session:
            logger.warning("No session found to end", call_id=call_id)
            return

        logger.info("Ending call", call_id=call_id)

        # End Twilio call
        if session.call_sid and self._twilio_client:
            try:
                self._twilio_client.end_call(session.call_sid)
            except Exception as e:
                logger.error("Failed to end Twilio call", error=str(e))

        await self._end_session(session)

    def _find_session_by_call_sid(self, call_sid: str) -> CallSession | None:
        """Find session by Twilio call SID."""
        for session in self._active_sessions.values():
            if session.call_sid == call_sid:
                return session
        return None

    def _find_session_by_call_id(self, call_id: str) -> CallSession | None:
        """Find session by call ID."""
        return self._active_sessions.get(call_id)

    async def _end_session(self, session: CallSession) -> None:
        """End a call session and clean up.

        Args:
            session: Session to end
        """
        # Stop audio bridge if running
        if session.bridge and session.bridge.is_running:
            await session.bridge.stop()

        # Calculate duration
        duration = (datetime.now() - session.start_time).total_seconds()

        # Get transcript
        transcript = ""
        if session.bridge:
            transcript = session.bridge.get_full_transcript()

        # Send call ended to gateway
        await self._send_call_ended(session, duration, transcript)

        # Clean up
        if session.call_sid in self._pending_calls:
            del self._pending_calls[session.call_sid]

        if session.call_id in self._active_sessions:
            del self._active_sessions[session.call_id]

        logger.info(
            "Session ended",
            call_id=session.call_id,
            duration=duration,
            status=session.status,
        )

    async def _send_call_started(self, session: CallSession) -> None:
        """Send call_started message to gateway."""
        if not self._gateway_client:
            return

        message = CallStartedMessage(
            call_id=session.call_id,
            to_number=session.to_number,
            prompt=session.prompt,
            metadata=session.metadata,
        )

        await self._gateway_client.send_message(message)

    async def _handle_transcript(
        self, session: CallSession, entry: TranscriptEntry
    ) -> None:
        """Handle transcript entry and send to gateway."""
        if not self._gateway_client:
            return

        message = TranscriptMessage(
            call_id=session.call_id,
            speaker=entry.speaker,
            text=entry.text,
            timestamp=entry.timestamp.isoformat(),
            is_final=entry.is_final,
        )

        await self._gateway_client.send_message(message)

    async def _send_call_ended(
        self, session: CallSession, duration: float, transcript: str
    ) -> None:
        """Send call_ended message to gateway."""
        if not self._gateway_client:
            return

        message = CallEndedMessage(
            call_id=session.call_id,
            duration=duration,
            outcome=session.status,
            full_transcript=transcript,
        )

        await self._gateway_client.send_message(message)
