// Piggyback content script: holds the checkout on ANY site, at the first of "Checkout" /
// "Proceed to checkout" in the cart, or the final "Place order" / "Pay now" / "Buy now", runs the
// conversation with brain.py, and releases or holds the checkout based on the verdict. Once you go
// ahead on a site, the rest of that checkout isn't held again for 30 minutes. If no button matched,
// it starts when a /checkout page opens instead.
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
    return btn && isCheckoutLabel(btn) ? btn : null;
  }

  // "Checkout · $45.00" / "Proceed to checkout (2 items)" / "CHECKOUT →" -> "Checkout" / "Proceed to checkout"
  function isCheckoutLabel(btn) {
    const clean = (t) => (t || "").replace(/\s+/g, " ").replace(/\(.*?\)/g, "")
      .replace(/[·•|–-]?\s*[$€£]\s?\d[\d,.]*/g, "").replace(/[^\p{L}\p{N}&$€£ ]+/gu, " ").replace(/\s+/g, " ").trim();
    return [btn.innerText, btn.value, btn.getAttribute("aria-label"), btn.getAttribute("title")]
      .map(clean).some((l) => l && l.length <= 40 && (PLACE_ORDER.test(l) || CHECKOUT.test(l)));
  }

  // For DevTools: right-click the button > Inspect, then piggybackCheckoutCheck($0)
  window.piggybackCheckoutCheck = (el) => {
    const btn = el && el.closest("button, input[type=submit], input[type=button], a, [role=button]");
    return { button: btn, label: btn && (btn.innerText || btn.value || btn.getAttribute("aria-label")),
             matches: !!btn && isCheckoutLabel(btn), holdEverywhere, recentlyCleared: recentlyCleared() };
  };

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
    const d = btn ? btn.dataset : {};
    let price = parseFloat(d.price);
    if (!Number.isFinite(price)) {
      const marked = document.querySelector('[data-piggyback="order-total"]');
      price = (btn && money(textOf(btn))) ?? (marked && money(textOf(marked))) ?? pageTotal() ?? productPrice() ?? 0;
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

  // Add to bag: the item goes in right away; Piggyback just says one quick line (the argument waits
  // for checkout). background.js posts it to the brain's /added.
  // Matched anywhere in the label ("Quick add · Add to Bag · $59"), but not "Added to bag" confirmations.
  const ADD_TO_BAG = /\badd (it )?to (shopping )?(cart|bag|basket|trolley)\b/i;
  const BUTTONS = "button, input[type=submit], input[type=button], a, [role=button]";
  let lastAdded = 0;
  window.addEventListener("click", (e) => {
    if (!isDemoStore && !holdEverywhere) return;
    // composedPath() reaches into web components (shadow DOM), where e.target is only the outer element.
    const btn = e.composedPath().find((el) => el instanceof Element && el.matches(BUTTONS));
    const label = btn && [btn.innerText, btn.value, btn.getAttribute("aria-label"), btn.title]
      .filter(Boolean).join(" ").trim().replace(/\s+/g, " ");
    if (!label || label.length > 80 || !ADD_TO_BAG.test(label) || Date.now() - lastAdded < 3000) return;
    lastAdded = Date.now();
    if (cleared[site]) {
      // Something new in the bag is a new purchase: hold this site's checkout again.
      const { [site]: _, ...rest } = cleared;
      cleared = rest;
      try { chrome.storage.local.set({ clearedSites: cleared }); } catch {}
    }
    const d = btn.dataset;
    const price = parseFloat(d.price);
    try {
      chrome.runtime.sendMessage({ kind: "added", url: location.href, item: d.item || itemName(),
                                   price: Number.isFinite(price) ? price : productPrice() });
    } catch {}
  }, true);

  // Also catch forms submitted with Enter.
  window.addEventListener("submit", (e) => {
    const btn = e.submitter && findCheckoutButton(e.submitter);
    if (!btn || bypass) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    if (!busy) startCheckout(btn);
  }, true);

  // Backup trigger: some stores' checkout buttons don't match (icons, odd labels, links that
  // navigate before a click handler runs). If a checkout page opens on a shopping site without a
  // conversation, start one there. Single-page stores change the URL without reloading, so poll.
  const CHECKOUT_PATH = /\/(checkout|secure-?checkout|payment)(\/|$|\.|\?)|\/checkouts\//i;
  let heldUrlPath = "";
  function checkoutPageOpened() {
    if (busy || isDemoStore || !holdEverywhere || recentlyCleared()) return;
    if (!CHECKOUT_PATH.test(location.pathname) || !(window.piggybackIsShop && window.piggybackIsShop())) return;
    if (heldUrlPath && CHECKOUT_PATH.test(heldUrlPath)) return; // once per checkout, not per step
    heldUrlPath = location.pathname;
    startCheckout(null);
  }
  setTimeout(checkoutPageOpened, 2500); // let the order total render
  let lastPath = location.pathname;
  setInterval(() => {
    if (location.pathname === lastPath) return;
    lastPath = location.pathname;
    if (!CHECKOUT_PATH.test(lastPath)) heldUrlPath = ""; // left checkout: the next visit counts again
    setTimeout(checkoutPageOpened, 2500);
  }, 1000);

  function releaseCheckout(btn) {
    if (!btn) return; // started from the checkout page itself: just let them continue
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
      // Put it back on the checkout page itself: go back to the cart.
      if (outcome === "abandoned" && !btn) history.back();
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
      } else if (msg.type === "listening") {
        ui.listening();
        armTimeout();
      } else if (msg.type === "nudge") {
        ui.say(msg.line, "thinking");
        ui.listening();
        armTimeout();
      } else if (msg.type === "heard") {
        ui.heard(msg.text);
        armTimeout();
      } else if (msg.type === "looked_away") {
        ui.status(msg.strikes >= 2 ? "You looked away twice." : "You looked away. Look at Piggyback and say it again.");
        armTimeout();
      } else if (msg.type === "confirm") {
        ui.say(msg.line, "suspicious");
        armTimeout();
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
    // A small card in the corner: eyes belong on the pig, not the screen. The page stays usable, but
    // the checkout buttons stay held (the click listener above) until the verdict.
    host.style.cssText = "all: initial; position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; display: none;";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
        .card { width: min(340px, calc(100vw - 32px)); background: #fff; color: #1d1d1f; border-radius: 14px; padding: 14px;
                box-shadow: 0 10px 40px rgba(0,0,0,.25); display: flex; flex-direction: column; gap: 10px; }
        .top { display: flex; align-items: center; gap: 12px; }
        .pig { width: 40px; height: 40px; border-radius: 50%; background: #f6b6c8; display: flex; align-items: center;
               justify-content: center; gap: 8px; flex: none; }
        .eye { width: 10px; height: 10px; background: #1d1d1f; border-radius: 50%; transition: all .2s; }
        .pig[data-expr="suspicious"] .eye, .pig[data-expr="skeptical"] .eye { height: 4px; border-radius: 2px; }
        .pig[data-expr="alert"] .eye { width: 13px; height: 13px; }
        .pig[data-expr="satisfied"] .eye { height: 5px; border-radius: 10px 10px 0 0; }
        .pig[data-expr="disappointed"] .eye { height: 5px; border-radius: 0 0 10px 10px; }
        .pig[data-expr="thinking"] .eye { animation: blink 1s infinite; }
        @keyframes blink { 50% { transform: translateY(-3px); } }
        .title { font-weight: 700; font-size: 14px; }
        .order { color: #6e6e73; font-size: 13px; }
        .line { font-size: 15px; line-height: 1.4; min-height: 21px; }
        .status { color: #6e6e73; font-size: 13px; display: flex; align-items: center; gap: 8px; }
        .status .dot { width: 8px; height: 8px; border-radius: 50%; background: #d6336c; animation: pulse 1.2s infinite; flex: none; }
        @keyframes pulse { 50% { opacity: .25; transform: scale(.7); } }
        .heard { font-size: 13px; color: #1d1d1f; background: #f5f5f7; border-radius: 10px; padding: 8px 10px; }
        .err { color: #b00020; font-size: 14px; }
        form { display: flex; gap: 8px; }
        input { flex: 1; font: inherit; font-size: 15px; padding: 10px 12px; border: 1px solid #d1d1d6; border-radius: 10px; }
        .row { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
        button { font: inherit; font-size: 14px; padding: 9px 14px; border-radius: 10px; border: 1px solid #d1d1d6;
                 background: #fff; cursor: pointer; }
        button.primary { background: #d6336c; border-color: #d6336c; color: #fff; }
        button:disabled { opacity: .5; cursor: not-allowed; }
        button.link { border: 0; padding: 0; background: none; color: #6e6e73; font-size: 13px; text-decoration: underline; align-self: flex-start; }
        .badge { display: inline-block; font-size: 12px; font-weight: 700; padding: 3px 8px; border-radius: 99px;
                 text-transform: uppercase; letter-spacing: .04em; }
        .badge.release { background: #e6f4ee; color: #0a7c55; }
        .badge.timer { background: #fff4e0; color: #9a5b00; }
        .badge.escalate { background: #fde7ec; color: #b00020; }
        .countdown { font-variant-numeric: tabular-nums; font-size: 24px; font-weight: 700; text-align: center; }
        [hidden] { display: none !important; }
      </style>
        <div class="card" role="dialog" aria-label="Piggyback">
          <div class="top">
            <div class="pig" data-expr="alert"><div class="eye"></div><div class="eye"></div></div>
            <div><div class="title">Piggyback paused your checkout</div><div class="order"></div></div>
          </div>
          <div class="line"></div>
          <div class="heard" hidden></div>
          <div class="status"></div>
          <div class="err" hidden></div>
          <button type="button" class="link type-instead" hidden>Can't talk? Type instead</button>
          <form hidden><input placeholder="Type your answer…" maxlength="300"><button class="primary">Send</button></form>
          <div class="verdict" hidden></div>
          <div class="row actions"></div>
        </div>`;
    (document.body || document.documentElement).appendChild(host);

    const q = (s) => root.querySelector(s);
    const pig = q(".pig"), lineEl = q(".line"), statusEl = q(".status"), errEl = q(".err");
    const form = q("form"), input = q("input"), verdictEl = q(".verdict"), actions = q(".actions");
    const typeInstead = q(".type-instead"), heardEl = q(".heard");
    // Answers are spoken to the pig: typed ones never count as eye contact, so typing is a fallback.
    let onAnswer = null, putBack = null, gotVerdict = false, countdown = null, typing = false;
    const showTyping = () => { typeInstead.hidden = true; form.hidden = false; input.focus(); };
    typeInstead.addEventListener("click", () => { typing = true; showTyping(); });

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text || !onAnswer) return;
      const cb = onAnswer; onAnswer = null;
      form.hidden = true; typeInstead.hidden = true;
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
        gotVerdict = false; onAnswer = null; typing = false;
        q(".order").textContent = `${order.item} · $${Number(order.price).toFixed(2)}`;
        lineEl.textContent = ""; heardEl.hidden = true; errEl.hidden = true; form.hidden = true; typeInstead.hidden = true; verdictEl.hidden = true;
        pig.dataset.expr = "alert";
        defaultActions();
        host.style.display = "block";
      },
      status(text) { statusEl.textContent = text; pig.dataset.expr = "thinking"; },
      say(line, expr) { lineEl.textContent = line; statusEl.textContent = ""; errEl.hidden = true; if (expr) pig.dataset.expr = expr; },
      // The brain is recording: show that it's waiting, so a pause to think doesn't feel like it froze.
      listening() {
        heardEl.hidden = true;
        statusEl.innerHTML = '<span class="dot"></span><span>Listening. Take your time, and look at Piggyback.</span>';
      },
      heard(text) {
        heardEl.textContent = text ? `You said: "${text}"` : "Didn't catch an answer.";
        heardEl.hidden = false; typeInstead.hidden = true;
        statusEl.textContent = "Piggyback is thinking…"; pig.dataset.expr = "thinking";
      },
      ask(cb) {
        onAnswer = cb; input.value = "";
        heardEl.hidden = true;
        if (typing) return showTyping(); // they already switched to typing in this conversation
        statusEl.textContent = "Piggyback is asking…";
        typeInstead.hidden = false;
      },
      onPutBack(fn) { putBack = fn; },
      hasVerdict() { return gotVerdict; },
      error(text, { retry, cancel, anyway }) {
        statusEl.textContent = ""; form.hidden = true; typeInstead.hidden = true;
        errEl.textContent = text; errEl.hidden = false;
        pig.dataset.expr = "disappointed";
        actions.innerHTML = "";
        button("Cancel order", "", cancel);
        if (anyway) button("Order anyway", "", anyway);
        button("Try again", "primary", retry);
      },
      verdict(msg, { buy, putBack: back }) {
        gotVerdict = true;
        form.hidden = true; typeInstead.hidden = true; actions.innerHTML = "";
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
