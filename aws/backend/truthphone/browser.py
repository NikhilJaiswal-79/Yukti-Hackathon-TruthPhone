"""AgentCore Browser session + Playwright CDP connection.

Chrome runs inside AgentCore Browser. This process only holds the CDP client
and generates a short-lived live-view URL for the React UI.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from bedrock_agentcore.tools.browser_client import BrowserClient
from playwright.async_api import async_playwright

from . import store
from .config import BROWSER_SESSION_TIMEOUT, REGION

logger = logging.getLogger(__name__)

LIVE_VIEW_SECONDS = 300
REFRESH_EVERY_SECONDS = 240
REMOTE_WIDTH = 1440
REMOTE_HEIGHT = 900

# AgentCore Browser appends an "Amazon-Bedrock-AgentCore-Browser" product token
# to navigator.userAgent. Amazon.in treats that token as a bot and serves its
# 503 "rush hour" page on search, even though the home page loads. Present a
# normal desktop Chrome fingerprint instead. The locale and timezone must match
# the UA: a UA-only override still gets blocked by Amazon.
BROWSER_LOCALE = "en-IN"
BROWSER_TIMEZONE = "Asia/Kolkata"
BROWSER_ACCEPT_LANGUAGE = "en-IN,en;q=0.9"
FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def _clean_agentcore_user_agent(raw: str) -> str:
    """Remove AgentCore's bot-identifying product token from the UA."""
    cleaned = re.sub(
        r"\s+Amazon-Bedrock-AgentCore-Browser/\S+.*$", "", raw or ""
    ).strip()
    cleaned = cleaned.replace("HeadlessChrome", "Chrome")
    if "Chrome/" not in cleaned:
        return FALLBACK_USER_AGENT
    if "Safari/" not in cleaned:
        cleaned = f"{cleaned} Safari/537.36"
    return cleaned


async def _apply_amazon_safe_fingerprint(context, page) -> object | None:
    """Make the live-view tab look like a normal desktop Chrome in India."""
    try:
        raw_ua = await page.evaluate("navigator.userAgent")
        platform = await page.evaluate("navigator.platform") or "Linux x86_64"
        ua = _clean_agentcore_user_agent(raw_ua)
        cdp = await context.new_cdp_session(page)
        await cdp.send(
            "Network.setUserAgentOverride",
            {
                "userAgent": ua,
                "acceptLanguage": BROWSER_ACCEPT_LANGUAGE,
                "platform": platform,
            },
        )
        await cdp.send(
            "Emulation.setTimezoneOverride", {"timezoneId": BROWSER_TIMEZONE}
        )
        await cdp.send(
            "Emulation.setLocaleOverride", {"locale": BROWSER_LOCALE}
        )
        logger.info("Applied Amazon-safe browser fingerprint: %s", ua)
        return cdp
    except Exception:
        logger.exception("Could not apply Amazon-safe browser fingerprint")
        return None


async def _publish_live_view(client: BrowserClient, session_id: str) -> None:
    try:
        url = await asyncio.to_thread(
            client.generate_live_view_url, LIVE_VIEW_SECONDS
        )
        await asyncio.to_thread(
            store.set_live_view,
            session_id,
            url,
            int(time.time()) + LIVE_VIEW_SECONDS - 10,
        )
        logger.info("Published live view URL for %s", session_id[:8])
    except Exception:
        logger.exception("Could not publish live view URL")


@asynccontextmanager
async def open_browser(
    session_id: str,
) -> AsyncIterator[tuple[BrowserClient, object, object, object]]:
    """Start an AgentCore browser, publish live view, and yield Playwright objects."""
    client = BrowserClient(REGION)
    browser = None
    fingerprint_session = None
    stop_refresh = asyncio.Event()
    refresh_task = None

    try:
        await asyncio.to_thread(
            client.start,
            session_timeout_seconds=BROWSER_SESSION_TIMEOUT,
            viewport={"width": REMOTE_WIDTH, "height": REMOTE_HEIGHT},
            name=f"truthphone-{session_id[:8]}",
        )
        await _publish_live_view(client, session_id)

        async def refresher() -> None:
            while not stop_refresh.is_set():
                try:
                    await asyncio.wait_for(
                        stop_refresh.wait(), timeout=REFRESH_EVERY_SECONDS
                    )
                except asyncio.TimeoutError:
                    await _publish_live_view(client, session_id)

        refresh_task = asyncio.create_task(refresher())

        ws_url, headers = await asyncio.to_thread(client.generate_ws_headers)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(
                ws_url, headers=headers
            )
            context = (
                browser.contexts[0]
                if browser.contexts
                else await browser.new_context(
                    viewport={"width": REMOTE_WIDTH, "height": REMOTE_HEIGHT}
                )
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(30_000)
            fingerprint_session = await _apply_amazon_safe_fingerprint(
                context, page
            )
            logger.info("Connected Playwright to AgentCore session %s", client.session_id)
            yield client, browser, context, page
    finally:
        stop_refresh.set()
        if refresh_task:
            refresh_task.cancel()
            try:
                await refresh_task
            except (asyncio.CancelledError, Exception):
                pass

        if fingerprint_session is not None:
            try:
                await fingerprint_session.detach()
            except Exception:
                pass

        # Disconnect Playwright first (this may also close the remote browser),
        # then stop the AgentCore session so it cannot keep billing.
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass

        try:
            await asyncio.to_thread(client.stop)
        except Exception:
            logger.warning("Browser stop failed", exc_info=True)

        try:
            await asyncio.to_thread(
                store.update_session,
                session_id,
                browserStatus="closed",
                liveViewUrl="",
                liveViewExpiresAt=0,
            )
        except Exception:
            logger.warning("Could not clear live view state", exc_info=True)
