// Piggyback cart watcher (any store): finds the cart, reads its item count and total, and reports
// them to background.js, which says the skeptical / satisfied / disappointed lines.
//
// Where the cart is found:
//   1. Demo store: data-count / data-price on the Place order button (exact, no AI).
//   2. A slide-out cart drawer / mini-cart that opens after "Add to bag" (any page).
//   3. A cart / bag / basket page (by URL, or a "Your cart / Shopping bag" heading).
// For 2 and 3, the cart's text goes to the brain's AI (POST /cart via background.js), which pulls
// out the item count and subtotal on any layout. A local guess (labeled "Subtotal" amount, "3 items",
// quantity fields) is sent along and used when the brain is off.
//
// Never reads checkout / payment pages: they contain addresses and card fields.

(() => {
  const MONEY = /[$€£]\s*([0-9][0-9,]*(?:\.[0-9]{2})?)/;
  const TOTAL_LABELS = [/^\s*(estimated\s+)?(subtotal|sub-total)\b/i, /^\s*(estimated|order|merchandise|bag|cart|basket)\s+total\b/i, /^\s*(total|grand total)\b/i];
  const CART_PATH = /\/(cart|bag|basket|trolley|shopping-?bag|shopping-?cart|viewcart)(\/|$|\.|\?)/i;
  const PRIVATE_PATH = /\/(checkout|payment|billing|shipping|account|login|signin|register)(\/|$|\.|\?)/i;
  const CART_HEADING = /^\s*(your |my |shopping )?(cart|bag|basket|shopping bag|shopping cart)\b/i;
  const CART_WORDS = /\b(subtotal|order total|estimated total)\b/i;
  const CHECKOUT_WORDS = /\b(check ?out|view (bag|cart|basket)|go to (bag|cart))\b/i;
  const DRAWER_SEL = '[role="dialog"], [aria-modal="true"], [class*="drawer" i], [class*="minicart" i], ' +
    '[class*="mini-cart" i], [class*="side-cart" i], [class*="cart-flyout" i], [class*="flyout" i], ' +
    '[id*="minicart" i], [id*="cart-drawer" i], [class*="cart-drawer" i], [class*="slideout" i]';

  // offsetParent is null for position:fixed elements (cart drawers), so check size and style instead.
  const visible = (el) => {
    if (!el || !el.getClientRects().length) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none" && parseFloat(st.opacity || "1") > 0.05;
  };
  // Horizontally within the window (drawers slide in from the side); onScreen also checks vertically.
  const onScreenX = (el) => { const r = el.getBoundingClientRect(); return r.right > 4 && r.left < innerWidth - 4; };
  const onScreen = (el) => { const r = el.getBoundingClientRect();
    return onScreenX(el) && r.bottom > 4 && r.top < innerHeight - 4; };
  const textOf = (el) => (el && el.innerText ? el.innerText.trim() : "");
  // "$1,234.50", "USD 1,234.50", "1.234,50 €", "3 200,00 EUR", "£36"
  const MONEY_ANY = /(?:[$€£]|\b(?:USD|EUR|GBP|CAD|AUD)\b)\s*([0-9][0-9.,\s]*[0-9]|[0-9])|([0-9][0-9.,\s]*[0-9]|[0-9])\s*(?:[$€£]|\b(?:USD|EUR|GBP|CAD|AUD)\b)/;
  const money = (text) => {
    const m = text && text.match(MONEY_ANY);
    if (!m) return null;
    let n = (m[1] || m[2]).replace(/\s/g, "");
    n = /,\d{2}$/.test(n) ? n.replace(/\./g, "").replace(",", ".") : n.replace(/,/g, "");
    const v = parseFloat(n);
    return Number.isFinite(v) ? v : null;
  };

  function isPrivatePage() {
    if (PRIVATE_PATH.test(location.pathname)) return true;
    return [...document.querySelectorAll('input[autocomplete^="cc-"], input[type="password"], ' +
      'input[name*="cardnumber" i], input[name*="card_number" i], input[name*="card-number" i]')].some(visible);
  }

  // ------------------------------------------------------------------ find the cart

  function findCart() {
    // Open cart drawer: visible, mentions a subtotal and a checkout / view-bag button.
    for (const el of document.querySelectorAll(DRAWER_SEL)) {
      if (!visible(el)) continue;
      if (!onScreen(el)) continue; // closed drawers are often just slid off-screen
      const t = textOf(el);
      if (t.length > 20 && t.length < 15000 && CART_WORDS.test(t) && CHECKOUT_WORDS.test(t)) {
        return { el, where: "cart drawer" };
      }
    }
    const main = document.querySelector("main, [role='main'], #main, #content") || document.body;
    if (CART_PATH.test(location.pathname)) return { el: main, where: "cart page" };
    const heading = [...document.querySelectorAll("h1, h2")].find((h) => visible(h) && onScreenX(h) && CART_HEADING.test(textOf(h)));
    if (heading && CART_WORDS.test(textOf(main))) return { el: main, where: "cart page" };
    return null;
  }

  // ------------------------------------------------------------------ local guess (no AI)

  function guessTotal(scope) {
    const candidates = [...scope.querySelectorAll("*")].filter((el) => el.children.length <= 4 && visible(el));
    for (const label of TOTAL_LABELS) {
      for (const el of candidates) {
        const t = textOf(el);
        if (!t || t.length > 80 || !label.test(t)) continue;
        const amount = money(t) ?? money(textOf(el.nextElementSibling))
          ?? (textOf(el.parentElement).length < 140 ? money(textOf(el.parentElement)) : null);
        if (amount != null) return amount;
      }
    }
    return null;
  }

  function guessCount(scope) {
    const qty = [...scope.querySelectorAll(
      'select[name*="qty" i], select[name*="quantity" i], input[name*="qty" i], input[name*="quantity" i], ' +
      'select[aria-label*="quantity" i], input[aria-label*="quantity" i]')]
      .filter(visible).map((el) => parseInt(el.value, 10)).filter((n) => Number.isFinite(n) && n > 0);
    if (qty.length) return qty.reduce((a, b) => a + b, 0);
    const text = textOf(scope);
    const m = text.match(/\b(\d{1,3})\s+items?\b/i) || text.match(/\b(?:bag|cart|basket)\s*\(\s*(\d{1,3})\s*\)/i);
    return m ? parseInt(m[1], 10) : null;
  }

  // ------------------------------------------------------------------ read + report

  function readCart() {
    const demo = document.querySelector('[data-piggyback="place-order"]');
    if (demo) {
      return { count: parseInt(demo.dataset.count, 10) || 0, total: parseFloat(demo.dataset.price) || 0,
               source: "demo store", where: "demo store", text: "" };
    }
    if (isPrivatePage()) return null;
    const shop = !window.piggybackIsShop || window.piggybackIsShop();
    if (!shop && !CART_PATH.test(location.pathname)) return null; // not a store: do nothing
    const found = findCart();
    if (!found) return null;
    const text = textOf(found.el).replace(/\n{3,}/g, "\n\n").slice(0, 7000);
    return {
      count: guessCount(found.el), total: guessTotal(found.el),
      source: location.hostname.replace(/^www\./, ""), where: found.where, text,
    };
  }

  let last = "";
  function report() {
    const cart = readCart();
    if (!cart) return;
    const key = cart.text || `${cart.count}|${cart.total}`;
    if (key === last) return;
    last = key;
    console.log("Piggyback cart found:", cart.where, { guessCount: cart.count, guessTotal: cart.total });
    try {
      chrome.runtime.sendMessage({ kind: "cart", url: location.href, title: document.title, ...cart });
    } catch {}
  }

  let timer = null;
  const soon = () => { clearTimeout(timer); timer = setTimeout(report, 600); };

  function start() {
    report();
    new MutationObserver(soon).observe(document.body, {
      subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ["data-count", "data-price", "value", "class", "aria-hidden", "open"],
    });
    document.addEventListener("change", soon, true); // quantity dropdowns
    document.addEventListener("click", soon, true);  // "Add to bag" opening a drawer
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();

  // For DevTools: piggybackReadCart() shows what would be sent right now.
  window.piggybackReadCart = readCart;
})();
