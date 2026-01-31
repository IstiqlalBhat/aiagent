"""WebSocket client for OpenClaw Gateway."""

import asyncio
import json
from typing import Any

import structlog
import websockets
from websockets.client import WebSocketClientProtocol

from .messages import GatewayMessage, HeartbeatMessage

logger = structlog.get_logger(__name__)


class GatewayClient:
    """WebSocket client for OpenClaw Gateway.

    Handles:
    - Connection management with reconnection
    - Message sending via sessions_send RPC
    - Heartbeat keepalive
    """

    def __init__(
        self,
        url: str = "ws://127.0.0.1:18789",
        max_reconnect_attempts: int = 10,
        reconnect_base_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
    ):
        """Initialize the gateway client.

        Args:
            url: WebSocket URL for the gateway
            max_reconnect_attempts: Maximum reconnection attempts
            reconnect_base_delay: Base delay for exponential backoff
            reconnect_max_delay: Maximum delay between reconnection attempts
        """
        self.url = url
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_base_delay = reconnect_base_delay
        self.reconnect_max_delay = reconnect_max_delay

        self._ws: WebSocketClientProtocol | None = None
        self._is_connected = False
        self._should_run = False
        self._reconnect_attempts = 0
        self._message_id = 0

        # Tasks
        self._heartbeat_task: asyncio.Task | None = None
        self._receive_task: asyncio.Task | None = None

        # Message queue for when disconnected
        self._pending_messages: asyncio.Queue[GatewayMessage] = asyncio.Queue(maxsize=1000)

    @property
    def is_connected(self) -> bool:
        """Check if connected to gateway."""
        return self._is_connected

    async def connect(self) -> None:
        """Connect to the gateway with automatic reconnection."""
        self._should_run = True
        await self._connect()

    async def _connect(self) -> None:
        """Establish connection to the gateway."""
        while self._should_run and self._reconnect_attempts < self.max_reconnect_attempts:
            try:
                logger.info("Connecting to gateway", url=self.url)

                self._ws = await websockets.connect(
                    self.url,
                    ping_interval=30,
                    ping_timeout=10,
                )

                self._is_connected = True
                self._reconnect_attempts = 0

                logger.info("Connected to gateway")

                # Start heartbeat
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                # Start receive loop
                self._receive_task = asyncio.create_task(self._receive_loop())

                # Send any pending messages
                await self._flush_pending_messages()

                # Wait for connection to close
                await self._receive_task

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning("Gateway connection closed", code=e.code, reason=e.reason)
            except ConnectionRefusedError:
                logger.warning("Gateway connection refused")
            except Exception as e:
                logger.error("Gateway connection error", error=str(e))

            self._is_connected = False

            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass

            if self._should_run:
                # Calculate backoff delay
                delay = min(
                    self.reconnect_base_delay * (2 ** self._reconnect_attempts),
                    self.reconnect_max_delay,
                )
                self._reconnect_attempts += 1

                logger.info(
                    "Reconnecting to gateway",
                    attempt=self._reconnect_attempts,
                    delay=delay,
                )

                await asyncio.sleep(delay)

        if self._reconnect_attempts >= self.max_reconnect_attempts:
            logger.error("Max reconnection attempts reached")

    async def disconnect(self) -> None:
        """Disconnect from the gateway."""
        logger.info("Disconnecting from gateway")
        self._should_run = False
        self._is_connected = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()
            self._ws = None

        logger.info("Disconnected from gateway")

    async def send_message(self, message: GatewayMessage) -> None:
        """Send a message to the gateway.

        Args:
            message: Message to send
        """
        if self._is_connected and self._ws:
            await self._send_rpc(message)
        else:
            # Queue message for later
            try:
                self._pending_messages.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("Pending message queue full, dropping message")

    async def _send_rpc(self, message: GatewayMessage) -> None:
        """Send message via sessions_send RPC method.

        Args:
            message: Message to send
        """
        if not self._ws:
            return

        self._message_id += 1

        # Format as JSON-RPC 2.0
        rpc_message = {
            "jsonrpc": "2.0",
            "id": self._message_id,
            "method": "sessions_send",
            "params": {
                "message": message.to_dict(),
            },
        }

        try:
            await self._ws.send(json.dumps(rpc_message))
            logger.debug("Sent message to gateway", type=message.message_type)
        except Exception as e:
            logger.error("Failed to send message", error=str(e))
            self._is_connected = False

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeat messages."""
        try:
            while self._is_connected:
                await asyncio.sleep(30)
                await self._send_rpc(HeartbeatMessage())
        except asyncio.CancelledError:
            pass

    async def _receive_loop(self) -> None:
        """Receive and process messages from gateway."""
        if not self._ws:
            return

        try:
            async for message in self._ws:
                await self._handle_message(message)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Connection closed in receive loop")
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, message: str) -> None:
        """Handle a message from the gateway.

        Args:
            message: JSON message string
        """
        try:
            data = json.loads(message)

            # Handle JSON-RPC response
            if "result" in data:
                logger.debug("RPC response received", id=data.get("id"))
            elif "error" in data:
                logger.error(
                    "RPC error received",
                    id=data.get("id"),
                    error=data.get("error"),
                )
            else:
                # Handle notifications/events from gateway
                logger.debug("Gateway notification", data=data)

        except json.JSONDecodeError:
            logger.error("Failed to parse gateway message", message=message[:100])

    async def _flush_pending_messages(self) -> None:
        """Send all pending messages after reconnection."""
        while not self._pending_messages.empty():
            try:
                message = self._pending_messages.get_nowait()
                await self._send_rpc(message)
            except asyncio.QueueEmpty:
                break
            except Exception as e:
                logger.error("Failed to send pending message", error=str(e))
