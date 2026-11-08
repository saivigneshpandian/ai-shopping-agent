"""
Purchase Authorization Service + payment state machine.

The AI agent can only PROPOSE a purchase (product IDs + quantities). This trusted
backend decides, and PayPal is only called after it says yes:

    proposal -> canonical re-pricing from the catalog -> policy engine (all rules)
             -> per-session lock (budget reserved atomically)
             -> PayPal order / vault charge -> verify captured amount + currency
             -> COMPLETED | AMOUNT_MISMATCH (flagged, not treated as paid)

Payment states:
    CREATED (awaiting buyer approval) -> COMPLETED | CAPTURE_PENDING | DENIED | CANCELLED
    CAPTURE_PENDING (PayPal still processing) -> COMPLETED | DENIED       (via signed webhook)
    COMPLETED / PARTIALLY_REFUNDED -> REFUND_REQUESTED -> back (rejected) | REFUND_PENDING
                                                       | PARTIALLY_REFUNDED | REFUNDED
    AMOUNT_MISMATCH  (PayPal reported a different amount/currency than authorized)

Every decision is written to the guardrail log with a full decision record.
"""

import os
import threading
import uuid

import db
from paypal_client import PayPalError, get_client

POLICY_VERSION = "mandate-v4"
ALLOWED_CURRENCY = "USD"
MAX_QTY = 20

_locks = {}
_locks_guard = threading.Lock()


def _session_lock(session_id):
    with _locks_guard:
        return _locks.setdefault(session_id, threading.Lock())


# ---------------------------------------------------------------- policy engine

def _canonical_items(items):
    """Re-price everything from the catalog. Prices/names the agent supplies are ignored."""
    out, claimed = [], 0.0
    for i in items:
        try:
            pid, qty = int(i["id"]), int(i.get("quantity", 1))
        except (KeyError, TypeError, ValueError):
            return None, claimed, "malformed item in purchase proposal"
        if qty < 1 or qty > MAX_QTY:
            return None, claimed, f"quantity {qty} outside allowed range 1-{MAX_QTY}"
        if "price" in i:
            claimed += float(i["price"]) * qty
        prods = db.get_products([pid])
        if not prods:
            return None, claimed, f"product {pid} is not in the catalog"
        p = prods[0]
        out.append({"id": p["id"], "name": p["name"], "category": p["category"], "price": p["price"], "quantity": qty})
    return out, round(claimed, 2), None


def evaluate(session_id, items, currency=ALLOWED_CURRENCY):
    """Run EVERY rule and report each one. -> (allowed, reason, amount, checks)"""
    m = db.get_mandate(session_id)
    amount = round(sum(i["price"] * i["quantity"] for i in items), 2)
    spent = db.spent_so_far(session_id)
    today = db.spent_today(session_id)
    remaining = round(m["total_budget"] - spent, 2)
    allowed_cats = [c.lower() for c in m["allowed_categories"]]
    bad_cats = sorted({i["category"] for i in items if allowed_cats and i["category"].lower() not in allowed_cats})
    blocked = [c.lower() for c in m.get("blocked_categories", [])]
    hit_blocked = sorted({i["category"] for i in items if i["category"].lower() in blocked})

    checks = [
        {"rule": "Currency", "limit": ALLOWED_CURRENCY, "value": currency, "pass": currency == ALLOWED_CURRENCY},
        {"rule": "Allowed categories", "limit": ", ".join(allowed_cats) or "all",
         "value": ", ".join(sorted({i["category"] for i in items})), "pass": not bad_cats},
        {"rule": "Blocked categories", "limit": ", ".join(blocked) or "none",
         "value": ", ".join(sorted({i["category"] for i in items})), "pass": not hit_blocked},
        {"rule": "Max per order", "limit": m["max_per_order"], "value": amount, "pass": amount <= m["max_per_order"]},
        {"rule": "Daily limit", "limit": m["daily_limit"], "value": round(today + amount, 2),
         "pass": today + amount <= m["daily_limit"] + 1e-9},
        {"rule": "Total budget", "limit": m["total_budget"], "value": round(spent + amount, 2),
         "pass": amount <= remaining + 1e-9},
    ]
    if currency != ALLOWED_CURRENCY:
        return False, f"currency {currency} not allowed: the mandate is in {ALLOWED_CURRENCY} only", amount, checks
    if hit_blocked:
        return False, f"category blocked by mandate: {', '.join(hit_blocked)}", amount, checks
    if bad_cats:
        return False, f"category not allowed by mandate: {', '.join(bad_cats)} (allowed: {', '.join(allowed_cats)})", amount, checks
    if amount > m["max_per_order"]:
        return False, f"order total ${amount:.2f} exceeds per-order limit ${m['max_per_order']:.2f}", amount, checks
    if today + amount > m["daily_limit"] + 1e-9:
        return False, (f"daily limit: ${today:.2f} already spent today + ${amount:.2f} would exceed "
                       f"${m['daily_limit']:.2f}"), amount, checks
    if amount > remaining + 1e-9:
        return False, (f"order total ${amount:.2f} exceeds remaining budget ${remaining:.2f} "
                       f"(spent ${spent:.2f} of ${m['total_budget']:.2f})"), amount, checks
    return True, f"all rules passed (remaining after order: ${remaining - amount:.2f})", amount, checks


def check_mandate(session_id, items):
    """Back-compat helper: -> (allowed, reason, amount)."""
    ok, reason, amount, _ = evaluate(session_id, items)
    return ok, reason, amount


def _record(txn, **kw):
    txn.update(kw)
    return txn


def _verify_capture(expected_amount, got_amount, got_currency):
    """PayPal's reported capture must match exactly what the policy engine authorized."""
    if got_currency != ALLOWED_CURRENCY:
        return False, f"PayPal captured in {got_currency or '?'} but {ALLOWED_CURRENCY} was authorized"
    if abs(float(got_amount) - float(expected_amount)) > 0.005:
        return False, f"PayPal reported ${float(got_amount):.2f} captured but only ${expected_amount:.2f} was authorized"
    return True, "captured amount and currency match the authorization"


# ---------------------------------------------------------------- checkout

def start_checkout(session_id, items, currency=ALLOWED_CURRENCY):
    """The one entry point for spending money. The agent proposes; this decides."""
    txn = {"txn_id": "T-" + uuid.uuid4().hex[:10].upper(), "policy_version": POLICY_VERSION,
           "currency": currency, "captured": False}

    with _session_lock(session_id):  # budget check + reservation are atomic per user session
        canon, claimed, err = _canonical_items(items)
        if err:
            db.log_decision(session_id, "create_order", None, "BLOCKED", err, _record(txn, checks=[]))
            return {"ok": False, "blocked": True, "reason": err}

        ok, reason, amount, checks = evaluate(session_id, canon, currency)
        _record(txn, requested_amount=claimed or amount, final_amount=amount, checks=checks,
                items=[f'{i["quantity"]} x {i["name"]}' for i in canon])
        if claimed and abs(claimed - amount) > 0.005:
            reason += f"; agent-claimed price ${claimed:.2f} ignored, catalog price ${amount:.2f} used"
        if not ok:
            db.log_decision(session_id, "create_order", amount, "BLOCKED", reason, txn)
            return {"ok": False, "blocked": True, "reason": reason, "amount": amount}

        client = get_client()
        pp_items = [{"name": i["name"], "price": i["price"], "quantity": i["quantity"], "sku": i["id"]} for i in canon]
        stored = [{"id": i["id"], "name": i["name"], "price": i["price"], "quantity": i["quantity"]} for i in canon]
        m = db.get_mandate(session_id)
        wallet = db.get_wallet(session_id)
        note = ""

        if wallet and amount <= m["autopay_limit"]:
            try:
                order_id, status, capture_id, got_amt, got_cur = client.charge_vault(wallet["vault_id"], pp_items)
                good, vmsg = _verify_capture(amount, got_amt, got_cur)
                _record(txn, paypal_order_id=order_id, capture_id=capture_id,
                        paypal_reported=f"{float(got_amt):.2f} {got_cur}")
                if status == "COMPLETED" and good:
                    db.payment_create(session_id, order_id, stored, amount, None,
                                      status="COMPLETED", capture_id=capture_id, auto_paid=True)
                    _finalize(db.payment_get(order_id))
                    db.log_decision(session_id, "auto_pay", amount, "ALLOWED",
                                    f"{reason}; under auto-pay limit ${m['autopay_limit']:.2f}: charged saved "
                                    f"PayPal wallet, {vmsg}", _record(txn, captured=True))
                    return {"ok": True, "auto_paid": True, "status": "COMPLETED", "paypal_order_id": order_id,
                            "amount": amount}
                if status == "PENDING":
                    db.payment_create(session_id, order_id, stored, amount, None,
                                      status="CAPTURE_PENDING", capture_id=capture_id, auto_paid=True)
                    db.log_decision(session_id, "auto_pay", amount, "ALLOWED",
                                    f"{reason}; PayPal capture is PENDING: budget reserved, not treated as paid "
                                    f"until a signed PayPal webhook confirms it", txn)
                    return {"ok": True, "auto_paid": True, "status": "CAPTURE_PENDING", "paypal_order_id": order_id,
                            "amount": amount, "note": "PayPal is still processing this payment"}
                if status == "COMPLETED" and not good:
                    db.payment_create(session_id, order_id, stored, amount, None,
                                      status="AMOUNT_MISMATCH", capture_id=capture_id, auto_paid=True)
                    db.log_decision(session_id, "verify_capture", amount, "BLOCKED",
                                    f"{vmsg}: NOT marked as paid, flagged for review", txn)
                    return {"ok": False, "blocked": True, "reason": vmsg + ", flagged for review", "amount": amount}
                note = f"auto-pay returned {status}"
            except PayPalError as e:
                note = f"auto-pay failed ({str(e)[:120]})"
        elif wallet:
            note = f"above auto-pay limit ${m['autopay_limit']:.2f}, needs your approval"

        try:
            order_id, approve_url = client.create_order(pp_items)
        except PayPalError as e:
            db.log_decision(session_id, "create_order", amount, "BLOCKED", f"PayPal error: {e}", txn)
            return {"ok": False, "blocked": False, "reason": str(e), "amount": amount}

        db.payment_create(session_id, order_id, stored, amount, approve_url)  # reserves budget (status CREATED)
        db.log_decision(session_id, "create_order", amount, "ALLOWED", reason + (f"; {note}" if note else ""),
                        _record(txn, paypal_order_id=order_id))
        return {"ok": True, "auto_paid": False, "needs_approval": True, "paypal_order_id": order_id,
                "approve_url": approve_url, "amount": amount, "note": note}


def _finalize(p):
    """After a verified capture: mirror into the legacy orders table and clear the cart."""
    with db.connect() as c:
        for it in p["items"]:
            for _ in range(it["quantity"]):
                c.execute(
                    "INSERT INTO orders (product_id, product_name, price) VALUES (?, ?, ?)",
                    (it["id"], it["name"], it["price"]),
                )
    db.cart_clear(p["session_id"])


def confirm_payment(paypal_order_id):
    """Capture a buyer-approved order and verify what PayPal actually captured. Idempotent."""
    p = db.payment_get(paypal_order_id)
    if not p:
        return {"ok": False, "reason": "unknown order"}
    with _session_lock(p["session_id"]):
        p = db.payment_get(paypal_order_id)
        if p["status"] == "COMPLETED":
            return {"ok": True, "status": "COMPLETED", "payment": p, "duplicate": True}
        if p["status"] != "CREATED":
            return {"ok": False, "reason": f"order is {p['status']}", "payment": p}
        try:
            status, capture_id, got_amt, got_cur = get_client().capture_order(paypal_order_id)
        except PayPalError as e:
            msg = str(e)
            if "ORDER_NOT_APPROVED" in msg or "PAYER_ACTION_REQUIRED" in msg:
                return {"ok": False, "pending": True, "reason": "waiting for you to approve the payment on PayPal",
                        "payment": p}
            return {"ok": False, "reason": msg, "payment": p}

        good, vmsg = _verify_capture(p["amount"], got_amt, got_cur)
        rec = {"txn_id": "T-" + uuid.uuid4().hex[:10].upper(), "policy_version": POLICY_VERSION,
               "paypal_order_id": paypal_order_id, "capture_id": capture_id, "final_amount": p["amount"],
               "paypal_reported": f"{float(got_amt):.2f} {got_cur}", "captured": status == "COMPLETED" and good}
        if status == "COMPLETED" and good:
            db.payment_update(paypal_order_id, status="COMPLETED", capture_id=capture_id)
            _finalize(p)
            db.log_decision(p["session_id"], "capture", p["amount"], "ALLOWED", f"buyer approved; {vmsg}", rec)
            return {"ok": True, "status": "COMPLETED", "payment": db.payment_get(paypal_order_id)}
        if status == "COMPLETED":
            db.payment_update(paypal_order_id, status="AMOUNT_MISMATCH", capture_id=capture_id)
            db.log_decision(p["session_id"], "verify_capture", p["amount"], "BLOCKED",
                            f"{vmsg}: NOT marked as paid, flagged for review", rec)
            return {"ok": False, "reason": vmsg + ", flagged for review", "payment": p}
        if status == "PENDING":
            db.payment_update(paypal_order_id, status="CAPTURE_PENDING", capture_id=capture_id)
            db.log_decision(p["session_id"], "capture", p["amount"], "ALLOWED",
                            "PayPal capture is PENDING: budget stays reserved; not treated as paid until a signed "
                            "PayPal webhook confirms it", rec)
            return {"ok": False, "pending": True, "reason": "PayPal is still processing this payment", "payment": p}
        if status in ("DECLINED", "DENIED", "FAILED"):
            db.payment_update(paypal_order_id, status="DENIED", capture_id=capture_id)
            db.log_decision(p["session_id"], "capture", p["amount"], "BLOCKED",
                            f"PayPal capture {status}: no money moved, reserved budget released", rec)
            return {"ok": False, "reason": f"PayPal {status.lower()} the payment", "payment": p}
        return {"ok": False, "reason": f"capture returned {status}", "payment": p}


def cancel_pending(paypal_order_id):
    p = db.payment_get(paypal_order_id)
    if p and p["status"] == "CREATED":
        db.payment_update(paypal_order_id, status="CANCELLED")
        db.log_decision(p["session_id"], "cancel_order", p["amount"], "ALLOWED",
                        "buyer cancelled on PayPal; reserved budget released")


# ---------------------------------------------------------------- refunds

def request_refund(session_id, local_payment_id, reason, evidence_path=None, upload_dir=None):
    """The buyer's agent can only ASK for a refund, with photo evidence. Money moves only after the seller approves."""
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
    if p["status"] not in ("COMPLETED", "PARTIALLY_REFUNDED"):
        return {"ok": False, "reason": f"payment #{local_payment_id} is {p['status']}, only completed payments can be refunded"}
    reason = (reason or "").strip()[:300] or "no reason given"
    refundable = round(p["amount"] - (p.get("refunded_amount") or 0), 2)
    db.payment_update(p["paypal_order_id"], status="REFUND_REQUESTED", refund_reason=reason, evidence_path=real,
                      refund_requested_amount=refundable)
    db.log_decision(session_id, "refund_request", p["amount"], "ALLOWED",
                    f"request with photo evidence sent to seller for review: {reason}",
                    {"policy_version": POLICY_VERSION, "paypal_order_id": p["paypal_order_id"],
                     "capture_id": p["capture_id"], "final_amount": p["amount"], "captured": True})
    return {"ok": True, "status": "REFUND_REQUESTED", "payment_id": p["id"], "amount": p["amount"],
            "note": "The seller must review and approve this before any money is returned."}


def _status_after_refunds(p, refunded):
    return "REFUNDED" if refunded >= p["amount"] - 0.005 else ("PARTIALLY_REFUNDED" if refunded > 0.005 else "COMPLETED")


def review_refund(session_id, local_payment_id, approve, note="", amount=None):
    """Seller decision. Only an approval calls PayPal, never for more than what is still refundable,
    and the budget is only restored once PayPal reports the refund COMPLETED."""
    p = db.payment_get_by_id(session_id, local_payment_id)
    if not p or p["status"] != "REFUND_REQUESTED":
        return {"ok": False, "reason": "no pending refund request for that payment"}
    note = (note or "").strip()[:200]
    already = round(p.get("refunded_amount") or 0, 2)
    refundable = round(p["amount"] - already, 2)
    rec = {"policy_version": POLICY_VERSION, "paypal_order_id": p["paypal_order_id"],
           "capture_id": p["capture_id"], "final_amount": p["amount"], "refundable": refundable}
    if not approve:
        db.payment_update(p["paypal_order_id"], status=_status_after_refunds(p, already),
                          refund_reason=f"REJECTED by seller{': ' + note if note else ''} (claim: {p['refund_reason']})")
        db.log_decision(session_id, "refund_review", p["amount"], "BLOCKED",
                        f"seller rejected refund{': ' + note if note else ''}", rec)
        return {"ok": True, "status": "REJECTED", "amount": p["amount"], "payment": p}

    amt = refundable if amount in (None, "") else round(float(amount), 2)
    if amt <= 0 or amt > refundable + 0.005:
        db.log_decision(session_id, "refund_review", amt, "BLOCKED",
                        f"refund of ${amt:.2f} rejected: only ${refundable:.2f} of the ${p['amount']:.2f} captured "
                        f"is still refundable", rec)
        return {"ok": False, "reason": f"refund amount must be between $0.01 and ${refundable:.2f}"}

    partial = amt < refundable - 0.005 or already > 0
    try:
        status, refund_id = get_client().refund_capture(
            p["capture_id"], p["refund_reason"], amount=amt if partial else None,
            attempt=int(round(already * 100)))
    except PayPalError as e:
        return {"ok": False, "reason": str(e)}
    rec.update(refund_id=refund_id, paypal_refund_status=status, refund_amount=amt)
    if status == "COMPLETED":
        new_total = round(already + amt, 2)
        new_status = _status_after_refunds(p, new_total)
        db.payment_update(p["paypal_order_id"], status=new_status, refund_id=refund_id, refunded_amount=new_total)
        db.log_decision(session_id, "refund", amt, "ALLOWED",
                        f"seller approved {'partial ' if new_status == 'PARTIALLY_REFUNDED' else ''}refund of "
                        f"${amt:.2f}; PayPal refund COMPLETED, budget restored by ${amt:.2f}: {p['refund_reason']}", rec)
        return {"ok": True, "status": new_status, "refund_id": refund_id, "amount": amt, "payment": p}
    db.payment_update(p["paypal_order_id"], status="REFUND_PENDING", refund_id=refund_id, refund_requested_amount=amt)
    db.log_decision(session_id, "refund", amt, "ALLOWED",
                    f"seller approved ${amt:.2f}; PayPal refund is {status}, budget stays reserved until a signed "
                    f"webhook confirms it", rec)
    return {"ok": True, "status": "REFUND_PENDING", "refund_id": refund_id, "amount": amt, "payment": p}


# ---------------------------------------------------------------- PayPal webhooks (reconciliation)

def handle_webhook(headers, event):
    """Only signature-verified PayPal events can change payment state. Replays are ignored."""
    etype, res = event.get("event_type", ""), event.get("resource", {}) or {}
    order_id = (res.get("supplementary_data", {}).get("related_ids", {}) or {}).get("order_id")
    up = next((l["href"].rstrip("/").split("/")[-1] for l in res.get("links", []) if l.get("rel") == "up"), None)
    p = db.payment_get(order_id) if order_id else (db.payment_get_by_capture(up) if up else None)
    if p is None and res.get("id"):
        p = db.payment_get_by_capture(res["id"])

    if not get_client().verify_webhook(headers, event):
        if p:
            db.log_decision(p["session_id"], "webhook", p["amount"], "BLOCKED",
                            f"forged/unsigned {etype or 'webhook'} rejected: signature verification failed, "
                            f"payment state unchanged ({p['status']})",
                            {"policy_version": POLICY_VERSION, "paypal_order_id": p["paypal_order_id"], "captured": False})
        return False, "signature verification failed"
    if db.webhook_seen(event.get("id", ""), etype):
        return True, "duplicate event ignored"
    if not p:
        return True, "no matching payment"

    rec = {"policy_version": POLICY_VERSION, "paypal_order_id": p["paypal_order_id"], "webhook_event": etype}
    with _session_lock(p["session_id"]):
        p = db.payment_get(p["paypal_order_id"])
        if etype == "PAYMENT.CAPTURE.COMPLETED" and p["status"] in ("CREATED", "CAPTURE_PENDING"):
            amt = res.get("amount", {})
            good, vmsg = _verify_capture(p["amount"], amt.get("value", 0), amt.get("currency_code", ""))
            db.payment_update(p["paypal_order_id"], status="COMPLETED" if good else "AMOUNT_MISMATCH",
                              capture_id=res.get("id"))
            if good:
                _finalize(p)
            db.log_decision(p["session_id"], "webhook", p["amount"], "ALLOWED" if good else "BLOCKED",
                            f"signed PayPal webhook: capture completed; {vmsg}", {**rec, "captured": good})
        elif etype == "PAYMENT.CAPTURE.PENDING" and p["status"] == "CREATED":
            db.payment_update(p["paypal_order_id"], status="CAPTURE_PENDING", capture_id=res.get("id"))
            db.log_decision(p["session_id"], "webhook", p["amount"], "ALLOWED",
                            "signed PayPal webhook: capture pending, budget stays reserved", rec)
        elif etype in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED") and p["status"] in ("CREATED", "CAPTURE_PENDING"):
            db.payment_update(p["paypal_order_id"], status="DENIED")
            db.log_decision(p["session_id"], "webhook", p["amount"], "BLOCKED",
                            "signed PayPal webhook: capture denied, no money moved, reserved budget released",
                            {**rec, "captured": False})
        elif etype == "PAYMENT.CAPTURE.REFUNDED" and p["status"] == "REFUND_PENDING":
            amt = float((res.get("amount") or {}).get("value", p.get("refund_requested_amount") or 0))
            new_total = round((p.get("refunded_amount") or 0) + amt, 2)
            db.payment_update(p["paypal_order_id"], status=_status_after_refunds(p, new_total), refunded_amount=new_total)
            db.log_decision(p["session_id"], "webhook", amt, "ALLOWED",
                            f"signed PayPal webhook: refund of ${amt:.2f} completed, budget restored", rec)
    return True, "processed"


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


# ---------------------------------------------------------------- wallet (PayPal Vault)

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
