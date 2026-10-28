"""Captain: shift-lead copilot.

Senses load every minute (plain code: utilisation now, 15-minute forecast,
backlog growth, hysteresis counter). When the numbers say the store should be
in a different mode, it raises a mode_signal and decides: which mode, which
limits (within ops bounds), whether to pull fast movers to the front racks,
whether to ask the shift lead for a floater picker, and what to say in the
Hindi voice briefing. Anything above its limits (staffing) needs approval.
"""
from core.llm import tool
from sim.constants import LIMITS, MODE_LABEL, MODE_UP, MODES, OPS_BOUNDS

from .base import Agent

MODE_HI = {"trough": "शांत", "normal": "सामान्य", "peak": "पीक", "surge": "सर्ज"}

PROMPT = """You are CAPTAIN, the shift-lead copilot. You own the store's operating mode and its limits,
the fast-mover front racks, staffing requests to the shift lead, and the Hindi voice briefing.

Modes: trough (discrete picking, 1 order per trolley, no hold), normal (cluster, up to 3, hold 30 s),
peak (cluster, up to 4, hold 75 s), surge (zone pick-and-merge, up to 4, hold 90 s).
Ops bounds you can never exceed: at most 4 orders per trolley, hold cap at most 90 s.
Thresholds on utilisation: below 0.50 trough, below 0.80 normal, else peak; 0.92 now or a backlog growing
3 minutes in a row means surge. Step down only one mode at a time and only after 3 calm minutes (the
input tells you when that is satisfied).

On mode_signal: decide with set_mode (and set_limits only if you want tighter limits than the mode
default). Going into peak or surge: move_fast_movers to the front, request_staff floater_in_store (and
floater_borrow for surge). Going down to normal or trough: move_fast_movers back. Always end with
brief_shift_lead: a short spoken briefing in Hindi (Devanagari script) plus its English meaning.
On shift_summary: call brief_shift_lead once with a 2-sentence summary using the numbers given.
Never extend the 10-minute promise."""

TOOLS = [
    tool("set_mode", "Switch the store's operating mode (also resets limits to that mode's defaults).",
         {"mode": {"type": "string", "enum": MODES}}, ["mode"]),
    tool("set_limits", "Tighten orders-per-trolley or the hold cap within ops bounds.",
         {"max_batch": {"type": "integer"}, "hold_s": {"type": "integer"}}, []),
    tool("move_fast_movers", "Move chips, cola and popcorn to the front racks (true) or back (false).",
         {"to_front": {"type": "boolean"}}, ["to_front"]),
    tool("request_staff", "Ask the shift lead to approve extra pickers.",
         {"kind": {"type": "string", "enum": ["floater_in_store", "floater_borrow"]}}, ["kind"]),
    tool("brief_shift_lead", "Speak a short briefing to the shift lead.",
         {"hindi": {"type": "string", "description": "Devanagari, under 160 characters"},
          "english": {"type": "string"}}, ["hindi", "english"]),
]

STAFF = {"floater_in_store": "Call the in-store floater picker (Kiran) for the peak",
         "floater_borrow": "Borrow a floater picker from the nearby store (Neel, about 4 minutes away)"}


class Captain(Agent):
    name = "captain"
    title = "Shift-lead copilot"
    tier = "smart"
    triggers = {"mode_signal", "shift_summary"}
    senses = {"minute"}
    prompt = PROMPT
    tools = TOOLS

    # ---------------------------------------------------------------- sense (code)
    def sense(self, ev, ctx):
        w = ctx.state.w
        b = ev["backlog"]
        grow = len(b) == 4 and b[3] > b[2] > b[1] > b[0] and b[3] >= 6
        v = max(ev["u_now"], ev["u_fc"])
        target = "trough" if v < 0.5 else "normal" if v < 0.8 else "peak"
        if ev["u_now"] >= 0.92 or grow:
            target = "surge"
        ri, ti = MODES.index(w.mode), MODES.index(target)
        why = None
        if ti > ri:
            w.down = 0
            why = "backlog grew 3 minutes in a row" if (target == "surge" and grow) else "load is above the threshold"
        elif ti < ri:
            thr = MODE_UP[w.mode]
            w.down = w.down + 1 if v <= thr - 0.05 else 0
            if w.down >= 3:
                target, why = MODES[ri - 1], "load stayed 5 points below the threshold for 3 minutes"
        else:
            w.down = 0
        if why and (w.t - getattr(self, "_last_sig", -999) > 170 or getattr(self, "_last_target", None) != target):
            self._last_sig, self._last_target = w.t, target
            w.emit("mode_signal", target=target, direction="up" if MODES.index(target) > ri else "down",
                   why=why, u_now=ev["u_now"], u_fc=ev["u_fc"], backlog=b, grow=grow)

    # ---------------------------------------------------------------- view
    def view(self, ev, ctx):
        w = ctx.state.w
        if ev["type"] == "shift_summary":
            return {"event": "shift_summary", "time": ctx.rt.clock(), "numbers": ev["numbers"]}
        return {"event": "mode_signal", "time": ctx.rt.clock(), "current_mode": w.mode, "current_limits": w.lim,
                "suggested_mode": ev["target"], "direction": ev["direction"], "why": ev["why"],
                "utilisation_now": round(ev["u_now"], 2), "forecast_15min": round(ev["u_fc"], 2),
                "backlog_last_4_min": ev["backlog"], "pickers_on_floor": len(w.active_pickers()),
                "calm_minutes_for_step_down": w.down, "ops_bounds": OPS_BOUNDS,
                "fast_movers_in_front": any(s["fwd"] for s in w.skus.values()),
                "staff_already_requested": [a["id"] for a in w.approvals]}

    def required(self, ev, ctx):
        if ev["type"] == "shift_summary":
            return {"brief_shift_lead"}
        return {"set_mode", "brief_shift_lead"} if ev["direction"] == "up" else set()

    # ---------------------------------------------------------------- rules (fallback)
    def rules(self, ev, ctx):
        w = ctx.state.w
        if ev["type"] == "shift_summary":
            n = ev["numbers"]
            return [("brief_shift_lead", {
                "english": f"Shift summary: {n['wrong_records_caught']} wrong stock records caught, "
                           f"{n['customer_chosen_swaps']} customer-chosen swaps applied, orders shared a trolley "
                           f"{n['shared_trolley_trips']} times, and about {n['walking_saved_m']} m of walking saved "
                           f"against the static plan.",
                "hindi": f"शिफ्ट सारांश: {n['wrong_records_caught']} गलत स्टॉक रिकॉर्ड पकड़े गए, "
                         f"{n['customer_chosen_swaps']} ग्राहकों की चुनी हुई अदला-बदली हुई, और लगभग "
                         f"{n['walking_saved_m']} मीटर पैदल चलना बचा।"})]
        m, prev = ev["target"], w.mode
        fc = f"utilisation {ev['u_now']:.2f} now, {ev['u_fc']:.2f} forecast in 15 min"
        plan = [("set_mode", {"mode": m})]
        if ev["direction"] == "up":
            if m in ("peak", "surge"):
                plan.append(("move_fast_movers", {"to_front": True}))
                plan.append(("request_staff", {"kind": "floater_in_store"}))
            if m == "surge":
                plan.append(("request_staff", {"kind": "floater_borrow"}))
            if m == "peak":
                plan.append(("brief_shift_lead", {
                    "english": f"Peak mode: {fc}. Batches up to 4, hold cap 75 s. Chips, cola and popcorn to the front racks.",
                    "hindi": "पीक मोड शुरू हो रहा है। फ्लोटर को तैयार रखिए, और चिप्स, कोला, पॉपकॉर्न आगे वाले रैक पर रखिए।"}))
            elif m == "surge":
                plan.append(("brief_shift_lead", {
                    "english": f"Surge mode: {ev['why']}. Zone pick-and-merge is on. The 10-minute promise is not extended.",
                    "hindi": "सर्ज मोड चालू है। ज़ोन पिकिंग शुरू। दस मिनट का वादा नहीं बदलेगा।"}))
            else:
                plan.append(("brief_shift_lead", {"english": f"{MODE_LABEL[m]} mode: {fc}.",
                                                  "hindi": f"{MODE_HI[m]} मोड।"}))
        else:
            if m in ("normal", "trough"):
                plan.append(("move_fast_movers", {"to_front": False}))
            plan.append(("brief_shift_lead", {
                "english": f"{MODE_LABEL[m]} mode: {ev['why']}, so the store steps down one mode (hysteresis).",
                "hindi": f"लोड कम हो रहा है। अब {MODE_HI[m]} मोड।"}))
        return plan

    # ---------------------------------------------------------------- execute
    def execute(self, name, a, ctx):
        st, w, ev = ctx.state, ctx.state.w, ctx.event
        if name == "set_mode":
            prev = w.mode
            if a["mode"] == prev:
                return None
            st.set_mode(self.name, a["mode"])
            up = MODES.index(a["mode"]) > MODES.index(prev)
            story = ("surge" if a["mode"] == "surge" else "peak" if a["mode"] == "peak" else None) if up else "winddown"
            lim = LIMITS[a["mode"]]
            return (f"{MODE_LABEL[prev]} → {MODE_LABEL[a['mode']]} mode ({ev['why']}). Up to {lim['max']} orders "
                    f"per trolley, hold cap {lim['hold']} s.", None, story)
        if name == "set_limits":
            st.set_limits(self.name, a.get("max_batch"), a.get("hold_s"))
            return f"Limits tightened: up to {w.lim['max']} per trolley, hold cap {w.lim['hold']} s."
        if name == "move_fast_movers":
            on = bool(a["to_front"])
            if any(s["fwd"] for s in w.skus.values()) == on:
                return None
            st.set_front_racks(self.name, on)
            return ("Asked for chips, cola and popcorn to move to the front racks." if on
                    else "Fast movers go back to their home slots.")
        if name == "request_staff":
            if st.request_approval(self.name, a["kind"], STAFF[a["kind"]]):
                return f"Above my limits, so I asked the shift lead: {STAFF[a['kind']]}."
            return None
        if name == "brief_shift_lead":
            st.set_brief(self.name, a["hindi"], a["english"])
            story = "summary" if ev["type"] == "shift_summary" else None
            return (a["english"], a["hindi"], story)
        raise ValueError(name)

    def describe(self, name, a, ctx):
        return {"set_mode": f"Switch to {a.get('mode')} mode.",
                "set_limits": f"Set limits {a.get('max_batch')} per trolley, hold {a.get('hold_s')} s.",
                "move_fast_movers": "Move fast movers to the front." if a.get("to_front") else "Move fast movers back.",
                "request_staff": f"Ask the shift lead: {STAFF.get(a.get('kind'), a.get('kind'))}.",
                "brief_shift_lead": f"Brief: {a.get('english')}"}.get(name, name)
