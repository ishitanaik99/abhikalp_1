"""One running store session: two worlds on the same orders, four agents, one bus.

  smart  - the store SyncStore runs (agents + fast optimiser)
  static - today's plan on an identical order stream, for the side-by-side KPIs
"""
from agents.captain import Captain
from agents.ledger import Ledger
from agents.promise import Promise
from agents.relay import Relay
from engine import optimiser
from sim.constants import HORIZON, PX_M, fmt, mmss
from sim.world import World, gen_orders, make_order

from .bus import EventBus
from .state import StoreState

import random


class Session:
    def __init__(self, llm, seed=7):
        self.llm = llm
        self.seed = seed
        self.llm_on = llm.configured
        self.layer = "live"
        self.speed = 10
        self.playing = True
        self.auto_approve = True
        self.demo = False
        self.spike_rng = random.Random(1234)
        self.user_seq = 9001
        self.thinking = None
        self._build()

    # ------------------------------------------------------------ setup
    def _build(self):
        orders = gen_orders(self.seed)
        self.smart = World(orders, "smart")
        self.static = World(orders, "static")
        self.smart.optimiser = optimiser.run
        self.logs = []
        self.story = {}
        self.state = StoreState(self.smart, self.log)
        self.agents = {a.name: a for a in (Ledger(), Captain(), Relay(), Promise())}
        self.bus = EventBus(self)
        self.summary_done = False
        self.apply_layer(self.layer, quiet=True)

    async def reset(self, start_at=840, demo=False):
        self.demo = demo
        self._build()
        await self.fast_forward(start_at)

    async def fast_forward(self, to):
        """Warm up to the start time using rules only, so no LLM budget is spent."""
        keep = self.llm_on
        self.llm_on = False
        while self.smart.t < to:
            await self.tick()
        self.llm_on = keep

    def clock(self):
        return fmt(self.smart.t)

    # ------------------------------------------------------------ log
    def log(self, agent, text, hi=None, via="rules", story=None, shadow=False, why=None, meta=None):
        self.logs.insert(0, {"t": self.smart.t, "agent": agent, "text": text, "hi": hi, "via": via,
                             "why": why, "meta": meta or {}, "shadow": shadow or self.layer == "shadow"})
        del self.logs[400:]
        if hi and not shadow and self.layer != "shadow":
            self.smart.brief = {"t": self.smart.t, "hi": hi, "en": text, "agent": agent}
        if story and story not in self.story and not shadow:
            self.story[story] = self.smart.t

    # ------------------------------------------------------------ the tick
    async def tick(self):
        s = self.smart
        s.step()
        self.static.step()
        self.static.drain()
        if self.demo and s.t == 2520:
            self.rider_late(auto=True)
        if s.t == 4500 and not self.summary_done:
            self.summary_done = True
            s.emit("shift_summary", numbers=self.summary_numbers())
        if s.t % 60 == 0 and self.auto_approve:
            for a in list(s.approvals):
                if a["status"] == "pending" and s.t - a["t"] >= 20:
                    self.approve(a["id"], "by voice")
        await self.bus.dispatch(s.drain())
        if s.t >= HORIZON - 5:
            self.playing = False

    def summary_numbers(self):
        S, X = self.smart.stats, self.static.stats
        walk_s = S["walk"] * PX_M / max(1, S["pickN"])
        walk_x = X["walk"] * PX_M / max(1, X["pickN"])
        return {"wrong_records_caught": S["hidden"] + S["fixed"], "customer_chosen_swaps": S["swaps"],
                "shared_trolley_trips": S["batchedOrders"] - S["batches"],
                "walking_saved_m": max(0, round((walk_x - walk_s) * S["pickN"] / 10) * 10),
                "cancellations": S["cancels"], "cancellations_static_plan": X["cancels"]}

    # ------------------------------------------------------------ controls
    def apply_layer(self, layer, quiet=False):
        prev, self.layer = self.layer, layer
        self.smart.policy = "smart" if layer == "live" else "static"
        self.smart.agents_on = layer != "off"
        if quiet:
            return
        if layer == "off":
            self.log("system", "Agent layer offline. Fail-safe: the store fell back to today’s static picking, and orders keep flowing.")
        elif layer == "shadow":
            self.log("system", "Shadow mode: the floor runs today’s static plan; the agents only log what they would have done.")
        else:
            self.log("system", "Agent layer back online. Agents resume from the current store state." if prev == "off"
                     else "Live mode: agents act on the floor.")

    def approve(self, kind, how="from the dashboard"):
        a = next((x for x in self.smart.approvals if x["id"] == kind), None)
        if not a or a["status"] != "pending":
            return
        a["status"], a["at"] = "approved", self.smart.t
        name, delay = ("Kiran", 60) if kind == "floater_in_store" else ("Neel", 240)
        self.smart.add_picker(name, delay)
        self.log("captain", f"Shift lead approved {how}: {name} joins the floor in {mmss(delay)}.", via="shift lead")

    def decline(self, kind):
        a = next((x for x in self.smart.approvals if x["id"] == kind), None)
        if not a or a["status"] != "pending":
            return
        a["status"], a["at"] = "declined", self.smart.t
        self.log("captain", f"Shift lead declined: {a['text']}. Captain keeps the current staffing.", via="shift lead")

    def empty_shelf(self, sid):
        for w in (self.smart, self.static):
            w.shrink(sid, 0)
        return self.smart.skus[sid]["name"]

    def rider_late(self, order_id=None, auto=False):
        s, t = self.smart, self.smart.t
        if order_id is None:
            best = None
            for o in s.active_orders():
                if o["status"] in ("pool", "picking") and o["rider_arrive"] > t + 20:
                    if best is None or o["rider_arrive"] < best["rider_arrive"]:
                        best = o
            if best is None:
                return None
            order_id = best["id"]
        for w in (self.smart, self.static):
            o = w.by_id.get(order_id)
            if o:
                o["rider_arrive"] += 120
        s.emit("rider_late", order_id=order_id)
        return order_id

    def spike(self):
        t, extra = self.smart.t, []
        for i in range(14):
            extra.append(make_order(self.spike_rng, t + 5 + int(self.spike_rng.random() * 360),
                                    8001 + int(self.spike_rng.random() * 900) + i * 1000))
        for w in (self.smart, self.static):
            w.insert_orders(extra)
        self.log("system", "Demand spike: 14 extra orders over the next 6 minutes (for example, a cricket match "
                           "ending). The forecast did not expect it, so Captain reacts to live load.")

    def place_order(self, cart, choices):
        s = self.smart
        cart = [c for c in cart if c in s.skus][:4]
        if not cart:
            return None

        def wants_swap(sid):
            sk = s.skus[sid]
            pick = choices.get(sid)
            return (pick != "refund" if sk["hidden"] else pick == "swap") and s.swap_candidate(sk) is not None
        any_swap = any(wants_swap(c) for c in cart)
        o = {"id": self.user_seq, "placed": s.t + 1, "items": list(cart), "ride": 200, "riderIn": 200,
             "saved": False, "chose": any_swap, "forceChoice": any_swap, "cancelIfMissing": False, "user": True}
        self.user_seq += 1
        for w in (self.smart, self.static):
            w.insert_orders([o])
        self.log("promise", f"Order #{o['id']} placed from the checkout with {len(cart)} item{'s' if len(cart) > 1 else ''}"
                 + ("; the customer chose a swap for a low-stock item." if any_swap
                    else "; no swap chosen, so a missing item would be refunded."))
        return o["id"]
