import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from aiohttp_socks import ProxyConnector
from pydantic import BaseModel, Field

from agent.tools import ToolRegistry
from proxy.models import ProxyEntry, HealthCheckResult, AnonymityLevel
from proxy.pool import ProxyPool


# Test endpoints
HTTPBIN_IP = "https://httpbin.org/ip"
HTTPBIN_HEADERS = "https://httpbin.org/headers"
FALLBACK_IP = "https://api.ipify.org?format=json"


async def _check_single_proxy(proxy: ProxyEntry, timeout: int = 10) -> HealthCheckResult:
    """Test a single proxy for connectivity, latency, and anonymity."""
    try:
        connector = ProxyConnector.from_url(proxy.url)
        client_timeout = aiohttp.ClientTimeout(total=timeout)

        start = time.monotonic()
        async with aiohttp.ClientSession(connector=connector, timeout=client_timeout) as session:
            # Test connectivity + latency
            try:
                async with session.get(HTTPBIN_IP) as resp:
                    data = await resp.json()
                    latency = (time.monotonic() - start) * 1000
                    external_ip = data.get("origin", "")
            except Exception:
                # Fallback endpoint
                async with session.get(FALLBACK_IP) as resp:
                    data = await resp.json()
                    latency = (time.monotonic() - start) * 1000
                    external_ip = data.get("ip", "")

            # Test anonymity via headers
            anonymity = AnonymityLevel.UNKNOWN
            try:
                async with session.get(HTTPBIN_HEADERS) as resp:
                    headers_data = await resp.json()
                    anonymity = _detect_anonymity(headers_data.get("headers", {}))
            except Exception:
                pass

        return HealthCheckResult(
            proxy_id=proxy.id,
            reachable=True,
            latency_ms=round(latency, 2),
            external_ip=external_ip,
            anonymity=anonymity,
        )
    except Exception as e:
        return HealthCheckResult(
            proxy_id=proxy.id,
            reachable=False,
            error=str(e)[:200],
        )


def _detect_anonymity(headers: dict) -> AnonymityLevel:
    """Detect anonymity level from response headers."""
    header_names = {k.lower() for k in headers}

    proxy_indicators = {
        "x-forwarded-for", "x-real-ip", "via",
        "x-proxy-id", "forwarded", "x-forwarded-host",
    }

    found = header_names & proxy_indicators

    if "x-forwarded-for" in found or "x-real-ip" in found:
        return AnonymityLevel.TRANSPARENT
    if "via" in found or "forwarded" in found:
        return AnonymityLevel.ANONYMOUS
    if not found:
        return AnonymityLevel.ELITE
    return AnonymityLevel.ANONYMOUS


def _update_proxy_with_result(proxy: ProxyEntry, result: HealthCheckResult) -> None:
    """Update a proxy entry with health check results."""
    proxy.is_alive = result.reachable
    proxy.latency_ms = result.latency_ms
    proxy.anonymity = result.anonymity
    proxy.last_checked = result.checked_at
    if result.reachable:
        proxy.consecutive_failures = 0
    else:
        proxy.consecutive_failures += 1


# --- Tool parameter models ---

class CheckProxyParams(BaseModel):
    proxy_id: Optional[str] = Field(default=None, description="Proxy ID to check")
    host: Optional[str] = Field(default=None, description="Proxy host (use with port)")
    port: Optional[int] = Field(default=None, description="Proxy port (use with host)")
    timeout_seconds: int = Field(default=10, description="Connection timeout in seconds")


class CheckAllParams(BaseModel):
    timeout_seconds: int = Field(default=10, description="Timeout per proxy in seconds")
    max_concurrent: int = Field(default=20, description="Maximum concurrent checks")


class CheckAnonymityParams(BaseModel):
    proxy_id: Optional[str] = Field(default=None, description="Proxy ID to check")
    host: Optional[str] = Field(default=None, description="Proxy host (use with port)")
    port: Optional[int] = Field(default=None, description="Proxy port (use with host)")


# --- Tool registration ---

def register_checker_tools(registry: ToolRegistry):

    @registry.register(
        name="check_proxy",
        description="Health-check a single proxy for connectivity, latency, and anonymity level.",
        parameters_model=CheckProxyParams,
    )
    async def check_proxy(
        proxy_id: str | None = None,
        host: str | None = None,
        port: int | None = None,
        timeout_seconds: int = 10,
    ) -> dict:
        pool = ProxyPool.load()
        proxy = None
        if proxy_id:
            proxy = pool.get_by_id(proxy_id)
        elif host and port:
            proxy = pool.get_by_address(host, port)

        if not proxy:
            return {"status": "not_found", "message": "Proxy not found in pool"}

        result = await _check_single_proxy(proxy, timeout_seconds)
        _update_proxy_with_result(proxy, result)
        pool.save()

        return {
            "status": "checked",
            "proxy": proxy.to_display_dict(),
            "reachable": result.reachable,
            "latency_ms": result.latency_ms,
            "external_ip": result.external_ip,
            "anonymity": result.anonymity.value,
            "error": result.error,
        }

    @registry.register(
        name="check_all_proxies",
        description="Run concurrent health checks on all proxies in the pool. Returns summary statistics.",
        parameters_model=CheckAllParams,
    )
    async def check_all_proxies(
        timeout_seconds: int = 10,
        max_concurrent: int = 20,
    ) -> dict:
        pool = ProxyPool.load()
        if not pool.proxies:
            return {"status": "empty", "message": "No proxies in pool to check"}

        semaphore = asyncio.Semaphore(max_concurrent)

        async def bounded_check(proxy: ProxyEntry) -> HealthCheckResult:
            async with semaphore:
                return await _check_single_proxy(proxy, timeout_seconds)

        results = await asyncio.gather(*[bounded_check(p) for p in pool.proxies])

        # Update pool with results
        for result in results:
            proxy = pool.get_by_id(result.proxy_id)
            if proxy:
                _update_proxy_with_result(proxy, result)

        pool.save()

        alive = sum(1 for r in results if r.reachable)
        dead = len(results) - alive
        latencies = [r.latency_ms for r in results if r.latency_ms is not None]

        return {
            "total": len(results),
            "alive": alive,
            "dead": dead,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "min_latency_ms": min(latencies) if latencies else None,
            "max_latency_ms": max(latencies) if latencies else None,
            "results": [
                {
                    "id": r.proxy_id,
                    "reachable": r.reachable,
                    "latency_ms": r.latency_ms,
                    "anonymity": r.anonymity.value,
                    "error": r.error,
                }
                for r in results
            ],
        }

    @registry.register(
        name="check_proxy_anonymity",
        description="Specifically check a proxy's anonymity level (transparent, anonymous, or elite).",
        parameters_model=CheckAnonymityParams,
    )
    async def check_proxy_anonymity(
        proxy_id: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> dict:
        pool = ProxyPool.load()
        proxy = None
        if proxy_id:
            proxy = pool.get_by_id(proxy_id)
        elif host and port:
            proxy = pool.get_by_address(host, port)

        if not proxy:
            return {"status": "not_found", "message": "Proxy not found in pool"}

        result = await _check_single_proxy(proxy, timeout=10)
        _update_proxy_with_result(proxy, result)
        pool.save()

        return {
            "proxy": proxy.to_display_dict(),
            "anonymity": result.anonymity.value,
            "external_ip": result.external_ip,
            "explanation": _anonymity_explanation(result.anonymity),
        }


def _anonymity_explanation(level: AnonymityLevel) -> str:
    explanations = {
        AnonymityLevel.TRANSPARENT: "The proxy reveals your real IP address via forwarding headers. Not recommended for privacy.",
        AnonymityLevel.ANONYMOUS: "The proxy hides your IP but identifies itself as a proxy via headers like 'Via'.",
        AnonymityLevel.ELITE: "The proxy hides your IP and does not reveal itself as a proxy. Best for privacy.",
        AnonymityLevel.UNKNOWN: "Could not determine anonymity level. The proxy may have failed the header test.",
    }
    return explanations.get(level, "Unknown anonymity level.")
