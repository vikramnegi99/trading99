"""Structured logging: console + JSON-lines file (data/logs/agent.log)."""
import json
import logging
import os
import sys
from datetime import datetime, timezone

_CONFIGURED = False


def setup_logging(log_dir: str = "data/logs", level=logging.INFO):
    global _CONFIGURED
    if _CONFIGURED:
        return
    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = logging.FileHandler(os.path.join(log_dir, "agent.log"))
    fh.setFormatter(fmt)
    root.addHandler(fh)
    _CONFIGURED = True


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(logger: logging.Logger, event: str, **fields):
    """Structured one-line event, safe for logs (never log secrets)."""
    payload = {"ts": utcnow_iso(), "event": event}
    payload.update({k: v for k, v in fields.items()})
    logger.info(json.dumps(payload, default=str))
