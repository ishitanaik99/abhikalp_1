"""Relay: rider-first release.

Each order's time buffer is written from the rider's arrival (code, in the
optimiser). Relay decides the judgment calls around it: when a rider runs late
it re-orders who goes first, and when the packing bench backs up it paces
releases so bags don't pile up. It never pushes an order below its 45 s buffer.
"""
from core.llm import tool
from sim.constants import mmss

from .base import Agent

PROMPT = """You are RELAY, the rider-first release agent. You own the release policy (bench_cap: how many
bags may wait at the packing bench before you hold back releases; pace_slack: orders with more buffer
than this are held while the bench is congested) and short priority boosts for specific orders.
Bounds: bench_cap 3-10, pace_slack 45-180 s. An order whose buffer is under 45 s is always released at once.

Events:
- rider_late: a rider is now 2 minutes late. Usually prioritise the waiting order whose rider arrives
  soonest (it should go first), and announce the change to the floor in one sentence.
- bench_congested: bags are piling up at the bench. Use set_pacing (typically bench_cap 4-5, pace_slack
  60-120: hold more when riders are not yet at the store, less when riders are already waiting) and announce.
Keep announcements under 30 words and mention order numbers with #."""

TOOLS = [
    tool("prioritise", "Boost one waiting order so the optimiser releases it first (3 minutes).",
         {"order_id": {"type": "integer"}}, ["order_id"]),
    tool("set_pacing", "Set how releases are paced while the packing bench is congested.",
         {"bench_cap": {"type": "integer"}, "pace_slack": {"type": "integer"}}, []),
    tool("announce", "Tell the floor what changed and why.", {"message": {"type": "string"}}, ["message"]),
]


class Relay(Agent):
    name = "relay"
    title = "Rider-first release"
    tier = "fast"
    triggers = {"rider_late", "bench_congested", "bench_cleared"}
    prompt = PROMPT
    tools = TOOLS

    def wants_llm(self, ev, ctx):
        return ev["type"] != "bench_cleared"

    def _waiting(self, ctx, exclude=None):
        w = ctx.state.w
        rows = [o for o in w.active_orders() if o["status"] == "pool" and o["id"] != exclude]
        rows.sort(key=lambda o: o["rider_arrive"])
        return rows

    def view(self, ev, ctx):
        w = ctx.state.w
        v = {"event": ev["type"], "time": ctx.rt.clock(), "release_policy": dict(w.release_policy),
             "bench_queue": len(w.bench["q"]), "bags_ready": len(w.bench["ready"]),
             "riders_at_store": sum(1 for o in w.active_orders() if o["rider_arrive"] <= w.t)}
        v["waiting_orders"] = [{"order_id": o["id"], "rider_arrives_in_s": o["rider_arrive"] - w.t,
                                "buffer_s": round(w.slack_of(o))}
                               for o in self._waiting(ctx, ev.get("order_id"))[:6]]
        if ev["type"] == "rider_late":
            o = w.by_id[ev["order_id"]]
            v["late_order"] = {"order_id": o["id"], "rider_now_arrives_in_s": o["rider_arrive"] - w.t,
                               "buffer_s": round(w.slack_of(o)), "status": o["status"]}
        return v

    def rules(self, ev, ctx):
        w = ctx.state.w
        if ev["type"] == "rider_late":
            nxt = self._waiting(ctx, ev["order_id"])
            nxt = nxt[0] if nxt else None
            msg = (f"Rider for #{ev['order_id']} is running 2 min late. Its time buffer grew by 120 s, so "
                   + (f"#{nxt['id']} (rider {mmss(nxt['rider_arrive'] - w.t)} away) goes first and " if nxt else "")
                   + f"#{ev['order_id']} can now share a trolley.")
            plan = [("prioritise", {"order_id": nxt["id"]})] if nxt else []
            return plan + [("announce", {"message": msg})]
        if ev["type"] == "bench_congested":
            waiting = sum(1 for o in w.active_orders() if o["rider_arrive"] <= w.t)
            slack = 60 if waiting >= 3 else 90
            return [("set_pacing", {"bench_cap": 5, "pace_slack": slack}),
                    ("announce", {"message": f"Packing bench has {ev['queue']} bags waiting. Holding orders with more "
                                             f"than {slack} s of buffer until it clears."})]
        if ev["type"] == "bench_cleared":
            return [("set_pacing", {"bench_cap": 5, "pace_slack": 90})]
        return []

    def execute(self, name, a, ctx):
        st, ev = ctx.state, ctx.event
        if name == "prioritise":
            st.boost(self.name, int(a["order_id"]))
            return None
        if name == "set_pacing":
            before = dict(st.w.release_policy)
            st.set_release_policy(self.name, a.get("bench_cap"), a.get("pace_slack"))
            after = st.w.release_policy
            if before == after or ev["type"] == "bench_cleared":
                return None
            return f"Release pacing set: hold releases when {after['bench_cap']}+ bags wait, unless an order has under {after['pace_slack']} s of buffer."
        if name == "announce":
            story = "riderlate" if ev["type"] == "rider_late" else None
            return (str(a["message"])[:300], None, story)
        raise ValueError(name)

    def describe(self, name, a, ctx):
        return {"prioritise": f"Release #{a.get('order_id')} first.",
                "set_pacing": f"Pace releases (bench cap {a.get('bench_cap')}, slack {a.get('pace_slack')} s).",
                "announce": str(a.get("message", ""))}.get(name, name)
