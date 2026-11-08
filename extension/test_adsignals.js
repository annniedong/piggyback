// Tests for adsignals.js. Run: node extension/test_adsignals.js
const assert = require("assert");
require("./adsignals.js");
const { adSource, isSponsoredLabel, upsellLabel } = globalThis.PiggybackAds;

const kind = (url) => (adSource(url) || {}).kind || null;
const source = (url) => (adSource(url) || {}).source || null;

// Ad click ids and paid utm tags
assert.strictEqual(source("https://shop.example/p?gclid=abc"), "a Google ad");
assert.strictEqual(source("https://shop.example/p?gbraid=x"), "a Google ad");
assert.strictEqual(source("https://shop.example/p?msclkid=1"), "a Microsoft (Bing) ad");
assert.strictEqual(source("https://shop.example/p?ttclid=1"), "a TikTok ad");
assert.match(source("https://shop.example/p?fbclid=1"), /Facebook or Instagram/);
assert.match(source("https://shop.example/p?utm_source=instagram&utm_medium=paid_social"), /paid ad from instagram/);
assert.match(source("https://shop.example/p?utm_medium=CPC"), /utm_medium=CPC/);
assert.strictEqual(adSource("https://shop.example/p?utm_medium=email&utm_source=newsletter"), null);
assert.strictEqual(adSource("https://shop.example/p?utm_medium=organic"), null);
assert.strictEqual(adSource("https://shop.example/p?q=gclid"), null);
assert.strictEqual(adSource("not a url"), null);

// Amazon sponsored listings
assert.strictEqual(kind("https://www.amazon.com/sspa/click?ie=UTF8&spc=abc"), "sponsored");
assert.strictEqual(kind("https://www.amazon.com/Mug/dp/B0123/ref=sr_1_1_sspa?keywords=mug&sr=8-1-spons"), "sponsored");
assert.strictEqual(kind("https://www.amazon.com/Mug/dp/B0123?sp_csd=d2lkZ2V0"), "sponsored");
assert.strictEqual(kind("https://www.amazon.com/Mug/dp/B0123/ref=sr_1_3?keywords=mug&sr=8-3"), null);
assert.strictEqual(kind("https://shop.example/x/ref=sr_1_1_sspa"), null); // only on Amazon

// Sponsored labels on search results
for (const t of ["Sponsored", " sponsored ", "Ad", "Ads", "Promoted", "Ad by CozyMugShop", "Sponsored ·", "Sponsored listing"])
  assert.ok(isSponsoredLabel(t), t);
for (const t of ["Add to cart", "Advertisement free shipping on orders over $50", "Adidas", "Sponsored by our partners and many other brands we love dearly", ""])
  assert.ok(!isSponsoredLabel(t), t);

// Upsell sections
assert.strictEqual(upsellLabel("Frequently bought together"), "Frequently bought together");
assert.ok(upsellLabel("Customers who bought this also bought"));
assert.ok(upsellLabel("Complete the look"));
assert.ok(upsellLabel("You may also like"));
assert.ok(upsellLabel("Add all three to Cart"));
assert.strictEqual(upsellLabel("Product details"), null);
assert.strictEqual(upsellLabel("Your cart"), null);

console.log("adsignals: all tests passed");
