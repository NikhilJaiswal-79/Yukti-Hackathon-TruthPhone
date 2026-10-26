"""Worker Lambda: browser-driven search and verification."""

from __future__ import annotations

import logging
from typing import List, Optional

from . import agents, store
from .amazon import (
    _clean_query,
    _model_key,
    _read_product,
    duckduckgo_search,
    pick_query_message,
    search_amazon,
    search_flipkart,
    web_search_listings,
)
from .browser import open_browser
from .config import LOG_LEVEL, MAX_PHONES
from .models import PhoneCandidate, SessionState, VerificationResult

logging.basicConfig(level=LOG_LEVEL, force=True)
logger = logging.getLogger(__name__)


def _user_messages(state: SessionState) -> List[str]:
    return [message.content for message in state.messages if message.role == "user"]


def _merge_phones(*groups: List[PhoneCandidate]) -> List[PhoneCandidate]:
    merged: List[PhoneCandidate] = []
    seen = set()
    for group in groups:
        for phone in group:
            key = _model_key(phone.name) or phone.name.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(phone)
    return merged[:MAX_PHONES]


def _search_inputs(state: SessionState, message: str) -> tuple[str, str]:
    user_messages = _user_messages(state)
    query_message = pick_query_message(message, user_messages)

    # A short refinement such as "under 20,000" has no usable search text.
    # Fall back to the previous message that does, while keeping the refinement
    # first in the context so its budget/brand wins the parse.
    if not _clean_query(query_message):
        for candidate in reversed(user_messages[:-1]):
            if _clean_query(candidate):
                query_message = candidate
                break

    # Keep the latest message first so a new budget or brand wins the parse,
    # then add recent user turns for context that the latest message omits.
    tail = [query_message] + list(reversed(user_messages[:-1]))[:2]
    parts = [message]
    for part in tail:
        if part and part.strip() != message.strip() and part not in parts:
            parts.append(part)
    context = " ".join(parts).strip()
    return query_message, context


def _no_match_message(requirements: dict) -> str:
    if requirements.get("error") == "captcha":
        return (
            "Amazon blocked the search with a **CAPTCHA** and it was not solved "
            "before the browser session timed out.\n\n"
            "Try the search again. When the live browser shows the challenge, "
            "click and type directly in the live view on the right."
        )
    if requirements.get("error") == "search_failed":
        return (
            "I opened Amazon but could not reach the search results page. "
            "This is usually a temporary bot check or a slow page load.\n\n"
            "Try again; the live browser will show exactly what Amazon returned."
        )
    if requirements.get("error") == "blocked":
        return (
            "Amazon returned a **503 Service Unavailable** page to the "
            "automated browser. This is Amazon blocking datacenter traffic, "
            "not a CAPTCHA you can solve.\n\n"
            "Try again in a minute; Amazon sometimes lets a retry through. "
            "If it keeps happening, the demo needs a residential proxy or a "
            "different shopping source."
        )
    if requirements.get("error") == "no_cards":
        title = requirements.get("pageTitle") or "an unexpected page"
        return (
            f"Amazon returned **{title}** with no readable product cards. "
            "This is usually a bot check, a location interstitial, or a page "
            "layout change.\n\n"
            "Try the search again. If the live browser shows a challenge or a "
            "**Continue shopping** button, click it in the live view and resend."
        )
    budget = requirements.get("budget")
    brand = requirements.get("brand")
    limit_note = f"₹{budget:,}" if budget else "your stated criteria"
    reasons = []
    if budget:
        reasons.append(f"priced above ₹{budget:,}")
    if brand:
        reasons.append(f"not from {brand}")
    why = " or ".join(reasons) if reasons else "not a phone match"
    suggestion = (
        f"try ₹{format(int(budget * 1.15), ',')}"
        if isinstance(budget, int) and budget
        else "a higher amount"
    )
    return (
        f"I searched Amazon and the web fallback but **no phones matched {limit_note}**.\n\n"
        f"Every listing was {why}, or was an accessory (case, charger, earphones).\n\n"
        "**What you can do:**\n"
        f"- Raise your budget a little (for example, {suggestion})\n"
        "- Drop the brand filter so I can show alternatives\n"
        "- Switch on **Live shopping search** and send a new request"
    )


def _fallback_comparison(phones: List) -> str:
    lines = ["I found these phones:"]
    for index, phone in enumerate(phones, 1):
        lines.append(
            f"{index}. **{phone.name}** — {phone.price}\n"
            f"   {phone.specs}\n"
            f"   {phone.url}"
        )
    lines.append("Reply with a number, or click **Fact-check this** on a card.")
    return "\n\n".join(lines)


def _fallback_report(phone, results: List[VerificationResult]) -> str:
    lines = [f"**TruthPhone report: {phone.name}**", ""]
    for result in results:
        status = "Verified" if result.is_verified else "Potentially misleading"
        lines.append(f"- **{result.claim}** — {status}. {result.explanation}")
    checked = len(results)
    verified = sum(1 for result in results if result.is_verified)
    lines.append("")
    if checked and verified == checked:
        lines.append("**Recommendation: Buy.** All checked claims held up.")
    elif verified:
        lines.append(
            "**Recommendation: Proceed with caution.** Some checked claims did not hold up."
        )
    else:
        lines.append(
            "**Recommendation: Avoid or verify further.** None of the checked claims were confirmed."
        )
    return "\n".join(lines)


async def _chat(session_id: str, state: SessionState, message: str) -> None:
    history = state.messages
    if history and history[-1].content == message:
        history = history[:-1]
    text = await agents.clarify(history, message)
    store.append_message(session_id, "assistant", text)


async def _search(session_id: str, state: SessionState, message: str) -> None:
    query_message, context = _search_inputs(state, message)
    await _set(
        session_id,
        phase="search",
        browserStatus="starting",
        liveViewUrl="",
        liveViewExpiresAt=0,
        error="",
    )

    async def on_status(text: str) -> None:
        store.append_message(session_id, "assistant", text)

    async with open_browser(session_id) as (_client, _browser, _context, page):
        store.update_session(session_id, browserStatus="live")
        phones, requirements = await search_amazon(
            page, query_message, context, on_status
        )

        if len(phones) < MAX_PHONES:
            reason = requirements.get("error")
            if on_status:
                if reason == "blocked":
                    await on_status(
                        "Amazon blocked the automated browser with a **503**. "
                        "I'm trying Flipkart next; the live view shows it."
                    )
                elif not phones:
                    await on_status(
                        "Amazon returned no usable listings. Trying Flipkart."
                    )
                else:
                    await on_status(
                        "Amazon returned one match. Checking Flipkart for "
                        "another option."
                    )
            flipkart_phones = await search_flipkart(
                page,
                requirements.get("query") or query_message,
                requirements.get("budget"),
                requirements.get("brand"),
            )
            if flipkart_phones:
                had_phones = bool(phones)
                phones = _merge_phones(phones, flipkart_phones)
                requirements["found"] = len(phones)
                requirements["source"] = "mixed" if had_phones else "flipkart"
                requirements.pop("error", None)
                requirements.pop("pageTitle", None)

        if len(phones) < MAX_PHONES:
            if on_status:
                await on_status(
                    "Trying a DuckDuckGo/Bing search for another option."
                )
            web_phones = await web_search_listings(
                page,
                requirements.get("query") or query_message,
                requirements.get("budget"),
                requirements.get("brand"),
            )
            if web_phones:
                had_phones = bool(phones)
                phones = _merge_phones(phones, web_phones)
                requirements["found"] = len(phones)
                requirements["source"] = "mixed" if had_phones else "web"
                requirements.pop("error", None)
                requirements.pop("pageTitle", None)

        if not phones:
            text = _no_match_message(requirements)
            store.update_session(
                session_id,
                phase="clarification",
                searchResults=[],
                comparisonSummary="",
                requirements=requirements,
                selectedIndex=None,
                claims=[],
                verificationResults=[],
                criticSynthesis="",
                finalReport="",
                browserStatus="closed",
                liveViewUrl="",
                liveViewExpiresAt=0,
            )
            store.append_message(session_id, "assistant", text)
            return

        phones = await agents.tidy_listings(
            requirements.get("query", ""),
            requirements.get("budget"),
            phones,
        )
        try:
            comparison = await agents.compare(requirements.get("query", ""), phones)
        except Exception:
            logger.warning("Comparison agent failed; using fallback", exc_info=True)
            comparison = _fallback_comparison(phones)

        store.update_session(
            session_id,
            phase="selection",
            requirements=requirements,
            searchResults=[phone.model_dump() for phone in phones],
            comparisonSummary=comparison,
            selectedIndex=None,
            claims=[],
            verificationResults=[],
            criticSynthesis="",
            finalReport="",
        )
        store.append_message(session_id, "assistant", comparison)

    store.update_session(
        session_id,
        browserStatus="closed",
        liveViewUrl="",
        liveViewExpiresAt=0,
    )


async def _verify(
    session_id: str, state: SessionState, index: Optional[int]
) -> None:
    if index is None:
        index = state.selectedIndex
    if index is None or not (0 <= index < len(state.searchResults)):
        raise ValueError("A phone must be selected before verification")

    phone = state.searchResults[index]
    if not phone.url or phone.url == "N/A":
        raise ValueError("The selected phone has no product URL")

    await _set(
        session_id,
        phase="verification",
        selectedIndex=index,
        browserStatus="starting",
        liveViewUrl="",
        liveViewExpiresAt=0,
        error="",
        claims=[],
        verificationResults=[],
        criticSynthesis="",
        finalReport="",
    )

    async def on_status(text: str) -> None:
        store.append_message(session_id, "assistant", text)

    async with open_browser(session_id) as (_client, _browser, _context, page):
        store.update_session(session_id, browserStatus="live")
        details = await _read_product(page, phone.url, on_status)
        if details:
            phone.details = details
            updated = list(state.searchResults)
            updated[index] = phone
            store.update_session(session_id, searchResults=updated)

        claims = await agents.plan_claims(phone)
        store.update_session(session_id, claims=[claim.model_dump() for claim in claims])

        results: List[VerificationResult] = []
        for claim in claims:
            raw = await duckduckgo_search(
                page, f"{phone.name} {claim.claim} specs review"
            )
            if len(raw) < 100:
                result = VerificationResult(
                    claim=claim.claim,
                    is_verified=False,
                    explanation="Could not find independent search results.",
                )
            else:
                result = await agents.verdict(phone, claim.claim, raw)
            results.append(result)
            store.update_session(
                session_id,
                verificationResults=[item.model_dump() for item in results],
            )

        critic_text = "Critic synthesis unavailable."
        try:
            critic_text = await agents.critic(results)
        except Exception:
            logger.warning("Critic agent failed; using fallback", exc_info=True)

        try:
            report = await agents.finalize(phone, results, critic_text)
        except Exception:
            logger.warning("Finalizer failed; using fallback report", exc_info=True)
            report = _fallback_report(phone, results)

        store.update_session(
            session_id,
            claims=[claim.model_dump() for claim in claims],
            verificationResults=[item.model_dump() for item in results],
            criticSynthesis=critic_text,
            finalReport=report,
            phase="done",
        )

    store.update_session(
        session_id,
        browserStatus="closed",
        liveViewUrl="",
        liveViewExpiresAt=0,
    )


async def _set(session_id: str, **fields) -> None:
    store.update_session(session_id, **fields)


async def run_job(
    session_id: str,
    action: str,
    message: str = "",
    index: Optional[int] = None,
) -> None:
    item = store.get_session(session_id)
    if not item:
        logger.warning("Session %s no longer exists", session_id)
        return

    state = SessionState.model_validate(item)
    logger.info("Worker action=%s session=%s", action, session_id[:8])

    try:
        if action == "chat":
            await _chat(session_id, state, message)
        elif action == "search":
            await _search(session_id, state, message)
        elif action == "verify":
            await _verify(session_id, state, index)
        else:
            raise ValueError(f"Unknown action: {action}")
        store.finish_job(session_id)
    except Exception as exc:
        logger.exception("Worker failed for %s", session_id)
        store.fail_job(session_id, str(exc))
        raise
