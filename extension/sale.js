// Piggyback sale watcher (any shopping site; shop.js decides what counts as one): collects offer-looking text from a shopping page and asks background.js
// to check it with the brain's AI (POST /sale). Only pages that look like they might have an offer
// (sale words or struck-through prices) are sent, so most pages cost nothing.

(() => {
  const OFFER = /(\d{1,2}\s*%\s*off|\bsale\b|clearance|coupon|promo|\bcode\b|\bdeal\b|discount|save \$?\d|bogo|buy one|flash|limited time|markdown|was \$|ends (today|tonight|soon))/i;
  // Same rule as cart.js: never read checkout / payment / account pages (addresses, card fields).
  const PRIVATE_PATH = /\/(checkout|payment|billing|shipping|account|login|signin|register)(\/|$|\.|\?)/i;
  const STRUCK_SEL = "s, del, strike, [class*='strike' i], [class*='was-price' i], [class*='original' i], [class*='compare' i], [class*='list-price' i]";

  function collect() {
    const all = (document.body && document.body.innerText) || "";
    const lines = all.split("\n").map((l) => l.trim().replace(/\s+/g, " ")).filter(Boolean);
    const keyword_lines = [...new Set(lines.filter((l) => l.length <= 160 && OFFER.test(l)))].slice(0, 25);
    const struck_prices = [...new Set([...document.querySelectorAll(STRUCK_SEL)]
      .filter((el) => el.getClientRects().length > 0)
      .map((el) => (el.innerText || "").trim())
      .filter((t) => /\$\s*\d/.test(t) && t.length < 40))].slice(0, 15);
    return {
      url: location.href,
      title: document.title,
      text: lines.join("\n").slice(0, 3000),
      keyword_lines,
      struck_prices,
    };
  }

  let lastUrl = "";
  function check() {
    if (location.href === lastUrl) return;
    if (PRIVATE_PATH.test(location.pathname)) return;
    if (!window.piggybackIsShop || !window.piggybackIsShop()) return; // only shopping pages
    const page = collect();
    if (!page.keyword_lines.length && !page.struck_prices.length) return; // nothing offer-like
    lastUrl = location.href;
    try { chrome.runtime.sendMessage({ kind: "sale", page }); } catch {}
  }

  // Give single-page stores a moment to render, then re-check when the URL changes.
  setTimeout(check, 1200);
  setTimeout(check, 3500); // late-rendering stores
  let seenUrl = location.href;
  setInterval(() => {
    if (location.href !== seenUrl) { seenUrl = location.href; setTimeout(check, 1200); }
  }, 1000);
})();
