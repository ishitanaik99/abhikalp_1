"""Promise: customer swap choices.

At checkout the customer app asks what to do if a low-trust item runs out
(that question is plain code, driven by Ledger's trust score). When an item
goes missing in a live order, Promise applies the customer's saved choice:
the swap they agreed to, or an instant refund of that item. It never swaps
on a guess, and every affected item is resolved before the handler returns.
"""
from core.llm import tool

from .base import Agent

PROMPT = """You are PROMISE, the customer-promise agent. You own the items in live orders and messages to
customers. An item just became unavailable. For EACH affected order line in the input:
- if customer_choice is "swap" and a swap_option exists: apply_swap to that option;
- otherwise refund_item (the customer gets an instant refund for that item).
Never swap when the customer did not choose a swap. Never pick a swap other than swap_option.
Then notify_customer once per affected order with one friendly sentence (under 25 words, no emojis),
e.g. what was swapped or refunded. Resolve every line; do not skip any."""

TOOLS = [
    tool("apply_swap", "Replace the missing item with the swap the customer chose.",
         {"order_id": {"type": "integer"}, "sku": {"type": "string"}, "to_sku": {"type": "string"}},
         ["order_id", "sku", "to_sku"]),
    tool("refund_item", "Drop the missing item and refund it instantly.",
         {"order_id": {"type": "integer"}, "sku": {"type": "string"}}, ["order_id", "sku"]),
    tool("notify_customer", "Send the customer a short update.",
         {"order_id": {"type": "integer"}, "message": {"type": "string"}}, ["order_id", "message"]),
]


class Promise(Agent):
    name = "promise"
    title = "Customer swap choices"
    tier = "smart"
    triggers = {"pick_not_found", "sku_hidden"}
    prompt = PROMPT
    tools = TOOLS

    def _affected(self, ev, ctx):
        """Unresolved order lines for the item in this event: (order, item, trip)."""
        w, sid = ctx.state.w, ev["sku"]
        rows = []
        if ev["type"] == "pick_not_found":
            for oid, it in ev["misses"]:
                o = w.by_id.get(oid)
                if o and o["status"] != "cancelled" and it["st"] == "missing":
                    rows.append((o, it, ev.get("trip")))
        else:
            for o in w.pool:
                if o["status"] == "cancelled":
                    continue
                for it in o["items"]:
                    if it["sku"] == sid and it["st"] == "open":
                        rows.append((o, it, None))
        return rows

    def wants_llm(self, ev, ctx):
        return bool(self._affected(ev, ctx))

    def view(self, ev, ctx):
        w = ctx.state.w
        s = w.skus[ev["sku"]]
        alt = w.swap_candidate(s)
        return {"event": ev["type"], "time": ctx.rt.clock(),
                "unavailable_item": {"sku": s["id"], "name": s["name"], "price": s["price"]},
                "swap_option": ({"sku": alt["id"], "name": alt["name"], "price": alt["price"]} if alt else None),
                "affected_lines": [{"order_id": o["id"], "sku": it["sku"],
                                    "where": "mid_pick" if it["st"] == "missing" else "not_yet_picked",
                                    "customer_choice": "swap" if (o["saved"] or o["chose"]) else "refund"}
                                   for o, it, _ in self._affected(ev, ctx)]}

    def rules(self, ev, ctx):
        w = ctx.state.w
        alt = w.swap_candidate(w.skus[ev["sku"]])
        plan, seen = [], set()
        for o, it, _ in self._affected(ev, ctx):
            if alt and (o["saved"] or o["chose"]):
                plan.append(("apply_swap", {"order_id": o["id"], "sku": it["sku"], "to_sku": alt["id"]}))
                msg = f"{w.skus[it['sku']].get('name')} ran out, so we packed {alt['name']} as you chose. No extra charge."
            else:
                plan.append(("refund_item", {"order_id": o["id"], "sku": it["sku"]}))
                msg = f"{w.skus[it['sku']].get('name')} ran out. We refunded it instantly; the rest of your order is on its way."
            if o["id"] not in seen:
                seen.add(o["id"])
                plan.append(("notify_customer", {"order_id": o["id"], "message": msg}))
        return plan

    # ---------------------------------------------------------------- execute
    async def handle(self, ev, ctx):
        self._tally = {"swap": 0, "drop": 0, "cancel": 0, "filled": 0}
        return await super().handle(ev, ctx)

    def execute(self, name, a, ctx):
        st, ev = ctx.state, ctx.event
        oid = int(a["order_id"])
        if name == "notify_customer":
            st.notify(self.name, oid, str(a["message"])[:220])
            return None
        o = st.order(oid)
        it = ctx.find_item(o, a["sku"])
        trip = next((tr for oo, ii, tr in self._affected(ev, ctx) if ii is it), None)
        if name == "apply_swap":
            st.apply_swap(self.name, oid, it, a["to_sku"], trip)
            self._tally["swap"] += 1
            return None
        if name == "refund_item":
            r = st.refund(self.name, oid, it)
            self._tally["drop"] += 1
            if r == "cancel":
                self._tally["cancel"] += 1
            return None
        raise ValueError(name)

    def post(self, ev, ctx):
        # Completeness guarantee (priority rule 3): nothing is left unresolved.
        left = self._affected(ev, ctx)
        if left:
            alt = ctx.state.w.swap_candidate(ctx.state.w.skus[ev["sku"]])
            for o, it, trip in left:
                if alt and (o["saved"] or o["chose"]):
                    ctx.state.apply_swap(self.name, o["id"], it, alt["id"], trip)
                    self._tally["swap"] += 1
                else:
                    if ctx.state.refund(self.name, o["id"], it) == "cancel":
                        self._tally["cancel"] += 1
                    self._tally["drop"] += 1
                self._tally["filled"] += 1
        tl = self._tally
        total = tl["swap"] + tl["drop"]
        if not total:
            return
        name = ctx.state.w.skus[ev["sku"]]["name"]
        where = "mid-pick" if ev["type"] == "pick_not_found" else "waiting in the pool"
        text = (f"{total} live order line{'s' if total > 1 else ''} with {name} ({where}): "
                f"{tl['swap']} customer-chosen swap{'' if tl['swap'] == 1 else 's'} applied, "
                f"{tl['drop']} item{'' if tl['drop'] == 1 else 's'} refunded instantly"
                + (f" ({tl['cancel']} customer{'s' if tl['cancel'] > 1 else ''} then cancelled)" if tl["cancel"] else "")
                + ". No swap was guessed.")
        if tl["filled"]:
            text += f" Rules resolved {tl['filled']} line{'s' if tl['filled'] > 1 else ''} the model skipped."
        ctx.state.log(self.name, text, via=getattr(self, "_via", "rules"), story="liveorders")

    def describe(self, name, a, ctx):
        return {"apply_swap": f"Swap {a.get('sku')} → {a.get('to_sku')} in #{a.get('order_id')}.",
                "refund_item": f"Refund {a.get('sku')} in #{a.get('order_id')}.",
                "notify_customer": f"Tell #{a.get('order_id')}: {a.get('message')}"}.get(name, name)
