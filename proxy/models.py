from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class ProxyProtocol(str, Enum):
    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"


class AnonymityLevel(str, Enum):
    TRANSPARENT = "transparent"
    ANONYMOUS = "anonymous"
    ELITE = "elite"
    UNKNOWN = "unknown"


class ProxyEntry(BaseModel):
    id: str = Field(description="Unique identifier")
    host: str = Field(description="Proxy IP or hostname")
    port: int = Field(description="Proxy port number")
    protocol: ProxyProtocol = Field(default=ProxyProtocol.HTTP)
    username: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None)
    country: Optional[str] = Field(default=None)
    tags: list[str] = Field(default_factory=list)

    # Health metrics
    is_alive: Optional[bool] = Field(default=None)
    latency_ms: Optional[float] = Field(default=None)
    anonymity: AnonymityLevel = Field(default=AnonymityLevel.UNKNOWN)
    last_checked: Optional[str] = Field(default=None)
    consecutive_failures: int = Field(default=0)

    # Metadata
    added_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = Field(default="manual")
    sources: list[str] = Field(default_factory=list)
    last_seen: Optional[str] = Field(default=None)

    @model_validator(mode="after")
    def _backfill_sources(self) -> "ProxyEntry":
        if not self.sources and self.source:
            self.sources = [self.source]
        return self

    @property
    def url(self) -> str:
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"{self.protocol.value}://{auth}{self.host}:{self.port}"

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def to_display_dict(self) -> dict:
        return {
            "id": self.id,
            "address": self.address,
            "protocol": self.protocol.value,
            "country": self.country,
            "alive": self.is_alive,
            "latency_ms": self.latency_ms,
            "anonymity": self.anonymity.value,
            "tags": self.tags,
            "source": self.source,
            "sources": self.sources,
            "last_seen": self.last_seen,
        }


class HealthCheckResult(BaseModel):
    proxy_id: str
    reachable: bool
    latency_ms: Optional[float] = None
    external_ip: Optional[str] = None
    anonymity: AnonymityLevel = AnonymityLevel.UNKNOWN
    error: Optional[str] = None
    checked_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
