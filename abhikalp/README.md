# SyncStore: four AI agents running a dark store, in sync

ABHIKALP 2026 · Case 4 (Quick commerce) · Team Problem Breakers, IIM Mumbai

SyncStore is a working multi-agent platform. Four independent agents (Ledger, Captain, Relay and Promise) run on a Python server. They decide with a Groq-hosted LLM and coordinate through one shared store state. A simulated dark store (MUM-114) runs twice on the same orders: once with SyncStore, and once with today's static plan. The control room shows both side by side.

## How it fits together

```
 Browser (web/)  ◄── snapshots 4×/s ──  FastAPI server (server/)  ── user actions ──►
                                              │
                         Session (core/runtime.py): one store per browser tab
                                              │
   Simulator (sim/world.py) ── emits events ──► Event bus (core/bus.py)
        ▲                                         │ routes each event, in order
        │                         ┌───────────┬───┴──────┬────────────┐
        │                       Ledger     Captain     Relay       Promise     (agents/)
        │                         │ LLM (Groq) or rules → guardrails → execute
        │                         ▼
        └──── Shared store state (core/state.py): one writer per field ◄── Fast optimiser
                                                                          (engine/, plain code)
```

| Agent | Wakes on | Owns (only it can write) | Tools | Model |
|---|---|---|---|---|
| Ledger | `pick_not_found`, `trust_drift`, `count_result`, `refill_done` | trust, visibility, record, count/refill tasks | set_trust, hide_item, lower_aisle_trust, create_count_task, correct_record, trigger_refill, restore_item, raise_reorder | fast |
| Captain | `mode_signal`, `shift_summary` | mode and limits, front racks, approvals, Hindi brief | set_mode, set_limits, move_fast_movers, request_staff, brief_shift_lead | smart |
| Relay | `rider_late`, `bench_congested`, `bench_cleared` | release pacing, priority boosts | prioritise, set_pacing, announce | fast |
| Promise | `pick_not_found`, `sku_hidden` | order lines, customer messages | apply_swap, refund_item, notify_customer | smart |

**How one agent works** (`agents/base.py`):

1. `sense()` is plain code that watches the store every simulated minute and raises events. For example, Ledger raises `trust_drift`, and Captain raises `mode_signal` when a threshold is crossed.
2. On a trigger event, the agent builds a compact JSON `view()` and sends it, with its own prompt and tools, to Groq. The model replies with tool calls only.
3. Every call goes through `core/guardrails.py`. Hard limits live there: hold cap ≤ 90 s, ≤ 4 orders per trolley, no guessed swaps, no restoring an item without a count, Hindi in Devanagari, and the 10-minute promise is never extended. A blocked call is logged with the reason.
4. Calls that pass are executed through `core/state.py`. Each field has one owner, and a write by any other agent raises `OwnershipError`.
5. If the model skipped a step that must happen (for example, hiding an empty item), the agent's `rules()` fill in exactly that step.
6. If the LLM is off, over budget, slow (more than 8 s) or erroring, the agent uses its `rules()` for the whole decision. The store never stops.

**How they stay in sync** (`core/bus.py`): agents never call each other. A state write emits an event, and the bus routes it to the next agent before the next simulated second. For example: `pick_not_found` → Ledger hides milk → the write emits `sku_hidden` → Promise resolves every order holding milk. The fast optimiser reads what the agents set (Captain's limits, Relay's pacing, Ledger's trust) and never calls the LLM.

## Run it on your laptop

```bash
pip install -r requirements.txt
cp .env.example .env          # then paste your Groq key after GROQ_API_KEY=
uvicorn server.app:app --reload
# open http://localhost:8000
```

If there is no key, everything still runs: the agents use their rules, and the header shows "Rules".

Tests: `python -m pytest -q`. There are 16 tests: each agent alone, its guardrails, field ownership, Shadow mode, LLM outage fallback, a mocked Groq HTTP path, and a full shift with all four agents.

To test the LLM path without a key or internet, use the included fake Groq:

```bash
uvicorn tools.fake_groq:app --port 9900 &
GROQ_API_KEY=fake GROQ_BASE_URL=http://localhost:9900/openai/v1 uvicorn server.app:app
```

## Deploy a link for the judges (Render, free)

1. Push this folder to a GitHub repo. `.env` is git-ignored; never commit your key.
2. On render.com: **New +** → **Blueprint** → pick the repo. `render.yaml` sets everything up.
3. When asked for `GROQ_API_KEY`, paste your key. You can also add it later under **Environment**.
4. Open the `https://syncstore-….onrender.com` URL and point your QR code at it.

Notes:

- The free plan sleeps after about 15 minutes idle. The first visit takes about 30–60 s to wake, so open the link a minute before you present.
- Each browser tab gets its own store. `MAX_SESSIONS` caps how many run at once.
- All tabs share one Groq budget (`LLM_RPM`, `LLM_RPD`). Check Groq's current free-tier limits and model list in the Groq console, and change `GROQ_MODEL_SMART` and `GROQ_MODEL_FAST` if a model is retired. If calls are refused, the agents fall back to rules and the Coordination page shows why.
- Other hosts (Railway, Fly.io, a VM) work with the `Dockerfile`. It needs a host that supports WebSockets.

## Changing an agent

Each agent is one file in `agents/`. The parts to edit:

- `PROMPT`: what the agent is responsible for and what it should do per event.
- `TOOLS`: what it may do. Add the matching branch in `execute()` and, if needed, a check in `core/guardrails.py`.
- `rules()`: the fallback behaviour, also used to fill required steps.
- `required()`: steps that must happen even if the model forgets them.
- To make it wake on a new event, emit the event (from the world or a state write) and add it to `ROUTES` in `core/bus.py`.

Write a test in `tests/test_agents.py` with the `Harness` and `FakeLLM` helpers before wiring anything into the UI.

## Folder map

```
sim/        constants.py (store layout, catalogue), world.py (simulator + static plan)
engine/     optimiser.py (batching, routing, assignment; plain code)
core/       state.py (shared state + ownership), bus.py (event routing), guardrails.py,
            llm.py (Groq client, budgets, fallback), runtime.py (one session)
agents/     base.py, ledger.py, captain.py, relay.py, promise.py
server/     app.py (FastAPI + WebSocket), snapshot.py (state → JSON for the UI)
web/        index.html + app.js (control room)
tools/      fake_groq.py (offline stand-in for Groq)
tests/      test_agents.py
```
