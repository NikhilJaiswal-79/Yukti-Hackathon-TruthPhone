"""FastAPI application for the TruthPhone API Lambda.

The API only does cheap work: create/read sessions, append the user message,
atomically claim the session, and asynchronously invoke the worker Lambda.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
import time
import uuid
from typing import Any, Dict, List

import boto3
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import store
from .config import (
    ALLOWED_ORIGINS,
    LOCAL_WORKER,
    LOG_LEVEL,
    MODEL_ID,
    REGION,
    WORKER_FUNCTION_NAME,
)

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

app = FastAPI(title="TruthPhone API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)
    forceSearch: bool = False


class SelectRequest(BaseModel):
    index: int = Field(ge=0)


_lambda_client = None
JOB_STALE_SECONDS = 900


def _get_available(session_id: str) -> Dict[str, Any]:
    """Return a session, rejecting a job that is genuinely still running."""
    item = _reconcile_running(session_id)
    if item.get("jobStatus") == "running":
        raise HTTPException(
            status_code=409,
            detail="The agent is still working. Wait for the live view to finish.",
        )
    return item


def _lambda():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=REGION)
    return _lambda_client


def _run_local_job(payload: Dict[str, Any]) -> None:
    """Run one worker job on a dedicated event loop (local development only).

    On Windows, `uvicorn --reload` forces the SelectorEventLoop, which cannot
    spawn subprocesses. Playwright launches its driver as a subprocess, so
    sharing uvicorn's loop fails with NotImplementedError. A thread with its own
    Proactor loop keeps `--reload` working and lets Playwright start.
    """
    from .worker import run_job

    policy = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if sys.platform == "win32" and policy is not None:
        asyncio.set_event_loop_policy(policy())
    try:
        asyncio.run(
            run_job(
                session_id=payload["sessionId"],
                action=payload["action"],
                message=payload.get("message", ""),
                index=payload.get("index"),
            )
        )
    except Exception:
        logger.exception("Local worker job failed")


def _invoke_worker(payload: Dict[str, Any]) -> None:
    if LOCAL_WORKER:
        threading.Thread(
            target=_run_local_job, args=(payload,), daemon=True
        ).start()
        return

    if not WORKER_FUNCTION_NAME:
        raise RuntimeError("WORKER_FUNCTION_NAME is not configured")

    response = _lambda().invoke(
        FunctionName=WORKER_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    if response.get("StatusCode") != 202:
        raise RuntimeError(
            f"worker invoke returned {response.get('StatusCode')}: "
            f"{response.get('FunctionError', '')}"
        )


def _get_or_404(session_id: str) -> Dict[str, Any]:
    item = store.get_session(session_id)
    if not item:
        raise HTTPException(status_code=404, detail="Session not found")
    return item


def _stale_running(item: Dict[str, Any]) -> bool:
    updated = int(item.get("updatedAt") or 0)
    return not updated or int(time.time()) - updated > JOB_STALE_SECONDS


def _reconcile_running(session_id: str) -> Dict[str, Any]:
    """Release a claim left behind by a worker that died before finishing."""
    item = _get_or_404(session_id)
    if item.get("jobStatus") == "running" and _stale_running(item):
        store.fail_job(session_id, "The previous job timed out. Please try again.")
        return _get_or_404(session_id)
    return item


def _begin_job(
    session_id: str,
    action: str,
    message: str = "",
    index: int | None = None,
    fields: Dict[str, Any] | None = None,
) -> None:
    if not store.claim_job(session_id):
        raise HTTPException(
            status_code=409,
            detail="The agent is still working on this session. Wait for it to finish.",
        )
    try:
        if fields:
            store.update_session(session_id, **fields)
        _invoke_worker(
            {
                "sessionId": session_id,
                "action": action,
                "message": message,
                "index": index,
            }
        )
    except Exception as exc:
        logger.exception("Could not start worker action %s", action)
        store.fail_job(session_id, f"Could not start the worker: {exc}")
        raise HTTPException(status_code=500, detail="Could not start the worker") from exc


@app.get("/")
def root() -> Dict[str, Any]:
    return {"service": "truthphone-api", "status": "ok"}


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "region": REGION, "model": MODEL_ID}


@app.post("/sessions")
def create_session() -> Dict[str, Any]:
    session_id = uuid.uuid4().hex
    item = store.create_session(session_id)
    return store.public_session(item)


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    return store.public_session(_reconcile_running(session_id))


@app.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, request: MessageRequest) -> Dict[str, Any]:
    item = _get_available(session_id)

    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty")

    phase = item.get("phase", "clarification")
    results = item.get("searchResults") or []

    # A bare number during selection is a phone choice, even without the button.
    if phase in ("selection", "comparison") and re.fullmatch(r"\d+[.)]?", message):
        index = int(re.match(r"\d+", message).group(0)) - 1
        if 0 <= index < len(results):
            return await select_phone(session_id, SelectRequest(index=index))
        raise HTTPException(
            status_code=400,
            detail=f"Choose a number between 1 and {len(results)}",
        )

    store.append_message(session_id, "user", message)

    if request.forceSearch:
        _begin_job(
            session_id,
            "search",
            message=message,
            fields={
                "phase": "search",
                "browserStatus": "starting",
                "liveViewUrl": "",
                "liveViewExpiresAt": 0,
                "error": "",
            },
        )
        action = "search"
    else:
        _begin_job(session_id, "chat", message=message)
        action = "chat"

    return {
        "queued": True,
        "action": action,
        "session": store.public_session(_get_or_404(session_id)),
    }


@app.post("/sessions/{session_id}/select")
async def select_phone(session_id: str, request: SelectRequest) -> Dict[str, Any]:
    item = _get_available(session_id)

    results: List[Dict[str, Any]] = item.get("searchResults") or []
    if not results:
        raise HTTPException(status_code=400, detail="No phones to select yet")
    if request.index >= len(results):
        raise HTTPException(
            status_code=400,
            detail=f"Choose a number between 1 and {len(results)}",
        )

    store.append_message(
        session_id, "user", f"Fact-check option {request.index + 1}."
    )
    _begin_job(
        session_id,
        "verify",
        index=request.index,
        fields={
            "phase": "verification",
            "selectedIndex": request.index,
            "browserStatus": "starting",
            "liveViewUrl": "",
            "liveViewExpiresAt": 0,
            "error": "",
            "claims": [],
            "verificationResults": [],
            "criticSynthesis": "",
            "finalReport": "",
        },
    )
    return {
        "queued": True,
        "action": "verify",
        "session": store.public_session(_get_or_404(session_id)),
    }


@app.post("/sessions/{session_id}/reset")
def reset_session(session_id: str) -> Dict[str, Any]:
    _get_available(session_id)
    item = store.reset_session(session_id)
    return store.public_session(item)


@app.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str) -> None:
    _get_available(session_id)
    store.delete_session(session_id)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    response = await call_next(request)
    if request.url.path != "/health":
        logger.info("%s %s -> %s", request.method, request.url.path, response.status_code)
    return response
