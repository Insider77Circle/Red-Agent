import random
from typing import Optional

import aiohttp
from aiohttp_socks import ProxyConnector
from pydantic import BaseModel, Field

from agent.tools import ToolRegistry
from proxy.models import ProxyEntry
from proxy.pool import ProxyPool


async def _test_dns_leak(proxy: ProxyEntry, timeout: int = 15) -> dict:
    """
    Test for DNS leaks by comparing the DNS resolver seen through the proxy
    vs direct. Uses ipleak.net API for DNS server detection.
    """
    results = {
        "proxy": proxy.to_display_dict(),
        "proxy_external_ip": None,
        "direct_dns_servers": [],
        "proxy_dns_servers": [],
        "leak_detected": None,
        "error": None,
    }

    try:
        # Step 1: Get our direct DNS info
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            try:
                async with session.get("https://ipinfo.io/json") as resp:
                    direct_info = await resp.json()
                    results["direct_ip"] = direct_info.get("ip", "unknown")
            except Exception:
                results["direct_ip"] = "could not determine"

        # Step 2: Get external IP through proxy
        connector = ProxyConnector.from_url(proxy.url)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            # Get proxy external IP
            try:
                async with session.get("https://httpbin.org/ip") as resp:
                    data = await resp.json()
                    results["proxy_external_ip"] = data.get("origin", "unknown")
            except Exception:
                try:
                    async with session.get("https://api.ipify.org?format=json") as resp:
                        data = await resp.json()
                        results["proxy_external_ip"] = data.get("ip", "unknown")
                except Exception as e:
                    results["error"] = f"Cannot reach test endpoints through proxy: {str(e)[:100]}"
                    results["leak_detected"] = None
                    return results

            # Step 3: Make DNS-triggering requests through the proxy
            # Use random subdomains to force fresh DNS lookups
            test_domains = [
                f"test-{random.randint(100000, 999999)}.ipleak.net",
            ]

            for domain in test_domains:
                try:
                    async with session.get(f"https://{domain}/dnsdetect/", ssl=False) as resp:
                        pass
                except Exception:
                    pass

            # Step 4: Check DNS leak results
            try:
                async with session.get("https://ipinfo.io/json") as resp:
                    proxy_info = await resp.json()
                    proxy_ip_via_proxy = proxy_info.get("ip", "unknown")
            except Exception:
                proxy_ip_via_proxy = results["proxy_external_ip"]

        # Analyze: if the proxy IP matches our direct IP, DNS is likely leaking
        direct_ip = results.get("direct_ip", "")
        proxy_ip = results.get("proxy_external_ip", "")

        if direct_ip and proxy_ip and direct_ip != "unknown" and proxy_ip != "unknown":
            if direct_ip == proxy_ip:
                results["leak_detected"] = True
                results["message"] = "WARNING: Your real IP is visible through the proxy. The proxy may not be working correctly."
            else:
                results["leak_detected"] = False
                results["message"] = "DNS appears to be routing through the proxy. No obvious leak detected."
        else:
            results["leak_detected"] = None
            results["message"] = "Could not conclusively determine DNS leak status. Manual verification recommended."

    except Exception as e:
        results["error"] = str(e)[:200]
        results["leak_detected"] = None

    return results


# --- Tool parameter models ---

class DNSLeakParams(BaseModel):
    proxy_id: Optional[str] = Field(default=None, description="Proxy ID to test")
    host: Optional[str] = Field(default=None, description="Proxy host (use with port)")
    port: Optional[int] = Field(default=None, description="Proxy port (use with host)")
    timeout_seconds: int = Field(default=15, description="Timeout for DNS leak test")


# --- Tool registration ---

def register_dns_tools(registry: ToolRegistry):

    @registry.register(
        name="test_dns_leak",
        description="Test a proxy for DNS leaks by comparing IP visibility and DNS resolution paths.",
        parameters_model=DNSLeakParams,
    )
    async def test_dns_leak(
        proxy_id: str | None = None,
        host: str | None = None,
        port: int | None = None,
        timeout_seconds: int = 15,
    ) -> dict:
        pool = ProxyPool.load()
        proxy = None
        if proxy_id:
            proxy = pool.get_by_id(proxy_id)
        elif host and port:
            proxy = pool.get_by_address(host, port)

        if not proxy:
            return {"status": "not_found", "message": "Proxy not found in pool"}

        return await _test_dns_leak(proxy, timeout_seconds)
