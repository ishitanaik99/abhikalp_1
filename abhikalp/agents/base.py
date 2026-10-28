"""What every SyncStore agent has in common.

An agent has two halves:
  sense()  - plain code that watches the shared state and raises events
             (cheap, runs every simulated minute, never calls an LLM)
  decide   - for the events it is triggered by: build a compact view, ask the
             LLM to choose tool calls (or use its rules if the LLM is off,
             over budget or failing), check every call against the
             guardrails, then execute it through the shared state.

Agents never call each other. They talk only by writing shared state, which
emits events that the bus routes to whichever agent owns the next step.
"""
import time

from core import guardrails
from core.llm import LLMUnavailable


class Ctx:
    """Everything an agent may touch while handling one event."""

    def __init__(self, runtime, event):
        self.rt = runtime
        self.state = runtime.state
        self.event = event
        self.llm = runtime.llm
        self.llm_on = runtime.llm_on
        self.dry_run = runtime.layer == "shadow"

    @staticmethod
    def find_item(o, sid):
        for it in o["items"]:
            if it["sku"] == sid and it["st"] in ("missing", "open"):
                return it
        return None


class Agent:
    name = "agent"
    title = ""
    tier = "fast"              # which Groq model: 'fast' or 'smart'
    triggers = set()           # events this agent decides on
    senses = set()             # events that only run sense() code
    prompt = ""
    tools = []

    def __init__(self):
        self.stats = {"decisions": 0, "llm": 0, "rules": 0, "fallbacks": 0, "blocked": 0,
                      "actions": 0, "ms_total": 0, "last_fallback": None}

    # -------------------------------------------------- override in each agent
    def sense(self, ev, ctx):
        pass

    def wants_llm(self, ev, ctx):
        return True

    def view(self, ev, ctx):
        return {}

    def rules(self, ev, ctx):
        return []

    def required(self, ev, ctx):
        return set()

    def execute(self, tool, args, ctx):
        raise NotImplementedError

    def post(self, ev, ctx):
        """Plain-code bookkeeping after the plan ran (never decided by the LLM)."""

    # -------------------------------------------------- shared loop
    @property
    def tool_names(self):
        return {t["function"]["name"] for t in self.tools}

    async def handle(self, ev, ctx):
        if ev["type"] in self.senses:
            self.sense(ev, ctx)
            return None
        if ev["type"] not in self.triggers:
            return None
        self.stats["decisions"] += 1
        plan, via, meta = None, "rules", {}
        if ctx.llm_on and self.wants_llm(ev, ctx):
            try:
                ctx.rt.thinking = {"agent": self.name, "event": ev["type"], "tier": self.tier}
                calls, meta = await ctx.llm.decide(self.prompt, self.view(ev, ctx), self.tools, self.tier)
                plan = [(n, a) for n, a in calls if n in self.tool_names and isinstance(a, dict)]
                via = "llm"
            except LLMUnavailable as e:
                self.stats["fallbacks"] += 1
                self.stats["last_fallback"] = str(e)
                meta = {"fallback": str(e)}
            finally:
                ctx.rt.thinking = None
        need = self.required(ev, ctx) if via == "llm" else set()
        if plan is None:
            plan = self.rules(ev, ctx)
        if via == "llm":
            self.stats["llm"] += 1
            self.stats["ms_total"] += meta.get("ms", 0)
        else:
            self.stats["rules"] += 1

        t0 = time.perf_counter()
        done, blocked = self._run(plan, via, meta, reason_on=via == "llm", ctx=ctx)
        missing = need - set(done)
        if missing:
            # The model skipped (or proposed a blocked version of) a step that must happen:
            # the agent's rules fill exactly those steps, still through the guardrails.
            fill = [c for c in self.rules(ev, ctx) if c[0] in missing]
            meta["filled"] = sorted(missing)
            d2, b2 = self._run(fill, "rules", meta, reason_on=False, ctx=ctx)
            done += d2
            blocked += b2
        self._via = via
        if not ctx.dry_run:
            self.post(ev, ctx)
        return {"agent": self.name, "via": via, "actions": done, "blocked": blocked,
                "ms": meta.get("ms"), "model": meta.get("model"), "fallback": meta.get("fallback"),
                "filled": meta.get("filled"), "code_ms": round((time.perf_counter() - t0) * 1000, 2)}

    def _run(self, plan, via, meta, reason_on, ctx):
        done, blocked = [], []
        for name, args in plan:
            ok, why = guardrails.check(self.name, name, args, ctx)
            reason = str(args.get("reason", "")).strip()
            if not ok:
                self.stats["blocked"] += 1
                blocked.append(name)
                ctx.state.log(self.name, f"Blocked {name}: {why}.", via="guardrail",
                              meta={"proposed_by": via})
                continue
            if ctx.dry_run:
                ctx.state.log(self.name, self.describe(name, args, ctx), via=via, shadow=True,
                              why=reason if reason_on and reason else None)
                done.append(name)
                continue
            try:
                out = self.execute(name, args, ctx)
            except (KeyError, ValueError, TypeError) as e:
                blocked.append(name)
                ctx.state.log(self.name, f"Could not run {name}: {type(e).__name__}.", via="guardrail")
                continue
            self.stats["actions"] += 1
            done.append(name)
            if out:
                text, hi, story = (tuple(out) + (None, None))[:3] if isinstance(out, tuple) else (out, None, None)
                ctx.state.log(self.name, text, hi=hi, via=via, story=story,
                              why=reason if reason_on and reason else None,
                              meta={k: meta[k] for k in ("model", "ms") if k in meta})
        return done, blocked

    def describe(self, name, args, ctx):
        """Plain-English line for an action logged in Shadow mode."""
        bits = ", ".join(f"{k}={v}" for k, v in args.items() if k != "reason")
        return f"{name}({bits})"

    def public_stats(self):
        s = dict(self.stats)
        s["avg_ms"] = round(s["ms_total"] / s["llm"]) if s["llm"] else None
        s["name"], s["title"], s["tier"] = self.name, self.title, self.tier
        s["triggers"] = sorted(self.triggers)
        s["tools"] = sorted(self.tool_names)
        return s
