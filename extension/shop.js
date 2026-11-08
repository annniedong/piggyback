// Piggyback shop detector: decides whether the current page is a shopping page on ANY site,
// and reports shopping visits to background.js. cart.js and sale.js (loaded after this file in
// the same content-script world) use window.piggybackIsShop().
//
// Signals (scored; 3+ = shopping page):
//   known store domain .......................... always
//   schema.org Product / Offer data ............. +3   (most stores publish this for Google)
//   og:type=product or product:price meta ....... +3
//   "Add to cart / bag / basket", "Buy now" ..... +2
//   Shopify / BigCommerce / WooCommerce markers . +2
//   URL is a cart / bag / checkout page ......... +2
//   link or button to a cart / bag .............. +1
//   3+ prices visible on the page ............... +1

(() => {
  const KNOWN = ["amazon.", "target.com", "walmart.com", "urbanoutfitters.com", "ebay.", "etsy.com",
    "bestbuy.com", "freepeople.com", "anthropologie.com", "nuuly.com", "shein.", "temu.com", "zara.com", "hm.com", "nike.com", "sephora.com", "ulta.com",
    "costco.com", "wayfair.com", "asos.com", "uniqlo.com", "macys.com", "nordstrom.com"];
  const NEVER = /(^|\.)(google|bing|duckduckgo|youtube|wikipedia|github|stackoverflow|reddit)\./i;

  // Captured at document_start, before stores strip ad parameters from the URL.
  const landingUrl = location.href;
  const landingAd = globalThis.PiggybackAds ? PiggybackAds.adSource(landingUrl) : null; // see adsignals.js

  let verdict = null; // { shop: bool, score, reasons } for the current URL
  let verdictUrl = "";

  function isDemoStore() {
    return /^(localhost|127\.0\.0\.1)$/.test(location.hostname) && location.port === "8000" &&
      location.pathname.startsWith("/store");
  }

  function hasProductJsonLd() {
    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
      if (/"@type"\s*:\s*\[?\s*"(Product|Offer|AggregateOffer|ProductGroup)"/i.test(s.textContent || "")) return true;
    }
    return false;
  }

  function score() {
    const host = location.hostname.toLowerCase();
    const reasons = [];
    if (isDemoStore()) return { shop: true, score: 99, reasons: ["demo store"] };
    if (/^(localhost|127\.0\.0\.1)$/.test(host) || NEVER.test(host)) return { shop: false, score: 0, reasons: [] };
    if (KNOWN.some((k) => host.includes(k))) return { shop: true, score: 99, reasons: ["known store"] };

    let n = 0;
    const add = (pts, why) => { n += pts; reasons.push(why); };
    if (hasProductJsonLd() || document.querySelector('[itemtype*="schema.org/Product" i], [itemprop="price"]'))
      add(3, "product data");
    if (document.querySelector('meta[property="og:type"][content="product" i], meta[property="product:price:amount"], meta[property="og:price:amount"]'))
      add(3, "product meta");
    const buttons = [...document.querySelectorAll('button, input[type=submit], a[role=button], [role=button]')]
      .slice(0, 400).map((b) => (b.innerText || b.value || b.getAttribute("aria-label") || "").trim());
    if (buttons.some((t) => /^(add to (cart|bag|basket|trolley)|buy (it )?now|add to shopping (cart|bag))\b/i.test(t)))
      add(2, "add-to-cart button");
    if (document.querySelector('link[href*="cdn.shopify.com"], script[src*="cdn.shopify.com"], meta[name="shopify-checkout-api-token"], ' +
        'script[src*="bigcommerce"], body.woocommerce, link[href*="woocommerce"]'))
      add(2, "store platform");
    if (document.querySelector('a[href*="/cart" i], a[href*="/bag" i], a[href*="/basket" i], ' +
        '[aria-label*="cart" i], [aria-label*="shopping bag" i]'))
      add(1, "cart link");
    if (/\/(cart|bag|basket|trolley|checkout|shopping-?bag|shopping-?cart|viewcart)(\/|$|\.|\?)/i.test(location.pathname))
      add(2, "cart/checkout page");
    const prices = ((document.body && document.body.innerText) || "").match(/[$€£]\s?\d[\d,]*(\.\d{2})?/g) || [];
    if (prices.length >= 3) add(1, `${prices.length} prices`);
    return { shop: n >= 3, score: n, reasons };
  }

  // Cached per URL; cart.js and sale.js call this.
  window.piggybackIsShop = () => {
    if (verdictUrl !== location.href || !verdict) { verdict = score(); verdictUrl = location.href; }
    return verdict.shop;
  };
  window.piggybackShopInfo = () => (window.piggybackIsShop(), verdict); // for DevTools

  let reported = "";
  let adSent = false;
  function check() {
    verdictUrl = ""; // re-score: the page may have rendered more since last time
    if (!window.piggybackIsShop() || reported === location.href) return;
    reported = location.href;
    const ad = adSent ? null : landingAd;
    try { chrome.runtime.sendMessage({ kind: "visit", url: location.href, reasons: verdict.reasons,
                                       cameFromAd: !!ad, ad }); } catch {}
    adSent = true; // only the first shopping page of this page load counts as "from an ad"
  }

  // Stores often render late, so check after load and again a bit later; re-check on URL changes.
  setTimeout(check, 1200);
  setTimeout(check, 4000);
  let seen = location.href;
  setInterval(() => {
    if (location.href !== seen) { seen = location.href; setTimeout(check, 1500); }
  }, 1000);
})();
