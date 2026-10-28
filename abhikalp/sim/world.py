"""The simulated dark store: orders, pickers, shelves, packing bench and riders.

This is the *world*, not the agents. It moves pickers, empties shelves and hands
bags to riders, and it emits events (a picker found an empty slot, a minute
passed, a count finished). It never decides anything an agent owns.

Two policies run the floor:
  'smart'  - the floor follows the shared state the agents write (SyncStore).
  'static' - today's plan: first-in-first-out, fixed route, hide after repeat
             reports, refund missing items. Used for the side-by-side baseline,
             for Shadow mode and for the fail-safe when the agent layer is off.
"""
import math
import random

from .constants import (BUFFER, CROSS_Y, DWELL, FAST_MOVERS, FWD_Y, HOME, HORIZON, LIMITS, PACK,
                        PICKERS, SHRINK, SKU_DEFS, SLOT_Y, SPEED_PX, cx, lambda_at)


# ---------------------------------------------------------------- orders
def make_order(r, t, oid):
    x = r.random()
    n = 1 if x < 0.3 else 2 if x < 0.65 else 3 if x < 0.9 else 4
    total = sum(d[8] for d in SKU_DEFS)
    items = []
    while len(items) < n:
        x, acc = r.random() * total, 0
        for d in SKU_DEFS:
            acc += d[8]
            if x <= acc:
                if d[0] not in items:
                    items.append(d[0])
                break
    return {"id": oid, "placed": round(t), "items": items, "ride": 170 + int(r.random() * 100),
            "riderIn": 150 + int(r.random() * 210), "saved": r.random() < 0.35,
            "chose": r.random() < 0.6, "cancelIfMissing": r.random() < 0.5}


def gen_orders(seed=7):
    r, out, t, oid = random.Random(seed), [], 0.0, 4101
    while True:
        t += -math.log(1 - r.random()) / (lambda_at(t) / 60)
        if t >= HORIZON:
            break
        out.append(make_order(r, t, oid))
        oid += 1
    k = 0  # demo: a few milk orders right after the milk shelf silently empties
    for o in out:
        if 1575 <= o["placed"] <= 1720 and k < 4 and "milk" not in o["items"]:
            o["items"].append("milk")
            k += 1
    return out


def _copy_order(o):
    c = dict(o)
    c["items"] = list(o["items"])
    return c


# ---------------------------------------------------------------- world
class World:
    def __init__(self, orders, policy="smart"):
        self.policy = policy            # 'smart' or 'static' (who runs the floor)
        self.agents_on = policy == "smart"
        self.t = 0
        self.arr = [_copy_order(o) for o in orders]
        self.ai = 0
        self.skus = {}
        for d in SKU_DEFS:
            self.skus[d[0]] = {
                "id": d[0], "name": d[1], "aisle": d[2], "slot": d[3], "temp": d[4], "price": d[5],
                "cat": d[6], "kg": d[7], "pop": d[8], "shelf": d[9], "record": d[9], "backroom": d[10],
                "trust": d[11] if policy == "smart" else 1.0, "hidden": False, "status": "live",
                "fwd": False, "reports": [], "counting": False, "restock_at": 0, "true_out": False,
                "last_check": 0}
        self.orders, self.by_id, self.pool, self.trips = [], {}, [], []
        self.ev = []                    # scheduled physical callbacks [(t, fn)]
        self.outbox = []                # events for the agent layer
        self.pickers = [{"id": i, "name": n, "trip": None, "join_at": 0, "floater": False}
                        for i, n in enumerate(PICKERS)]
        self.bench = {"q": [], "busy": [0, 0], "ready": []}
        # System state written by agents (through StoreState, which enforces ownership)
        self.mode = "normal"
        self.lim = dict(LIMITS["normal"])
        self.release_policy = {"bench_cap": 5, "pace_slack": 90, "bench_order": "rider_first"}
        self.priority = {}              # order id -> expiry time (Relay boosts)
        self.approvals = []
        self.brief = None
        # Metrics
        self.stats = {k: 0 for k in (
            "placed", "done", "onTime", "cancels", "swaps", "refunds", "notFound", "hidden", "walk",
            "pickSum", "pickN", "riderWait", "riderWaitN", "batches", "batchedOrders", "lost", "asked",
            "prevented", "conflicts", "reroutes", "missingHits", "fixed", "autoSwaps", "bufferHolds",
            "notified")}
        self.arr_min = [0] * 240
        self.backlog = []
        self.u_now = 0.0
        self.u_fc = 0.0
        self.down = 0
        self.trip_seq = 0
        self.optimiser = None           # set by the runtime for the smart policy
        self.log = []                   # world-side log lines (used by optimiser/policy notes)
        self.decisions = []
        self.last_bench_alert = -999
        self.bench_alerted = False

    # ------------------------------------------------------------ helpers
    @property
    def smart(self):
        return self.policy == "smart"

    def emit(self, kind, **data):
        self.outbox.append({"type": kind, "t": self.t, **data})

    def drain(self):
        out, self.outbox = self.outbox, []
        return out

    def schedule(self, t, fn):
        self.ev.append((t, fn))

    def active_pickers(self):
        return [p for p in self.pickers if p["join_at"] <= self.t]

    def active_orders(self):
        return [o for o in self.orders if o["status"] not in ("done", "cancelled")]

    def recent_lambda(self):
        m, n, k = self.t // 60, 0, 0
        for i in range(m - 1, max(0, m - 10) - 1, -1):
            n += self.arr_min[i]
            k += 1
        return n / k if k else 0

    def avg_pick(self):
        return self.stats["pickSum"] / self.stats["pickN"] if self.stats["pickN"] else 0

    def pos_of(self, s):
        return {"x": cx(s["aisle"]), "y": FWD_Y if s["fwd"] else SLOT_Y[s["slot"]]}

    @staticmethod
    def path_pts(a, b):
        if a["x"] == b["x"]:
            return [{"x": b["x"], "y": b["y"]}]
        return [{"x": a["x"], "y": CROSS_Y}, {"x": b["x"], "y": CROSS_Y}, {"x": b["x"], "y": b["y"]}]

    @staticmethod
    def dist(a, b):
        if a["x"] == b["x"]:
            return abs(a["y"] - b["y"])
        return abs(a["y"] - CROSS_Y) + abs(a["x"] - b["x"]) + abs(CROSS_Y - b["y"])

    def order_stops(self, ids):
        arr = [self.skus[i] for i in ids]
        if not self.smart:
            return sorted(arr, key=lambda s: (s["aisle"], s["slot"]))
        # SyncStore route: low-trust ambient items first (fail fast), cold chain last (aisles 6-8)
        return sorted(arr, key=lambda s: (0 if (s["temp"] == "amb" and s["trust"] < 0.5) else 1,
                                          s["aisle"], s["slot"]))

    def route_length(self, ids):
        pos, length = HOME, 0
        for s in self.order_stops(list(ids)):
            p = self.pos_of(s)
            length += self.dist(pos, p)
            pos = p
        return length + self.dist(pos, HOME)

    def est_dur(self, ids):
        return self.route_length(ids) / SPEED_PX + DWELL * len(ids)

    @staticmethod
    def open_skus(o):
        return [i["sku"] for i in o["items"] if i["st"] == "open"]

    def slack_of(self, o):
        ids = self.open_skus(o)
        d = self.est_dur(ids) if ids else 0
        return min(o["D"], o["rider_arrive"]) - self.t - d - PACK

    def swap_candidate(self, s):
        """Cheapest same-category item of equal or higher value that the system trusts."""
        best = None
        for x in self.skus.values():
            if x is s or x["cat"] != s["cat"] or x["price"] < s["price"] or x["hidden"] or x["record"] <= 0:
                continue
            if self.smart and x["trust"] < 0.7:
                continue
            if best is None or x["price"] < best["price"]:
                best = x
        return best

    # ------------------------------------------------------------ main step
    def step(self):
        self.t += 1
        t = self.t
        while self.ai < len(self.arr) and self.arr[self.ai]["placed"] <= t:
            self.place(self.arr[self.ai])
            self.ai += 1
        if self.ev:
            due = sorted([e for e in self.ev if e[0] <= t], key=lambda e: e[0])
            if due:
                self.ev = [e for e in self.ev if e[0] > t]
                for _, fn in due:
                    fn()
        for st, sku, val in SHRINK:
            if st == t:
                self.shrink(sku, val)
        for p in self.pickers:
            if p["trip"] and p["trip"]["end"] <= t:
                self.finish_trip(p["trip"])
        if t % 10 == 0:
            self.restock()
        if t % 5 == 0:
            if self.smart and self.optimiser:
                self.optimiser(self)
            else:
                self.optimise_static()
        self.pack_bench()
        self.handover()
        if t % 60 == 0:
            self.minute()

    def shrink(self, sid, val):
        s = self.skus[sid]
        s["shelf"] = min(s["shelf"], val)

    def insert_orders(self, lst):
        rest = self.arr[self.ai:] + [_copy_order(o) for o in lst]
        rest.sort(key=lambda o: o["placed"])
        self.arr = self.arr[:self.ai] + rest

    # ------------------------------------------------------------ checkout
    def place(self, src):
        t = self.t
        o = {"id": src["id"], "placed": t, "ride": src["ride"], "rider_arrive": t + src["riderIn"],
             "D": t + 600 - src["ride"], "items": [], "saved": src["saved"], "chose": False,
             "cancel_if_missing": src["cancelIfMissing"], "status": "pool", "pick_time": 0, "legs": 0,
             "key": src["items"][0], "user": bool(src.get("user")), "asked": []}
        for sid in src["items"]:
            s = self.skus[sid]
            if s["hidden"]:
                # The app no longer sells it: the customer either takes the offered swap or skips it.
                alt = self.swap_candidate(s)
                if alt and src["chose"]:
                    o["items"].append({"sku": alt["id"], "orig": sid, "st": "open", "from": sid})
                self.stats["prevented"] += 1
                continue
            o["items"].append({"sku": sid, "orig": sid, "st": "open"})
            if self.smart and s["trust"] < 0.7:
                # Promise's checkout question for low-trust items
                self.stats["asked"] += 1
                o["asked"].append(sid)
                if src["chose"]:
                    o["chose"] = True
        if "forceChoice" in src:
            o["chose"] = src["forceChoice"]
        if not o["items"]:
            self.stats["lost"] += 1
            return
        if not any(i["orig"] == o["key"] for i in o["items"]):
            o["key"] = o["items"][0]["orig"]
        self.arr_min[min(len(self.arr_min) - 1, t // 60)] += 1
        self.pool.append(o)
        self.orders.append(o)
        self.by_id[o["id"]] = o
        self.stats["placed"] += 1
        if len(self.orders) > 600:
            self.orders = [x for x in self.orders if x["status"] not in ("done", "cancelled")
                           or t - (x.get("handed_at") or x.get("cancelled_at") or t) < 600]

    # ------------------------------------------------------------ picking
    def optimise_static(self):
        idle = [p for p in self.active_pickers() if not p["trip"]]
        if not idle or not self.pool:
            return
        self.pool = sorted([o for o in self.pool if o["status"] == "pool"], key=lambda o: o["placed"])
        while idle and self.pool:
            o = self.pool.pop(0)
            if not self.open_skus(o):
                continue
            self.plan_trip(idle.pop(0), [o], None)

    def plan_trip(self, p, orders, zone=None, zone_name=None):
        t0 = self.t
        groups = {}
        for o in orders:
            for it in o["items"]:
                if it["st"] != "open":
                    continue
                s = self.skus[it["sku"]]
                if zone and s["aisle"] not in zone:
                    continue
                groups.setdefault(s["id"], []).append((o, it))
                it["st"] = "queued"
        stops = self.order_stops(list(groups.keys()))
        trip = {"id": self.trip_seq + 1, "p": p, "orders": orders, "zone": zone_name,
                "pts": [{"x": HOME["x"], "y": HOME["y"], "t": t0}], "start": t0, "end": t0, "len": 0,
                "aisles": {s["aisle"] for s in stops}, "stops": [s["id"] for s in stops],
                "extended": False, "rr": 0, "done": False}
        self.trip_seq += 1
        pos, T = dict(HOME), float(t0)
        others = [x for x in self.trips if x["end"] > t0]
        for s in stops:
            k = sum(1 for x in others if s["aisle"] in x["aisles"])
            f = 1 + (0.05 if self.smart else 0.15) * k
            if self.smart and k > 0 and trip["rr"] < 2:
                trip["rr"] += 1
                self.stats["reroutes"] += 1
            for q in self.path_pts(pos, self.pos_of(s)):
                d = abs(q["x"] - pos["x"]) + abs(q["y"] - pos["y"])
                T += d / SPEED_PX * f
                trip["len"] += d
                trip["pts"].append({"x": q["x"], "y": q["y"], "t": T})
                pos = q
            lst = groups[s["id"]]
            need = len(lst)
            got = min(need, max(0, s["shelf"]))
            s["shelf"] -= got
            s["record"] = max(0, s["record"] - got)
            for _, it in lst[:got]:
                it["st"] = "picked"
            dwell = DWELL + 3 * (got - 1) if got > 0 else 0
            if got < need:
                miss = lst[got:]
                for _, it in miss:
                    it["st"] = "missing"
                dwell += 8 if self.smart else 40   # smart: tap "not found" and move on
                self.stats["missingHits"] += len(miss)
                at = math.ceil(T + dwell)
                self.schedule(at, lambda s=s, miss=miss, trip=trip: self.on_not_found(s, miss, trip))
            elif self.smart:
                s["trust"] = min(0.98, s["trust"] + 0.01)
            T += dwell
            trip["pts"].append({"x": pos["x"], "y": pos["y"], "t": T})
        for q in self.path_pts(pos, HOME):
            d = abs(q["x"] - pos["x"]) + abs(q["y"] - pos["y"])
            T += d / SPEED_PX
            trip["len"] += d
            trip["pts"].append({"x": q["x"], "y": q["y"], "t": T})
            pos = q
        trip["end"] = T
        self.trips.append(trip)
        p["trip"] = trip
        self.stats["walk"] += trip["len"]
        for o in orders:
            if o["status"] in ("pool", "picking"):
                o["status"] = "picking"
                o["legs"] += 1
                o.setdefault("pick_start", t0)
        if len(self.trips) > 80:
            self.trips = [x for x in self.trips if x["end"] > self.t - 2]
        return trip

    def pos_at(self, tr, t):
        pts = tr["pts"]
        if t <= pts[0]["t"]:
            return pts[0]
        if t >= pts[-1]["t"]:
            return pts[-1]
        for i in range(1, len(pts)):
            if t <= pts[i]["t"]:
                a, b = pts[i - 1], pts[i]
                f = 1 if b["t"] == a["t"] else (t - a["t"]) / (b["t"] - a["t"])
                return {"x": a["x"] + (b["x"] - a["x"]) * f, "y": a["y"] + (b["y"] - a["y"]) * f}
        return pts[-1]

    def extend_trip(self, tr, sec):
        at = self.t
        pos = self.pos_at(tr, at)
        out = [q for q in tr["pts"] if q["t"] <= at]
        out += [{"x": pos["x"], "y": pos["y"], "t": at}, {"x": pos["x"], "y": pos["y"], "t": at + sec}]
        out += [{"x": q["x"], "y": q["y"], "t": q["t"] + sec} for q in tr["pts"] if q["t"] > at]
        tr["pts"] = out
        tr["end"] += sec

    def finish_trip(self, tr):
        tr["p"]["trip"] = None
        tr["done"] = True
        share = (tr["end"] - tr["start"]) / len(tr["orders"])
        for o in tr["orders"]:
            o["pick_time"] += share
            o["legs"] -= 1
            if o["legs"] <= 0 and o["status"] == "picking":
                self.stats["pickSum"] += o["pick_time"]
                self.stats["pickN"] += 1
                if any(i["st"] == "picked" for i in o["items"]):
                    o["status"] = "bench"
                    o["bench_at"] = self.t
                    self.bench["q"].append(o)
                else:
                    self.cancel(o)

    def cancel(self, o):
        if o["status"] in ("done", "cancelled"):
            return
        o["status"] = "cancelled"
        o["cancelled_at"] = self.t
        self.stats["cancels"] += 1
        if o in self.pool:
            self.pool.remove(o)
        if o in self.bench["q"]:
            self.bench["q"].remove(o)

    # ------------------------------------------------------------ missing items
    def on_not_found(self, s, miss, trip):
        self.stats["notFound"] += 1
        if self.smart or self.agents_on:
            # Tell the agent layer. In Live mode Ledger and Promise act on it;
            # in Shadow mode they only log what they would have done.
            self.emit("pick_not_found", sku=s["id"], record=s["record"] + len(miss),
                      misses=[(o["id"], it) for o, it in miss], trip=trip)
        if not self.smart:
            self._static_not_found(s, miss)

    def _static_not_found(self, s, miss):
        t = self.t
        s["reports"] = [x for x in s["reports"] if t - x < 900] + [t]
        if not s["hidden"] and len(s["reports"]) >= 2:
            s["hidden"] = True
            s["status"] = "hidden"
            nxt = math.ceil((t + 1) / 1800) * 1800       # next scheduled cycle count
            self.schedule(nxt, lambda: self._static_count(s))
        for o, it in miss:
            if o["status"] == "cancelled":
                continue
            self.drop_item(o, it)

    def _static_count(self, s):
        s["record"] = s["shelf"]
        if s["backroom"] > 0:
            s["status"] = "refilling"

            def refill():
                q = min(16, s["backroom"])
                s["shelf"] += q
                s["record"] = s["shelf"]
                s["backroom"] -= q
                s["hidden"] = False
                s["status"] = "live"
            self.schedule(self.t + 180, refill)
        else:
            s["status"] = "out"
            s["true_out"] = True

    def is_key(self, o, it):
        return it["orig"] == o["key"]

    def swap_item(self, o, it, alt, trip=None):
        """Apply a customer-approved swap. Mid-pick, the picker grabs the swap on the way."""
        it["from"] = it["sku"]
        it["sku"] = alt["id"]
        self.stats["swaps"] += 1
        if trip and not trip["done"] and it["st"] == "missing":
            it["st"] = "picked"
            alt["shelf"] = max(0, alt["shelf"] - 1)
            alt["record"] = max(0, alt["record"] - 1)
            if not trip["extended"]:
                trip["extended"] = True
                self.extend_trip(trip, 15)
        else:
            it["st"] = "open"

    def drop_item(self, o, it):
        """Refund one item; the customer may then cancel the whole order."""
        it["st"] = "dropped"
        self.stats["refunds"] += 1
        if (self.is_key(o, it) and o["cancel_if_missing"]) or all(i["st"] == "dropped" for i in o["items"]):
            self.cancel(o)
            return "cancel"
        return "drop"

    # ------------------------------------------------------------ physical tasks requested by agents
    def schedule_count(self, sid, mode):
        s = self.skus[sid]
        s["counting"] = True
        self.schedule(self.t + 240, lambda: self.emit(
            "count_result", sku=sid, counted=s["shelf"], backroom=s["backroom"], mode=mode,
            record=s["record"]))

    def schedule_refill(self, sid, qty):
        s = self.skus[sid]
        s["status"] = "refilling"

        def done():
            q = min(qty, s["backroom"])
            s["shelf"] += q
            s["record"] = s["shelf"]       # put-away is scanned, so the record follows
            s["backroom"] -= q
            self.emit("refill_done", sku=sid, qty=q, shelf=s["shelf"])
        self.schedule(self.t + 180, done)

    def restock(self):
        for s in self.skus.values():
            if s["restock_at"] and self.t >= s["restock_at"]:
                q = min(12, s["backroom"])
                s["shelf"] += q
                s["record"] += q
                s["backroom"] -= q
                s["restock_at"] = 0
            elif not s["restock_at"] and not s["hidden"] and s["record"] <= 4 and s["backroom"] > 0:
                s["restock_at"] = self.t + 120

    # ------------------------------------------------------------ bench and handover
    def pack_bench(self):
        t, b = self.t, self.bench
        if self.smart and self.release_policy.get("bench_order") == "rider_first" and len(b["q"]) > 1:
            b["q"].sort(key=lambda o: o["rider_arrive"])
        i = 0
        while i < 2:
            if b["busy"][i] <= t and b["q"]:
                o = b["q"].pop(0)
                if o["status"] != "bench":
                    continue
                o["status"] = "packing"
                b["busy"][i] = t + PACK

                def ready(o=o):
                    if o["status"] != "packing":
                        return
                    o["status"] = "ready"
                    o["ready_at"] = self.t
                    b["ready"].append(o)
                self.schedule(t + PACK, ready)
            i += 1
        if self.smart:
            n = len(b["q"])
            if n >= self.release_policy["bench_cap"] and not self.bench_alerted:
                self.bench_alerted = True
                self.emit("bench_congested", queue=n, ready=len(b["ready"]))
            elif n <= 2 and self.bench_alerted:
                self.bench_alerted = False
                self.emit("bench_cleared", queue=n)

    def handover(self):
        t = self.t
        keep = []
        for o in self.bench["ready"]:
            if t >= o["rider_arrive"]:
                o["status"] = "done"
                o["handed_at"] = t
                self.stats["done"] += 1
                if t <= max(o["D"], o["rider_arrive"]):
                    self.stats["onTime"] += 1
                self.stats["riderWait"] += t - o["rider_arrive"]
                self.stats["riderWaitN"] += 1
            else:
                keep.append(o)
        self.bench["ready"] = keep

    # ------------------------------------------------------------ minute metrics
    def minute(self):
        t = self.t
        n = max(1, len(self.active_pickers()))
        self.u_now = self.recent_lambda() * 1.5 / n
        self.u_fc = lambda_at(t + 900) * (1 + 0.05 * math.sin(t / 700)) * 1.5 / n
        self.backlog = (self.backlog + [len(self.pool)])[-4:]
        self.priority = {k: v for k, v in self.priority.items() if v > t}
        self.emit("minute", u_now=self.u_now, u_fc=self.u_fc, backlog=list(self.backlog),
                  pickers=n, pool=len(self.pool))

    # ------------------------------------------------------------ staffing (approved by the shift lead)
    def add_picker(self, name, delay):
        self.pickers.append({"id": len(self.pickers), "name": name, "trip": None,
                             "join_at": self.t + delay, "floater": True})


__all__ = ["World", "gen_orders", "make_order", "FAST_MOVERS", "BUFFER"]
