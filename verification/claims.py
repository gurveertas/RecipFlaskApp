"""Extract factual claims from a source and tag each with its origin.

v1 uses deterministic heuristics (no API key needed): statistics (numbers,
percentages, currency, CVE IDs) and direct quotes are the claims most worth
verifying and most dangerous to get wrong. An optional LLM-backed extractor
(Anthropic) can be enabled for richer claim detection — see llm_extract().
"""
from __future__ import annotations

import re

from retrieval.models import Source, Claim
from config import load_settings

# ---- lightweight sentence splitter (avoids a heavy NLP dependency) ----------
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")

_NUMBER = re.compile(r"(?:\$|£|€|₹|INR|AUD|USD|SGD)?\s?\d[\d,]*(?:\.\d+)?%?")
_CURRENCY = re.compile(r"(?:\$|£|€|₹|INR|AUD|USD|SGD)\s?\d[\d,]*")
_PERCENT = re.compile(r"\d[\d,]*(?:\.\d+)?\s?%")
_CVE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)
_QUOTE = re.compile(r"[\"“”]([^\"“”]{12,240})[\"“”]")
# crude entity grab: runs of Capitalised Words, CVE ids, vendor product names
_ENTITY = re.compile(r"\b(?:[A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+){0,3})\b")


def _split_sentences(text: str) -> list[str]:
    # collapse whitespace, split on sentence boundaries
    text = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in _SENT_SPLIT.split(text) if len(s.strip()) > 20]


def _is_security_claim(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in keywords)


def _numbers_in(text: str) -> list[str]:
    nums = set()
    for rx in (_CURRENCY, _PERCENT, _CVE):
        nums.update(m.group(0).strip() for m in rx.finditer(text))
    return sorted(nums)


def _entities_in(text: str) -> list[str]:
    ents = {m.group(0) for m in _ENTITY.finditer(text)}
    ents.update(m.group(0).upper() for m in _CVE.finditer(text))
    # drop single very common words that slip through
    stop = {"The", "This", "These", "Those", "According", "It", "As", "In", "For"}
    return sorted(e for e in ents if e not in stop and len(e) > 2)


def extract_claims(source: Source) -> list[Claim]:
    settings = load_settings()
    sec_kw = settings["corroboration"]["security_keywords"]
    claims: list[Claim] = []

    for sent in _split_sentences(source.text):
        has_number = bool(_NUMBER.search(sent))
        has_cve = bool(_CVE.search(sent))
        quote_match = _QUOTE.search(sent)

        claim_type = None
        if quote_match:
            claim_type = "quote"
        elif has_number or has_cve:
            claim_type = "stat"

        if claim_type is None:
            continue  # only extract verifiable stat/quote sentences in v1

        claims.append(Claim(
            text=sent,
            claim_type=claim_type,
            source_url=source.url,
            source_domain=source.domain,
            source_tier=source.tier,
            is_vendor_source=source.is_vendor,
            numbers=_numbers_in(sent),
            entities=_entities_in(sent)[:8],
            is_security_claim=_is_security_claim(sent, sec_kw),
        ))
    return claims


# ---------------------------------------------------------------------------
# Optional: LLM-backed extraction for richer, cleaner claims.
# Enabled only if ANTHROPIC_API_KEY is set AND caller opts in. Falls back to
# the heuristic extractor on any error so the pipeline never hard-depends on it.
# ---------------------------------------------------------------------------
def llm_extract(source: Source) -> list[Claim] | None:
    import os
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic, json
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic()
        prompt = (
            "Extract the checkable factual claims (statistics, dated facts, "
            "attributed quotes) from the article text below. Return ONLY a JSON "
            "array of objects with keys: text, claim_type (stat|quote|fact), "
            "is_security_claim (bool). No prose.\n\n" + source.text[:6000]
        )
        msg = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in msg.content if b.type == "text")
        data = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
        out = []
        for d in data:
            out.append(Claim(
                text=d["text"], claim_type=d.get("claim_type", "fact"),
                source_url=source.url, source_domain=source.domain,
                source_tier=source.tier, is_vendor_source=source.is_vendor,
                numbers=_numbers_in(d["text"]), entities=_entities_in(d["text"])[:8],
                is_security_claim=bool(d.get("is_security_claim")),
            ))
        return out
    except Exception:
        return None  # graceful fallback to heuristic
