"""
Thin PayPal REST client (Orders v2 + Payments v2) for the sandbox.

Real mode  : PAYPAL_CLIENT_ID + PAYPAL_CLIENT_SECRET set, talks to api-m.sandbox.paypal.com
Mock mode  : PAYPAL_MODE=mock, simulates the same responses offline (for tests / no-keys demos)
"""

import os
import time
import uuid

import requests

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
LIVE_BASE = "https://api-m.paypal.com"


class PayPalError(Exception):
    pass


class PayPalClient:
    def __init__(self):
        self.mode = os.environ.get("PAYPAL_MODE", "sandbox").lower()
        self.client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
        self.secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
        self.base = LIVE_BASE if self.mode == "live" else SANDBOX_BASE
        self.app_url = os.environ.get("APP_URL", "http://localhost:8000").rstrip("/")
        self._token = None
        self._token_exp = 0
        if self.mode != "mock" and not (self.client_id and self.secret):
            raise PayPalError("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (or PAYPAL_MODE=mock).")

    @property
    def is_mock(self):
        return self.mode == "mock"

    # ---------------- auth ----------------

    def _access_token(self):
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = requests.post(
            f"{self.base}/v1/oauth2/token",
            auth=(self.client_id, self.secret),
            data={"grant_type": "client_credentials"},
            timeout=20,
        )
        if r.status_code != 200:
            raise PayPalError(f"PayPal auth failed ({r.status_code}): {r.text[:300]}")
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3000))
        return self._token

    def _request(self, method, path, json_body=None, idempotency_key=None, extra_headers=None):
        headers = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}
        if idempotency_key:
            headers["PayPal-Request-Id"] = idempotency_key
        if extra_headers:
            headers.update(extra_headers)
        r = requests.request(method, f"{self.base}{path}", json=json_body, headers=headers, timeout=30)
        if r.status_code >= 400:
            raise PayPalError(f"PayPal {method} {path} failed ({r.status_code}): {r.text[:500]}")
        return r.json() if r.text else {}

    # ---------------- orders ----------------

    @staticmethod
    def _purchase_unit(items, currency, reference=None):
        item_total = round(sum(i["price"] * i["quantity"] for i in items), 2)
        return {
            "reference_id": reference or uuid.uuid4().hex[:16],
            "description": "Order placed by AgentPay Guard",
            "amount": {
                "currency_code": currency,
                "value": f"{item_total:.2f}",
                "breakdown": {"item_total": {"currency_code": currency, "value": f"{item_total:.2f}"}},
            },
            "items": [
                {
                    "name": i["name"][:127],
                    "sku": str(i.get("sku", "")),
                    "quantity": str(i["quantity"]),
                    "unit_amount": {"currency_code": currency, "value": f"{i['price']:.2f}"},
                    "category": "PHYSICAL_GOODS",
                }
                for i in items
            ],
        }

    def create_order(self, items, currency="USD", reference=None):
        """items: [{name, price, quantity, sku}]  ->  (order_id, approve_url)"""
        item_total = round(sum(i["price"] * i["quantity"] for i in items), 2)
        if self.is_mock:
            oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
            _MOCK[oid] = {"status": "CREATED", "amount": item_total}
            return oid, f"{self.app_url}/paypal/return?token={oid}&PayerID=MOCKPAYER"

        body = {
            "intent": "CAPTURE",
            "purchase_units": [self._purchase_unit(items, currency, reference)],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": "AgentPay Guard",
                        "user_action": "PAY_NOW",
                        "shipping_preference": "NO_SHIPPING",
                        "return_url": f"{self.app_url}/paypal/return",
                        "cancel_url": f"{self.app_url}/paypal/cancel",
                    }
                }
            },
        }
        data = self._request("POST", "/v2/checkout/orders", body, idempotency_key=str(uuid.uuid4()))
        approve = next((l["href"] for l in data.get("links", []) if l["rel"] in ("payer-action", "approve")), None)
        return data["id"], approve

    def get_order_status(self, order_id):
        if self.is_mock:
            return _MOCK.get(order_id, {}).get("status", "NOT_FOUND")
        return self._request("GET", f"/v2/checkout/orders/{order_id}")["status"]

    def mock_approve(self, order_id):
        if self.is_mock and order_id in _MOCK and _MOCK[order_id]["status"] == "CREATED":
            _MOCK[order_id]["status"] = "APPROVED"

    def capture_order(self, order_id):
        """-> (status, capture_id). Safe to call twice (idempotency key = order id)."""
        if self.is_mock:
            o = _MOCK.get(order_id)
            if not o or o["status"] not in ("APPROVED", "COMPLETED"):
                raise PayPalError("ORDER_NOT_APPROVED: buyer has not approved the payment yet.")
            o["status"] = "COMPLETED"
            o.setdefault("capture_id", "MOCKCAP-" + uuid.uuid4().hex[:10].upper())
            return "COMPLETED", o["capture_id"]

        data = self._request("POST", f"/v2/checkout/orders/{order_id}/capture", {}, idempotency_key=f"cap-{order_id}")
        cap = data["purchase_units"][0]["payments"]["captures"][0]
        return data["status"], cap["id"]

    # ---------------- vault (saved PayPal wallet for auto-pay) ----------------

    def create_vault_setup(self, return_url, cancel_url):
        """Step 1 of connecting a wallet: buyer approves saving PayPal once. -> (setup_token_id, approve_url)"""
        if self.is_mock:
            tid = "MOCKSETUP-" + uuid.uuid4().hex[:10].upper()
            sep = "&" if "?" in return_url else "?"
            return tid, f"{return_url}{sep}approval_token_id={tid}"
        body = {
            "payment_source": {
                "paypal": {
                    "description": "Lets AgentPay Guard pay for you within your spending mandate",
                    "usage_type": "MERCHANT",
                    "customer_type": "CONSUMER",
                    "permit_multiple_payment_tokens": False,
                    "experience_context": {
                        "brand_name": "AgentPay Guard",
                        "shipping_preference": "NO_SHIPPING",
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                    },
                }
            }
        }
        data = self._request("POST", "/v3/vault/setup-tokens", body, idempotency_key=str(uuid.uuid4()))
        approve = next((l["href"] for l in data.get("links", []) if l["rel"] in ("approve", "payer-action")), None)
        return data["id"], approve

    def create_payment_token(self, setup_token_id):
        """Step 2: turn the approved setup token into a reusable payment token. -> (vault_id, payer_email)"""
        if self.is_mock:
            return "MOCKVAULT-" + uuid.uuid4().hex[:10].upper(), "buyer@sandbox.example"
        body = {"payment_source": {"token": {"id": setup_token_id, "type": "SETUP_TOKEN"}}}
        data = self._request("POST", "/v3/vault/payment-tokens", body, idempotency_key=f"pt-{setup_token_id}")
        email = data.get("payment_source", {}).get("paypal", {}).get("email_address", "")
        return data["id"], email

    def delete_payment_token(self, vault_id):
        if self.is_mock:
            return
        self._request("DELETE", f"/v3/vault/payment-tokens/{vault_id}")

    def charge_vault(self, vault_id, items, currency="USD", reference=None):
        """Charge a saved wallet with no buyer present. -> (order_id, status, capture_id)"""
        item_total = round(sum(i["price"] * i["quantity"] for i in items), 2)
        if self.is_mock:
            oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
            cap = "MOCKCAP-" + uuid.uuid4().hex[:10].upper()
            _MOCK[oid] = {"status": "COMPLETED", "amount": item_total, "capture_id": cap}
            return oid, "COMPLETED", cap
        body = {
            "intent": "CAPTURE",
            "payment_source": {"paypal": {"vault_id": vault_id}},
            "purchase_units": [self._purchase_unit(items, currency, reference)],
        }
        data = self._request(
            "POST", "/v2/checkout/orders", body,
            idempotency_key=str(uuid.uuid4()), extra_headers={"Prefer": "return=representation"},
        )
        status = data.get("status")
        if status != "COMPLETED":  # some accounts need an explicit capture
            status, cap_id = self.capture_order(data["id"])
            return data["id"], status, cap_id
        cap = data["purchase_units"][0]["payments"]["captures"][0]
        return data["id"], cap.get("status", status), cap["id"]

    # ---------------- refunds ----------------

    def refund_capture(self, capture_id, reason="", amount=None, currency="USD"):
        """Full refund unless amount given. -> (status, refund_id)"""
        if self.is_mock:
            return "COMPLETED", "MOCKREF-" + uuid.uuid4().hex[:10].upper()
        body = {"note_to_payer": reason[:255]} if reason else {}
        if amount is not None:
            body["amount"] = {"currency_code": currency, "value": f"{amount:.2f}"}
        data = self._request(
            "POST", f"/v2/payments/captures/{capture_id}/refund", body, idempotency_key=f"ref-{capture_id}"
        )
        return data["status"], data["id"]


_MOCK = {}
_client = None


def get_client():
    global _client
    if _client is None:
        _client = PayPalClient()
    return _client
