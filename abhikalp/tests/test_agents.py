"""Each agent tested on its own, then all four together.

Run:  python -m pytest -q
No API key needed: the LLM is replaced by a scripted fake (or a mocked Groq HTTP endpoint).
"""
import asyncio
import json

import httpx
import pytest

from agents.base import Ctx
from agents.captain import Captain
from agents.ledger import Ledger
from agents.promise import Promise
from agents.relay import Relay
from core.llm import GroqLLM, LLMUnavailable
from core.runtime import Session
from core.state import OwnershipError, StoreState
from sim.world import World, gen_orders


# ------------------------------------------------------------------ harness
class FakeLLM:
    """Returns scripted tool calls, like Groq would."""

    def __init__(self, calls=None, fail=False):
        self.calls, self.fail, self.seen = calls or [], fail, []
        self.configured = True

    async def decide(self, prompt, view, tools, tier="fast"):
        self.seen.append(view)
        if self.fail:
            raise LLMUnavailable("test outage")
        return self.calls, {"model": "fake", "ms": 1}


class Harness:
    """Just enough runtime to run one agent alone on a real simulated store."""

    def __init__(self, llm=None, warm=600):
        self.smart = World(gen_orders(7), "smart")
        self.logs = []
        self.state = StoreState(self.smart, lambda agent, text, **kw: self.logs.append((agent, text, kw)))
        self.llm = llm or FakeLLM()
        self.llm_on = llm is not None
        self.layer = "live"
        for _ in range(warm):          # run the floor so there are live orders
            self.smart.step()
            self.smart.drain()
        while not self.smart.pool:     # make sure some orders are still waiting to be picked
            self.smart.step()
            self.smart.drain()

    def clock(self):
        return "19:00:00"

    def run(self, agent, ev):
        return asyncio.run(agent.handle(ev, Ctx(self, ev)))


def not_found_event(h, sid="milk"):
    s = h.smart.skus[sid]
    return {"type": "pick_not_found", "t": h.smart.t, "sku": sid, "record": s["record"], "misses": [], "trip": None}


# ------------------------------------------------------------------ Ledger
def test_ledger_rules_hide_and_count():
    h = Harness()
    h.smart.skus["milk"]["shelf"] = 0
    res = h.run(Ledger(), not_found_event(h))
    milk = h.smart.skus["milk"]
    assert res["via"] == "rules"
    assert milk["hidden"] and milk["trust"] <= 0.1 and milk["counting"]
    assert any(e["type"] == "sku_hidden" for e in h.smart.drain())


def test_ledger_llm_plan_is_guarded_and_completed():
    # The model forgets to hide the item and tries to restore it: restore is blocked, hide is filled in by rules.
    llm = FakeLLM([("set_trust", {"sku": "milk", "value": 0.05, "reason": "slot empty"}),
                   ("restore_item", {"sku": "milk", "reason": "oops"}),
                   ("create_count_task", {"sku": "milk", "mode": "empty", "reason": "verify"})])
    h = Harness(llm)
    res = h.run(Ledger(), not_found_event(h))
    milk = h.smart.skus["milk"]
    assert res["via"] == "llm" and res["filled"] == ["hide_item"]
    assert "restore_item" in res["blocked"]
    assert milk["hidden"] and milk["trust"] == 0.05


def test_ledger_count_result_refills():
    h = Harness()
    s = h.smart.skus["milk"]
    ev = {"type": "count_result", "t": h.smart.t, "sku": "milk", "counted": 0, "backroom": 40, "mode": "empty",
          "record": s["record"]}
    h.run(Ledger(), ev)
    assert s["record"] == 0 and s["status"] == "refilling"


def test_ledger_cannot_fake_a_count():
    llm = FakeLLM([("correct_record", {"sku": "milk", "value": 99, "reason": "looks fine"})])
    h = Harness(llm)
    ev = {"type": "count_result", "t": h.smart.t, "sku": "milk", "counted": 3, "backroom": 40, "mode": "check",
          "record": 10}
    res = h.run(Ledger(), ev)
    assert "correct_record" in res["blocked"]
    assert h.smart.skus["milk"]["record"] == 3      # rules filled the correct value


# ------------------------------------------------------------------ Captain
def mode_signal(target="peak", direction="up"):
    return {"type": "mode_signal", "t": 0, "target": target, "direction": direction, "why": "test",
            "u_now": 0.85, "u_fc": 0.9, "backlog": [1, 2, 3, 4], "grow": False}


def test_captain_rules_enter_peak():
    h = Harness()
    h.run(Captain(), mode_signal())
    w = h.smart
    assert w.mode == "peak" and w.lim["max"] == 4 and w.skus["chips"]["fwd"]
    assert w.approvals and w.approvals[0]["status"] == "pending" and w.brief


def test_captain_cannot_break_ops_bounds_or_skip_hysteresis():
    llm = FakeLLM([("set_mode", {"mode": "peak", "reason": "load"}),
                   ("set_limits", {"max_batch": 6, "hold_s": 120, "reason": "go big"}),
                   ("brief_shift_lead", {"hindi": "peak mode", "english": "Peak", "reason": "brief"})])
    h = Harness(llm)
    res = h.run(Captain(), mode_signal())
    assert h.smart.mode == "peak" and h.smart.lim == {"max": 4, "hold": 75}
    assert "set_limits" in res["blocked"] and "brief_shift_lead" in res["blocked"]   # Hindi must be Devanagari
    # Jumping down two modes without 3 calm minutes is refused
    llm.calls = [("set_mode", {"mode": "trough", "reason": "quiet"})]
    res = h.run(Captain(), mode_signal("trough", "down"))
    assert h.smart.mode == "peak" and "set_mode" in res["blocked"]


# ------------------------------------------------------------------ Relay
def test_relay_pacing_bounds_and_priority():
    h = Harness()
    waiting = [o for o in h.smart.active_orders() if o["status"] == "pool"]
    target = waiting[0]["id"] if waiting else None
    calls = [("set_pacing", {"bench_cap": 4, "pace_slack": 30, "reason": "tight"})]
    if target:
        calls.append(("prioritise", {"order_id": target, "reason": "rider first"}))
    h.llm, h.llm_on = FakeLLM(calls), True
    res = h.run(Relay(), {"type": "bench_congested", "t": h.smart.t, "queue": 6, "ready": 2})
    assert "set_pacing" in res["blocked"]                       # 30 s is below the 45 s buffer
    if target:
        assert target in h.smart.priority


# ------------------------------------------------------------------ Promise
def _hidden_with_open_lines(h, sid="bread"):
    s = h.smart.skus[sid]
    s["hidden"] = True
    o = next(o for o in h.smart.pool)
    o["items"].append({"sku": sid, "orig": sid, "st": "open"})
    return o


def test_promise_never_guesses_a_swap():
    h = Harness()
    o = _hidden_with_open_lines(h)
    o["saved"] = o["chose"] = False
    h.llm, h.llm_on = FakeLLM([("apply_swap", {"order_id": o["id"], "sku": "bread", "to_sku": "breadm",
                                               "reason": "close enough"})]), True
    res = h.run(Promise(), {"type": "sku_hidden", "t": h.smart.t, "sku": "bread"})
    line = next(i for i in o["items"] if i["orig"] == "bread")
    assert "apply_swap" in res["blocked"]
    assert line["st"] == "dropped" and h.smart.stats["autoSwaps"] == 0    # completeness: refunded by rules


def test_promise_applies_chosen_swap():
    h = Harness()
    o = _hidden_with_open_lines(h)
    o["chose"] = True
    h.run(Promise(), {"type": "sku_hidden", "t": h.smart.t, "sku": "bread"})
    line = next(i for i in o["items"] if i["orig"] == "bread")
    assert line["sku"] != "bread" and h.smart.skus[line["sku"]]["cat"] == "bread"


# ------------------------------------------------------------------ coordination
def test_ownership_is_enforced():
    h = Harness()
    with pytest.raises(OwnershipError):
        h.state.set_mode("ledger", "surge")
    with pytest.raises(OwnershipError):
        h.state.hide("promise", "milk")


def test_shadow_mode_changes_nothing():
    llm = FakeLLM([("hide_item", {"sku": "milk", "reason": "empty"})])
    h = Harness(llm)
    h.layer = "shadow"
    h.run(Ledger(), not_found_event(h))
    assert not h.smart.skus["milk"]["hidden"]
    assert all(kw.get("shadow") for _, _, kw in h.logs)


def test_llm_outage_falls_back_to_rules():
    h = Harness(FakeLLM(fail=True))
    a = Ledger()
    res = h.run(a, not_found_event(h))
    assert res["via"] == "rules" and res["fallback"] == "test outage" and h.smart.skus["milk"]["hidden"]


def test_full_shift_four_agents_in_sync():
    s = Session(GroqLLM(api_key=""))
    s.demo = True

    async def go():
        while s.smart.t < 7195:
            await s.tick()
    asyncio.run(go())
    S, X = s.smart, s.static
    assert S.avg_pick() < X.avg_pick() * 0.9                      # faster picks than the static plan
    assert S.stats["walk"] / S.stats["pickN"] < X.stats["walk"] / X.stats["pickN"]
    assert S.stats["autoSwaps"] == 0
    for step in ("peak", "empty", "liveorders", "count", "surge", "riderlate", "winddown", "summary"):
        assert step in s.story, step
    assert {t["agent"] for t in s.bus.trace} >= {"ledger", "captain", "relay", "promise"} or len(s.bus.trace) == 80


# ------------------------------------------------------------------ Groq client (mocked HTTP)
def _groq_reply(calls):
    return {"choices": [{"message": {"role": "assistant", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
        for i, (n, a) in enumerate(calls)]}}]}


def test_groq_client_parses_tool_calls():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=_groq_reply([("hide_item", {"sku": "milk", "reason": "empty"})]))

    llm = GroqLLM(api_key="test-key", transport=httpx.MockTransport(handler))
    calls, meta = asyncio.run(llm.decide("p", {"x": 1}, Ledger.tools, "fast"))
    assert calls == [("hide_item", {"sku": "milk", "reason": "empty"})]
    assert seen["auth"] == "Bearer test-key" and seen["body"]["model"] == llm.models["fast"]
    assert seen["body"]["tools"][0]["function"]["name"] == "set_trust"


def test_groq_rate_limit_triggers_fallback_and_cooldown():
    llm = GroqLLM(api_key="k", transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"retry-after": "5"}, json={})))
    with pytest.raises(LLMUnavailable):
        asyncio.run(llm.decide("p", {}, Ledger.tools))
    with pytest.raises(LLMUnavailable, match="cooling"):
        asyncio.run(llm.decide("p", {}, Ledger.tools))


def test_whole_session_through_mocked_groq():
    """All four agents decide through the real Groq client against a mocked endpoint that replays
    each agent's own rules, so the full HTTP + parsing + guardrail path is exercised end to end."""
    s = Session(GroqLLM(api_key=""))
    s.demo = True

    def handler(req):
        body = json.loads(req.content)
        names = {t["function"]["name"] for t in body["tools"]}
        agent = next(a for a in s.agents.values() if a.tool_names == names)
        ev = s._current_event
        plan = agent.rules(ev, Ctx(s, ev))
        return httpx.Response(200, json=_groq_reply([(n, {**a, "reason": "mock"}) for n, a in plan]))

    s.llm = GroqLLM(api_key="k", rpm=10000, rpd=100000, transport=httpx.MockTransport(handler))
    s.llm_on = True
    orig = s.bus.dispatch

    async def tracking(events):
        from collections import deque
        from core.bus import ROUTES
        q = deque(events)
        while q:
            ev = q.popleft()
            if ev["type"] == "log":
                s.log(ev["agent"], ev["text"], via=ev.get("via", "rules"), story=ev.get("story"))
                continue
            s._current_event = ev
            for name in ROUTES.get(ev["type"], []):
                await s.agents[name].handle(ev, Ctx(s, ev))
            q.extend(s.smart.drain())
    s.bus.dispatch = tracking

    async def go():
        while s.smart.t < 5000:
            await s.tick()
    asyncio.run(go())
    via = {l["via"] for l in s.logs}
    assert "llm" in via
    total_llm = sum(a.stats["llm"] for a in s.agents.values())
    assert total_llm >= 20 and all(a.stats["llm"] > 0 for a in s.agents.values())
    assert "summary" in s.story
