"""Groq LLM client used by all four agents (OpenAI-compatible tool calling).

Each agent sends: its system prompt, a compact JSON view of the store, and its
own tools. The model answers with tool calls only. This client never executes
anything itself; the agent validates every call against guardrails first.

Safety nets so the live demo never stalls:
  - timeout per call (default 8 s)
  - per-minute and per-day request budgets shared by every session
  - cooldown after a 429 or repeated errors
  - any failure raises LLMUnavailable, and the agent falls back to its rules
The API key is read from the GROQ_API_KEY environment variable only.
"""
import asyncio
import json
import os
import time
from collections import deque

import httpx

GROQ_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1") + "/chat/completions"

CHARTER = (
    "You are one of four AI agents that jointly run SyncStore, the operating system of a quick-commerce "
    "dark store (MUM-114, Andheri East, Mumbai) during the evening peak. The agents are Ledger (inventory "
    "truth), Captain (shift-lead copilot), Relay (rider-first release) and Promise (customer swap choices). "
    "You coordinate only through a shared store state: you read the JSON view you are given and act ONLY by "
    "calling your tools. Conflict rules, in order: 1 safety and compliance (cold chain, labour limits); "
    "2 the customer promise (the 10-minute promise is never extended); 3 completeness (full basket, or the "
    "swap the customer chose); 4 efficiency. Hard limits are enforced in code and any tool call that breaks "
    "them is rejected. Use only ids that appear in the input. Call every tool this event needs in one "
    "response. Every 'reason' is one plain-English sentence of at most 25 words that a shift lead can read "
    "aloud. Do not reply with prose; reply with tool calls."
)


class LLMUnavailable(Exception):
    pass


class GroqLLM:
    def __init__(self, api_key=None, smart_model=None, fast_model=None, rpm=None, rpd=None,
                 timeout=None, transport=None):
        self.api_key = api_key if api_key is not None else os.environ.get("GROQ_API_KEY", "")
        self.models = {
            "smart": smart_model or os.environ.get("GROQ_MODEL_SMART", "llama-3.3-70b-versatile"),
            "fast": fast_model or os.environ.get("GROQ_MODEL_FAST", "llama-3.1-8b-instant"),
        }
        self.rpm = int(rpm or os.environ.get("LLM_RPM", 25))
        self.rpd = int(rpd or os.environ.get("LLM_RPD", 900))
        self.timeout = float(timeout or os.environ.get("LLM_TIMEOUT", 8))
        self._minute = deque()
        self._day = deque()
        self._sem = asyncio.Semaphore(int(os.environ.get("LLM_CONCURRENCY", 3)))
        self._cool_until = 0.0
        self._errors_in_row = 0
        self.calls = 0
        self.errors = 0
        self.last_error = None
        self.last_ok = None
        self._client = httpx.AsyncClient(timeout=self.timeout, transport=transport)

    @property
    def configured(self):
        return bool(self.api_key)

    def health(self):
        now = time.time()
        self._trim(now)
        return {"configured": self.configured, "models": self.models, "calls": self.calls,
                "errors": self.errors, "last_error": self.last_error,
                "rpm_used": len(self._minute), "rpm_limit": self.rpm,
                "rpd_used": len(self._day), "rpd_limit": self.rpd,
                "cooling": max(0, round(self._cool_until - now)), "last_ok": self.last_ok}

    def _trim(self, now):
        while self._minute and now - self._minute[0] > 60:
            self._minute.popleft()
        while self._day and now - self._day[0] > 86400:
            self._day.popleft()

    def _budget_ok(self):
        now = time.time()
        self._trim(now)
        if now < self._cool_until:
            return "cooling down after an API error"
        if len(self._minute) >= self.rpm:
            return f"over the {self.rpm} calls/min budget"
        if len(self._day) >= self.rpd:
            return f"over the {self.rpd} calls/day budget"
        return None

    async def decide(self, system_prompt, view, tools, tier="fast"):
        """Return [(tool_name, args_dict), ...] or raise LLMUnavailable."""
        if not self.configured:
            raise LLMUnavailable("no GROQ_API_KEY set")
        why = self._budget_ok()
        if why:
            raise LLMUnavailable(why)
        model = self.models[tier]
        body = {
            "model": model,
            "messages": [{"role": "system", "content": CHARTER + "\n\n" + system_prompt},
                         {"role": "user", "content": json.dumps(view, ensure_ascii=False, separators=(",", ":"))}],
            "tools": tools,
            "tool_choice": os.environ.get("LLM_TOOL_CHOICE", "required"),
            "temperature": 0.1,
            "max_tokens": 700,
        }
        async with self._sem:
            now = time.time()
            self._minute.append(now)
            self._day.append(now)
            self.calls += 1
            t0 = time.perf_counter()
            try:
                r = await self._client.post(GROQ_URL, json=body,
                                            headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.HTTPError as e:
                self._fail(f"network: {type(e).__name__}")
                raise LLMUnavailable(self.last_error)
            ms = round((time.perf_counter() - t0) * 1000)
        if r.status_code == 429:
            retry = float(r.headers.get("retry-after", 20) or 20)
            self._fail("rate limited by Groq (429)", cool=min(60.0, retry))
            raise LLMUnavailable(self.last_error)
        if r.status_code >= 400:
            msg = ""
            try:
                msg = r.json().get("error", {}).get("message", "")[:160]
            except Exception:
                pass
            self._fail(f"HTTP {r.status_code} {msg}".strip())
            raise LLMUnavailable(self.last_error)
        try:
            msg = r.json()["choices"][0]["message"]
            calls = []
            for tc in msg.get("tool_calls") or []:
                fn = tc["function"]
                args = fn.get("arguments") or "{}"
                calls.append((fn["name"], json.loads(args) if isinstance(args, str) else args))
        except (KeyError, IndexError, ValueError, TypeError) as e:
            self._fail(f"unreadable reply: {type(e).__name__}")
            raise LLMUnavailable(self.last_error)
        self._errors_in_row = 0
        self.last_ok = {"model": model, "ms": ms, "at": time.time()}
        return calls, {"model": model, "ms": ms}

    def _fail(self, why, cool=None):
        self.errors += 1
        self._errors_in_row += 1
        self.last_error = why
        if cool is None and self._errors_in_row >= 3:
            cool = 30.0
        if cool:
            self._cool_until = time.time() + cool

    async def close(self):
        await self._client.aclose()


def tool(name, description, props, required=None):
    """Build an OpenAI/Groq tool schema. Every tool also takes a 'reason'."""
    props = dict(props)
    props["reason"] = {"type": "string", "description": "One sentence a shift lead can read aloud."}
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props,
                       "required": list(required or []) + ["reason"]}}}
