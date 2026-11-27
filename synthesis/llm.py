"""Shared LLM plumbing: call whichever provider has a key configured, and
parse its JSON response robustly.

Extracted out of synthesis/generate.py so synthesis/topics.py (Topic Engine)
can reuse the exact same "talk to a provider, get parseable JSON back" logic
the Writer already relies on, instead of duplicating it. Nothing here is
Draft-specific — that stays in generate.py.
"""
from __future__ import annotations

import json
import os

# Whichever provider has a key configured:
#   • Google Gemini (free tier)  — set GEMINI_API_KEY  (aistudio.google.com)
#   • Anthropic / Claude         — set ANTHROPIC_API_KEY
# Gemini is reached through its OpenAI-compatible endpoint, so ANY other
# OpenAI-compatible provider (Groq, OpenRouter, local Ollama) also works by
# setting LLM_BASE_URL / LLM_API_KEY / LLM_MODEL instead.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"   # override with GEMINI_MODEL


def complete_with_configured_provider(system_prompt: str, user_prompt: str,
                                      model: str | None = None,
                                      purpose: str = "llm") -> tuple[str, str]:
    """Call whichever LLM provider has a key configured; return (text, model).

    Order: an explicit OpenAI-compatible override (LLM_BASE_URL + LLM_API_KEY),
    then Google Gemini (GEMINI_API_KEY), then Anthropic (ANTHROPIC_API_KEY).
    The returned model name is for display/audit (e.g. Topic Engine records
    which model proposed each idea) — callers that don't care can do
    `text, _ = complete_with_configured_provider(...)`.

    `purpose` labels the call in the ApiUsage meter ("topics", "brief",
    "article", "section_rewrite", "adhoc") so /admin/usage can attribute
    spend per feature. Every successful call is recorded with the
    provider-reported token counts (which used to be discarded).
    """
    from config.db import record_api_usage

    if os.getenv("LLM_BASE_URL") and os.getenv("LLM_API_KEY"):
        used_model = model or os.getenv("LLM_MODEL") or DEFAULT_GEMINI_MODEL
        text, p_tok, c_tok = _complete_openai_compatible(
            system_prompt, user_prompt,
            base_url=os.getenv("LLM_BASE_URL"),
            api_key=os.getenv("LLM_API_KEY"),
            model=used_model,
        )
        record_api_usage("llm", purpose, model=used_model,
                         prompt_tokens=p_tok, completion_tokens=c_tok)
        return text, used_model
    if os.getenv("GEMINI_API_KEY"):
        used_model = model or os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
        text, p_tok, c_tok = _complete_openai_compatible(
            system_prompt, user_prompt,
            base_url=GEMINI_BASE_URL,
            api_key=os.getenv("GEMINI_API_KEY"),
            model=used_model,
        )
        record_api_usage("gemini", purpose, model=used_model,
                         prompt_tokens=p_tok, completion_tokens=c_tok)
        return text, used_model
    if os.getenv("ANTHROPIC_API_KEY"):
        used_model = model or DEFAULT_ANTHROPIC_MODEL
        text, p_tok, c_tok = _complete_anthropic(system_prompt, user_prompt, used_model)
        record_api_usage("anthropic", purpose, model=used_model,
                         prompt_tokens=p_tok, completion_tokens=c_tok)
        return text, used_model

    raise RuntimeError(
        "No LLM key set. Set GEMINI_API_KEY (free — aistudio.google.com) or "
        "ANTHROPIC_API_KEY for real generation, or use the offline/mock paths."
    )


def _complete_anthropic(system_prompt: str, user_prompt: str,
                        model: str) -> tuple[str, int, int]:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    usage = getattr(msg, "usage", None)
    return (text,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0)


def _complete_openai_compatible(system_prompt: str, user_prompt: str,
                                base_url: str, api_key: str,
                                model: str) -> tuple[str, int, int]:
    """Call any OpenAI-compatible chat endpoint (Gemini, Groq, OpenRouter, ...).
    Returns (text, prompt_tokens, completion_tokens) — token counts are the
    provider's own `usage` figures (0 when a provider omits them).

    JSON mode is requested so the model emits strictly-valid JSON — without it,
    models routinely leave unescaped quotes/newlines in a long text field,
    which breaks parsing. It's supported by the major compatible providers, and
    callers already ask for JSON in their prompt so the mode's requirements
    are met.
    """
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=api_key)
    # Headroom matters: "thinking" models (e.g. Gemini 2.5) spend part of the
    # token budget reasoning, so a tight cap can leave the JSON truncated/empty.
    resp = client.chat.completions.create(
        model=model,
        max_tokens=8192,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    usage = getattr(resp, "usage", None)
    return (resp.choices[0].message.content or "",
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0)


def extract_json(raw: str) -> dict:
    """Pull the first {...} JSON object out of a raw model response, with a
    repair fallback for the common case of a slightly-broken JSON string
    (an unescaped quote/newline in a long text field) even under JSON mode."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Model did not return JSON.")
    snippet = raw[start:end + 1]
    try:
        return json.loads(snippet)
    except json.JSONDecodeError:
        from json_repair import repair_json
        return json.loads(repair_json(snippet))
