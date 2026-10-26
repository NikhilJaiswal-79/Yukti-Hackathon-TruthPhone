"""Pydantic models stored in DynamoDB and returned to the UI."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Phase = Literal[
    "clarification",
    "search",
    "comparison",
    "selection",
    "verification",
    "done",
    "error",
]
BrowserStatus = Literal["idle", "starting", "live", "closed", "error"]
JobStatus = Literal["idle", "running", "error"]


def _now() -> int:
    return int(time.time())


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant", "system"]
    content: str
    ts: int = Field(default_factory=_now)


class PhoneCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    price: str = ""
    specs: str = ""
    url: str = ""
    details: str = ""
    asin: str = ""


class VerificationClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claim: str
    context: str = ""


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claim: str
    is_verified: bool = False
    explanation: str = ""


class SessionState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sessionId: str
    messages: List[ChatMessage] = Field(default_factory=list)
    phase: Phase = "clarification"
    requirements: Dict[str, Any] = Field(default_factory=dict)
    searchResults: List[PhoneCandidate] = Field(default_factory=list)
    comparisonSummary: str = ""
    selectedIndex: Optional[int] = None
    claims: List[VerificationClaim] = Field(default_factory=list)
    verificationResults: List[VerificationResult] = Field(default_factory=list)
    criticSynthesis: str = ""
    finalReport: str = ""
    liveViewUrl: str = ""
    liveViewExpiresAt: int = 0
    browserStatus: BrowserStatus = "idle"
    jobStatus: JobStatus = "idle"
    error: str = ""
    createdAt: int = Field(default_factory=_now)
    updatedAt: int = Field(default_factory=_now)
    expiresAt: int = Field(default_factory=lambda: _now() + 86_400)
