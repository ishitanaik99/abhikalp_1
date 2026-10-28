"""Ledger: inventory truth.

Every failed pick is a sensor. Ledger keeps a trust score per item, hides an
item the moment a picker finds its slot empty, sends a 30-second count task,
corrects the record from the count and triggers the refill (or a reorder).
Hiding is reversible, so Ledger acts without asking the shift lead.
"""
from core.llm import tool

from .base import Agent

PROMPT = """You are LEDGER, the inventory-truth agent. You own each item's trust score (0-0.99), whether
it is visible in the customer app, its stock record, and count/refill tasks.

Events you handle:
- pick_not_found: a picker found the slot empty or short while the record shows stock (phantom stock).
  Expected: set_trust to 0.10 or lower; hide_item if it is not already hidden, so no new order takes it;
  lower_aisle_trust for its neighbours the first time; create_count_task(mode="empty") unless a count is open.
- trust_drift: an item's trust slipped below 0.60 with no recent count. Expected: create_count_task(mode="check").
- count_result: an associate counted the shelf. Expected: correct_record to the counted number when it
  differs from the record. If the count was for an empty slot: if counted > 0, restore_item and set_trust ~0.90;
  if counted is 0 and the backroom has stock, trigger_refill (up to 16); if the backroom is empty, raise_reorder.
  For a 'check' count that matches the record (within 1), set_trust to 0.95.
Never restore an item without a count or refill showing stock."""

TOOLS = [
    tool("set_trust", "Set an item's trust score.", {"sku": {"type": "string"}, "value": {"type": "number"}},
         ["sku", "value"]),
    tool("hide_item", "Hide the item in the customer app so no new order can take it.",
         {"sku": {"type": "string"}}, ["sku"]),
    tool("lower_aisle_trust", "Lower trust of other items in the same aisle (likely the same cause).",
         {"sku": {"type": "string"}}, ["sku"]),
    tool("create_count_task", "Send a 30-second shelf count to the nearest idle associate.",
         {"sku": {"type": "string"}, "mode": {"type": "string", "enum": ["empty", "check"]}}, ["sku", "mode"]),
    tool("correct_record", "Set the stock record to the counted quantity.",
         {"sku": {"type": "string"}, "value": {"type": "integer"}}, ["sku", "value"]),
    tool("trigger_refill", "Move stock from the backroom to the shelf (about 3 minutes).",
         {"sku": {"type": "string"}, "qty": {"type": "integer"}}, ["sku", "qty"]),
    tool("restore_item", "Make the item visible in the app again.", {"sku": {"type": "string"}}, ["sku"]),
    tool("raise_reorder", "The item is truly out: keep it hidden and send a reorder to the warehouse.",
         {"sku": {"type": "string"}}, ["sku"]),
]


class Ledger(Agent):
    name = "ledger"
    title = "Inventory truth"
    tier = "fast"
    triggers = {"pick_not_found", "trust_drift", "count_result", "refill_done"}
    senses = {"minute"}
    prompt = PROMPT
    tools = TOOLS

    # ---------------------------------------------------------------- sense (code)
    def sense(self, ev, ctx):
        """Trust decays slowly without counts; raise trust_drift before an order fails on it."""
        w = ctx.state.w
        for s in w.skus.values():
            if s["hidden"] or s["counting"] or s["status"] == "out":
                continue
            s["trust"] = round(max(0.3, s["trust"] - 0.004), 3)
            if s["trust"] < 0.6 and w.t - s.get("drift_at", -999) > 300:
                s["drift_at"] = w.t
                w.emit("trust_drift", sku=s["id"], trust=s["trust"])

    def wants_llm(self, ev, ctx):
        return ev["type"] != "refill_done"      # nothing to judge: refill landed, item comes back

    # ---------------------------------------------------------------- view for the LLM
    def view(self, ev, ctx):
        s = ctx.state.sku(ev["sku"])
        item = {"sku": s["id"], "name": s["name"], "aisle": s["aisle"], "record": s["record"],
                "trust": s["trust"], "hidden": s["hidden"], "status": s["status"],
                "count_task_open": s["counting"], "backroom": s["backroom"]}
        v = {"event": ev["type"], "time": ctx.rt.clock(), "item": item}
        if ev["type"] == "pick_not_found":
            v["record_when_picked"] = ev["record"]
            v["units_short"] = len(ev["misses"])
            v["aisle_neighbours"] = [{"sku": x["id"], "trust": x["trust"]} for x in ctx.state.w.skus.values()
                                     if x["aisle"] == s["aisle"] and x is not s]
        if ev["type"] == "count_result":
            v["count"] = {"mode": ev["mode"], "counted_on_shelf": ev["counted"], "record_at_count": ev["record"],
                          "backroom": ev["backroom"]}
        return v

    # ---------------------------------------------------------------- rules (fallback)
    def rules(self, ev, ctx):
        s = ctx.state.sku(ev["sku"])
        sid, typ = s["id"], ev["type"]
        if typ == "pick_not_found":
            plan = [("set_trust", {"sku": sid, "value": 0.1})]
            if not s["hidden"]:
                plan += [("hide_item", {"sku": sid}), ("lower_aisle_trust", {"sku": sid})]
            if not s["counting"]:
                plan.append(("create_count_task", {"sku": sid, "mode": "empty"}))
            return plan
        if typ == "trust_drift":
            return [("create_count_task", {"sku": sid, "mode": "check"})]
        if typ == "count_result":
            c, rec = ev["counted"], ev["record"]
            plan = []
            if c != rec:
                plan.append(("correct_record", {"sku": sid, "value": c}))
            if ev["mode"] == "check":
                plan.append(("set_trust", {"sku": sid, "value": 0.95 if c >= rec - 1 else 0.9}))
            elif c > 0:
                plan += [("restore_item", {"sku": sid}), ("set_trust", {"sku": sid, "value": 0.9})]
            elif ev["backroom"] > 0:
                plan.append(("trigger_refill", {"sku": sid, "qty": min(16, ev["backroom"])}))
            else:
                plan.append(("raise_reorder", {"sku": sid}))
            return plan
        if typ == "refill_done":
            return [("set_trust", {"sku": sid, "value": 0.95}), ("restore_item", {"sku": sid})]
        return []

    def required(self, ev, ctx):
        s = ctx.state.sku(ev["sku"])
        if ev["type"] == "pick_not_found":
            return ({"hide_item"} if not s["hidden"] else set()) | ({"create_count_task"} if not s["counting"] else set())
        if ev["type"] == "count_result":
            need = {"correct_record"} if ev["counted"] != ev["record"] else set()
            if ev["mode"] == "empty" and ev["counted"] <= 0:
                need.add("trigger_refill" if ev["backroom"] > 0 else "raise_reorder")
            return need
        return set()

    # ---------------------------------------------------------------- execute (writes shared state)
    def execute(self, name, a, ctx):
        st, sid = ctx.state, a["sku"]
        s = st.sku(sid)
        ev = ctx.event
        if name == "set_trust":
            old = s["trust"]
            st.set_trust(self.name, sid, a["value"])
            if ev["type"] == "count_result" and ev["mode"] == "check":
                c, rec = ev["counted"], ev["record"]
                if c >= rec - 1:
                    return f"Check on {s['name']}: record confirmed ({c} on the shelf). Trust {old:.2f} → {s['trust']:.2f}."
                return (f"Check on {s['name']} found {c} on the shelf, {rec - c} fewer than the record. "
                        f"Fixed before any order hit it. Trust {old:.2f} → {s['trust']:.2f}.")
            if ev["type"] == "trust_drift" and abs(old - s["trust"]) >= 0.005:
                return f"Trust for {s['name']}: {old:.2f} → {s['trust']:.2f}."
            return None   # on not-found and refill the hide/restore line already says it
        if name == "hide_item":
            if st.hide(self.name, sid):
                return (f"{s['name']}: a picker found the slot empty while the record shows "
                        f"{ev.get('record', s['record'])}. Hidden in the app, so no new order takes it.",
                        f"{s['name']} का शेल्फ खाली है। इसे ऐप से छिपा दिया और गिनती का काम भेज दिया है।", "empty")
            return None
        if name == "lower_aisle_trust":
            st.lower_aisle_trust(self.name, sid)
            return f"Neighbours of {s['name']} in aisle A{s['aisle']} get a small trust cut (same likely cause)."
        if name == "create_count_task":
            if st.request_count(self.name, sid, a["mode"]):
                if a["mode"] == "check":
                    return f"Trust for {s['name']} fell to {s['trust']:.2f} with no recent count. Sent a 30-second check before any order fails on it."
                return f"30-second count task for {s['name']} sent to the nearest idle associate."
            return None
        if name == "correct_record":
            old = s["record"]
            st.correct_record(self.name, sid, a["value"])
            return (f"Count for {s['name']}: {s['record']} on the shelf, {ev.get('backroom', s['backroom'])} in the "
                    f"backroom. Record corrected from {old}.", None, "count")
        if name == "trigger_refill":
            st.request_refill(self.name, sid, a["qty"])
            return f"Refill of {a['qty']} × {s['name']} from the backroom requested (about 3 minutes)."
        if name == "restore_item":
            st.restore(self.name, sid)
            return (f"{s['name']} is live again with {s['shelf']} units. Similar items get extra checks.", None, "live")
        if name == "raise_reorder":
            st.mark_out(self.name, sid)
            return f"{s['name']} is truly out of stock: none in the backroom. It stays hidden and a reorder went to the warehouse."
        raise ValueError(name)

    def post(self, ev, ctx):
        s = ctx.state.sku(ev["sku"]) if "sku" in ev else None
        if s is None:
            return
        if ev["type"] == "count_result":
            if ev["mode"] == "check" or (not s["hidden"] and s["status"] != "refilling"):
                ctx.state.finish_check(self.name, s["id"])
            if ev["mode"] == "check" and ev["counted"] < ev["record"] - 1:
                ctx.state.w.stats["fixed"] += 1
        if ev["type"] == "refill_done" and s["hidden"]:
            # The refill landed but the agent chose not to restore: keep the task open for a recount.
            s["counting"] = False

    def describe(self, name, a, ctx):
        s = ctx.state.sku(a.get("sku", ""))
        nm = s["name"] if s else a.get("sku")
        return {"set_trust": f"Set trust for {nm} to {a.get('value')}.",
                "hide_item": f"Hide {nm} in the app.", "lower_aisle_trust": f"Lower trust for {nm}'s aisle.",
                "create_count_task": f"Send a {a.get('mode')} count for {nm}.",
                "correct_record": f"Correct {nm}'s record to {a.get('value')}.",
                "trigger_refill": f"Refill {a.get('qty')} × {nm}.", "restore_item": f"Restore {nm}.",
                "raise_reorder": f"Reorder {nm}."}.get(name, name)
