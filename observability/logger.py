import json
import logging
from datetime import datetime, timezone
from pathlib import Path


LOG_FILE = Path(__file__).parent.parent / "data" / "redagent.log"


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra_data"):
            entry["data"] = record.extra_data
        return json.dumps(entry, default=str)


def setup_logging() -> logging.Logger:
    """Set up structured JSON logging to file."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("redagent")
    if logger.handlers:
        return logger  # Already configured

    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    return logger


def log_tool_call(tool_name: str, args: dict, result: dict) -> None:
    """Log a tool invocation."""
    logger = logging.getLogger("redagent")
    record = logger.makeRecord(
        name="redagent",
        level=logging.INFO,
        fn="",
        lno=0,
        msg=f"Tool called: {tool_name}",
        args=(),
        exc_info=None,
    )
    record.extra_data = {
        "tool": tool_name,
        "arguments": args,
        "result_keys": list(result.keys()) if isinstance(result, dict) else type(result).__name__,
    }
    logger.handle(record)
