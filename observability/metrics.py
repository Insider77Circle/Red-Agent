from typing import Optional

from pydantic import BaseModel, Field

from agent.tools import ToolRegistry
from proxy.models import AnonymityLevel
from proxy.pool import ProxyPool


# --- Tool parameter models ---

class PoolStatsParams(BaseModel):
    pass


class LatencyReportParams(BaseModel):
    top_n: int = Field(default=10, description="Number of top proxies to show")


# --- Tool registration ---

def register_metrics_tools(registry: ToolRegistry):

    @registry.register(
        name="show_pool_stats",
        description="Show summary statistics of the proxy pool: counts, protocols, countries, latency distribution, anonymity breakdown.",
        parameters_model=PoolStatsParams,
    )
    def show_pool_stats() -> dict:
        pool = ProxyPool.load()
        proxies = pool.proxies

        if not proxies:
            return {"status": "empty", "message": "No proxies in pool."}

        total = len(proxies)
        alive = sum(1 for p in proxies if p.is_alive is True)
        dead = sum(1 for p in proxies if p.is_alive is False)
        unchecked = sum(1 for p in proxies if p.is_alive is None)

        # By protocol
        by_protocol: dict[str, int] = {}
        for p in proxies:
            key = p.protocol.value
            by_protocol[key] = by_protocol.get(key, 0) + 1

        # By country
        by_country: dict[str, int] = {}
        for p in proxies:
            key = p.country or "unknown"
            by_country[key] = by_country.get(key, 0) + 1
        # Sort by count, top 15
        by_country = dict(sorted(by_country.items(), key=lambda x: -x[1])[:15])

        # By anonymity
        by_anonymity: dict[str, int] = {}
        for level in AnonymityLevel:
            count = sum(1 for p in proxies if p.anonymity == level)
            if count > 0:
                by_anonymity[level.value] = count

        # By source
        by_source: dict[str, int] = {}
        for p in proxies:
            by_source[p.source] = by_source.get(p.source, 0) + 1

        # Latency stats
        latencies = [p.latency_ms for p in proxies if p.latency_ms is not None]
        latency_stats = {}
        if latencies:
            latencies_sorted = sorted(latencies)
            latency_stats = {
                "avg_ms": round(sum(latencies) / len(latencies), 2),
                "min_ms": latencies_sorted[0],
                "max_ms": latencies_sorted[-1],
                "median_ms": latencies_sorted[len(latencies_sorted) // 2],
                "under_500ms": sum(1 for l in latencies if l < 500),
                "under_1000ms": sum(1 for l in latencies if l < 1000),
                "over_1000ms": sum(1 for l in latencies if l >= 1000),
            }

        return {
            "total_proxies": total,
            "alive": alive,
            "dead": dead,
            "unchecked": unchecked,
            "by_protocol": by_protocol,
            "by_country": by_country,
            "by_anonymity": by_anonymity,
            "by_source": by_source,
            "latency": latency_stats if latency_stats else "No latency data (run health checks first)",
        }

    @registry.register(
        name="show_latency_report",
        description="Show a latency ranking of proxies, sorted fastest to slowest.",
        parameters_model=LatencyReportParams,
    )
    def show_latency_report(top_n: int = 10) -> dict:
        pool = ProxyPool.load()

        # Only proxies with latency data
        with_latency = [p for p in pool.proxies if p.latency_ms is not None and p.is_alive]
        with_latency.sort(key=lambda p: p.latency_ms)

        if not with_latency:
            return {
                "status": "no_data",
                "message": "No latency data available. Run check_all_proxies first.",
            }

        fastest = with_latency[:top_n]
        slowest = list(reversed(with_latency[-min(top_n, len(with_latency)):]))

        return {
            "total_with_data": len(with_latency),
            "fastest": [
                {
                    "rank": i + 1,
                    "id": p.id,
                    "address": p.address,
                    "protocol": p.protocol.value,
                    "latency_ms": p.latency_ms,
                    "country": p.country,
                    "anonymity": p.anonymity.value,
                }
                for i, p in enumerate(fastest)
            ],
            "slowest": [
                {
                    "rank": i + 1,
                    "id": p.id,
                    "address": p.address,
                    "latency_ms": p.latency_ms,
                }
                for i, p in enumerate(slowest)
            ],
        }
