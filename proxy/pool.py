import json
import uuid
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from agent.tools import ToolRegistry
from proxy.models import ProxyEntry, ProxyProtocol


DATA_FILE = Path(__file__).parent.parent / "data" / "proxies.json"


class ProxyPool:
    """Manages a persistent pool of proxies stored as JSON."""

    def __init__(self, proxies: list[ProxyEntry] | None = None):
        self.proxies: list[ProxyEntry] = proxies or []

    @classmethod
    def load(cls) -> "ProxyPool":
        if DATA_FILE.exists():
            try:
                data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
                proxies = [ProxyEntry(**p) for p in data]
                return cls(proxies)
            except (json.JSONDecodeError, Exception):
                return cls()
        return cls()

    def save(self) -> None:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = [p.model_dump(mode="json") for p in self.proxies]
        DATA_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def add(self, proxy: ProxyEntry) -> None:
        self.proxies.append(proxy)

    def remove_by_id(self, proxy_id: str) -> bool:
        before = len(self.proxies)
        self.proxies = [p for p in self.proxies if p.id != proxy_id]
        return len(self.proxies) < before

    def remove_by_address(self, host: str, port: int) -> bool:
        before = len(self.proxies)
        self.proxies = [p for p in self.proxies if not (p.host == host and p.port == port)]
        return len(self.proxies) < before

    def get_by_id(self, proxy_id: str) -> ProxyEntry | None:
        for p in self.proxies:
            if p.id == proxy_id:
                return p
        return None

    def get_by_address(self, host: str, port: int) -> ProxyEntry | None:
        for p in self.proxies:
            if p.host == host and p.port == port:
                return p
        return None

    def merge_entry(self, host: str, port: int, updates: dict) -> ProxyEntry | None:
        """Find an existing proxy by address and merge metadata updates into it."""
        existing = self.get_by_address(host, port)
        if not existing:
            return None
        if "country" in updates and updates["country"] and not existing.country:
            existing.country = updates["country"]
        if "last_seen" in updates and updates["last_seen"]:
            existing.last_seen = updates["last_seen"]
        if "source" in updates and updates["source"]:
            if updates["source"] not in existing.sources:
                existing.sources.append(updates["source"])
        return existing

    def find(
        self,
        protocol: str | None = None,
        country: str | None = None,
        alive_only: bool = False,
        tags: list[str] | None = None,
    ) -> list[ProxyEntry]:
        result = self.proxies
        if protocol:
            result = [p for p in result if p.protocol.value == protocol]
        if country:
            result = [p for p in result if p.country and p.country.lower() == country.lower()]
        if alive_only:
            result = [p for p in result if p.is_alive is True]
        if tags:
            result = [p for p in result if any(t in p.tags for t in tags)]
        return result


# --- Tool parameter models ---

class AddProxyParams(BaseModel):
    host: str = Field(description="Proxy IP address or hostname")
    port: int = Field(description="Proxy port number")
    protocol: str = Field(default="http", description="Protocol: http, https, socks4, or socks5")
    username: Optional[str] = Field(default=None, description="Optional auth username")
    password: Optional[str] = Field(default=None, description="Optional auth password")
    tags: list[str] = Field(default_factory=list, description="Optional tags like 'us', 'fast', 'private'")


class RemoveProxyParams(BaseModel):
    proxy_id: Optional[str] = Field(default=None, description="Proxy ID to remove")
    host: Optional[str] = Field(default=None, description="Proxy host (use with port)")
    port: Optional[int] = Field(default=None, description="Proxy port (use with host)")


class ListProxiesParams(BaseModel):
    protocol: Optional[str] = Field(default=None, description="Filter by protocol: http, socks4, socks5")
    country: Optional[str] = Field(default=None, description="Filter by country code")
    alive_only: bool = Field(default=False, description="Only show alive proxies")
    tags: list[str] = Field(default_factory=list, description="Filter by tags")


class ClearDeadParams(BaseModel):
    failure_threshold: int = Field(default=3, description="Remove proxies with this many consecutive failures or more")


class ImportProxiesParams(BaseModel):
    text: str = Field(description="Proxy list text, one per line in ip:port format")
    protocol: str = Field(default="http", description="Protocol to assign: http, socks4, socks5")


class CleanStaleParams(BaseModel):
    stale_hours: int = Field(default=72, description="Remove proxies not seen in discovery for this many hours. 0 = only remove those never seen.")


# --- Tool registration ---

def register_pool_tools(registry: ToolRegistry):

    @registry.register(
        name="add_proxy",
        description="Add a single proxy to the local pool. Requires host and port. Protocol defaults to HTTP.",
        parameters_model=AddProxyParams,
    )
    def add_proxy(
        host: str,
        port: int,
        protocol: str = "http",
        username: str | None = None,
        password: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        pool = ProxyPool.load()
        existing = pool.get_by_address(host, port)
        if existing:
            return {"status": "duplicate", "message": f"Proxy {host}:{port} already exists with ID {existing.id}"}

        proxy = ProxyEntry(
            id=uuid.uuid4().hex[:8],
            host=host,
            port=port,
            protocol=ProxyProtocol(protocol),
            username=username,
            password=password,
            tags=tags or [],
            source="manual",
        )
        pool.add(proxy)
        pool.save()
        return {"status": "added", "proxy": proxy.to_display_dict()}

    @registry.register(
        name="remove_proxy",
        description="Remove a proxy from the pool by its ID or by host:port address.",
        parameters_model=RemoveProxyParams,
    )
    def remove_proxy(
        proxy_id: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> dict:
        pool = ProxyPool.load()
        removed = False
        if proxy_id:
            removed = pool.remove_by_id(proxy_id)
        elif host and port:
            removed = pool.remove_by_address(host, port)
        else:
            return {"status": "error", "message": "Provide either proxy_id or host+port"}
        if removed:
            pool.save()
            return {"status": "removed"}
        return {"status": "not_found", "message": "No matching proxy found"}

    @registry.register(
        name="list_proxies",
        description="List all proxies in the pool, optionally filtered by protocol, country, alive status, or tags.",
        parameters_model=ListProxiesParams,
    )
    def list_proxies(
        protocol: str | None = None,
        country: str | None = None,
        alive_only: bool = False,
        tags: list[str] | None = None,
    ) -> dict:
        pool = ProxyPool.load()
        results = pool.find(protocol=protocol, country=country, alive_only=alive_only, tags=tags)
        return {
            "total": len(results),
            "proxies": [p.to_display_dict() for p in results],
        }

    @registry.register(
        name="clear_dead_proxies",
        description="Remove all proxies that have failed health checks beyond the failure threshold.",
        parameters_model=ClearDeadParams,
    )
    def clear_dead_proxies(failure_threshold: int = 3) -> dict:
        pool = ProxyPool.load()
        before = len(pool.proxies)
        pool.proxies = [
            p for p in pool.proxies
            if p.consecutive_failures < failure_threshold
        ]
        removed = before - len(pool.proxies)
        pool.save()
        return {"removed": removed, "remaining": len(pool.proxies)}

    @registry.register(
        name="import_proxies",
        description="Bulk import proxies from text. Expects one proxy per line in ip:port format.",
        parameters_model=ImportProxiesParams,
    )
    def import_proxies(text: str, protocol: str = "http") -> dict:
        pool = ProxyPool.load()
        added = 0
        skipped = 0
        errors = 0

        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                line_protocol = protocol
                # Handle protocol://host:port format
                if "://" in line:
                    proto_part, addr = line.split("://", 1)
                    line_protocol = proto_part
                    line = addr
                # Handle user:pass@host:port
                if "@" in line:
                    auth, line = line.rsplit("@", 1)
                parts = line.rsplit(":", 1)
                if len(parts) != 2:
                    errors += 1
                    continue
                host = parts[0]
                port = int(parts[1])

                if pool.get_by_address(host, port):
                    skipped += 1
                    continue

                proxy = ProxyEntry(
                    id=uuid.uuid4().hex[:8],
                    host=host,
                    port=port,
                    protocol=ProxyProtocol(line_protocol),
                    source="import",
                )
                pool.add(proxy)
                added += 1
            except (ValueError, KeyError):
                errors += 1

        pool.save()
        return {"added": added, "skipped_duplicates": skipped, "errors": errors}

    @registry.register(
        name="clean_stale_proxies",
        description=(
            "Remove proxies that haven't been seen during discovery for a specified number of hours. "
            "Helps keep the pool fresh by pruning entries no longer reported by any source."
        ),
        parameters_model=CleanStaleParams,
    )
    def clean_stale_proxies(stale_hours: int = 72) -> dict:
        from datetime import datetime, timezone, timedelta

        pool = ProxyPool.load()
        before = len(pool.proxies)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=stale_hours)).isoformat()

        kept = []
        for p in pool.proxies:
            if p.last_seen is None:
                # Never seen in discovery — if stale_hours is 0, remove these
                if stale_hours == 0:
                    continue
                kept.append(p)
            elif p.last_seen < cutoff:
                continue
            else:
                kept.append(p)

        pool.proxies = kept
        removed = before - len(pool.proxies)
        pool.save()

        return {
            "removed": removed,
            "remaining": len(pool.proxies),
            "stale_hours": stale_hours,
        }
