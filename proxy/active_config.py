import json
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from agent.tools import ToolRegistry
from proxy.pool import ProxyPool


CONFIG_FILE = Path(__file__).parent.parent / "data" / "active_config.json"


class ChainMode(str, Enum):
    DIRECT = "direct"
    SINGLE = "single"
    CHAIN = "chain"


class ActiveConfig(BaseModel):
    """Persisted active proxy routing configuration."""

    mode: ChainMode = Field(default=ChainMode.DIRECT)
    proxy_ids: list[str] = Field(default_factory=list)
    proxy_dns: bool = Field(default=True)
    timeout: int = Field(default=30)

    def save(self) -> None:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls) -> Optional["ActiveConfig"]:
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                return cls(**data)
            except Exception:
                return None
        return None

    @classmethod
    def clear(cls) -> bool:
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
            return True
        return False


# --- Tool parameter models ---

class ApplyProxyConfigParams(BaseModel):
    proxy_ids: list[str] = Field(description="Ordered list of proxy IDs to set as the active chain")
    proxy_dns: bool = Field(default=True, description="Route DNS through the chain")
    timeout: int = Field(default=30, description="Default timeout for proxied requests in seconds")


class EmptyParams(BaseModel):
    pass


# --- Tool registration ---

def register_active_config_tools(registry: ToolRegistry):

    @registry.register(
        name="apply_proxy_config",
        description=(
            "Set the active proxy configuration. All subsequent proxy_fetch/proxy_curl calls "
            "that don't specify an explicit proxy will use this config. "
            "Pass a single proxy ID for single-hop, or multiple IDs for a multi-hop chain. "
            "The order of IDs defines the chain order."
        ),
        parameters_model=ApplyProxyConfigParams,
    )
    def apply_proxy_config(
        proxy_ids: list[str],
        proxy_dns: bool = True,
        timeout: int = 30,
    ) -> dict:
        pool = ProxyPool.load()

        resolved = []
        missing = []
        for pid in proxy_ids:
            p = pool.get_by_id(pid)
            if p:
                resolved.append(p)
            else:
                missing.append(pid)

        if missing:
            return {"status": "error", "message": f"Proxy IDs not found: {', '.join(missing)}"}

        if not resolved:
            return {"status": "error", "message": "No proxy IDs provided."}

        mode = ChainMode.SINGLE if len(resolved) == 1 else ChainMode.CHAIN

        config = ActiveConfig(
            mode=mode,
            proxy_ids=proxy_ids,
            proxy_dns=proxy_dns,
            timeout=timeout,
        )
        config.save()

        return {
            "status": "applied",
            "mode": mode.value,
            "chain": [p.to_display_dict() for p in resolved],
            "proxy_dns": proxy_dns,
            "timeout": timeout,
            "message": (
                f"Active config set: {mode.value} mode with {len(resolved)} proxy(ies). "
                f"All proxy_fetch/proxy_curl calls will now route through this config."
            ),
        }

    @registry.register(
        name="show_active_config",
        description="Show the currently active proxy configuration that RedAgent uses for proxied requests.",
        parameters_model=EmptyParams,
    )
    def show_active_config() -> dict:
        config = ActiveConfig.load()
        if not config or not config.proxy_ids:
            return {
                "status": "none",
                "mode": "direct",
                "message": "No active proxy config. Requests go direct (no proxy).",
            }

        pool = ProxyPool.load()
        chain = []
        for pid in config.proxy_ids:
            p = pool.get_by_id(pid)
            if p:
                chain.append(p.to_display_dict())
            else:
                chain.append({"id": pid, "status": "NOT_FOUND_IN_POOL"})

        return {
            "status": "active",
            "mode": config.mode.value,
            "proxy_dns": config.proxy_dns,
            "timeout": config.timeout,
            "chain": chain,
        }

    @registry.register(
        name="clear_active_config",
        description="Remove the active proxy config. All requests will go direct (no proxy) unless explicitly specified.",
        parameters_model=EmptyParams,
    )
    def clear_active_config() -> dict:
        cleared = ActiveConfig.clear()
        if cleared:
            return {"status": "cleared", "message": "Active proxy config removed. Requests now go direct."}
        return {"status": "none", "message": "No active config was set. Already in direct mode."}
