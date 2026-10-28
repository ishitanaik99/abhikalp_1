"""Hard limits, enforced in code before any agent action runs.

An LLM can propose anything; only actions that pass these checks reach the
store. Each check returns (ok, why). A rejected action is logged as "blocked"
with the reason, so the shift lead can see what an agent tried to do.
"""
from sim.constants import MODES, OPS_BOUNDS

TRUST_RANGE = (0.0, 0.99)


def _sku(ctx, args):
    return ctx.state.sku(args.get("sku", ""))


def _ledger(tool, a, ctx):
    s = _sku(ctx, a)
    if s is None:
        return False, f"unknown item '{a.get('sku')}'"
    ev = ctx.event
    if tool == "set_trust":
        v = a.get("value")
        if not isinstance(v, (int, float)) or not TRUST_RANGE[0] <= v <= TRUST_RANGE[1]:
            return False, "trust must be a number between 0 and 0.99"
        if ev["type"] == "pick_not_found" and a["sku"] == ev["sku"] and v > 0.3:
            return False, "an item a picker just found empty cannot be trusted above 0.30"
    if tool == "correct_record":
        if ev["type"] != "count_result" or a["sku"] != ev["sku"]:
            return False, "a record can only be corrected from a count result"
        if int(a.get("value", -1)) != ev["counted"]:
            return False, f"record must match the count ({ev['counted']})"
    if tool == "restore_item":
        if s["status"] == "out" or (ev["type"] not in ("refill_done", "count_result")):
            return False, "an item comes back only after a refill or a count shows stock"
        if ev["type"] == "count_result" and ev["counted"] <= 0:
            return False, "the count found none on the shelf"
    if tool == "trigger_refill":
        q = a.get("qty", 0)
        if not isinstance(q, (int, float)) or q <= 0 or q > 24:
            return False, "refill quantity must be 1–24"
        if s["backroom"] <= 0:
            return False, "nothing in the backroom to refill from"
    if tool == "raise_reorder" and s["backroom"] > 0:
        return False, "backroom still has stock; refill instead"
    return True, ""


def _captain(tool, a, ctx):
    w = ctx.state.w
    if tool == "set_mode":
        m = a.get("mode")
        if m not in MODES:
            return False, f"unknown mode '{m}'"
        cur, nxt = MODES.index(w.mode), MODES.index(m)
        if nxt < cur:
            if nxt != cur - 1:
                return False, "step down one mode at a time"
            if w.down < 3:
                return False, f"hysteresis: load must stay low for 3 minutes before stepping down (now {w.down})"
    if tool == "set_limits":
        mb, hs = a.get("max_batch"), a.get("hold_s")
        if mb is not None and not 1 <= int(mb) <= OPS_BOUNDS["max_batch"]:
            return False, f"orders per trolley must be 1–{OPS_BOUNDS['max_batch']} (ops bound)"
        if hs is not None and not 0 <= int(hs) <= OPS_BOUNDS["hold_s"]:
            return False, f"hold cap must be 0–{OPS_BOUNDS['hold_s']} s (ops bound)"
    if tool == "request_staff" and a.get("kind") not in ("floater_in_store", "floater_borrow"):
        return False, "kind must be floater_in_store or floater_borrow"
    if tool == "brief_shift_lead":
        hi, en = str(a.get("hindi", "")), str(a.get("english", ""))
        if not hi or not en or len(hi) > 280 or len(en) > 320:
            return False, "briefing needs short Hindi and English text"
        if not any("ऀ" <= ch <= "ॿ" for ch in hi):
            return False, "the Hindi briefing must be in Devanagari"
    if tool == "extend_promise":
        return False, "the 10-minute promise is a hard limit and is never extended"
    return True, ""


def _relay(tool, a, ctx):
    if tool == "set_pacing":
        bc, ps = a.get("bench_cap"), a.get("pace_slack")
        if bc is not None and not 3 <= int(bc) <= 10:
            return False, "bench cap must be 3–10 bags"
        if ps is not None and not 45 <= int(ps) <= 180:
            return False, "pace slack must be 45–180 s (never below the 45 s buffer)"
    if tool == "prioritise":
        o = ctx.state.order(a.get("order_id"))
        if o is None or o["status"] not in ("pool", "picking"):
            return False, f"order #{a.get('order_id')} is not waiting to be picked"
    return True, ""


def _promise(tool, a, ctx):
    w = ctx.state.w
    oid = a.get("order_id")
    o = ctx.state.order(oid)
    if o is None:
        return False, f"unknown order #{oid}"
    if tool in ("apply_swap", "refund_item"):
        it = ctx.find_item(o, a.get("sku"))
        if it is None:
            return False, f"#{oid} has no unresolved {a.get('sku')}"
    if tool == "apply_swap":
        if not (o["saved"] or o["chose"]):
            return False, "this customer did not choose a swap; refund instead (no guessed swaps)"
        bad = w.skus.get(a.get("sku"))
        alt = w.skus.get(a.get("to_sku"))
        if alt is None or bad is None:
            return False, "unknown swap item"
        if alt["cat"] != bad["cat"] or alt["price"] < bad["price"]:
            return False, "swaps must be same category and same-or-higher value"
        if alt["hidden"] or alt["trust"] < 0.7 or alt["record"] <= 0:
            return False, f"{alt['name']} is not trusted to be on the shelf"
    if tool == "notify_customer" and len(str(a.get("message", ""))) > 220:
        return False, "customer message must be under 220 characters"
    return True, ""


CHECKS = {"ledger": _ledger, "captain": _captain, "relay": _relay, "promise": _promise}


def check(agent, tool, args, ctx):
    try:
        return CHECKS[agent](tool, args, ctx)
    except (TypeError, ValueError) as e:
        return False, f"bad arguments ({type(e).__name__})"
