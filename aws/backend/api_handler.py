"""Lambda entrypoint for the FastAPI API function."""

from __future__ import annotations

from mangum import Mangum

from truthphone.api import app

handler = Mangum(app, lifespan="off")
