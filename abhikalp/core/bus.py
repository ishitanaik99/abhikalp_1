"""Event bus: how the four agents stay in sync.

The world and the agents' own state writes emit events. The bus routes each
event to the agents subscribed to it, in a fixed order, and keeps going until
no follow-on events are left, all inside one simulated second. Example chain:

    pick_not_found ─► Ledger  (hide milk, count task)  ─► emits sku_hidden
                   └► Promise (resolve the lines mid-pick)
    sku_hidden     ─► Promise (resolve lines still waiting in the pool)

Because the chain finishes before the next tick, every agent always sees a
consistent shared state, and no agent ever calls another directly.
"""
from collections import Counter, deque

from agents.base import Ctx

# event type -> agents, in the order they act
ROUTES = {
    "minute":          ["ledger", "captain"],    # sense() only: plain code, no LLM
    "pick_not_found":  ["ledger", "promise"],
    "trust_drift":     ["ledger"],
    "count_result":    ["ledger"],
    "refill_done":     ["ledger"],
    "sku_hidden":      ["promise"],
    "mode_signal":     ["captain"],
    "shift_summary":   ["captain"],
    "rider_late":      ["relay"],
    "bench_congested": ["relay"],
    "bench_cleared":   ["relay"],
}


def summarise(ev, w):
    t = ev["type"]
    if "sku" in ev:
        nm = w.skus[ev["sku"]]["name"]
        if t == "pick_not_found":
            return f"{nm}: slot empty, {len(ev['misses'])} short"
        if t == "count_result":
            return f"{nm}: counted {ev['counted']} ({ev['mode']})"
        if t == "trust_drift":
            return f"{nm}: trust {ev['trust']:.2f}"
        return nm
    if t == "mode_signal":
        return f"suggest {ev['target']} ({ev['why']})"
    if t == "rider_late":
        return f"#{ev['order_id']} rider +2 min"
    if t == "bench_congested":
        return f"{ev['queue']} bags queued"
    if t == "minute":
        return f"u={ev['u_now']:.2f}, fc={ev['u_fc']:.2f}"
    return ""


class EventBus:
    def __init__(self, runtime):
        self.rt = runtime
        self.trace = deque(maxlen=80)
        self.counts = Counter()

    async def dispatch(self, events):
        rt = self.rt
        q = deque(events)
        while q:
            ev = q.popleft()
            if ev["type"] == "log":
                rt.log(ev["agent"], ev["text"], via=ev.get("via", "rules"), story=ev.get("story"))
                continue
            if rt.layer == "off":
                continue                     # agent layer down: the store runs the static plan
            self.counts[ev["type"]] += 1
            for name in ROUTES.get(ev["type"], []):
                res = await rt.agents[name].handle(ev, Ctx(rt, ev))
                if res is not None:
                    self.trace.appendleft({"t": rt.smart.t, "event": ev["type"],
                                           "detail": summarise(ev, rt.smart), **res,
                                           "shadow": rt.layer == "shadow"})
                q.extend(rt.smart.drain())   # follow-on events from this agent's writes
