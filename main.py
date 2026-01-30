import asyncio
import os
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from agent.chat import ChatSession
from agent.tools import ToolRegistry
from observability.logger import setup_logging


BANNER = r"""
 ____          _    _                    _
|  _ \ ___  __| |  / \   __ _  ___ _ __ | |_
| |_) / _ \/ _` | / _ \ / _` |/ _ \ '_ \| __|
|  _ <  __/ (_| |/ ___ \ (_| |  __/ | | | |_
|_| \_\___|\__,_/_/   \_\__, |\___|_| |_|\__|
                        |___/
"""


def build_tool_registry() -> ToolRegistry:
    """Build and populate the tool registry with all modules."""
    registry = ToolRegistry()

    from proxy.pool import register_pool_tools
    from proxy.checker import register_checker_tools
    from proxy.discovery import register_discovery_tools
    from proxy.router import register_router_tools
    from proxy.proxychains import register_proxychains_tools
    from security.dns_leak import register_dns_tools
    from security.tls_check import register_tls_tools
    from security.analyzer import register_analyzer_tools
    from observability.metrics import register_metrics_tools
    from proxy.scheduler import register_scheduler_tools
    from proxy.executor import register_executor_tools
    from proxy.active_config import register_active_config_tools

    register_pool_tools(registry)
    register_checker_tools(registry)
    register_discovery_tools(registry)
    register_router_tools(registry)
    register_proxychains_tools(registry)
    register_executor_tools(registry)
    register_active_config_tools(registry)
    register_dns_tools(registry)
    register_tls_tools(registry)
    register_analyzer_tools(registry)
    register_metrics_tools(registry)
    register_scheduler_tools(registry)

    return registry


async def main():
    load_dotenv()

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("Error: DEEPSEEK_API_KEY not found in .env file.")
        sys.exit(1)

    setup_logging()
    console = Console()

    console.print(Panel(
        Text(BANNER, style="bold red", justify="center"),
        title="[bold white]Proxy Management AI[/bold white]",
        border_style="red",
        padding=(0, 2),
    ))
    registry = build_tool_registry()
    tool_count = len(registry.get_tool_definitions())
    console.print(f"[dim]{tool_count} tools loaded. Type your message to interact with RedAgent. Type 'exit' or 'quit' to leave.[/dim]\n")

    session = ChatSession(console, registry)
    loop = asyncio.get_running_loop()

    while True:
        try:
            user_input = await loop.run_in_executor(
                None, lambda: console.input("[bold green]You > [/bold green]")
            )
        except (KeyboardInterrupt, EOFError):
            break

        stripped = user_input.strip().lower()
        if stripped in ("exit", "quit", "/quit", "/exit"):
            break
        if not stripped:
            continue

        try:
            await session.send_message(user_input)
        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted.[/dim]")
        except Exception as e:
            console.print(f"\n[bold red]Error:[/bold red] {type(e).__name__}: {e}\n")

    # Stop scheduler if running
    from proxy.scheduler import DiscoveryScheduler
    scheduler = DiscoveryScheduler.get_instance()
    if scheduler.running:
        scheduler.stop()
        console.print("[dim]Discovery scheduler stopped.[/dim]")

    console.print("\n[dim]RedAgent signing off.[/dim]\n")


if __name__ == "__main__":
    asyncio.run(main())
