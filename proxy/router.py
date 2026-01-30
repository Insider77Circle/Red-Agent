import asyncio
import json
import random
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp_socks import ProxyConnector
from pydantic import BaseModel, Field

from agent.tools import ToolRegistry
from proxy.models import ProxyEntry, AnonymityLevel
from proxy.pool import ProxyPool


ROTATION_FILE = Path(__file__).parent.parent / "data" / "rotation.json"


class RotationConfig:
    """Manages proxy rotation state."""

    def __init__(self, strategy: str = "round-robin", proxy_ids: list[str] | None = None):
        self.strategy = strategy
        self.proxy_ids = proxy_ids or []
        self.current_index = 0

    def save(self):
        ROTATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "strategy": self.strategy,
            "proxy_ids": self.proxy_ids,
            "current_index": self.current_index,
        }
        ROTATION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "RotationConfig":
        if ROTATION_FILE.exists():
            try:
                data = json.loads(ROTATION_FILE.read_text(encoding="utf-8"))
                config = cls(data.get("strategy", "round-robin"), data.get("proxy_ids", []))
                config.current_index = data.get("current_index", 0)
                return config
            except Exception:
                return cls()
        return cls()

    def get_next(self, pool: ProxyPool) -> ProxyEntry | None:
        if not self.proxy_ids:
            return None

        if self.strategy == "random":
            pid = random.choice(self.proxy_ids)
            return pool.get_by_id(pid)

        elif self.strategy == "least-latency":
            proxies = [pool.get_by_id(pid) for pid in self.proxy_ids]
            alive = [p for p in proxies if p and p.is_alive]
            if not alive:
                return None
            alive.sort(key=lambda p: p.latency_ms or float("inf"))
            return alive[0]

        else:  # round-robin
            if self.current_index >= len(self.proxy_ids):
                self.current_index = 0
            pid = self.proxy_ids[self.current_index]
            self.current_index += 1
            self.save()
            return pool.get_by_id(pid)


# --- Tool parameter models ---

class SelectProxyParams(BaseModel):
    country: Optional[str] = Field(default=None, description="Country code (e.g. 'US', 'DE')")
    max_latency_ms: Optional[float] = Field(default=None, description="Maximum acceptable latency in ms")
    protocol: Optional[str] = Field(default=None, description="Protocol: http, socks4, socks5")
    anonymity_level: Optional[str] = Field(default=None, description="Minimum anonymity: anonymous, elite")


class BuildChainParams(BaseModel):
    chain_length: int = Field(default=2, description="Number of hops in the chain")
    countries: list[str] = Field(default_factory=list, description="Preferred countries for hops (in order)")
    protocols: list[str] = Field(default_factory=list, description="Preferred protocols for hops (in order)")


class ConfigureRotationParams(BaseModel):
    strategy: str = Field(default="round-robin", description="Rotation strategy: round-robin, random, least-latency")
    protocol: Optional[str] = Field(default=None, description="Only rotate proxies with this protocol")
    country: Optional[str] = Field(default=None, description="Only rotate proxies from this country")
    alive_only: bool = Field(default=True, description="Only include alive proxies in rotation")


class EmptyParams(BaseModel):
    pass


class TorCheckParams(BaseModel):
    tor_host: str = Field(default="127.0.0.1", description="Tor SOCKS5 host")
    tor_port: int = Field(default=9050, description="Tor SOCKS5 port")


# --- Tool registration ---

def register_router_tools(registry: ToolRegistry):

    @registry.register(
        name="select_proxy",
        description="Find the best proxy matching criteria (country, latency, protocol, anonymity). Returns the fastest alive match.",
        parameters_model=SelectProxyParams,
    )
    def select_proxy(
        country: str | None = None,
        max_latency_ms: float | None = None,
        protocol: str | None = None,
        anonymity_level: str | None = None,
    ) -> dict:
        pool = ProxyPool.load()
        candidates = [p for p in pool.proxies if p.is_alive]

        if not candidates:
            candidates = pool.proxies  # fall back to all if none checked yet

        if country:
            candidates = [p for p in candidates if p.country and p.country.lower() == country.lower()]
        if protocol:
            candidates = [p for p in candidates if p.protocol.value == protocol]
        if anonymity_level:
            level_order = {"transparent": 0, "anonymous": 1, "elite": 2, "unknown": -1}
            min_level = level_order.get(anonymity_level, 0)
            candidates = [
                p for p in candidates
                if level_order.get(p.anonymity.value, -1) >= min_level
            ]
        if max_latency_ms:
            candidates = [p for p in candidates if p.latency_ms and p.latency_ms <= max_latency_ms]

        if not candidates:
            return {"status": "no_match", "message": "No proxies match the specified criteria."}

        # Sort by latency (fastest first)
        candidates.sort(key=lambda p: p.latency_ms or float("inf"))
        best = candidates[0]

        return {
            "status": "selected",
            "proxy": best.to_display_dict(),
            "proxy_url": best.url,
            "alternatives": len(candidates) - 1,
        }

    @registry.register(
        name="build_proxy_chain",
        description="Build a multi-hop proxy chain from the pool. Prefers geographic diversity and alive proxies.",
        parameters_model=BuildChainParams,
    )
    def build_proxy_chain(
        chain_length: int = 2,
        countries: list[str] | None = None,
        protocols: list[str] | None = None,
    ) -> dict:
        pool = ProxyPool.load()
        alive = [p for p in pool.proxies if p.is_alive]
        if not alive:
            alive = pool.proxies

        if len(alive) < chain_length:
            return {
                "status": "insufficient",
                "available": len(alive),
                "requested": chain_length,
                "message": f"Need {chain_length} proxies but only {len(alive)} available.",
            }

        selected: list[ProxyEntry] = []
        remaining = list(alive)

        for i in range(chain_length):
            candidates = remaining

            # Apply country preference for this hop
            if countries and i < len(countries):
                country_filtered = [p for p in candidates if p.country and p.country.lower() == countries[i].lower()]
                if country_filtered:
                    candidates = country_filtered

            # Apply protocol preference for this hop
            if protocols and i < len(protocols):
                proto_filtered = [p for p in candidates if p.protocol.value == protocols[i]]
                if proto_filtered:
                    candidates = proto_filtered

            if not candidates:
                candidates = remaining

            # Pick best by latency
            candidates.sort(key=lambda p: p.latency_ms or float("inf"))
            pick = candidates[0]
            selected.append(pick)
            remaining = [p for p in remaining if p.id != pick.id]

        return {
            "status": "chain_built",
            "hops": len(selected),
            "chain": [p.to_display_dict() for p in selected],
            "chain_urls": [p.url for p in selected],
        }

    @registry.register(
        name="configure_rotation",
        description="Configure proxy rotation strategy. Selects proxies from the pool based on filters and sets up rotation.",
        parameters_model=ConfigureRotationParams,
    )
    def configure_rotation(
        strategy: str = "round-robin",
        protocol: str | None = None,
        country: str | None = None,
        alive_only: bool = True,
    ) -> dict:
        pool = ProxyPool.load()
        candidates = pool.find(protocol=protocol, country=country, alive_only=alive_only)

        if not candidates:
            return {"status": "no_proxies", "message": "No proxies match the rotation criteria."}

        config = RotationConfig(
            strategy=strategy,
            proxy_ids=[p.id for p in candidates],
        )
        config.save()

        return {
            "status": "configured",
            "strategy": strategy,
            "proxy_count": len(candidates),
            "proxies": [p.to_display_dict() for p in candidates[:10]],
        }

    @registry.register(
        name="get_rotation_next",
        description="Get the next proxy from the configured rotation. Must configure_rotation first.",
        parameters_model=EmptyParams,
    )
    def get_rotation_next() -> dict:
        config = RotationConfig.load()
        if not config.proxy_ids:
            return {"status": "not_configured", "message": "No rotation configured. Use configure_rotation first."}

        pool = ProxyPool.load()
        proxy = config.get_next(pool)

        if not proxy:
            return {"status": "exhausted", "message": "No valid proxy available in rotation pool."}

        return {
            "status": "ok",
            "strategy": config.strategy,
            "proxy": proxy.to_display_dict(),
            "proxy_url": proxy.url,
        }

    @registry.register(
        name="check_tor_availability",
        description="Check if Tor SOCKS5 proxy is available on localhost.",
        parameters_model=TorCheckParams,
    )
    async def check_tor_availability(
        tor_host: str = "127.0.0.1",
        tor_port: int = 9050,
    ) -> dict:
        tor_url = f"socks5://{tor_host}:{tor_port}"
        try:
            connector = ProxyConnector.from_url(tor_url)
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get("https://check.torproject.org/api/ip") as resp:
                    data = await resp.json()
                    is_tor = data.get("IsTor", False)
                    ip = data.get("IP", "unknown")

            return {
                "status": "available",
                "is_tor": is_tor,
                "exit_ip": ip,
                "tor_address": tor_url,
                "message": "Tor is running and confirmed." if is_tor else "SOCKS5 is reachable but Tor not confirmed.",
            }
        except Exception as e:
            return {
                "status": "unavailable",
                "error": str(e)[:200],
                "message": f"Tor SOCKS5 not reachable at {tor_url}. Ensure Tor service is running.",
            }
