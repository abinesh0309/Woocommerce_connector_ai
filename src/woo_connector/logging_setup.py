"""Logging goes to stderr only: stdout belongs to the stdio MCP transport."""

import json
import logging
import os
import sys


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure() -> None:
    level = os.environ.get("WOO_LOG_LEVEL", "INFO").upper()
    fmt = os.environ.get("WOO_LOG_FORMAT", "text").lower()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level if level in logging.getLevelNamesMapping() else "INFO")
    # OAuth1 URLs carry the consumer key in the query string, and httpx logs full URLs at INFO.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
