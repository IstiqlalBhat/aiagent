"""Conversation brain with memory and intent understanding."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable
import json

import structlog
from google import genai
from google.genai import types

logger = structlog.get_logger(__name__)


@dataclass
class ConversationTurn:
    """A complete conversation turn."""
    speaker: str  # "user" or "assistant"
    text: str
    timestamp: datetime
    intent: str | None = None  # Extracted intent
    command: dict | None = None  # Parsed command


@dataclass 
class ConversationMemory:
    """Memory for the conversation."""
    call_id: str
    turns: list[ConversationTurn] = field(default_factory=list)
    context: dict = field(default_factory=dict)  # Persistent context
    extracted_info: dict = field(default_factory=dict)  # Info extracted from conversation
    
    def add_turn(self, speaker: str, text: str, intent: str = None, command: dict = None):
        """Add a conversation turn."""
        turn = ConversationTurn(
            speaker=speaker,
            text=text,
            timestamp=datetime.now(),
            intent=intent,
            command=command,
        )
        self.turns.append(turn)
        return turn
    
    def get_recent_context(self, max_turns: int = 10) -> str:
        """Get recent conversation context as string."""
        recent = self.turns[-max_turns:] if len(self.turns) > max_turns else self.turns
        lines = []
        for turn in recent:
            speaker = "User" if turn.speaker == "user" else "Assistant"
            lines.append(f"{speaker}: {turn.text}")
        return "\n".join(lines)
    
    def to_summary(self) -> str:
        """Generate a summary of the conversation."""
        if not self.turns:
            return "No conversation yet."
        
        summary_parts = [f"Call ID: {self.call_id}"]
        summary_parts.append(f"Total turns: {len(self.turns)}")
        
        if self.extracted_info:
            summary_parts.append(f"Extracted info: {json.dumps(self.extracted_info)}")
        
        # Last few turns
        summary_parts.append("\nRecent conversation:")
        summary_parts.append(self.get_recent_context(5))
        
        return "\n".join(summary_parts)


class ConversationBrain:
    """Brain that understands intent and manages conversation memory.
    
    Uses Gemini to:
    - Understand user intent from transcripts
    - Extract commands and parameters
    - Maintain conversation context
    """
    
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3-flash-preview",  # Latest fast model for intent understanding
        telegram_client = None,
        call_id: str = "",
    ):
        """Initialize the conversation brain.
        
        Args:
            api_key: Gemini API key
            model: Model for intent understanding
            telegram_client: Telegram client for sending commands
            call_id: Call identifier
        """
        self.api_key = api_key
        self.model = model
        self.telegram = telegram_client
        
        self.client = genai.Client(api_key=api_key)
        self.memory = ConversationMemory(call_id=call_id)
        
        # Transcript buffers
        self._assistant_buffer: list[str] = []
        self._user_buffer: list[str] = []
        self._last_assistant_flush = datetime.now()
        self._last_user_flush = datetime.now()
        
        # Callbacks
        self._on_command: Callable[[str, dict], Awaitable[None]] | None = None
    
    def set_callbacks(
        self,
        on_command: Callable[[str, dict], Awaitable[None]] | None = None,
    ):
        """Set event callbacks."""
        self._on_command = on_command
    
    def add_assistant_transcript(self, text: str):
        """Add assistant transcript fragment (word-by-word)."""
        self._assistant_buffer.append(text.strip())
    
    def add_user_transcript(self, text: str):
        """Add user transcript fragment."""
        self._user_buffer.append(text.strip())
    
    async def flush_assistant_turn(self):
        """Flush buffered assistant transcript as a complete turn."""
        if not self._assistant_buffer:
            return
        
        full_text = " ".join(self._assistant_buffer).strip()
        self._assistant_buffer.clear()
        
        if full_text:
            self.memory.add_turn("assistant", full_text)
            print(f"=== BRAIN: Assistant said: {full_text[:100]}... ===", flush=True)
            
            # Send Clawdy's response to Telegram
            if self.telegram:
                self.telegram.send_message(f"🤖 *Clawdy*: {full_text}")
    
    async def flush_user_turn(self):
        """Flush buffered user transcript as a complete turn and analyze intent."""
        if not self._user_buffer:
            return
        
        full_text = " ".join(self._user_buffer).strip()
        self._user_buffer.clear()
        
        if not full_text:
            return
        
        print(f"=== BRAIN: User said: {full_text[:100]}... ===", flush=True)
        
        # Analyze intent with Gemini 3
        intent, command = await self._analyze_intent(full_text)
        
        self.memory.add_turn("user", full_text, intent=intent, command=command)
        
        # Send user message with intent to Telegram
        if self.telegram:
            msg = f"👤 *User*: {full_text}"
            if intent and intent != "conversation":
                msg += f"\n📌 Intent: `{intent}`"
                # If there's a command, include key details
                if command:
                    if command.get("message"):
                        msg += f"\n💬 Message: _{command['message']}_"
                    if command.get("recipient"):
                        msg += f"\n📍 To: _{command['recipient']}_"
            self.telegram.send_message(msg)
        
        # Execute command callback if set
        if command and self._on_command:
            await self._on_command(command.get("action", ""), command)
    
    async def _analyze_intent(self, user_text: str) -> tuple[str, dict | None]:
        """Analyze user text to extract intent and command.
        
        Args:
            user_text: What the user said
            
        Returns:
            Tuple of (intent, command_dict or None)
        """
        try:
            context = self.memory.get_recent_context(5)
            
            prompt = f"""Analyze this user request and extract the intent.

Recent conversation:
{context}

User just said: "{user_text}"

Respond with JSON only:
{{
  "intent": "one of: send_message, make_call, search_web, set_reminder, take_note, get_info, conversation, unclear",
  "confidence": 0.0-1.0,
  "command": {{
    "action": "the intent",
    "recipient": "who (if applicable)",
    "message": "what to send (if applicable)", 
    "query": "search query (if applicable)",
    "details": "other relevant details"
  }} or null if just conversation,
  "extracted_entities": {{
    "names": [],
    "phone_numbers": [],
    "dates": [],
    "locations": []
  }}
}}"""

            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
            )
            
            # Parse JSON response
            response_text = response.text.strip()
            # Remove markdown code blocks if present
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]
            
            data = json.loads(response_text)
            
            intent = data.get("intent", "conversation")
            command = data.get("command")
            
            # Update extracted info in memory
            if data.get("extracted_entities"):
                for key, values in data["extracted_entities"].items():
                    if values:
                        if key not in self.memory.extracted_info:
                            self.memory.extracted_info[key] = []
                        self.memory.extracted_info[key].extend(values)
            
            print(f"=== BRAIN: Intent={intent}, Command={command} ===", flush=True)
            
            return intent, command
            
        except Exception as e:
            logger.error("Error analyzing intent", error=str(e))
            print(f"=== BRAIN ERROR: {e} ===", flush=True)
            return "conversation", None
    
    def get_memory_summary(self) -> str:
        """Get a summary of the conversation memory."""
        return self.memory.to_summary()
    
    def get_extracted_info(self) -> dict:
        """Get information extracted from the conversation."""
        return self.memory.extracted_info
    
    def send_call_summary(self, duration: float):
        """Send a concise call summary to Telegram.
        
        Args:
            duration: Call duration in seconds
        """
        if not self.telegram:
            return
        
        # Build concise summary
        commands = []
        for turn in self.memory.turns:
            if turn.command and turn.intent != "conversation":
                cmd_summary = turn.intent
                if turn.command.get("message"):
                    cmd_summary += f": {turn.command['message'][:50]}"
                if turn.command.get("recipient"):
                    cmd_summary += f" → {turn.command['recipient']}"
                commands.append(cmd_summary)
        
        if commands:
            # There were actionable commands
            msg = f"📋 *Call Summary* ({duration:.0f}s)\n"
            msg += "\n".join([f"• `{cmd}`" for cmd in commands])
            
            # Add extracted entities if any
            if self.memory.extracted_info:
                info_parts = []
                for key, values in self.memory.extracted_info.items():
                    if values:
                        info_parts.append(f"{key}: {', '.join(str(v) for v in values[:3])}")
                if info_parts:
                    msg += f"\n\n📎 Extracted: _{', '.join(info_parts)}_"
        else:
            # Just conversation, no commands
            msg = f"📋 *Call ended* ({duration:.0f}s) - No actionable commands"
        
        self.telegram.send_message(msg)

