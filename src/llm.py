"""Thin wrapper around the Claude API.

Two entry points:
  complete()   - free-form text, optionally with tools
  structured() - force a JSON object by making the model call a single tool
                 whose input_schema is the shape you want. This is how every
                 agent returns machine-readable output.
"""
from __future__ import annotations

from typing import Any

import anthropic

from . import config

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def complete(
    system: str,
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    tools: list[dict] | None = None,
    tool_choice: dict | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
):
    kwargs: dict[str, Any] = dict(
        model=model or config.MODEL_MAIN,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
    )
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    return client().messages.create(**kwargs)


def text_of(response) -> str:
    return "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()


def tool_input_of(response, name: str | None = None) -> dict | None:
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and (name is None or block.name == name):
            return block.input
    return None


def structured(
    system: str,
    user_text: str,
    schema: dict,
    *,
    name: str = "emit",
    model: str | None = None,
    max_tokens: int = 1024,
) -> dict:
    """Return a dict matching `schema`, guaranteed by forced tool use."""
    tools = [{"name": name, "description": "Emit the structured result.", "input_schema": schema}]
    response = complete(
        system,
        [{"role": "user", "content": user_text}],
        model=model or config.MODEL_FAST,
        tools=tools,
        tool_choice={"type": "tool", "name": name},
        max_tokens=max_tokens,
        temperature=0.0,
    )
    out = tool_input_of(response, name)
    if out is None:
        raise RuntimeError("Model did not return structured output")
    return out


def chat(system: str, history: list[dict], *, model: str | None = None, max_tokens: int = 1024) -> str:
    """Plain conversational completion over a message history."""
    response = complete(system, history, model=model, max_tokens=max_tokens)
    return text_of(response)
