"""Gemini Live API handler for real-time voice conversations."""

import asyncio
import base64
from typing import Any, AsyncIterator, Callable, Awaitable

import structlog
from google import genai
from google.genai import types

logger = structlog.get_logger(__name__)


class GeminiLiveHandler:
    """Handler for Gemini Live API WebSocket connections.

    Manages real-time audio streaming with Gemini, handling:
    - Audio input (PCM 16kHz)
    - Audio output (PCM 24kHz)
    - Transcripts and interruptions
    """

    def __init__(
        self,
        api_key: str,
        model: str = "models/gemini-2.5-flash-native-audio-preview-12-2025",
        voice: str = "Zephyr",
        system_instruction: str | None = None,
    ):
        """Initialize the Gemini Live handler.

        Args:
            api_key: Gemini API key
            model: Model name for live audio
            voice: Voice for speech synthesis
            system_instruction: System prompt for the conversation
        """
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.system_instruction = system_instruction or "You are a helpful AI assistant."

        self._client: genai.Client | None = None
        self._session: Any = None
        self._is_connected = False
        self._receive_task: asyncio.Task | None = None

        # Callbacks
        self._on_audio: Callable[[bytes], Awaitable[None]] | None = None
        self._on_transcript: Callable[[str, bool], Awaitable[None]] | None = None
        self._on_interrupted: Callable[[], Awaitable[None]] | None = None
        self._on_turn_complete: Callable[[], Awaitable[None]] | None = None

    @property
    def is_connected(self) -> bool:
        """Check if connected to Gemini."""
        return self._is_connected

    def set_callbacks(
        self,
        on_audio: Callable[[bytes], Awaitable[None]] | None = None,
        on_transcript: Callable[[str, bool], Awaitable[None]] | None = None,
        on_interrupted: Callable[[], Awaitable[None]] | None = None,
        on_turn_complete: Callable[[], Awaitable[None]] | None = None,
    ):
        """Set event callbacks.

        Args:
            on_audio: Called with PCM 24kHz audio bytes from Gemini
            on_transcript: Called with (text, is_final) for transcripts
            on_interrupted: Called when user interrupts (barge-in)
            on_turn_complete: Called when Gemini finishes speaking
        """
        self._on_audio = on_audio
        self._on_transcript = on_transcript
        self._on_interrupted = on_interrupted
        self._on_turn_complete = on_turn_complete

    async def connect(self, initial_prompt: str | None = None) -> None:
        """Connect to Gemini Live API.

        Args:
            initial_prompt: Optional initial prompt to set context
        """
        logger.info("Connecting to Gemini Live API", model=self.model)

        self._client = genai.Client(
            http_options={"api_version": "v1beta"},
            api_key=self.api_key,
        )

        # Configure live session
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self.voice
                    )
                )
            ),
            system_instruction=types.Content(
                parts=[types.Part(text=self.system_instruction)]
            ),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=25600,
                sliding_window=types.SlidingWindow(target_tokens=12800),
            ),
        )

        # Connect to live session
        self._session = await self._client.aio.live.connect(
            model=self.model,
            config=config,
        )

        self._is_connected = True
        logger.info("Connected to Gemini Live API")

        # Send initial prompt if provided
        if initial_prompt:
            await self.send_text(initial_prompt)

        # Start receiving responses
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def disconnect(self) -> None:
        """Disconnect from Gemini Live API."""
        logger.info("Disconnecting from Gemini Live API")

        self._is_connected = False

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self._session:
            await self._session.close()
            self._session = None

        self._client = None
        logger.info("Disconnected from Gemini Live API")

    async def send_audio(self, pcm_audio: bytes) -> None:
        """Send audio to Gemini.

        Args:
            pcm_audio: PCM 16-bit 16kHz audio bytes
        """
        if not self._is_connected or not self._session:
            logger.warning("Cannot send audio: not connected")
            return

        # Encode audio as base64 for the API
        audio_b64 = base64.b64encode(pcm_audio).decode("ascii")

        # Send as realtime input
        await self._session.send(
            input=types.LiveClientRealtimeInput(
                media_chunks=[
                    types.Blob(
                        mime_type="audio/pcm;rate=16000",
                        data=audio_b64,
                    )
                ]
            )
        )

    async def send_text(self, text: str) -> None:
        """Send text message to Gemini.

        Args:
            text: Text message to send
        """
        if not self._is_connected or not self._session:
            logger.warning("Cannot send text: not connected")
            return

        logger.debug("Sending text to Gemini", text=text[:100])

        await self._session.send(
            input=types.LiveClientContent(
                turns=[
                    types.Content(
                        role="user",
                        parts=[types.Part(text=text)],
                    )
                ],
                turn_complete=True,
            )
        )

    async def _receive_loop(self) -> None:
        """Receive and process responses from Gemini."""
        try:
            async for response in self._session.receive():
                await self._handle_response(response)
        except asyncio.CancelledError:
            logger.debug("Receive loop cancelled")
            raise
        except Exception as e:
            logger.error("Error in receive loop", error=str(e))
            self._is_connected = False

    async def _handle_response(self, response: Any) -> None:
        """Handle a response from Gemini.

        Args:
            response: Response from Gemini Live API
        """
        # Handle server content (audio/text)
        if hasattr(response, "server_content") and response.server_content:
            content = response.server_content

            # Check for turn complete
            if hasattr(content, "turn_complete") and content.turn_complete:
                logger.debug("Turn complete")
                if self._on_turn_complete:
                    await self._on_turn_complete()

            # Check for interrupted (barge-in)
            if hasattr(content, "interrupted") and content.interrupted:
                logger.debug("User interrupted")
                if self._on_interrupted:
                    await self._on_interrupted()

            # Process model turn parts
            if hasattr(content, "model_turn") and content.model_turn:
                for part in content.model_turn.parts:
                    # Handle audio data
                    if hasattr(part, "inline_data") and part.inline_data:
                        audio_data = part.inline_data.data
                        if isinstance(audio_data, str):
                            audio_bytes = base64.b64decode(audio_data)
                        else:
                            audio_bytes = audio_data

                        if self._on_audio:
                            await self._on_audio(audio_bytes)

                    # Handle text transcript
                    if hasattr(part, "text") and part.text:
                        if self._on_transcript:
                            await self._on_transcript(part.text, True)

        # Handle tool calls if needed (for future expansion)
        if hasattr(response, "tool_call") and response.tool_call:
            logger.debug("Tool call received", tool=response.tool_call)

    async def __aenter__(self) -> "GeminiLiveHandler":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.disconnect()
