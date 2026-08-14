"""Small, backend-only Groq JSON client used by runtime agents."""

import json
import os

import httpx


class GroqError(RuntimeError):
    """A controlled Groq transport or response error."""


def generate_json_with_usage(system_prompt, payload, *, temperature=0.1):
    """Return (parsed_json, safe_usage). Invalid JSON is never repaired."""
    api_key = os.getenv("GROQ_API_KEY")
    model = os.getenv("GROQ_MODEL")
    if not api_key or not model:
        raise GroqError("Groq is not configured")

    body = {
        "model": model,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload)},
        ],
    }
    try:
        response = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        result = json.loads(content)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise GroqError("Groq returned no valid JSON output") from exc
    if not isinstance(result, dict):
        raise GroqError("Groq JSON output must be an object")
    usage = data.get("usage") or {}
    return result, {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
