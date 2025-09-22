"""Configuration management for the Asterisk ARI library.

This module provides configuration classes using Pydantic for type safety,
validation, and environment variable integration.
"""

import os
from typing import Any, Dict, Optional, Union
from pydantic import Field, field_validator, model_validator, BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class ARIConfig(BaseModel):
    """
    Configuration class for Asterisk ARI connection settings.
    """

    # Default configuration – no env file is forced
    model_config = SettingsConfigDict(
        env_prefix="ASTERISK_ARI_",
        case_sensitive=False,
        validate_assignment=True,
        env_override=False,
        env_file=None,
        extra="ignore",  # allow unknown env vars without failing
    )

    # ────────────────────────── Connection settings ──────────────────────────
    host: str = Field(
        default="localhost",
        description="Asterisk server hostname"
    )

    port: int = Field(default=8088, ge=1000, le=65535, description="Asterisk ARI port")
    username: str = Field(..., description="ARI username")
    password: str = Field(..., description="ARI password")
    app_name: str = Field(..., description="Stasis application name")
    external_media_host: Optional[str] = Field(
        default=None, description="External media host (for streamed media)"
    )
    external_media_port: Optional[int] = Field(
        default=None, description="External media port (for streamed media)"
    )

    # ────────────────────────── Security settings ────────────────────────────
    use_ssl: bool = Field(default=False, description="Use HTTPS/WSS connections")
    verify_ssl: bool = Field(default=True, description="Verify SSL certificates")
    ssl_ca_bundle: Optional[str] = Field(None, description="Path to CA bundle")

    # ────────────────────────── Timeout / Retry ──────────────────────────────
    timeout: float = Field(default=30.0, gt=0, description="Request timeout in seconds")
    retry_attempts: int = Field(default=3, ge=0, description="Number of retry attempts")
    retry_backoff: float = Field(default=1.0, ge=0, description="Retry backoff factor")
    max_retry_delay: float = Field(default=60.0, gt=0, description="Max retry delay")

    # ────────────────────────── Connection pool ──────────────────────────────
    connection_pool_size: int = Field(default=100, ge=1, description="Connection pool size")
    connection_keepalive: float = Field(default=30.0, gt=0, description="Keep-alive timeout")

    # ────────────────────────── WebSocket settings ───────────────────────────
    websocket_ping_interval: float = Field(default=20.0, gt=0, description="WS ping interval")
    websocket_pong_timeout: float = Field(default=10.0, gt=0, description="WS pong timeout")
    websocket_close_timeout: float = Field(default=5.0, gt=0, description="WS close timeout")
    websocket_max_size: int = Field(default=2**20, gt=0, description="Max WS message size")

    # ────────────────────────── HTTP settings ────────────────────────────────
    user_agent: str = Field(
        default="asterisk-ari-python/0.1.0",
        description="User-Agent header"
    )
    max_redirects: int = Field(default=10, ge=0, description="Max HTTP redirects")

    # ────────────────────────── Logging ──────────────────────────────────────
    debug: bool = Field(default=False, description="Enable debug logging")
    log_level: str = Field(default="INFO", description="Logging level")

    # ────────────────────────── Advanced settings ────────────────────────────
    circuit_breaker_failure_threshold: int = Field(
        default=5, ge=1, description="Circuit breaker failure threshold"
    )
    circuit_breaker_recovery_timeout: float = Field(
        default=60.0, gt=0, description="Circuit breaker recovery timeout"
    )
    circuit_breaker_expected_exception: Optional[str] = Field(
        default=None, description="Expected exception for circuit breaker"
    )

    # ────────────────────────── Validators ───────────────────────────────────
    @field_validator("port")
    def validate_port(cls, v: Union[int, str]) -> int:
        return int(v)

    @field_validator("host", "username", "app_name")
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field cannot be empty")
        return v.strip()

    @field_validator("password")
    def validate_password(cls, v: str) -> str:
        if not v:
            raise ValueError("Password cannot be empty")
        return v

    @field_validator("log_level")
    def validate_log_level(cls, v: str) -> str:
        levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        u = v.upper()
        if u not in levels:
            raise ValueError(f"Log level must be one of: {', '.join(levels)}")
        return u

    @model_validator(mode="before")
    @classmethod
    def check_ssl_settings(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if values.get("use_ssl") and values.get("verify_ssl") and values.get("ssl_ca_bundle"):
            if not os.path.exists(values["ssl_ca_bundle"]):
                raise ValueError(f"SSL CA bundle not found: {values['ssl_ca_bundle']}")
        return values

    @model_validator(mode="before")
    @classmethod
    def check_retry_settings(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if values.get("retry_backoff", 1.0) > values.get("max_retry_delay", 60.0):
            raise ValueError("Retry backoff cannot exceed max retry delay")
        return values

    @model_validator(mode="before")
    @classmethod
    def check_websocket_settings(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if values.get("websocket_pong_timeout", 10.0) >= values.get("websocket_ping_interval", 20.0):
            raise ValueError("WS pong timeout must be less than ping interval")
        return values

    # ────────────────────────── Helper Properties ────────────────────────────
    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.host}:{self.port}"

    @property
    def ari_url(self) -> str:
        return f"{self.base_url}/ari"

    @property
    def websocket_url(self) -> str:
        scheme = "wss" if self.use_ssl else "ws"
        return f"{scheme}://{self.host}:{self.port}/ari/events"

    @property
    def auth_tuple(self) -> tuple[str, str]:
        return self.username, self.password

    # # ────────────────────────── Convenience Constructors ─────────────────────
    @classmethod
    def from_env(cls) -> "ARIConfig":
        """
        Load config from environment, using .env only if present.
        """
        env_kwargs = {}
        if os.path.exists(".env"):
            env_kwargs["env_file"] = ".env"
            env_kwargs["env_file_encoding"] = "utf-8"
        return cls.model_validate({}, **env_kwargs)

    @classmethod
    def from_dict(cls, config: Dict[str, Any]) -> "ARIConfig":
        return cls(**config)

    @classmethod
    def from_file(cls, path: str) -> "ARIConfig":
        import json, tomli
        if not os.path.exists(path):
            raise FileNotFoundError(f"Configuration file not found: {path}")
        with open(path, "rb") as f:
            if path.endswith(".json"):
                data = json.load(f)
            elif path.endswith((".toml", ".tml")):
                data = tomli.load(f)
            else:
                raise ValueError(f"Unsupported config file format: {path}")
        return cls(**data)

    # # ────────────────────────── Utility ──────────────────────────────────────
    def mask_sensitive_data(self) -> Dict[str, Any]:
        d = self.model_dump()
        if "password" in d:
            d["password"] = "*" * len(d["password"])
        return d

    def __repr__(self) -> str:
        return f"ARIConfig({self.mask_sensitive_data()})"

    def get_connector_kwargs(self) -> Dict[str, Any]:
        kwargs = {
            "limit": self.connection_pool_size,
            "keepalive_timeout": self.connection_keepalive,
            "enable_cleanup_closed": True,
        }
        if self.use_ssl:
            kwargs["verify_ssl"] = self.verify_ssl
            if self.ssl_ca_bundle:
                kwargs["ca_file"] = self.ssl_ca_bundle
        return kwargs

    def get_timeout_config(self) -> Dict[str, float]:
        return {
            "total": self.timeout,
            "connect": min(self.timeout / 3, 10.0),
            "sock_read": self.timeout,
            "sock_connect": min(self.timeout / 3, 10.0),
        }

