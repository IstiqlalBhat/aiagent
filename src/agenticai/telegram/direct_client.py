"""Direct Telegram Bot API client (bypasses ClawdBot)."""

import requests
from typing import Optional
import structlog

logger = structlog.get_logger(__name__)


class TelegramDirectClient:
    """Simple Telegram Bot API client for sending messages directly."""

    def __init__(self, bot_token: str, chat_id: str):
        """Initialize Telegram client.

        Args:
            bot_token: Telegram bot token
            chat_id: Default chat ID to send messages to
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        parse_mode: str = "Markdown",
        disable_notification: bool = False,
    ) -> dict:
        """Send a text message.

        Args:
            text: Message text
            chat_id: Override default chat ID
            parse_mode: Message formatting (Markdown, HTML, or None)
            disable_notification: Send silently

        Returns:
            API response dict
        """
        url = f"{self.base_url}/sendMessage"

        payload = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }

        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            response = requests.post(url, json=payload, timeout=10)
            data = response.json()

            if data.get("ok"):
                logger.info("Message sent to Telegram", chat_id=chat_id or self.chat_id)
                return data
            else:
                error = data.get("description", "Unknown error")
                logger.error("Failed to send Telegram message", error=error)
                return data

        except Exception as e:
            logger.error("Telegram API error", error=str(e))
            return {"ok": False, "error": str(e)}

    def send_transcript(
        self,
        speaker: str,
        text: str,
        is_final: bool = True,
        chat_id: Optional[str] = None,
    ) -> dict:
        """Send a formatted transcript message.

        Args:
            speaker: "user" or "assistant"
            text: Transcript text
            is_final: Whether this is a final transcript
            chat_id: Override default chat ID

        Returns:
            API response dict
        """
        icon = "🤖" if speaker == "assistant" else "👤"
        label = "Assistant" if speaker == "assistant" else "User"
        status = "" if is_final else " _(partial)_"

        message = f"{icon} *{label}*{status}\n{text}"

        return self.send_message(message, chat_id=chat_id)

    def send_call_started(
        self,
        call_id: str,
        prompt: str,
        to_number: str = "test",
        chat_id: Optional[str] = None,
    ) -> dict:
        """Send call started notification.

        Args:
            call_id: Unique call identifier
            prompt: Call prompt/purpose
            to_number: Phone number or "test"
            chat_id: Override default chat ID

        Returns:
            API response dict
        """
        message = f"""📞 *Call Started*

*Call ID:* `{call_id}`
*To:* {to_number}
*Prompt:* {prompt}

_Listening for transcripts..._"""

        return self.send_message(message, chat_id=chat_id)

    def send_call_ended(
        self,
        call_id: str,
        duration: float,
        transcript_count: int,
        outcome: str = "completed",
        summary: str = "",
        chat_id: Optional[str] = None,
    ) -> dict:
        """Send call ended notification.

        Args:
            call_id: Unique call identifier
            duration: Call duration in seconds
            transcript_count: Number of transcripts
            outcome: Call outcome (completed, failed, etc.)
            summary: Optional summary text
            chat_id: Override default chat ID

        Returns:
            API response dict
        """
        status_icon = "✅" if outcome == "completed" else "❌"

        message = f"""{status_icon} *Call Ended*

*Call ID:* `{call_id}`
*Duration:* {duration:.1f}s
*Transcripts:* {transcript_count}
*Outcome:* {outcome}"""

        if summary:
            message += f"\n\n*Summary:*\n{summary}"

        return self.send_message(message, chat_id=chat_id)

    def test_connection(self, chat_id: Optional[str] = None) -> bool:
        """Test the Telegram connection.

        Args:
            chat_id: Override default chat ID

        Returns:
            True if connection works
        """
        response = self.send_message(
            "✅ *Agentic AI Connected*\n\nTelegram integration is working!",
            chat_id=chat_id,
        )

        return response.get("ok", False)
