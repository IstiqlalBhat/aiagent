"""FastAPI server for Twilio TwiML webhook and Media Streams."""

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, WebSocket, Request, Response
from fastapi.responses import PlainTextResponse

from ..core.config import Config, load_config
from ..core.call_manager import CallManager
from ..twilio.websocket import TwilioMediaStreamHandler

logger = structlog.get_logger(__name__)

# Global state
_config: Config | None = None
_call_manager: CallManager | None = None


def get_config() -> Config:
    """Get the loaded configuration."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def get_call_manager() -> CallManager:
    """Get the call manager instance."""
    global _call_manager
    if _call_manager is None:
        config = get_config()
        _call_manager = CallManager(config)
    return _call_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("Starting Agentic AI server")

    # Initialize call manager
    call_manager = get_call_manager()
    await call_manager.start()

    yield

    # Cleanup
    logger.info("Shutting down Agentic AI server")
    await call_manager.stop()


def create_app(config: Config | None = None) -> FastAPI:
    """Create the FastAPI application.

    Args:
        config: Optional configuration. If None, loads from config.yaml.

    Returns:
        FastAPI application instance
    """
    global _config, _call_manager

    if config:
        _config = config

    app = FastAPI(
        title="Agentic AI",
        description="Twilio + Gemini + OpenClaw Gateway Integration",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        call_manager = get_call_manager()
        return {
            "status": "healthy",
            "active_calls": len(call_manager.active_sessions),
        }

    @app.post("/twilio/voice")
    async def twilio_voice_webhook(request: Request) -> Response:
        """TwiML webhook for incoming/outbound calls.

        Returns TwiML to connect Media Streams.
        """
        # Get the base URL for WebSocket connection
        config = get_config()

        # Construct WebSocket URL - use the ngrok URL for public access
        host = request.headers.get("host", f"localhost:{config.server.port}")
        ws_protocol = "wss" if "ngrok" in host or request.url.scheme == "https" else "ws"
        ws_url = f"{ws_protocol}://{host}{config.server.websocket_path}"

        # Parse form data to get call info
        form_data = await request.form()
        call_sid = form_data.get("CallSid", "")
        from_number = form_data.get("From", "")
        to_number = form_data.get("To", "")

        logger.info(
            "Voice webhook called",
            call_sid=call_sid,
            from_number=from_number,
            to_number=to_number,
            ws_url=ws_url,
        )

        # Get custom parameters if this call was initiated with them
        call_manager = get_call_manager()
        call_info = call_manager.get_pending_call_info(call_sid)
        prompt = call_info.get("prompt", "") if call_info else ""
        
        logger.info("Call info found", has_call_info=bool(call_info), prompt_length=len(prompt))

        # Return TwiML with Stream instruction
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}">
            <Parameter name="prompt" value="{prompt}" />
            <Parameter name="call_sid" value="{call_sid}" />
        </Stream>
    </Connect>
</Response>"""

        logger.info("Returning TwiML", twiml_length=len(twiml))
        return Response(content=twiml, media_type="application/xml")

    @app.post("/twilio/status")
    async def twilio_status_callback(request: Request):
        """Status callback for call events."""
        form_data = await request.form()
        call_sid = form_data.get("CallSid", "")
        call_status = form_data.get("CallStatus", "")

        logger.info("Call status update", call_sid=call_sid, status=call_status)

        # Notify call manager of status change
        call_manager = get_call_manager()
        await call_manager.handle_call_status(call_sid, call_status)

        return {"status": "ok"}

    @app.post("/api/call")
    async def api_initiate_call(request: Request):
        """API endpoint to initiate a call.
        
        This allows external tools to trigger calls through the running server.
        """
        data = await request.json()
        to_number = data.get("to")
        prompt = data.get("prompt", "You are a helpful AI assistant making a phone call.")
        webhook_url = data.get("webhook_url")
        metadata = data.get("metadata", {})

        if not to_number:
            return {"error": "Missing 'to' phone number"}
        if not webhook_url:
            return {"error": "Missing 'webhook_url'"}

        call_manager = get_call_manager()
        
        try:
            call_id = await call_manager.initiate_call(
                to_number=to_number,
                prompt=prompt,
                webhook_base_url=webhook_url,
                metadata=metadata,
            )
            return {"success": True, "call_id": call_id}
        except Exception as e:
            logger.error("Failed to initiate call", error=str(e))
            return {"error": str(e)}

    @app.websocket("/twilio/media-stream")
    async def twilio_media_stream(websocket: WebSocket):
        """WebSocket endpoint for Twilio Media Streams."""
        logger.info("WebSocket connection request received")
        
        handler = TwilioMediaStreamHandler(websocket)
        await handler.accept()
        logger.info("WebSocket connection accepted")

        call_manager = get_call_manager()

        try:
            # Process the stream
            logger.info("Starting media stream handler")
            await call_manager.handle_media_stream(handler)
            logger.info("Media stream handler completed")
        except Exception as e:
            logger.error("Error in media stream", error=str(e), exc_info=True)
        finally:
            await handler.close()
            logger.info("WebSocket connection closed")

    return app


def run_server(config: Config | None = None):
    """Run the server with uvicorn.

    Args:
        config: Optional configuration.
    """
    import uvicorn

    if config is None:
        config = get_config()

    app = create_app(config)

    # Disable uvloop due to Python 3.14 recursion bug with task cancellation
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level="info",
        loop="asyncio",  # Use standard asyncio instead of uvloop
    )
