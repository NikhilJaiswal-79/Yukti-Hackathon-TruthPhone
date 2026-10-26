import os
import sys
import re
import json
import random
import asyncio
import traceback
import urllib.parse
import logging
from typing import List, Dict, Any

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_aws import ChatBedrock
from langgraph.graph import StateGraph, START, END

# ── Windows console encoding (cp1252 can't handle LLM emoji) ───────────────
os.environ["PYTHONIOENCODING"] = "utf-8"
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class UTF8StreamHandler(logging.StreamHandler):
    def __init__(self, stream=None):
        super().__init__(stream or sys.stdout)

    def emit(self, record):
        try:
            self.stream.write(self.format(record) + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)


logging.basicConfig(level=logging.INFO, handlers=[UTF8StreamHandler()])

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from state import AgentState, PhoneCandidate, VerificationClaim, VerificationResult


# ── Load config from local .env ────────────────────────────────────────────
def _load_env_file():
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_file):
        return
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env_file()

BEDROCK_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID", "anthropic.claude-haiku-4-5-20251001-v1:0"
)
BEDROCK_REGION = os.getenv("AWS_DEFAULT_REGION", os.getenv("AWS_REGION", "us-east-1"))

# ── LangChain Bedrock client (conversation, comparison, critic, finalizer) ──
llm = ChatBedrock(
    model_id=BEDROCK_MODEL_ID,
    region_name=BEDROCK_REGION,
    provider="anthropic",
    temperature=0.3,
    model_kwargs={"max_tokens": 4096},
)
llm_structured = ChatBedrock(
    model_id=BEDROCK_MODEL_ID,
    region_name=BEDROCK_REGION,
    provider="anthropic",
    temperature=0.0,
    model_kwargs={"max_tokens": 4096},
)


def extract_text(content) -> str:
    """Safely extract plain text from any response content format."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and "text" in p:
                parts.append(p["text"])
        return " ".join(parts)
    return str(content)


# ══════════════════════════════════════════════════════════════════════════════
#  Stagehand (v4) browser agent, driven by Bedrock Claude Haiku 4.5
# ══════════════════════════════════════════════════════════════════════════════
import boto3
import stagehand
from pydantic import BaseModel, RootModel
from stagehand import Stagehand
from stagehand._generated.models import (
    LLMRole,
    LLMTextContent,
    LLMUsage,
    FieldSchema8,
    LLMStructuredGenerateResult,
)

_bedrock_runtime = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)


def _to_plain(o):
    """Recursively unwrap pydantic RootModel wrappers into plain JSON values."""
    if isinstance(o, RootModel):
        return _to_plain(o.root)
    if isinstance(o, BaseModel):
        return {k: _to_plain(v) for k, v in o.__dict__.items()}
    if isinstance(o, dict):
        return {k: _to_plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_to_plain(v) for v in o]
    return o


def _flatten_messages(messages) -> str:
    """Render Stagehand's message list into a single prompt string for Bedrock."""
    parts = []
    for msg in messages:
        role = getattr(msg.role, "value", msg.role)
        content = msg.content
        blocks = content if isinstance(content, list) else [content]
        texts = []
        for b in blocks:
            b = getattr(b, "root", b)
            btype = getattr(b, "type", None)
            if btype == "text":
                texts.append(b.text)
            elif btype == "image":
                texts.append("[image content omitted]")
            else:
                texts.append(json.dumps(_to_plain(b), default=str)[:6000])
        parts.append(f"[{role}]:\n" + "\n".join(texts))
    return "\n\n".join(parts)


def _parse_json_loose(text: str):
    """Pull the first JSON object out of a model response."""
    s = text.strip()
    if s.startswith("```"):
        parts = s.split("```")
        s = parts[1] if len(parts) > 1 else s
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        s = s[start:end + 1]
    return json.loads(s)


async def bedrock_llm_callback(params):
    """Stagehand's brain: route every LLM decision to Bedrock Claude Haiku 4.5.

    Stagehand v4 passes this the params object already unwrapped, so we use
    getattr(..., 'root', params) to stay compatible with both shapes.
    """
    root = getattr(params, "root", params)

    prompt = _flatten_messages(root.messages)
    if root.system_prompt:
        prompt = f"{root.system_prompt}\n\n---\n{prompt}"

    rf = root.response_format
    if rf is not None and rf.schema_ is not None:
        schema = _to_plain(rf.schema_)
        prompt += (
            "\n\n---\nSTRICT OUTPUT REQUIREMENT:\n"
            "Respond with a single JSON object and nothing else. "
            "No markdown fences, no prose, no explanation.\n"
            "It MUST validate against this JSON schema:\n"
            f"{json.dumps(schema, indent=2)}\n"
            f"{(rf.description or '').strip()}"
        )

    resp = _bedrock_runtime.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={
            "maxTokens": 4096,
            "temperature": root.temperature if root.temperature is not None else 0.2,
        },
    )
    text = resp["output"]["message"]["content"][0]["text"]
    usage = resp.get("usage", {})

    structured = None
    if rf is not None and rf.schema_ is not None:
        try:
            structured = FieldSchema8(root=_parse_json_loose(text))
        except Exception as e:
            print(f"[stagehand-callback] JSON parse failed: {e}")
            print(f"[stagehand-callback] raw: {text[:600]}")

    return LLMStructuredGenerateResult(
        role=LLMRole.assistant,
        content=LLMTextContent(type="text", text=text),
        stop_reason="end_turn",
        usage=LLMUsage(
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
            total_tokens=usage.get("totalTokens", 0),
        ),
        output_format="json_schema",
        structured_content=structured,
    )


# ── Shared Stagehand session (launched once, reused across requests) ───────
# A single long-lived tab is kept open for the whole session. If every tab were
# closed after a task, Chrome would quit and the user would never see it browse.
_stagehand_bundle = None
_shared_page = None
_session_lock = asyncio.Lock()


async def get_stagehand():
    """Return (stagehand, browser), launching Chrome + Stagehand on first use.

    Self-heals: if the user closed the Chrome window the CDP link dies, so we
    detect that and relaunch instead of failing every future request.
    """
    global _stagehand_bundle, _shared_page
    if _stagehand_bundle is not None:
        _sh, browser = _stagehand_bundle
        if not getattr(browser, "closed", False):
            return _stagehand_bundle
        print("⚠️  Chrome window was closed — relaunching Stagehand...")
        _stagehand_bundle = None
        _shared_page = None
    async with _session_lock:
        if _stagehand_bundle is not None:
            return _stagehand_bundle
        loop = asyncio.get_running_loop()
        if not hasattr(loop, "_make_subprocess_transport") or \
                loop._make_subprocess_transport.__qualname__.startswith("Selector"):
            raise RuntimeError(
                "The running asyncio event loop cannot spawn subprocesses, so "
                "Stagehand cannot launch Chrome. On Windows this happens when "
                "uvicorn runs with reload=True (it forces a SelectorEventLoop). "
                "Start the server with reload=False."
            )
        print("🌐 Launching Stagehand Chrome (Bedrock Claude Haiku 4.5 brain)...")
        browser = await stagehand.local_browser.launch(
            headless=False,
            viewport_width=1440,
            viewport_height=900,
            args=[
                "--window-position=40,40",
                "--new-window",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--disable-session-crashed-bubble",
            ],
        )
        sh = await Stagehand.create(browser=browser, model=bedrock_llm_callback)
        print("✅ Stagehand ready.")
        _stagehand_bundle = (sh, browser)
    return _stagehand_bundle


async def get_page():
    """Return (stagehand, page) using one persistent tab so Chrome stays visible."""
    global _shared_page
    sh, browser = await get_stagehand()
    if _shared_page is None:
        _shared_page = await browser.context.new_page()
        print("👁️  Chrome tab ready — the browser will stay visible while browsing.")
    return sh, _shared_page


async def reset_page():
    """Drop the shared tab after a failure so the next call gets a clean one."""
    global _shared_page
    if _shared_page is not None:
        try:
            await _shared_page.close()
        except Exception:
            pass
        _shared_page = None


async def invalidate_stagehand():
    """Mark the whole session dead so the next request relaunches Chrome.

    Used when the CDP/RPC link drops (typically because the user closed Chrome).
    """
    global _stagehand_bundle, _shared_page
    _shared_page = None
    _stagehand_bundle = None
    print("⚠️  Stagehand session invalidated — Chrome will relaunch on next search.")


async def shutdown_stagehand():
    global _stagehand_bundle, _shared_page
    if _stagehand_bundle is not None:
        sh, _browser = _stagehand_bundle
        _stagehand_bundle = None
        _shared_page = None
        try:
            await sh.close()
        except Exception:
            pass


# ── Deterministic DOM harvesters (real URLs + prices, no LLM guessing) ─────
# NOTE: Stagehand's evaluate() requires an INVOKED IIFE: "(() => {...})()".
_HARVEST_PRODUCTS_JS = r"""
(() => {
  const cards = Array.from(
    document.querySelectorAll('div[data-asin]:not([data-asin=""])')
  );
  const out = [];
  const seen = new Set();
  for (const c of cards) {
    const asin = (c.getAttribute('data-asin') || '').trim();
    if (!asin || seen.has(asin)) continue;

    // Title link for this exact card (not the image link, not an ad)
    const link =
      c.querySelector('h2 a[href*="/dp/"]') ||
      c.querySelector('a.a-link-normal.a-text-normal[href*="/dp/"]') ||
      c.querySelector('a.a-link-normal[href*="/dp/"]');
    if (!link || !link.href) continue;

    const href = link.href.split('?')[0];
    const m = href.match(/\/dp\/([A-Z0-9]{10})/);
    const hrefAsin = m ? m[1] : '';
    if (hrefAsin !== asin) continue;   // link points at a different variant

    seen.add(asin);
    const priceEl = c.querySelector('.a-price > span.a-offscreen');
    out.push({
      asin: asin,
      title: link.innerText.trim().replace(/\s+/g, ' '),
      price: priceEl ? priceEl.innerText.trim() : '',
      text: c.innerText.trim().replace(/\s+/g, ' ').slice(0, 700),
      url: 'https://www.amazon.in/dp/' + asin
    });
  }
  return out;
})()
"""

_HARVEST_PRODUCT_PAGE_JS = r"""
(() => {
  const t = (sel) => { const e = document.querySelector(sel); return e ? e.innerText.trim() : ''; };
  const many = (sel) => Array.from(document.querySelectorAll(sel))
      .map(e => e.innerText.trim().replace(/\s+/g, ' '))
      .filter(s => s && s.length > 1);
  return {
    title: t('#productTitle'),
    brand: t('#bylineInfo'),
    price: t('#corePrice_feature_div .a-offscreen')
        || t('#corePriceDisplay_desktop_feature_div .a-offscreen')
        || t('#priceblock_ourprice'),
    mrp: t('#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen')
        || t('#basisPrice .a-offscreen'),
    saving: t('#corePriceDisplay_desktop_feature_div .savingsPercentage') || '',
    rating: t('#acrPopover .a-icon-alt') || t('[data-hook="rating-out-of-text"]'),
    reviews: t('#acrCustomerReviewText'),
    delivery: t('#mir-layout-DELIVERY_BLOCK')
          || t('#delivery-block-ubb')
          || t('#delivery-block-message-buy-box')
          || t('#availability'),
    soldBy: t('#sellerProfileTriggerId') || t('#tabular-buybox .tabular-buybox-text'),
    bullets: t('#feature-bullets'),
    highlights: many('#productHighlights_feature_div li'),
    specs: many('#productOverview_feature_div tr, #technicalSpecifications_section_1 tr, '
              + '#productDetails_techSpec_section_1 tr, #productDetails_techSpec_section_2 tr, '
              + '#prodDetails tr, #technicalSpecifications_section_1 li'),
    offers: many('#tabular-buybox .tabular-buybox-text, #buybox .tabular-buybox-text'),
    warranty: t('#warranty_feature_div') || t('#sellerProfileTriggerId'),
    description: t('#productDescription').slice(0, 2500),
    alsoBought: many('#sims-consolidated-1_feature_div .a-carousel-card, #similarities_feature_div h2')
  };
})()
"""


def _merge_harvest(acc: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the richest value seen for each field as we scroll and re-read."""
    for k, v in (new or {}).items():
        if not v:
            continue
        if isinstance(v, list):
            merged = list(acc.get(k) or [])
            seen = set(merged)
            for item in v:
                if item not in seen:
                    merged.append(item)
                    seen.add(item)
            acc[k] = merged
        else:
            cur = acc.get(k) or ""
            if len(str(v)) > len(str(cur)):
                acc[k] = v
    return acc


def _format_details(h: Dict[str, Any]) -> str:
    """Render the harvested product page into readable plain text."""
    if not h:
        return ""
    out = []
    label_map = [
        ("brand", "Brand"), ("price", "Price"), ("mrp", "M.R.P"),
        ("saving", "Discount"), ("rating", "Rating"), ("reviews", "Reviews"),
        ("delivery", "Delivery"), ("soldBy", "Sold by"), ("warranty", "Warranty"),
        ("offers", "Offers"), ("highlights", "Highlights"),
    ]
    for key, label in label_map:
        val = h.get(key)
        if not val:
            continue
        if isinstance(val, list):
            val = " | ".join(str(v) for v in val[:6])
        out.append(f"{label}: {str(val)[:400]}")

    bullets = h.get("bullets")
    if bullets:
        out.append("Key Features:\n" + str(bullets)[:1500])

    specs = h.get("specs")
    if specs:
        rows = [str(s) for s in specs[:70]]
        out.append("Technical Specifications:\n" + "\n".join(rows)[:2500])

    desc = h.get("description")
    if desc:
        out.append("Description:\n" + str(desc)[:1200])

    return "\n\n".join(out)


def _clean_specs(raw: str) -> str:
    """Trim harvested card text down to a readable spec blurb."""
    s = re.sub(r"\s+", " ", raw or "").strip()
    s = re.sub(r"^Save extra with No Cost EMI\s*", "", s)
    s = re.sub(r"FREE delivery.*$", "", s).strip()
    s = re.sub(r"\b\d+\.\d+\s+out of 5 stars.*$", "", s).strip()
    s = re.sub(r"\b[\d,]+ ratings?\b", "", s).strip()
    return s[:320].strip()


def _clean_query(raw: str) -> str:
    """Turn a chatty message into a clean Amazon search query."""
    q = (raw or "").strip()
    # Drop conversational lead-ins like "search for", "find me", "show me", etc.
    q = re.sub(
        r"^\s*(please\s+)?(can you\s+|could you\s+)?"
        r"(search for|search|find me|find|show me|show|look for|get me|get|"
        r"i want|i need|i'm looking for|im looking for|looking for)\b[:\s]*",
        "", q, flags=re.I,
    ).strip()
    # Strip trailing requests that confuse Amazon's search box
    q = re.sub(
        r"\b(from\s+(amazon|flipkart)[a-z.]*|on\s+(amazon|flipkart)[a-z.]*|"
        r"in\s+india|please)\b\s*\.?$",
        "", q, flags=re.I,
    ).strip(" .,?!")
    return q or "smartphones"


def _price_to_int(price: str):
    """'₹19,999' -> 19999 (returns None when there is no number)."""
    if not price:
        return None
    m = re.search(r"[\d][\d,]*", str(price))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


_BUDGET_UNITS = {"k": 1_000, "thousand": 1_000, "lakh": 100_000,
                 "lac": 100_000, "lakhs": 100_000, "crore": 10_000_000}


def _scale(number: str, unit: str = None) -> int:
    """'20','k' -> 20000 | '1.5','lakh' -> 150000 | '25000',None -> 25000"""
    try:
        val = float(number)
    except (TypeError, ValueError):
        return 0
    if unit:
        val *= _BUDGET_UNITS.get(unit.lower(), 1)
    return int(val)


def _parse_budget(text: str):
    """Find the user's maximum budget. Handles 25000 / 20k / 1.5 lakh / Rs.20000."""
    t = (text or "").lower().replace(",", "")
    patterns = [
        r"(?:under|below|less than|within|up ?to|upto|max(?:imum)?|budget of|budget|"
        r"lesser than|cheaper than|around|about|approximately)\s*(?:rs\.?|₹)?\s*"
        r"(\d+(?:\.\d+)?)\s*(k|thousand|lakhs?|lacs?|crore)?",
        r"(?:₹|rs\.?)\s*(\d+(?:\.\d+)?)\s*(k|thousand|lakhs?|lacs?|crore)?",
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            val = _scale(m.group(1), m.group(2))
            if val:
                return val

    # Fallback: a bare 4+ digit number reads as a budget (e.g. "iphone 99999").
    m = re.search(r"(?<!\d)(\d{4,7})(?!\d)", t)
    if m:
        return int(m.group(1))
    return None


_KNOWN_BRANDS = [
    "samsung", "redmi", "xiaomi", "oneplus", "realme", "vivo", "oppo", "moto",
    "motorola", "nokia", "iphone", "apple", "nothing", "tecno", "infinix",
    "iqoo", "honor", "asus", "sony", "lg",
]


def _parse_brand(text: str):
    """Return the phone brand the user asked for, if any."""
    t = (text or "").lower()
    for b in _KNOWN_BRANDS:
        if re.search(rf"\b{re.escape(b)}\b", t):
            return b
    return None


_BRAND_ALIASES = {
    "samsung": ["samsung", "galaxy"],
    "redmi": ["redmi", "xiaomi"],
    "xiaomi": ["xiaomi", "redmi"],
    "oneplus": ["oneplus"],
    "motorola": ["motorola", "moto"],
    "moto": ["moto", "motorola"],
    "apple": ["apple", "iphone"],
    "iphone": ["iphone", "apple"],
}


def _brand_matches(brand: str, *texts: str) -> bool:
    """Amazon card titles often omit the brand ('Galaxy M17e' for Samsung),
    so match against the card text too and honour common aliases."""
    if not brand:
        return True
    aliases = _BRAND_ALIASES.get(brand, [brand])
    blob = " ".join(t or "" for t in texts).lower()
    return any(re.search(rf"\b{re.escape(a)}\b", blob) for a in aliases)


_COLOUR_WORDS = (
    r"black|white|blue|green|red|pink|gold|silver|violet|grey|gray|mint|navy|beige|"
    r"purple|orange|yellow|maroon|charcoal|lilac|coral|magenta|teal|"
    r"pop|lit|rogue|spotlight|blitz|vibe|sapphire|moonlight|awesome|limelight|"
    r"midnight|neon|ice|edition"
)


def _model_key(title: str) -> str:
    """Collapse a listing title down to its model identity.

    'Galaxy F36 5G (Violet, 128 GB) (8 GB RAM)' and
    'Galaxy F36 5G (Black, 8GB RAM, 128GB Storage)' both become 'galaxy f36 5g',
    so colour/RAM variants of one phone are not listed twice.
    """
    t = (title or "").lower()
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"\b\d+\s?(gb|tb|mb)\b", " ", t)
    t = re.sub(r"\b\d{3,5}\s?mah\b", " ", t)
    t = re.sub(r"\b\d+(\.\d+)?\s?(inch|inches|in)\b", " ", t)
    t = re.sub(rf"\b({_COLOUR_WORDS})\b", " ", t)
    t = re.sub(r"\b(ram|rom|storage|internal|mobile|phone|smartphone|with|and|"
               r"selfie|camera|display|battery|wifi|bluetooth|model)\b", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_ACCESSORY_RE = re.compile(
    r"\b(case|cases|cover|covers|screen guard|tempered|charger|adaptor|adapter|cable|"
    r"power ?bank|earphone|earphones|earbud|earbuds|headphone|headphones|headset|"
    r"strap|mount|skin|sticker|protector|wallet|sleeve|pouch|holder|stand|tripod|"
    r"selfie|ring|pop socket|lens|glass|back cover|flip cover|screen protector|"
    r"phone cover|holster|armband|lanyard)\b", re.I,
)

_PHONE_HINT_RE = re.compile(
    r"\b(5g|4g|\d+\s?gb|\d+\s?tb|mah|amoled|oled|ips\s?lcd|"
    r"helio|dimensity|snapdragon|exynos|unisoc|kirin|octorcore|octa core|"
    r"ram|storage|dual sim|android)\b", re.I,
)


def _looks_like_phone(title: str, text: str = "") -> bool:
    """Reject accessories so we never open a phone case by mistake."""
    if not title:
        return False
    if _ACCESSORY_RE.search(title):
        return False
    return bool(_PHONE_HINT_RE.search(title) or _PHONE_HINT_RE.search(text or ""))


# ── Human-like browser behaviour ───────────────────────────────────────────
async def _human_type(page, text: str, per_char_ms: int = 45):
    """Type like a person: fast, one character at a time, no typos."""
    print(f"⌨️  Typing: '{text}'")
    await page.type(text, delay=per_char_ms, with_mistakes=False)
    await page.wait_for_timeout(350)


async def _smooth_scroll(page, delta: int):
    """Scroll with real smooth animation.

    page.scroll() can time out on heavy Amazon pages, whereas a smooth
    scrollBy animation is reliable and looks more human while it happens.
    """
    d = delta + random.randint(-90, 170)
    try:
        await page.evaluate(
            f"window.scrollBy({{top: {d}, left: 0, behavior: 'smooth'}});"
        )
    except Exception:
        try:
            await page.evaluate(f"window.scrollBy(0, {d});")
        except Exception:
            pass


async def _human_scroll(page, steps: int = 4, delta: int = 620,
                        pause_ms=(600, 1100)):
    """Scroll the page in visible, unhurried steps."""
    for _ in range(steps):
        await _smooth_scroll(page, delta)
        await page.wait_for_timeout(random.randint(*pause_ms))


def _price_from_text(raw: str) -> str:
    """Pull a rupee price out of harvested card text."""
    m = re.search(r"(?:₹|\bRs\.?\s?)\s?[\d,]+", raw or "")
    if not m:
        return ""
    return m.group(0).replace(" ", "").replace("Rs.", "₹").replace("Rs", "₹")


# ── Agent 1: Conversation / Clarification ──────────────────────────────────
async def conversation_node(state: AgentState):
    """Manages dialogue state, asks clarifying questions one-by-one."""
    system_prompt = """You are the Conversation Agent for TruthPhone, a guided smartphone buying and fact-check assistant.
Your job is to clarify what smartphone the user wants.
You ideally want to know: budget range, brand preference, use case, and 4G vs 5G.

Rules:
- Ask questions ONE at a time.
- NEVER ask the same question twice. NEVER ask for budget if it was already mentioned.
- Do NOT output any system commands, function calls, tool calls, or JSON. YOU MUST RESPOND ONLY IN PLAIN CONVERSATIONAL TEXT.
- The user will manually initiate the search when they are ready. Just converse patiently."""

    messages = [SystemMessage(content=system_prompt)] + state["messages"]
    response = await llm.ainvoke(messages)
    text = extract_text(response.content)

    return {
        "messages": [AIMessage(content=text)],
        "current_phase": "clarification",
    }


# ── Agent 2: Search (Stagehand + Bedrock extraction) ───────────────────────
MAX_PHONES = 2


async def search_node(state: AgentState):
    """Stagehand browses Amazon like a person, then reads each matching phone."""
    raw_message = ""
    for msg in reversed(state["messages"]):
        if type(msg).__name__ == "HumanMessage" or msg.type == "human":
            raw_message = extract_text(msg.content)
            break

    search_query = _clean_query(raw_message or "")
    budget = _parse_budget(raw_message)
    brand = _parse_brand(raw_message)
    print(f"\n🔍 Query   : '{search_query}'")
    print(f"💰 Budget  : {'₹' + format(budget, ',') if budget else 'not specified'}")
    print(f"🏷️  Brand  : {brand or 'any'}")

    harvested: List[Dict[str, str]] = []
    search_url = ""

    try:
        sh, page = await get_page()
        try:
            # ── Land on Amazon ────────────────────────────────────────────
            print("🌐 Opening amazon.in ...")
            await page.goto("https://www.amazon.in", timeout=60000,
                            wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            print(f"   📍 {await page.url()}")

            # ── Click the search box, then type like a person ─────────────
            try:
                await sh.act("Click the Amazon website search box.", page=page)
                await page.wait_for_timeout(700)
            except Exception as e:
                print(f"   (click via AI failed: {type(e).__name__}, typing anyway)")

            await _human_type(page, search_query)
            print("⏎ Pressing Enter...")
            await page.key_press("Enter")
            await page.wait_for_timeout(4000)
            search_url = await page.url()
            print(f"   📍 {search_url}")

            # ── A short scroll only; each phone is found as we go ────
            print("📜 Scanning the product list...")
            await _human_scroll(page, steps=2, delta=620)

            harvested = await page.evaluate(_HARVEST_PRODUCTS_JS) or []
            print(f"✅ Read {len(harvested)} product cards from the page.")
        except Exception:
            await reset_page()
            raise
    except Exception as e:
        print(f"❌ STAGEHAND SEARCH FAILED: {type(e).__name__}: {e}")
        await invalidate_stagehand()
        traceback.print_exc()

    # ── Filter: real phones only, within budget, matching brand ─────────────
    candidates = []
    seen_models = set()
    for item in harvested:
        title = item.get("title", "")
        if not _looks_like_phone(title, item.get("text", "")):
            continue
        price_int = _price_to_int(item.get("price")) or _price_to_int(
            _price_from_text(item.get("text", "")))
        if budget and (price_int is None or price_int > budget):
            print(f"   ✗ Over budget: {title[:42]} "
                  f"({item.get('price') or '?'} > ₹{budget:,})")
            continue
        if not _brand_matches(brand, title, item.get("text", "")):
            print(f"   ✗ Other brand: {title[:45]}")
            continue
        key = _model_key(title)
        if key and key in seen_models:
            print(f"   ✗ Duplicate model: {title[:42]}")
            continue
        if key:
            seen_models.add(key)
        item["_price_int"] = price_int
        candidates.append(item)

    # Keep the order Amazon returned them in: the agent walks down the results
    # page and opens each match as it reaches it, so page order must be kept.
    candidates = candidates[:MAX_PHONES]
    print(f"🎯 {len(candidates)} phone(s) match the criteria (limit {MAX_PHONES}).")

    # Remember what we searched for so later agents don't re-parse the chat history.
    requirements = {"query": search_query, "budget": budget, "brand": brand}

    phones: List[PhoneCandidate] = []
    if candidates:
        try:
            sh, page = await get_page()
            _sh, _browser = await get_stagehand()
        except Exception as e:
            print(f"❌ Could not get browser for product visits: {e}")
            sh, page, _browser = None, None, None

        for idx, item in enumerate(candidates, 1):
            details = ""
            if sh is not None and page is not None:
                try:
                    details = await _visit_product(sh, page, _browser, item, idx,
                                                   search_url)
                except Exception as e:
                    print(f"   ⚠️  Visit issue for {item.get('asin')}: "
                          f"{type(e).__name__}: {e}")
                    # Only tear the whole session down if Chrome is really gone.
                    dead = _browser is not None and getattr(_browser, "closed", False)
                    if dead:
                        await invalidate_stagehand()
                        try:
                            sh, page = await get_page()
                            _sh, _browser = await get_stagehand()
                        except Exception:
                            sh, page, _browser = None, None, None
                    else:
                        await reset_page()
                        try:
                            sh, page = await get_page()
                        except Exception:
                            sh, page, _browser = None, None, None
            phones.append(PhoneCandidate(
                name=item.get("title", "")[:120],
                price=item.get("price") or _price_from_text(item.get("text", "")) or "See page",
                specs=_clean_specs(item.get("text", "")),
                url=item.get("url", "N/A"),
                details=details,
            ))

    # ── One LLM pass to tidy names/specs (URLs and prices stay untouched) ───
    if phones:
        try:
            blocks = "\n".join(
                f"[{i}] {p.name}\n    CARD TEXT: {_clean_specs(p.specs)}"
                for i, p in enumerate(phones, 1)
            )
            resp = await llm.ainvoke([HumanMessage(content=f"""You are tidying scraped Amazon listings.

Below are {len(phones)} real smartphone listings scraped from Amazon India for: '{search_query}'
The user's budget: {'₹' + format(budget, ',') if budget else 'not specified'}

{blocks}

For EACH numbered listing [{1}]..[{len(phones)}], output exactly ONE line:
Name | Short specs

Rules:
- Name: the real product name including brand and model. Never invent models.
- Specs: a short comma-separated blurb (RAM, storage, battery, processor, camera). Max 20 words.
- Output exactly {len(phones)} lines, in the SAME ORDER as the numbers.
- No prices, no URLs, no markdown, no numbering, no commentary.""")])
            lines = [ln.strip() for ln in extract_text(resp.content).strip().split("\n") if ln.strip()]
            lines = [re.sub(r'^\d+[\.\)]\s*', '', ln) for ln in lines]
            for i, p in enumerate(phones):
                if i < len(lines) and "|" in lines[i]:
                    nm, _, sp = lines[i].partition("|")
                    if nm.strip():
                        p.name = nm.strip()[:120]
                    if sp.strip():
                        p.specs = sp.strip()
        except Exception as e:
            print(f"[SEARCH] LLM tidy-up failed ({e}); keeping scraped values.")

    print(f"[SEARCH] Final phones: {len(phones)}")
    return {
        "search_results": phones,
        "requirements": requirements,
        "current_phase": "comparison",
    }


# ── Agent 3: Comparison / Summarizer ───────────────────────────────────────
async def comparison_node(state: AgentState):
    """Compares the phones and highlights trade-offs."""
    phones = state.get("search_results", [])
    if not phones:
        req = state.get("requirements") or {}
        budget = req.get("budget")
        brand = req.get("brand")
        limit_note = f"₹{budget:,}" if budget else "your stated criteria"
        reason = []
        if budget:
            reason.append(f"priced above ₹{budget:,}")
        if brand:
            reason.append(f"not from {brand}")
        why = " or ".join(reason) if reason else "not a match"
        suggestion = (
            f"try ₹{format(int(budget * 1.15), ',')}" if budget else "a higher amount"
        )
        msg = (
            f"😕 I searched Amazon carefully but **no phones matched {limit_note}**.\n\n"
            f"Every listing was {why}, or was an accessory (case, charger, earphones).\n\n"
            "**What you can do:**\n"
            f"- Raise your budget a little (for example, {suggestion})\n"
            "- Drop the brand filter so I can show alternatives\n"
            "- Ask me again with a different budget"
        )
        return {
            "messages": [AIMessage(content=msg)],
            "comparison_summary": msg,
            "current_phase": "clarification",
        }

    phones_text = "\n".join([
        f"{i+1}. **{p.name}** — {p.price}\n"
        f"   Specs: {p.specs}\n"
        f"   Link: {p.url}"
        + (f"\n   Full page details scraped:\n{p.details[:2500]}" if p.details else "")
        for i, p in enumerate(phones)
    ])

    prompt = f"""You are the Comparison Agent. Compare these phones found on Amazon for the user:

{phones_text}

Provide a clear, plain-language breakdown of what differs between them (trade-offs in RAM, battery, camera, etc.).
After the comparison, show the numbered list with the Amazon links so the user can click them directly.
End by asking the user to reply with a number (1-{len(phones)}) to fact-check that listing's claims."""

    response = await llm.ainvoke([HumanMessage(content=prompt)])
    text = extract_text(response.content)

    links_section = "\n\n---\n**Amazon Links:**\n" + "\n".join([
        f"{i+1}. [{p.name}]({p.url}) — {p.price}"
        for i, p in enumerate(phones)
        if p.url and p.url != "N/A"
    ])

    return {
        "messages": [AIMessage(content=text + links_section)],
        "comparison_summary": text + links_section,
        "current_phase": "selection",
    }


# ── Selection handler (called from main.py) ────────────────────────────────
async def selection_wait_node(state: AgentState):
    """Parses the user's phone selection."""
    last_msg = extract_text(state["messages"][-1].content)
    try:
        digits = [s for s in last_msg.split() if s.isdigit()]
        if digits:
            selection = int(digits[0]) - 1
            if 0 <= selection < len(state.get("search_results", [])):
                return {"selected_phone_index": selection, "current_phase": "verification"}
    except Exception:
        pass
    return {
        "messages": [AIMessage(content="Please reply with the number (1-5) of the phone you want me to fact-check.")],
        "current_phase": "selection",
    }


# ── Stagehand: read a single Amazon product page ───────────────────────────
_STRIP_NEW_TAB_JS = r"""
(() => {
  let n = 0;
  document.querySelectorAll('a[target="_blank"]').forEach(a => {
    a.removeAttribute('target');
    n++;
  });
  return n;
})()
"""


async def _scroll_until_asin_visible(page, asin: str, max_steps: int = 7) -> bool:
    """Scroll the results list down until this phone's link is on screen.

    This is what makes the demo read like a person browsing: the page keeps
    moving, and each phone is opened only once the agent has 'found' it.
    """
    js = ("(() => { const a = document.querySelector('a[href*=\"/dp/"
          + asin + "\"]'); if (!a) return 'missing';"
          "const r = a.getBoundingClientRect();"
          "return (r.top >= 40 && r.bottom <= (window.innerHeight - 40))"
          " ? 'visible' : 'below'; })()")
    for _ in range(max_steps):
        try:
            state = await page.evaluate(js)
        except Exception:
            state = "below"

        if state == "visible":
            return True
        if state == "missing":
            return False

        await _smooth_scroll(page, 520)
        await page.wait_for_timeout(random.randint(450, 800))
    return False


async def _close_extra_tabs(browser, keep_page):
    """Guarantee exactly one tab so the demo never spawns extra windows.

    NOTE: must compare page_id, not object identity. Stagehand returns a fresh
    wrapper object on every call, so `p is keep_page` is always False and we
    would close our own tab (killing Chrome when it is the only one).
    """
    if browser is None or keep_page is None:
        return
    try:
        pages = await browser.context.pages()
    except Exception:
        return
    pages = list(pages or [])
    if len(pages) <= 1:
        return

    keep_id = getattr(keep_page, "page_id", None)
    for p in pages:
        try:
            if getattr(p, "page_id", None) == keep_id:
                continue
            await p.close()
            print("   🧹 closed an extra tab")
        except Exception:
            pass


async def _visit_product(sh, page, browser, item: Dict[str, str], index: int,
                         search_url: str) -> str:
    """Open one product the way a person would, then read it while scrolling.

    Returns the full formatted details text scraped from the page.
    """
    asin = item.get("asin", "")
    name = item.get("title", "")[:60]
    print(f"\n🔎 {index}. Opening: {name}")

    # 0. Go back to the results list, exactly like a person would.
    if index > 1:
        try:
            await page.go_back()
            await page.wait_for_timeout(1800)
        except Exception:
            pass

        try:
            current_url = await page.url()
        except Exception:
            current_url = ""

        if search_url and "/dp/" in current_url:
            try:
                await page.goto(search_url, timeout=60000,
                                wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)
            except Exception:
                pass

        # Read a little further down the list before picking the next phone.
        if asin:
            print("   🔍 Scrolling the list to find it...")
            await _scroll_until_asin_visible(page, asin)
    else:
        try:
            current_url = await page.url()
        except Exception:
            current_url = ""

        if search_url and "/dp/" in current_url:
            try:
                await page.goto(search_url, timeout=60000,
                                wait_until="domcontentloaded")
                await page.wait_for_timeout(1200)
            except Exception:
                pass

    # Force every link to open in THIS tab instead of spawning new windows.
    try:
        stripped = await page.evaluate(_STRIP_NEW_TAB_JS)
        if stripped:
            print(f"   ↪ forced {stripped} link(s) to stay in this tab")
    except Exception:
        pass

    # 1. Perform a genuine click on that exact listing.
    #    A real click is faster and lands on precisely the right product,
    #    where an AI "find the link" step is slow and sometimes picks nothing.
    clicked = False
    if asin:
        try:
            link = page.locator(f'a[href*="/dp/{asin}"]').first()
            await link.click(timeout=12000)
            clicked = True
            print("   🖱️  Clicked the product listing")
        except Exception as e:
            print(f"   (direct click failed: {type(e).__name__})")

    if not clicked:
        try:
            await sh.act(
                f"Click the product listing link for '{name}' on this results page.",
                page=page,
            )
            print("   🖱️  Clicked the product listing (via AI)")
            clicked = True
            await page.wait_for_timeout(1200)
        except Exception:
            clicked = False

    await page.wait_for_timeout(2200)

    # 2. Make sure we actually landed on the right product, in one tab.
    try:
        current = await page.url()
    except Exception:
        current = ""
    if asin and asin not in current:
        print("   ↪ correcting to the exact product link")
        await page.goto(item["url"], timeout=60000,
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

    await _close_extra_tabs(browser, page)

    # 3. Scroll and read together so the reading follows what is on screen.
    acc: Dict[str, Any] = {}
    try:
        first = await page.evaluate(_HARVEST_PRODUCT_PAGE_JS) or {}
        _merge_harvest(acc, first)
    except Exception:
        pass

    for _ in range(5):
        await _smooth_scroll(page, 600)
        await page.wait_for_timeout(random.randint(550, 1000))
        try:
            chunk = await page.evaluate(_HARVEST_PRODUCT_PAGE_JS) or {}
            _merge_harvest(acc, chunk)
        except Exception:
            pass

    details = _format_details(acc)
    title = acc.get("title") or name
    print(f"   📖 {str(title)[:70]}")
    if acc.get("delivery"):
        print(f"   🚚 {str(acc['delivery'])[:70]}")
    print(f"   📄 {len(details)} chars of detail captured")
    return details


async def _stagehand_read_product(url: str) -> str:
    """Use Stagehand to open an Amazon product page and pull its full specs."""
    try:
        sh, page = await get_page()
        try:
            print(f"   📍 {url}")
            await page.goto(url, timeout=60000)
            await page.wait_for_timeout(2500)
            await page.evaluate("window.scrollBy(0, 1500)")
            await page.wait_for_timeout(2000)

            data = await page.evaluate(_HARVEST_PRODUCT_PAGE_JS) or {}
            title = (data.get("title") or "").strip()
            bullets = (data.get("bullets") or "").strip()
            tech = (data.get("tech") or "").strip()
            desc = (data.get("description") or "").strip()

            if not any([title, bullets, tech]):
                raise RuntimeError("product page selectors returned nothing")

            print(f"   📖 {title[:70]}")
            return (
                f"Title: {title}\n\n"
                f"Key Features:\n{bullets}\n\n"
                f"Technical Specifications:\n{tech}\n\n"
                f"Description:\n{desc}"
            )
        except Exception:
            await reset_page()
            raise
    except Exception as e:
        print(f"❌ STAGEHAND PRODUCT PAGE FAILED: {type(e).__name__}: {e}")
        await invalidate_stagehand()
        return ""


# ── Agent 4: Verification Planner ──────────────────────────────────────────
async def verification_planner_node(state: AgentState):
    """Decomposes the listing into specific checkable claims."""
    phone = state["search_results"][state["selected_phone_index"]]

    full_amazon_text = await _stagehand_read_product(phone.url)
    phone_specs_context = (
        full_amazon_text[:15000] if len(full_amazon_text) > 100 else phone.specs
    )

    prompt = f"""You are the Verification Planner. The user wants to fact-check this phone listing:
Name: {phone.name}
URL: {phone.url}
Full Product Details: {phone_specs_context}

List 3-4 specific checkable claims extracted STRICTLY from this listing's specs. Do not invent claims.
Focus on claims that are often exaggerated in marketing (e.g., Battery capacity, RAM size, Processor model).

Reply ONLY as a numbered list, one claim per line. Example:
1. [Insert Claim 1 from specs]
2. [Insert Claim 2 from specs]
3. [Insert Claim 3 from specs]"""

    try:
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        raw = extract_text(response.content)
        claims = []
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if line[0].isdigit():
                line = line.split(".", 1)[-1].strip()
            elif line.startswith("-"):
                line = line[1:].strip()
            if line:
                claims.append(VerificationClaim(claim=line, context=phone.specs))
        if not claims:
            claims = [VerificationClaim(claim="RAM amount", context=phone.specs)]
    except Exception as e:
        print(f"[PLANNER] Claims extraction failed: {e}")
        claims = [VerificationClaim(claim="RAM amount", context=phone.specs)]

    print(f"[PLANNER] Extracted {len(claims)} claims to verify")
    return {"claims_to_check": claims, "current_phase": "verification_execution"}


# ── Stagehand: web search for fact-checking ────────────────────────────────
async def _stagehand_search_text(query: str) -> str:
    """Use the Stagehand browser to run a web search and return the page text."""
    try:
        sh, page = await get_page()
        try:
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            print(f"   📍 DuckDuckGo: {query[:60]}")
            await page.goto(url, timeout=60000)
            await page.wait_for_timeout(2000)
            for _ in range(2):
                await page.evaluate("window.scrollBy(0, 700)")
                await page.wait_for_timeout(1200)
            return await page.evaluate("document.body.innerText")
        except Exception:
            await reset_page()
            raise
    except Exception as e:
        print(f"❌ STAGEHAND SEARCH FAILED: {type(e).__name__}: {e}")
        await invalidate_stagehand()
        return ""


# ── Agent 5: Verifier ──────────────────────────────────────────────────────
async def verifier_node(state: AgentState):
    """Independently verifies each claim via Stagehand browser search."""
    claims = state.get("claims_to_check", [])
    phone = state["search_results"][state["selected_phone_index"]]

    results = []
    for claim in claims:
        query = f"{phone.name} {claim.claim} specs review"
        print(f"   🔍 Fact-checking: {claim.claim}")
        raw_text = await _stagehand_search_text(query)

        if not raw_text or len(raw_text) < 100:
            findings = f"Could not independently verify '{claim.claim}'. Marking as potentially misleading."
        else:
            findings = raw_text[:15000]

        try:
            verdict_prompt = f"""Based on these search results, is the claim '{claim.claim}' genuine or misleading?

Search Results:
{findings}

IMPORTANT RULE: If the search results generally confirm the claim (e.g. they mention the phone has 128GB storage), you MUST say it is verified. Do not be overly skeptical or guess that it might be a lower model unless the text explicitly says so.

Reply in exactly this format (two lines only):
VERDICT: true   (or false)
EXPLANATION: <one sentence explaining why>"""
            verdict_response = await llm.ainvoke([HumanMessage(content=verdict_prompt)])
            verdict_text = extract_text(verdict_response.content).strip()
            is_verified = False
            explanation = verdict_text
            for line in verdict_text.split("\n"):
                if line.upper().startswith("VERDICT:"):
                    is_verified = "true" in line.lower()
                elif line.upper().startswith("EXPLANATION:"):
                    explanation = line.split(":", 1)[-1].strip()
            results.append(VerificationResult(
                claim=claim.claim, is_verified=is_verified, explanation=explanation))
        except Exception as e:
            print(f"[VERIFIER] Verdict extraction failed: {e}")
            results.append(VerificationResult(
                claim=claim.claim, is_verified=False,
                explanation=f"Verification inconclusive: {str(e)}"))

    return {"verification_results": results}


# ── Agent 6: Critic / Risk Synthesis ───────────────────────────────────────
async def critic_node(state: AgentState):
    """Synthesizes verification results looking for compounding red flags."""
    results = state.get("verification_results", [])

    prompt = "You are the Critic Agent. Review these verification results for a phone listing:\n\n"
    for r in results:
        status = "✅ Verified" if r.is_verified else "⚠️ Potentially Misleading"
        prompt += f"- Claim: {r.claim} | {status} | {r.explanation}\n"
    prompt += "\nSynthesize these findings. Look for compounding red flags. Provide a harsh, honest risk synthesis."

    response = await llm.ainvoke([HumanMessage(content=prompt)])
    return {"critic_synthesis": extract_text(response.content)}


# ── Agent 7: Finalizer ─────────────────────────────────────────────────────
async def finalizer_node(state: AgentState):
    """Assembles the final plain-language report."""
    phone = state["search_results"][state["selected_phone_index"]]
    results = state.get("verification_results", [])

    results_text = "\n".join([
        f"- {r.claim}: {'✅ Verified' if r.is_verified else '⚠️ Misleading'} — {r.explanation}"
        for r in results
    ])

    prompt = f"""You are the Finalizer Agent. Write the final report for the user in plain, non-technical language.

Phone: {phone.name} — {phone.price}
Specs: {phone.specs}

Individual Verification Results:
{results_text}

Critic Risk Synthesis:
{state.get('critic_synthesis', '')}

Write a clear, honest summary:
1. Which claims are genuine
2. Which claims are misleading (explain WHY in simple terms)
3. Your final recommendation: Buy, Proceed with Caution, or Avoid"""

    response = await llm.ainvoke([HumanMessage(content=prompt)])
    text = extract_text(response.content)

    return {
        "messages": [AIMessage(content=text)],
        "final_report": text,
        "current_phase": "selection",
    }


# ── Graph Construction ─────────────────────────────────────────────────────
workflow = StateGraph(AgentState)

workflow.add_node("conversation_node", conversation_node)
workflow.add_node("search_node", search_node)
workflow.add_node("comparison_node", comparison_node)
workflow.add_node("selection_wait_node", selection_wait_node)
workflow.add_node("verification_planner_node", verification_planner_node)
workflow.add_node("verifier_node", verifier_node)
workflow.add_node("critic_node", critic_node)
workflow.add_node("finalizer_node", finalizer_node)


def route_clarification(state: AgentState):
    if state.get("ready_to_search"):
        return "search_node"
    return END


def route_selection(state: AgentState):
    if state.get("selected_phone_index") is not None:
        return "verification_planner_node"
    return END


workflow.set_entry_point("conversation_node")
workflow.add_conditional_edges("conversation_node", route_clarification,
                               {"search_node": "search_node", END: END})
workflow.add_edge("search_node", "comparison_node")
workflow.add_edge("comparison_node", END)
workflow.add_conditional_edges("selection_wait_node", route_selection,
                               {"verification_planner_node": "verification_planner_node", END: END})
workflow.add_edge("verification_planner_node", "verifier_node")
workflow.add_edge("verifier_node", "critic_node")
workflow.add_edge("critic_node", "finalizer_node")
workflow.add_edge("finalizer_node", END)

app = workflow.compile()