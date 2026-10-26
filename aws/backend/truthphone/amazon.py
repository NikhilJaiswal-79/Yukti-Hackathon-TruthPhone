"""Amazon.in harvesting and browser helpers.

The JavaScript below is deliberately DOM-based: prices, ASINs and URLs come
from the page, not from an LLM. The LLM is only used to tidy the final names
and specs after the harvest.
"""

from __future__ import annotations

import json
import logging
import random
import re
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .config import MAX_DETAILS_CHARS, MAX_PHONES
from .models import PhoneCandidate

logger = logging.getLogger(__name__)

StatusCallback = Optional[Callable[[str], Awaitable[None]]]

_HARVEST_PRODUCTS_JS = r"""
(() => {
  const cards = Array.from(document.querySelectorAll(
    '[data-component-type="s-search-result"], div[data-asin]:not([data-asin=""])'
  ));
  const out = [];
  const seen = new Set();
  for (const c of cards) {
    const link =
      c.querySelector('h2 a[href*="/dp/"]') ||
      c.querySelector('a.a-link-normal[href*="/dp/"]') ||
      c.querySelector('a[href*="/dp/"]');
    if (!link) continue;

    const href = (link.getAttribute('href') || '').split('?')[0];
    const m = href.match(/\/dp\/([A-Z0-9]{10})/);
    const asin = (m ? m[1] : (c.getAttribute('data-asin') || '')).trim();
    if (!asin || seen.has(asin)) continue;

    const titleEl = c.querySelector('h2') || link;
    const img = c.querySelector('img[alt]');
    const title = (titleEl.innerText || (img ? img.alt : '') || '')
      .trim().replace(/\s+/g, ' ');
    if (!title) continue;

    const priceEl =
      c.querySelector('.a-price > span.a-offscreen') ||
      c.querySelector('.a-price .a-offscreen') ||
      c.querySelector('.a-price');

    seen.add(asin);
    out.push({
      asin: asin,
      title: title,
      price: priceEl ? priceEl.innerText.trim() : '',
      text: c.innerText.trim().replace(/\s+/g, ' ').slice(0, 700),
      url: 'https://www.amazon.in/dp/' + asin
    });
  }
  return out;
})()
"""


async def _amazon_blocked(page) -> bool:
    """Detect Amazon's 503 bot page (different from a solvable CAPTCHA)."""
    try:
        title = await page.title()
        if re.search(r"\b503\b|service unavailable", title or "", re.I):
            return True
        text = await page.evaluate(
            "document.body ? document.body.innerText.slice(0, 600) : ''"
        )
        lowered = (text or "").lower()
        return (
            "service unavailable" in lowered
            or "automated access to amazon data" in lowered
        )
    except Exception:
        return False


async def _page_diagnostics(page) -> Dict[str, Any]:
    """Small, log-safe snapshot of what Amazon actually returned."""
    try:
        return await page.evaluate(
            r"""
            (() => ({
              title: document.title,
              url: location.href,
              asinNodes: document.querySelectorAll('div[data-asin]').length,
              resultNodes: document.querySelectorAll(
                '[data-component-type="s-search-result"]'
              ).length,
              dpLinks: document.querySelectorAll('a[href*="/dp/"]').length,
              body: (document.body ? document.body.innerText : '').slice(0, 1200)
            }))()
            """
        )
    except Exception as exc:
        return {"error": str(exc)}


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

_GENERIC_QUERY_RE = re.compile(
    r"^(please\s+)?(search|search now|search amazon|go ahead|yes|ok|okay|"
    r"find them|show me|do it|proceed|start|continue)[.! ]*$",
    re.I,
)


def _merge_harvest(acc: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in (new or {}).items():
        if not value:
            continue
        if isinstance(value, list):
            merged = list(acc.get(key) or [])
            seen = set(merged)
            for item in value:
                if item not in seen:
                    merged.append(item)
                    seen.add(item)
            acc[key] = merged
        else:
            current = acc.get(key) or ""
            if len(str(value)) > len(str(current)):
                acc[key] = value
    return acc


def _format_details(harvest: Dict[str, Any]) -> str:
    if not harvest:
        return ""
    out = []
    labels = [
        ("brand", "Brand"),
        ("price", "Price"),
        ("mrp", "M.R.P"),
        ("saving", "Discount"),
        ("rating", "Rating"),
        ("reviews", "Reviews"),
        ("delivery", "Delivery"),
        ("soldBy", "Sold by"),
        ("warranty", "Warranty"),
        ("offers", "Offers"),
        ("highlights", "Highlights"),
    ]
    for key, label in labels:
        value = harvest.get(key)
        if not value:
            continue
        if isinstance(value, list):
            value = " | ".join(str(item) for item in value[:6])
        out.append(f"{label}: {str(value)[:400]}")

    bullets = harvest.get("bullets")
    if bullets:
        out.append("Key Features:\n" + str(bullets)[:1500])

    specs = harvest.get("specs")
    if specs:
        rows = [str(item) for item in specs[:70]]
        out.append("Technical Specifications:\n" + "\n".join(rows)[:2500])

    description = harvest.get("description")
    if description:
        out.append("Description:\n" + str(description)[:1200])

    return "\n\n".join(out)


def _clean_specs(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw or "").strip()
    text = re.sub(r"^Save extra with No Cost EMI\s*", "", text)
    text = re.sub(r"FREE delivery.*$", "", text).strip()
    text = re.sub(r"\b\d+\.\d+\s+out of 5 stars.*$", "", text).strip()
    text = re.sub(r"\b[\d,]+ ratings?\b", "", text).strip()
    return text[:320].strip()


def _clean_query(raw: str) -> str:
    query = (raw or "").strip()
    query = re.sub(
        r"^\s*(please\s+)?(can you\s+|could you\s+)?"
        r"(search for|search|find me|find|show me|show|look for|get me|get|"
        r"i want|i need|i'm looking for|im looking for|looking for)\b[:\s]*",
        "",
        query,
        flags=re.I,
    ).strip()
    query = re.sub(
        r"\b(from\s+(amazon|flipkart)[a-z.]*|on\s+(amazon|flipkart)[a-z.]*|"
        r"in\s+india|please)\b\s*\.?$",
        "",
        query,
        flags=re.I,
    ).strip(" .,?!")
    # Amazon search works better without the budget phrase. Keep the budget in
    # the filter, not in the query text.
    query = re.sub(
        r"\b(under|below|less than|lesser than|within|up ?to|upto|max(?:imum)?|"
        r"budget(?: of)?|around|about|approximately|cheaper than)\s*"
        r"(?:rs\.?|₹)?\s*\d[\d,]*(?:\.\d+)?\s*(?:k|thousand|lakhs?|lacs?|crore)?\b",
        " ",
        query,
        flags=re.I,
    )
    query = re.sub(
        r"(?:₹|rs\.?)\s*\d[\d,]*(?:\.\d+)?\s*(?:k|thousand|lakhs?|lacs?|crore)?\b",
        " ",
        query,
        flags=re.I,
    )
    # A bare 4-7 digit number is almost always a budget (or a model year),
    # not part of the Amazon search phrase.
    query = re.sub(r"(?<![\w.])\d{4,7}(?![\w.])", " ", query)
    query = re.sub(r"\s+", " ", query).strip(" .,?!")
    query = re.sub(r"^a\s+", "", query, flags=re.I)
    return query


def is_generic_query(text: str) -> bool:
    return bool(_GENERIC_QUERY_RE.match((text or "").strip()))


def pick_query_message(message: str, user_messages: List[str]) -> str:
    """If the user just says "search", reuse their last substantive message."""
    if not is_generic_query(message):
        return message
    for previous in reversed(user_messages):
        if previous.strip() == message.strip() or is_generic_query(previous):
            continue
        if len(previous.split()) >= 2:
            return previous
    return message


def _price_to_int(price: str) -> Optional[int]:
    if not price:
        return None
    match = re.search(r"[\d][\d,]*", str(price))
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


_BUDGET_UNITS = {
    "k": 1_000,
    "thousand": 1_000,
    "lakh": 100_000,
    "lac": 100_000,
    "lakhs": 100_000,
    "crore": 10_000_000,
}


def _scale(number: str, unit: Optional[str] = None) -> int:
    try:
        value = float(number)
    except (TypeError, ValueError):
        return 0
    if unit:
        value *= _BUDGET_UNITS.get(unit.lower(), 1)
    return int(value)


def _parse_budget(text: str) -> Optional[int]:
    text = (text or "").lower().replace(",", "")
    patterns = [
        r"(?:under|below|less than|within|up ?to|upto|max(?:imum)?|budget of|budget|"
        r"lesser than|cheaper than|around|about|approximately)\s*(?:rs\.?|₹)?\s*"
        r"(\d+(?:\.\d+)?)\s*(k|thousand|lakhs?|lacs?|crore)?",
        r"(?:₹|rs\.?)\s*(\d+(?:\.\d+)?)\s*(k|thousand|lakhs?|lacs?|crore)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = _scale(match.group(1), match.group(2))
            if value:
                return value

    match = re.search(r"(?<!\d)(\d{4,7})(?!\d)", text)
    if match:
        value = int(match.group(1))
        # Do not mistake a model year such as 2026 for a budget.
        if 1900 <= value <= 2100:
            return None
        return value
    return None


_KNOWN_BRANDS = [
    "samsung",
    "redmi",
    "xiaomi",
    "oneplus",
    "realme",
    "vivo",
    "oppo",
    "moto",
    "motorola",
    "nokia",
    "iphone",
    "apple",
    "nothing",
    "tecno",
    "infinix",
    "iqoo",
    "honor",
    "asus",
    "sony",
    "lg",
]


def _parse_brand(text: str) -> Optional[str]:
    text = (text or "").lower()
    best_index: Optional[int] = None
    best_brand: Optional[str] = None
    for brand in _KNOWN_BRANDS:
        match = re.search(rf"\b{re.escape(brand)}\b", text)
        if match and (best_index is None or match.start() < best_index):
            best_index = match.start()
            best_brand = brand
    return best_brand


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


def _brand_matches(brand: Optional[str], *texts: str) -> bool:
    if not brand:
        return True
    aliases = _BRAND_ALIASES.get(brand, [brand])
    blob = " ".join(text or "" for text in texts).lower()
    return any(re.search(rf"\b{re.escape(alias)}\b", blob) for alias in aliases)


_COLOUR_WORDS = (
    r"black|white|blue|green|red|pink|gold|silver|violet|grey|gray|mint|navy|beige|"
    r"purple|orange|yellow|maroon|charcoal|lilac|coral|magenta|teal|"
    r"pop|lit|rogue|spotlight|blitz|vibe|sapphire|moonlight|awesome|limelight|"
    r"midnight|neon|ice|edition"
)


def _model_key(title: str) -> str:
    text = (title or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b\d+\s?(gb|tb|mb)\b", " ", text)
    text = re.sub(r"\b\d{3,5}\s?mah\b", " ", text)
    text = re.sub(r"\b\d+(\.\d+)?\s?(inch|inches|in)\b", " ", text)
    text = re.sub(rf"\b({_COLOUR_WORDS})\b", " ", text)
    text = re.sub(
        r"\b(ram|rom|storage|internal|mobile|phone|smartphone|with|and|"
        r"selfie|camera|display|battery|wifi|bluetooth|model)\b",
        " ",
        text,
    )
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_ACCESSORY_RE = re.compile(
    r"\b(case|cases|cover|covers|screen guard|tempered|charger|adaptor|adapter|cable|"
    r"power ?bank|earphone|earphones|earbud|earbuds|headphone|headphones|headset|"
    r"strap|mount|skin|sticker|protector|wallet|sleeve|pouch|holder|stand|tripod|"
    r"selfie|ring|pop socket|lens|glass|back cover|flip cover|screen protector|"
    r"phone cover|holster|armband|lanyard)\b",
    re.I,
)

_PHONE_HINT_RE = re.compile(
    r"\b(5g|4g|\d+\s?gb|\d+\s?tb|mah|amoled|oled|ips\s?lcd|"
    r"helio|dimensity|snapdragon|exynos|unisoc|kirin|octorcore|octa core|"
    r"ram|storage|dual sim|android)\b",
    re.I,
)


def _looks_like_phone(title: str, text: str = "") -> bool:
    if not title:
        return False
    if _ACCESSORY_RE.search(title):
        return False
    return bool(_PHONE_HINT_RE.search(title) or _PHONE_HINT_RE.search(text or ""))


def _price_from_text(raw: str) -> str:
    match = re.search(r"(?:₹|\bRs\.?\s?)\s?[\d,]+", raw or "")
    if not match:
        return ""
    return match.group(0).replace(" ", "").replace("Rs.", "₹").replace("Rs", "₹")


async def _smooth_scroll(page, delta: int) -> None:
    distance = delta + random.randint(-90, 170)
    try:
        await page.evaluate(
            f"window.scrollBy({{top: {distance}, left: 0, behavior: 'smooth'}});"
        )
    except Exception:
        try:
            await page.evaluate(f"window.scrollBy(0, {distance});")
        except Exception:
            pass


async def _human_scroll(
    page, steps: int = 4, delta: int = 620, pause_ms: Tuple[int, int] = (600, 1100)
) -> None:
    for _ in range(steps):
        await _smooth_scroll(page, delta)
        await page.wait_for_timeout(random.randint(*pause_ms))


async def _captcha_visible(page) -> bool:
    try:
        if await page.locator("form[action*='validateCaptcha']").count():
            return True
        if await page.locator("input#captchacharacters").count():
            return True
        title = (await page.title()).lower()
        if "robot check" in title or "captcha" in title:
            return True
    except Exception:
        pass
    return False


async def _wait_for_selector(page, selector: str, timeout_seconds: int = 90) -> bool:
    deadline = timeout_seconds * 1000
    elapsed = 0
    step = 3000
    while elapsed <= deadline:
        try:
            if await page.locator(selector).count():
                return True
        except Exception:
            pass
        await page.wait_for_timeout(step)
        elapsed += step
    return False


async def _read_product(
    page, url: str, on_status: StatusCallback = None
) -> str:
    try:
        await page.goto(url, timeout=60_000, wait_until="domcontentloaded")
        await page.wait_for_timeout(1800)
        if await _captcha_visible(page):
            if on_status:
                await on_status(
                    "Amazon is showing a CAPTCHA. Solve it in the live browser "
                    "on the right; I'll continue automatically."
                )
            if not await _wait_for_selector(page, "#productTitle", 120):
                return ""
        harvest: Dict[str, Any] = {}
        try:
            harvest = _merge_harvest(
                harvest, await page.evaluate(_HARVEST_PRODUCT_PAGE_JS) or {}
            )
        except Exception:
            pass
        for _ in range(4):
            await _smooth_scroll(page, 600)
            await page.wait_for_timeout(random.randint(550, 1000))
            try:
                harvest = _merge_harvest(
                    harvest, await page.evaluate(_HARVEST_PRODUCT_PAGE_JS) or {}
                )
            except Exception:
                pass
        return _format_details(harvest)[:MAX_DETAILS_CHARS]
    except Exception as exc:
        logger.warning("Product read failed for %s: %s", url, exc)
        return ""


async def search_amazon(
    page,
    message: str,
    context: str = "",
    on_status: StatusCallback = None,
) -> Tuple[List[PhoneCandidate], Dict[str, Any]]:
    """Search Amazon.in, filter real phones, and read each product page."""
    query = _clean_query(message) or "smartphones"
    budget_text = context or message
    budget = _parse_budget(budget_text)
    brand = _parse_brand(budget_text)

    logger.info(
        "Amazon search query=%r budget=%s brand=%s", query, budget, brand
    )
    try:
        await page.set_extra_http_headers(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
    except Exception:
        logger.warning("Could not apply browser headers", exc_info=True)

    for attempt in range(2):
        try:
            await page.goto(
                "https://www.amazon.in",
                timeout=60_000,
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(2000 + attempt * 1500)
            if await _amazon_blocked(page):
                logger.warning(
                    "Amazon home page showed a 503 (attempt %s)", attempt + 1
                )
                await page.wait_for_timeout(3000)
                continue
            break
        except Exception as exc:
            logger.warning(
                "Could not open Amazon.in (attempt %s): %s", attempt + 1, exc
            )
            await page.wait_for_timeout(3000)
    else:
        return [], {
            "query": query,
            "budget": budget,
            "brand": brand,
            "error": "blocked",
        }

    if await _captcha_visible(page):
        if on_status:
            await on_status(
                "Amazon is showing a CAPTCHA. Solve it in the live browser on "
                "the right; I'll continue automatically."
            )
        if not await _wait_for_selector(page, "input#twotabsearchtextbox", 120):
            logger.warning("Search box never appeared after CAPTCHA")
            return [], {
                "query": query,
                "budget": budget,
                "brand": brand,
                "error": "captcha",
            }

    box = page.locator("input#twotabsearchtextbox")
    try:
        await box.wait_for(state="visible", timeout=20_000)
        await box.click()
        try:
            await box.press_sequentially(query, delay=45)
        except AttributeError:
            await box.type(query, delay=45)
        await box.press("Enter")
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(3500)
        if "/s?" not in page.url and "/s/" not in page.url:
            try:
                await page.locator("input#nav-search-submit-button").first.click(
                    timeout=5000
                )
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(2500)
            except Exception:
                pass

    except Exception as exc:
        logger.warning("Amazon search box interaction failed: %s", exc)
        return [], {
            "query": query,
            "budget": budget,
            "brand": brand,
            "error": "search_failed",
        }

    if await _amazon_blocked(page):
        logger.warning("Amazon search page showed a 503")
        return [], {
            "query": query,
            "budget": budget,
            "brand": brand,
            "error": "blocked",
            "pageTitle": await page.title(),
        }

    if await _captcha_visible(page):
        if on_status:
            await on_status(
                "Amazon is asking for a CAPTCHA after the search. Solve it in "
                "the live browser on the right; I'll continue automatically."
            )
        for _ in range(40):
            if not await _captcha_visible(page):
                break
            await page.wait_for_timeout(3000)
        else:
            logger.warning("Search results CAPTCHA was not solved in time")
            return [], {
                "query": query,
                "budget": budget,
                "brand": brand,
                "error": "captcha",
            }

    search_url = page.url
    await _human_scroll(page, steps=2, delta=620)

    harvested = []
    for _ in range(2):
        try:
            harvested = await page.evaluate(_HARVEST_PRODUCTS_JS) or []
        except Exception:
            harvested = []
        if harvested:
            break
        await page.wait_for_timeout(2000)

    logger.info("Harvested %s product cards", len(harvested))
    diagnostics: Dict[str, Any] = {}
    if not harvested:
        diagnostics = await _page_diagnostics(page)
        logger.warning(
            "Amazon harvest empty: %s",
            json.dumps(diagnostics, ensure_ascii=False)[:2000],
        )

    candidates = []
    seen_models = set()
    for item in harvested:
        title = item.get("title", "")
        if not _looks_like_phone(title, item.get("text", "")):
            continue
        price_int = _price_to_int(item.get("price")) or _price_to_int(
            _price_from_text(item.get("text", ""))
        )
        if budget and (price_int is None or price_int > budget):
            continue
        if not _brand_matches(brand, title, item.get("text", "")):
            continue
        key = _model_key(title)
        if key and key in seen_models:
            continue
        if key:
            seen_models.add(key)
        item["_price_int"] = price_int
        candidates.append(item)

    candidates = candidates[:MAX_PHONES]
    logger.info("Matched %s phone(s)", len(candidates))

    phones: List[PhoneCandidate] = []
    for item in candidates:
        url = item.get("url") or ""
        details = await _read_product(page, url, on_status) if url else ""
        phones.append(
            PhoneCandidate(
                name=(item.get("title") or "")[:120],
                price=(
                    item.get("price")
                    or _price_from_text(item.get("text", ""))
                    or "See page"
                ),
                specs=_clean_specs(item.get("text", "")),
                url=url or "N/A",
                details=details,
                asin=item.get("asin", ""),
            )
        )

    requirements: Dict[str, Any] = {
        "query": query,
        "budget": budget,
        "brand": brand,
        "searchUrl": search_url,
        "found": len(phones),
    }
    if not harvested:
        requirements["error"] = "no_cards"
        requirements["pageTitle"] = diagnostics.get("title", "")
        requirements["pageUrl"] = diagnostics.get("url", "")
    return phones, requirements


_SEARCH_ENGINE_JS = r"""
(() => {
  const out = [];
  const push = (title, snippet, url) => {
    if (title && url) out.push({
      title: title.trim().replace(/\s+/g, ' '),
      snippet: (snippet || '').trim().replace(/\s+/g, ' '),
      url: url
    });
  };

  // DuckDuckGo HTML results
  for (const r of document.querySelectorAll('div.result')) {
    const a = r.querySelector('a.result__a');
    if (!a) continue;
    let href = a.getAttribute('href') || '';
    try {
      const u = new URL(href, location.href);
      const target = u.searchParams.get('uddg');
      href = target ? decodeURIComponent(target) : u.href;
    } catch (e) {}
    if (!/^https?:/i.test(href)) continue;
    const s = r.querySelector('.result__snippet');
    push(a.innerText || '', s ? s.innerText : '', href);
  }

  // Bing results
  for (const r of document.querySelectorAll('li.b_algo')) {
    const a = r.querySelector('h2 a');
    if (!a || !a.href) continue;
    const s = r.querySelector('.b_caption p, .b_snippet, p');
    push(a.innerText || '', s ? s.innerText : '', a.href);
  }
  return out;
})()
"""


def _shop_rank(url: str) -> int:
    lowered = (url or "").lower()
    if "amazon.in/" in lowered and "/dp/" in lowered:
        return 0
    if "flipkart.com/" in lowered:
        return 1
    if any(host in lowered for host in ("smartprix.com", "91mobiles.com", "croma.com")):
        return 2
    if any(host in lowered for host in ("reliancedigital.in", "vijaysales.com", "samsung.com")):
        return 2
    return 3


def _is_search_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(
        token in lowered
        for token in (
            "amazon.in/s?",
            "amazon.in/s/",
            "flipkart.com/search",
            "google.com/search",
            "bing.com/search",
            "duckduckgo.com/",
        )
    )


_FLIPKART_HARVEST_JS = r"""
(() => {
  const cards = Array.from(document.querySelectorAll('div[data-id]'));
  const out = [];
  const seen = new Set();
  for (const c of cards) {
    const a = c.querySelector('a[href*="/p/"]');
    if (!a) continue;
    let abs = '';
    try {
      abs = new URL(a.getAttribute('href') || '', location.origin).href;
    } catch (e) { continue; }
    const id = (c.getAttribute('data-id') || abs).trim();
    if (!id || seen.has(id)) continue;

    const titleEl =
      c.querySelector('a[title]') ||
      c.querySelector('div._4rR01T') ||
      c.querySelector('a.s1Q9rs') ||
      a;
    let title = '';
    if (titleEl) {
      title = titleEl.getAttribute('title') || titleEl.innerText || '';
    }
    if (!title) {
      const img = c.querySelector('img[alt]');
      title = img ? img.alt : '';
    }
    title = (title || '').trim().replace(/\s+/g, ' ');
    if (!title) continue;

    const priceEl =
      c.querySelector('div._30jeq3') ||
      c.querySelector('div.Nx9bqj') ||
      c.querySelector('div[class*="price"]');
    const price = priceEl ? priceEl.innerText.trim() : '';

    seen.add(id);
    out.push({
      id: id,
      title: title,
      price: price,
      text: c.innerText.trim().replace(/\s+/g, ' ').slice(0, 700),
      url: abs
    });
  }
  return out;
})()
"""


async def search_flipkart(
    page,
    query: str,
    budget: Optional[int] = None,
    brand: Optional[str] = None,
) -> List[PhoneCandidate]:
    """Search Flipkart directly; used when Amazon blocks the browser."""
    search_url = "https://www.flipkart.com/search?q=" + urllib.parse.quote_plus(query)
    try:
        await page.goto(search_url, timeout=60_000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2200)
        for _ in range(2):
            await _smooth_scroll(page, 700)
            await page.wait_for_timeout(700)
        items = await page.evaluate(_FLIPKART_HARVEST_JS) or []
    except Exception as exc:
        logger.warning("Flipkart search failed: %s", exc)
        return []

    logger.info("Flipkart search returned %s cards", len(items))
    if not items:
        diagnostics = await _page_diagnostics(page)
        logger.warning(
            "Flipkart harvest empty: %s",
            json.dumps(diagnostics, ensure_ascii=False)[:1200],
        )

    candidates: List[PhoneCandidate] = []
    seen = set()
    for item in items:
        title = item.get("title", "")
        text = item.get("text", "")
        if not title or not _looks_like_phone(title, text):
            continue
        if not _brand_matches(brand, title, text):
            continue
        price_text = item.get("price", "") or _price_from_text(text)
        price_int = _price_to_int(price_text)
        if budget and price_int is None:
            continue
        if budget and price_int and price_int > budget:
            continue
        key = _model_key(title)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        candidates.append(
            PhoneCandidate(
                name=title[:120],
                price=price_text or "See page",
                specs=_clean_specs(text),
                url=item.get("url", ""),
                details="",
                asin=item.get("id", ""),
            )
        )

    candidates = candidates[:MAX_PHONES]
    logger.info("Flipkart matched %s phone(s)", len(candidates))
    return candidates


async def web_search_listings(
    page,
    query: str,
    budget: Optional[int] = None,
    brand: Optional[str] = None,
) -> List[PhoneCandidate]:
    """Fallback listing discovery when Amazon blocks the browser (HTTP 503)."""
    search_query = f"{query} price India smartphone"
    engines = [
        "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(search_query),
        "https://www.bing.com/search?q="
        + urllib.parse.quote_plus(search_query)
        + "&cc=IN&setlang=en-IN",
    ]

    items: List[Dict[str, Any]] = []
    for engine_url in engines:
        try:
            await page.goto(
                engine_url, timeout=45_000, wait_until="domcontentloaded"
            )
            await page.wait_for_timeout(1800)
            found = await page.evaluate(_SEARCH_ENGINE_JS) or []
            logger.info(
                "Web fallback %s returned %s results",
                "DuckDuckGo" if "duckduckgo" in engine_url else "Bing",
                len(found),
            )
            items.extend(found)
        except Exception as exc:
            logger.warning("Web fallback failed for %s: %s", engine_url, exc)

    if not items:
        logger.warning("Web fallback returned no results for %r", query)

    # Prefer results that already show a price, then real shop pages. This way
    # a priced Bing snippet wins over an unpriced DuckDuckGo snippet for the
    # same model before deduplication.
    def _has_price(item: Dict[str, Any]) -> bool:
        return bool(
            _price_from_text(item.get("snippet", ""))
            or _price_from_text(item.get("title", ""))
        )

    items.sort(
        key=lambda item: (
            0 if _has_price(item) else 1,
            _shop_rank(item.get("url", "")),
        )
    )

    candidates: List[PhoneCandidate] = []
    unknown_price: List[PhoneCandidate] = []
    seen = set()
    for item in items:
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        url = item.get("url", "")
        if not title or not url or _is_search_url(url):
            continue
        if not _looks_like_phone(title, snippet):
            continue
        if not _brand_matches(brand, title, snippet):
            continue

        key = _model_key(title)
        if key and key in seen:
            continue

        price_text = _price_from_text(snippet) or _price_from_text(title)
        price_int = _price_to_int(price_text)
        if budget and price_int and price_int > budget:
            continue
        if key:
            seen.add(key)

        phone = PhoneCandidate(
            name=title[:120],
            price=price_text or "See page",
            specs=_clean_specs(snippet),
            url=url,
            details="",
            asin=(
                re.search(r"/dp/([A-Z0-9]{10})", url).group(1)
                if re.search(r"/dp/([A-Z0-9]{10})", url)
                else ""
            ),
        )
        if budget and price_int is None:
            unknown_price.append(phone)
        else:
            candidates.append(phone)

    if budget and not candidates:
        candidates = unknown_price

    candidates.sort(key=lambda phone: _shop_rank(phone.url))
    return candidates[:MAX_PHONES]


async def duckduckgo_search(page, query: str) -> str:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    try:
        await page.goto(url, timeout=60_000, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        for _ in range(2):
            await _smooth_scroll(page, 700)
            await page.wait_for_timeout(1000)
        text = await page.evaluate("document.body.innerText")
        return (text or "")[:15_000]
    except Exception as exc:
        logger.warning("DuckDuckGo search failed: %s", exc)
        return ""
