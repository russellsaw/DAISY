"""Thin wrapper around the Claude API.

Two entry points:
  chat()       - free-form text over a message history
  structured() - a JSON object guaranteed to match a schema, via structured outputs
                 (output_config.format). This is how every agent returns
                 machine-readable output.

Model notes (Sonnet 5.5 / Haiku 5.5):
  - Non-default `temperature` is rejected with a 400, so it is never sent.
  - Forced tool_choice is rejected on Sonnet 5.5, so structured() uses structured
    outputs instead of a forced tool call — it works on whichever model is configured.
  - Thinking is adaptive by default and counts against max_tokens; `effort` is the
    latency/quality lever. Routing and short replies run at low effort; compliance review, product
    answers and narration at medium.
  - Sonnet 5.5 requests opt into server-side refusal fallbacks ("default" routing).
    If a whole chain still refuses, Refusal is raised and the pipeline answers with
    the banker handoff instead of an error.
"""
from __future__ import annotations

import copy
import json
from typing import Any

import anthropic

from . import config

_client: anthropic.Anthropic | None = None

# Models that accept the server-side `fallbacks: "default"` parameter on the Claude API.
_FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class Refusal(RuntimeError):
    """The model (and any fallback) declined the request."""


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        # A stuck request should fail within a turn's patience, not after the SDK's 10-minute default.
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=config.LLM_TIMEOUT_S)
    return _client


def complete(
    system: str,
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    max_tokens: int = 2048,
    effort: str | None = None,
    output_format: dict | None = None,
):
    model = model or config.MODEL_MAIN
    kwargs: dict[str, Any] = dict(model=model, system=system, messages=messages, max_tokens=max_tokens)
    output_config: dict[str, Any] = {}
    if effort:
        output_config["effort"] = effort
    if output_format:
        output_config["format"] = output_format
    if output_config:
        kwargs["output_config"] = output_config

    if model in _FALLBACK_MODELS:
        response = client().beta.messages.create(**kwargs, betas=[_FALLBACK_BETA], fallbacks="default")
    else:
        response = client().messages.create(**kwargs)

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise Refusal(f"model declined ({getattr(details, 'category', None) or 'no category'})")
    return response


def text_of(response) -> str:
    return "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()


def strict_schema(schema: dict) -> dict:
    """Adapt a JSON schema for structured outputs: every object closed with
    additionalProperties=false, and `type: [a, b]` unions written as anyOf."""
    schema = copy.deepcopy(schema)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if isinstance(node.get("type"), list):
                types = node.pop("type")
                node["anyOf"] = [{"type": t} for t in types]
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node.setdefault("properties", {})
            for key in ("properties", "$defs"):
                if isinstance(node.get(key), dict):
                    node[key] = {k: walk(v) for k, v in node[key].items()}
            for key in ("items", "anyOf", "allOf"):
                if key in node:
                    node[key] = [walk(v) for v in node[key]] if isinstance(node[key], list) else walk(node[key])
        return node

    return walk(schema)


def structured(
    system: str,
    user_text: str,
    schema: dict,
    *,
    model: str | None = None,
    max_tokens: int = 4096,
    effort: str = "low",
) -> dict:
    """Return a dict matching `schema`, guaranteed by structured outputs."""
    response = complete(
        system,
        [{"role": "user", "content": user_text}],
        model=model or config.MODEL_FAST,
        max_tokens=max_tokens,
        effort=effort,
        output_format={"type": "json_schema", "schema": strict_schema(schema)},
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Structured output was cut off at max_tokens")
    return json.loads(text_of(response))


def chat(system: str, history: list[dict], *, model: str | None = None, max_tokens: int = 2048,
         effort: str = "low") -> str:
    """Plain conversational completion over a message history."""
    response = complete(system, history, model=model, max_tokens=max_tokens, effort=effort)
    return text_of(response)
