"""Log output: plain lines locally, one JSON object per line in production."""

import json
import logging
import os
import sys
from datetime import UTC, datetime

from opentelemetry import trace


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "target": record.name,
            "message": record.getMessage(),
        }
        span = trace.get_current_span().get_span_context()
        if span.is_valid:  # only with telemetry export on
            event["trace_id"] = format(span.trace_id, "032x")
            event["span_id"] = format(span.span_id, "016x")
        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)
        return json.dumps(event, ensure_ascii=False)


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if os.getenv("LOG_FORMAT", "").lower() == "json"
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.basicConfig(level=level, handlers=[handler])
    if level != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("httpx2").setLevel(logging.WARNING)  # the OpenAI SDK's
