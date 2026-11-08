// Piggyback ad watcher (any page): notices two clicks and tells background.js, silently.
//
//   sponsored_click  a click on a search / listing result labeled "Sponsored", "Ad", "Promoted",
//                    or "Ad by <shop>" (Amazon, Google Shopping, Walmart, Target, Etsy, ...)
//   upsell           an "Add to cart" click inside a "Frequently bought together" / "Customers
//                    also bought" / "Complete the look" section
//
// Uses PiggybackAds from adsignals.js (loaded first in the same content-script world).

(() => {
  const ADD_TO_CART = /^(\+\s*)?(add( all \w+| both)? to (cart|bag|basket|trolley)\b|add$)/i;
  const LABEL_SEL = "span, div, p, small, a, button, label, h2, h3, h4, h5, strong, em";
  const send = (msg) => { try { chrome.runtime.sendMessage(msg); } catch {} };
  const ownText = (el) => [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(" ").trim();

  // Is this result card marked as sponsored? Walk up from the link to the card, checking labels.
  function sponsoredCard(link) {
    let el = link;
    for (let depth = 0; el && depth < 8; depth++, el = el.parentElement) {
      if (el === document.body) break;
      if (el.matches('[data-component-type="sp-sponsored-result"], [data-ad-id], [data-ad], [data-is-sponsored="true"]') ||
          /sponsored/i.test(el.getAttribute("aria-label") || "")) return true;
      if ((el.innerText || "").length > 2000) break; // past the card, into the result list
      const labels = [...el.querySelectorAll(LABEL_SEL)].slice(0, 150);
      if (labels.some((l) => PiggybackAds.isSponsoredLabel(ownText(l) || (l.children.length ? "" : l.innerText)) ||
                             /sponsored/i.test(l.getAttribute("aria-label") || ""))) return true;
    }
    return false;
  }

  // The title of the section an add-to-cart button sits in, if it's an upsell section.
  function upsellSection(button) {
    const own = PiggybackAds.upsellLabel(button.innerText || button.value || "");
    if (own) return own; // "Add all three to Cart"
    let el = button.parentElement;
    for (let depth = 0; el && depth < 10; depth++, el = el.parentElement) {
      if (el === document.body) break;
      const named = PiggybackAds.upsellLabel(el.getAttribute("aria-label") || "");
      if (named) return named;
      const heads = el.querySelectorAll('h1, h2, h3, h4, h5, [role="heading"], [class*="title" i], [class*="heading" i]');
      for (const h of [...heads].slice(0, 8)) {
        const label = PiggybackAds.upsellLabel(h.innerText);
        if (label) return label;
      }
      if ((el.innerText || "").length > 6000) break;
    }
    return null;
  }

  window.addEventListener("click", (e) => {
    if (!globalThis.PiggybackAds) return;
    const link = e.target.closest("a[href]");
    if (link && /^https?:/i.test(link.href) && sponsoredCard(link)) {
      send({ kind: "sponsored_click", from: location.href, to: link.href });
      return;
    }
    const button = e.target.closest('button, input[type=submit], input[type=button], [role=button], a');
    if (!button) return;
    const text = (button.innerText || button.value || button.getAttribute("aria-label") || "").trim();
    if (!ADD_TO_CART.test(text)) return;
    const section = upsellSection(button);
    if (section) send({ kind: "upsell", url: location.href, label: section });
  }, true);

  // For DevTools: what would a click on this element report?
  window.piggybackAdCheck = (el) => ({ sponsored: !!el.closest("a[href]") && sponsoredCard(el.closest("a[href]")),
                                       upsell: upsellSection(el) });
})();
