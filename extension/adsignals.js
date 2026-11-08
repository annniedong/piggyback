// Piggyback ad signals: pure helpers shared by the content scripts (shop.js, adwatch.js) and
// background.js. No page access here, so `node extension/test_adsignals.js` can test them.
//
//   adSource(url)       which ad (if any) a landing URL came from: click ids, paid utm tags,
//                       Amazon sponsored-listing links
//   isSponsoredLabel(t) "Sponsored", "Ad", "Promoted", "Ad by <shop>" labels on search results
//   upsellLabel(t)      "Frequently bought together", "Customers also bought", ... section titles

(() => {
  // Click ids that ad networks add to the landing URL. fbclid is added to every outbound Facebook /
  // Instagram link, so it means "from Facebook or Instagram, often an ad", not always an ad.
  const CLICK_IDS = [
    [["gclid", "gbraid", "wbraid", "dclid"], "a Google ad"],
    [["msclkid"], "a Microsoft (Bing) ad"],
    [["ttclid"], "a TikTok ad"],
    [["twclid"], "an ad on X"],
    [["li_fat_id"], "a LinkedIn ad"],
    [["epik"], "a Pinterest ad"],
    [["rdt_cid"], "a Reddit ad"],
    [["sccid"], "a Snapchat ad"],
    [["fbclid"], "a Facebook or Instagram link (often an ad)"],
  ];
  // utm_medium values marketers use for paid traffic.
  const PAID_MEDIUM = /^(cpc|ppc|cpm|cpv|paid|paid[-_ ]?(search|social|media|shopping)|display|banner|retargeting|remarketing|shopping|sem|ads?|sponsored|influencer)$/i;

  function params(url) {
    try {
      const u = new URL(url);
      const p = new Map();
      u.searchParams.forEach((v, k) => p.set(k.toLowerCase(), v));
      return { u, p };
    } catch {
      return null;
    }
  }

  // null, or { kind: "ad" | "sponsored", source: "a Google ad" }
  function adSource(url) {
    const parsed = params(url);
    if (!parsed) return null;
    const { u, p } = parsed;
    // Amazon sponsored products go through /sspa/click and land with ref=..._sspa, sr=...-spons, or sp_csd.
    if (/(^|\.)amazon\./i.test(u.hostname) &&
        (/\/sspa\/click|_sspa(\b|_|$)/i.test(u.pathname) || /-spons(\b|$)/i.test(p.get("sr") || "") || p.has("sp_csd"))) {
      return { kind: "sponsored", source: "a sponsored Amazon listing" };
    }
    for (const [ids, source] of CLICK_IDS) {
      if (ids.some((id) => p.has(id))) return { kind: "ad", source };
    }
    const medium = p.get("utm_medium") || "";
    if (PAID_MEDIUM.test(medium.trim())) {
      const from = (p.get("utm_source") || "").trim().slice(0, 30);
      return { kind: "ad", source: `a paid ad${from ? ` from ${from}` : ""} (utm_medium=${medium.trim().slice(0, 20)})` };
    }
    return null;
  }

  const SPONSORED = /^(sponsored|sponsored (ad|listing|product|result)s?|ad|ads|promoted|promoted listing|ad by .{1,40}|featured \(?sponsored\)?)$/i;
  function isSponsoredLabel(text) {
    const t = (text || "").replace(/\s+/g, " ").trim().replace(/[·•|:]+$/, "").trim();
    return t.length <= 50 && SPONSORED.test(t);
  }

  const UPSELL = /(frequently bought together|bought together|customers (who bought this )?(also )?(bought|viewed|purchased)|people also (bought|viewed)|you (may|might) also (like|need)|complete the look|shop the look|wear it with|pairs? (well )?with|goes (well )?with|add-?ons?\b|recommended for you|you might like|similar items|related products|before you go|don't forget|add all \w+ to (cart|bag|basket))/i;
  function upsellLabel(text) {
    const t = (text || "").replace(/\s+/g, " ").trim();
    if (!t || t.length > 120) return null;
    const m = t.match(UPSELL);
    return m ? m[0] : null;
  }

  globalThis.PiggybackAds = { adSource, isSponsoredLabel, upsellLabel };
})();
