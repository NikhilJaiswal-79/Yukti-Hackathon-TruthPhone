"""Lambda entrypoint for the worker function."""

from __future__ import annotations

import asyncio
import json
import logging

from truthphone.config import LOG_LEVEL
from truthphone.worker import run_job

logging.basicConfig(level=LOG_LEVEL, force=True)
logger = logging.getLogger(__name__)


def handler(event, context):
    if isinstance(event, str):
        event = json.loads(event)
    session_id = event.get("sessionId")
    if not session_id:
        raise ValueError("sessionId is required")

    action = event.get("action", "chat")
    logger.info(
        "Worker invoked action=%s session=%s remaining_ms=%s",
        action,
        str(session_id)[:8],
        getattr(context, "get_remaining_time_in_millis", lambda: "?")(),
    )
    asyncio.run(
        run_job(
            session_id=session_id,
            action=action,
            message=event.get("message", ""),
            index=event.get("index"),
        )
    )
    return {"ok": True}
