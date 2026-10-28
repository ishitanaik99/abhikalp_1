"""A stand-in for the Groq API, for testing the platform without a key or internet.

Answers /openai/v1/chat/completions with plausible tool calls after a short delay.
    uvicorn tools.fake_groq:app --port 9900
    GROQ_API_KEY=fake GROQ_BASE_URL=http://localhost:9900/openai/v1 uvicorn server.app:app
"""
import asyncio
import json

from fastapi import FastAPI, Request

app = FastAPI()


def call(name, **args):
    args.setdefault("reason", "Fake model decision for testing.")
    return {"id": name, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@app.post("/openai/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    view = json.loads(body["messages"][-1]["content"])
    ev = view.get("event")
    calls = []
    if ev == "pick_not_found" and "item" in view:
        sid = view["item"]["sku"]
        calls = [call("set_trust", sku=sid, value=0.08), call("hide_item", sku=sid),
                 call("create_count_task", sku=sid, mode="empty", reason="Verify the shelf before the next order.")]
    elif ev == "mode_signal":
        calls = [call("set_mode", mode=view["suggested_mode"]),
                 call("brief_shift_lead", hindi="मोड बदल रहा है, सब तैयार रहें।", english="Mode is changing; everyone get ready.")]
    elif ev in ("sku_hidden",) and view.get("affected_lines"):
        line = view["affected_lines"][0]
        calls = [call("refund_item", order_id=line["order_id"], sku=line["sku"])]
    await asyncio.sleep(0.4)
    return {"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}
