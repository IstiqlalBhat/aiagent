"""Bidirectional audio bridge between Twilio and Gemini."""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable

import structlog

from ..audio.converter import AudioConverter
from ..twilio.websocket import TwilioMediaStreamHandler
from ..gemini.realtime_handler import GeminiRealtimeHandler
from .conversation_brain import ConversationBrain

logger = structlog.get_logger(__name__)


@dataclass
class TranscriptEntry:
    """A transcript entry."""
    speaker: str  # "user" or "assistant"
    text: str
    timestamp: datetime
    is_final: bool = True


class AudioBridge:
    """Bidirectional audio bridge between Twilio and Gemini.

    Uses ConversationBrain as the ONLY handler for transcript processing
    and Telegram communication. No direct Telegram access here.
    """

    def __init__(
        self,
        twilio_handler: TwilioMediaStreamHandler,
        gemini_handler: GeminiRealtimeHandler,
        telegram_client = None,  # Passed to brain only
        call_id: str = "",
        gemini_api_key: str = "",
    ):
        """Initialize audio bridge.

        Args:
            twilio_handler: Twilio WebSocket handler
            gemini_handler: Gemini realtime handler
            telegram_client: Telegram client (passed to brain only)
            call_id: Call identifier
            gemini_api_key: Gemini API key for the brain
        """
        self.twilio = twilio_handler
        self.gemini = gemini_handler
        self.call_id = call_id

        self._converter = AudioConverter()
        self._is_running = False
        self._transcripts: list[TranscriptEntry] = []
        
        # Conversation brain with memory - ONLY component that talks to Telegram
        self._brain = ConversationBrain(
            api_key=gemini_api_key or os.environ.get("GEMINI_API_KEY", ""),
            telegram_client=telegram_client,
            call_id=call_id,
        )

        # Tasks
        self._tasks: list[asyncio.Task] = []

    @property
    def is_running(self) -> bool:
        """Check if bridge is running."""
        return self._is_running

    @property
    def transcripts(self) -> list[TranscriptEntry]:
        """Get collected transcripts."""
        return self._transcripts

    async def start(self):
        """Start the audio bridge."""
        if self._is_running:
            return

        logger.info("Starting audio bridge v2")
        self._is_running = True

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
            on_user_transcript=self._handle_user_transcript_async,
            on_turn_complete=self._handle_gemini_turn_complete,
            on_user_turn_complete=self._handle_user_turn_complete,
        )

        # Start processing tasks
        self._tasks = [
            asyncio.create_task(self.twilio.receive_loop()),
            asyncio.create_task(self._process_gemini_audio()),
        ]

        logger.info("Audio bridge v2 started")

    async def stop(self):
        """Stop the audio bridge."""
        if not self._is_running:
            return

        logger.info("Stopping audio bridge v2")
        self._is_running = False

        # Cancel tasks
        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Close connections
        await self.twilio.close()

        logger.info("Audio bridge v2 stopped")

    async def _handle_twilio_audio(self, payload: str):
        """Handle audio from Twilio (phone).

        Args:
            payload: Base64-encoded mulaw audio
        """
        # Convert Twilio audio (mulaw 8kHz) to Gemini format (PCM 16kHz)
        pcm_audio = self._converter.twilio_to_gemini(payload)

        # Send to Gemini
        await self.gemini.send_audio(pcm_audio)

    async def _handle_twilio_start(self, metadata):
        """Handle Twilio stream start."""
        logger.info("Twilio stream started", stream_sid=metadata.stream_sid)

    async def _handle_twilio_stop(self):
        """Handle Twilio stream stop."""
        logger.info("Twilio stream stopped")
        await self.stop()

    async def _handle_gemini_audio(self, audio_bytes: bytes):
        """Handle audio from Gemini.

        Args:
            audio_bytes: PCM 24kHz audio
        """
        # Audio handled in _process_gemini_audio task
        pass

    async def _process_gemini_audio(self):
        """Process audio from Gemini and send to Twilio."""
        chunk_count = 0
        try:
            print("=== AUDIO BRIDGE: Waiting for Gemini audio... ===", flush=True)
            while self._is_running:
                # Get audio from Gemini
                audio_bytes = await self.gemini.get_audio()
                chunk_count += 1
                
                if chunk_count == 1:
                    print(f"=== AUDIO BRIDGE: First Gemini audio received! {len(audio_bytes)} bytes ===", flush=True)
                elif chunk_count % 50 == 0:
                    print(f"=== AUDIO BRIDGE: Processed {chunk_count} audio chunks ===", flush=True)

                # Convert Gemini audio (PCM 24kHz) to Twilio format (mulaw 8kHz)
                twilio_payload = self._converter.gemini_to_twilio(audio_bytes)

                # Send to Twilio
                await self.twilio.send_audio(twilio_payload)

        except asyncio.CancelledError:
            print(f"=== AUDIO BRIDGE: Cancelled after {chunk_count} chunks ===", flush=True)
        except Exception as e:
            logger.error("Error processing Gemini audio", error=str(e))
            print(f"=== AUDIO BRIDGE ERROR: {e} ===", flush=True)

    async def _handle_gemini_transcript(self, text: str, is_final: bool):
        """Handle transcript from Gemini.

        Args:
            text: Transcript text (word-by-word)
            is_final: Whether this is a final transcript
        """
        # Buffer in the brain (don't send word-by-word to Telegram)
        self._brain.add_assistant_transcript(text)
        
        # Also store in raw transcripts for the call summary
        entry = TranscriptEntry(
            speaker="assistant",
            text=text,
            timestamp=datetime.now(),
            is_final=is_final,
        )
        self._transcripts.append(entry)
    
    async def _handle_user_transcript_async(self, text: str):
        """Handle user transcript from input audio (async callback).
        
        Args:
            text: What the user said
        """
        # Buffer in the brain
        self._brain.add_user_transcript(text)
        
        # Also store in raw transcripts
        entry = TranscriptEntry(
            speaker="user",
            text=text,
            timestamp=datetime.now(),
            is_final=True,
        )
        self._transcripts.append(entry)
        
        print(f"=== BRAIN: User fragment: {text[:50]}... ===", flush=True)

    async def _handle_gemini_turn_complete(self):
        """Handle Gemini turn complete - flush buffered transcripts."""
        logger.debug("Gemini turn complete")
        
        # Flush assistant's complete turn to Telegram
        await self._brain.flush_assistant_turn()
    
    async def _handle_user_turn_complete(self):
        """Handle user turn complete - analyze intent and flush."""
        # Flush user's turn and analyze intent
        await self._brain.flush_user_turn()

    def get_full_transcript(self) -> str:
        """Get the full transcript as a formatted string."""
        lines = []
        for entry in self._transcripts:
            speaker = "User" if entry.speaker == "user" else "Assistant"
            lines.append(f"{speaker}: {entry.text}")
        return "\n".join(lines)
    
    @property
    def brain(self) -> ConversationBrain:
        """Get the conversation brain."""
        return self._brain
    
    def get_conversation_summary(self) -> str:
        """Get the brain's conversation summary with analyzed intents."""
        return self._brain.get_memory_summary()
