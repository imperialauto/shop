"""Current Groq model names. Mirrors messaging/src/lib/groqModel.js (issue #150).

Groq retired the Llama 3.3 / Llama 4 Scout IDs in 2026; an old ID configured in Railway
is mapped to a supported one instead of silently failing."""
import os

DEFAULT_MODEL = "openai/gpt-oss-120b"
RETIRED = {
    "llama-3.3-70b-versatile": DEFAULT_MODEL,
    "llama-3.1-8b-instant": "openai/gpt-oss-20b",
    "qwen/qwen3-32b": DEFAULT_MODEL,
    "meta-llama/llama-4-scout-17b-16e-instruct": DEFAULT_MODEL,
    "meta-llama/llama-4-maverick-17b-128e-instruct": DEFAULT_MODEL,
    "moonshotai/kimi-k2-instruct-0905": DEFAULT_MODEL,
}


def text_model() -> str:
    requested = (os.getenv("GROQ_MODEL") or os.getenv("GROQ_DRAFT_MODEL") or "").strip()
    return RETIRED.get(requested, requested or DEFAULT_MODEL)


def vision_model() -> str | None:
    """A screenshot-capable model only when the owner configured a current one."""
    requested = (os.getenv("GROQ_VISION_MODEL") or "").strip()
    return None if not requested or requested in RETIRED else requested
