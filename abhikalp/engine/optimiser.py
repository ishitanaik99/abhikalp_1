"""Fast optimiser: plain code, not an agent.

Every 5 simulated seconds it batches orders, routes each trolley and assigns pickers.
It runs in milliseconds and never calls an LLM. The agents change only its inputs
and limits, all read from the shared store state:
  - Captain: mode, max orders per trolley, hold cap, fast movers in front racks
  - Relay:   release pacing (bench cap, pace slack) and priority boosts
  - Ledger:  trust scores (low-trust items are visited first so failures surface early)
"""
from sim.constants import BUFFER, PACK, mmss


def run(w):
    t = w.t
    idle = [p for p in w.active_pickers() if not p["trip"]]
    if not idle or not w.pool:
        return
    w.pool = [o for o in w.pool if o["status"] == "pool"]
    info = {}
    for o in w.pool:
        ids = w.open_skus(o)
        d = w.est_dur(ids)
        info[o["id"]] = {"ids": ids, "d": d,
                         "slack": min(o["D"], o["rider_arrive"]) - t - d - PACK}

    def boost(o):  # Relay priority boosts sort first among equals
        return 0 if o["id"] in w.priority else 1

    cand = sorted([o for o in w.pool if info[o["id"]]["ids"]],
                  key=lambda o: (info[o["id"]]["slack"] - (60 if boost(o) == 0 else 0)))

    # Relay's pacing: when the bench is congested, hold orders that still have spare time.
    rp = w.release_policy
    if len(w.bench["q"]) >= rp["bench_cap"]:
        before = len(cand)
        cand = [o for o in cand if info[o["id"]]["slack"] <= rp["pace_slack"]]
        if len(cand) < before and t - getattr(w, "_last_pace", -999) > 120:
            w._last_pace = t
            w.emit("log", agent="relay", via="policy",
                   text=f"Packing bench has {len(w.bench['q'])} bags waiting. Holding {before - len(cand)} "
                        f"orders that still have more than {rp['pace_slack']} s of buffer, so bags don’t pile up.")

    zone_mode = w.mode == "surge"
    while idle and cand:
        seed = cand[0]
        batch, ids, max_b = [seed], set(info[seed["id"]]["ids"]), w.lim["max"]
        while len(batch) < max_b:
            best, best_save, best_ids = None, 0, None
            cur_len = w.route_length(ids)
            for c in cand:
                if c in batch:
                    continue
                ci = info[c["id"]]
                u = ids | set(ci["ids"])
                if len(u) > 9:
                    continue
                u_len = w.route_length(u)
                save = cur_len + w.route_length(ci["ids"]) - u_len
                if save <= 20:
                    continue
                u_dur = u_len / 28 + 10 * len(u)
                ok = all(info[m["id"]]["slack"] - (u_dur - info[m["id"]]["d"]) >= BUFFER for m in batch + [c])
                if ok and save > best_save:
                    best, best_save, best_ids = c, save, u
            if not best:
                break
            batch.append(best)
            ids = best_ids
        min_slack = min(info[m["id"]]["slack"] for m in batch)
        waited = t - seed["placed"]
        full = len(batch) >= max_b
        urgent = min_slack <= BUFFER + 45
        time_up = waited >= w.lim["hold"]
        if not (full or urgent or time_up):
            break
        if urgent and not full and not time_up and w.lim["hold"] > 30:
            # Priority rule 2 (customer promise) beats rule 4 (efficiency)
            w.stats["bufferHolds"] += 1
            if t - getattr(w, "_last_conflict", -999) > 300 and min_slack > 0:
                w._last_conflict = t
                w.stats["conflicts"] += 1
                w.emit("log", agent="captain", via="rule",
                       text=f"Conflict settled: the optimiser wanted to hold #{seed['id']} for a better batch, "
                            f"but Relay reports its time buffer is down to {max(0, round(min_slack))} s. Rule 2 "
                            f"(customer promise) beats rule 4 (efficiency), so #{seed['id']} was released.")
        oldest = min(w.pool, key=lambda o: o["placed"])
        if (seed is not oldest and t - getattr(w, "_last_relay", -999) > 240
                and seed["rider_arrive"] < oldest["rider_arrive"] - 30):
            w._last_relay = t
            w.emit("log", agent="relay", via="policy", story="riderfirst",
                   text=f"Released #{seed['id']} ahead of older #{oldest['id']}: its rider arrives in "
                        f"{mmss(seed['rider_arrive'] - t)}, while #{oldest['id']}’s rider is "
                        f"{mmss(oldest['rider_arrive'] - t)} away.")
        for b in batch:
            w.pool.remove(b)
            cand.remove(b)
        solo = sum(w.route_length(info[b["id"]]["ids"]) for b in batch)
        union_len = w.route_length(ids)
        save_pct = round((1 - union_len / solo) * 100) if len(batch) > 1 and solo else 0
        if full:
            reason = f"Trolley full ({len(batch)} totes)"
        elif urgent:
            reason = f"Time buffer of #{seed['id']} down to {max(0, round(min_slack))} s"
        elif w.lim["hold"] == 0:
            reason = "Quiet hours: pick at once"
        else:
            reason = f"Held {waited} s; no better partner arrived"
        used = []
        if zone_mode and len(idle) >= 2:
            has_a = any(w.skus[i]["aisle"] <= 4 for i in ids)
            has_b = any(w.skus[i]["aisle"] > 4 for i in ids)
            if has_a:
                pa = idle.pop(0)
                used.append(pa)
                w.plan_trip(pa, batch, {1, 2, 3, 4}, "A")
            if has_b:
                pb = idle.pop(0)
                used.append(pb)
                w.plan_trip(pb, batch, {5, 6, 7, 8}, "B")
            method = "Zone pick-and-merge"
        else:
            p = idle.pop(0)
            used.append(p)
            w.plan_trip(p, batch, None)
            method = "Discrete" if len(batch) == 1 else "Cluster"
        w.decisions.insert(0, {"t": t, "ids": [o["id"] for o in batch], "method": method, "reason": reason,
                               "save": save_pct, "pickers": [p["name"] for p in used]})
        del w.decisions[30:]
        w.stats["batches"] += 1
        w.stats["batchedOrders"] += len(batch)
