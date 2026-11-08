// Piggyback background: watches shopping in your normal Chrome, silently. What it notices goes to
// brain.py as context, and Piggyback brings it up out loud at checkout ("An ad brought you here").
//
//   ad_referral     arrived at a shopping site from an ad (gclid / fbclid / msclkid)
//   shopping_visit  shopping pages visited today (value = count)
//   cart            items and total in the cart (count, value = total)
//   sales           sale.js sends offer text to the brain's AI (/sale), which remembers what it found
//
// Works on any shopping site: shop.js decides whether a page is a shop and reports visits;
// cart.js reads the cart on cart/bag pages. Nothing here speaks.

const ALERT_URL = "http://localhost:8000/alert";
const AD_PARAMS = ["gclid", "fbclid", "msclkid"];

const lastUrl = new Map();     // tabId -> last URL we counted

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

async function postToBrain(type, website, value = null, count = null) {
  try {
    await fetch(ALERT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, website, value, count }),
    });
  } catch {
    // Brain is off; nothing to tell.
  }
}

// ------------------------------------------------------------------ visits

async function onVisit(url, cameFromAd) {
  const day = today();
  const stored = await chrome.storage.local.get(["visitDay", "visitCount"]);
  const count = (stored.visitDay === day ? stored.visitCount || 0 : 0) + 1;
  await chrome.storage.local.set({ visitDay: day, visitCount: count });

  postToBrain("shopping_visit", url, count);
  if (cameFromAd || isAdReferral(url)) postToBrain("ad_referral", url);
}

// shop.js sends this once per URL when it decides the page is a shopping page.
function onShopPage(tabId, url, cameFromAd) {
  if (lastUrl.get(tabId) === url) return;
  lastUrl.set(tabId, url);
  onVisit(url, cameFromAd);
}

chrome.tabs.onRemoved.addListener((tabId) => lastUrl.delete(tabId));

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
  onCart(tabId, { ...reading, url: msg.url, source: `${msg.source} ${msg.where}` });
}

async function onCart(tabId, { url, total, count, source, method = "exact" }) {
  await chrome.storage.local.set({ lastCart: { total, count, source, method, at: Date.now() } });
  // count or total can be null when a real store's page didn't show it; the brain skips those.
  if (url) postToBrain("cart", url, total ?? null, count ?? null);
}

// ------------------------------------------------------------------ sales (AI, via brain.py /sale)

const SALE_URL = "http://localhost:8000/sale";

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

  // The brain remembers the sale for this site's checkout; this copy is just for the popup.
  const host = new URL(page.url).hostname.replace(/^www\./, "");
  await chrome.storage.local.set({ lastSale: { ...result, site: host, at: Date.now() } });
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
  } else if (msg.kind === "resetVisits") {
    chrome.storage.local.set({ visitCount: 0 });
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
