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

load_dotenv(override=True)  # .env wins over old Windows env vars

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


def _run_agent(sid, text, display=None, image=None):
    import agent as agent_mod

    db.chat_add(sid, "user", display if display is not None else text, image)
    # Tell the agent about seller questions it hasn't seen yet, so it can relay the buyer's answer.
    unseen = db.take_unseen_seller_msgs(sid)
    if unseen:
        notes = "\n".join(f'[SELLER QUESTION about refund for payment #{m["payment_id"]}: "{m["text"]}"]' for m in unseen)
        text = f"{notes}\n\n{text}"
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


@app.get("/api/vault/connect")
def vault_connect(session_id: str):
    sid = _check_sid(session_id)
    try:
        _, approve = payments.start_wallet_connect(sid, get_client().app_url)
    except Exception as e:
        db.chat_add(sid, "assistant", f"⚠️ Couldn't start PayPal wallet connection: {e}")
        return RedirectResponse(f"/?sid={sid}", status_code=303)
    return RedirectResponse(approve, status_code=303)


@app.get("/paypal/vault/return")
def vault_return(sid: str, approval_token_id: str = "", token: str = ""):
    sid = _check_sid(sid)
    setup_id = approval_token_id or token
    try:
        email = payments.finish_wallet_connect(sid, setup_id)
        m = db.get_mandate(sid)
        db.chat_add(sid, "assistant",
                    f"🔗 PayPal wallet connected{(' (' + email + ')') if email else ''}. I can now pay **automatically** "
                    f"for orders up to **${m['autopay_limit']:.2f}**. Anything above that still needs your approval, "
                    f"and nothing can break your mandate.")
    except Exception as e:
        traceback.print_exc()
        db.chat_add(sid, "assistant", f"⚠️ Couldn't save your PayPal wallet: {e}")
    return RedirectResponse(f"/?sid={sid}&vault=1", status_code=303)


@app.get("/paypal/vault/cancel")
def vault_cancel(sid: str):
    sid = _check_sid(sid)
    db.chat_add(sid, "assistant", "✖️ PayPal wallet connection cancelled. Payments will keep asking for your approval.")
    return RedirectResponse(f"/?sid={sid}", status_code=303)


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
        "payments": [{**p, "thread": threads.get(p["id"], [])} for p in db.payments_for(sid)]
        if (threads := db.refund_msgs(sid)) is not None else [],
        "log": db.guardrail_log(sid, 25),
        "messages": db.chat_history(sid),
        "wallet": {"connected": bool(w := db.get_wallet(sid)), "email": (w or {}).get("payer_email")},
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
def upload(session_id: str = Form(...), file: UploadFile = File(...), message: str = Form("")):
    sid = _check_sid(session_id)
    ext = os.path.splitext(file.filename or "")[1].lower() or ".png"
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(400, "upload a jpg, png or webp image")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = f"{sid}-{uuid.uuid4().hex[:8]}{ext}"
    path = os.path.join(UPLOAD_DIR, name)
    with open(path, "wb") as f:
        f.write(file.file.read(8 * 1024 * 1024))
    message = (message or "").strip()[:1000]
    agent_text = (
        (message + "\n\n" if message else "")
        + f"I uploaded a photo. Its file path is: {path} (if we are talking about a refund, this is my evidence; "
        f"otherwise identify the product in it)"
    )
    return {"reply": _run_agent(sid, agent_text, message, image=name)}


@app.get("/api/uploads/{name}")
def uploaded_image(name: str, session_id: str):
    """Serve a photo the user attached, only to the same session."""
    sid = _check_sid(session_id)
    if "/" in name or "\\" in name or not name.startswith(sid + "-"):
        raise HTTPException(404, "not found")
    path = os.path.join(UPLOAD_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path)


@app.get("/api/evidence/{payment_id}")
def evidence(payment_id: int, session_id: str):
    """Photo evidence attached to a refund request (shown in the seller review panel)."""
    sid = _check_sid(session_id)
    p = db.payment_get_by_id(sid, payment_id)
    if not p or not p.get("evidence_path"):
        raise HTTPException(404, "no evidence")
    real = os.path.realpath(p["evidence_path"])
    if not real.startswith(os.path.realpath(UPLOAD_DIR) + os.sep) or not os.path.isfile(real):
        raise HTTPException(404, "no evidence")
    return FileResponse(real)


class MandateIn(BaseModel):
    session_id: str
    total_budget: float = Field(ge=0, le=100000)
    max_per_order: float = Field(ge=0, le=100000)
    autopay_limit: float = Field(default=0, ge=0, le=100000)
    allowed_categories: list[str] = []


@app.post("/api/mandate")
def set_mandate(body: MandateIn):
    sid = _check_sid(body.session_id)
    cats = [c for c in body.allowed_categories if c in db.list_categories()]
    db.set_mandate(sid, body.total_budget, body.max_per_order, cats, body.autopay_limit)
    db.log_decision(sid, "mandate_update", None, "ALLOWED",
                    f"human set budget ${body.total_budget:.2f}, per-order ${body.max_per_order:.2f}, "
                    f"auto-pay up to ${body.autopay_limit:.2f}, categories {', '.join(cats) or 'all'}")
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


class RefundReview(BaseModel):
    session_id: str
    approve: bool
    note: str = Field(default="", max_length=200)


@app.post("/api/payments/{payment_id}/refund-review")
def refund_review(payment_id: int, body: RefundReview):
    """Seller-side decision on a buyer's refund request (shown as the seller view in the demo)."""
    sid = _check_sid(body.session_id)
    r = payments.review_refund(sid, payment_id, body.approve, body.note)
    if not r["ok"]:
        raise HTTPException(400, r["reason"])
    msg = (f"↩️ The seller approved your refund for payment #{payment_id}: **${r['amount']:.2f}** returned via PayPal."
           if r["status"] == "REFUNDED" else
           f"✖️ The seller rejected the refund request for payment #{payment_id}."
           + (f" Reason: {body.note}" if body.note else ""))
    db.chat_add(sid, "assistant", msg)
    return {"ok": True, "message": msg}


class SellerMessage(BaseModel):
    session_id: str
    text: str = Field(min_length=1, max_length=500)


@app.post("/api/payments/{payment_id}/seller-message")
def seller_message(payment_id: int, body: SellerMessage):
    """Seller asks the buyer a question about a refund request. Saved, and shown in the buyer's chat."""
    sid = _check_sid(body.session_id)
    r = payments.seller_ask(sid, payment_id, body.text)
    if not r["ok"]:
        raise HTTPException(400, r["reason"])
    db.chat_add(sid, "assistant",
                f"🏪 **Seller asks about your refund for payment #{payment_id}:** {body.text.strip()}\n\n"
                f"Reply here (text or 📷 photo) and I'll pass it on.")
    return {"ok": True}


@app.post("/api/vault/disconnect")
def vault_disconnect(body: PaymentAction):
    sid = _check_sid(body.session_id)
    payments.disconnect_wallet(sid)
    db.chat_add(sid, "assistant", "🔌 PayPal wallet disconnected. Every payment will now ask for your approval.")
    return {"ok": True}


@app.post("/api/reset")
def reset(body: PaymentAction):
    """Fresh demo: a brand-new session (old data stays in the DB)."""
    _check_sid(body.session_id)
    return new_session()
