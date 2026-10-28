"""Shared store state: the one place the agents coordinate through.

Every field has exactly one owner. An agent may read anything, but it can only
write the fields it owns; a write to someone else's field raises OwnershipError.
This is what keeps four independent agents in sync without them calling each
other directly: Ledger lowers a trust score, and the optimiser, Promise and the
customer app all see it on their next read.
"""
from sim.constants import FAST_MOVERS, LIMITS, MODES

# field -> owner. Readers are listed for the UI's coordination table.
OWNERSHIP = {
    "sku.trust":          ("ledger", "Optimiser, Promise, Captain, customer app"),
    "sku.visibility":     ("ledger", "Customer app, Promise"),
    "sku.record":         ("ledger", "Reorder system, Captain"),
    "count_and_refill":   ("ledger", "Store associates (handheld)"),
    "mode_and_limits":    ("captain", "Optimiser"),
    "front_racks":        ("captain", "Optimiser (routing), store associates"),
    "approvals_and_brief": ("captain", "Shift lead"),
    "release_policy":     ("relay", "Optimiser, packing bench"),
    "priority":           ("relay", "Optimiser"),
    "order_items":        ("promise", "Picker handheld, packing bench"),
    "customer_messages":  ("promise", "Customer app"),
}


class OwnershipError(PermissionError):
    pass


class StoreState:
    def __init__(self, world, logger):
        self.w = world
        self._log = logger

    # ------------------------------------------------------------ plumbing
    def require(self, agent, field):
        owner = OWNERSHIP[field][0]
        if agent != owner:
            raise OwnershipError(f"{agent} tried to write '{field}', which {owner} owns")

    @property
    def t(self):
        return self.w.t

    def log(self, agent, text, hi=None, via="rules", story=None, shadow=False, why=None, meta=None):
        self._log(agent, text, hi=hi, via=via, story=story, shadow=shadow, why=why, meta=meta)

    def sku(self, sid):
        return self.w.skus.get(sid)

    def order(self, oid):
        return self.w.by_id.get(oid)

    # ------------------------------------------------------------ Ledger writes
    def set_trust(self, agent, sid, value):
        self.require(agent, "sku.trust")
        self.w.skus[sid]["trust"] = round(max(0.0, min(0.99, float(value))), 2)

    def lower_aisle_trust(self, agent, sid, delta=0.08):
        self.require(agent, "sku.trust")
        s = self.w.skus[sid]
        for x in self.w.skus.values():
            if x is not s and x["aisle"] == s["aisle"] and not x["hidden"]:
                x["trust"] = round(max(0.3, x["trust"] - delta), 2)

    def hide(self, agent, sid):
        self.require(agent, "sku.visibility")
        s = self.w.skus[sid]
        if s["hidden"]:
            return False
        s["hidden"] = True
        s["status"] = "hidden"
        self.w.stats["hidden"] += 1
        self.w.emit("sku_hidden", sku=sid)
        return True

    def restore(self, agent, sid):
        self.require(agent, "sku.visibility")
        s = self.w.skus[sid]
        s["hidden"] = False
        s["counting"] = False
        s["status"] = "live"
        self.w.stats["fixed"] += 1

    def correct_record(self, agent, sid, value):
        self.require(agent, "sku.record")
        self.w.skus[sid]["record"] = int(value)

    def request_count(self, agent, sid, mode):
        self.require(agent, "count_and_refill")
        s = self.w.skus[sid]
        if s["counting"]:
            return False
        if mode == "check":
            s["status"] = "checking"
        elif s["status"] != "hidden":
            s["status"] = "counting"
        self.w.schedule_count(sid, mode)
        return True

    def request_refill(self, agent, sid, qty):
        self.require(agent, "count_and_refill")
        self.w.schedule_refill(sid, int(qty))

    def mark_out(self, agent, sid):
        self.require(agent, "count_and_refill")
        s = self.w.skus[sid]
        s["status"] = "out"
        s["true_out"] = True
        s["counting"] = False

    def finish_check(self, agent, sid):
        self.require(agent, "count_and_refill")
        s = self.w.skus[sid]
        s["counting"] = False
        s["last_check"] = self.w.t
        if not s["hidden"]:
            s["status"] = "live"

    # ------------------------------------------------------------ Captain writes
    def set_mode(self, agent, mode):
        self.require(agent, "mode_and_limits")
        prev = self.w.mode
        self.w.mode = mode
        self.w.lim = dict(LIMITS[mode])
        self.w.down = 0
        return prev

    def set_limits(self, agent, max_batch=None, hold_s=None):
        self.require(agent, "mode_and_limits")
        if max_batch is not None:
            self.w.lim["max"] = int(max_batch)
        if hold_s is not None:
            self.w.lim["hold"] = int(hold_s)

    def set_front_racks(self, agent, on):
        self.require(agent, "front_racks")
        for sid in FAST_MOVERS:
            self.w.skus[sid]["fwd"] = bool(on)

    def request_approval(self, agent, kind, text):
        self.require(agent, "approvals_and_brief")
        if any(a["id"] == kind for a in self.w.approvals):
            return False
        self.w.approvals.insert(0, {"id": kind, "text": text, "t": self.w.t, "status": "pending"})
        return True

    def set_brief(self, agent, hi, en):
        self.require(agent, "approvals_and_brief")
        self.w.brief = {"t": self.w.t, "hi": hi, "en": en, "agent": agent}

    # ------------------------------------------------------------ Relay writes
    def set_release_policy(self, agent, bench_cap=None, pace_slack=None):
        self.require(agent, "release_policy")
        if bench_cap is not None:
            self.w.release_policy["bench_cap"] = int(bench_cap)
        if pace_slack is not None:
            self.w.release_policy["pace_slack"] = int(pace_slack)

    def boost(self, agent, oid, secs=180):
        self.require(agent, "priority")
        self.w.priority[oid] = self.w.t + secs

    # ------------------------------------------------------------ Promise writes
    def apply_swap(self, agent, oid, it, alt_sid, trip=None):
        self.require(agent, "order_items")
        o = self.w.by_id[oid]
        self.w.swap_item(o, it, self.w.skus[alt_sid], trip)

    def refund(self, agent, oid, it):
        self.require(agent, "order_items")
        return self.w.drop_item(self.w.by_id[oid], it)

    def notify(self, agent, oid, msg):
        self.require(agent, "customer_messages")
        o = self.w.by_id.get(oid)
        if o is not None:
            o.setdefault("messages", []).append({"t": self.w.t, "text": msg})
        self.w.stats["notified"] += 1

    # ------------------------------------------------------------ derived reads
    def mode_index(self, m=None):
        return MODES.index(m or self.w.mode)
