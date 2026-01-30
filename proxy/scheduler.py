import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from agent.tools import ToolRegistry

logger = logging.getLogger("redagent")


class DiscoveryScheduler:
    """Background scheduler that periodically runs proxy discovery."""

    _instance: Optional["DiscoveryScheduler"] = None

    def __init__(self):
        self.running: bool = False
        self.interval_minutes: int = 30
        self.source: str = "all"
        self.protocol: str = "http"
        self.auto_validate: bool = True
        self.limit: int = 100
        self._task: Optional[asyncio.Task] = None
        self._last_run: Optional[str] = None
        self._run_count: int = 0
        self._last_result: Optional[dict] = None

    @classmethod
    def get_instance(cls) -> "DiscoveryScheduler":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def _run_loop(self):
        """Internal loop that runs discovery on a schedule."""
        while self.running:
            try:
                from proxy.discovery import discover_proxies_internal

                result = await discover_proxies_internal(
                    source=self.source,
                    protocol=self.protocol,
                    limit=self.limit,
                    auto_validate=self.auto_validate,
                )
                self._last_run = datetime.now(timezone.utc).isoformat()
                self._run_count += 1
                self._last_result = result
                logger.info(
                    f"Scheduled discovery completed: {result.get('new_added', 0)} added, "
                    f"{result.get('total_in_pool', '?')} total"
                )
            except Exception as e:
                logger.error(f"Scheduled discovery failed: {e}")

            # Sleep in 5-second increments to allow clean cancellation
            for _ in range(self.interval_minutes * 12):
                if not self.running:
                    return
                await asyncio.sleep(5)

    def start(
        self,
        interval_minutes: int = 30,
        source: str = "all",
        protocol: str = "http",
        auto_validate: bool = True,
        limit: int = 100,
    ) -> bool:
        """Start the background scheduler. Returns False if already running."""
        if self.running:
            return False
        self.interval_minutes = interval_minutes
        self.source = source
        self.protocol = protocol
        self.auto_validate = auto_validate
        self.limit = limit
        self.running = True
        self._task = asyncio.create_task(self._run_loop())
        return True

    def stop(self) -> bool:
        """Stop the background scheduler. Returns False if not running."""
        if not self.running:
            return False
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        return True

    def status(self) -> dict:
        """Return current scheduler status."""
        return {
            "running": self.running,
            "interval_minutes": self.interval_minutes,
            "source": self.source,
            "protocol": self.protocol,
            "auto_validate": self.auto_validate,
            "limit": self.limit,
            "last_run": self._last_run,
            "run_count": self._run_count,
            "last_result_summary": (
                {
                    "new_added": self._last_result.get("new_added"),
                    "total_in_pool": self._last_result.get("total_in_pool"),
                }
                if self._last_result
                else None
            ),
        }


# --- Tool parameter models ---

class StartSchedulerParams(BaseModel):
    interval_minutes: int = Field(
        default=30,
        description="Minutes between discovery runs (minimum 5)",
    )
    source: str = Field(default="all", description="Source(s) to use: 'all' or a specific source name")
    protocol: str = Field(default="http", description="Protocol filter for discovery")
    auto_validate: bool = Field(default=True, description="Auto-validate new proxies after each discovery")
    limit: int = Field(default=100, description="Maximum proxies per discovery run")


class StopSchedulerParams(BaseModel):
    pass


class SchedulerStatusParams(BaseModel):
    pass


# --- Tool registration ---

def register_scheduler_tools(registry: ToolRegistry):

    @registry.register(
        name="start_discovery_scheduler",
        description=(
            "Start a background task that periodically discovers new proxies and adds them to the pool. "
            "Runs in the background while you continue using RedAgent."
        ),
        parameters_model=StartSchedulerParams,
    )
    def start_discovery_scheduler(
        interval_minutes: int = 30,
        source: str = "all",
        protocol: str = "http",
        auto_validate: bool = True,
        limit: int = 100,
    ) -> dict:
        if interval_minutes < 5:
            return {"status": "error", "message": "Minimum interval is 5 minutes."}

        scheduler = DiscoveryScheduler.get_instance()
        started = scheduler.start(
            interval_minutes=interval_minutes,
            source=source,
            protocol=protocol,
            auto_validate=auto_validate,
            limit=limit,
        )
        if started:
            return {
                "status": "started",
                "interval_minutes": interval_minutes,
                "source": source,
                "auto_validate": auto_validate,
                "message": f"Discovery scheduler started. Will run every {interval_minutes} minutes.",
            }
        return {
            "status": "already_running",
            "message": "Scheduler is already running. Stop it first to reconfigure.",
            "current": scheduler.status(),
        }

    @registry.register(
        name="stop_discovery_scheduler",
        description="Stop the background discovery scheduler.",
        parameters_model=StopSchedulerParams,
    )
    def stop_discovery_scheduler() -> dict:
        scheduler = DiscoveryScheduler.get_instance()
        stopped = scheduler.stop()
        if stopped:
            return {"status": "stopped", "message": "Discovery scheduler stopped."}
        return {"status": "not_running", "message": "Scheduler was not running."}

    @registry.register(
        name="discovery_scheduler_status",
        description="Check the current status of the background discovery scheduler.",
        parameters_model=SchedulerStatusParams,
    )
    def discovery_scheduler_status() -> dict:
        scheduler = DiscoveryScheduler.get_instance()
        return scheduler.status()
