"""Twilio REST API client for initiating outbound calls."""

import structlog
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

logger = structlog.get_logger(__name__)


class TwilioClient:
    """Client for Twilio REST API operations."""

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        """Initialize Twilio client.

        Args:
            account_sid: Twilio Account SID
            auth_token: Twilio Auth Token
            from_number: Phone number to call from
        """
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self._client = Client(account_sid, auth_token)

    def initiate_call(
        self,
        to_number: str,
        webhook_url: str,
        status_callback_url: str | None = None,
        timeout: int = 30,
    ) -> str:
        """Initiate an outbound call.

        Args:
            to_number: Phone number to call
            webhook_url: URL for TwiML webhook (returns <Stream> TwiML)
            status_callback_url: Optional URL for call status updates
            timeout: Ring timeout in seconds

        Returns:
            Call SID
        """
        logger.info(
            "Initiating outbound call",
            to=to_number,
            from_=self.from_number,
            webhook_url=webhook_url,
        )

        try:
            call = self._client.calls.create(
                to=to_number,
                from_=self.from_number,
                url=webhook_url,
                method="POST",
                status_callback=status_callback_url,
                status_callback_method="POST" if status_callback_url else None,
                timeout=timeout,
            )

            logger.info("Call initiated", call_sid=call.sid, status=call.status)
            return call.sid

        except TwilioRestException as e:
            logger.error("Failed to initiate call", error=str(e), code=e.code)
            raise

    def get_call_status(self, call_sid: str) -> dict:
        """Get the status of a call.

        Args:
            call_sid: Call SID to check

        Returns:
            Dict with call status information
        """
        call = self._client.calls(call_sid).fetch()
        return {
            "sid": call.sid,
            "status": call.status,
            "direction": call.direction,
            "duration": call.duration,
            "start_time": str(call.start_time) if call.start_time else None,
            "end_time": str(call.end_time) if call.end_time else None,
            "from": call.from_,
            "to": call.to,
        }

    def end_call(self, call_sid: str) -> None:
        """End an active call.

        Args:
            call_sid: Call SID to end
        """
        logger.info("Ending call", call_sid=call_sid)
        self._client.calls(call_sid).update(status="completed")

    def list_active_calls(self) -> list[dict]:
        """List all active calls.

        Returns:
            List of active call information
        """
        calls = self._client.calls.list(status="in-progress")
        return [
            {
                "sid": call.sid,
                "from": call.from_,
                "to": call.to,
                "direction": call.direction,
                "duration": call.duration,
            }
            for call in calls
        ]
