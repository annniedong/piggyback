"""AI page reading for the extension: sale / coupon detection (POST /sale) and cart reading (POST /cart).

The extension (extension/sale.js) sends text from a shopping page: the top of the page, lines that
mention sales or codes, and struck-through prices. An LLM decides whether a real sale, discount,
coupon, or promo code is on offer and writes a short Piggyback line. The extension speaks it.

POST /sale
  in:  {"url", "title", "text", "keyword_lines": [...], "struck_prices": [...]}
  out: {"on_sale": bool, "kind": str, "discount": str, "urgency": bool, "line": str, "error": str|null}

Uses the same LLM_PROVIDER / GEMINI_* / ANTHROPIC_API_KEY env vars as brain.py. With
LLM_PROVIDER=mock (or if the LLM call fails) a keyword check is used instead.
"""

import asyncio
import json
import os
import re

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field


def load_env_file(path: Path = Path(__file__).parent / ".env") -> None:
    """Load KEY=value lines from piggyback/.env (git-ignored) without overriding the real environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m and not line.lstrip().startswith("#"):
            os.environ.setdefault(m.group(1), m.group(2).strip("'\""))


load_env_file()
# GEMINI_API_KEY_WEB is the key for the web-scraping checks here. If no separate GEMINI_API_KEY is
# set, the checkout chat in brain.py uses the same key.
if os.getenv("GEMINI_API_KEY_WEB") and not os.getenv("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = os.environ["GEMINI_API_KEY_WEB"]

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")
# Fast, cheap model for the web-scraping checks (gemini-3.5-flash-lite answered in ~1 s in testing).
GEMINI_MODEL = os.getenv("GEMINI_WEB_MODEL", "gemini-3.5-flash-lite")
SALE_MODEL = os.getenv("SALE_MODEL", os.getenv("EXECUTOR_MODEL", "claude-sonnet-5-5"))  # when LLM_PROVIDER=claude
TIMEOUT_S = 5  # flash-lite answers in ~1 s; give up fast and use the local result
# When the web-scraping checks (/sale, /cart) call the LLM. The checkout chat in brain.py always does.
#   auto   (default) free local check first; LLM only when that check isn't sure
#   always LLM on every check (best quality, uses the most quota)
#   never  local checks only
SCRAPE_AI = os.getenv("SCRAPE_AI", "auto").lower()

KINDS = ["sale", "coupon", "promo_code", "clearance", "bogo", "flash_sale", "none"]

SYSTEM = """You are Piggyback, a robot piggy bank that keeps its owner from impulse buying.
You get text scraped from one shopping web page. Decide whether the page is actively offering a
sale, discount, coupon, or promo code that could push someone to buy.

Counts: percent or dollar off, sale/clearance prices, struck-through original prices next to lower
prices, promo or coupon codes, buy-one-get-one, flash sales and countdowns.
Does NOT count on its own: free shipping thresholds, "sign up for 10% off" newsletter pop-ups,
rewards-program ads, gift cards, or the word "deals" in a navigation menu.

Reply with JSON:
- on_sale: true only if a real offer is on this page.
- kind: one of sale, coupon, promo_code, clearance, bogo, flash_sale, none.
- discount: the offer in a few words, like "30% off sitewide" or "code SAVE20". Empty if none.
- urgency: true if it pressures with time limits ("ends tonight", countdowns, "only 2 left").
- line: what Piggyback says out loud, under 20 words, dry and funny, reminding them a discount is
  still spending. Mention the actual offer. No emoji. Empty if on_sale is false."""

SCHEMA = {
    "type": "object",
    "properties": {
        "on_sale": {"type": "boolean"},
        "kind": {"type": "string", "enum": KINDS},
        "discount": {"type": "string"},
        "urgency": {"type": "boolean"},
        "line": {"type": "string"},
    },
    "required": ["on_sale", "kind", "discount", "urgency", "line"],
    "additionalProperties": False,
}

router = APIRouter()
_cache: dict[str, dict] = {}


class SaleCheck(BaseModel):
    url: str
    title: str = ""
    text: str = Field("", max_length=6000)
    keyword_lines: list[str] = Field(default_factory=list, max_length=30)
    struck_prices: list[str] = Field(default_factory=list, max_length=20)


# ---------------------------------------------------------------- keyword fallback

PERCENT = re.compile(r"\b(\d{1,2})\s*%\s*off\b", re.I)
CODE = re.compile(r"\b(?:code|coupon|promo)\b[:\s]+([A-Z0-9]{4,15})\b")
IGNORE = re.compile(r"free shipping|sign up|newsletter|first order|rewards", re.I)


def keyword_check(req: SaleCheck) -> dict:
    """Local sale check. Adds "sure": False when only weak hints were found (a lone "Sale" word,
    which is often just a menu link, or struck-through prices with no stated offer)."""
    lines = [l for l in req.keyword_lines if not IGNORE.search(l)]
    blob = " ".join(lines)
    code = CODE.search(blob)
    pct = PERCENT.search(blob)
    urgency = bool(re.search(r"ends (today|tonight|soon)|limited time|hours? left|countdown|flash", blob, re.I))
    if code:
        return {"on_sale": True, "kind": "promo_code", "discount": f"code {code.group(1)}", "urgency": urgency,
                "line": f"A promo code. Saving money by spending money. Classic.", "sure": True}
    if pct or req.struck_prices or re.search(r"\b(sale|clearance|markdown)\b", blob, re.I):
        discount = f"{pct.group(1)}% off" if pct else ("marked-down prices" if req.struck_prices else "a sale")
        return {"on_sale": True, "kind": "sale", "discount": discount, "urgency": urgency,
                "line": f"{discount.capitalize()}. A discount is still spending. Put it back.", "sure": bool(pct)}
    # Nothing offer-like left after dropping free-shipping / sign-up lines.
    return {"on_sale": False, "kind": "none", "discount": "", "urgency": False, "line": "", "sure": not lines}


# ---------------------------------------------------------------- LLM

def build_prompt(req: SaleCheck) -> str:
    return (
        f"URL: {req.url}\nTitle: {req.title}\n\n"
        f"Struck-through prices: {', '.join(req.struck_prices) or '(none)'}\n\n"
        f"Lines mentioning offers:\n" + ("\n".join(f"- {l}" for l in req.keyword_lines) or "(none)") +
        f"\n\nTop of page:\n{req.text[:3000]}"
    )


async def ask_gemini(prompt: str, system: str = SYSTEM, schema: dict = SCHEMA) -> str:
    from google import genai
    from google.genai import types

    key = os.getenv("GEMINI_API_KEY_WEB") or os.getenv("GEMINI_API_KEY")
    resp = await genai.Client(api_key=key).aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema,
        ),
    )
    return resp.text or ""


async def ask_claude(prompt: str, system: str = SYSTEM, schema: dict = SCHEMA) -> str:
    import anthropic

    resp = await anthropic.AsyncAnthropic().beta.messages.create(
        model=SALE_MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        fallbacks="default",
        betas=["server-side-fallback-2026-07-01"],
    )
    texts = [b.text for b in resp.content if b.type == "text"]
    return texts[-1] if texts else ""


def clean(data: dict) -> dict:
    on_sale = bool(data.get("on_sale"))
    return {
        "on_sale": on_sale,
        "kind": data.get("kind") if data.get("kind") in KINDS else ("sale" if on_sale else "none"),
        "discount": str(data.get("discount", ""))[:80],
        "urgency": bool(data.get("urgency")),
        "line": str(data.get("line", "")).strip()[:200] if on_sale else "",
    }


@router.post("/sale")
async def check_sale(req: SaleCheck):
    key = req.url + "|" + str(hash((req.text[:3000], tuple(req.keyword_lines), tuple(req.struck_prices))))
    if key in _cache:
        return _cache[key]

    error = None
    local = keyword_check(req)
    sure = local.pop("sure")
    use_ai = LLM_PROVIDER != "mock" and SCRAPE_AI != "never" and (SCRAPE_AI == "always" or not sure)
    if not use_ai:
        result = {**local, "method": "local"}
    else:
        try:
            ask = ask_claude if LLM_PROVIDER == "claude" else ask_gemini
            text = await asyncio.wait_for(ask(build_prompt(req)), TIMEOUT_S)
            result = clean(json.loads(text[text.index("{"): text.rindex("}") + 1]))
            if result["on_sale"] and not result["line"]:
                result["line"] = local["line"] or "A sale. Still spending. Put it back."
            result["method"] = "ai"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:200]
            result = {**local, "method": "local"}

    result = {**result, "error": error}
    print("Sale check:", req.url[:80], "->", result, flush=True)
    _cache[key] = result
    return result


# ---------------------------------------------------------------- cart reading (any store)
#
# POST /cart
#   in:  {"url", "title", "text", "heuristic": {"count", "total"}}   text = the cart page or cart drawer
#   out: {"is_cart": bool, "count": int|null, "total": float|null, "method": "ai"|"guess", "error"}
#
# The extension never sends checkout/payment pages (they have addresses and card fields).

CART_SYSTEM = """You read the text of a shopping cart (a cart/bag page, or a slide-out cart drawer)
from any online store and extract two numbers.

- is_cart: true if this text is a shopping cart/bag/basket (including an empty one).
- item_count: total quantity of items in the cart (sum the quantities; "Qty 2" counts as 2).
  Do NOT count recommendations ("you may also like", "recently viewed"), saved-for-later or
  wishlist items. 0 for an empty cart. -1 if you can't tell.
- subtotal: the cart subtotal as a plain number (before tax and shipping; if only an order total
  is shown, use it). Use the currency shown, no symbol. 0 for an empty cart. -1 if you can't tell.
- currency: like USD, EUR, GBP; "" if unknown."""

CART_SCHEMA = {
    "type": "object",
    "properties": {
        "is_cart": {"type": "boolean"},
        "item_count": {"type": "integer"},
        "subtotal": {"type": "number"},
        "currency": {"type": "string"},
    },
    "required": ["is_cart", "item_count", "subtotal", "currency"],
    "additionalProperties": False,
}

_cart_cache: dict[str, dict] = {}

# Text-based fallback (no AI): used when the LLM is off/failing and the extension's guess is missing.
NOT_CART = re.compile(r"you may also like|recently viewed|recommended for you|saved for later|"
                      r"complete the look|wish ?list|customers also|frequently bought", re.I)
# Prices like "$1,234.50", "USD 1,234.50", "1,234.50 USD", "1.234,50 €", "€ 84", "£36".
MONEY_RE = re.compile(
    r"(?:[$€£]|\b(?:USD|EUR|GBP|CAD|AUD)\b)\s*([0-9][0-9.,\s]*[0-9]|[0-9])"
    r"|([0-9][0-9.,\s]*[0-9]|[0-9])\s*(?:[$€£]|\b(?:USD|EUR|GBP|CAD|AUD)\b)")


def parse_amount(raw: str) -> float | None:
    """'1,234.50' -> 1234.5, '1.234,50' -> 1234.5, '3 200,00' -> 3200.0, '84' -> 84.0"""
    n = re.sub(r"\s", "", raw)
    if re.search(r",\d{2}$", n):            # European decimal comma
        n = n.replace(".", "").replace(",", ".")
    else:
        n = n.replace(",", "")
    try:
        return float(n)
    except ValueError:
        return None


def find_money(text: str) -> float | None:
    m = MONEY_RE.search(text)
    return parse_amount(m.group(1) or m.group(2)) if m else None
TOTAL_LINE = [re.compile(r"^\s*(estimated\s+)?subtotal\b", re.I),
              re.compile(r"^\s*(bag|cart|basket|order|merchandise|estimated|item)s?\s+total\b", re.I),
              re.compile(r"^\s*(total|grand total|amount)\b", re.I),
              re.compile(r"\b(subtotal|sub-total|total)\b", re.I)]  # last resort: label anywhere on a short line


def text_guess(text: str) -> dict:
    cut = NOT_CART.search(text)
    head = text[:cut.start()] if cut else text  # cart items come before recommendations
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    total = None
    for pattern in TOTAL_LINE:
        for i, line in enumerate(lines):
            if len(line) < 60 and pattern.search(line):
                amount = find_money(" ".join(lines[i:i + 3]))  # amount on the line or just after it
                if amount is not None:
                    total = amount
                    break
        if total is not None:
            break
    qtys = [int(q) for q in re.findall(r"\b(?:qty|quantity)\s*:?\s*(\d{1,3})\b", head, re.I)]
    if qtys:
        count = sum(qtys)
    else:
        m = re.search(r"\b(\d{1,3})\s+items?\b", text, re.I) or re.search(r"\b(?:bag|cart|basket)\s*\(\s*(\d{1,3})\s*\)", text, re.I)
        count = int(m.group(1)) if m else None
    if re.search(r"\b(your|shopping) (bag|cart|basket) is empty\b", text, re.I):
        count, total = 0, 0.0
    return {"count": count, "total": total}


class CartGuess(BaseModel):
    count: int | None = None
    total: float | None = None


class CartCheck(BaseModel):
    url: str
    title: str = ""
    text: str = Field("", max_length=8000)
    heuristic: CartGuess = Field(default_factory=CartGuess)


@router.post("/cart")
async def read_cart(req: CartCheck):
    key = str(hash(req.text))
    if key in _cart_cache:
        return _cart_cache[key]

    from_text = text_guess(req.text)
    h = req.heuristic
    guess = {"is_cart": True,
             "count": h.count if h.count is not None else from_text["count"],
             "total": h.total if h.total is not None else from_text["total"],
             "method": "guess"}
    # The local read is "sure" when it found both numbers and the two local readers don't disagree.
    disagree = (h.total is not None and from_text["total"] is not None and abs(h.total - from_text["total"]) > 0.01) or \
               (h.count is not None and from_text["count"] is not None and h.count != from_text["count"])
    sure = guess["count"] is not None and guess["total"] is not None and not disagree
    use_ai = (LLM_PROVIDER != "mock" and SCRAPE_AI != "never" and req.text.strip()
              and (SCRAPE_AI == "always" or not sure))
    error = None
    if not use_ai:
        result = guess
    else:
        try:
            ask = ask_claude if LLM_PROVIDER == "claude" else ask_gemini
            prompt = f"URL: {req.url}\nTitle: {req.title}\n\nCart text:\n{req.text[:7000]}"
            text = await asyncio.wait_for(ask(prompt, CART_SYSTEM, CART_SCHEMA), TIMEOUT_S)
            data = json.loads(text[text.index("{"): text.rindex("}") + 1])
            count = int(data.get("item_count", -1))
            total = float(data.get("subtotal", -1))
            result = {
                "is_cart": bool(data.get("is_cart")),
                # Fill in from the guess only where the AI couldn't tell.
                "count": count if count >= 0 else req.heuristic.count,
                "total": total if total >= 0 else req.heuristic.total,
                "method": "ai",
            }
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:200]
            result = guess

    result = {**result, "error": error}
    print("Cart read:", req.url[:80], "->", result, flush=True)
    _cart_cache[key] = result
    return result
