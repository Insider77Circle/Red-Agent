import hashlib
import ssl
import socket
from typing import Optional

import aiohttp
from aiohttp_socks import ProxyConnector
from pydantic import BaseModel, Field

from agent.tools import ToolRegistry
from proxy.models import ProxyEntry
from proxy.pool import ProxyPool


def _get_cert_fingerprint_direct(host: str, port: int = 443) -> dict:
    """Get TLS certificate fingerprint via direct connection."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert_bin = ssock.getpeercert(binary_form=True)
                fingerprint = hashlib.sha256(cert_bin).hexdigest()
                cert_info = ssock.getpeercert()
                return {
                    "fingerprint": fingerprint,
                    "subject": str(cert_info.get("subject", "")),
                    "issuer": str(cert_info.get("issuer", "")),
                    "serial": cert_info.get("serialNumber", ""),
                    "error": None,
                }
    except Exception as e:
        return {"fingerprint": None, "error": str(e)[:200]}


async def _get_cert_fingerprint_via_proxy(proxy: ProxyEntry, host: str, port: int = 443) -> dict:
    """Get TLS certificate fingerprint through a proxy connection."""
    try:
        connector = ProxyConnector.from_url(proxy.url)
        # We need to inspect the cert, so we use a custom SSL context
        ssl_ctx = ssl.create_default_context()

        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=15)) as session:
            url = f"https://{host}:{port}/"
            async with session.get(url, ssl=ssl_ctx) as resp:
                # Get peer cert from the connection
                transport = resp.connection.transport
                ssl_object = transport.get_extra_info("ssl_object")
                if ssl_object:
                    cert_bin = ssl_object.getpeercert(binary_form=True)
                    fingerprint = hashlib.sha256(cert_bin).hexdigest()
                    cert_info = ssl_object.getpeercert()
                    return {
                        "fingerprint": fingerprint,
                        "subject": str(cert_info.get("subject", "")),
                        "issuer": str(cert_info.get("issuer", "")),
                        "serial": cert_info.get("serialNumber", ""),
                        "error": None,
                    }
                else:
                    return {"fingerprint": None, "error": "Could not extract SSL object from proxy connection."}
    except Exception as e:
        return {"fingerprint": None, "error": str(e)[:200]}


async def _check_content_injection(proxy: ProxyEntry, timeout: int = 15) -> dict:
    """Detect if a proxy modifies HTTP response content."""
    test_url = "https://httpbin.org/html"

    try:
        # Direct fetch
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(test_url) as resp:
                direct_content = await resp.text()
                direct_length = len(direct_content)
                direct_status = resp.status
    except Exception as e:
        return {"error": f"Direct fetch failed: {str(e)[:100]}"}

    try:
        # Fetch through proxy
        connector = ProxyConnector.from_url(proxy.url)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(test_url) as resp:
                proxy_content = await resp.text()
                proxy_length = len(proxy_content)
                proxy_status = resp.status
    except Exception as e:
        return {
            "proxy": proxy.to_display_dict(),
            "error": f"Proxy fetch failed: {str(e)[:100]}",
            "verdict": "PROXY_UNREACHABLE",
        }

    modified = direct_content != proxy_content

    result = {
        "proxy": proxy.to_display_dict(),
        "content_modified": modified,
        "direct_length": direct_length,
        "proxy_length": proxy_length,
        "length_difference": proxy_length - direct_length,
        "direct_status": direct_status,
        "proxy_status": proxy_status,
    }

    if modified:
        result["verdict"] = "INJECTION_DETECTED"
        result["warning"] = "The proxy is modifying HTTP content. Do NOT use for sensitive traffic."
        # Show what was added/changed (first 200 chars of diff)
        if proxy_length > direct_length:
            extra = proxy_content[direct_length:][:200]
            result["injected_sample"] = extra
    else:
        result["verdict"] = "CLEAN"
        result["message"] = "No content modification detected."

    return result


# --- Tool parameter models ---

class TLSFingerprintParams(BaseModel):
    proxy_id: Optional[str] = Field(default=None, description="Proxy ID to check")
    host: Optional[str] = Field(default=None, description="Proxy host (use with port if no proxy_id)")
    port: Optional[int] = Field(default=None, description="Proxy port (use with host)")
    target_host: str = Field(default="www.google.com", description="Target HTTPS host to compare certificates")


class ContentInjectionParams(BaseModel):
    proxy_id: Optional[str] = Field(default=None, description="Proxy ID to check")
    host: Optional[str] = Field(default=None, description="Proxy host (use with port if no proxy_id)")
    port: Optional[int] = Field(default=None, description="Proxy port (use with host)")


# --- Tool registration ---

def register_tls_tools(registry: ToolRegistry):

    @registry.register(
        name="check_tls_fingerprint",
        description="Compare TLS certificate fingerprints through a proxy vs direct connection to detect MITM interception.",
        parameters_model=TLSFingerprintParams,
    )
    async def check_tls_fingerprint(
        proxy_id: str | None = None,
        host: str | None = None,
        port: int | None = None,
        target_host: str = "www.google.com",
    ) -> dict:
        pool = ProxyPool.load()
        proxy = None
        if proxy_id:
            proxy = pool.get_by_id(proxy_id)
        elif host and port:
            proxy = pool.get_by_address(host, port)

        if not proxy:
            return {"status": "not_found", "message": "Proxy not found in pool"}

        direct = _get_cert_fingerprint_direct(target_host)
        via_proxy = await _get_cert_fingerprint_via_proxy(proxy, target_host)

        if direct.get("error") or via_proxy.get("error"):
            return {
                "proxy": proxy.to_display_dict(),
                "target": target_host,
                "direct_result": direct,
                "proxy_result": via_proxy,
                "verdict": "INCONCLUSIVE",
                "message": "One or both connections failed. Cannot compare fingerprints.",
            }

        match = direct["fingerprint"] == via_proxy["fingerprint"]

        return {
            "proxy": proxy.to_display_dict(),
            "target": target_host,
            "direct_fingerprint": direct["fingerprint"],
            "proxy_fingerprint": via_proxy["fingerprint"],
            "match": match,
            "verdict": "TLS_INTACT" if match else "TLS_INTERCEPTION_DETECTED",
            "message": (
                "Certificate fingerprints match. TLS connection is not being intercepted."
                if match else
                "WARNING: Certificate fingerprints DO NOT match. This proxy may be performing TLS interception (MITM)."
            ),
            "direct_issuer": direct.get("issuer", ""),
            "proxy_issuer": via_proxy.get("issuer", ""),
        }

    @registry.register(
        name="check_content_injection",
        description="Detect if a proxy is injecting or modifying HTTP response content (ads, tracking, malware).",
        parameters_model=ContentInjectionParams,
    )
    async def check_content_injection(
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

        return await _check_content_injection(proxy)
