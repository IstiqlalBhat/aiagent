"""Conversation brain with memory and intent understanding."""

import asyncio
import subprocess
import threading
import traceback
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
        telegram_chat_id: str = "",
        call_id: str = "",
    ):
        """Initialize the conversation brain.
        
        Args:
            api_key: Gemini API key
            model: Model for intent understanding
            telegram_client: Telegram client (legacy, not used)
            telegram_chat_id: Telegram chat ID for ClawdBot agent
            call_id: Call identifier
        """
        self.api_key = api_key
        self.model = model
        self.telegram = telegram_client
        self.telegram_chat_id = telegram_chat_id
        
        self.client = genai.Client(api_key=api_key)
        self.memory = ConversationMemory(call_id=call_id)
        
        # Transcript buffers
        self._assistant_buffer: list[str] = []
        self._user_buffer: list[str] = []
        self._last_assistant_flush = datetime.now()
        self._last_user_flush = datetime.now()
        
        # Callbacks
        self._on_command: Callable[[str, dict], Awaitable[None]] | None = None
        self._on_clawdbot_response: Callable[[str], Awaitable[None]] | None = None  # Callback to speak ClawdBot's response

    async def _send_to_clawdbot_async(self, command: str) -> str | None:
        """Send command to ClawdBot agent and wait for response.

        Args:
            command: The command to execute

        Returns:
            ClawdBot's response text, or None if failed
        """
        try:
            if not self.telegram_chat_id:
                print(f"=== BRAIN ERROR: telegram_chat_id is empty! ===", flush=True)
                return None

            # Convert literal \n strings to actual newlines (for email composition)
            # This handles cases where the user says "new line" and it gets transcribed as \n
            processed_command = command.replace('\\n', '\n')

            # Use clawdbot agent WITHOUT --deliver to get response directly
            # We'll speak the response via Gemini instead of sending to Telegram
            cmd = [
                "clawdbot", "agent",
                "--session-id", "agent:main:main",
                "--message", processed_command,
                "--timeout", "90",
            ]
            print(f"=== BRAIN: Sending to ClawdBot agent ===", flush=True)
            print(f"=== BRAIN: Command: {' '.join(cmd)} ===", flush=True)

            # Run async and capture output
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**dict(__import__('os').environ), 'GOG_ACCOUNT': 'istiqlalclemson@gmail.com'},
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=95
                )
            except asyncio.TimeoutError:
                process.kill()
                print(f"=== BRAIN: ClawdBot timeout ===", flush=True)
                return "I'm still working on that. It's taking longer than expected."

            response_text = stdout.decode('utf-8').strip() if stdout else None

            # Filter out deprecation warnings and extract actual response
            if response_text:
                lines = response_text.split('\n')
                # Skip lines that are warnings or empty
                clean_lines = [
                    line for line in lines
                    if line.strip()
                    and 'DeprecationWarning' not in line
                    and not line.startswith('(node:')
                    and not line.startswith('(Use `node')
                ]
                response_text = '\n'.join(clean_lines).strip()

            if response_text:
                print(f"=== BRAIN: ClawdBot response: {response_text[:300]}... ===", flush=True)
            else:
                print(f"=== BRAIN: ClawdBot returned empty response ===", flush=True)

            if stderr:
                stderr_text = stderr.decode('utf-8')
                if 'DeprecationWarning' not in stderr_text:
                    print(f"=== BRAIN: ClawdBot stderr: {stderr_text[:200]} ===", flush=True)

            return response_text

        except Exception as e:
            print(f"=== BRAIN: ClawdBot error: {e} ===", flush=True)
            traceback.print_exc()
            return f"Sorry, I encountered an error: {str(e)}"
    
    def set_callbacks(
        self,
        on_command: Callable[[str, dict], Awaitable[None]] | None = None,
        on_clawdbot_response: Callable[[str], Awaitable[None]] | None = None,
    ):
        """Set event callbacks.

        Args:
            on_command: Called when a command is detected
            on_clawdbot_response: Called with ClawdBot's response to speak it
        """
        self._on_command = on_command
        self._on_clawdbot_response = on_clawdbot_response
    
    def add_assistant_transcript(self, text: str):
        """Add assistant transcript fragment.

        Gemini sends incremental fragments. We concatenate them directly
        (no added spaces) - Gemini includes spaces where needed.
        """
        if text:
            self._assistant_buffer.append(text)
    
    def add_user_transcript(self, text: str):
        """Add user transcript fragment.

        Gemini sends incremental fragments. We concatenate them directly
        (no added spaces) - Gemini includes spaces where needed.
        """
        if text:
            self._user_buffer.append(text)
            # Log buffer state
            current = "".join(self._user_buffer)
            print(f"=== BRAIN BUFFER: {len(self._user_buffer)} fragments, current: \"{current[:80]}...\" ===", flush=True)
    
    async def flush_assistant_turn(self):
        """Flush buffered assistant transcript as a complete turn."""
        if not self._assistant_buffer:
            return

        # Concatenate fragments directly (Gemini includes spaces where needed)
        full_text = "".join(self._assistant_buffer).strip()
        self._assistant_buffer.clear()
        
        if full_text:
            self.memory.add_turn("assistant", full_text)
            print(f"=== BRAIN: Assistant said: {full_text[:100]}... ===", flush=True)
            # Note: Don't send Alchemy's responses to Telegram
            # Only executable commands should go to Telegram for ClawdBot to act on
    
    async def flush_user_turn(self):
        """Flush buffered user transcript as a complete turn and analyze intent."""
        if not self._user_buffer:
            return

        # Concatenate fragments directly (Gemini includes spaces where needed)
        full_text = "".join(self._user_buffer).strip()
        num_fragments = len(self._user_buffer)
        self._user_buffer.clear()

        if not full_text:
            print(f"=== BRAIN FLUSH: Empty buffer, skipping ===", flush=True)
            return

        print(f"=== BRAIN FLUSH: {num_fragments} fragments → \"{full_text}\" ===", flush=True)

        # Quick check if this looks actionable
        intent, command, is_actionable = await self._analyze_intent(full_text)

        self.memory.add_turn("user", full_text, intent=intent, command=command)

        # Send to ClawdBot if actionable and get response
        if is_actionable:
            print(f"=== BRAIN: Sending to ClawdBot: \"{full_text}\" ===", flush=True)

            # Get response from ClawdBot
            response = await self._send_to_clawdbot_async(full_text)

            if response and self._on_clawdbot_response:
                # Feed response back to Gemini to speak it
                print(f"=== BRAIN: Feeding response to Gemini to speak ===", flush=True)
                await self._on_clawdbot_response(response)
            elif response:
                print(f"=== BRAIN: Got response but no callback to speak it ===", flush=True)
        else:
            print(f"=== BRAIN: Not actionable, skipping ClawdBot ===", flush=True)

        # Execute command callback if set
        if command and self._on_command:
            await self._on_command(command.get("action", ""), command)
    
    def _format_executable_command(self, intent: str, command: dict, original_text: str) -> str | None:
        """Format command for ClawdBot execution.

        ClawdBot has its own LLM, so we just send the natural language request
        directly without over-processing it.

        Args:
            intent: The detected intent
            command: Command details
            original_text: What the user originally said

        Returns:
            The original user request for ClawdBot to interpret, or None if not actionable
        """
        # Just send the original natural language request to ClawdBot
        # ClawdBot's LLM will understand and execute it
        if intent != "conversation":
            return original_text
        return None
    
    async def _analyze_intent(self, user_text: str) -> tuple[str, dict | None, bool]:
        """Analyze user text to determine if it's actionable.

        Uses a simple heuristic + LLM check to decide if the request
        should be forwarded to ClawdBot for execution.

        Args:
            user_text: What the user said

        Returns:
            Tuple of (intent, command_dict or None, is_actionable)
        """
        # Quick heuristics to skip LLM call for obvious cases (saves ~300-800ms)
        text_lower = user_text.lower().strip()

        # Skip LLM for greetings and simple phrases
        non_actionable_phrases = [
            "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
            "how are you", "what's up", "sup", "yo", "thanks", "thank you",
            "okay", "ok", "alright", "sure", "yes", "no", "yeah", "nope",
            "bye", "goodbye", "see you", "later", "nevermind", "never mind",
            "forget it", "forget about it", "nothing", "hmm", "um", "uh",
        ]

        if text_lower in non_actionable_phrases or len(text_lower) < 3:
            print(f"=== BRAIN: Quick skip (greeting/short) ===", flush=True)
            return "conversation", None, False

        # Quick actionable keywords (skip LLM, go straight to ClawdBot)
        action_keywords = [
            "open", "play", "search", "find", "send", "call", "text",
            "check", "show", "get", "set", "turn", "start", "stop",
            "email", "message", "youtube", "spotify", "browser", "google",
        ]

        if any(text_lower.startswith(kw) or f" {kw} " in f" {text_lower} " for kw in action_keywords):
            print(f"=== BRAIN: Quick action keyword detected ===", flush=True)
            return "action", {"original_request": user_text}, True

        try:
            context = self.memory.get_recent_context(5)

            # Simple, liberal prompt - let ClawdBot's LLM handle the details
            prompt = f"""You are a simple intent classifier. Determine if the user wants you to DO something or just chatting.

Recent conversation:
{context}

User said: "{user_text}"

Is this a request to DO something? (open app, search, play music, send message, make call, browse web, take notes, execute command, control device, etc.)

Answer with just ONE word: YES or NO

If the user is asking you to perform ANY action, task, or command - say YES.
If the user is just chatting, greeting, asking a question about yourself, or having casual conversation - say NO."""

            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
            )

            answer = response.text.strip().upper()
            is_actionable = answer.startswith("YES")

            # Simple classification
            intent = "action" if is_actionable else "conversation"

            # For actionable requests, create a simple command with the original text
            command = {"original_request": user_text} if is_actionable else None

            print(f"=== BRAIN: Actionable={is_actionable} (LLM said: {answer}) ===", flush=True)

            return intent, command, is_actionable

        except Exception as e:
            logger.error("Error analyzing intent", error=str(e))
            print(f"=== BRAIN ERROR: {e} ===", flush=True)
            # On error, assume it's actionable to avoid missing commands
            return "action", {"original_request": user_text}, True
    
    def get_memory_summary(self) -> str:
        """Get a summary of the conversation memory."""
        return self.memory.to_summary()
    
    def get_extracted_info(self) -> dict:
        """Get information extracted from the conversation."""
        return self.memory.extracted_info
    
    def send_call_summary(self, duration: float):
        """Log call summary (don't send to Telegram - only executable commands go there).
        
        Args:
            duration: Call duration in seconds
        """
        # Build summary for logging only
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
            print(f"=== BRAIN: Call ended ({duration:.0f}s) - Commands: {commands} ===", flush=True)
        else:
            print(f"=== BRAIN: Call ended ({duration:.0f}s) - No commands ===", flush=True)

