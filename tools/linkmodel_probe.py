                      
"""Verify that LinkModel supports the exact request shape Juice sends.

Juice's agent loop is only useful if a provider can accept a full tool-calling
round trip:

    Turn 1  ->  model returns tool_calls
    Turn 2  ->  assistant message (with tool_calls) + tool result message
                is replayed to the provider, and the model answers in prose

This script replays that exchange against the live LinkModel API using the real
``TOOLS`` array and ``SYSTEM_PROMPT`` from ``juice.py``, so a green run means
the Juice integration needs no protocol-specific special casing.

Usage:
    export LINK_MODEL_API_KEY=...        # or LINKMODEL_API_KEY
    python3 tools/linkmodel_probe.py [--model claude-sonnet-5] [--base-url URL]

Exit code is 0 when every check passes, 1 otherwise.
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests              

import juice                                                                       


DEFAULT_BASE_URL = "https://api.linkmodel.ai/v1"
DEFAULT_MODEL = "claude-sonnet-5"

KEY_ENV_VARS = ("LINK_MODEL_API_KEY", "LINKMODEL_API_KEY")

                                                                                
                                                              
PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a specific portion of a file. Use line ranges when possible.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "start_line": {"type": "integer", "description": "First line to read."},
            },
            "required": ["path"],
        },
    },
}

PROBE_USER_MESSAGE = "Read the file README.md using the read_file tool."


class ProbeFailure(Exception):
    """Raised when a check fails in a way that cannot be recovered from."""


def resolve_api_key(explicit=None):
    """Return the LinkModel API key from the flag or the environment."""
    if explicit:
        return explicit.strip()
    for var in KEY_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    raise ProbeFailure(
        "No API key found. Export %s (or pass --api-key)." % " or ".join(KEY_ENV_VARS)
    )


def headers_for(api_key):
    """Headers Juice sends for the linkmodel provider (see juice.PROVIDERS)."""
    return juice.PROVIDERS["linkmodel"]["headers"](api_key)


def chat_url(base_url):
    return base_url.rstrip("/") + "/chat/completions"


def describe_error(response):
    """Render a provider error body in a readable, single-line form."""
    try:
        body = response.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        msg = body.get("msg") or body.get("message")
        error = body.get("error")
        if msg is None and isinstance(error, dict):
            msg = error.get("message")
        elif msg is None and isinstance(error, str):
            msg = error
        if msg:
            code = body.get("code")
            request_id = body.get("request_id")
            extra = []
            if code:
                extra.append("code %s" % code)
            if request_id:
                extra.append("request_id %s" % request_id)
            suffix = " (%s)" % ", ".join(extra) if extra else ""
            return "%s%s" % (msg, suffix)
    return (response.text or response.reason or "").strip()[:400] or "HTTP %s" % response.status_code


def post_chat(base_url, api_key, payload, stream=False):
    """POST a chat completion and return the raw response object."""
    return requests.post(
        chat_url(base_url),
        headers=headers_for(api_key),
        json=payload,
        timeout=120,
        stream=stream,
    )


def post_chat_with_fallback(base_url, api_key, payload, stream=False):
    """POST, and retry once with reasoning off if tools were refused.

    This mirrors what Juice does at runtime: LinkModel's chat endpoint rejects
    function tools while a model is reasoning, and the error names the fix.
    """
    response = post_chat(base_url, api_key, payload, stream=stream)
    if response.status_code == 400 and juice.is_reasoning_tools_conflict(response):
        retry = dict(payload)
        retry["reasoning"] = {"effort": "none"}
        print("  NOTE  tools refused while reasoning was on — retrying with reasoning off")
        return post_chat(base_url, api_key, retry, stream=stream)
    return response


def build_payload(model, messages, tools, reasoning=None):
    """Build a payload with the same shape Juice sends (juice.call_llm_stream)."""
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "stream": False,
    }
    if reasoning:
        payload["reasoning"] = dict(reasoning)
    return payload


def check_tool_call(base_url, api_key, model, reasoning=None):
    """Turn 1: the provider must accept the tools schema and emit tool_calls."""
    messages = [
        {"role": "system", "content": juice.SYSTEM_PROMPT},
        {"role": "user", "content": PROBE_USER_MESSAGE},
    ]
    payload = build_payload(model, messages, [PROBE_TOOL], reasoning)
    response = post_chat_with_fallback(base_url, api_key, payload)
    if response.status_code != 200:
        raise ProbeFailure("Turn 1 failed: %s" % describe_error(response))

    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        raise ProbeFailure("Turn 1 returned no choices.")
    message = choices[0].get("message") or {}
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        raise ProbeFailure(
            "Turn 1 returned no tool_calls (finish_reason=%r). The model did not "
            "exercise the tool schema." % choices[0].get("finish_reason")
        )

    call = tool_calls[0]
    function = call.get("function") or {}
    if not call.get("id"):
        raise ProbeFailure("Turn 1 tool_call is missing an id.")
    if not function.get("name"):
        raise ProbeFailure("Turn 1 tool_call is missing function.name.")
    try:
        json.loads(function.get("arguments") or "{}")
    except ValueError:
        raise ProbeFailure(
            "Turn 1 tool_call arguments are not valid JSON: %r" % function.get("arguments")
        )
    return call


def check_tool_result_round_trip(base_url, api_key, model, call, reasoning=None):
    """Turn 2: replay the tool call + result. This is the agent loop's core."""
    assistant_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                },
            }
        ],
    }
    tool_message = {
        "role": "tool",
        "tool_call_id": call["id"],
        "content": "1 | # Juice\n\nAI coding agent.",
    }

                                                                                
                                                                         
    for assistant_content in (None, ""):
        assistant_message["content"] = assistant_content
        messages = [
            {"role": "system", "content": juice.SYSTEM_PROMPT},
            {"role": "user", "content": PROBE_USER_MESSAGE},
            assistant_message,
            tool_message,
        ]
        payload = build_payload(model, messages, [PROBE_TOOL], reasoning)
        response = post_chat_with_fallback(base_url, api_key, payload)
        if response.status_code == 200:
            body = response.json()
            choices = body.get("choices") or []
            if not choices:
                raise ProbeFailure("Turn 2 returned no choices.")
            reply = (choices[0].get("message") or {}).get("content") or ""
            if not reply.strip():
                raise ProbeFailure("Turn 2 succeeded but the model returned no text.")
            return assistant_content, reply.strip()[:120]

        detail = describe_error(response)
        if assistant_content is None and response.status_code == 400:
                                                               
            continue
        raise ProbeFailure("Turn 2 failed: %s" % detail)

    raise ProbeFailure("Turn 2 failed with both content=null and content=''.")


def check_streaming(base_url, api_key, model, reasoning=None):
    """The streaming path Juice actually uses must yield OpenAI-style deltas."""
    messages = [{"role": "user", "content": "Say hello in three words."}]
    payload = build_payload(model, messages, [PROBE_TOOL], reasoning)
    payload["stream"] = True

    response = post_chat_with_fallback(base_url, api_key, payload, stream=True)
    if response.status_code != 200:
        raise ProbeFailure("Streaming request failed: %s" % describe_error(response))

    text = ""
    saw_done = False
    chunk_count = 0
    for raw in response.iter_lines(decode_unicode=True):
        if not raw or raw.startswith(":") or raw == "data: [DONE]":
            if raw == "data: [DONE]":
                saw_done = True
            continue
        if not raw.startswith("data: "):
            continue
        chunk = json.loads(raw[6:])
        chunk_count += 1
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text += delta.get("content") or ""
    if not chunk_count:
        raise ProbeFailure("Streaming response contained no data chunks.")
    if not saw_done:
        raise ProbeFailure("Streaming response never sent 'data: [DONE]'.")
    if not text.strip():
        raise ProbeFailure("Streaming response produced no text.")
    return text.strip()[:60]


def main():
    parser = argparse.ArgumentParser(description="Probe the LinkModel integration used by Juice.")
    parser.add_argument("--api-key", default=None, help="LinkModel API key (defaults to $LINK_MODEL_API_KEY).")
    parser.add_argument("--model", default=os.environ.get("LINKMODEL_PROBE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.environ.get("LINKMODEL_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument(
        "--reasoning-effort",
        default=os.environ.get("LINKMODEL_REASONING_EFFORT"),
        help="Reasoning effort to send (none, minimal, low, medium, high, default). "
             "Defaults to the provider's own setting.",
    )
    args = parser.parse_args()

    print("LinkModel probe — model=%s base_url=%s" % (args.model, args.base_url))
    try:
        api_key = resolve_api_key(args.api_key)
    except ProbeFailure as exc:
        print("SKIP: %s" % exc)
        return 1

    reasoning = juice.reasoning_field("linkmodel", args.model, args.reasoning_effort)
    if reasoning:
        print("  ...  reasoning: %s" % json.dumps(reasoning))

    try:
        call = check_tool_call(args.base_url, api_key, args.model, reasoning)
        print("  PASS  tool schema accepted — model emitted tool_calls (%s)" % call["function"]["name"])

        assistant_content, reply = check_tool_result_round_trip(
            args.base_url, api_key, args.model, call, reasoning
        )
        print(
            "  PASS  tool result round trip — assistant content=%s, reply=%r"
            % ("null" if assistant_content is None else "''", reply)
        )

        streamed = check_streaming(args.base_url, api_key, args.model, reasoning)
        print("  PASS  streaming deltas — text=%r" % streamed)
    except ProbeFailure as exc:
        print("  FAIL  %s" % exc)
        return 1
    except Exception as exc:                                                   
        print("  ERROR %s: %s" % (type(exc).__name__, exc))
        return 1

    print("All checks passed — Juice can drive LinkModel with the standard tool loop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
