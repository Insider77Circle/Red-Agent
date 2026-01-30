import asyncio
import inspect
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from pydantic import BaseModel, Field

from agent.tools import ToolRegistry
from proxy.models import ProxyEntry, ProxyProtocol
from proxy.pool import ProxyPool


# ---------------------------------------------------------------------------
# Source fetcher functions
# ---------------------------------------------------------------------------

PROXYSCRAPE_URL = "https://api.proxyscrape.com/v2/"


async def _fetch_from_proxyscrape(
    protocol: str = "http",
    timeout: int = 10000,
    country: str = "all",
    anonymity: str = "all",
) -> list[dict]:
    """Fetch proxies from ProxyScrape free API."""
    params = {
        "request": "displayproxies",
        "protocol": protocol,
        "timeout": str(timeout),
        "country": country,
        "anonymity": anonymity,
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(PROXYSCRAPE_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()

    proxies = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        try:
            host, port_str = line.rsplit(":", 1)
            port = int(port_str)
            proxies.append({"host": host, "port": port, "protocol": protocol})
        except ValueError:
            continue

    return proxies


async def _fetch_from_geonode(
    protocol: str = "http",
    limit: int = 50,
    country: str | None = None,
    anonymity: str | None = None,
) -> list[dict]:
    """Fetch proxies from Geonode free API."""
    params = {
        "limit": str(limit),
        "page": "1",
        "sort_by": "lastChecked",
        "sort_type": "desc",
    }
    if protocol:
        params["protocols"] = protocol
    if country:
        params["country"] = country
    if anonymity:
        params["anonymityLevel"] = anonymity

    url = "https://proxylist.geonode.com/api/proxy-list"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()

        proxies = []
        for entry in data.get("data", []):
            host = entry.get("ip", "")
            port = int(entry.get("port", 0))
            proto = entry.get("protocols", [protocol])[0] if entry.get("protocols") else protocol
            if host and port:
                proxies.append({
                    "host": host,
                    "port": port,
                    "protocol": proto,
                    "country": entry.get("country", None),
                })
        return proxies
    except Exception:
        return []


PROXY_LIST_DOWNLOAD_URL = "https://www.proxy-list.download/api/v1/get"


async def _fetch_from_proxy_list_download(
    protocol: str = "http",
    anonymity: str | None = None,
) -> list[dict]:
    """Fetch proxies from proxy-list.download API."""
    params = {"type": protocol}
    if anonymity:
        params["anon"] = anonymity

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                PROXY_LIST_DOWNLOAD_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                text = await resp.text()

        proxies = []
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            try:
                host, port_str = line.rsplit(":", 1)
                port = int(port_str)
                proxies.append({"host": host, "port": port, "protocol": protocol})
            except ValueError:
                continue
        return proxies
    except Exception:
        return []


PUBPROXY_URL = "http://pubproxy.com/api/proxy"


async def _fetch_from_pubproxy(
    protocol: str = "http",
    country: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Fetch proxies from PubProxy API. Free tier: max 5 per request, rate limited."""
    proxies = []
    calls_needed = min((limit + 4) // 5, 4)

    for _ in range(calls_needed):
        params = {"type": protocol, "limit": "5", "format": "json"}
        if country:
            params["country"] = country.upper()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    PUBPROXY_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()

            for entry in data.get("data", []):
                ip_port = entry.get("ipPort", "")
                if ":" in ip_port:
                    host, port_str = ip_port.rsplit(":", 1)
                    proxies.append({
                        "host": host,
                        "port": int(port_str),
                        "protocol": entry.get("type", protocol).lower(),
                        "country": entry.get("country", None),
                    })
        except Exception:
            pass

        await asyncio.sleep(1.0)

    return proxies


async def _fetch_from_proxyscrape_multi(
    protocols: list[str] | None = None,
    timeout: int = 10000,
    country: str = "all",
    anonymity: str = "all",
) -> list[dict]:
    """Fetch from ProxyScrape for multiple protocols, aggregating results."""
    if protocols is None:
        protocols = ["http", "socks4", "socks5"]

    all_proxies = []
    for proto in protocols:
        fetched = await _fetch_from_proxyscrape(
            protocol=proto, timeout=timeout, country=country, anonymity=anonymity,
        )
        all_proxies.extend(fetched)

    return all_proxies


FREE_PROXY_LIST_URL = "https://free-proxy-list.net/"


async def _fetch_from_free_proxy_list() -> list[dict]:
    """Fetch proxies from free-proxy-list.net by parsing the HTML table."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                FREE_PROXY_LIST_URL,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                html = await resp.text()

        proxies = []
        rows = re.findall(
            r"<tr><td>(\d+\.\d+\.\d+\.\d+)</td><td>(\d+)</td><td>(\w*)</td>"
            r"<td>[^<]*</td><td>([^<]*)</td><td>[^<]*</td><td>(yes|no)</td>",
            html,
        )
        for ip, port, country_code, _anonymity, https in rows:
            protocol = "https" if https == "yes" else "http"
            proxies.append({
                "host": ip,
                "port": int(port),
                "protocol": protocol,
                "country": country_code.upper() if country_code else None,
            })
        return proxies
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

PROXY_SOURCES = {
    "proxyscrape": {
        "fetch": _fetch_from_proxyscrape,
        "label": "ProxyScrape",
    },
    "geonode": {
        "fetch": _fetch_from_geonode,
        "label": "Geonode",
    },
    "proxy_list_download": {
        "fetch": _fetch_from_proxy_list_download,
        "label": "Proxy-List.Download",
    },
    "pubproxy": {
        "fetch": _fetch_from_pubproxy,
        "label": "PubProxy",
    },
    "proxyscrape_multi": {
        "fetch": _fetch_from_proxyscrape_multi,
        "label": "ProxyScrape Multi-Protocol",
    },
    "free_proxy_list": {
        "fetch": _fetch_from_free_proxy_list,
        "label": "Free-Proxy-List.net",
    },
}


# ---------------------------------------------------------------------------
# Core discovery logic (callable by tool and scheduler)
# ---------------------------------------------------------------------------

async def discover_proxies_internal(
    source: str = "all",
    protocol: str = "http",
    country: str | None = None,
    anonymity: str | None = None,
    limit: int = 100,
    auto_validate: bool = False,
    validate_timeout: int = 8,
    validate_concurrency: int = 20,
) -> dict:
    """Core discovery logic shared by the tool and the scheduler."""

    if source == "all":
        source_keys = list(PROXY_SOURCES.keys())
    elif source in PROXY_SOURCES:
        source_keys = [source]
    else:
        return {"status": "error", "message": f"Unknown source '{source}'. Available: {list(PROXY_SOURCES.keys())}"}

    raw: list[dict] = []
    sources_used: list[str] = []
    source_counts: dict[str, int] = {}

    for src_key in source_keys:
        fetcher = PROXY_SOURCES[src_key]["fetch"]
        try:
            sig = inspect.signature(fetcher)
            kwargs: dict = {}
            if "protocol" in sig.parameters:
                kwargs["protocol"] = protocol
            if "country" in sig.parameters:
                kwargs["country"] = country or ("all" if src_key.startswith("proxyscrape") else None)
            if "anonymity" in sig.parameters:
                kwargs["anonymity"] = anonymity or ("all" if src_key.startswith("proxyscrape") else None)
            if "limit" in sig.parameters:
                kwargs["limit"] = limit

            fetched = await fetcher(**kwargs)
            for p in fetched:
                p["_source"] = src_key
            raw.extend(fetched)
            source_counts[src_key] = len(fetched)
            sources_used.append(src_key)
        except Exception:
            source_counts[src_key] = 0

    # Deduplicate fetched list
    seen: set[tuple[str, int]] = set()
    unique: list[dict] = []
    for p in raw:
        key = (p["host"], p["port"])
        if key not in seen:
            seen.add(key)
            unique.append(p)
    raw = unique[:limit]

    now_iso = datetime.now(timezone.utc).isoformat()
    pool = ProxyPool.load()
    added = 0
    merged = 0
    skipped = 0
    newly_added: list[ProxyEntry] = []

    for p in raw:
        src_label = p.get("_source", source if source != "all" else "multi")
        existing = pool.get_by_address(p["host"], p["port"])

        if existing:
            result = pool.merge_entry(p["host"], p["port"], {
                "country": p.get("country"),
                "last_seen": now_iso,
                "source": src_label,
            })
            if result:
                merged += 1
            else:
                skipped += 1
            continue

        try:
            proto = ProxyProtocol(p["protocol"])
        except ValueError:
            proto = ProxyProtocol.HTTP

        entry = ProxyEntry(
            id=uuid.uuid4().hex[:8],
            host=p["host"],
            port=p["port"],
            protocol=proto,
            country=p.get("country"),
            source=src_label,
            sources=[src_label],
            last_seen=now_iso,
        )
        pool.add(entry)
        newly_added.append(entry)
        added += 1

    pool.save()

    # Auto-validate newly added proxies
    validated_alive = 0
    validated_dead = 0

    if auto_validate and newly_added:
        from proxy.checker import _check_single_proxy, _update_proxy_with_result

        semaphore = asyncio.Semaphore(validate_concurrency)

        async def bounded_check(proxy):
            async with semaphore:
                return proxy, await _check_single_proxy(proxy, validate_timeout)

        results = await asyncio.gather(*[bounded_check(e) for e in newly_added])

        dead_ids: set[str] = set()
        for proxy, check_result in results:
            _update_proxy_with_result(proxy, check_result)
            if check_result.reachable:
                validated_alive += 1
            else:
                validated_dead += 1
                dead_ids.add(proxy.id)

        if dead_ids:
            pool.proxies = [p for p in pool.proxies if p.id not in dead_ids]

        pool.save()

    result = {
        "sources_queried": sources_used,
        "source_counts": source_counts,
        "fetched_total": len(raw),
        "new_added": added if not auto_validate else validated_alive,
        "metadata_merged": merged,
        "duplicates_skipped": skipped,
        "total_in_pool": len(pool.proxies),
    }

    if auto_validate:
        result["validation"] = {
            "checked": len(newly_added),
            "alive": validated_alive,
            "dead_removed": validated_dead,
        }

    return result


# ---------------------------------------------------------------------------
# Tool parameter models
# ---------------------------------------------------------------------------

class DiscoverProxiesParams(BaseModel):
    source: str = Field(
        default="all",
        description=(
            "Source to fetch from: 'proxyscrape', 'geonode', 'proxy_list_download', "
            "'pubproxy', 'free_proxy_list', 'proxyscrape_multi', or 'all' for all sources"
        ),
    )
    protocol: str = Field(default="http", description="Protocol filter: http, socks4, socks5")
    country: Optional[str] = Field(default=None, description="Country code filter (e.g. 'US', 'DE')")
    anonymity: Optional[str] = Field(default=None, description="Anonymity filter: anonymous, elite")
    limit: int = Field(default=100, description="Maximum number of proxies to fetch")
    auto_validate: bool = Field(
        default=False,
        description="If true, immediately health-check newly added proxies and discard dead ones",
    )
    validate_timeout: int = Field(default=8, description="Timeout in seconds for auto-validation health checks")
    validate_concurrency: int = Field(default=20, description="Max concurrent health checks during auto-validation")


class ListSourcesParams(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register_discovery_tools(registry: ToolRegistry):

    @registry.register(
        name="discover_proxies",
        description=(
            "Fetch proxies from public APIs (ProxyScrape, Geonode, Proxy-List.Download, "
            "PubProxy, Free-Proxy-List.net). Adds new ones to the pool with smart dedup. "
            "Optionally auto-validates new proxies and discards dead ones."
        ),
        parameters_model=DiscoverProxiesParams,
    )
    async def discover_proxies(
        source: str = "all",
        protocol: str = "http",
        country: str | None = None,
        anonymity: str | None = None,
        limit: int = 100,
        auto_validate: bool = False,
        validate_timeout: int = 8,
        validate_concurrency: int = 20,
    ) -> dict:
        return await discover_proxies_internal(
            source=source,
            protocol=protocol,
            country=country,
            anonymity=anonymity,
            limit=limit,
            auto_validate=auto_validate,
            validate_timeout=validate_timeout,
            validate_concurrency=validate_concurrency,
        )

    @registry.register(
        name="list_discovery_sources",
        description="List all available proxy discovery sources.",
        parameters_model=ListSourcesParams,
    )
    def list_discovery_sources() -> dict:
        return {
            "sources": [
                {"name": key, "label": info["label"]}
                for key, info in PROXY_SOURCES.items()
            ],
            "total": len(PROXY_SOURCES),
        }
