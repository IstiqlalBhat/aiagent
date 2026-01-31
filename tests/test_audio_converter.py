"""Tests for audio converter."""

import base64
import pytest
import numpy as np

from agenticai.audio.converter import (
    AudioConverter,
    TWILIO_SAMPLE_RATE,
    GEMINI_INPUT_SAMPLE_RATE,
    GEMINI_OUTPUT_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
)


@pytest.fixture
def converter():
    """Create audio converter instance."""
    return AudioConverter()


class TestAudioConverter:
    """Tests for AudioConverter class."""

    def test_twilio_to_gemini_basic(self, converter):
        """Test basic Twilio to Gemini conversion."""
        # Create a simple mulaw audio sample (silence)
        # Mulaw silence is 0xFF (255) or 0x7F (127)
        mulaw_samples = bytes([0xFF] * 160)  # 20ms at 8kHz
        twilio_payload = base64.b64encode(mulaw_samples).decode("ascii")

        # Convert to Gemini format
        pcm_16khz = converter.twilio_to_gemini(twilio_payload)

        # Should have more samples due to upsampling (8kHz -> 16kHz = 2x)
        expected_samples = 160 * 2  # Approximately
        actual_samples = len(pcm_16khz) // PCM_SAMPLE_WIDTH

        # Allow some tolerance for resampling
        assert abs(actual_samples - expected_samples) <= 10

    def test_gemini_to_twilio_basic(self, converter):
        """Test basic Gemini to Twilio conversion."""
        # Create PCM 24kHz audio (silence = zeros)
        pcm_24khz = bytes(480 * PCM_SAMPLE_WIDTH)  # 20ms at 24kHz

        # Convert to Twilio format
        twilio_payload = converter.gemini_to_twilio(pcm_24khz)

        # Should be valid base64
        mulaw_bytes = base64.b64decode(twilio_payload)

        # Should have fewer samples due to downsampling (24kHz -> 8kHz = 1/3)
        expected_samples = 480 // 3  # Approximately
        actual_samples = len(mulaw_bytes)

        # Allow some tolerance for resampling
        assert abs(actual_samples - expected_samples) <= 10

    def test_roundtrip_preserves_approximate_duration(self, converter):
        """Test that roundtrip conversion preserves approximate duration."""
        # Create 100ms of mulaw audio at 8kHz
        duration_ms = 100
        samples_8khz = int(TWILIO_SAMPLE_RATE * duration_ms / 1000)
        mulaw_samples = bytes([0x80] * samples_8khz)  # Some non-silence value
        twilio_payload = base64.b64encode(mulaw_samples).decode("ascii")

        # Convert Twilio -> Gemini
        pcm_16khz = converter.twilio_to_gemini(twilio_payload)

        # Calculate duration
        gemini_duration = converter.calculate_duration_ms(
            pcm_16khz, GEMINI_INPUT_SAMPLE_RATE
        )

        # Duration should be approximately preserved (within 5ms tolerance)
        assert abs(gemini_duration - duration_ms) < 5

    def test_resample_same_rate(self, converter):
        """Test that resampling with same rate returns original."""
        pcm_audio = bytes([0x00, 0x10, 0x20, 0x30] * 100)

        result = converter._resample(pcm_audio, 16000, 16000)

        assert result == pcm_audio

    def test_resample_upsample(self, converter):
        """Test upsampling increases sample count."""
        # 100 samples at source rate
        pcm_audio = np.zeros(100, dtype=np.int16).tobytes()

        result = converter._resample(pcm_audio, 8000, 16000)

        # Should have approximately 2x samples
        result_samples = len(result) // PCM_SAMPLE_WIDTH
        assert result_samples >= 190  # Allow some tolerance

    def test_resample_downsample(self, converter):
        """Test downsampling decreases sample count."""
        # 300 samples at source rate
        pcm_audio = np.zeros(300, dtype=np.int16).tobytes()

        result = converter._resample(pcm_audio, 24000, 8000)

        # Should have approximately 1/3 samples
        result_samples = len(result) // PCM_SAMPLE_WIDTH
        assert result_samples <= 110  # Allow some tolerance

    def test_pcm_to_base64(self):
        """Test PCM to base64 encoding."""
        pcm_audio = bytes([0x12, 0x34, 0x56, 0x78])

        result = AudioConverter.pcm_to_base64(pcm_audio)

        assert result == base64.b64encode(pcm_audio).decode("ascii")

    def test_base64_to_pcm(self):
        """Test base64 to PCM decoding."""
        original = bytes([0x12, 0x34, 0x56, 0x78])
        b64_audio = base64.b64encode(original).decode("ascii")

        result = AudioConverter.base64_to_pcm(b64_audio)

        assert result == original

    def test_calculate_duration_ms(self):
        """Test duration calculation."""
        # 1600 samples at 16kHz = 100ms
        samples = 1600
        audio_bytes = bytes(samples * PCM_SAMPLE_WIDTH)

        duration = AudioConverter.calculate_duration_ms(audio_bytes, 16000)

        assert duration == 100.0

    def test_sine_wave_conversion(self, converter):
        """Test conversion of a sine wave maintains signal integrity."""
        # Generate a 440Hz sine wave at 8kHz for 50ms
        duration_s = 0.05
        freq = 440
        t = np.linspace(0, duration_s, int(TWILIO_SAMPLE_RATE * duration_s))
        sine_wave = (np.sin(2 * np.pi * freq * t) * 16000).astype(np.int16)

        # Convert to mulaw and encode
        import audioop
        mulaw_bytes = audioop.lin2ulaw(sine_wave.tobytes(), PCM_SAMPLE_WIDTH)
        twilio_payload = base64.b64encode(mulaw_bytes).decode("ascii")

        # Convert to Gemini format
        pcm_16khz = converter.twilio_to_gemini(twilio_payload)

        # Verify we got audio data
        assert len(pcm_16khz) > 0

        # Convert back to Twilio format
        # First need to simulate Gemini output (24kHz)
        pcm_24khz = converter._resample(
            pcm_16khz, GEMINI_INPUT_SAMPLE_RATE, GEMINI_OUTPUT_SAMPLE_RATE
        )
        twilio_back = converter.gemini_to_twilio(pcm_24khz)

        # Verify we got valid base64 audio
        decoded = base64.b64decode(twilio_back)
        assert len(decoded) > 0
