"""
Payment service + spending-mandate guardrail.

The LLM can *ask* to spend money, but every request passes through
check_mandate() first. Limits live in code and the DB, so a prompt
injection or a confused model cannot talk its way past them.
"""

import db
from paypal_client import PayPalError, get_client


def check_mandate(session_id, items):
    """items: [{id, name, category, price, quantity}] -> (allowed: bool, reason: str, amount: float)"""
    m = db.get_mandate(session_id)
    amount = round(sum(i["price"] * i["quantity"] for i in items), 2)
    spent = db.spent_so_far(session_id)
    remaining = round(m["total_budget"] - spent, 2)

    allowed_cats = [c.lower() for c in m["allowed_categories"]]
    if allowed_cats:
        bad = sorted({i["category"] for i in items if i["category"].lower() not in allowed_cats})
        if bad:
            return False, f"category not allowed by mandate: {', '.join(bad)} (allowed: {', '.join(allowed_cats)})", amount

    if amount > m["max_per_order"]:
        return False, f"order total ${amount:.2f} exceeds per-order limit ${m['max_per_order']:.2f}", amount

    if amount > remaining:
        return False, f"order total ${amount:.2f} exceeds remaining budget ${remaining:.2f} (spent ${spent:.2f} of ${m['total_budget']:.2f})", amount

    return True, f"within mandate (remaining after order: ${remaining - amount:.2f})", amount


def start_checkout(session_id, items):
    """Validate against mandate, then pay:
    - auto-pay from the saved PayPal wallet if one is connected and the total is under the auto-pay limit
    - otherwise create a PayPal order the buyer approves."""
    ok, reason, amount = check_mandate(session_id, items)
    if not ok:
        db.log_decision(session_id, "create_order", amount, "BLOCKED", reason)
        return {"ok": False, "blocked": True, "reason": reason, "amount": amount}

    client = get_client()
    pp_items = [{"name": i["name"], "price": i["price"], "quantity": i["quantity"], "sku": i["id"]} for i in items]
    stored_items = [{"id": i["id"], "name": i["name"], "price": i["price"], "quantity": i["quantity"]} for i in items]
    m = db.get_mandate(session_id)
    wallet = db.get_wallet(session_id)
    fallback_note = ""

    if wallet and amount <= m["autopay_limit"]:
        try:
            order_id, status, capture_id = client.charge_vault(wallet["vault_id"], pp_items)
            if status == "COMPLETED":
                db.payment_create(session_id, order_id, stored_items, amount, None,
                                  status="COMPLETED", capture_id=capture_id, auto_paid=True)
                _finalize(db.payment_get(order_id))
                db.log_decision(session_id, "auto_pay", amount, "ALLOWED",
                                f"{reason}; under auto-pay limit ${m['autopay_limit']:.2f}: charged saved PayPal wallet, no approval needed")
                return {"ok": True, "auto_paid": True, "status": "COMPLETED", "paypal_order_id": order_id, "amount": amount}
            fallback_note = f"auto-pay returned {status}"
        except PayPalError as e:
            fallback_note = f"auto-pay failed ({str(e)[:120]})"
    elif wallet:
        fallback_note = f"above auto-pay limit ${m['autopay_limit']:.2f}, needs your approval"

    try:
        order_id, approve_url = client.create_order(pp_items)
    except PayPalError as e:
        db.log_decision(session_id, "create_order", amount, "BLOCKED", f"PayPal error: {e}")
        return {"ok": False, "blocked": False, "reason": str(e), "amount": amount}

    db.payment_create(session_id, order_id, stored_items, amount, approve_url)
    db.log_decision(session_id, "create_order", amount, "ALLOWED", reason + (f"; {fallback_note}" if fallback_note else ""))
    return {"ok": True, "auto_paid": False, "needs_approval": True, "paypal_order_id": order_id,
            "approve_url": approve_url, "amount": amount, "note": fallback_note}


def _finalize(p):
    """After a successful capture: mirror into the legacy orders table and clear the cart."""
    with db.connect() as c:
        for it in p["items"]:
            for _ in range(it["quantity"]):
                c.execute(
                    "INSERT INTO orders (product_id, product_name, price) VALUES (?, ?, ?)",
                    (it["id"], it["name"], it["price"]),
                )
    db.cart_clear(p["session_id"])


def confirm_payment(paypal_order_id):
    """Capture an approved order. Idempotent: already-completed orders just return their state."""
    p = db.payment_get(paypal_order_id)
    if not p:
        return {"ok": False, "reason": "unknown order"}
    if p["status"] == "COMPLETED":
        return {"ok": True, "status": "COMPLETED", "payment": p}
    if p["status"] != "CREATED":
        return {"ok": False, "reason": f"order is {p['status']}", "payment": p}

    client = get_client()
    try:
        status, capture_id = client.capture_order(paypal_order_id)
    except PayPalError as e:
        msg = str(e)
        if "ORDER_NOT_APPROVED" in msg or "PAYER_ACTION_REQUIRED" in msg:
            return {"ok": False, "pending": True, "reason": "waiting for you to approve the payment on PayPal", "payment": p}
        return {"ok": False, "reason": msg, "payment": p}

    if status == "COMPLETED":
        db.payment_update(paypal_order_id, status="COMPLETED", capture_id=capture_id)
        _finalize(p)
        return {"ok": True, "status": "COMPLETED", "payment": db.payment_get(paypal_order_id)}
    return {"ok": False, "reason": f"capture returned {status}", "payment": p}


def seller_ask(session_id, local_payment_id, text):
    p = db.payment_get_by_id(session_id, local_payment_id)
    if not p or p["status"] != "REFUND_REQUESTED":
        return {"ok": False, "reason": "no pending refund request for that payment"}
    text = (text or "").strip()[:500]
    if not text:
        return {"ok": False, "reason": "empty message"}
    db.refund_msg_add(session_id, p["id"], "seller", text)
    return {"ok": True, "payment_id": p["id"]}


def buyer_reply(session_id, local_payment_id, text, image_path=None, upload_dir=None):
    import os
    p = db.payment_get_by_id(session_id, local_payment_id)
    if not p or p["status"] != "REFUND_REQUESTED":
        return {"ok": False, "reason": "there is no open refund request for that payment"}
    image = None
    if image_path:
        real = os.path.realpath(image_path)
        if upload_dir and real.startswith(os.path.realpath(upload_dir) + os.sep) and os.path.isfile(real) \
                and os.path.basename(real).startswith(session_id + "-"):
            image = os.path.basename(real)
    text = (text or "").strip()[:500] or ("(sent a photo)" if image else "")
    if not text:
        return {"ok": False, "reason": "empty reply"}
    db.refund_msg_add(session_id, p["id"], "buyer", text, image)
    return {"ok": True, "payment_id": p["id"], "note": "Reply delivered to the seller."}


# ---------------- wallet ----------------

def start_wallet_connect(session_id, app_url):
    return get_client().create_vault_setup(
        f"{app_url}/paypal/vault/return?sid={session_id}",
        f"{app_url}/paypal/vault/cancel?sid={session_id}",
    )


def finish_wallet_connect(session_id, setup_token_id):
    vault_id, email = get_client().create_payment_token(setup_token_id)
    db.set_wallet(session_id, vault_id, email)
    m = db.get_mandate(session_id)
    db.log_decision(session_id, "wallet_connect", None, "ALLOWED",
                    f"human connected PayPal wallet {email or ''} for auto-pay up to ${m['autopay_limit']:.2f}".replace("  ", " "))
    return email


def disconnect_wallet(session_id):
    w = db.get_wallet(session_id)
    if not w:
        return
    try:
        get_client().delete_payment_token(w["vault_id"])
    except PayPalError:
        pass  # still forget it locally
    db.remove_wallet(session_id)
    db.log_decision(session_id, "wallet_disconnect", None, "ALLOWED", "human disconnected the saved PayPal wallet")


def cancel_pending(paypal_order_id):
    p = db.payment_get(paypal_order_id)
    if p and p["status"] == "CREATED":
        db.payment_update(paypal_order_id, status="CANCELLED")
        db.log_decision(p["session_id"], "cancel_order", p["amount"], "ALLOWED", "buyer cancelled on PayPal")


def request_refund(session_id, local_payment_id, reason, evidence_path=None, upload_dir=None):
    """The buyer's agent can only ASK for a refund, with photo evidence. Money moves only after the seller approves."""
    import os
    if not evidence_path:
        return {"ok": False, "needs_photo": True,
                "reason": "a photo of the problem is required: ask the user to upload one with the camera button"}
    real = os.path.realpath(evidence_path)
    if upload_dir and (not real.startswith(os.path.realpath(upload_dir) + os.sep) or not os.path.isfile(real)
                       or not os.path.basename(real).startswith(session_id + "-")):
        return {"ok": False, "needs_photo": True, "reason": "that photo was not found, ask the user to upload it again"}
    p = db.payment_get_by_id(session_id, local_payment_id)
    if not p:
        return {"ok": False, "reason": f"no payment #{local_payment_id} in this session"}
    if p["status"] == "REFUND_REQUESTED":
        return {"ok": False, "reason": f"payment #{local_payment_id} already has a refund request waiting for the seller"}
    if p["status"] != "COMPLETED":
        return {"ok": False, "reason": f"payment #{local_payment_id} is {p['status']}, only completed payments can be refunded"}
    reason = (reason or "").strip()[:300] or "no reason given"
    db.payment_update(p["paypal_order_id"], status="REFUND_REQUESTED", refund_reason=reason, evidence_path=real)
    db.log_decision(session_id, "refund_request", p["amount"], "ALLOWED",
                    f"request with photo evidence sent to seller for review: {reason}")
    return {"ok": True, "status": "REFUND_REQUESTED", "payment_id": p["id"], "amount": p["amount"],
            "note": "The seller must review and approve this before any money is returned."}


def review_refund(session_id, local_payment_id, approve, note=""):
    """Seller decision. Only an approval calls the PayPal refund API."""
    p = db.payment_get_by_id(session_id, local_payment_id)
    if not p or p["status"] != "REFUND_REQUESTED":
        return {"ok": False, "reason": "no pending refund request for that payment"}
    note = (note or "").strip()[:200]
    if not approve:
        db.payment_update(p["paypal_order_id"], status="COMPLETED",
                          refund_reason=f"REJECTED by seller{': ' + note if note else ''} (claim: {p['refund_reason']})")
        db.log_decision(session_id, "refund_review", p["amount"], "BLOCKED",
                        f"seller rejected refund{': ' + note if note else ''}")
        return {"ok": True, "status": "REJECTED", "amount": p["amount"], "payment": p}
    try:
        status, refund_id = get_client().refund_capture(p["capture_id"], p["refund_reason"])
    except PayPalError as e:
        return {"ok": False, "reason": str(e)}
    db.payment_update(p["paypal_order_id"], status="REFUNDED", refund_id=refund_id)
    db.log_decision(session_id, "refund", p["amount"], "ALLOWED",
                    f"seller approved, refunded via PayPal: {p['refund_reason']}")
    return {"ok": True, "status": "REFUNDED", "refund_id": refund_id, "amount": p["amount"], "payment": p}


def seller_ask(session_id, local_payment_id, text):
    p = db.payment_get_by_id(session_id, local_payment_id)
    if not p or p["status"] != "REFUND_REQUESTED":
        return {"ok": False, "reason": "no pending refund request for that payment"}
    text = (text or "").strip()[:500]
    if not text:
        return {"ok": False, "reason": "empty message"}
    db.refund_msg_add(session_id, p["id"], "seller", text)
    return {"ok": True, "payment_id": p["id"]}


def buyer_reply(session_id, local_payment_id, text, image_path=None, upload_dir=None):
    import os
    p = db.payment_get_by_id(session_id, local_payment_id)
    if not p or p["status"] != "REFUND_REQUESTED":
        return {"ok": False, "reason": "there is no open refund request for that payment"}
    image = None
    if image_path:
        real = os.path.realpath(image_path)
        if upload_dir and real.startswith(os.path.realpath(upload_dir) + os.sep) and os.path.isfile(real) \
                and os.path.basename(real).startswith(session_id + "-"):
            image = os.path.basename(real)
    text = (text or "").strip()[:500] or ("(sent a photo)" if image else "")
    if not text:
        return {"ok": False, "reason": "empty reply"}
    db.refund_msg_add(session_id, p["id"], "buyer", text, image)
    return {"ok": True, "payment_id": p["id"], "note": "Reply delivered to the seller."}


# ---------------- wallet ----------------

def start_wallet_connect(session_id, app_url):
    return get_client().create_vault_setup(
        f"{app_url}/paypal/vault/return?sid={session_id}",
        f"{app_url}/paypal/vault/cancel?sid={session_id}",
    )


def finish_wallet_connect(session_id, setup_token_id):
    vault_id, email = get_client().create_payment_token(setup_token_id)
    db.set_wallet(session_id, vault_id, email)
    m = db.get_mandate(session_id)
    db.log_decision(session_id, "wallet_connect", None, "ALLOWED",
                    f"human connected PayPal wallet {email or ''} for auto-pay up to ${m['autopay_limit']:.2f}".replace("  ", " "))
    return email


def disconnect_wallet(session_id):
    w = db.get_wallet(session_id)
    if not w:
        return
    try:
        get_client().delete_payment_token(w["vault_id"])
    except PayPalError:
        pass  # still forget it locally
    db.remove_wallet(session_id)
    db.log_decision(session_id, "wallet_disconnect", None, "ALLOWED", "human disconnected the saved PayPal wallet")


def cancel_pending(paypal_order_id):
    p = db.payment_get(paypal_order_id)
    if p and p["status"] == "CREATED":
        db.payment_update(paypal_order_id, status="CANCELLED")
        db.log_decision(p["session_id"], "cancel_order", p["amount"], "ALLOWED", "buyer cancelled on PayPal")


def refund(session_id, local_payment_id, reason):
    p = db.payment_get_by_id(session_id, local_payment_id)
    if not p:
        return {"ok": False, "reason": f"no payment #{local_payment_id} in this session"}
    if p["status"] != "COMPLETED":
        return {"ok": False, "reason": f"payment #{local_payment_id} is {p['status']}, only completed payments can be refunded"}
    try:
        status, refund_id = get_client().refund_capture(p["capture_id"], reason)
    except PayPalError as e:
        return {"ok": False, "reason": str(e)}
    db.payment_update(p["paypal_order_id"], status="REFUNDED", refund_id=refund_id, refund_reason=reason)
    db.log_decision(session_id, "refund", p["amount"], "ALLOWED", f"refunded: {reason}")
    return {"ok": True, "refund_id": refund_id, "status": status, "amount": p["amount"]}
