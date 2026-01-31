"""Gemini Realtime API handler - improved bidirectional audio."""

import asyncio
from typing import Callable, Awaitable
import structlog

from google import genai
from google.genai import types

logger = structlog.get_logger(__name__)


class GeminiRealtimeHandler:
    """Handles bidirectional audio streaming with Gemini Live API.

    This is the improved version that works properly with continuous audio.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "models/gemini-2.5-flash-native-audio-preview-12-2025",
        voice: str = "Zephyr",
        system_instruction: str = "You are a helpful AI assistant.",
    ):
        """Initialize Gemini realtime handler.

        Args:
            api_key: Gemini API key
            model: Model to use
            voice: Voice name
            system_instruction: System prompt
        """
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.system_instruction = system_instruction

        self.client = genai.Client(
            http_options={"api_version": "v1beta"},
            api_key=api_key,
        )

        self.config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
            system_instruction=system_instruction,
            # Enable transcription of AI's audio output
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Enable transcription of user's audio input
            input_audio_transcription=types.AudioTranscriptionConfig(),
        )

        self.session = None
        self._context_manager = None  # Store the context manager to keep connection alive
        self.audio_in_queue = None
        self.audio_out_queue = None

        # Callbacks
        self._on_audio: Callable[[bytes], Awaitable[None]] | None = None
        self._on_transcript: Callable[[str, bool], Awaitable[None]] | None = None
        self._on_user_transcript: Callable[[str], Awaitable[None]] | None = None
        self._on_turn_complete: Callable[[], Awaitable[None]] | None = None
        self._on_user_turn_complete: Callable[[], Awaitable[None]] | None = None

        self._is_running = False
        self._tasks = []
        self._user_spoke = False  # Track if user spoke since last Gemini response
        self._use_external_stt = False  # When True, ignore input_transcription (using Whisper)

    def set_callbacks(
        self,
        on_audio: Callable[[bytes], Awaitable[None]] | None = None,
        on_transcript: Callable[[str, bool], Awaitable[None]] | None = None,
        on_user_transcript: Callable[[str], Awaitable[None]] | None = None,
        on_turn_complete: Callable[[], Awaitable[None]] | None = None,
        on_user_turn_complete: Callable[[], Awaitable[None]] | None = None,
    ):
        """Set event callbacks.

        If on_user_transcript is None, input transcription will be ignored
        (useful when using external STT like Whisper).
        """
        self._on_audio = on_audio
        self._on_transcript = on_transcript
        self._on_user_transcript = on_user_transcript
        self._on_turn_complete = on_turn_complete
        self._on_user_turn_complete = on_user_turn_complete

        # Track if we're using external STT (Whisper)
        self._use_external_stt = on_user_transcript is None

    async def connect(self, initial_prompt: str | None = None):
        """Connect to Gemini Live API."""
        logger.info("Connecting to Gemini realtime", model=self.model)

        # Store the context manager to prevent it from being garbage collected
        self._context_manager = self.client.aio.live.connect(
            model=self.model,
            config=self.config
        )
        self.session = await self._context_manager.__aenter__()

        self.audio_in_queue = asyncio.Queue()  # Audio FROM Gemini
        self.audio_out_queue = asyncio.Queue()  # Audio TO Gemini - unbounded to prevent drops

        logger.info("Connected to Gemini realtime")

        # Start processing tasks
        self._is_running = True
        self._tasks = [
            asyncio.create_task(self._receive_from_gemini()),
            asyncio.create_task(self._send_to_gemini()),
        ]

        # Send initial prompt if provided
        if initial_prompt:
            print(f"=== GEMINI: Sending initial prompt: {initial_prompt[:100]}... ===", flush=True)
            await self.session.send(input=initial_prompt, end_of_turn=True)
            print("=== GEMINI: Initial prompt sent! ===", flush=True)
            logger.info("Sent initial prompt to Gemini")

    async def disconnect(self):
        """Disconnect from Gemini."""
        logger.info("Disconnecting from Gemini realtime")
        self._is_running = False

        # Cancel tasks
        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Close session using the stored context manager
        if self._context_manager:
            try:
                await self._context_manager.__aexit__(None, None, None)
            except Exception as e:
                print(f"=== GEMINI: Error closing connection: {e} ===", flush=True)
            self._context_manager = None
            self.session = None

        logger.info("Disconnected from Gemini realtime")

    async def send_audio(self, audio_data: bytes):
        """Send audio data to Gemini.

        Args:
            audio_data: PCM audio bytes
        """
        if not audio_data or len(audio_data) == 0:
            return

        await self.audio_out_queue.put({
            "data": audio_data,
            "mime_type": "audio/pcm"
        })

        # Log queue size periodically to detect backpressure
        qsize = self.audio_out_queue.qsize()
        if qsize > 20 and qsize % 10 == 0:
            print(f"=== GEMINI: Audio queue building up: {qsize} chunks ===", flush=True)

    async def _send_to_gemini(self):
        """Background task to send audio to Gemini with batching."""
        chunks_sent = 0
        try:
            while self._is_running:
                # Get first chunk (blocking)
                msg = await self.audio_out_queue.get()

                # Batch multiple chunks if available (reduces API calls)
                audio_buffer = msg["data"]
                batch_count = 1

                # Grab more chunks if available (non-blocking), up to 10
                while batch_count < 10:
                    try:
                        extra = self.audio_out_queue.get_nowait()
                        audio_buffer += extra["data"]
                        batch_count += 1
                    except asyncio.QueueEmpty:
                        break

                # Send batched audio
                await self.session.send(input={
                    "data": audio_buffer,
                    "mime_type": "audio/pcm"
                })

                chunks_sent += batch_count
                if chunks_sent % 100 == 0:
                    print(f"=== GEMINI: Sent {chunks_sent} audio chunks ===", flush=True)

        except asyncio.CancelledError:
            print(f"=== GEMINI: Send task cancelled after {chunks_sent} chunks ===", flush=True)
        except Exception as e:
            logger.error("Error sending to Gemini", error=str(e))
            print(f"=== GEMINI SEND ERROR: {e} ===", flush=True)

    async def _receive_from_gemini(self):
        """Background task to receive audio and text from Gemini."""
        audio_chunk_count = 0
        try:
            print("=== GEMINI: Waiting for response... ===", flush=True)
            while self._is_running:
                turn = self.session.receive()
                async for response in turn:
                    # Handle audio data
                    if data := response.data:
                        audio_chunk_count += 1
                        if audio_chunk_count == 1:
                            print(f"=== GEMINI: First audio chunk received! {len(data)} bytes ===", flush=True)
                        elif audio_chunk_count % 50 == 0:
                            print(f"=== GEMINI: Received {audio_chunk_count} audio chunks ===", flush=True)
                        self.audio_in_queue.put_nowait(data)
                        if self._on_audio:
                            await self._on_audio(data)

                    # Handle text transcript (direct text response)
                    if text := response.text:
                        print(f"=== GEMINI TEXT: {text[:100]}... ===", flush=True)
                        logger.debug("Gemini transcript", text=text[:100])
                        if self._on_transcript:
                            await self._on_transcript(text, True)
                    
                    # Handle transcription from server_content (for native audio models)
                    if hasattr(response, 'server_content') and response.server_content:
                        sc = response.server_content

                        # IMPORTANT: Process input_transcription FIRST (user speech)
                        # before checking output_transcription (which triggers user turn flush)
                        if hasattr(sc, 'input_transcription') and sc.input_transcription:
                            transcript_text = sc.input_transcription.text if hasattr(sc.input_transcription, 'text') else str(sc.input_transcription)
                            if transcript_text:
                                self._user_spoke = True  # Mark that user spoke
                                print(f"=== USER TRANSCRIPT: {transcript_text[:100]}... ===", flush=True)
                                if self._on_user_transcript:
                                    await self._on_user_transcript(transcript_text)

                        # Then check for output transcription (Gemini speaking)
                        if hasattr(sc, 'output_transcription') and sc.output_transcription:
                            transcript_text = sc.output_transcription.text if hasattr(sc.output_transcription, 'text') else str(sc.output_transcription)
                            if transcript_text:
                                # If user was speaking, their turn is now complete
                                if self._user_spoke and self._on_user_turn_complete:
                                    print(f"=== USER TURN COMPLETE (Gemini responding) ===", flush=True)
                                    await self._on_user_turn_complete()
                                    self._user_spoke = False

                                print(f"=== GEMINI OUTPUT TRANSCRIPT: {transcript_text[:100]}... ===", flush=True)
                                if self._on_transcript:
                                    await self._on_transcript(transcript_text, True)

                # Gemini turn complete - flush assistant transcript
                print(f"=== GEMINI: Turn complete (total audio chunks: {audio_chunk_count}) ===", flush=True)
                if self._on_turn_complete:
                    await self._on_turn_complete()

        except asyncio.CancelledError:
            print(f"=== GEMINI: Receive task cancelled ===", flush=True)
        except Exception as e:
            logger.error("Error receiving from Gemini", error=str(e))
            print(f"=== GEMINI ERROR: {e} ===", flush=True)

    async def get_audio(self) -> bytes:
        """Get audio chunk from Gemini.

        Returns:
            Audio bytes (PCM 24kHz)
        """
        return await self.audio_in_queue.get()

    async def send_text(self, text: str, end_of_turn: bool = True):
        """Send text to Gemini to respond to.

        This is used to inject ClawdBot responses for Gemini to speak.

        Args:
            text: Text for Gemini to respond to
            end_of_turn: Whether this ends the turn (triggers response)
        """
        if self.session and text:
            print(f"=== GEMINI: Injecting text: {text[:100]}... ===", flush=True)
            await self.session.send(input=text, end_of_turn=end_of_turn)
            print(f"=== GEMINI: Text injected, waiting for response ===", flush=True)
