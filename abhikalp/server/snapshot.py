"""Turn a running Session into the JSON the control room renders."""
from core.state import OWNERSHIP
from sim.constants import BUFFER, HORIZON, LIMITS, MODE_LABEL, MODE_METHOD, PX_M, fmt

CHECKOUT_ITEMS = ["milk", "bread", "breadb", "eggs", "curd", "paneer", "chips", "popcorn", "cola", "biscuit",
                  "coffee", "icv"]


def _stats(w):
    s = dict(w.stats)
    s["avgPick"] = round(w.avg_pick(), 1)
    s["walkPerOrder"] = round(w.stats["walk"] * PX_M / max(1, w.stats["pickN"]), 1)
    s["riderWaitAvg"] = round(w.stats["riderWait"] / w.stats["riderWaitN"], 1) if w.stats["riderWaitN"] else 0
    return s


def _trip(tr, t):
    return {"id": tr["id"], "picker": tr["p"]["name"], "zone": tr["zone"], "n": len(tr["orders"]),
            "ids": [o["id"] for o in tr["orders"]], "start": tr["start"], "end": round(tr["end"], 1),
            "stops": len(tr["stops"]), "len": round(tr["len"]), "pts": [[round(p["x"], 1), round(p["y"], 1),
                                                                         round(p["t"], 2)] for p in tr["pts"]]}


def _state_rows(sess):
    w = sess.smart
    t = w.t
    low = min(w.skus.values(), key=lambda s: s["trust"])
    pool = [o for o in w.pool if o["status"] == "pool"]
    tight = min(pool, key=w.slack_of) if pool else None
    aisles = [0] * 9
    for tr in w.trips:
        if not tr["done"] and tr["end"] > t:
            for a in tr["aisles"]:
                aisles[a] += 1
    busy = max(range(1, 9), key=lambda a: aisles[a])
    saved = sum(1 for o in w.active_orders() if o["saved"] or o["chose"])
    live = {
        "sku.trust": f"{low['name']} at {low['trust']:.2f} (lowest)",
        "sku.visibility": f"{sum(1 for s in w.skus.values() if s['hidden'])} hidden in the app",
        "sku.record": f"{sum(1 for s in w.skus.values() if s['record'] > s['shelf'])} records above the real shelf",
        "count_and_refill": f"{sum(1 for s in w.skus.values() if s['counting'])} tasks open",
        "mode_and_limits": (f"{MODE_LABEL[w.mode]} · ≤{w.lim['max']} per trolley · hold ≤{w.lim['hold']} s"
                            if sess.layer == "live" else "Agents not driving the floor"),
        "front_racks": "Fast movers in front" if any(s["fwd"] for s in w.skus.values()) else "Home slots",
        "approvals_and_brief": f"{sum(1 for a in w.approvals if a['status'] == 'pending')} awaiting the shift lead",
        "release_policy": f"bench cap {w.release_policy['bench_cap']} · pace slack {w.release_policy['pace_slack']} s",
        "priority": (f"#{tight['id']} tightest at {round(w.slack_of(tight))} s" if tight else "No orders waiting")
                    + (f" · {len(w.priority)} boosted" if w.priority else ""),
        "order_items": f"{saved} live orders have a saved or chosen swap",
        "customer_messages": f"{w.stats['notified']} sent this shift",
    }
    return [{"field": f, "owner": o, "readers": r, "live": live[f]} for f, (o, r) in OWNERSHIP.items()] + [
        {"field": "trolleys_and_routes", "owner": "opt", "readers": "Captain, pickers",
         "live": f"Aisle A{busy} busiest ({aisles[busy]} trolleys)" if aisles[busy] else "All aisles clear"}]


def build(sess):
    w, X = sess.smart, sess.static
    t = w.t
    pickers = []
    for p in w.pickers:
        tr = p["trip"]
        pickers.append({"name": p["name"], "floater": p["floater"], "joining": p["join_at"] > t,
                        "trip": _trip(tr, t) if tr and not tr["done"] else None})
    orders = []
    for o in w.active_orders():
        row = {"id": o["id"], "user": o["user"], "status": o["status"], "riderIn": o["rider_arrive"] - t,
               "items": sum(1 for i in o["items"] if i["st"] != "dropped"),
               "swapped": sum(1 for i in o["items"] if i.get("from")), "boost": o["id"] in w.priority}
        if o["status"] in ("pool", "picking"):
            row["slack"] = round(w.slack_of(o))
        orders.append(row)
    orders.sort(key=lambda r: (r.get("slack", 10 ** 6), r["riderIn"]))
    skus = [{k: s[k] for k in ("id", "name", "aisle", "slot", "temp", "price", "cat", "shelf", "record", "backroom",
                               "trust", "hidden", "status", "fwd", "counting")} for s in w.skus.values()]
    swaps = {}
    for sid in CHECKOUT_ITEMS:
        alt = w.swap_candidate(w.skus[sid])
        swaps[sid] = {"id": alt["id"], "name": alt["name"], "price": alt["price"]} if alt else None
    trace = [{k: v for k, v in tr.items()} for tr in list(sess.bus.trace)[:40]]
    return {
        "type": "snap", "t": t, "clock": fmt(t), "horizon": HORIZON, "speed": sess.speed, "playing": sess.playing,
        "layer": sess.layer, "llmOn": sess.llm_on, "autoApprove": sess.auto_approve, "demo": sess.demo,
        "thinking": getattr(sess, "thinking", None), "llm": sess.llm.health(),
        "mode": w.mode, "lim": w.lim, "limits": LIMITS, "methods": MODE_METHOD, "uNow": round(w.u_now, 3),
        "uFc": round(w.u_fc, 3), "N": len(w.active_pickers()), "down": w.down, "buffer": BUFFER,
        "arrMin": w.arr_min[: t // 60 + 1], "skus": skus, "pickers": pickers,
        "bench": {"q": len(w.bench["q"]), "ready": len(w.bench["ready"])},
        "riders": sum(1 for o in w.active_orders() if o["rider_arrive"] <= t),
        "orders": orders[:40], "activeCount": len(orders), "pool": len(w.pool),
        "stats": _stats(w), "staticStats": _stats(X), "log": sess.logs[:150], "decisions": w.decisions[:10],
        "approvals": w.approvals, "brief": w.brief, "story": sess.story, "trace": trace,
        "agents": {n: a.public_stats() for n, a in sess.agents.items()}, "busCounts": dict(sess.bus.counts),
        "stateRows": _state_rows(sess), "releasePolicy": w.release_policy, "swaps": swaps,
        "checkout": CHECKOUT_ITEMS,
    }
