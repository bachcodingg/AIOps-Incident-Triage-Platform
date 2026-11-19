"""The models the agent can run on, behind one small interface.

  ClaudeLLM    Claude through the Anthropic SDK (variant A1; A2 and A3 too)
  OllamaLLM    an open-weight model on a local Ollama server (variant A4, and free tuning)
  ScriptedLLM  replies written in advance, for the tests: no network, no cost

The agent keeps the conversation in each provider's own message format and
only ever appends to it, so what the model said (including Claude's thinking
blocks) goes back exactly as it came.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .tools import Tool


class LLMError(RuntimeError):
    """The provider failed after its own retries; the run falls back to a human."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str             # end_turn | tool_use | max_tokens | refusal
    usage: Usage
    model: str                   # the model that answered (a fallback may have)
    message: dict[str, Any]      # the assistant message to append, in the provider's format
    raw: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0


class LLM(Protocol):
    model: str

    def reply(self, system: str, messages: list[dict[str, Any]], tools: list[Tool], allow_tools: bool) -> Reply: ...
    def user(self, text: str) -> dict[str, Any]: ...
    def tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> list[dict[str, Any]]: ...


# --- Claude ------------------------------------------------------------------------
class ClaudeLLM:
    """Messages API with the tools, adaptive thinking (always on for these
    models) at a set effort, and automatic prompt caching: tools and system
    prompt are the same for every incident, and each turn of the loop re-reads
    the conversation so far from the cache.

    With `fallbacks` (the default), a request a safety classifier declines is
    re-run on a fallback model the API picks by category, inside the same call.
    The run log records which model answered every turn, so a verdict that came
    from a fallback model is visible in the evaluation."""

    def __init__(self, model: str = "claude-opus-5-5", *, effort: str = "medium", max_tokens: int = 16000,
                 fallbacks: bool = True, timeout_s: float = 120.0, max_retries: int = 2, client: Any = None):
        import anthropic  # only needed for this provider

        self._anthropic = anthropic
        self.model, self.effort, self.max_tokens, self.fallbacks = model, effort, max_tokens, fallbacks
        self.client = client or anthropic.Anthropic(timeout=timeout_s, max_retries=max_retries)

    def user(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> list[dict[str, Any]]:
        # All results of one turn in a single user message.
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": out, **({"is_error": True} if err else {})}
            for call, out, err in results]}]

    def reply(self, system: str, messages: list[dict[str, Any]], tools: list[Tool], allow_tools: bool) -> Reply:
        a = self._anthropic
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            # The same list every turn (it is part of the cached prefix); on the
            # last turn tool_choice none asks for the answer instead.
            "tools": [{"name": t.name, "description": t.description, "input_schema": t.input_schema()} for t in tools],
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if not allow_tools:
            kwargs["tool_choice"] = {"type": "none"}
        started = time.monotonic()
        try:
            if self.fallbacks:
                response = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                response = self.client.messages.create(**kwargs)
        except (a.BadRequestError, a.AuthenticationError, a.PermissionDeniedError, a.NotFoundError) as exc:
            raise LLMError(f"{type(exc).__name__}: {getattr(exc, 'message', exc)}") from exc
        except a.RateLimitError as exc:
            raise LLMError(f"rate limited after retries: {exc.message}") from exc
        except a.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code} after retries: {exc.message}") from exc
        except a.APIConnectionError as exc:
            raise LLMError(f"connection failed after retries: {exc}") from exc
        u = response.usage
        usage = Usage(u.input_tokens or 0, u.output_tokens or 0,
                      getattr(u, "cache_read_input_tokens", 0) or 0, getattr(u, "cache_creation_input_tokens", 0) or 0)
        content = [b.to_dict() for b in response.content]
        calls = [ToolCall(b["id"], b["name"], b.get("input") or {}) for b in content if b.get("type") == "tool_use"]
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        return Reply(text=text, tool_calls=calls, stop_reason=response.stop_reason or "end_turn", usage=usage,
                     model=response.model, message={"role": "assistant", "content": content},
                     raw={"id": response.id, "request_id": getattr(response, "_request_id", None),
                          "stop_details": response.stop_details.to_dict() if getattr(response, "stop_details", None) else None},
                     seconds=round(time.monotonic() - started, 3))


# --- Ollama --------------------------------------------------------------------------
class OllamaLLM:
    """A local open-weight model through Ollama's /api/chat, with its tool
    calling. Free, slower and weaker: good for debugging prompts and tools, and
    for variant A4. `seed` makes its sampling repeatable."""

    def __init__(self, model: str = "qwen3:8b", *, host: str = "http://localhost:11434", seed: int = 0,
                 timeout_s: float = 300.0, num_ctx: int = 32768,
                 post: Callable[[str, dict[str, Any], float], dict[str, Any]] | None = None):
        self.model, self.host, self.seed, self.timeout_s, self.num_ctx = model, host.rstrip("/"), seed, timeout_s, num_ctx
        self._post = post or self._http_post
        self._n = 0

    @staticmethod
    def _http_post(url: str, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"ollama at {url}: {exc}") from exc

    def user(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> list[dict[str, Any]]:
        return [{"role": "tool", "tool_name": call.name, "content": ("ERROR: " if err else "") + out}
                for call, out, err in results]

    def reply(self, system: str, messages: list[dict[str, Any]], tools: list[Tool], allow_tools: bool) -> Reply:
        body: dict[str, Any] = {
            "model": self.model, "stream": False,
            "messages": [{"role": "system", "content": system}, *messages],
            "options": {"seed": self.seed, "num_ctx": self.num_ctx},
        }
        if allow_tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                "parameters": t.input_schema()}} for t in tools]
        started = time.monotonic()
        data = self._post(f"{self.host}/api/chat", body, self.timeout_s)
        msg = data.get("message") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_unparsed": args}
            self._n += 1
            calls.append(ToolCall(f"call_{self._n}", fn.get("name", ""), args))
        stop = "tool_use" if calls else ("max_tokens" if data.get("done_reason") == "length" else "end_turn")
        message = {"role": "assistant", "content": msg.get("content", "")}
        if msg.get("tool_calls"):
            message["tool_calls"] = msg["tool_calls"]
        return Reply(text=msg.get("content", ""), tool_calls=calls, stop_reason=stop,
                     usage=Usage(data.get("prompt_eval_count", 0), data.get("eval_count", 0)),
                     model=data.get("model", self.model), message=message, seconds=round(time.monotonic() - started, 3))


# --- tests -----------------------------------------------------------------------------
class ScriptedLLM:
    """Plays back replies: a string is a final text answer, a list of
    (tool name, arguments) pairs is one turn of tool calls."""

    def __init__(self, script: list[str | list[tuple[str, dict[str, Any]]]], model: str = "scripted",
                 usage: Usage | None = None, stop_reason: str | None = None):
        self.script, self.model, self.calls = list(script), model, []
        self._usage = usage or Usage(1000, 100)
        self._stop = stop_reason
        self._n = 0

    def user(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> list[dict[str, Any]]:
        return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": c.id, "content": o, "is_error": e}
                                             for c, o, e in results]}]

    def reply(self, system: str, messages: list[dict[str, Any]], tools: list[Tool], allow_tools: bool) -> Reply:
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "allow_tools": allow_tools,
                           "tools": [t.name for t in tools]})
        if not self.script:
            raise LLMError("script ran out")
        step = self.script.pop(0)
        u = Usage(**self._usage.__dict__)
        if isinstance(step, str):
            return Reply(step, [], self._stop or "end_turn", u, self.model, {"role": "assistant", "content": step})
        calls = []
        for name, args in step:
            self._n += 1
            calls.append(ToolCall(f"t{self._n}", name, args))
        content = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
        return Reply("", calls, "tool_use", u, self.model, {"role": "assistant", "content": content})
