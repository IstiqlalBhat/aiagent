"""Audio format conversion utilities.

Handles conversion between:
- Twilio Media Streams: mulaw 8kHz mono
- Gemini Live API: PCM 16-bit 16kHz mono (input) / 24kHz mono (output)
"""

import base64
from typing import Literal

import audioop
import numpy as np
import soxr

# Audio format constants
TWILIO_SAMPLE_RATE = 8000
GEMINI_INPUT_SAMPLE_RATE = 16000
GEMINI_OUTPUT_SAMPLE_RATE = 24000
OPENAI_SAMPLE_RATE = 24000  # OpenAI Realtime uses 24kHz for both input and output

# PCM format
PCM_SAMPLE_WIDTH = 2  # 16-bit = 2 bytes


class AudioConverter:
    """Bidirectional audio converter for Twilio <-> Gemini pipeline."""

    def __init__(self):
        """Initialize the audio converter."""
        # Resampler instances for consistent quality
        self._resamplers: dict = {}

    def _get_resampler(
        self, from_rate: int, to_rate: int, quality: str = "HQ"
    ) -> soxr.ResampleStream:
        """Get or create a resampler for the given rates."""
        key = (from_rate, to_rate, quality)
        if key not in self._resamplers:
            self._resamplers[key] = soxr.ResampleStream(
                from_rate, to_rate, num_channels=1, dtype=np.int16
            )
        return self._resamplers[key]

    def twilio_to_gemini(self, twilio_payload: str) -> bytes:
        """Convert Twilio media payload to Gemini-compatible PCM.

        Pipeline:
        1. Base64 decode
        2. mulaw -> PCM 16-bit
        3. Resample 8kHz -> 16kHz

        Args:
            twilio_payload: Base64-encoded mulaw audio from Twilio

        Returns:
            PCM 16-bit 16kHz audio bytes for Gemini
        """
        # Decode base64
        mulaw_bytes = base64.b64decode(twilio_payload)

        # Convert mulaw to PCM 16-bit
        pcm_8khz = audioop.ulaw2lin(mulaw_bytes, PCM_SAMPLE_WIDTH)

        # Resample 8kHz -> 16kHz
        pcm_16khz = self._resample(pcm_8khz, TWILIO_SAMPLE_RATE, GEMINI_INPUT_SAMPLE_RATE)

        return pcm_16khz

    def gemini_to_twilio(self, gemini_audio: bytes) -> str:
        """Convert Gemini audio to Twilio media payload.

        Pipeline:
        1. Resample 24kHz -> 8kHz
        2. PCM 16-bit -> mulaw
        3. Base64 encode

        Args:
            gemini_audio: PCM 16-bit 24kHz audio from Gemini

        Returns:
            Base64-encoded mulaw audio for Twilio
        """
        # Resample 24kHz -> 8kHz
        pcm_8khz = self._resample(gemini_audio, GEMINI_OUTPUT_SAMPLE_RATE, TWILIO_SAMPLE_RATE)

        # Convert PCM to mulaw
        mulaw_bytes = audioop.lin2ulaw(pcm_8khz, PCM_SAMPLE_WIDTH)

        # Base64 encode
        return base64.b64encode(mulaw_bytes).decode("ascii")

    def twilio_to_openai(self, twilio_payload: str) -> bytes:
        """Convert Twilio media payload to OpenAI Realtime format (24kHz PCM).

        Pipeline:
        1. Base64 decode
        2. mulaw -> PCM 16-bit
        3. Resample 8kHz -> 24kHz

        Args:
            twilio_payload: Base64-encoded mulaw audio from Twilio

        Returns:
            PCM 16-bit 24kHz audio bytes for OpenAI Realtime
        """
        # Decode base64
        mulaw_bytes = base64.b64decode(twilio_payload)

        # Convert mulaw to PCM 16-bit
        pcm_8khz = audioop.ulaw2lin(mulaw_bytes, PCM_SAMPLE_WIDTH)

        # Resample 8kHz -> 24kHz
        pcm_24khz = self._resample(pcm_8khz, TWILIO_SAMPLE_RATE, OPENAI_SAMPLE_RATE)

        return pcm_24khz

    def openai_to_twilio(self, openai_audio: bytes) -> str:
        """Convert OpenAI Realtime audio to Twilio media payload.

        Pipeline:
        1. Resample 24kHz -> 8kHz
        2. PCM 16-bit -> mulaw
        3. Base64 encode

        Args:
            openai_audio: PCM 16-bit 24kHz audio from OpenAI

        Returns:
            Base64-encoded mulaw audio for Twilio
        """
        # Same conversion as Gemini (both use 24kHz output)
        return self.gemini_to_twilio(openai_audio)

    def _resample(self, audio_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
        """Resample audio using soxr.

        Args:
            audio_bytes: Input PCM 16-bit audio
            from_rate: Source sample rate
            to_rate: Target sample rate

        Returns:
            Resampled PCM 16-bit audio
        """
        if from_rate == to_rate:
            return audio_bytes

        # Convert bytes to numpy array
        audio_array = np.frombuffer(audio_bytes, dtype=np.int16)

        # Use soxr for high-quality resampling
        resampled = soxr.resample(audio_array, from_rate, to_rate, quality="HQ")

        # Convert back to bytes
        return resampled.astype(np.int16).tobytes()

    def resample_for_gemini_input(self, pcm_audio: bytes, from_rate: int) -> bytes:
        """Resample PCM audio to Gemini input format (16kHz).

        Args:
            pcm_audio: Input PCM 16-bit audio
            from_rate: Source sample rate

        Returns:
            PCM 16-bit 16kHz audio
        """
        return self._resample(pcm_audio, from_rate, GEMINI_INPUT_SAMPLE_RATE)

    def resample_from_gemini_output(self, pcm_audio: bytes, to_rate: int) -> bytes:
        """Resample Gemini output PCM audio to target format.

        Args:
            pcm_audio: Gemini output PCM 16-bit 24kHz audio
            to_rate: Target sample rate

        Returns:
            Resampled PCM 16-bit audio
        """
        return self._resample(pcm_audio, GEMINI_OUTPUT_SAMPLE_RATE, to_rate)

    @staticmethod
    def pcm_to_base64(pcm_audio: bytes) -> str:
        """Encode PCM audio to base64 string."""
        return base64.b64encode(pcm_audio).decode("ascii")

    @staticmethod
    def base64_to_pcm(b64_audio: str) -> bytes:
        """Decode base64 string to PCM audio."""
        return base64.b64decode(b64_audio)

    @staticmethod
    def calculate_duration_ms(audio_bytes: bytes, sample_rate: int) -> float:
        """Calculate audio duration in milliseconds.

        Args:
            audio_bytes: PCM 16-bit audio
            sample_rate: Sample rate in Hz

        Returns:
            Duration in milliseconds
        """
        num_samples = len(audio_bytes) // PCM_SAMPLE_WIDTH
        return (num_samples / sample_rate) * 1000
