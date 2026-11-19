"""The triage agent: a tool loop with hard limits and a strict answer.

  1. The model gets the system prompt, the tools and the incident brief.
  2. It calls tools until it answers, or until a limit is reached (turns, tool
     calls, tokens, dollars, wall clock). Then it is asked to answer now, with
     tools switched off.
  3. The answer must be a JSON verdict that validates (verdict.py). If it does
     not, the model is told what is wrong and gets one more try. After that,
     or on a refusal or a provider error, the run falls back to none_escalate.

Every run returns a record with the prompt, each model call (usage, stop
reason, which model answered), each tool call with its arguments and output,
the raw answer, the verdict and the cost. The evaluation stores it as JSON.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .llm import LLM, LLMError, Usage
from .prompt import prompt_sha256
from .replay import View
from .tools import Tool, run_tool
from .verdict import fallback, parse

RUN_FORMAT = 1


@dataclass
class Limits:
    max_turns: int = 12              # model calls, the answer included
    max_tool_calls: int = 20
    max_input_tokens: int = 400_000  # summed over calls, cached tokens included
    max_output_tokens: int = 40_000
    max_cost_usd: float = 0.50       # hard cap per incident
    max_seconds: float = 300.0
    tool_timeout_s: float = 10.0


@dataclass
class Price:
    """US dollars per million tokens."""
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0

    def cost(self, u: Usage) -> float:
        return (u.input_tokens * self.input + u.output_tokens * self.output
                + u.cache_read_tokens * self.cache_read + u.cache_write_tokens * self.cache_write) / 1e6


@dataclass
class Run:
    incident: str
    variant: str
    model: str
    seed: int
    triage_at: float
    system_sha256: str
    system: str
    brief: str
    tools: list[str]
    calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    verdict: dict[str, Any] | None = None
    valid: bool = False
    attempts: int = 0
    stopped_by: str | None = None    # which limit ended the tool loop, if any
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    seconds: float = 0.0
    models_answered: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"format": RUN_FORMAT, **asdict(self)}


ANSWER_NOW = ("Limit reached ({why}): no more tool calls. Give your verdict now, from what you have, "
              "as the JSON object only.")
RETRY = ("Your answer is not a valid verdict: {problems}\nReply with only the corrected JSON object.")


class Agent:
    def __init__(self, llm: LLM, tools: list[Tool], system: str, *, variant: str = "A1", seed: int = 0,
                 limits: Limits | None = None, price: Price | None = None, prices: dict[str, Price] | None = None):
        self.llm, self.tools, self.system = llm, tools, system
        self.variant, self.seed = variant, seed
        self.limits, self.price, self.prices = limits or Limits(), price or Price(), prices or {}
        self.by_name = {t.name: t for t in tools}

    def _cost(self, model: str, usage: Usage) -> float:
        return self.prices.get(model, self.price).cost(usage)

    def triage(self, view: View, incident_id: str, brief: str) -> Run:
        lim, started = self.limits, time.monotonic()
        run = Run(incident_id, self.variant, self.llm.model, self.seed, view.now, prompt_sha256(self.system),
                  self.system, brief, [t.name for t in self.tools])
        total = Usage()
        messages: list[dict[str, Any]] = [self.llm.user(brief)]
        allow_tools, tool_count, why = True, 0, None
        reason = None

        def over() -> str | None:
            if len(run.calls) >= lim.max_turns - 1:
                return f"{lim.max_turns} turns"
            if tool_count >= lim.max_tool_calls:
                return f"{lim.max_tool_calls} tool calls"
            if total.total_input >= lim.max_input_tokens:
                return f"{lim.max_input_tokens} input tokens"
            if total.output_tokens >= lim.max_output_tokens:
                return f"{lim.max_output_tokens} output tokens"
            if run.cost_usd >= lim.max_cost_usd:
                return f"${lim.max_cost_usd:.2f}"
            if time.monotonic() - started >= lim.max_seconds:
                return f"{lim.max_seconds:.0f} s"
            return None

        while True:
            if allow_tools and (why := over()):
                allow_tools, run.stopped_by = False, why
                messages.append(self.llm.user(ANSWER_NOW.format(why=why)))
            if len(run.calls) >= lim.max_turns + 2 or run.cost_usd >= lim.max_cost_usd * 1.5:
                reason = "limits exhausted without an answer"
                break
            try:
                reply = self.llm.reply(self.system, messages, self.tools, allow_tools)
            except LLMError as exc:
                reason = f"model error: {exc}"
                break
            total.add(reply.usage)
            cost = self._cost(reply.model, reply.usage)
            run.cost_usd = round(run.cost_usd + cost, 6)
            run.models_answered.append(reply.model)
            run.calls.append({"n": len(run.calls) + 1, "model": reply.model, "stop_reason": reply.stop_reason,
                              "allow_tools": allow_tools, "usage": asdict(reply.usage), "cost_usd": round(cost, 6),
                              "seconds": reply.seconds, "text": reply.text[:4000], **reply.raw})
            messages.append(reply.message)
            if reply.stop_reason == "refusal":
                reason = f"refused ({(reply.raw.get('stop_details') or {}).get('category')})"
                break
            if reply.tool_calls and allow_tools:
                results = []
                for call in reply.tool_calls:
                    out, err, secs = run_tool(self.by_name, view, call.name, call.input, lim.tool_timeout_s)
                    tool_count += 1
                    run.tool_calls.append({"turn": len(run.calls), "name": call.name, "input": call.input,
                                           "error": err, "seconds": secs, "output": out})
                    results.append((call, out, err))
                messages += self.llm.tool_results(results)
                continue
            # An answer (or tool calls when none were allowed, which is not one).
            run.attempts += 1
            run.answer = reply.text
            verdict, problems = parse(reply.text) if not reply.tool_calls else (None, "tool calls instead of an answer")
            if reply.stop_reason == "max_tokens":
                verdict, problems = None, "the answer was cut off at max_tokens"
            if verdict is not None:
                run.verdict, run.valid = verdict.model_dump(), True
                break
            if run.attempts >= 2:
                reason = f"invalid answer twice: {problems}"
                break
            allow_tools = False
            if reply.tool_calls:  # every tool call needs its result before the conversation goes on
                messages += self.llm.tool_results([(c, "tools are switched off now", True) for c in reply.tool_calls])
            messages.append(self.llm.user(RETRY.format(problems=problems)))
        if not run.valid:
            run.verdict = fallback(reason or "no answer")
        run.usage = asdict(total)
        run.seconds = round(time.monotonic() - started, 3)
        return run


def dump(run: Run) -> str:
    return json.dumps(run.to_dict(), indent=1, default=str) + "\n"
