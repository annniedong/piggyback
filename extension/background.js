// Piggyback background: watches shopping in your normal Chrome, silently. What it notices goes to
// brain.py as context, and Piggyback brings it up out loud at checkout ("An ad brought you here").
//
//   ad_referral     arrived at a shopping site from an ad (click ids or paid utm tags; detail = which ad)
//   sponsored       arrived by clicking a "Sponsored" / "Ad" search result (adwatch.js, or Amazon's links)
//   retargeted      an ad brought them back to a product or store they'd already visited (value = days)
//   upsell          added an item from a "Frequently bought together"-style section (detail = section)
//   shopping_visit  shopping pages visited today (value = count)
//   cart            items and total in the cart (count, value = total)
//   sales           sale.js sends offer text to the brain's AI (/sale), which remembers what it found
//
// Works on any shopping site: shop.js decides whether a page is a shop and reports visits;
// cart.js reads the cart on cart/bag pages. Nothing here speaks.

importScripts("adsignals.js"); // PiggybackAds.adSource

const ALERT_URL = "http://localhost:8000/alert";
const RETARGET_AFTER_MS = 60 * 60 * 1000;   // an ad bringing you back after 1 h+ counts as retargeting
const SPONSORED_MATCH_MS = 10 * 60 * 1000;  // a sponsored click counts for its site's visits for 10 min
const MAX_REMEMBERED = 300;                  // products / stores remembered for retargeting

const lastUrl = new Map();     // tabId -> last URL we counted

// ------------------------------------------------------------------ helpers

const hostOf = (url) => { try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; } };
const productKey = (url) => { try { const u = new URL(url); return hostOf(url) + u.pathname.replace(/\/ref=.*$/, ""); } catch { return ""; } };

function today() {
  return new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD, local time
}

async function postToBrain(type, website, value = null, count = null, detail = null) {
  try {
    await fetch(ALERT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, website, value, count, detail }),
    });
  } catch {
    // Brain is off; nothing to tell.
  }
}

// ------------------------------------------------------------------ visits

async function onVisit(url, landingAd) {
  const day = today();
  const stored = await chrome.storage.local.get(["visitDay", "visitCount", "seenProducts", "seenHosts", "pendingSponsored"]);
  const count = (stored.visitDay === day ? stored.visitCount || 0 : 0) + 1;
  await chrome.storage.local.set({ visitDay: day, visitCount: count });
  postToBrain("shopping_visit", url, count);

  const ad = landingAd || PiggybackAds.adSource(url);
  const host = hostOf(url), key = productKey(url), now = Date.now();
  const pending = stored.pendingSponsored;
  if (ad && ad.kind === "sponsored") {
    postToBrain("sponsored", url, null, null, ad.source);
  } else if (pending && pending.host === host && now - pending.at < SPONSORED_MATCH_MS) {
    postToBrain("sponsored", url, null, null, `a sponsored result on ${pending.from}`);
  } else if (ad) {
    postToBrain("ad_referral", url, null, null, ad.source);
  }

  // Retargeting: an ad (not a search they ran) brought them back to something they'd already seen.
  const products = stored.seenProducts || {}, hosts = stored.seenHosts || {};
  if (ad) {
    const days = (since) => Math.max(0, Math.round((now - since) / 86400000));
    if (products[key] && now - products[key] > RETARGET_AFTER_MS) {
      postToBrain("retargeted", url, days(products[key]), null, "product");
    } else if (hosts[host] && now - hosts[host] > RETARGET_AFTER_MS) {
      postToBrain("retargeted", url, days(hosts[host]), null, "store");
    }
  }
  products[key] = products[key] || now;
  hosts[host] = hosts[host] || now;
  const newest = (o) => Object.fromEntries(Object.entries(o).sort((a, b) => b[1] - a[1]).slice(0, MAX_REMEMBERED));
  await chrome.storage.local.set({ seenProducts: newest(products), seenHosts: newest(hosts) });
}

// adwatch.js: a click on a result labeled "Sponsored" / "Ad". The landing page's visit (onVisit)
// is matched to it by host, since ad links often bounce through a redirect first.
async function onSponsoredClick(from, to) {
  const ad = PiggybackAds.adSource(to);
  let host = hostOf(to);
  if (/(^|\.)google\./.test(host) || /(^|\.)bing\./.test(host)) host = ""; // ad redirect: gclid/msclkid covers it
  if (!host) return;
  await chrome.storage.local.set({ pendingSponsored: { host, from: hostOf(from), at: Date.now(), source: ad && ad.source } });
}

// shop.js sends this once per URL when it decides the page is a shopping page.
function onShopPage(tabId, url, landingAd) {
  if (lastUrl.get(tabId) === url) return;
  lastUrl.set(tabId, url);
  onVisit(url, landingAd);
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
    onShopPage(sender.tab.id, msg.url, msg.ad || null);
  } else if (msg.kind === "sponsored_click") {
    onSponsoredClick(msg.from, msg.to);
  } else if (msg.kind === "upsell") {
    postToBrain("upsell", msg.url, null, null, msg.label);
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
