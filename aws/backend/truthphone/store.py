"""DynamoDB-backed session store.

The table is on-demand and has TTL enabled on `expiresAt`, so abandoned demo
sessions clean themselves up without a scheduled job.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel

from .config import MAX_MESSAGES, SESSION_TABLE, SESSION_TTL_SECONDS
from .models import ChatMessage, SessionState

logger = logging.getLogger(__name__)

_table = None


def _now() -> int:
    return int(time.time())


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(SESSION_TABLE)
    return _table


def _clean(value: Any) -> Any:
    """Convert Pydantic models to dicts, drop None values and fix floats.

    DynamoDB rejects Python floats, so any float (for example a judge
    confidence score) is stored as a Decimal.
    """
    if isinstance(value, BaseModel):
        value = value.model_dump()
    if isinstance(value, dict):
        return {
            key: _clean(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def create_session(session_id: str) -> Dict[str, Any]:
    now = _now()
    state = SessionState(
        sessionId=session_id,
        createdAt=now,
        updatedAt=now,
        expiresAt=now + SESSION_TTL_SECONDS,
    )
    item = _clean(state.model_dump())
    table().put_item(Item=item)
    return item


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    response = table().get_item(Key={"sessionId": session_id})
    return response.get("Item")


def update_session(session_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    fields = {key: _clean(value) for key, value in fields.items()}
    fields["updatedAt"] = _now()

    names: Dict[str, str] = {}
    values: Dict[str, Any] = {}
    sets = []
    removes = []

    for index, (key, value) in enumerate(fields.items()):
        name = f"#f{index}"
        names[name] = key
        if value is None:
            removes.append(name)
        else:
            token = f":v{index}"
            values[token] = value
            sets.append(f"{name} = {token}")

    expression = ""
    if sets:
        expression += "SET " + ", ".join(sets)
    if removes:
        expression += (" " if expression else "") + "REMOVE " + ", ".join(removes)

    kwargs: Dict[str, Any] = {
        "Key": {"sessionId": session_id},
        "UpdateExpression": expression,
        "ExpressionAttributeNames": names,
        "ConditionExpression": "attribute_exists(sessionId)",
    }
    if values:
        kwargs["ExpressionAttributeValues"] = values

    try:
        table().update_item(**kwargs)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return get_session(session_id)


def append_message(
    session_id: str, role: str, content: str, max_messages: int = MAX_MESSAGES
) -> None:
    item = get_session(session_id)
    if not item:
        return
    messages = list(item.get("messages") or [])
    messages.append(ChatMessage(role=role, content=content).model_dump())
    update_session(session_id, messages=messages[-max_messages:])


def claim_job(session_id: str) -> bool:
    """Atomically move a session from idle to running. Returns False if busy."""
    try:
        table().update_item(
            Key={"sessionId": session_id},
            UpdateExpression="SET jobStatus = :running, updatedAt = :now",
            ConditionExpression=(
                "attribute_exists(sessionId) AND "
                "(attribute_not_exists(jobStatus) OR jobStatus <> :running)"
            ),
            ExpressionAttributeValues={
                ":running": "running",
                ":now": _now(),
            },
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def finish_job(session_id: str, phase: Optional[str] = None) -> None:
    fields: Dict[str, Any] = {"jobStatus": "idle", "error": ""}
    if phase:
        fields["phase"] = phase
    update_session(session_id, **fields)


def fail_job(session_id: str, message: str) -> None:
    update_session(
        session_id,
        jobStatus="error",
        phase="error",
        error=str(message)[:1_500],
        browserStatus="error",
        liveViewUrl="",
        liveViewExpiresAt=0,
    )


def set_live_view(session_id: str, url: str, expires_at: int) -> None:
    update_session(
        session_id,
        liveViewUrl=url,
        liveViewExpiresAt=expires_at,
        browserStatus="live",
    )


def delete_session(session_id: str) -> None:
    table().delete_item(Key={"sessionId": session_id})


def reset_session(session_id: str) -> Dict[str, Any]:
    return create_session(session_id)


def public_session(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return a JSON-safe session payload for FastAPI."""
    return SessionState.model_validate(item).model_dump(mode="json")
