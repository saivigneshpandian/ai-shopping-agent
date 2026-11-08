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
    """Validate against mandate, then create a PayPal order. Returns a dict for the agent/UI."""
    ok, reason, amount = check_mandate(session_id, items)
    db.log_decision(session_id, "create_order", amount, "ALLOWED" if ok else "BLOCKED", reason)
    if not ok:
        return {"ok": False, "blocked": True, "reason": reason, "amount": amount}

    client = get_client()
    try:
        order_id, approve_url = client.create_order(
            [{"name": i["name"], "price": i["price"], "quantity": i["quantity"], "sku": i["id"]} for i in items]
        )
    except PayPalError as e:
        db.log_decision(session_id, "create_order", amount, "BLOCKED", f"PayPal error: {e}")
        return {"ok": False, "blocked": False, "reason": str(e), "amount": amount}

    db.payment_create(
        session_id,
        order_id,
        [{"id": i["id"], "name": i["name"], "price": i["price"], "quantity": i["quantity"]} for i in items],
        amount,
        approve_url,
    )
    return {"ok": True, "paypal_order_id": order_id, "approve_url": approve_url, "amount": amount}


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
        # Mirror into the original orders table so the old order history still works.
        with db.connect() as c:
            for it in p["items"]:
                for _ in range(it["quantity"]):
                    c.execute(
                        "INSERT INTO orders (product_id, product_name, price) VALUES (?, ?, ?)",
                        (it["id"], it["name"], it["price"]),
                    )
        db.cart_clear(p["session_id"])
        return {"ok": True, "status": "COMPLETED", "payment": db.payment_get(paypal_order_id)}
    return {"ok": False, "reason": f"capture returned {status}", "payment": p}


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
