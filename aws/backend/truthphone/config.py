"""Configuration shared by the API and worker Lambdas."""

from __future__ import annotations

import os


def _load_env_file() -> None:
    """Load aws/.env for local development without adding python-dotenv."""
    here = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.abspath(os.path.join(here, "..", "..", ".env"))
    if not os.path.exists(env_file):
        return
    try:
        with open(env_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


_load_env_file()

REGION = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "ap-south-1"
)
MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "in.anthropic.claude-haiku-4-5-20251001-v1:0"
)
# Claim extraction, judging and the final report use the strongest Sonnet the
# account can invoke. The global profile is the only Sonnet 4.6 route available
# in ap-south-1 today; Sonnet 5.x needs Bedrock model access first.
VERIFY_MODEL_ID = os.environ.get(
    "VERIFY_MODEL_ID", "global.anthropic.claude-sonnet-4-6"
)
SESSION_TABLE = os.environ.get("SESSION_TABLE", "truthphone-sessions")
WORKER_FUNCTION_NAME = os.environ.get("WORKER_FUNCTION_NAME", "")
SESSION_TTL_SECONDS = _int("SESSION_TTL_SECONDS", 86_400)
BROWSER_SESSION_TIMEOUT = _int("BROWSER_SESSION_TIMEOUT", 600)
MAX_PHONES = _int("MAX_PHONES", 2)
MAX_CLAIMS = _int("MAX_CLAIMS", 12)
MAX_MESSAGES = _int("MAX_MESSAGES", 40)
MAX_DETAILS_CHARS = _int("MAX_DETAILS_CHARS", 9_000)
MAX_EVIDENCE_CHARS = _int("MAX_EVIDENCE_CHARS", 16_000)
LOCAL_WORKER = os.environ.get("LOCAL_WORKER", "0") == "1"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]
