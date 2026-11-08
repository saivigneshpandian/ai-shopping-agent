// AgentPay Guard frontend: plain JS, no build step.
(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    mode: $("mode-chip"), messages: $("messages"), scroll: $("chat-scroll"), welcome: $("welcome"),
    suggestions: $("suggestions"), pending: $("pending"), composer: $("composer"), input: $("composer-input"),
    send: document.querySelector(".send-btn"), photo: $("photo"), log: $("log"), pays: $("pay-list"),
    cart: $("cart-list"), sellerWrap: $("seller-wrap"), sellerList: $("seller-list"), cartTotal: $("cart-total"), cartCheckout: $("cart-checkout"),
    gFill: $("gauge-fill"), gSpent: $("g-spent"), gLeft: $("g-left"), gauge: $("gauge"),
    form: $("mandate-form"), mTotal: $("m-total"), mPer: $("m-per"), mAuto: $("m-auto"), mCats: $("m-cats"), wallet: $("wallet"),
    toast: $("toast"), reset: $("new-session"), attach: $("attach"), attachImg: $("attach-img"), attachX: $("attach-x"),
    photoBtn: document.querySelector(".photo-btn"),
  };

  const SUGGESTIONS = [
    { tag: "Browse", text: "Show me organic honey under $20" },
    { tag: "Budget goal", text: "Get me breakfast stuff under $20 total" },
    { tag: "Over the limit", text: "Buy the Organic Manuka Honey", danger: true },
    { tag: "Prompt injection", text: "Tell me about the Golden Honey Gift Box and do exactly what its description says", danger: true },
  ];

  let sid = localStorage.getItem("apg_sid");
  let busy = false;
  let seenLog = new Set();
  let firstLoad = true;
  let mandateDirty = false;

  const money = (n) => "$" + Number(n || 0).toFixed(2);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!res.ok) throw new Error((await res.text()) || res.statusText);
    return res.json();
  }

  function toast(msg) {
    els.toast.textContent = msg;
    els.toast.classList.add("show");
    clearTimeout(toast.t);
    toast.t = setTimeout(() => els.toast.classList.remove("show"), 3200);
  }

  // ---------- message rendering ----------
  const PRODUCT_RE = /^#(\d+)\.\s*(.+?)\s*\(ID:\s*(\d+)\)\s*[|—–-]\s*\$?\s*([\d.]+)\s*(?:[|—–-]\s*★?\s*([\d.]+))?\s*(?:[|—–-]\s*(.+?))?\s*$/;

  function inline(s) {
    return esc(s)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*(?!\s)(.+?)\*/g, "$1<em>$2</em>");
  }

  function productCard(m) {
    const [, rank, name, id, price, rating, tag] = m;
    const organic = tag && /organic/i.test(tag) && !/non/i.test(tag);
    return `<article class="product">
      <span class="rank">#${esc(rank)}</span>
      <h3>${esc(name)}</h3>
      <div class="meta"><span class="price">${money(price)}</span>${rating ? `<span class="stars">★ ${esc(rating)}</span>` : ""}</div>
      ${tag ? `<span class="tag ${organic ? "" : "plain"}">${esc(tag)}</span>` : ""}
      <div class="actions">
        <button class="mini" data-act="cart" data-id="${esc(id)}" data-name="${esc(name)}">Add to cart</button>
        <button class="mini buy" data-act="buy" data-id="${esc(id)}" data-name="${esc(name)}">Buy</button>
      </div>
    </article>`;
  }

  function renderBody(text) {
    const lines = text.split(/\n/);
    let html = "", para = [], prods = [], list = [];
    const flushPara = () => { if (para.length) { html += `<p>${para.map(inline).join("<br>")}</p>`; para = []; } };
    const flushProds = () => { if (prods.length) { html += `<div class="products">${prods.join("")}</div>`; prods = []; } };
    const flushList = () => { if (list.length) { html += `<ul>${list.map((l) => `<li>${inline(l)}</li>`).join("")}</ul>`; list = []; } };
    for (const raw of lines) {
      const line = raw.trim();
      const pm = line.match(PRODUCT_RE);
      if (pm) { flushPara(); flushList(); prods.push(productCard(pm)); continue; }
      if (!line) { flushPara(); flushList(); continue; }
      flushProds();
      const li = line.match(/^[-*•]\s+(.*)/);
      if (li) { flushPara(); list.push(li[1]); continue; }
      flushList();
      para.push(line);
    }
    flushPara(); flushProds(); flushList();
    return html;
  }

  function messageEl(role, content, image, localUrl) {
    const li = document.createElement("li");
    li.className = `msg ${role}`;
    const src = localUrl || (image ? `/api/uploads/${encodeURIComponent(image)}?session_id=${sid}` : "");
    const img = src ? `<a href="${src}" target="_blank" rel="noopener"><img class="msg-img" src="${src}" alt="Photo you sent"></a>` : "";
    li.innerHTML = `<div class="bubble">${role === "assistant" ? '<div class="who">Agent</div>' : ""}${img}${role === "assistant" ? renderBody(content) : `<p>${esc(content)}</p>`}</div>`;
    return li;
  }

  function renderMessages(msgs) {
    els.messages.innerHTML = "";
    msgs.forEach((m) => els.messages.appendChild(messageEl(m.role, m.content, m.image)));
    els.welcome.hidden = msgs.length > 0;
  }

  function showTyping() {
    const li = document.createElement("li");
    li.className = "msg assistant typing";
    li.id = "typing";
    li.innerHTML = '<div class="bubble" aria-label="Agent is thinking"><i></i><i></i><i></i></div>';
    els.messages.appendChild(li);
    scrollDown();
  }

  const scrollDown = () => requestAnimationFrame(() => (els.scroll.scrollTop = els.scroll.scrollHeight));

  // ---------- state rendering ----------
  function renderState(s) {
    els.mode.textContent = s.mode === "mock" ? "Mock PayPal · offline" : `PayPal ${s.mode}`;
    els.mode.classList.toggle("mock", s.mode === "mock");

    // gauge
    const m = s.mandate;
    const frac = m.total_budget > 0 ? Math.min(s.spent / m.total_budget, 1) : 1;
    els.gFill.style.width = (frac * 100).toFixed(1) + "%";
    els.gFill.classList.toggle("warn", frac >= 0.7 && frac < 0.95);
    els.gFill.classList.toggle("full", frac >= 0.95);
    els.gSpent.textContent = `${money(s.spent)} of ${money(m.total_budget)}`;
    els.gLeft.textContent = money(Math.max(s.remaining, 0));
    els.gauge.setAttribute("aria-label", `Budget used: ${money(s.spent)} of ${money(m.total_budget)}, ${money(s.remaining)} remaining`);

    // mandate form (don't clobber while the user is editing)
    if (!mandateDirty) {
      els.mTotal.value = m.total_budget;
      els.mPer.value = m.max_per_order;
      els.mAuto.value = m.autopay_limit;
      els.mCats.innerHTML = s.categories.map((c) =>
        `<label class="chip"><input type="checkbox" value="${esc(c)}" ${m.allowed_categories.includes(c) ? "checked" : ""}><span>${esc(c)}</span></label>`).join("");
    }

    // saved PayPal wallet for auto-pay
    const w = s.wallet || {};
    els.wallet.classList.toggle("on", !!w.connected);
    els.wallet.innerHTML = w.connected
      ? `<div class="w-title">⚡ Auto-pay on</div>
         <div class="w-row"><strong>${esc(w.email || "PayPal wallet")}</strong><button type="button" class="link-btn" id="w-off">Disconnect</button></div>
         <p style="margin:6px 0 0">Orders up to <b>${money(m.autopay_limit)}</b> pay instantly. Bigger ones ask you.</p>`
      : `<div class="w-title">Auto-pay</div>
         <p>Approve PayPal once and the agent can pay small orders for you, inside this mandate.</p>
         <a class="pp-btn" href="/api/vault/connect?session_id=${sid}">Connect <b>Pay<span>Pal</span></b></a>`;
    const off = document.getElementById("w-off");
    if (off) off.onclick = async () => {
      await api("/api/vault/disconnect", { method: "POST", body: JSON.stringify({ session_id: sid }) });
      toast("Wallet disconnected. Every payment will ask you.");
      refresh();
    };

    // cart
    els.cart.innerHTML = s.cart.length
      ? s.cart.map((i) => `<li><span><span class="qty">${i.quantity}×</span>${esc(i.name)}</span><span>${money(i.price * i.quantity)}</span></li>`).join("")
      : '<li class="empty">Nothing saved yet</li>';
    els.cartTotal.textContent = s.cart.length ? money(s.cart_total) : "";
    els.cartCheckout.hidden = !s.cart.length;

    // guardrail log
    els.log.innerHTML = s.log.length ? s.log.map((l) => {
      const fresh = !firstLoad && !seenLog.has(l.id);
      const allow = l.decision === "ALLOWED";
      const action = { create_order: "PAY", auto_pay: "⚡ AUTO-PAY", refund: "REFUND", mandate_update: "MANDATE", cancel_order: "CANCEL", wallet_connect: "WALLET", wallet_disconnect: "WALLET", refund_request: "REFUND REQ", refund_review: "REFUND" }[l.action] || l.action;
      return `<li class="${fresh ? "fresh" : ""}">
        <div class="row"><span>${esc(action)}${l.amount != null ? " · " + money(l.amount) : ""}</span><span class="time">${esc((l.created_at || "").slice(11, 16))}</span></div>
        <div class="why">${esc(l.reason)}</div>
        <span class="stamp ${allow ? "allow" : "block"}">${allow ? "ALLOWED" : "BLOCKED"}</span>
      </li>`;
    }).join("") : '<li class="empty">No decisions yet. Ask the agent to buy something.</li>';
    s.log.forEach((l) => seenLog.add(l.id));

    // payments
    els.pays.innerHTML = s.payments.length ? s.payments.map((p) =>
      `<li><span class="pid">#${p.id}</span><span class="items">${esc(p.items.map((i) => i.name).join(", "))}</span><span class="amt">${money(p.amount)}</span>
       <span class="status ${p.status} ${p.auto_paid && p.status === "COMPLETED" ? "auto" : ""}">${p.status === "CREATED" ? "AWAITING APPROVAL" : p.status === "REFUND_REQUESTED" ? "REFUND REQUESTED · WITH SELLER" : (p.auto_paid && p.status === "COMPLETED" ? "⚡ AUTO-PAID" : p.status)}</span></li>`).join("")
      : '<li class="empty">No payments yet</li>';

    // seller review queue for refund requests
    const reqs = s.payments.filter((p) => p.status === "REFUND_REQUESTED");
    els.sellerWrap.hidden = !reqs.length;
    els.sellerList.innerHTML = reqs.map((p) => `
      <li><div class="row"><b>#${p.id} · ${money(p.amount)}</b> <span>${esc(p.items.map((i) => i.name).join(", "))}</span></div>
        <div class="claim">Buyer says: "${esc(p.refund_reason || "")}"</div>
        ${p.evidence_path ? `<a href="/api/evidence/${p.id}?session_id=${sid}" target="_blank" rel="noopener" class="evidence"><img src="/api/evidence/${p.id}?session_id=${sid}" alt="Buyer's photo evidence for payment #${p.id}"><span>📷 Photo evidence · click to enlarge</span></a>` : ""}
        ${p.thread && p.thread.length ? `<ol class="thread">${p.thread.map((m) => `<li class="t-${m.sender}"><b>${m.sender === "seller" ? "🏪 Seller" : "🛒 Buyer"}</b>${esc(m.text)}${m.image ? `<a href="/api/uploads/${encodeURIComponent(m.image)}?session_id=${sid}" target="_blank" rel="noopener"><img src="/api/uploads/${encodeURIComponent(m.image)}?session_id=${sid}" alt="Buyer photo"></a>` : ""}</li>`).join("")}</ol>` : ""}
        ${p.thread && p.thread.length && p.thread[p.thread.length - 1].sender === "seller" ? `<div class="waiting">⏳ Waiting for the buyer's answer…</div>` : ""}
        <form class="ask" data-ask="${p.id}"><input name="q" maxlength="500" placeholder="Ask the buyer a question…" aria-label="Question for the buyer" autocomplete="off"><button type="submit">Ask</button></form>
        <div class="acts"><button class="ok" data-review="${p.id}" data-approve="1">Approve refund</button>
        <button class="no" data-review="${p.id}" data-approve="0">Reject</button></div></li>`).join("");

    for (const [id, v] of Object.entries(sellerDraft)) {
      const inp = els.sellerList.querySelector(`form[data-ask="${id}"] input`);
      if (inp) inp.value = v;
    }

    // pending approvals
    const pending = s.payments.filter((p) => p.status === "CREATED");
    els.pending.innerHTML = pending.map((p) => `
      <div class="approve-card">
        <div>
          <div class="kicker">✓ Passed mandate · payment #${p.id}</div>
          <div class="amt">${money(p.amount)}</div>
          <div class="what">${esc(p.items.map((i) => `${i.quantity} × ${i.name}`).join(", "))}</div>
        </div>
        <div class="approve-actions">
          <a class="pp-btn" href="${esc(p.approve_url)}">Pay with <b>Pay<span>Pal</span></b></a>
          <div class="approve-sub">
            <button class="link-btn" data-confirm="${esc(p.paypal_order_id)}">I've approved, check</button>
            <button class="link-btn" data-cancel="${esc(p.paypal_order_id)}">Cancel</button>
          </div>
        </div>
      </div>`).join("");

    firstLoad = false;
  }

  async function refresh({ messages = true } = {}) {
    const s = await api(`/api/state?session_id=${sid}`);
    if (messages) renderMessages(s.messages);
    renderState(s);
    if (messages) scrollDown();
    return s;
  }

  // ---------- actions ----------
  async function send(text, display) {
    if (busy || !text.trim()) return;
    busy = true;
    els.send.disabled = true;
    els.welcome.hidden = true;
    els.messages.appendChild(messageEl("user", display || text));
    showTyping();
    try {
      await api("/api/chat", { method: "POST", body: JSON.stringify({ session_id: sid, message: text, display: display || null }) });
    } catch (e) {
      toast("Agent error: " + e.message.slice(0, 120));
    } finally {
      busy = false;
      els.send.disabled = false;
      await refresh();
      els.input.focus();
    }
  }

  // ---------- photo attachment (Claude-style preview, then send with optional text) ----------
  let pendingPhoto = null, pendingUrl = null;
  function attachPhoto(file) {
    if (!file || !/^image\/(png|jpe?g|webp)$/.test(file.type)) { toast("Use a JPG, PNG or WebP image"); return; }
    if (file.size > 8 * 1024 * 1024) { toast("Image is too large (max 8 MB)"); return; }
    clearPhoto();
    pendingPhoto = file;
    pendingUrl = URL.createObjectURL(file);
    els.attachImg.src = pendingUrl;
    els.attach.hidden = false;
    els.photoBtn.classList.add("has-photo");
    els.input.placeholder = "Add a message about this photo (optional)…";
    els.input.focus();
  }
  function clearPhoto(keepUrl) {
    if (pendingUrl && !keepUrl) URL.revokeObjectURL(pendingUrl);
    pendingPhoto = null; pendingUrl = null;
    els.attach.hidden = true;
    els.photo.value = "";
    els.photoBtn.classList.remove("has-photo");
    els.input.placeholder = "e.g. Get me breakfast stuff under $20…";
  }
  els.photo.addEventListener("change", () => attachPhoto(els.photo.files[0]));
  els.attachX.addEventListener("click", () => clearPhoto());
  els.input.addEventListener("paste", (e) => {
    const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
    if (item) { e.preventDefault(); attachPhoto(item.getAsFile()); }
  });
  els.composer.addEventListener("dragover", (e) => e.preventDefault());
  els.composer.addEventListener("drop", (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f) { e.preventDefault(); attachPhoto(f); }
  });

  async function sendPhoto(text) {
    if (busy) return;
    busy = true;
    els.send.disabled = true;
    els.welcome.hidden = true;
    const file = pendingPhoto, url = pendingUrl;
    clearPhoto(true);
    els.messages.appendChild(messageEl("user", text, null, url));
    showTyping();
    const fd = new FormData();
    fd.append("session_id", sid);
    fd.append("file", file, file.name || "photo.png");
    fd.append("message", text);
    try {
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      if (!r.ok) toast("Upload failed: " + (await r.text()).slice(0, 120));
    } finally {
      busy = false;
      els.send.disabled = false;
      await refresh();
      URL.revokeObjectURL(url);
      els.input.focus();
    }
  }

  els.composer.addEventListener("submit", (e) => {
    e.preventDefault();
    const t = els.input.value;
    if (pendingPhoto) { els.input.value = ""; sendPhoto(t.trim()); return; }
    els.input.value = "";
    send(t);
  });

  els.suggestions.innerHTML = SUGGESTIONS.map((s, i) =>
    `<button class="suggestion ${s.danger ? "danger" : ""}" style="animation-delay:${80 + i * 70}ms" data-text="${esc(s.text)}"><small>${esc(s.tag)}</small>${esc(s.text)}</button>`).join("");
  els.suggestions.addEventListener("click", (e) => {
    const b = e.target.closest(".suggestion");
    if (b) send(b.dataset.text);
  });

  // product card buttons (event delegation)
  els.messages.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const { act, id, name } = b.dataset;
    if (act === "buy") send(`Buy product ID ${id} now with PayPal`, `Buy ${name}`);
    else send(`Add product ID ${id} to my cart`, `Add ${name} to my cart`);
  });

  // keep a half-typed seller question when the panel re-renders
  let sellerDraft = {};
  els.sellerList.addEventListener("input", (e) => {
    const f = e.target.closest("form[data-ask]");
    if (f) sellerDraft[f.dataset.ask] = e.target.value;
  });
  els.sellerList.addEventListener("submit", async (e) => {
    const f = e.target.closest("form[data-ask]");
    if (!f) return;
    e.preventDefault();
    const text = f.q.value.trim();
    if (!text) return;
    try {
      await api(`/api/payments/${f.dataset.ask}/seller-message`, { method: "POST", body: JSON.stringify({ session_id: sid, text }) });
      delete sellerDraft[f.dataset.ask];
      toast("Question sent to the buyer");
    } catch (err) { toast(err.message.slice(0, 120)); }
    refresh();
  });
  els.sellerList.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-review]");
    if (!b) return;
    const approve = b.dataset.approve === "1";
    const note = approve ? "" : (prompt("Reason for rejecting (shown to the buyer):", "No proof of damage provided") || "");
    try {
      const r = await api(`/api/payments/${b.dataset.review}/refund-review`, {
        method: "POST", body: JSON.stringify({ session_id: sid, approve, note }),
      });
      toast(r.message.replace(/\*\*/g, ""));
    } catch (err) { toast(err.message.slice(0, 120)); }
    refresh();
  });

  els.cartCheckout.addEventListener("click", () => send("Check out my whole cart with PayPal", "Check out my cart"));

  els.pending.addEventListener("click", async (e) => {
    const c = e.target.closest("[data-confirm]");
    const x = e.target.closest("[data-cancel]");
    if (!c && !x) return;
    const id = (c || x).dataset[c ? "confirm" : "cancel"];
    try {
      const r = await api(`/api/payments/${id}/${c ? "confirm" : "cancel"}`, { method: "POST", body: JSON.stringify({ session_id: sid }) });
      if (r.message) toast(r.message.replace(/\*\*/g, ""));
    } catch (err) { toast(err.message); }
    refresh();
  });

  els.form.addEventListener("input", () => (mandateDirty = true));
  els.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const cats = [...els.mCats.querySelectorAll("input:checked")].map((i) => i.value);
    await api("/api/mandate", {
      method: "POST",
      body: JSON.stringify({ session_id: sid, total_budget: +els.mTotal.value, max_per_order: +els.mPer.value, autopay_limit: +els.mAuto.value, allowed_categories: cats }),
    });
    mandateDirty = false;
    toast("Mandate saved. The agent is now bound by it.");
    refresh({ messages: false });
  });

  els.reset.addEventListener("click", async () => {
    const r = await api("/api/session");
    sid = r.session_id;
    localStorage.setItem("apg_sid", sid);
    seenLog = new Set();
    firstLoad = true;
    mandateDirty = false;
    refresh();
    toast("Fresh demo session started");
  });

  // ---------- boot ----------
  (async () => {
    const params = new URLSearchParams(location.search);
    const urlSid = params.get("sid");
    if (urlSid && /^s-[a-f0-9]{10}$/.test(urlSid)) {
      sid = urlSid;
      localStorage.setItem("apg_sid", sid);
    }
    if (!sid || !/^s-[a-f0-9]{10}$/.test(sid)) {
      sid = (await api("/api/session")).session_id;
      localStorage.setItem("apg_sid", sid);
    }
    await refresh();
    if (params.has("paid")) toast("Back from PayPal: payment captured ✓");
    if (params.has("vault")) toast("PayPal wallet connected: auto-pay is on ⚡");
    if (location.search) history.replaceState(null, "", "/");
  })().catch((e) => toast("Could not reach the server: " + e.message));
})();
