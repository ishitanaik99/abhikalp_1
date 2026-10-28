"""SyncStore platform server.

  GET  /          the control room (one page)
  GET  /healthz   health check for the host
  GET  /api/llm   LLM status (configured, models, budget used) - never the key
  WS   /ws        one simulated store session per browser tab

Run locally:   GROQ_API_KEY=... uvicorn server.app:app --reload
"""
import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

def _load_dotenv(path=Path(__file__).resolve().parent.parent / ".env"):
    """Read KEY=value lines from .env for local runs (hosts set real env vars instead)."""
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

from core.llm import GroqLLM  # noqa: E402  (after .env is loaded)
from core.runtime import Session  # noqa: E402
from sim.constants import HORIZON  # noqa: E402

from . import snapshot  # noqa: E402

WEB = Path(__file__).resolve().parent.parent / "web"
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", 6))

app = FastAPI(title="SyncStore")
app.mount("/static", StaticFiles(directory=WEB), name="static")
LLM = GroqLLM()                         # shared by every session (one budget, one key)
ACTIVE = set()


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "sessions": len(ACTIVE)}


@app.get("/api/llm")
async def llm_status():
    return JSONResponse(LLM.health())


@app.on_event("shutdown")
async def _close():
    await LLM.close()


async def _sim_loop(sess):
    loop = asyncio.get_running_loop()
    last, acc = loop.time(), 0.0
    while True:
        await asyncio.sleep(0.05)
        now = loop.time()
        dt, last = min(0.25, now - last), now
        if not sess.playing:
            acc = 0.0
            continue
        acc += dt * sess.speed
        n, acc = int(acc), acc - int(acc)
        for _ in range(n):
            async with sess.lock:
                await sess.tick()
            if not sess.playing:
                break


async def _sender(ws, sess):
    while True:
        await ws.send_text(json.dumps(snapshot.build(sess), ensure_ascii=False, separators=(",", ":")))
        await asyncio.sleep(0.25)


async def _handle(ws, sess, msg):
    c = msg.get("cmd")
    toast = None
    if c == "play":
        if not sess.playing and sess.smart.t >= HORIZON - 5:
            async with sess.lock:
                await sess.reset(840)
        sess.playing = not sess.playing
    elif c == "speed":
        sess.speed = max(1, min(60, int(msg.get("value", 10))))
    elif c == "reset":
        async with sess.lock:
            await sess.reset(840)
        sess.playing = True
        toast = "Reset to 18:44."
    elif c == "demo":
        async with sess.lock:
            await sess.reset(480, demo=True)
        sess.speed, sess.playing = 20, True
        toast = "Demo started at 18:38. The steps light up at the top of the Overview."
    elif c == "layer" and msg.get("value") in ("live", "shadow", "off"):
        sess.apply_layer(msg["value"])
    elif c == "llm":
        want = bool(msg.get("value"))
        if want and not LLM.configured:
            toast = "No GROQ_API_KEY is set on the server, so the agents use their rules."
        sess.llm_on = want and LLM.configured
    elif c == "empty" and msg.get("sku") in sess.smart.skus:
        name = sess.empty_shelf(msg["sku"])
        toast = f"{name}: shelf emptied. The record still shows stock, so watch Ledger."
    elif c == "late":
        oid = sess.rider_late(int(msg["order_id"]) if msg.get("order_id") else None)
        toast = f"Rider for #{oid} is now 2 minutes late." if oid else "No order is waiting for a rider right now."
    elif c == "spike":
        sess.spike()
        toast = "14 extra orders are coming in over 6 minutes."
    elif c == "approve":
        sess.approve(msg.get("id"), "from the dashboard")
    elif c == "decline":
        sess.decline(msg.get("id"))
    elif c == "auto_approve":
        sess.auto_approve = bool(msg.get("value"))
    elif c == "place":
        oid = sess.place_order(list(msg.get("cart") or []), dict(msg.get("choices") or {}))
        toast = f"Order #{oid} placed. Follow it in Relay." if oid else None
    if toast:
        await ws.send_text(json.dumps({"type": "toast", "text": toast}))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    if len(ACTIVE) >= MAX_SESSIONS:
        await ws.send_text(json.dumps({"type": "full", "text": "The demo server is full. Please try again in a few minutes."}))
        await ws.close()
        return
    sess = Session(LLM)
    sess.lock = asyncio.Lock()
    sess.started = time.time()
    await sess.reset(840)
    ACTIVE.add(sess)
    tasks = [asyncio.create_task(_sim_loop(sess)), asyncio.create_task(_sender(ws, sess))]
    try:
        while True:
            raw = await ws.receive_text()
            try:
                await _handle(ws, sess, json.loads(raw))
            except (ValueError, TypeError, KeyError):
                pass
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        ACTIVE.discard(sess)
