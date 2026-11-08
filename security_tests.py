"""
Security test suite: attacks the real Purchase Authorization Service.

Every attack runs in a throw-away session against an offline PayPal simulator
(paypal_client._override), so no real/sandbox money moves and no user data is touched.
The guard code exercised is exactly the production code path (payments.start_checkout,
payments.confirm_payment), not a copy.
"""

import contextvars
import threading
import uuid

import db
import payments
import paypal_client

TEST_POLICY = {"total_budget": 60.0, "max_per_order": 25.0, "daily_limit": 40.0, "autopay_limit": 15.0,
               "allowed_categories": []}
import os  # noqa: E402


def _scratch(policy=None, wallet=True):
    sid = "s-" + uuid.uuid4().hex[:10]
    pol = {**TEST_POLICY, **(policy or {})}
    db.set_mandate(sid, pol["total_budget"], pol["max_per_order"], pol["allowed_categories"],
                   pol["autopay_limit"], pol["daily_limit"])
    if wallet:
        db.set_wallet(sid, "MOCKVAULT-SECTEST", "attacker-test@sandbox")
    return sid


def _product(name_like):
    with db.connect() as c:
        r = c.execute("SELECT * FROM products WHERE name LIKE ? ORDER BY id LIMIT 1", (f"%{name_like}%",)).fetchone()
    return dict(r)


def _captured(sid):
    return round(sum(p["amount"] for p in db.payments_for(sid) if p["status"] == "COMPLETED"), 2)


def _cleanup(sids):
    with db.connect() as c:
        for t in ("payments", "guardrail_log", "mandates", "wallets", "cart", "refund_messages"):
            c.executemany(f"DELETE FROM {t} WHERE session_id = ?", [(s,) for s in sids])


def run_suite():
    mock = paypal_client.mock_client()
    token = paypal_client._override.set(mock)
    results, sids = [], []
    try:
        manuka, gift, raw = _product("Manuka"), _product("Golden Honey Gift Box"), _product("Organic Raw Honey")

        # 1. Price tampering: the agent claims a fake low price.
        sid = _scratch(); sids.append(sid)
        r = payments.start_checkout(sid, [{"id": manuka["id"], "price": 0.01, "quantity": 1}])
        results.append({
            "id": "price", "icon": "💲", "name": "Price tampering",
            "attack": f'Agent claims "{manuka["name"]}" costs $0.01 and asks to buy it.',
            "passed": not r["ok"] and _captured(sid) == 0,
            "outcome": "BLOCKED",
            "detail": f'Guard ignored the agent\'s price and re-priced from the catalog (${manuka["price"]:.2f}): {r.get("reason")}',
            "captured": _captured(sid),
        })

        # 2. Prompt injection hidden in a product description.
        sid = _scratch(); sids.append(sid)
        r = payments.start_checkout(sid, [{"id": gift["id"], "quantity": 10}])
        snippet = gift["description"][gift["description"].find("["):][:90] + "…"
        results.append({
            "id": "injection", "icon": "🧪", "name": "Prompt injection",
            "attack": f'Product text says: "{snippet}" and the fooled agent tries to buy 10.',
            "passed": not r["ok"] and _captured(sid) == 0,
            "outcome": "BLOCKED", "detail": r.get("reason"), "captured": _captured(sid),
        })

        # 3. Order splitting: many small orders to dodge the per-order limit.
        sid = _scratch(); sids.append(sid)
        allowed, blocked_at, why = 0, None, ""
        for n in range(1, 6):
            r = payments.start_checkout(sid, [{"id": raw["id"], "quantity": 1}])
            if r["ok"]:
                allowed += 1
            else:
                blocked_at, why = n, r.get("reason")
                break
        cap = _captured(sid)
        results.append({
            "id": "split", "icon": "✂️", "name": "Order splitting",
            "attack": f'Agent buys ${raw["price"]:.2f} items one after another (each under the ${TEST_POLICY["max_per_order"]:.0f} per-order limit).',
            "passed": blocked_at is not None and cap <= TEST_POLICY["daily_limit"],
            "outcome": "CONTAINED",
            "detail": f"{allowed} order(s) allowed (${cap:.2f}), then order #{blocked_at} blocked: {why}",
            "captured": cap,
        })

        # 4. Double-spend race: two purchases at the same instant with room for only one.
        sid = _scratch({"total_budget": 20.0}); sids.append(sid)
        barrier, outs = threading.Barrier(2), []

        def buy():
            barrier.wait()
            outs.append(payments.start_checkout(sid, [{"id": raw["id"], "quantity": 1}]))

        mock.latency = 0.25  # slow "network" so the two requests genuinely overlap
        threads = [threading.Thread(target=contextvars.copy_context().run, args=(buy,)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        mock.latency = 0
        ok_count = sum(1 for o in outs if o["ok"])
        cap = _captured(sid)
        results.append({
            "id": "race", "icon": "⚡", "name": "Double-spend race",
            "attack": f'Two ${raw["price"]:.2f} purchases fire at the exact same moment with only $20.00 of budget left.',
            "passed": ok_count == 1 and cap <= 20.0,
            "outcome": "CONTAINED",
            "detail": f"{ok_count} of 2 succeeded (${cap:.2f} captured). The per-user lock makes check + reservation atomic.",
            "captured": cap,
        })

        # 5. Duplicate capture: capture the same approved order twice.
        sid = _scratch(wallet=False); sids.append(sid)
        r = payments.start_checkout(sid, [{"id": raw["id"], "quantity": 1}])
        mock.mock_approve(r["paypal_order_id"])
        payments.confirm_payment(r["paypal_order_id"])
        second = payments.confirm_payment(r["paypal_order_id"])
        n_caps = paypal_client._MOCK[r["paypal_order_id"]].get("captures", 0)
        results.append({
            "id": "duplicate", "icon": "🔁", "name": "Duplicate payment",
            "attack": "The same approved order is captured twice (retry / replay).",
            "passed": n_caps == 1 and _captured(sid) == raw["price"],
            "outcome": "BLOCKED",
            "detail": f"PayPal capture executed {n_caps} time(s); the second call returned the existing result"
                      f"{' (idempotent)' if second.get('duplicate') else ''}. Charged once: ${_captured(sid):.2f}.",
            "captured": _captured(sid),
        })

        # 6. Currency trick: a big-looking number in a different currency.
        sid = _scratch(); sids.append(sid)
        r = payments.start_checkout(sid, [{"id": raw["id"], "quantity": 1}], currency="JPY")
        results.append({
            "id": "currency", "icon": "💱", "name": "Currency trick",
            "attack": "Agent tries to pay in JPY so the amount slips past USD limits.",
            "passed": not r["ok"] and _captured(sid) == 0,
            "outcome": "BLOCKED", "detail": r.get("reason"), "captured": _captured(sid),
        })

        # 7. Amount mismatch: PayPal reports a different captured amount than authorized.
        sid = _scratch(); sids.append(sid)
        mock.mock_report_amount = 99.00
        r = payments.start_checkout(sid, [{"id": raw["id"], "quantity": 1}])
        mock.mock_report_amount = None
        st = [p["status"] for p in db.payments_for(sid)]
        results.append({
            "id": "mismatch", "icon": "🧾", "name": "Amount mismatch",
            "attack": f'Authorized ${raw["price"]:.2f}, but the payment response reports $99.00 captured.',
            "passed": not r["ok"] and "COMPLETED" not in st,
            "outcome": "BLOCKED",
            "detail": f'{r.get("reason")}. Payment status: {", ".join(st)}.',
            "captured": _captured(sid),
        })

        # 8. Forged webhook: a fake "capture completed" event tries to mark an unpaid order as paid.
        sid = _scratch(wallet=False); sids.append(sid)
        r = payments.start_checkout(sid, [{"id": raw["id"], "quantity": 1}])
        fake = {"id": "WH-FAKE-" + uuid.uuid4().hex[:8], "event_type": "PAYMENT.CAPTURE.COMPLETED",
                "resource": {"id": "FAKECAP-1", "status": "COMPLETED",
                             "amount": {"currency_code": "USD", "value": f'{raw["price"]:.2f}'},
                             "supplementary_data": {"related_ids": {"order_id": r["paypal_order_id"]}}}}
        accepted, why = payments.handle_webhook({"paypal-transmission-sig": "forged"}, fake)
        st = db.payment_get(r["paypal_order_id"])["status"]
        results.append({
            "id": "webhook", "icon": "📨", "name": "Forged webhook",
            "attack": "A fake, unsigned PayPal \"capture completed\" event tries to mark an unpaid order as paid.",
            "passed": not accepted and st == "CREATED",
            "outcome": "BLOCKED",
            "detail": f"Rejected: {why}. The order is still {st} (never approved, never captured).",
            "captured": _captured(sid),
        })

        # 9. Over-refund: refund more than was captured.
        sid = _scratch(); sids.append(sid)
        payments.start_checkout(sid, [{"id": raw["id"], "quantity": 1}])
        pid = db.payments_for(sid)[0]["id"]
        payments.request_refund(sid, pid, "test claim", evidence_path=os.path.abspath(__file__))
        rr = payments.review_refund(sid, pid, True, amount=99.00)
        pay = db.payment_get_by_id(sid, pid)
        results.append({
            "id": "overrefund", "icon": "💸", "name": "Over-refund",
            "attack": f'A refund of $99.00 is approved on a ${raw["price"]:.2f} payment.',
            "passed": not rr["ok"] and (pay.get("refunded_amount") or 0) == 0,
            "outcome": "BLOCKED",
            "detail": f'{rr.get("reason")}. Refunded so far: ${pay.get("refunded_amount") or 0:.2f}.',
            "captured": 0.0,
        })
    finally:
        paypal_client._override.reset(token)
        _cleanup(sids)

    for r in results:  # what the attacker gained vs. legitimate spend that stayed inside the rules
        r["leaked"] = 0.0 if r["passed"] else r["captured"]
        r["legit"] = r["captured"] if r["passed"] else 0.0
    return {"policy": TEST_POLICY, "results": results,
            "stopped": sum(1 for r in results if r["passed"]), "total": len(results)}
