"""Bidirectional audio bridge between Twilio and Gemini."""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable, Optional

import structlog

from ..audio.converter import AudioConverter
from ..audio.whisper_stt import WhisperSTT, SilenceDetector
from ..twilio.websocket import TwilioMediaStreamHandler
from ..gemini.realtime_handler import GeminiRealtimeHandler
from .conversation_brain import ConversationBrain

logger = structlog.get_logger(__name__)

# Audio format constants
GEMINI_INPUT_SAMPLE_RATE = 16000


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
        gemini_handler,  # Can be GeminiRealtimeHandler or OpenAIRealtimeHandler
        telegram_client = None,  # Legacy, not used
        telegram_chat_id: str = "",
        call_id: str = "",
        gemini_api_key: str = "",
        whisper_api_key: str = "",
        whisper_enabled: bool = False,
        use_openai: bool = False,
    ):
        """Initialize audio bridge.

        Args:
            twilio_handler: Twilio WebSocket handler
            gemini_handler: Realtime handler (Gemini or OpenAI)
            telegram_client: Legacy Telegram client (not used)
            telegram_chat_id: Telegram chat ID for ClawdBot agent
            call_id: Call identifier
            gemini_api_key: Gemini API key for the brain
            whisper_api_key: OpenAI API key for Whisper STT
            whisper_enabled: Whether to use Whisper for STT
            use_openai: Whether using OpenAI Realtime (affects audio conversion)
        """
        self.twilio = twilio_handler
        self.gemini = gemini_handler  # Generic name, works with both
        self.call_id = call_id
        self._use_openai = use_openai

        self._converter = AudioConverter()
        self._is_running = False
        self._transcripts: list[TranscriptEntry] = []

        # Audio buffering for better STT (aggregate small chunks)
        self._audio_buffer = bytearray()
        # OpenAI uses 24kHz, Gemini uses 16kHz
        # Reduced buffer size for lower latency (was 100ms, now 50ms)
        self._min_chunk_size = 2400 if use_openai else 1600  # ~50ms of audio

        # Whisper STT for accurate transcription
        self._whisper: Optional[WhisperSTT] = None
        self._whisper_enabled = whisper_enabled
        if whisper_enabled and whisper_api_key:
            self._whisper = WhisperSTT(api_key=whisper_api_key)
            self._whisper_enabled = self._whisper.is_enabled
            if self._whisper_enabled:
                print("=== WHISPER STT ENABLED - Using OpenAI for accurate transcription ===", flush=True)

        # Whisper audio buffering with silence detection
        self._whisper_audio_buffer = bytearray()
        self._silence_detector = SilenceDetector(
            silence_threshold=500,
            silence_duration_ms=500,
            sample_rate=GEMINI_INPUT_SAMPLE_RATE,
        )
        self._whisper_transcribe_task: Optional[asyncio.Task] = None

        # Conversation brain - sends commands to ClawdBot agent
        self._brain = ConversationBrain(
            api_key=gemini_api_key or os.environ.get("GEMINI_API_KEY", ""),
            telegram_client=telegram_client,
            telegram_chat_id=telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID", ""),
            call_id=call_id,
        )

        # Set up brain callback to feed ClawdBot responses to Gemini
        self._brain.set_callbacks(
            on_clawdbot_response=self._handle_clawdbot_response,
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
        # When Whisper is enabled, disable Gemini's user transcript callback
        # Whisper handles STT instead
        self.gemini.set_callbacks(
            on_audio=self._handle_gemini_audio,
            on_transcript=self._handle_gemini_transcript,
            on_user_transcript=None if self._whisper_enabled else self._handle_user_transcript_async,
            on_turn_complete=self._handle_gemini_turn_complete,
            on_user_turn_complete=None if self._whisper_enabled else self._handle_user_turn_complete,
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
        # Convert Twilio audio based on which realtime API we're using
        if self._use_openai:
            pcm_audio = self._converter.twilio_to_openai(payload)
        else:
            pcm_audio = self._converter.twilio_to_gemini(payload)

        # Buffer audio to send larger chunks (improves STT accuracy)
        self._audio_buffer.extend(pcm_audio)

        # Send when buffer reaches minimum size (~100ms of audio)
        if len(self._audio_buffer) >= self._min_chunk_size:
            await self.gemini.send_audio(bytes(self._audio_buffer))
            self._audio_buffer.clear()

        # If Whisper is enabled (and not using OpenAI which has built-in Whisper)
        if self._whisper_enabled and self._whisper and not self._use_openai:
            await self._buffer_for_whisper(pcm_audio)

    async def _handle_twilio_start(self, metadata):
        """Handle Twilio stream start."""
        logger.info("Twilio stream started", stream_sid=metadata.stream_sid)

    async def _handle_twilio_stop(self):
        """Handle Twilio stream stop."""
        logger.info("Twilio stream stopped")

        # Flush any remaining buffered audio
        if self._audio_buffer:
            await self.gemini.send_audio(bytes(self._audio_buffer))
            self._audio_buffer.clear()

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

                # Convert audio to Twilio format (mulaw 8kHz)
                if self._use_openai:
                    twilio_payload = self._converter.openai_to_twilio(audio_bytes)
                else:
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
            text: What the user said (from Gemini's input_transcription)
        """
        # Log the raw transcript fragment from Gemini (show repr to see spaces)
        print(f"=== GEMINI STT FRAGMENT: {repr(text)} ===", flush=True)

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

    async def _handle_gemini_turn_complete(self):
        """Handle Gemini turn complete - flush buffered transcripts."""
        logger.debug("Gemini turn complete")

        # Flush assistant's complete turn to Telegram
        await self._brain.flush_assistant_turn()

    async def _handle_clawdbot_response(self, response: str):
        """Handle response from ClawdBot - feed it to Gemini to speak.

        Args:
            response: ClawdBot's response text
        """
        if not response:
            return

        print(f"=== AUDIO BRIDGE: Feeding ClawdBot response to Gemini ===", flush=True)

        # Create a prompt that tells Gemini to relay the information
        prompt = f"""The system has retrieved the following information for the user.
Please relay this information to them naturally in a conversational way.
Keep your response concise and to the point. Don't add unnecessary commentary.

Information to relay:
{response}"""

        # Send to Gemini to speak
        await self.gemini.send_text(prompt)
    
    async def _handle_user_turn_complete(self):
        """Handle user turn complete - analyze intent and flush."""
        print(f"=== USER TURN COMPLETE - Flushing to brain ===", flush=True)
        # Flush user's turn and analyze intent
        await self._brain.flush_user_turn()

    async def _buffer_for_whisper(self, pcm_audio: bytes):
        """Buffer audio for Whisper transcription with silence detection.

        Args:
            pcm_audio: PCM 16kHz audio bytes
        """
        # Add audio to Whisper buffer
        self._whisper_audio_buffer.extend(pcm_audio)

        # Check for end of speech (silence after speech)
        if self._silence_detector.process(pcm_audio):
            # User stopped speaking - transcribe the buffer
            await self._transcribe_whisper_buffer()

    async def _transcribe_whisper_buffer(self):
        """Transcribe the accumulated Whisper audio buffer."""
        if not self._whisper or not self._whisper_audio_buffer:
            return

        # Minimum audio length for transcription (~300ms)
        min_audio_bytes = int(0.3 * GEMINI_INPUT_SAMPLE_RATE * 2)  # 16kHz, 16-bit
        if len(self._whisper_audio_buffer) < min_audio_bytes:
            # Too short, likely just noise - clear and continue
            self._whisper_audio_buffer.clear()
            self._silence_detector.reset()
            return

        # Copy buffer and clear for next segment
        audio_to_transcribe = bytes(self._whisper_audio_buffer)
        self._whisper_audio_buffer.clear()
        self._silence_detector.reset()

        print(f"=== WHISPER: Transcribing {len(audio_to_transcribe)} bytes ===", flush=True)

        # Transcribe asynchronously with prompt for better proper noun recognition
        try:
            transcript = await self._whisper.transcribe(
                audio_bytes=audio_to_transcribe,
                sample_rate=GEMINI_INPUT_SAMPLE_RATE,
                prompt="YouTube, Spotify, WhatsApp, Telegram, Instagram, TikTok, Zayn, Dusk Till Dawn, Taylor Swift, Drake, Billie Eilish, Ed Sheeran, Ariana Grande, The Weeknd, BTS, Coldplay, Adele, Bruno Mars, Rihanna",
            )

            if transcript:
                # Process the Whisper transcript
                await self._handle_whisper_transcript(transcript)

        except Exception as e:
            logger.error("Whisper transcription failed", error=str(e))
            print(f"=== WHISPER TRANSCRIPTION ERROR: {e} ===", flush=True)

    async def _handle_whisper_transcript(self, text: str):
        """Handle transcript from Whisper STT.

        Args:
            text: The transcribed text from Whisper
        """
        print(f"=== WHISPER TRANSCRIPT: {text} ===", flush=True)

        # Add to brain for intent analysis
        self._brain.add_user_transcript(text)

        # Store in raw transcripts
        entry = TranscriptEntry(
            speaker="user",
            text=text,
            timestamp=datetime.now(),
            is_final=True,
        )
        self._transcripts.append(entry)

        # Immediately flush user turn since Whisper gives complete utterances
        print(f"=== WHISPER: USER TURN COMPLETE - Flushing to brain ===", flush=True)
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
