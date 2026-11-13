"""Optional LLM sentence picker for text answers. OFF by default: needs KNOWLEDGE_LLM=on and a key.

The rule-based search finds the candidate passages; Claude only chooses WHICH of their sentences
answer the question, by number. The answer is always the site's own sentences, so the model can't
invent a value, and anything unexpected (no SDK, no key, timeout, refusal, bad output) falls back
to the rule-based answer. Rate questions never come here: their numbers come from the rate tables.

    KNOWLEDGE_LLM=on                              opt in (measured: no accuracy gain on the public
                                                  eval, p95 latency 0.01 s -> 5.5 s; see NOTES.md)
    ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN)   then uses Claude (default claude-opus-5-5)
    GEMINI_API_KEY (or GOOGLE_API_KEY)            then uses Gemini (default gemini-4-hydrogen)
    KNOWLEDGE_LLM_PROVIDER=anthropic|gemini       picks one when both keys are set (default: anthropic)
    KNOWLEDGE_LLM_MODEL                           overrides the model id
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

DEFAULT_MODEL = {"anthropic": "claude-opus-5-5", "gemini": "gemini-4-hydrogen"}
TIMEOUT_S = 15.0
MAX_SENTENCES = 60

SYSTEM = (
    "You help a credit union's member assistant answer from its own website. You get a member's "
    "question and numbered sentences taken from the website. Pick the sentences that directly "
    "answer the question: usually one to three. If two sentences are needed together (a rule and "
    "the places it applies to), pick both. If none of them answers it, return an empty list. "
    "Never pick a sentence only because it shares words with the question."
)
SCHEMA = {
    "type": "object",
    "properties": {"sentence_ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["sentence_ids"],
    "additionalProperties": False,
}

_client = None
_cache: dict[tuple, Optional[list[int]]] = {}


def _has_sdk(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def provider() -> Optional[str]:
    """Which backend runs, or None (then every answer is rule-based)."""
    if os.environ.get("KNOWLEDGE_LLM", "").lower() not in ("on", "1", "true"):
        return None                              # opt-in only: a key alone doesn't turn it on
    ready = {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
                     and _has_sdk("anthropic"),
        "gemini": bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
                  and _has_sdk("google.genai"),
    }
    wanted = os.environ.get("KNOWLEDGE_LLM_PROVIDER", "").lower()
    if wanted in ready:
        return wanted if ready[wanted] else None
    return next((p for p in ("anthropic", "gemini") if ready[p]), None)


def enabled() -> bool:
    return provider() is not None


def model() -> str:
    return os.environ.get("KNOWLEDGE_LLM_MODEL") or DEFAULT_MODEL.get(provider() or "", "")


def _get_client():
    global _client
    if _client is None:
        if provider() == "gemini":
            from google import genai
            _client = genai.Client()             # reads GEMINI_API_KEY / GOOGLE_API_KEY
        else:
            import anthropic
            _client = anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=1)
    return _client


def split_sentences(text: str) -> list[str]:
    out = []
    for line in text.split("\n"):
        out += [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line) if s.strip()]
    return out


def pick(question: str, passages: list[str]) -> Optional[list[str]]:
    """Sentences (verbatim, in page order) that answer the question; None = use the rule answer."""
    sents = []
    for p in passages:
        sents += split_sentences(p)
    sents = sents[:MAX_SENTENCES]
    if not sents:
        return None
    key = (question.strip().lower(), tuple(sents))
    if key not in _cache:                       # the same question always gets the same answer
        _cache[key] = _ask(question, sents)
    ids = _cache[key]
    return [sents[i] for i in ids] if ids else None


def _ask(question: str, sents: list[str]) -> Optional[list[int]]:
    numbered = "\n".join(f"[{i}] {s}" for i, s in enumerate(sents))
    prompt = f"Question: {question}\n\nSentences:\n{numbered}"
    text = _ask_gemini(prompt) if provider() == "gemini" else _ask_claude(prompt)
    if text is None:
        return None
    try:
        ids = json.loads(text)["sentence_ids"]
    except (ValueError, KeyError, TypeError):
        return None
    ids = sorted({i for i in ids if isinstance(i, int) and 0 <= i < len(sents)})
    return ids[:4] or None


def _ask_claude(prompt: str) -> Optional[str]:
    import anthropic
    try:
        response = _get_client().beta.messages.create(
            model=model(),
            max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.RateLimitError,
            anthropic.APIStatusError):
        return None
    if response.stop_reason != "end_turn":      # refusal, max_tokens, ...
        return None
    return next((b.text for b in response.content if b.type == "text"), None)


def _ask_gemini(prompt: str) -> Optional[str]:
    try:
        interaction = _get_client().interactions.create(
            model=model(),
            input=f"{SYSTEM}\n\n{prompt}",
            response_format={"type": "text", "mime_type": "application/json", "schema": SCHEMA},
            timeout=TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - API, network (httpx2/requests) or parsing errors alike: an
        return None       # optional layer must never break an answer; the rule answer is used instead
    return getattr(interaction, "output_text", None) or None
