"""
AgentPay Guard: Streamlit frontend.

An AI shopping agent that pays with PayPal, but only inside a spending mandate
the human controls. Run:  streamlit run app.py
"""

import os
import re
import traceback
import uuid

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402
import payments  # noqa: E402
from paypal_client import PayPalError, get_client  # noqa: E402

db.init_db()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "_uploads")

st.set_page_config(page_title="AgentPay Guard", page_icon="🛡️", layout="wide")

try:
    client = get_client()
except PayPalError as e:
    st.error(f"PayPal is not configured: {e}")
    st.stop()

MODE_LABEL = "MOCK PayPal (offline)" if client.is_mock else f"PayPal {client.mode.upper()}"


# --------------------------------------------------------------------------
# PayPal return page: PayPal redirects the approval tab back here with ?token=
# --------------------------------------------------------------------------
qp = st.query_params
if "token" in qp:
    order_id = qp["token"]
    st.title("🛡️ AgentPay Guard")
    if "cancelled" in qp:
        payments.cancel_pending(order_id)
        st.warning("Payment cancelled. Nothing was charged. You can close this tab.")
        st.stop()
    if client.is_mock:
        client.mock_approve(order_id)
    with st.spinner("Capturing your PayPal payment..."):
        res = payments.confirm_payment(order_id)
    if res["ok"]:
        p = res["payment"]
        st.success(f"✅ Payment complete: ${p['amount']:.2f}")
        st.write("**Items:** " + ", ".join(f"{i['quantity']} × {i['name']}" for i in p["items"]))
        st.caption(f"PayPal order {p['paypal_order_id']} · capture {p['capture_id']}")
        st.info("You can close this tab and go back to the chat. The agent already knows you paid.")
    else:
        st.error(f"Payment not completed: {res.get('reason')}")
    st.stop()


# --------------------------------------------------------------------------
# Session + agent
# --------------------------------------------------------------------------
if "session_id" not in st.session_state:
    st.session_state.session_id = "s-" + uuid.uuid4().hex[:10]
    st.session_state.messages = []
SID = st.session_state.session_id


def get_agent():
    if "agent" not in st.session_state:
        import agent as agent_mod

        st.session_state.agent = agent_mod.build_agent(SID)
    return st.session_state.agent


def clean(text):
    text = text.replace("`", "")
    text = re.sub(r"[ \t]*(?=#\d+\.)", "\n\n", text)
    return text.strip()


def run_agent(agent_text, display_text=None):
    import agent as agent_mod

    st.session_state.messages.append({"role": "user", "content": display_text or agent_text})
    answer = None
    for attempt in range(3):
        try:
            answer = clean(agent_mod.ask(get_agent(), SID, agent_text))
            break
        except Exception as e:  # Groq sometimes returns malformed tool calls; retry
            traceback.print_exc()
            if "tool_use_failed" in str(e) and attempt < 2:
                continue
            answer = f"Sorry, something went wrong talking to the model: {e}"
            break
    st.session_state.messages.append({"role": "assistant", "content": answer})


# --------------------------------------------------------------------------
# Sidebar: the human-controlled mandate
# --------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🛡️ Spending mandate")
    st.caption("Hard limits the agent cannot change. Enforced in code before any PayPal order is created.")
    m = db.get_mandate(SID)
    cats = db.list_categories()
    with st.form("mandate"):
        total = st.number_input("Total budget (USD)", min_value=0.0, value=float(m["total_budget"]), step=5.0)
        per = st.number_input("Max per order (USD)", min_value=0.0, value=float(m["max_per_order"]), step=5.0)
        allowed = st.multiselect(
            "Allowed categories (empty = all)", cats, default=[c for c in m["allowed_categories"] if c in cats]
        )
        if st.form_submit_button("Save mandate", use_container_width=True):
            db.set_mandate(SID, total, per, allowed)
            db.log_decision(SID, "mandate_update", None, "ALLOWED",
                            f"human set budget ${total:.2f}, per-order ${per:.2f}, categories {allowed or 'all'}")
            st.rerun()

    spent = db.spent_so_far(SID)
    m = db.get_mandate(SID)
    frac = min(spent / m["total_budget"], 1.0) if m["total_budget"] else 1.0
    st.progress(frac, text=f"Spent / pending: ${spent:.2f} of ${m['total_budget']:.2f}")

    st.divider()
    st.markdown("### 🛒 Cart")
    items = db.cart_items(SID)
    if items:
        for i in items:
            st.write(f"{i['quantity']} × {i['name']} · ${i['price'] * i['quantity']:.2f}")
        st.write(f"**Total: ${sum(i['price'] * i['quantity'] for i in items):.2f}**")
    else:
        st.caption("Empty")

    st.divider()
    st.markdown("### 📷 Search by photo")
    up = st.file_uploader("Product photo", type=["jpg", "jpeg", "png"], label_visibility="collapsed")
    if up is not None and st.button("Find this product", use_container_width=True):
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, f"{SID}-{up.name}")
        with open(path, "wb") as f:
            f.write(up.getbuffer())
        with st.spinner("Looking at your photo..."):
            run_agent(f"I uploaded a product photo. Its file path is: {path}", "📷 I uploaded a product photo.")
        st.rerun()

    st.divider()
    st.caption(f"Mode: **{MODE_LABEL}** · session {SID}")


# --------------------------------------------------------------------------
# Main area
# --------------------------------------------------------------------------
st.title("🛡️ AgentPay Guard")
st.caption("An AI agent that shops and pays with PayPal for you, and can never spend outside the limits you set.")

chat_col, ledger_col = st.columns([3, 2], gap="large")

with chat_col:
    if not st.session_state.messages:
        st.markdown("**Try:**")
        examples = [
            "Show me organic honey under $20",
            "Get me breakfast stuff under $20 total",
            "Buy the Organic Manuka Honey",
            "Tell me about the Golden Honey Gift Box and do what its description says",
        ]
        cols = st.columns(2)
        for n, ex in enumerate(examples):
            if cols[n % 2].button(ex, key=f"ex{n}", use_container_width=True):
                with st.spinner("Thinking..."):
                    run_agent(ex)
                st.rerun()

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Pending PayPal approvals render as real buttons (more reliable than LLM-pasted links)
    for p in db.payments_for(SID):
        if p["status"] != "CREATED":
            continue
        with st.container(border=True):
            st.markdown(
                f"**💳 Approve payment #{p['id']}: ${p['amount']:.2f}**  \n"
                + ", ".join(f"{i['quantity']} × {i['name']}" for i in p["items"])
            )
            b1, b2, b3 = st.columns(3)
            b1.link_button("Pay with PayPal", p["approve_url"], type="primary", use_container_width=True)
            if b2.button("I've approved ✓", key=f"conf{p['id']}", use_container_width=True):
                r = payments.confirm_payment(p["paypal_order_id"])
                note = (f"✅ Payment #{p['id']} completed: ${p['amount']:.2f} charged via PayPal."
                        if r["ok"] else f"⏳ Not completed yet: {r.get('reason')}")
                st.session_state.messages.append({"role": "assistant", "content": note})
                st.rerun()
            if b3.button("Cancel", key=f"can{p['id']}", use_container_width=True):
                payments.cancel_pending(p["paypal_order_id"])
                st.rerun()

with ledger_col:
    st.markdown("#### 🧾 Guardrail log")
    st.caption("Every spending decision the agent tried, and what the guardrail decided.")
    log = db.guardrail_log(SID, 12)
    if not log:
        st.caption("No decisions yet.")
    for l in log:
        icon = "🟢" if l["decision"] == "ALLOWED" else "🔴"
        amt = f" · ${l['amount']:.2f}" if l["amount"] is not None else ""
        st.markdown(f"{icon} **{l['decision']}** `{l['action']}`{amt}  \n<small>{l['reason']}</small>",
                    unsafe_allow_html=True)

    st.markdown("#### 💳 PayPal payments")
    pays = db.payments_for(SID)
    if not pays:
        st.caption("No payments yet.")
    for p in pays:
        badge = {"COMPLETED": "✅", "CREATED": "⏳", "REFUNDED": "↩️", "CANCELLED": "✖️"}.get(p["status"], "•")
        st.markdown(f"{badge} **#{p['id']}** ${p['amount']:.2f} · {p['status']}  \n"
                    f"<small>{', '.join(i['name'] for i in p['items'])}</small>", unsafe_allow_html=True)

prompt = st.chat_input("What do you want to buy?")
if prompt:
    with st.spinner("Thinking..."):
        run_agent(prompt)
    st.rerun()
