"""Advisory prose review — the one LLM-judged check in this codebase.

Everything in synthesis/validate.py is deterministic on purpose: the same
draft must produce the same verdict every time, and a gate whose answer
depends on a model's mood or on remaining quota is not a gate. That rules out
grammar checking, which genuinely needs a language model.

So this lives OUTSIDE the harness:

  * run on demand from a button, never automatically and never inside the
    write/retry loop — a call there would triple the cost of every generation
    and make QA non-reproducible;
  * advisory only. It has no `passed` field and nothing consults it before
    publishing. It cannot block an article, by construction rather than by
    convention;
  * body only. The FAQ and CTA are short and formulaic, and excluding them
    cuts the token cost for no real loss.

The output is quoted-with-suggestion rather than an auto-applied rewrite,
because a model reviewing prose WILL flag correct technical phrasing — vendor
terminology is exactly where that happens. A human decides.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Citation markers and fenced code are stripped before review: [S3] is not a
# typo, and a model asked to proofread will reliably say it is.
# Combined markers ("[S4, S5]") are stripped too. Left in, they reached the
# proofreader as ordinary text, which is exactly the "typo" it was told not to
# report — and applying such a suggestion would corrupt the citation.
_CITATION = re.compile(r"\[\s*S\d+(?:\s*(?:,|;|and)\s*S\d+)*\s*\]", re.I)
_FENCED = re.compile(r"```.*?```", re.DOTALL)

# Guard against sending an unbounded body if something upstream produced one.
# ~24k characters is roughly 6k tokens, well beyond any article this writes.
_MAX_CHARS = 24_000

PROSE_SYSTEM_PROMPT = """\
You are a copy editor for Red Education, an authorised IT training company. \
You are reviewing one already-written article body for LANGUAGE errors only.

Report ONLY these, and nothing else:
  - grammar errors (subject-verb disagreement, broken tense, mangled clauses)
  - spelling errors in ordinary English words
  - sentences that are garbled or unfinished
  - register slips too informal for a professional training audience \
("stuff", "a bunch of", "super easy")

Do NOT report, ever:
  - technical terminology, product names, certification codes, exam codes or \
version numbers you do not recognise. This is specialist vendor vocabulary \
and it is correct even when it looks wrong. CCTE, R82, PCNSE, BIG-IP, EDU-210 \
and similar are real and correctly spelled.
  - British spelling ("optimise", "centre", "licence"). It is house style.
  - style preferences: sentence length, passive voice, word choice, how the \
article is structured, what it covers, or anything about SEO.
  - anything you would phrase differently but which is not an error.

If the prose is clean, return an empty list. An empty list is a perfectly \
good answer and is expected more often than not — do not invent problems to \
appear useful.

Respond with JSON only, no prose, no markdown fences:
{"issues": [
  {"quote": "the exact text from the article, 3-12 words, copied verbatim",
   "problem": "what is wrong, in one short sentence",
   "suggestion": "the corrected text for that quote",
   "kind": "grammar|spelling|garbled|register"}
]}
Report at most 8 issues, most serious first. Every "quote" MUST appear \
verbatim in the article — it is how the editor finds the sentence."""


@dataclass
class ProseIssue:
    quote: str
    problem: str
    suggestion: str
    kind: str


def _prepare(body_markdown: str) -> str:
    text = _FENCED.sub("", body_markdown or "")
    text = _CITATION.sub("", text)
    return text.strip()[:_MAX_CHARS]


def review_prose(body_markdown: str,
                 vendor_name: str = "") -> tuple[list[ProseIssue], str]:
    """Review the article body. Returns (issues, model_used).

    Raises on a provider/parse failure — the caller turns that into a message
    rather than showing a silent empty result, because "no issues found" and
    "the review did not run" must never look the same.
    """
    from .llm import complete_with_configured_provider, extract_json

    text = _prepare(body_markdown)
    if not text:
        return [], ""

    prompt = (f"Vendor: {vendor_name}\n\n" if vendor_name else "") + \
             "Article body:\n\n" + text
    raw, model = complete_with_configured_provider(
        PROSE_SYSTEM_PROMPT, prompt, purpose="prose_review")

    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Prose review returned an unexpected shape.")

    issues: list[ProseIssue] = []
    for item in (parsed.get("issues") or [])[:8]:
        if not isinstance(item, dict):
            continue
        quote = (item.get("quote") or "").strip()
        # Drop anything whose quote is not actually in the article. A quote
        # the editor cannot find is worse than no finding at all, and a
        # paraphrased "quote" is the most common way this output goes wrong.
        if not quote or quote not in body_markdown:
            continue
        issues.append(ProseIssue(
            quote=quote,
            problem=(item.get("problem") or "").strip(),
            suggestion=(item.get("suggestion") or "").strip(),
            kind=(item.get("kind") or "grammar").strip().lower(),
        ))
    return issues, model
