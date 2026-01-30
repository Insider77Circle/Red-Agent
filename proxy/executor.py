import asyncio
import os
import time
from typing import Optional

import aiohttp
from aiohttp_socks import ProxyConnector, ChainProxyConnector, ProxyInfo
from pydantic import BaseModel, Field
from python_socks import ProxyType

from agent.tools import ToolRegistry
from proxy.models import ProxyEntry, ProxyProtocol
from proxy.pool import ProxyPool


MAX_BODY_LENGTH = 4000
DEFAULT_TIMEOUT = 30


def _proto_to_proxy_type(protocol: ProxyProtocol) -> ProxyType:
    """Map ProxyProtocol enum to python-socks ProxyType."""
    mapping = {
        ProxyProtocol.SOCKS4: ProxyType.SOCKS4,
        ProxyProtocol.SOCKS5: ProxyType.SOCKS5,
        ProxyProtocol.HTTP: ProxyType.HTTP,
        ProxyProtocol.HTTPS: ProxyType.HTTP,
    }
    return mapping[protocol]


async def _execute_direct(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
) -> dict:
    """Execute a request directly (no proxy)."""
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    start = time.monotonic()

    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        kwargs: dict = {"headers": headers or {}, "allow_redirects": follow_redirects}
        if body and method.upper() in ("POST", "PUT", "PATCH"):
            kwargs["data"] = body

        async with session.request(method.upper(), url, **kwargs) as resp:
            elapsed_ms = round((time.monotonic() - start) * 1000, 2)
            resp_body = await resp.text()
            truncated = len(resp_body) > MAX_BODY_LENGTH

            return {
                "status": "success",
                "status_code": resp.status,
                "reason": resp.reason,
                "response_headers": dict(resp.headers),
                "body": resp_body[:MAX_BODY_LENGTH],
                "body_truncated": truncated,
                "body_full_length": len(resp_body),
                "elapsed_ms": elapsed_ms,
                "proxy_used": "direct (no proxy)",
            }


async def _execute_single_proxy(
    proxy: ProxyEntry,
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
) -> dict:
    """Execute an HTTP request through a single proxy."""
    connector = ProxyConnector.from_url(proxy.url)
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    start = time.monotonic()
    async with aiohttp.ClientSession(connector=connector, timeout=client_timeout) as session:
        kwargs: dict = {"headers": headers or {}, "allow_redirects": follow_redirects}
        if body and method.upper() in ("POST", "PUT", "PATCH"):
            kwargs["data"] = body

        async with session.request(method.upper(), url, **kwargs) as resp:
            elapsed_ms = round((time.monotonic() - start) * 1000, 2)
            resp_body = await resp.text()
            truncated = len(resp_body) > MAX_BODY_LENGTH

            return {
                "status": "success",
                "status_code": resp.status,
                "reason": resp.reason,
                "response_headers": dict(resp.headers),
                "body": resp_body[:MAX_BODY_LENGTH],
                "body_truncated": truncated,
                "body_full_length": len(resp_body),
                "elapsed_ms": elapsed_ms,
                "proxy_used": proxy.to_display_dict(),
            }


async def _execute_chain(
    proxies: list[ProxyEntry],
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
) -> dict:
    """Execute an HTTP request through a multi-hop proxy chain."""
    if len(proxies) == 1:
        return await _execute_single_proxy(proxies[0], url, method, headers, body, timeout, follow_redirects)

    proxy_infos = [
        ProxyInfo(
            proxy_type=_proto_to_proxy_type(p.protocol),
            host=p.host,
            port=p.port,
            username=p.username,
            password=p.password,
        )
        for p in proxies
    ]

    connector = ChainProxyConnector(proxy_infos)
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    start = time.monotonic()
    async with aiohttp.ClientSession(connector=connector, timeout=client_timeout) as session:
        kwargs: dict = {"headers": headers or {}, "allow_redirects": follow_redirects}
        if body and method.upper() in ("POST", "PUT", "PATCH"):
            kwargs["data"] = body

        async with session.request(method.upper(), url, **kwargs) as resp:
            elapsed_ms = round((time.monotonic() - start) * 1000, 2)
            resp_body = await resp.text()
            truncated = len(resp_body) > MAX_BODY_LENGTH

            return {
                "status": "success",
                "status_code": resp.status,
                "reason": resp.reason,
                "response_headers": dict(resp.headers),
                "body": resp_body[:MAX_BODY_LENGTH],
                "body_truncated": truncated,
                "body_full_length": len(resp_body),
                "elapsed_ms": elapsed_ms,
                "chain_hops": len(proxies),
                "chain": [p.to_display_dict() for p in proxies],
            }


# --- Tool parameter models ---

class ProxyFetchParams(BaseModel):
    url: str = Field(description="Target URL to fetch")
    method: str = Field(default="GET", description="HTTP method: GET, POST, PUT, DELETE, HEAD, PATCH")
    proxy_id: Optional[str] = Field(default=None, description="Single proxy ID to route through")
    chain_proxy_ids: list[str] = Field(default_factory=list, description="Ordered list of proxy IDs for multi-hop chain")
    headers: dict[str, str] = Field(default_factory=dict, description="Custom request headers")
    body: Optional[str] = Field(default=None, description="Request body (for POST/PUT/PATCH)")
    timeout: int = Field(default=30, description="Request timeout in seconds")
    follow_redirects: bool = Field(default=True, description="Follow HTTP redirects")


class ProxyCurlParams(BaseModel):
    url: str = Field(description="URL to fetch")
    proxy_id: Optional[str] = Field(default=None, description="Proxy ID to use. Omit for active config or direct.")
    headers: dict[str, str] = Field(default_factory=dict, description="Custom headers")


class ProxyExecParams(BaseModel):
    command: str = Field(description="Shell command to execute through the proxy (e.g. 'curl https://example.com', 'python script.py')")
    proxy_id: Optional[str] = Field(default=None, description="Proxy ID to use. Omit to use active config.")
    timeout: int = Field(default=60, description="Command timeout in seconds")


MAX_OUTPUT_LENGTH = 8000


# --- Tool registration ---

def register_executor_tools(registry: ToolRegistry):

    @registry.register(
        name="proxy_fetch",
        description=(
            "Execute an HTTP request through a proxy or proxy chain. "
            "Specify proxy_id for single-hop or chain_proxy_ids for multi-hop. "
            "If neither is specified, uses the active proxy config (if set) or direct connection. "
            "Returns status code, headers, and body (truncated to 4KB)."
        ),
        parameters_model=ProxyFetchParams,
    )
    async def proxy_fetch(
        url: str,
        method: str = "GET",
        proxy_id: str | None = None,
        chain_proxy_ids: list[str] | None = None,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        timeout: int = 30,
        follow_redirects: bool = True,
    ) -> dict:
        pool = ProxyPool.load()

        try:
            # Case 1: Explicit multi-hop chain
            if chain_proxy_ids:
                proxies = []
                missing = []
                for pid in chain_proxy_ids:
                    p = pool.get_by_id(pid)
                    if p:
                        proxies.append(p)
                    else:
                        missing.append(pid)
                if missing:
                    return {"status": "error", "message": f"Proxy IDs not found: {', '.join(missing)}"}
                return await _execute_chain(proxies, url, method, headers, body, timeout, follow_redirects)

            # Case 2: Single explicit proxy
            if proxy_id:
                proxy = pool.get_by_id(proxy_id)
                if not proxy:
                    return {"status": "error", "message": f"Proxy ID '{proxy_id}' not found in pool"}
                return await _execute_single_proxy(proxy, url, method, headers, body, timeout, follow_redirects)

            # Case 3: Use active config
            from proxy.active_config import ActiveConfig
            config = ActiveConfig.load()
            if config and config.proxy_ids:
                proxies = []
                for pid in config.proxy_ids:
                    p = pool.get_by_id(pid)
                    if p:
                        proxies.append(p)
                if proxies:
                    cfg_timeout = config.timeout if timeout == 30 else timeout
                    if len(proxies) == 1:
                        return await _execute_single_proxy(proxies[0], url, method, headers, body, cfg_timeout, follow_redirects)
                    return await _execute_chain(proxies, url, method, headers, body, cfg_timeout, follow_redirects)

            # Case 4: Direct connection
            return await _execute_direct(url, method, headers, body, timeout, follow_redirects)

        except Exception as e:
            return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}

    @registry.register(
        name="proxy_curl",
        description=(
            "Simple curl-like fetch through a proxy. Provide a URL and optionally a proxy_id. "
            "Uses GET method. Returns status code and response body."
        ),
        parameters_model=ProxyCurlParams,
    )
    async def proxy_curl(
        url: str,
        proxy_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        return await proxy_fetch(
            url=url,
            method="GET",
            proxy_id=proxy_id,
            headers=headers or {},
        )

    @registry.register(
        name="proxy_exec",
        description=(
            "Run any shell command with proxy environment variables set (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY). "
            "Most CLI tools (curl, wget, pip, git, python, nmap --proxies, etc.) respect these variables. "
            "Uses the specified proxy_id or the active config. Returns stdout and stderr."
        ),
        parameters_model=ProxyExecParams,
    )
    async def proxy_exec(
        command: str,
        proxy_id: str | None = None,
        timeout: int = 60,
    ) -> dict:
        pool = ProxyPool.load()

        # Resolve which proxy to use
        proxy_url = None
        proxy_info = None

        if proxy_id:
            proxy = pool.get_by_id(proxy_id)
            if not proxy:
                return {"status": "error", "message": f"Proxy ID '{proxy_id}' not found in pool"}
            proxy_url = proxy.url
            proxy_info = proxy.to_display_dict()
        else:
            from proxy.active_config import ActiveConfig
            config = ActiveConfig.load()
            if config and config.proxy_ids:
                # Use the first proxy for env vars (multi-hop chains can't be
                # expressed via a single env var — warn the user)
                p = pool.get_by_id(config.proxy_ids[0])
                if p:
                    proxy_url = p.url
                    proxy_info = p.to_display_dict()
                    if len(config.proxy_ids) > 1:
                        proxy_info["note"] = (
                            "Only the first proxy in the chain is used for environment variables. "
                            "Multi-hop chaining via env vars is not supported by most tools."
                        )

        if not proxy_url:
            return {
                "status": "error",
                "message": "No proxy specified and no active config set. Provide a proxy_id or apply_proxy_config first.",
            }

        # Build environment with proxy variables
        env = os.environ.copy()
        env["HTTP_PROXY"] = proxy_url
        env["HTTPS_PROXY"] = proxy_url
        env["ALL_PROXY"] = proxy_url
        env["http_proxy"] = proxy_url
        env["https_proxy"] = proxy_url
        env["all_proxy"] = proxy_url

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")

            return {
                "status": "completed",
                "exit_code": proc.returncode,
                "stdout": stdout_text[:MAX_OUTPUT_LENGTH],
                "stdout_truncated": len(stdout_text) > MAX_OUTPUT_LENGTH,
                "stderr": stderr_text[:MAX_OUTPUT_LENGTH] if stderr_text else None,
                "proxy_used": proxy_info,
                "env_vars_set": ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"],
            }

        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "message": f"Command timed out after {timeout} seconds.",
                "proxy_used": proxy_info,
            }
        except Exception as e:
            return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}
