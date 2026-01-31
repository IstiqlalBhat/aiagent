"""Call lifecycle manager for Agentic AI."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

import structlog

from .config import Config
from .audio_bridge import AudioBridge
from ..twilio.client import TwilioClient
from ..twilio.websocket import TwilioMediaStreamHandler
from ..gemini.realtime_handler import GeminiRealtimeHandler
from ..openai.realtime_handler import OpenAIRealtimeHandler
from ..gateway.client import GatewayClient
from ..gateway.messages import (
    CallStartedMessage,
    CallEndedMessage,
)
from ..telegram.direct_client import TelegramDirectClient

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
        self._telegram_client: TelegramDirectClient | None = None
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

        # Initialize Telegram client if enabled
        if self.config.telegram.enabled:
            self._telegram_client = TelegramDirectClient(
                bot_token=self.config.telegram.bot_token,
                chat_id=self.config.telegram.chat_id,
            )
            logger.info("Telegram client initialized", chat_id=self.config.telegram.chat_id)

        # Initialize gateway client (optional, for ClawdBot integration)
        self._gateway_client = None
        self._gateway_task = None
        # Uncomment to enable gateway:
        # self._gateway_client = GatewayClient(
        #     url=self.config.gateway.url,
        #     max_reconnect_attempts=self.config.gateway.reconnect_max_attempts,
        #     reconnect_base_delay=self.config.gateway.reconnect_base_delay,
        #     reconnect_max_delay=self.config.gateway.reconnect_max_delay,
        # )
        # self._gateway_task = asyncio.create_task(self._gateway_client.connect())

        self._is_running = True
        logger.info("Call manager started")

    async def stop(self) -> None:
        """Stop the call manager and clean up all sessions."""
        logger.info("Stopping call manager")
        self._is_running = False

        # Stop all active sessions
        for session in list(self._active_sessions.values()):
            await self._end_session(session)

        # Cancel gateway task and disconnect
        if hasattr(self, '_gateway_task') and self._gateway_task:
            self._gateway_task.cancel()
            try:
                await self._gateway_task
            except asyncio.CancelledError:
                pass
        
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

    async def register_incoming_call(
        self,
        call_sid: str,
        from_number: str,
        to_number: str,
    ) -> str:
        """Register an incoming call and create a session.
        
        Called when someone calls our Twilio number.
        
        Args:
            call_sid: Twilio call SID
            from_number: Caller's phone number
            to_number: Our Twilio number
            
        Returns:
            Call ID for the new session
        """
        call_id = str(uuid4())
        
        logger.info(
            "Registering incoming call",
            call_id=call_id,
            call_sid=call_sid,
            from_number=from_number,
        )
        
        # Use default prompt for incoming calls
        prompt = self.config.gemini.system_instruction or (
            "You are Alchemy, an AI agent created by Istiqlal. "
            "Be helpful, friendly, and assist the caller with whatever they need."
        )
        
        # Create session for incoming call
        session = CallSession(
            call_id=call_id,
            call_sid=call_sid,
            to_number=from_number,  # "to" is the caller for incoming
            prompt=prompt,
            metadata={"direction": "incoming", "original_to": to_number},
        )
        session.status = "ringing"
        
        # Store pending call info for media stream handler
        self._pending_calls[call_sid] = {
            "call_id": call_id,
            "prompt": prompt,
            "metadata": {"direction": "incoming"},
        }
        
        self._active_sessions[call_id] = session
        
        # Notify gateway/telegram
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
        print("=== MEDIA STREAM HANDLER STARTED ===", flush=True)
        
        # Wait for start event - get metadata from first few messages
        session: CallSession | None = None
        received_call_sid = None
        
        # Read messages until we get the start event
        for _ in range(50):  # Max 50 messages to find start
            try:
                msg = await asyncio.wait_for(
                    twilio_handler.websocket.receive_text(),
                    timeout=2.0
                )
                import json
                data = json.loads(msg)
                
                if data.get("event") == "connected":
                    # Mark handler as connected
                    twilio_handler._is_connected = True
                    print("=== TWILIO WEBSOCKET CONNECTED ===", flush=True)
                    
                if data.get("event") == "start":
                    start_data = data.get("start", {})
                    received_call_sid = start_data.get("callSid", "")
                    print(f"Got start event, call_sid: {received_call_sid}", flush=True)
                    
                    # Set metadata on the handler
                    twilio_handler.metadata.stream_sid = start_data.get("streamSid", "")
                    twilio_handler.metadata.call_sid = received_call_sid
                    twilio_handler._is_connected = True
                    
                    call_info = self._pending_calls.get(received_call_sid)
                    if call_info:
                        print(f"Found call info: {call_info['call_id']}", flush=True)
                        session = self._find_session_by_call_id(call_info["call_id"])
                    break
                    
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Error reading message: {e}", flush=True)
                break

        if not session:
            print(f"No session found for call_sid: {received_call_sid}", flush=True)
            return

        print(f"=== Session found: {session.call_id} ===", flush=True)

        # Create agent system instruction
        agent_system_instruction = f"""You are Alchemy, an AI agent created by Istiqlal. You are on a phone call and have REAL capabilities to execute commands.

IMPORTANT: You are NOT a regular chatbot. You are an AI AGENT with real abilities:
- You CAN check and send emails
- You CAN send messages to Telegram, WhatsApp, Discord
- You CAN search the web and get information
- You CAN check your calendar and create events
- You CAN execute commands on the computer

When the user asks you to do something, SAY "I'll do that now" and then describe what you're doing.
For example:
- "Check my emails" → "I'll check your emails now."
- "Send a message to John" → "I'll send that message to John now."
- "Search for restaurants nearby" → "I'm searching for nearby restaurants now..."

User's specific instructions: {session.prompt}

Be helpful, proactive, and ALWAYS act like you can do things. Never say "I cannot" - instead say "I'll do that" or ask for more details if needed.
"""

        # Create initial greeting prompt
        initial_greeting = (
            "You are now connected to a phone call as Alchemy, an AI agent created by Istiqlal. "
            "Greet the caller warmly and let them know you're ready to help with anything - "
            "checking emails, sending messages, searching the web, or any other task."
        )

        # Choose between OpenAI Realtime and Gemini based on config
        use_openai = self.config.openai_realtime and self.config.openai_realtime.enabled
        realtime_handler = None

        if use_openai:
            print(f"=== Creating OpenAI REALTIME handler ===", flush=True)
            realtime_handler = OpenAIRealtimeHandler(
                api_key=self.config.openai_realtime.api_key,
                model=self.config.openai_realtime.model,
                voice=self.config.openai_realtime.voice,
                system_instruction=agent_system_instruction,
            )
            print("=== CONNECTING TO OPENAI REALTIME ===", flush=True)
            await realtime_handler.connect(initial_prompt=initial_greeting)
            print("=== OPENAI REALTIME CONNECTED ===", flush=True)
        else:
            print(f"=== Creating Gemini REALTIME handler with model: {self.config.gemini.model} ===", flush=True)
            realtime_handler = GeminiRealtimeHandler(
                api_key=self.config.gemini.api_key,
                model="models/gemini-2.5-flash-native-audio-preview-12-2025",
                voice=self.config.gemini.voice,
                system_instruction=agent_system_instruction,
            )
            print("=== CONNECTING TO GEMINI REALTIME ===", flush=True)
            await realtime_handler.connect(initial_prompt=initial_greeting)
            print("=== GEMINI REALTIME CONNECTED ===", flush=True)

        # Create audio bridge with conversation brain
        # Note: AudioBridge accepts gemini_handler but works with any handler that has the same interface
        bridge = AudioBridge(
            twilio_handler=twilio_handler,
            gemini_handler=realtime_handler,  # Works with both OpenAI and Gemini handlers
            telegram_client=self._telegram_client,
            telegram_chat_id=self.config.telegram.chat_id if self.config.telegram else "",
            call_id=session.call_id,
            gemini_api_key=self.config.gemini.api_key,
            whisper_api_key=self.config.whisper.api_key if self.config.whisper else "",
            whisper_enabled=self.config.whisper.enabled if self.config.whisper and not use_openai else False,
            use_openai=use_openai,
        )

        session.bridge = bridge
        session.status = "in-progress"

        print("=== STARTING AUDIO BRIDGE ===", flush=True)

        try:
            # Start the bridge
            await bridge.start()
            print("=== AUDIO BRIDGE STARTED ===", flush=True)

            # Wait for bridge to complete (stream closed or error)
            while bridge.is_running:
                await asyncio.sleep(0.5)

            print("=== Audio bridge loop ended ===", flush=True)

        except Exception as e:
            print(f"=== Error in audio bridge: {e} ===", flush=True)
            import traceback
            traceback.print_exc()
        finally:
            await bridge.stop()
            await realtime_handler.disconnect()

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

        # Get transcript summary from the brain (with analyzed intents)
        transcript = ""
        if session.bridge:
            transcript = session.bridge.get_conversation_summary()

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
        """Send call_started message to gateway only."""
        # Note: Don't send to Telegram - only executable commands go there
        # ClawdBot will receive commands from the ConversationBrain

        # Send to gateway (if enabled)
        if self._gateway_client:
            message = CallStartedMessage(
                call_id=session.call_id,
                to_number=session.to_number,
                prompt=session.prompt,
                metadata=session.metadata,
            )
            await self._gateway_client.send_message(message)

    # NOTE: _handle_transcript removed - ConversationBrain now handles all transcripts
    # Transcripts flow: Gemini -> AudioBridge -> ConversationBrain -> Telegram
    # The brain buffers word-by-word transcripts and sends complete sentences with intent analysis

    async def _send_call_ended(
        self, session: CallSession, duration: float, transcript: str
    ) -> None:
        """Send call_ended message to gateway and Telegram summary via brain."""
        # Send concise summary to Telegram via the brain
        if session.bridge and session.bridge.brain:
            session.bridge.brain.send_call_summary(duration)

        # Send to gateway (if enabled)
        if self._gateway_client:
            message = CallEndedMessage(
                call_id=session.call_id,
                duration=duration,
                outcome=session.status,
                full_transcript=transcript,
            )
            await self._gateway_client.send_message(message)
