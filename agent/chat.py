import os
import json
import logging

from openai import AsyncOpenAI
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.live import Live

from agent.system_prompt import SYSTEM_PROMPT
from agent.tools import ToolRegistry
from observability.logger import log_tool_call


class ChatSession:
    """Manages conversation with DeepSeek, including tool dispatch."""

    MAX_HISTORY = 80  # Keep last N messages to avoid context overflow

    def __init__(self, console: Console, tool_registry: ToolRegistry):
        self.console = console
        self.client = AsyncOpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
        )
        self.tool_registry = tool_registry
        self.messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    async def send_message(self, user_input: str) -> None:
        """Send user message, handle tool calls, render final response."""
        self.messages.append({"role": "user", "content": user_input})
        self._trim_history()

        response = await self._call_api()

        # Tool call loop — model may chain multiple rounds of tool use
        while response.choices[0].message.tool_calls:
            assistant_msg = response.choices[0].message
            # Append assistant message with tool_calls to history
            self.messages.append({
                "role": "assistant",
                "content": assistant_msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in assistant_msg.tool_calls
                ],
            })

            # Execute each tool call
            for tool_call in assistant_msg.tool_calls:
                func_name = tool_call.function.name
                try:
                    func_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    func_args = {}

                self.console.print(
                    f"  [dim]> Calling {func_name}({json.dumps(func_args, default=str)[:120]})[/dim]"
                )

                result = await self.tool_registry.execute(func_name, func_args)

                # Log the tool call
                if isinstance(result, dict):
                    log_tool_call(func_name, func_args, result)

                result_str = json.dumps(result, default=str) if not isinstance(result, str) else result

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_str,
                })

            # Call API again with tool results
            response = await self._call_api()

        # Final text response
        final_text = response.choices[0].message.content or ""
        self.messages.append({"role": "assistant", "content": final_text})
        self._render_response(final_text)

    async def _call_api(self):
        """Call DeepSeek chat completions with tools."""
        tools = self.tool_registry.get_tool_definitions()
        kwargs = {
            "model": "deepseek-chat",
            "messages": self.messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        return await self.client.chat.completions.create(**kwargs)

    def _render_response(self, text: str) -> None:
        """Render the assistant's response with rich markdown."""
        self.console.print()
        self.console.print(Panel(
            Markdown(text),
            title="[bold red]RedAgent[/bold red]",
            border_style="red",
            padding=(1, 2),
        ))
        self.console.print()

    def _trim_history(self) -> None:
        """Keep conversation history within bounds to avoid context overflow.

        Finds a safe cut point that doesn't split a tool call sequence
        (assistant message with tool_calls followed by tool result messages).
        """
        if len(self.messages) <= self.MAX_HISTORY:
            return

        tail = self.messages[-(self.MAX_HISTORY - 1):]

        # Walk forward to find a safe starting point — skip any orphaned
        # tool-result messages that reference a tool_calls message we cut off.
        start = 0
        while start < len(tail):
            msg = tail[start]
            if msg["role"] == "tool":
                # This tool result's parent assistant message was trimmed; skip it.
                start += 1
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                # Count how many tool results must follow this assistant message.
                expected = len(msg["tool_calls"])
                remaining = len(tail) - (start + 1)
                tool_results = 0
                for j in range(start + 1, len(tail)):
                    if tail[j]["role"] == "tool":
                        tool_results += 1
                    else:
                        break
                if tool_results < expected:
                    # Incomplete tool sequence — skip the whole group.
                    start += 1 + tool_results
                else:
                    break
            else:
                break

        self.messages = [self.messages[0]] + tail[start:]
