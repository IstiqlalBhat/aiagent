"""Configuration management for Agentic AI."""

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class TwilioConfig(BaseModel):
    """Twilio configuration."""

    account_sid: str = Field(..., description="Twilio Account SID")
    auth_token: str = Field(..., description="Twilio Auth Token")
    from_number: str = Field(..., description="Twilio phone number to call from")


class GeminiConfig(BaseModel):
    """Gemini configuration."""

    api_key: str = Field(..., description="Gemini API key")
    model: str = Field(
        default="models/gemini-2.5-flash-preview-native-audio-dialog",
        description="Gemini model to use",
    )
    voice: str = Field(default="Zephyr", description="Voice for Gemini TTS")
    system_instruction: str = Field(
        default="You are a helpful AI assistant.",
        description="System instruction for Gemini",
    )


class GatewayConfig(BaseModel):
    """OpenClaw Gateway configuration."""

    url: str = Field(default="ws://127.0.0.1:18789", description="Gateway WebSocket URL")
    reconnect_max_attempts: int = Field(default=10, description="Max reconnection attempts")
    reconnect_base_delay: float = Field(default=1.0, description="Base delay for reconnection")
    reconnect_max_delay: float = Field(default=60.0, description="Max delay for reconnection")


class ServerConfig(BaseModel):
    """Server configuration."""

    host: str = Field(default="0.0.0.0", description="Server host")
    port: int = Field(default=8080, description="Server port")
    webhook_path: str = Field(default="/twilio/voice", description="TwiML webhook path")
    websocket_path: str = Field(default="/twilio/media-stream", description="WebSocket path")


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = Field(default="INFO", description="Log level")
    format: str = Field(default="json", description="Log format (json or console)")


class Config(BaseModel):
    """Main configuration."""

    twilio: TwilioConfig
    gemini: GeminiConfig
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def _expand_env_vars(value: Any) -> Any:
    """Recursively expand environment variables in config values."""
    if isinstance(value, str):
        # Match ${VAR_NAME} pattern
        pattern = r"\$\{([^}]+)\}"
        matches = re.findall(pattern, value)
        for match in matches:
            env_value = os.environ.get(match, "")
            value = value.replace(f"${{{match}}}", env_value)
        return value
    elif isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    return value


def load_config(config_path: str | Path | None = None) -> Config:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, looks for config.yaml in current dir.

    Returns:
        Loaded configuration.
    """
    if config_path is None:
        config_path = Path("config.yaml")
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path) as f:
        raw_config = yaml.safe_load(f)

    # Expand environment variables
    expanded_config = _expand_env_vars(raw_config)

    return Config(**expanded_config)


def load_schedules(schedules_path: str | Path | None = None) -> dict:
    """Load schedules from YAML file.

    Args:
        schedules_path: Path to schedules file. If None, looks for schedules.yaml.

    Returns:
        Loaded schedules configuration.
    """
    if schedules_path is None:
        schedules_path = Path("schedules.yaml")
    else:
        schedules_path = Path(schedules_path)

    if not schedules_path.exists():
        return {"schedules": []}

    with open(schedules_path) as f:
        return yaml.safe_load(f) or {"schedules": []}
