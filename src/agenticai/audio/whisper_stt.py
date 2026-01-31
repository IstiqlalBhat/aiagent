"""OpenAI Whisper STT integration for accurate speech-to-text."""

import io
import wave
import asyncio
from typing import Optional

import structlog
from openai import AsyncOpenAI

logger = structlog.get_logger(__name__)

# Audio format constants
DEFAULT_SAMPLE_RATE = 16000
PCM_SAMPLE_WIDTH = 2  # 16-bit = 2 bytes
CHANNELS = 1


class WhisperSTT:
    """OpenAI Whisper API client for speech-to-text.

    Provides more accurate transcription than Gemini's native STT,
    especially for proper nouns, song titles, and specialized terms.
    """

    def __init__(self, api_key: str, model: str = "whisper-1"):
        """Initialize Whisper STT client.

        Args:
            api_key: OpenAI API key
            model: Whisper model to use (default: whisper-1)
        """
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self._is_enabled = bool(api_key)

        if self._is_enabled:
            logger.info("WhisperSTT initialized", model=model)
        else:
            logger.warning("WhisperSTT disabled - no API key provided")

    @property
    def is_enabled(self) -> bool:
        """Check if Whisper STT is enabled."""
        return self._is_enabled

    def _pcm_to_wav(self, audio_bytes: bytes, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
        """Convert raw PCM audio to WAV format.

        Args:
            audio_bytes: Raw PCM 16-bit audio bytes
            sample_rate: Audio sample rate in Hz

        Returns:
            WAV-formatted audio bytes
        """
        wav_buffer = io.BytesIO()

        with wave.open(wav_buffer, 'wb') as wav_file:
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(PCM_SAMPLE_WIDTH)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_bytes)

        wav_buffer.seek(0)
        return wav_buffer.read()

    async def transcribe(
        self,
        audio_bytes: bytes,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        language: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> Optional[str]:
        """Transcribe audio using OpenAI Whisper API.

        Args:
            audio_bytes: Raw PCM 16-bit audio bytes
            sample_rate: Audio sample rate in Hz
            language: Optional language hint (e.g., "en" for English)
            prompt: Optional prompt to guide transcription (e.g., proper nouns)

        Returns:
            Transcribed text, or None if transcription failed
        """
        if not self._is_enabled:
            return None

        if not audio_bytes or len(audio_bytes) < 100:
            return None

        try:
            # Convert PCM to WAV format
            wav_audio = self._pcm_to_wav(audio_bytes, sample_rate)

            # Create a file-like object for the API
            audio_file = io.BytesIO(wav_audio)
            audio_file.name = "audio.wav"  # Required for the API

            # Call Whisper API
            kwargs = {"model": self.model, "file": audio_file}
            if language:
                kwargs["language"] = language
            if prompt:
                kwargs["prompt"] = prompt

            response = await self.client.audio.transcriptions.create(**kwargs)

            transcript = response.text.strip()

            if transcript:
                logger.debug("Whisper transcription", text=transcript[:100])
                print(f"=== WHISPER STT: {transcript} ===", flush=True)

            return transcript if transcript else None

        except Exception as e:
            logger.error("Whisper transcription failed", error=str(e))
            print(f"=== WHISPER ERROR: {e} ===", flush=True)
            return None


class SilenceDetector:
    """Detects silence in audio stream to segment speech.

    Used to determine when a user has finished speaking and
    the audio buffer should be sent for transcription.
    """

    def __init__(
        self,
        silence_threshold: int = 500,
        silence_duration_ms: int = 500,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ):
        """Initialize silence detector.

        Args:
            silence_threshold: RMS amplitude below which audio is considered silence
            silence_duration_ms: Duration of silence needed to trigger end-of-speech
            sample_rate: Audio sample rate in Hz
        """
        self.silence_threshold = silence_threshold
        self.silence_duration_ms = silence_duration_ms
        self.sample_rate = sample_rate

        # Calculate samples needed for silence duration
        self._samples_for_silence = int(
            (silence_duration_ms / 1000) * sample_rate * PCM_SAMPLE_WIDTH
        )

        self._consecutive_silence_bytes = 0
        self._has_speech = False

    def reset(self):
        """Reset the detector state."""
        self._consecutive_silence_bytes = 0
        self._has_speech = False

    def _calculate_rms(self, audio_bytes: bytes) -> float:
        """Calculate RMS amplitude of audio chunk."""
        import numpy as np

        if len(audio_bytes) < 2:
            return 0.0

        samples = np.frombuffer(audio_bytes, dtype=np.int16)
        return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))

    def is_silence(self, audio_bytes: bytes) -> bool:
        """Check if audio chunk is silence."""
        rms = self._calculate_rms(audio_bytes)
        return rms < self.silence_threshold

    def process(self, audio_bytes: bytes) -> bool:
        """Process audio chunk and detect end of speech.

        Args:
            audio_bytes: PCM audio chunk

        Returns:
            True if speech ended (silence after speech), False otherwise
        """
        is_silent = self.is_silence(audio_bytes)

        if is_silent:
            self._consecutive_silence_bytes += len(audio_bytes)

            # Check if we had speech followed by enough silence
            if self._has_speech and self._consecutive_silence_bytes >= self._samples_for_silence:
                return True
        else:
            # Audio is not silent - mark that we have speech
            self._has_speech = True
            self._consecutive_silence_bytes = 0

        return False
