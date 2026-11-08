"""
AgentPay Guard: FastAPI backend + static web UI.

Run:  uvicorn server:app --reload     ->  http://localhost:8000
"""

import os
import re
import threading
import traceback
import uuid

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import db  # noqa: E402
import payments  # noqa: E402
from paypal_client import get_client  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(BASE_DIR, "_uploads")
SID_RE = re.compile(r"^s-[a-f0-9]{10}$")

db.init_db()
app = FastAPI(title="AgentPay Guard")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_agents = {}
_locks = {}
_agents_lock = threading.Lock()


def _check_sid(sid):
    if not SID_RE.match(sid or ""):
        raise HTTPException(400, "bad session id")
    return sid


def _agent_for(sid):
    import agent as agent_mod

    with _agents_lock:
        if sid not in _agents:
            _agents[sid] = agent_mod.build_agent(sid)
            _locks[sid] = threading.Lock()
        return _agents[sid], _locks[sid]


def _run_agent(sid, text, display=None):
    import agent as agent_mod

    db.chat_add(sid, "user", display or text)
    ag, lock = _agent_for(sid)
    answer = None
    with lock:  # one turn at a time per session
        for attempt in range(3):
            try:
                answer = agent_mod.ask(ag, sid, text)
                break
            except Exception as e:  # Groq occasionally emits malformed tool calls; retry
                traceback.print_exc()
                if "tool_use_failed" in str(e) and attempt < 2:
                    continue
                answer = f"Sorry, something went wrong talking to the model: {e}"
                break
    answer = (answer or "").replace("`", "").strip()
    db.chat_add(sid, "assistant", answer)
    return answer


# ---------------- pages ----------------

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/paypal/return")
def paypal_return(token: str):
    client = get_client()
    if client.is_mock:
        client.mock_approve(token)
    res = payments.confirm_payment(token)
    p = db.payment_get(token)
    if p:
        if res["ok"]:
            items = ", ".join(f"{i['quantity']} × {i['name']}" for i in p["items"])
            db.chat_add(p["session_id"], "assistant",
                        f"✅ Payment #{p['id']} complete: **${p['amount']:.2f}** charged via PayPal for {items}.")
        else:
            db.chat_add(p["session_id"], "assistant", f"⏳ Payment #{p['id']} not completed: {res.get('reason')}")
    # Carry the session id back so the chat resumes even if the browser lost localStorage
    sid = p["session_id"] if p else ""
    return RedirectResponse(f"/?paid={token}&sid={sid}", status_code=303)


@app.get("/paypal/cancel")
def paypal_cancel(token: str = ""):
    p = db.payment_get(token) if token else None
    if p:
        payments.cancel_pending(token)
        db.chat_add(p["session_id"], "assistant", f"✖️ Payment #{p['id']} cancelled on PayPal. Nothing was charged.")
        return RedirectResponse(f"/?sid={p['session_id']}", status_code=303)
    return RedirectResponse("/", status_code=303)


# ---------------- API ----------------

@app.get("/api/session")
def new_session():
    return {"session_id": "s-" + uuid.uuid4().hex[:10]}


@app.get("/api/state")
def state(session_id: str):
    sid = _check_sid(session_id)
    m = db.get_mandate(sid)
    spent = db.spent_so_far(sid)
    cart = db.cart_items(sid)
    return {
        "mode": "mock" if get_client().is_mock else get_client().mode,
        "mandate": m,
        "spent": spent,
        "remaining": round(m["total_budget"] - spent, 2),
        "categories": db.list_categories(),
        "cart": cart,
        "cart_total": round(sum(i["price"] * i["quantity"] for i in cart), 2),
        "payments": db.payments_for(sid),
        "log": db.guardrail_log(sid, 25),
        "messages": db.chat_history(sid),
    }


class ChatIn(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=2000)
    display: str | None = Field(default=None, max_length=300)  # friendlier text shown in the transcript


@app.post("/api/chat")
def chat(body: ChatIn):
    sid = _check_sid(body.session_id)
    return {"reply": _run_agent(sid, body.message, body.display)}


@app.post("/api/upload")
def upload(session_id: str = Form(...), file: UploadFile = File(...)):
    sid = _check_sid(session_id)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(400, "upload a jpg, png or webp image")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, f"{sid}-{uuid.uuid4().hex[:6]}{ext}")
    with open(path, "wb") as f:
        f.write(file.file.read(8 * 1024 * 1024))
    return {"reply": _run_agent(sid, f"I uploaded a product photo. Its file path is: {path}", "📷 I uploaded a product photo.")}


class MandateIn(BaseModel):
    session_id: str
    total_budget: float = Field(ge=0, le=100000)
    max_per_order: float = Field(ge=0, le=100000)
    allowed_categories: list[str] = []


@app.post("/api/mandate")
def set_mandate(body: MandateIn):
    sid = _check_sid(body.session_id)
    cats = [c for c in body.allowed_categories if c in db.list_categories()]
    db.set_mandate(sid, body.total_budget, body.max_per_order, cats)
    db.log_decision(sid, "mandate_update", None, "ALLOWED",
                    f"human set budget ${body.total_budget:.2f}, per-order ${body.max_per_order:.2f}, "
                    f"categories {', '.join(cats) or 'all'}")
    return {"ok": True}


class PaymentAction(BaseModel):
    session_id: str


@app.post("/api/payments/{order_id}/confirm")
def confirm(order_id: str, body: PaymentAction):
    sid = _check_sid(body.session_id)
    p = db.payment_get(order_id)
    if not p or p["session_id"] != sid:
        raise HTTPException(404, "payment not found")
    r = payments.confirm_payment(order_id)
    msg = (f"✅ Payment #{p['id']} complete: **${p['amount']:.2f}** charged via PayPal."
           if r["ok"] else f"⏳ Payment #{p['id']} not completed yet: {r.get('reason')}")
    db.chat_add(sid, "assistant", msg)
    return {"ok": r["ok"], "message": msg}


@app.post("/api/payments/{order_id}/cancel")
def cancel(order_id: str, body: PaymentAction):
    sid = _check_sid(body.session_id)
    p = db.payment_get(order_id)
    if not p or p["session_id"] != sid:
        raise HTTPException(404, "payment not found")
    payments.cancel_pending(order_id)
    db.chat_add(sid, "assistant", f"✖️ Payment #{p['id']} cancelled. Nothing was charged.")
    return {"ok": True}


@app.post("/api/reset")
def reset(body: PaymentAction):
    """Fresh demo: a brand-new session (old data stays in the DB)."""
    _check_sid(body.session_id)
    return new_session()
