// Piggyback background: watches shopping in your normal Chrome and says Piggyback's reaction
// out loud with Chrome's built-in voice (chrome.tts), so it works even when brain.py is off.
//
//   alert         more than 6 shopping visits today
//   suspicious    arrived at a shopping site from an ad (gclid / fbclid / msclkid)
//   skeptical     more than 5 items in the cart
//   satisfied     cart total within the weekly budget
//   disappointed  cart total over the weekly budget
//
// Works on any shopping site: shop.js decides whether a page is a shop and reports visits;
// cart.js reads the cart on cart/bag pages; sale.js sends offer text to the brain's AI (/sale).
// Visits are also posted to brain.py's /alert as `shopping_visit` (logged there, silent).
// The other reactions are NOT sent to the brain yet, because the brain would speak them too.

const ALERT_URL = "http://localhost:8000/alert";
const AD_PARAMS = ["gclid", "fbclid", "msclkid"];
const VISIT_LIMIT = 6;       // react when today's visits go above this
const CART_ITEM_LIMIT = 5;   // react when the cart has more items than this
const DEFAULT_WEEKLY_BUDGET = 150;

const LINES = {
  alert: "Alert. That's more than six shopping visits today.",
  suspicious: "Suspicious. An ad brought you here.",
  skeptical: "Skeptical. More than five items in your cart? Really?",
  satisfied: "Satisfied. Your cart is within your weekly budget. Nice.",
  disappointed: "Disappointed. Your cart is over your weekly budget. Put it back.",
};

const lastUrl = new Map();     // tabId -> last URL we counted
const cartRules = new Map();   // tabId -> Set of cart reactions already said (until the rule turns off)

// ------------------------------------------------------------------ speech

function react(expression, detail = "") {
  const line = LINES[expression];
  console.log("Piggyback:", expression, detail);
  chrome.tts.speak(line, { enqueue: true, rate: 1.0 });
}

// ------------------------------------------------------------------ helpers

function isAdReferral(url) {
  try {
    const params = new URL(url).searchParams;
    return AD_PARAMS.some((p) => params.has(p));
  } catch {
    return false;
  }
}

function today() {
  return new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD, local time
}

async function postToBrain(type, website, value = null) {
  try {
    await fetch(ALERT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, website, value }),
    });
  } catch {
    // Brain is off; speech still works.
  }
}

// ------------------------------------------------------------------ visits

async function onVisit(url, cameFromAd) {
  const day = today();
  const stored = await chrome.storage.local.get(["visitDay", "visitCount", "alertedDay"]);
  const count = (stored.visitDay === day ? stored.visitCount || 0 : 0) + 1;
  await chrome.storage.local.set({ visitDay: day, visitCount: count });

  postToBrain("shopping_visit", url, count);
  if (cameFromAd || isAdReferral(url)) react("suspicious", url);
  if (count > VISIT_LIMIT && stored.alertedDay !== day) {
    await chrome.storage.local.set({ alertedDay: day }); // once per day
    react("alert", `${count} visits today`);
  }
}

// shop.js sends this once per URL when it decides the page is a shopping page.
function onShopPage(tabId, url, cameFromAd) {
  if (lastUrl.get(tabId) === url) return;
  lastUrl.set(tabId, url);
  onVisit(url, cameFromAd);
}

chrome.tabs.onRemoved.addListener((tabId) => {
  lastUrl.delete(tabId);
  cartRules.delete(tabId);
});

// ------------------------------------------------------------------ cart

const CART_URL = "http://localhost:8000/cart";

// cart.js found a cart on a real store: ask the brain's AI to read it, fall back to cart.js's guess.
async function onCartText(tabId, msg) {
  let reading = { count: msg.count, total: msg.total, method: "guess" };
  try {
    const res = await fetch(CART_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: msg.url, title: msg.title, text: msg.text,
                             heuristic: { count: msg.count, total: msg.total } }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const ai = await res.json();
    if (!ai.is_cart) return; // AI says this isn't really a cart
    reading = { count: ai.count, total: ai.total, method: ai.method };
  } catch (e) {
    console.warn("Piggyback cart AI unavailable, using guess:", e.message);
  }
  onCart(tabId, { ...reading, source: `${msg.source} ${msg.where}` });
}

async function onCart(tabId, { total, count, source, method = "exact" }) {
  const { weeklyBudget = DEFAULT_WEEKLY_BUDGET } = await chrome.storage.local.get("weeklyBudget");
  await chrome.storage.local.set({ lastCart: { total, count, source, method, at: Date.now() } });
  // count or total can be null when a real store's page didn't show it; skip those rules.
  const hasItems = count == null ? total > 0 : count > 0;
  const rules = {
    skeptical: count != null && count > CART_ITEM_LIMIT,
    disappointed: total != null && hasItems && total > weeklyBudget,
    satisfied: total != null && hasItems && total <= weeklyBudget,
  };
  const said = cartRules.get(tabId) || new Set();
  cartRules.set(tabId, said);
  // Say each reaction once when its rule turns on; it can fire again after the rule turns off.
  for (const [expression, on] of Object.entries(rules)) {
    if (on && !said.has(expression)) {
      said.add(expression);
      react(expression, `cart ${count} items, $${total}, budget $${weeklyBudget}`);
    } else if (!on) {
      said.delete(expression);
    }
  }
}

// ------------------------------------------------------------------ sales (AI, via brain.py /sale)

const SALE_URL = "http://localhost:8000/sale";
const SALE_REWARN_MS = 10 * 60 * 1000; // speak about sales on the same site at most every 10 min
const saleSpokenAt = new Map();        // hostname -> time

async function onSalePage(page) {
  let result;
  try {
    const res = await fetch(SALE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(page),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    result = await res.json();
  } catch (e) {
    console.warn("Piggyback sale check unavailable (is brain.py running?)", e.message);
    return;
  }
  if (!result.on_sale) return;

  const host = new URL(page.url).hostname.replace(/^www\./, "");
  await chrome.storage.local.set({ lastSale: { ...result, site: host, at: Date.now() } });
  const now = Date.now();
  if (now - (saleSpokenAt.get(host) || 0) < SALE_REWARN_MS) return;
  saleSpokenAt.set(host, now);
  console.log("Piggyback: sale", result);
  chrome.tts.speak(`Suspicious. ${result.line}`, { enqueue: true });
}

// ------------------------------------------------------------------ messages

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.kind === "visit" && sender.tab) {
    onShopPage(sender.tab.id, msg.url, msg.cameFromAd);
  } else if (msg.kind === "cart" && sender.tab) {
    if (msg.text) onCartText(sender.tab.id, msg);
    else onCart(sender.tab.id, msg);
  } else if (msg.kind === "sale" && msg.page) {
    onSalePage(msg.page);
  } else if (msg.kind === "test" && LINES[msg.expression]) {
    chrome.tts.stop();
    react(msg.expression, "(test)");
  } else if (msg.kind === "resetVisits") {
    chrome.storage.local.set({ visitCount: 0, alertedDay: null });
  }
  sendResponse({ ok: true });
});

// ------------------------------------------------------------------ checkout relay
// content.js talks to the brain's WebSocket through this port: Chrome blocks web pages (and their
// content scripts) from connecting to localhost, but the extension itself may.

const WS_URL = "ws://localhost:8000/ws";

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "checkout") return;
  let ws;
  let opened = false;
  const queue = [];
  try {
    ws = new WebSocket(WS_URL);
  } catch {
    port.postMessage({ type: "_closed", opened: false });
    return;
  }
  ws.onopen = () => {
    opened = true;
    port.postMessage({ type: "_open" });
    queue.splice(0).forEach((m) => ws.send(m));
  };
  ws.onmessage = (ev) => {
    try { port.postMessage(JSON.parse(ev.data)); } catch {}
  };
  ws.onclose = () => {
    try { port.postMessage({ type: "_closed", opened }); } catch {}
  };
  port.onMessage.addListener((m) => {
    const data = JSON.stringify(m);
    if (opened && ws.readyState === WebSocket.OPEN) ws.send(data);
    else queue.push(data);
  });
  // Give a just-sent "decision" a moment to go out before closing.
  port.onDisconnect.addListener(() => setTimeout(() => { try { ws.close(); } catch {} }, 500));
});
