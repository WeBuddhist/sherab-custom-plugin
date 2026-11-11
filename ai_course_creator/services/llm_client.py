"""
Thin wrapper around the AI model for the Sherab course-creator chatbot, reached through OpenRouter
(https://openrouter.ai), which serves Google's Gemini models (and others) behind one OpenAI-style API.

The bundled skill (``skill/SKILL.md`` + ``skill/references/*.md``) is used as the
system instruction so the model behaves exactly like the "Sherab" persona.

Credentials come from Django settings (``OPENROUTER_API_KEY`` / ``OPENROUTER_MODEL``),
which Tutor injects from ``config.yml``. See settings/common.py.

Plain HTTP calls with ``requests`` (no provider SDK): one call streams the chat reply, the other
returns a JSON object for the course generator.
"""

import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache

from django.conf import settings

log = logging.getLogger(__name__)

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(PLUGIN_DIR, "skill")

# Legacy markers — kept so existing callers don't break.
COURSE_JSON_START = "===COURSE_JSON_START==="
COURSE_JSON_END = "===COURSE_JSON_END==="

# Section-editor markers: the per-section editor emits the full desired section
# tree between these so the backend can apply it. Mirrors the COURSE_JSON idiom.
SECTION_EDITS_START = "===SECTION_EDITS_START==="
SECTION_EDITS_END = "===SECTION_EDITS_END==="

# Phase marker regex (lazy-initialised).
PHASE_MARKER_RE = None


def _phase_re():
    global PHASE_MARKER_RE  # pylint: disable=global-statement
    if PHASE_MARKER_RE is None:
        import re  # pylint: disable=import-outside-toplevel
        PHASE_MARKER_RE = re.compile(r"===SHERAB_PHASE:(\d+)===")
    return PHASE_MARKER_RE


def extract_phase_marker(text):
    """Return the phase number embedded in text, or None."""
    m = _phase_re().search(text)
    return int(m.group(1)) if m else None


def strip_phase_marker(text):
    """Remove ===SHERAB_PHASE:N=== markers from text."""
    return _phase_re().sub("", text).strip()


class LLMNotConfigured(Exception):
    """Raised when no API key is available."""


class LLMError(Exception):
    """Raised for an LLM API failure, with a user-friendly message."""


@lru_cache(maxsize=8)
def _load_system_instruction(skill_name="skill"):
    """
    Build the system instruction from a bundled skill directory.

    ``skill_name`` selects which skill dir under the plugin to load (e.g. "skill"
    for the 5-phase course creator, "skill_section_editor" for the per-section
    editor). Cached per skill_name for the process lifetime.
    """
    import re as _re  # pylint: disable=import-outside-toplevel
    parts = []
    skill_dir = os.path.join(PLUGIN_DIR, skill_name)
    skill_md = os.path.join(skill_dir, "SKILL.md")
    try:
        with open(skill_md, encoding="utf-8") as handle:
            raw = handle.read()
        stripped = _re.sub(r"^---\n.*?\n---\n", "", raw, flags=_re.DOTALL)
        parts.append(stripped)
    except OSError:
        log.exception("ai_course_creator: could not read SKILL.md at %s", skill_md)
        parts.append(
            "You are Sherab, a warm course-design companion inside Open edX Studio. "
            "Guide the creator through designing a course, one question at a time."
        )

    references_dir = os.path.join(skill_dir, "references")
    if os.path.isdir(references_dir):
        for filename in sorted(os.listdir(references_dir)):
            if not filename.endswith(".md"):
                continue
            try:
                with open(os.path.join(references_dir, filename), encoding="utf-8") as handle:
                    parts.append(f"\n\n# Reference: {filename}\n\n{handle.read()}")
            except OSError:
                log.warning("ai_course_creator: could not read reference %s", filename)

    return "\n".join(parts)


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"
# Shown in the OpenRouter account's activity list, to tell this app's use apart.
APP_TITLE = "WeBuddhist Sherab course creator"
# Connect / read timeouts (s). A long generation streams or answers within this.
REQUEST_TIMEOUT = (10, 180)


class LLMHTTPError(Exception):
    """OpenRouter (or the model behind it) answered with an error."""

    def __init__(self, status, message, retry_after=None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.retry_after = retry_after


def _get_api_key():
    key = getattr(settings, "OPENROUTER_API_KEY", "") or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise LLMNotConfigured(
            "OPENROUTER_API_KEY is not set. Add it to Tutor config.yml and re-run `tutor config save`."
        )
    return key


def _get_model_name():
    return (
        getattr(settings, "OPENROUTER_MODEL", "")
        or os.environ.get("OPENROUTER_MODEL", "")
        or DEFAULT_MODEL
    )


def _to_messages(history, materials_context="", system_instruction=""):
    """
    Convert our stored chat messages to OpenAI-style chat messages (system, user, assistant).
    The materials digest is prepended to the last user turn only; empty turns (an opening
    trigger) become "." since models refuse empty content.
    """
    result = [{"role": "system", "content": system_instruction}] if system_instruction else []
    for i, message in enumerate(history):
        role = message["role"] if isinstance(message, dict) else message.role
        content = message["content"] if isinstance(message, dict) else message.content
        if materials_context and i == len(history) - 1 and role == "user":
            content = (
                f"[Course materials the creator has shared so far:\n{materials_context}\n]\n\n{content}"
            )
        result.append({"role": "assistant" if role == "assistant" else "user", "content": content or "."})
    return result


def _post(body, stream=False):
    """POST a chat completion to OpenRouter; raises LLMHTTPError for an error answer."""
    import requests  # pylint: disable=import-outside-toplevel

    resp = requests.post(
        OPENROUTER_URL,
        json={**body, "model": _get_model_name(), "stream": stream},
        headers={"Authorization": f"Bearer {_get_api_key()}", "X-Title": APP_TITLE},
        timeout=REQUEST_TIMEOUT,
        stream=stream,
    )
    if resp.status_code != 200:
        try:
            message = resp.json().get("error", {}).get("message", "")
        except ValueError:
            message = resp.text[:300]
        raise LLMHTTPError(resp.status_code, message, _retry_after_header(resp.headers.get("retry-after")))
    return resp


def _retry_after_header(value):
    """Seconds to wait from a Retry-After header: a number of seconds or an HTTP date. None if missing or odd."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:  # HTTP dates are GMT
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _error_in(payload):
    """An error OpenRouter reports inside an answer (it can, even with HTTP 200)."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if error:
        return LLMHTTPError(error.get("code") or 500, error.get("message", ""))
    return None


def _is_rate_limit(exc):
    if _is_too_large(exc):
        return False
    if isinstance(exc, LLMHTTPError):
        return exc.status == 429
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text


def _is_server_unavailable(exc):
    """True for transient errors worth retrying: the provider down or busy, or the connection dropped."""
    import requests  # pylint: disable=import-outside-toplevel

    # ChunkedEncodingError: the connection was cut while the answer was streaming in.
    if isinstance(exc, (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)):
        return True
    return isinstance(exc, LLMHTTPError) and exc.status in (500, 502, 503, 504)


def _is_too_large(exc):
    text = str(exc).lower()
    return (
        (isinstance(exc, LLMHTTPError) and exc.status == 413)
        or "too large" in text
        or "context length" in text
        or "maximum context" in text
    )


def _retry_after_seconds(exc, default):
    """The wait OpenRouter suggests for a 429 (Retry-After), or ``default``."""
    return getattr(exc, "retry_after", None) or default


CHAT_MAX_TOKENS = 1024
# Section editor embeds a SECTION_EDITS JSON block; 8192 is generous without
# reserving a huge compute slot that triggers 503 high-demand errors.
SECTION_CHAT_MAX_TOKENS = 8192
RATE_LIMIT_MAX_WAIT = 65
JSON_MAX_RETRIES = 6
CHAT_MAX_RETRIES = 5
CHAT_RATE_LIMIT_MAX_WAIT = 30


def _stream_chunks(resp):
    """The text of an OpenRouter stream (server-sent events), chunk by chunk."""
    import json  # pylint: disable=import-outside-toplevel

    # The stream is UTF-8; without a charset in its Content-Type, requests would read text/* as Latin-1 and
    # garble every non-ASCII character (Tibetan included).
    resp.encoding = "utf-8"
    for line in resp.iter_lines(decode_unicode=True):
        if not line or line.startswith(":"):  # keep-alive comments, e.g. ": OPENROUTER PROCESSING"
            continue
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        error = _error_in(payload)
        if error:
            raise error
        for choice in payload.get("choices") or []:
            text = (choice.get("delta") or {}).get("content") or ""
            if text:
                yield text


def stream_reply(history, materials_context="", skill_name="skill", max_tokens=CHAT_MAX_TOKENS):
    """
    Stream the assistant's reply token-by-token.

    ``skill_name`` selects the system instruction (skill dir) to use.
    ``max_tokens`` caps the output (larger for the section editor, which embeds
    a desired section tree in its reply).

    Yields:
        str chunks of assistant text.
    """
    body = {
        "messages": _to_messages(history, materials_context, _load_system_instruction(skill_name)),
        "temperature": 0.7,
        "max_tokens": max_tokens,
    }

    last_exc = None
    for attempt in range(CHAT_MAX_RETRIES):
        sent_any = False
        try:
            with _post(body, stream=True) as resp:
                for text in _stream_chunks(resp):
                    sent_any = True
                    yield text
            return
        except (LLMError, LLMNotConfigured):
            raise
        except Exception as exc:  # pylint: disable=broad-except
            last_exc = exc
            if _is_too_large(exc):
                raise LLMError(
                    "There's too much material for one request. Remove or shorten some "
                    "uploaded materials and try again."
                ) from exc
            # Once part of the reply was shown, asking again would repeat it: stop with the error instead.
            if attempt < CHAT_MAX_RETRIES - 1 and not sent_any:
                if _is_rate_limit(exc):
                    wait = min(_retry_after_seconds(exc, default=8 * (attempt + 1)) + 1, CHAT_RATE_LIMIT_MAX_WAIT)
                    log.warning(
                        "ai_course_creator: AI model rate limited (chat), waiting %.1fs (attempt %d/%d)",
                        wait, attempt + 1, CHAT_MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                if _is_server_unavailable(exc):
                    wait = min(4 * (attempt + 1), 20)
                    log.warning(
                        "ai_course_creator: AI model unavailable (%s), retrying in %.1fs (attempt %d/%d)",
                        exc, wait, attempt + 1, CHAT_MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
            raise LLMError(_friendly_llm_error(exc)) from exc
    raise LLMError(_friendly_llm_error(last_exc))


def complete_json(
    history, instruction, materials_context="", temperature=0.4,
    system_instruction=None, max_tokens=None,
):
    """
    Make a single non-streaming call that must return a JSON object.

    Used by the course generator. ``system_instruction`` overrides the skill
    prompt for generation calls. ``max_tokens`` caps the completion size.

    Returns:
        str: the raw JSON text from the model.
    """
    sys_instr = system_instruction if system_instruction is not None else _load_system_instruction()
    messages = _to_messages(history, materials_context, sys_instr)
    messages.append({"role": "user", "content": instruction})
    body = {"messages": messages, "temperature": temperature, "response_format": {"type": "json_object"}}
    if max_tokens:
        body["max_tokens"] = max_tokens

    last_exc = None
    for attempt in range(JSON_MAX_RETRIES):
        try:
            payload = _post(body).json()
            error = _error_in(payload)
            if error:
                raise error
            choice = (payload.get("choices") or [{}])[0]
            return (choice.get("message") or {}).get("content") or ""
        except LLMNotConfigured:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            last_exc = exc
            if _is_too_large(exc):
                raise LLMError(_friendly_llm_error(exc)) from exc
            if attempt < JSON_MAX_RETRIES - 1 and (_is_rate_limit(exc) or _is_server_unavailable(exc)):
                wait = min(_retry_after_seconds(exc, default=10 * (attempt + 1)) + 1, RATE_LIMIT_MAX_WAIT)
                log.warning(
                    "ai_course_creator: AI model busy or unavailable (json: %s), waiting %.1fs (attempt %d/%d)",
                    exc, wait, attempt + 1, JSON_MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            raise LLMError(_friendly_llm_error(exc)) from exc
    raise LLMError(_friendly_llm_error(last_exc))


def _friendly_llm_error(exc):
    """Turn a raw API exception into a short, user-facing message."""
    if _is_too_large(exc):
        return (
            "There's too much material for one request. Remove or shorten some "
            "uploaded materials and try again."
        )
    if _is_rate_limit(exc):
        return "Sherab is busy right now — the AI service is rate-limited. Please wait a moment and try again."
    status = getattr(exc, "status", None)
    if status == 402:
        log.error("ai_course_creator: the OpenRouter account is out of credit: %s", exc)
        return "Sherab's AI service has run out of credit. Please contact your platform administrator."
    if status in (401, 403, 404):
        log.error("ai_course_creator: OpenRouter refused the key or model: %s", exc)
        return "Sherab couldn't connect to the AI service. Please contact your platform administrator."
    log.error("ai_course_creator: unexpected LLM error: %s", exc)
    return "Sherab ran into a problem. Please try again in a moment."


def _extract_json_block(text, start, end, label):
    """Pull a fenced JSON block delimited by ``start``/``end`` out of a message."""
    import json  # pylint: disable=import-outside-toplevel

    if start not in text or end not in text:
        return None
    try:
        raw = text.split(start, 1)[1].split(end, 1)[0].strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            raw = raw.rsplit("```", 1)[0]
        return json.loads(raw.strip())
    except (IndexError, ValueError):
        log.warning("ai_course_creator: failed to parse %s block", label)
        return None


def _strip_block(text, start, end):
    """Remove a block delimited by ``start``/``end`` so it is never shown to the user."""
    if start not in text:
        return text
    before = text.split(start, 1)[0]
    after = ""
    if end in text:
        after = text.split(end, 1)[1]
    return (before + after).strip()


def extract_course_json(text):
    """Pull the embedded COURSE_JSON block out of an assistant message."""
    return _extract_json_block(text, COURSE_JSON_START, COURSE_JSON_END, "COURSE_JSON")


def strip_course_json(text):
    """Remove the COURSE_JSON block so it is never shown to the user."""
    return _strip_block(text, COURSE_JSON_START, COURSE_JSON_END)


def extract_section_edits(text):
    """Pull the embedded SECTION_EDITS desired-tree block out of an assistant message."""
    return _extract_json_block(text, SECTION_EDITS_START, SECTION_EDITS_END, "SECTION_EDITS")


def strip_section_edits(text):
    """Remove the SECTION_EDITS block so it is never shown to the user."""
    return _strip_block(text, SECTION_EDITS_START, SECTION_EDITS_END)
