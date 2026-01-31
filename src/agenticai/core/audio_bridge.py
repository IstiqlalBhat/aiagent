"""Bidirectional audio bridge between Twilio and Gemini."""

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable
from datetime import datetime

import structlog

from ..audio.converter import AudioConverter
from ..twilio.websocket import TwilioMediaStreamHandler, StreamMetadata
from ..gemini.handler import GeminiLiveHandler

logger = structlog.get_logger(__name__)


@dataclass
class TranscriptEntry:
    """A transcript entry."""

    speaker: str  # "user" or "assistant"
    text: str
    timestamp: datetime
    is_final: bool = True


@dataclass
class AudioBridgeStats:
    """Statistics for the audio bridge."""

    twilio_packets_received: int = 0
    twilio_packets_sent: int = 0
    gemini_packets_received: int = 0
    gemini_packets_sent: int = 0
    interruptions: int = 0
    start_time: datetime = field(default_factory=datetime.now)


class AudioBridge:
    """Bidirectional audio bridge between Twilio and Gemini.

    Handles:
    - Audio format conversion (mulaw <-> PCM, resampling)
    - Buffering with async queues
    - Interrupt handling (barge-in)
    - Transcript collection
    """

    def __init__(
        self,
        twilio_handler: TwilioMediaStreamHandler,
        gemini_handler: GeminiLiveHandler,
        initial_prompt: str | None = None,
    ):
        """Initialize the audio bridge.

        Args:
            twilio_handler: Twilio WebSocket handler
            gemini_handler: Gemini Live API handler
            initial_prompt: Optional prompt to start the conversation
        """
        self.twilio = twilio_handler
        self.gemini = gemini_handler
        self.initial_prompt = initial_prompt

        self._converter = AudioConverter()
        self._is_running = False
        self._stats = AudioBridgeStats()
        self._transcripts: list[TranscriptEntry] = []

        # Audio buffers
        self._twilio_to_gemini_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._gemini_to_twilio_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)

        # Tasks
        self._tasks: list[asyncio.Task] = []

        # Callbacks
        self._on_transcript: Callable[[TranscriptEntry], Awaitable[None]] | None = None
        self._on_turn_complete: Callable[[], Awaitable[None]] | None = None

    @property
    def is_running(self) -> bool:
        """Check if the bridge is running."""
        return self._is_running

    @property
    def stats(self) -> AudioBridgeStats:
        """Get bridge statistics."""
        return self._stats

    @property
    def transcripts(self) -> list[TranscriptEntry]:
        """Get collected transcripts."""
        return self._transcripts

    def set_callbacks(
        self,
        on_transcript: Callable[[TranscriptEntry], Awaitable[None]] | None = None,
        on_turn_complete: Callable[[], Awaitable[None]] | None = None,
    ):
        """Set event callbacks.

        Args:
            on_transcript: Called when a transcript is received
            on_turn_complete: Called when assistant finishes speaking
        """
        self._on_transcript = on_transcript
        self._on_turn_complete = on_turn_complete

    async def start(self) -> None:
        """Start the audio bridge."""
        if self._is_running:
            return

        logger.info("Starting audio bridge")
        self._is_running = True
        self._stats = AudioBridgeStats()

        # Set up Twilio callbacks
        self.twilio.set_callbacks(
            on_audio=self._handle_twilio_audio,
            on_start=self._handle_twilio_start,
            on_stop=self._handle_twilio_stop,
        )

        # Set up Gemini callbacks
        self.gemini.set_callbacks(
            on_audio=self._handle_gemini_audio,
            on_transcript=self._handle_gemini_transcript,
            on_interrupted=self._handle_gemini_interrupted,
            on_turn_complete=self._handle_gemini_turn_complete,
        )

        # Connect to Gemini
        await self.gemini.connect(self.initial_prompt)

        # Start processing tasks
        self._tasks = [
            asyncio.create_task(self._process_twilio_to_gemini()),
            asyncio.create_task(self._process_gemini_to_twilio()),
            asyncio.create_task(self.twilio.receive_loop()),
        ]

        logger.info("Audio bridge started")

    async def stop(self) -> None:
        """Stop the audio bridge."""
        if not self._is_running:
            return

        logger.info("Stopping audio bridge")
        self._is_running = False

        # Cancel tasks
        for task in self._tasks:
            task.cancel()

        # Wait for tasks to complete
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Disconnect from Gemini
        await self.gemini.disconnect()

        # Close Twilio connection
        await self.twilio.close()

        logger.info(
            "Audio bridge stopped",
            duration_seconds=(datetime.now() - self._stats.start_time).total_seconds(),
            stats=self._stats,
        )

    async def _handle_twilio_audio(self, payload: str) -> None:
        """Handle audio from Twilio.

        Args:
            payload: Base64-encoded mulaw audio
        """
        self._stats.twilio_packets_received += 1

        # Convert Twilio audio to Gemini format
        pcm_audio = self._converter.twilio_to_gemini(payload)

        # Queue for sending to Gemini
        try:
            self._twilio_to_gemini_queue.put_nowait(pcm_audio)
        except asyncio.QueueFull:
            # Drop oldest packet if queue is full
            try:
                self._twilio_to_gemini_queue.get_nowait()
                self._twilio_to_gemini_queue.put_nowait(pcm_audio)
            except asyncio.QueueEmpty:
                pass

    async def _handle_twilio_start(self, metadata: StreamMetadata) -> None:
        """Handle Twilio stream start.

        Args:
            metadata: Stream metadata
        """
        logger.info(
            "Twilio stream started",
            stream_sid=metadata.stream_sid,
            call_sid=metadata.call_sid,
        )

    async def _handle_twilio_stop(self) -> None:
        """Handle Twilio stream stop."""
        logger.info("Twilio stream stopped")
        await self.stop()

    async def _handle_gemini_audio(self, audio_bytes: bytes) -> None:
        """Handle audio from Gemini.

        Args:
            audio_bytes: PCM 24kHz audio
        """
        self._stats.gemini_packets_received += 1

        # Convert Gemini audio to Twilio format
        twilio_payload = self._converter.gemini_to_twilio(audio_bytes)

        # Queue for sending to Twilio
        try:
            self._gemini_to_twilio_queue.put_nowait(twilio_payload)
        except asyncio.QueueFull:
            pass

    async def _handle_gemini_transcript(self, text: str, is_final: bool) -> None:
        """Handle transcript from Gemini.

        Args:
            text: Transcript text
            is_final: Whether this is a final transcript
        """
        entry = TranscriptEntry(
            speaker="assistant",
            text=text,
            timestamp=datetime.now(),
            is_final=is_final,
        )
        self._transcripts.append(entry)

        logger.debug("Gemini transcript", text=text[:100], is_final=is_final)

        if self._on_transcript:
            await self._on_transcript(entry)

    async def _handle_gemini_interrupted(self) -> None:
        """Handle barge-in interruption from user."""
        self._stats.interruptions += 1
        logger.debug("User interrupted (barge-in)")

        # Clear Twilio's audio buffer
        await self.twilio.send_clear()

        # Clear our outbound queue
        while not self._gemini_to_twilio_queue.empty():
            try:
                self._gemini_to_twilio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _handle_gemini_turn_complete(self) -> None:
        """Handle Gemini turn complete."""
        logger.debug("Gemini turn complete")

        if self._on_turn_complete:
            await self._on_turn_complete()

    async def _process_twilio_to_gemini(self) -> None:
        """Process audio from Twilio to Gemini."""
        try:
            while self._is_running:
                # Get audio from queue
                pcm_audio = await self._twilio_to_gemini_queue.get()

                # Send to Gemini
                await self.gemini.send_audio(pcm_audio)
                self._stats.gemini_packets_sent += 1

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Error in Twilio->Gemini processor", error=str(e))

    async def _process_gemini_to_twilio(self) -> None:
        """Process audio from Gemini to Twilio."""
        try:
            while self._is_running:
                # Get audio from queue
                twilio_payload = await self._gemini_to_twilio_queue.get()

                # Send to Twilio
                await self.twilio.send_audio(twilio_payload)
                self._stats.twilio_packets_sent += 1

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Error in Gemini->Twilio processor", error=str(e))

    def get_full_transcript(self) -> str:
        """Get the full transcript as a formatted string.

        Returns:
            Formatted transcript string
        """
        lines = []
        for entry in self._transcripts:
            speaker = "User" if entry.speaker == "user" else "Assistant"
            lines.append(f"{speaker}: {entry.text}")
        return "\n".join(lines)
