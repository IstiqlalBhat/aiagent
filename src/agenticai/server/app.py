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
        
        Handles both:
        - Outbound calls (we initiated them)
        - Incoming calls (someone called our Twilio number)
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
        direction = form_data.get("Direction", "")  # "inbound" or "outbound-api"

        logger.info(
            "Voice webhook called",
            call_sid=call_sid,
            from_number=from_number,
            to_number=to_number,
            direction=direction,
            ws_url=ws_url,
        )

        call_manager = get_call_manager()
        
        # Check if this is an outbound call we initiated
        call_info = call_manager.get_pending_call_info(call_sid)
        
        if call_info:
            # Outbound call - use the prompt we set
            prompt = call_info.get("prompt", "")
            logger.info("Outbound call", prompt_length=len(prompt))
        else:
            # INCOMING CALL - create a session on-the-fly
            logger.info("Incoming call detected", from_number=from_number)
            
            # Register this as an incoming call
            call_id = await call_manager.register_incoming_call(
                call_sid=call_sid,
                from_number=from_number,
                to_number=to_number,
            )
            
            # Use a default prompt for incoming calls
            prompt = config.gemini.system_instruction or (
                "You are Alchemy, an AI agent created by Istiqlal. "
                "Be helpful, friendly, and assist the caller with whatever they need. "
                "You can send messages, search the web, make notes, and more."
            )
            logger.info("Created session for incoming call", call_id=call_id)

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
        
        This allows external tools (like ClawdBot) to trigger calls.
        
        Body:
            {
                "to": "+1234567890",  # Required: Phone number to call
                "prompt": "...",       # Optional: Custom prompt
                "webhook_url": "...",  # Optional: Uses AGENTICAI_WEBHOOK_URL env if not set
                "metadata": {}         # Optional: Extra metadata
            }
        """
        import os
        
        data = await request.json()
        to_number = data.get("to")
        prompt = data.get("prompt")
        webhook_url = data.get("webhook_url")
        metadata = data.get("metadata", {})
        
        config = get_config()

        if not to_number:
            return {"success": False, "error": "Missing 'to' phone number"}
        
        # Use environment variable if webhook_url not provided
        if not webhook_url:
            webhook_url = os.environ.get("AGENTICAI_WEBHOOK_URL") or os.environ.get("NGROK_URL")
        
        if not webhook_url:
            return {
                "success": False, 
                "error": "Missing 'webhook_url'. Set AGENTICAI_WEBHOOK_URL environment variable or pass in request."
            }
        
        # Use default prompt if not provided
        if not prompt:
            prompt = config.gemini.system_instruction or "You are a helpful AI assistant making a phone call."

        call_manager = get_call_manager()
        
        try:
            call_id = await call_manager.initiate_call(
                to_number=to_number,
                prompt=prompt,
                webhook_base_url=webhook_url,
                metadata=metadata,
            )
            return {
                "success": True, 
                "call_id": call_id,
                "to": to_number,
                "webhook_url": webhook_url,
            }
        except Exception as e:
            logger.error("Failed to initiate call", error=str(e))
            return {"success": False, "error": str(e)}
    
    @app.get("/api/calls")
    async def api_list_calls():
        """List active calls."""
        call_manager = get_call_manager()
        
        calls = []
        for call_id, session in call_manager.active_sessions.items():
            calls.append({
                "call_id": call_id,
                "to_number": session.to_number,
                "status": session.status,
                "direction": session.metadata.get("direction", "outbound"),
            })
        
        return {"calls": calls, "count": len(calls)}
    
    @app.post("/api/calls/{call_id}/end")
    async def api_end_call(call_id: str):
        """End an active call."""
        call_manager = get_call_manager()
        
        if call_id not in call_manager.active_sessions:
            return {"success": False, "error": "Call not found"}
        
        try:
            await call_manager.end_call(call_id)
            return {"success": True, "message": f"Call {call_id} ended"}
        except Exception as e:
            return {"success": False, "error": str(e)}

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
