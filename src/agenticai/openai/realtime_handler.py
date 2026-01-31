"""OpenAI Realtime API handler for bidirectional audio."""

import asyncio
import base64
import json
from typing import Callable, Awaitable

import structlog
import websockets

logger = structlog.get_logger(__name__)

# OpenAI Realtime API endpoint
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"


class OpenAIRealtimeHandler:
    """Handles bidirectional audio streaming with OpenAI Realtime API.

    Replaces Gemini Live API with OpenAI's equivalent for better
    transcription accuracy, especially with proper nouns.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-realtime-preview-2024-12-17",
        voice: str = "alloy",
        system_instruction: str = "You are a helpful AI assistant.",
    ):
        """Initialize OpenAI Realtime handler.

        Args:
            api_key: OpenAI API key
            model: Model to use (gpt-4o-realtime-preview)
            voice: Voice for TTS (alloy, echo, fable, onyx, nova, shimmer)
            system_instruction: System prompt
        """
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.system_instruction = system_instruction

        self.websocket = None
        self.audio_in_queue: asyncio.Queue = None  # Audio FROM OpenAI
        self.audio_out_queue: asyncio.Queue = None  # Audio TO OpenAI

        # Callbacks
        self._on_audio: Callable[[bytes], Awaitable[None]] | None = None
        self._on_transcript: Callable[[str, bool], Awaitable[None]] | None = None
        self._on_user_transcript: Callable[[str], Awaitable[None]] | None = None
        self._on_turn_complete: Callable[[], Awaitable[None]] | None = None
        self._on_user_turn_complete: Callable[[], Awaitable[None]] | None = None

        self._is_running = False
        self._tasks = []
        self._user_spoke = False

    def set_callbacks(
        self,
        on_audio: Callable[[bytes], Awaitable[None]] | None = None,
        on_transcript: Callable[[str, bool], Awaitable[None]] | None = None,
        on_user_transcript: Callable[[str], Awaitable[None]] | None = None,
        on_turn_complete: Callable[[], Awaitable[None]] | None = None,
        on_user_turn_complete: Callable[[], Awaitable[None]] | None = None,
    ):
        """Set event callbacks."""
        self._on_audio = on_audio
        self._on_transcript = on_transcript
        self._on_user_transcript = on_user_transcript
        self._on_turn_complete = on_turn_complete
        self._on_user_turn_complete = on_user_turn_complete

    async def connect(self, initial_prompt: str | None = None):
        """Connect to OpenAI Realtime API."""
        logger.info("Connecting to OpenAI Realtime", model=self.model)

        # Connect with API key in header
        url = f"{OPENAI_REALTIME_URL}?model={self.model}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        self.websocket = await websockets.connect(url, additional_headers=headers)

        self.audio_in_queue = asyncio.Queue()
        self.audio_out_queue = asyncio.Queue()

        logger.info("Connected to OpenAI Realtime")
        print(f"=== OPENAI: Connected to Realtime API ===", flush=True)

        # Configure the session
        await self._configure_session()

        # Start processing tasks
        self._is_running = True
        self._tasks = [
            asyncio.create_task(self._receive_from_openai()),
            asyncio.create_task(self._send_audio_to_openai()),
        ]

        # Send initial prompt if provided
        if initial_prompt:
            print(f"=== OPENAI: Sending initial prompt: {initial_prompt[:100]}... ===", flush=True)
            await self.send_text(initial_prompt)

    async def _configure_session(self):
        """Configure the OpenAI Realtime session."""
        config = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": self.system_instruction,
                "voice": self.voice,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": "whisper-1"
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.6,  # Slightly higher = less false triggers
                    "prefix_padding_ms": 200,  # Reduced from 300ms
                    "silence_duration_ms": 300,  # Reduced from 500ms - faster response
                },
            }
        }
        await self.websocket.send(json.dumps(config))
        print(f"=== OPENAI: Session configured with voice={self.voice} ===", flush=True)

    async def disconnect(self):
        """Disconnect from OpenAI Realtime API."""
        logger.info("Disconnecting from OpenAI Realtime")
        self._is_running = False

        # Cancel tasks
        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Close websocket
        if self.websocket:
            await self.websocket.close()
            self.websocket = None

        logger.info("Disconnected from OpenAI Realtime")

    async def send_audio(self, audio_data: bytes):
        """Send audio data to OpenAI.

        Args:
            audio_data: PCM 16-bit audio bytes
        """
        if audio_data and len(audio_data) > 0:
            await self.audio_out_queue.put(audio_data)

    async def _send_audio_to_openai(self):
        """Background task to send audio to OpenAI."""
        chunks_sent = 0
        try:
            while self._is_running:
                # Get audio chunk
                audio_data = await self.audio_out_queue.get()

                # Batch multiple chunks if available
                audio_buffer = audio_data
                batch_count = 1
                while batch_count < 10:
                    try:
                        extra = self.audio_out_queue.get_nowait()
                        audio_buffer += extra
                        batch_count += 1
                    except asyncio.QueueEmpty:
                        break

                # Send as base64 encoded audio
                audio_b64 = base64.b64encode(audio_buffer).decode('utf-8')
                event = {
                    "type": "input_audio_buffer.append",
                    "audio": audio_b64,
                }
                await self.websocket.send(json.dumps(event))

                chunks_sent += batch_count
                if chunks_sent % 100 == 0:
                    print(f"=== OPENAI: Sent {chunks_sent} audio chunks ===", flush=True)

        except asyncio.CancelledError:
            print(f"=== OPENAI: Send task cancelled after {chunks_sent} chunks ===", flush=True)
        except Exception as e:
            logger.error("Error sending to OpenAI", error=str(e))
            print(f"=== OPENAI SEND ERROR: {e} ===", flush=True)

    async def _receive_from_openai(self):
        """Background task to receive events from OpenAI."""
        audio_chunk_count = 0
        try:
            print("=== OPENAI: Waiting for response... ===", flush=True)
            while self._is_running:
                message = await self.websocket.recv()
                event = json.loads(message)
                event_type = event.get("type", "")

                # Handle different event types
                if event_type == "response.audio.delta":
                    # Audio chunk from OpenAI
                    audio_b64 = event.get("delta", "")
                    if audio_b64:
                        audio_data = base64.b64decode(audio_b64)
                        audio_chunk_count += 1
                        if audio_chunk_count == 1:
                            print(f"=== OPENAI: First audio chunk received! {len(audio_data)} bytes ===", flush=True)
                        self.audio_in_queue.put_nowait(audio_data)
                        if self._on_audio:
                            await self._on_audio(audio_data)

                elif event_type == "response.audio_transcript.delta":
                    # Assistant transcript (what AI is saying)
                    text = event.get("delta", "")
                    if text:
                        print(f"=== OPENAI OUTPUT TRANSCRIPT: {text[:50]}... ===", flush=True)
                        if self._on_transcript:
                            await self._on_transcript(text, False)

                elif event_type == "conversation.item.input_audio_transcription.completed":
                    # User transcript (what user said) - this uses Whisper
                    transcript = event.get("transcript", "")
                    if transcript:
                        self._user_spoke = True
                        print(f"=== OPENAI USER TRANSCRIPT: {transcript} ===", flush=True)
                        if self._on_user_transcript:
                            await self._on_user_transcript(transcript)

                elif event_type == "input_audio_buffer.speech_started":
                    print(f"=== OPENAI: User started speaking ===", flush=True)

                elif event_type == "input_audio_buffer.speech_stopped":
                    print(f"=== OPENAI: User stopped speaking ===", flush=True)
                    # User turn complete
                    if self._user_spoke and self._on_user_turn_complete:
                        await self._on_user_turn_complete()
                        self._user_spoke = False

                elif event_type == "response.done":
                    # AI response complete
                    print(f"=== OPENAI: Response complete (audio chunks: {audio_chunk_count}) ===", flush=True)
                    if self._on_turn_complete:
                        await self._on_turn_complete()
                    audio_chunk_count = 0

                elif event_type == "error":
                    error = event.get("error", {})
                    print(f"=== OPENAI ERROR: {error} ===", flush=True)
                    logger.error("OpenAI Realtime error", error=error)

                elif event_type == "session.created":
                    print(f"=== OPENAI: Session created ===", flush=True)

                elif event_type == "session.updated":
                    print(f"=== OPENAI: Session updated ===", flush=True)

        except asyncio.CancelledError:
            print(f"=== OPENAI: Receive task cancelled ===", flush=True)
        except websockets.exceptions.ConnectionClosed as e:
            print(f"=== OPENAI: Connection closed: {e} ===", flush=True)
        except Exception as e:
            logger.error("Error receiving from OpenAI", error=str(e))
            print(f"=== OPENAI ERROR: {e} ===", flush=True)

    async def get_audio(self) -> bytes:
        """Get audio chunk from OpenAI.

        Returns:
            Audio bytes (PCM 16-bit 24kHz)
        """
        return await self.audio_in_queue.get()

    async def send_text(self, text: str, end_of_turn: bool = True):
        """Send text to OpenAI to respond to.

        Args:
            text: Text for OpenAI to respond to
            end_of_turn: Whether to trigger a response
        """
        if self.websocket and text:
            print(f"=== OPENAI: Sending text: {text[:100]}... ===", flush=True)

            # Create a conversation item with the text
            event = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": text,
                        }
                    ]
                }
            }
            await self.websocket.send(json.dumps(event))

            # Trigger response if end of turn
            if end_of_turn:
                response_event = {"type": "response.create"}
                await self.websocket.send(json.dumps(response_event))
