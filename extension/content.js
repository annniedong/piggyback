// Piggyback content script: holds the checkout on ANY site, at the first of "Checkout" /
// "Proceed to checkout" in the cart, or the final "Place order" / "Pay now" / "Buy now", runs the
// conversation with brain.py, and releases or holds the checkout based on the verdict. Once you go
// ahead on a site, the rest of that checkout isn't held again for 30 minutes.
// The demo store marks its button with data-piggyback="place-order" plus data-item, data-price,
// data-category. On other sites the total is read from the button ("Pay $84.00") or the page's
// "Order total" / "Total" line. Turn off with "Hold checkout on all sites" in the popup.
// The WebSocket to the brain goes through background.js: Chrome blocks web pages from
// connecting to localhost directly.

(() => {
  const PLACE_ORDER = /^(place (my |your )?order|place order (and|&) pay|complete (my |your )?(order|purchase)|submit (my |your )?order|confirm (order|purchase|and pay)|pay now|pay( with [a-z ]+)?|pay [$€£]?\s?\d[\d,.]*|buy now|buy it now|purchase|complete checkout)$/i;
  // The cart's checkout button: the earliest point to step in, before shipping and payment pages.
  const CHECKOUT = /^(check ?out( now| securely)?|secure check ?out|proceed to (secure )?check ?out|continue to (check ?out|payment)|go to check ?out|start check ?out|begin check ?out|review (and|&) pay)$/i;
  const CLEARED_FOR_MS = 30 * 60 * 1000; // after going ahead, don't hold the same site's checkout again
  const site = location.hostname.replace(/^www\./, "");
  const MONEY = /[$€£]\s*([0-9][0-9,]*(?:\.[0-9]{2})?)/;
  const TOTAL_LABELS = [/^\s*(order total|total due|estimated total|grand total|total)\b/i, /^\s*(estimated\s+)?subtotal\b/i];
  const isDemoStore = /^(localhost|127\.0\.0\.1)$/.test(location.hostname) && location.pathname.startsWith("/store");

  let holdEverywhere = true;
  let cleared = {}; // site -> time the user went ahead with a checkout there
  try {
    chrome.storage.local.get({ holdEverywhere: true, clearedSites: {} }, (r) => {
      holdEverywhere = r.holdEverywhere;
      cleared = r.clearedSites;
    });
    chrome.storage.onChanged.addListener((c) => {
      if (c.holdEverywhere) holdEverywhere = c.holdEverywhere.newValue;
      if (c.clearedSites) cleared = c.clearedSites.newValue || {};
    });
  } catch {}
  const recentlyCleared = () => Date.now() - (cleared[site] || 0) < CLEARED_FOR_MS;
  function markCleared() {
    cleared = { ...cleared, [site]: Date.now() };
    try { chrome.storage.local.set({ clearedSites: cleared }); } catch {}
  }
  const REPLY_TIMEOUT_MS = 25000; // LLM + speech; after this we assume the brain is stuck
  const pageLoadedAt = Date.now();

  let busy = false;      // a conversation is open
  let bypass = false;    // set right before we re-click the button ourselves

  // ------------------------------------------------------------------ intercept

  function findCheckoutButton(target) {
    const el = target instanceof Element ? target : null;
    if (!el) return null;
    const marked = el.closest('[data-piggyback="place-order"]');
    if (marked) return marked;
    if (!isDemoStore && !holdEverywhere) return null;
    if (recentlyCleared()) return null; // already argued and went ahead on this site
    const btn = el.closest("button, input[type=submit], input[type=button], a, [role=button]");
    if (!btn) return null;
    // "Checkout · $45.00" / "Proceed to checkout (2 items)" -> "Checkout" / "Proceed to checkout"
    const label = (btn.innerText || btn.value || btn.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ")
      .replace(/\(.*?\)/g, "").replace(/[·•|–-]?\s*[$€£]\s?\d[\d,.]*/g, "").trim();
    if (label.length <= 40 && (PLACE_ORDER.test(label) || CHECKOUT.test(label))) return btn;
    return null;
  }

  const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const textOf = (el) => (el && el.innerText ? el.innerText.trim() : "");
  const money = (t) => { const m = t && t.match(MONEY); return m ? parseFloat(m[1].replace(/,/g, "")) : null; };

  function pageTotal() {
    const els = [...document.querySelectorAll("body *")].filter((el) => el.children.length <= 4 && visible(el));
    for (const label of TOTAL_LABELS) {
      for (const el of els) {
        const t = textOf(el);
        if (!t || t.length > 80 || !label.test(t)) continue;
        const amount = money(t) ?? money(textOf(el.nextElementSibling))
          ?? (textOf(el.parentElement).length < 140 ? money(textOf(el.parentElement)) : null);
        if (amount != null) return amount;
      }
    }
    return null;
  }

  // "Buy now" on a product page: the product's own price (schema.org data, price meta, or near the title).
  function productPrice() {
    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
      const m = (s.textContent || "").match(/"price"\s*:\s*"?([0-9][0-9.]*)/);
      if (m) return parseFloat(m[1]);
    }
    const meta = document.querySelector('meta[property="product:price:amount"], meta[property="og:price:amount"], [itemprop="price"]');
    const v = meta && parseFloat(meta.getAttribute("content") || textOf(meta).replace(/[^0-9.]/g, ""));
    if (Number.isFinite(v)) return v;
    const h1 = [...document.querySelectorAll("h1")].find(visible);
    return h1 ? money(textOf(h1.parentElement)) ?? money(textOf(h1.parentElement && h1.parentElement.parentElement)) : null;
  }

  function itemName() {
    const host = location.hostname.replace(/^www\./, "");
    const h1 = [...document.querySelectorAll("h1")].find(visible);
    const name = textOf(h1);
    if (name && name.length < 80 && !/checkout|cart|bag|basket|review|payment|order/i.test(name)) return name;
    return `your order on ${host}`;
  }

  function readOrder(btn) {
    const d = btn.dataset;
    let price = parseFloat(d.price);
    if (!Number.isFinite(price)) {
      const marked = document.querySelector('[data-piggyback="order-total"]');
      price = money(textOf(btn)) ?? (marked && money(textOf(marked))) ?? pageTotal() ?? productPrice() ?? 0;
    }
    // url lets the brain match what the extension noticed on this site (ad, sale, cart).
    return { item: d.item || itemName(), price, category: d.category || "other", url: location.href };
  }

  // Capture phase on window runs before any handler the page attached to the button.
  window.addEventListener("click", (e) => {
    const btn = findCheckoutButton(e.target);
    if (!btn) return;
    if (bypass) { bypass = false; return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    if (!busy) startCheckout(btn);
  }, true);

  // Also catch forms submitted with Enter.
  window.addEventListener("submit", (e) => {
    const btn = e.submitter && findCheckoutButton(e.submitter);
    if (!btn || bypass) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    if (!busy) startCheckout(btn);
  }, true);

  function releaseCheckout(btn) {
    bypass = true;
    btn.click();
    bypass = false;
  }

  // A WebSocket-like wrapper around a port to background.js, which holds the real WebSocket.
  function connectBrain() {
    const port = chrome.runtime.connect({ name: "checkout" });
    const openListeners = [];
    let closed = false;
    const sock = {
      readyState: 0, onopen: null, onmessage: null, onclose: null, onerror: null,
      send(data) { port.postMessage(JSON.parse(data)); },
      close() { try { port.disconnect(); } catch {} fireClose(); },
      addEventListener(type, fn) { if (type === "open") openListeners.push(fn); },
    };
    function fireClose() {
      if (closed) return;
      closed = true; sock.readyState = 3;
      if (sock.onclose) sock.onclose();
    }
    port.onMessage.addListener((m) => {
      if (m.type === "_open") {
        sock.readyState = 1;
        openListeners.forEach((f) => f());
        if (sock.onopen) sock.onopen();
      } else if (m.type === "_closed") {
        fireClose();
      } else if (sock.onmessage) {
        sock.onmessage({ data: JSON.stringify(m) });
      }
    });
    port.onDisconnect.addListener(fireClose);
    return sock;
  }

  // ------------------------------------------------------------------ conversation

  function startCheckout(btn) {
    busy = true;
    const order = readOrder(btn);
    const ui = overlay();
    ui.reset(order);
    ui.status("Connecting to Piggyback…");

    let ws;
    let timer = null;
    let finished = false;

    const armTimeout = () => {
      clearTimeout(timer);
      timer = setTimeout(() => fail("Piggyback stopped answering."), REPLY_TIMEOUT_MS);
    };

    const sendDecision = (outcome) => {
      const msg = { type: "decision", item: order.item, price: order.price, outcome,
                    seconds_on_page: Math.round((Date.now() - pageLoadedAt) / 1000) };
      try { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)); } catch {}
    };

    const finish = (outcome) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      if (outcome) sendDecision(outcome);
      setTimeout(() => { try { ws && ws.close(); } catch {} }, 300);
      ui.close();
      busy = false;
      if (outcome === "completed") { markCleared(); releaseCheckout(btn); }
    };

    const fail = (why) => {
      clearTimeout(timer);
      if (finished) return;
      ui.error(`${why} Is brain.py running on localhost:8000?`, {
        retry: () => { finished = true; try { ws && ws.close(); } catch {} busy = false; ui.close(); startCheckout(btn); },
        cancel: () => finish(null),
        // Real stores: never leave someone stuck when the brain is down.
        anyway: isDemoStore ? null : () => { finished = false; finish("completed"); },
      });
    };

    ui.onPutBack(() => finish("abandoned"));

    try {
      ws = connectBrain();
    } catch {
      return fail("Can't reach Piggyback.");
    }

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: "checkout", ...order }));
      ui.status("Piggyback is looking at your order…");
      armTimeout();
    };

    let opened = false;
    ws.addEventListener("open", () => { opened = true; });
    ws.onerror = () => {}; // onclose always follows and reports
    ws.onclose = () => {
      if (!finished && !ui.hasVerdict()) fail(opened ? "Lost connection to Piggyback." : "Can't reach Piggyback.");
    };

    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      clearTimeout(timer);

      if (msg.type === "question") {
        ui.say(msg.line, msg.expression);
        ui.ask((text) => {
          ws.send(JSON.stringify({ type: "answer", text }));
          ui.status("Piggyback is thinking…");
          armTimeout();
        });
      } else if (msg.type === "verdict") {
        ui.say(msg.line, msg.expression);
        ui.verdict(msg, {
          buy: () => finish("completed"),
          putBack: () => finish("abandoned"),
        });
      }
    };
  }

  // ------------------------------------------------------------------ overlay UI

  let ui = null;
  function overlay() {
    if (ui) return ui;
    const host = document.createElement("div");
    host.style.cssText = "all: initial; position: fixed; inset: 0; z-index: 2147483647; display: none;";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
        .scrim { position: fixed; inset: 0; background: rgba(20, 10, 15, .45); display: grid; place-items: center; padding: 16px; }
        .card { width: min(440px, 100%); background: #fff; color: #1d1d1f; border-radius: 16px; padding: 20px;
                box-shadow: 0 20px 60px rgba(0,0,0,.3); display: flex; flex-direction: column; gap: 14px; }
        .top { display: flex; align-items: center; gap: 12px; }
        .pig { width: 52px; height: 52px; border-radius: 50%; background: #f6b6c8; display: flex; align-items: center;
               justify-content: center; gap: 8px; flex: none; }
        .eye { width: 10px; height: 10px; background: #1d1d1f; border-radius: 50%; transition: all .2s; }
        .pig[data-expr="suspicious"] .eye, .pig[data-expr="skeptical"] .eye { height: 4px; border-radius: 2px; }
        .pig[data-expr="alert"] .eye { width: 13px; height: 13px; }
        .pig[data-expr="satisfied"] .eye { height: 5px; border-radius: 10px 10px 0 0; }
        .pig[data-expr="disappointed"] .eye { height: 5px; border-radius: 0 0 10px 10px; }
        .pig[data-expr="thinking"] .eye { animation: blink 1s infinite; }
        @keyframes blink { 50% { transform: translateY(-3px); } }
        .title { font-weight: 700; font-size: 16px; }
        .order { color: #6e6e73; font-size: 13px; }
        .line { font-size: 17px; line-height: 1.4; min-height: 24px; }
        .status { color: #6e6e73; font-size: 14px; }
        .err { color: #b00020; font-size: 14px; }
        form { display: flex; gap: 8px; }
        input { flex: 1; font: inherit; font-size: 15px; padding: 10px 12px; border: 1px solid #d1d1d6; border-radius: 10px; }
        .row { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
        button { font: inherit; font-size: 14px; padding: 9px 14px; border-radius: 10px; border: 1px solid #d1d1d6;
                 background: #fff; cursor: pointer; }
        button.primary { background: #d6336c; border-color: #d6336c; color: #fff; }
        button:disabled { opacity: .5; cursor: not-allowed; }
        .badge { display: inline-block; font-size: 12px; font-weight: 700; padding: 3px 8px; border-radius: 99px;
                 text-transform: uppercase; letter-spacing: .04em; }
        .badge.release { background: #e6f4ee; color: #0a7c55; }
        .badge.timer { background: #fff4e0; color: #9a5b00; }
        .badge.escalate { background: #fde7ec; color: #b00020; }
        .countdown { font-variant-numeric: tabular-nums; font-size: 28px; font-weight: 700; text-align: center; }
        [hidden] { display: none !important; }
      </style>
      <div class="scrim">
        <div class="card" role="dialog" aria-label="Piggyback">
          <div class="top">
            <div class="pig" data-expr="alert"><div class="eye"></div><div class="eye"></div></div>
            <div><div class="title">Piggyback paused your checkout</div><div class="order"></div></div>
          </div>
          <div class="line"></div>
          <div class="status"></div>
          <div class="err" hidden></div>
          <form hidden><input placeholder="Type your answer…" maxlength="300"><button class="primary">Send</button></form>
          <div class="verdict" hidden></div>
          <div class="row actions"></div>
        </div>
      </div>`;
    (document.body || document.documentElement).appendChild(host);

    const q = (s) => root.querySelector(s);
    const pig = q(".pig"), lineEl = q(".line"), statusEl = q(".status"), errEl = q(".err");
    const form = q("form"), input = q("input"), verdictEl = q(".verdict"), actions = q(".actions");
    let onAnswer = null, putBack = null, gotVerdict = false, countdown = null;

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text || !onAnswer) return;
      const cb = onAnswer; onAnswer = null;
      form.hidden = true;
      lineEl.textContent = "";
      cb(text);
    });

    const button = (label, cls, fn) => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = label; if (cls) b.className = cls;
      b.addEventListener("click", fn);
      actions.appendChild(b);
      return b;
    };
    const defaultActions = () => {
      actions.innerHTML = "";
      button("Put it back", "", () => putBack && putBack());
    };

    ui = {
      reset(order) {
        clearInterval(countdown);
        gotVerdict = false; onAnswer = null;
        q(".order").textContent = `${order.item} · $${Number(order.price).toFixed(2)}`;
        lineEl.textContent = ""; errEl.hidden = true; form.hidden = true; verdictEl.hidden = true;
        pig.dataset.expr = "alert";
        defaultActions();
        host.style.display = "block";
      },
      status(text) { statusEl.textContent = text; pig.dataset.expr = "thinking"; },
      say(line, expr) { lineEl.textContent = line; statusEl.textContent = ""; errEl.hidden = true; if (expr) pig.dataset.expr = expr; },
      ask(cb) { onAnswer = cb; input.value = ""; form.hidden = false; input.focus(); },
      onPutBack(fn) { putBack = fn; },
      hasVerdict() { return gotVerdict; },
      error(text, { retry, cancel, anyway }) {
        statusEl.textContent = ""; form.hidden = true;
        errEl.textContent = text; errEl.hidden = false;
        pig.dataset.expr = "disappointed";
        actions.innerHTML = "";
        button("Cancel order", "", cancel);
        if (anyway) button("Order anyway", "", anyway);
        button("Try again", "primary", retry);
      },
      verdict(msg, { buy, putBack: back }) {
        gotVerdict = true;
        form.hidden = true; actions.innerHTML = "";
        const label = { release: "Released", timer: "Cooling off", escalate: "Escalated" }[msg.verdict] || msg.verdict;
        verdictEl.innerHTML = `<span class="badge ${msg.verdict}">${label}</span>`;
        verdictEl.hidden = false;

        if (msg.verdict === "release") {
          button("Put it back", "", back);
          button("Complete order", "primary", buy);
        } else if (msg.verdict === "timer") {
          let left = Math.max(1, msg.seconds || 60);
          const cd = document.createElement("div");
          cd.className = "countdown";
          verdictEl.appendChild(cd);
          button("Put it back", "primary", back);
          const buyBtn = button("Still want it", "", buy);
          buyBtn.disabled = true;
          const tick = () => {
            cd.textContent = left > 0 ? `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "Time's up.";
            if (left <= 0) { clearInterval(countdown); buyBtn.disabled = false; }
            left -= 1;
          };
          tick();
          countdown = setInterval(tick, 1000);
        } else {
          const note = document.createElement("div");
          note.className = "status";
          note.textContent = msg.rule === "over_budget"
            ? "This is over your remaining budget. Your accountability buddy has been flagged."
            : "Piggyback is flagging this to your accountability buddy.";
          verdictEl.appendChild(note);
          button("Put it back", "primary", back);
        }
      },
      close() { clearInterval(countdown); host.style.display = "none"; },
    };
    return ui;
  }
})();
