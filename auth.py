"""
Accounts for AgentPay Guard: sign-up / login with salted PBKDF2 password hashes and
opaque session tokens in an HttpOnly cookie. Each user owns their shopping sessions
(mandate, wallet, chat, payments), and every API call is checked against that ownership.
"""

import contextvars
import hashlib
import hmac
import re
import secrets
import uuid

import db

COOKIE = "apg_auth"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
current_user = contextvars.ContextVar("current_user", default=None)


def init():
    with db.connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                active_session TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS user_sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        if "is_demo" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")


def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2_sha256$200000${salt}${dk.hex()}"


def _verify_password(password, stored):
    try:
        _, iters, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except ValueError:
        return False


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _new_sid():
    return "s-" + uuid.uuid4().hex[:10]


def signup(name, email, password):
    name, email = (name or "").strip()[:60], (email or "").strip().lower()[:120]
    if not name:
        return None, "Please enter your name."
    if not EMAIL_RE.match(email):
        return None, "Please enter a valid email."
    if len(password or "") < 8:
        return None, "Password must be at least 8 characters."
    sid = _new_sid()
    with db.connect() as c:
        if c.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            return None, "An account with this email already exists. Log in instead."
        cur = c.execute(
            "INSERT INTO users (email, name, password_hash, active_session) VALUES (?, ?, ?, ?)",
            (email, name, _hash_password(password), sid),
        )
        c.execute("INSERT INTO user_sessions (session_id, user_id) VALUES (?, ?)", (sid, cur.lastrowid))
        user_id = cur.lastrowid
    return _issue_token(user_id), None


def create_demo_user():
    """One-click demo: a private throw-away account (each visitor gets their own, nothing is shared)."""
    sid = _new_sid()
    email = f"demo-{secrets.token_hex(4)}@agentpay.demo"
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO users (email, name, password_hash, active_session, is_demo) VALUES (?, ?, ?, ?, 1)",
            (email, "Demo user", _hash_password(secrets.token_urlsafe(24)), sid),
        )
        c.execute("INSERT INTO user_sessions (session_id, user_id) VALUES (?, ?)", (sid, cur.lastrowid))
        user_id = cur.lastrowid
    return _issue_token(user_id)


def login(email, password):
    email = (email or "").strip().lower()
    with db.connect() as c:
        r = c.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    if not r or not _verify_password(password or "", r["password_hash"]):
        return None, "Wrong email or password."
    return _issue_token(r["id"]), None


def _issue_token(user_id):
    token = secrets.token_urlsafe(32)
    with db.connect() as c:
        c.execute("INSERT INTO auth_tokens (token_hash, user_id) VALUES (?, ?)", (_token_hash(token), user_id))
    return token


def logout(token):
    if token:
        with db.connect() as c:
            c.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (_token_hash(token),))


def user_from_token(token):
    if not token:
        return None
    with db.connect() as c:
        r = c.execute(
            """SELECT u.id, u.email, u.name, u.active_session, u.is_demo FROM auth_tokens t
               JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?""",
            (_token_hash(token),),
        ).fetchone()
    return dict(r) if r else None


def owns_session(user_id, sid):
    with db.connect() as c:
        return bool(c.execute(
            "SELECT 1 FROM user_sessions WHERE session_id = ? AND user_id = ?", (sid, user_id)
        ).fetchone())


def new_session_for(user_id):
    """'New demo': a fresh budget/chat/wallet for this user."""
    sid = _new_sid()
    with db.connect() as c:
        c.execute("INSERT INTO user_sessions (session_id, user_id) VALUES (?, ?)", (sid, user_id))
        c.execute("UPDATE users SET active_session = ? WHERE id = ?", (sid, user_id))
    return sid
