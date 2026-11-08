# Piggyback Chrome extension

## Load it

1. Start the brain from the repo root: `uvicorn brain:app --port 8000`
   (add `LLM_PROVIDER=mock` to test without an API key).
2. In Chrome, open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**,
   and pick this `extension/` folder.
3. Open the demo store: <http://localhost:8000/store/>

After editing extension files, click the reload icon on the Piggyback card in
`chrome://extensions`, then reload the store tab.

## What it does

- **Checkout** (`content.js`): on the demo store, clicking **Place order** opens Piggyback's
  overlay instead of placing the order. It sends `checkout` over `ws://localhost:8000/ws`,
  shows each `question` with an answer box, then the `verdict`:
  - `release`: **Complete order** places it, or **Put it back**.
  - `timer`: countdown; **Still want it** unlocks when it hits zero.
  - `escalate`: only **Put it back**.
  The choice is sent as a `decision` (`completed` / `abandoned`). If the brain is off or stops
  answering (25 s), the order stays held and the overlay offers **Try again** / **Cancel order**.
- **Budget review** (`popup.html`): toolbar button. Shows brain/ESP32 status, then calls
  `GET /review` (optionally `?speak_it=true`) and shows numbers, summary, and tips.
- **Silent signals** (`background.js`): the extension never speaks. It sends what it notices to the
  brain's `/alert`, and Piggyback brings it up out loud at that site's checkout ("An ad brought you
  here and the sale ends tonight. Would you buy it at full price?"):

  | Signal | When |
  |---|---|
  | `ad_referral` | arrived from an ad: click ids (`gclid`, `msclkid`, `ttclid`, `fbclid`, ...) or paid `utm_medium` (`cpc`, `paid_social`, ...), e.g. <http://localhost:8000/store/?gclid=demo> |
  | `sponsored` | clicked a result labeled "Sponsored" / "Ad" / "Promoted" / "Ad by …" (`adwatch.js`), or an Amazon sponsored link (`/sspa/click`, `_sspa`, `-spons`) |
  | `retargeted` | an ad brought them back to a product or store first visited over an hour earlier (`value` = days) |
  | `upsell` | "Add to cart" inside "Frequently bought together", "Customers also bought", "Complete the look", ... |
  | `shopping_visit` | each shopping page, with today's count |
  | `cart` | the cart's item count and total |
  | sale | `/sale` finds a real sale, coupon, or countdown (the brain remembers it itself) |

  Link and label rules live in `adsignals.js` (tests: `node extension/test_adsignals.js`); in DevTools,
  `piggybackAdCheck(element)` shows what a click there would report. The checkout message includes the page `url`, so the brain matches signals from the same site
  (kept for 30 minutes).

  **Works on any shopping site.** `shop.js` runs on every page and decides whether it's a shop:
  known stores always count; otherwise it scores product data (schema.org Product/Offer), product
  meta tags, "Add to cart / bag" or "Buy now" buttons, Shopify/WooCommerce/BigCommerce markers,
  cart/checkout URLs, cart links, and visible prices (3+ points = shop). Search engines, YouTube,
  Wikipedia, GitHub, and Reddit never count. In DevTools, `piggybackShopInfo()` shows the score.
  Cart reactions (`cart.js`) find the cart on any store: a cart/bag/basket page, or the slide-out
  cart drawer that opens after "Add to bag". The cart's text goes to the brain's AI
  (`POST /cart`). It reads the item count and subtotal locally first and only asks the AI when it
  can't find both or its two readers disagree (`SCRAPE_AI=auto`, the default), ignoring
  recommendations. With the brain off or `LLM_PROVIDER=mock`, a local guess is used (labeled
  "Subtotal" amount, "3 items", quantity fields). Checkout/payment/account pages are never read.
  The popup shows "Last cart seen (read by AI / guessed)"; `piggybackReadCart()` in DevTools shows
  what would be sent. Checkout is only ever held on the demo store.
  The budget lives in the brain's profile (dashboard at <http://localhost:8000/>), not the
  extension. The popup has **Reset visits**.

## Sale detection (AI)

`sale.js` runs on any page `shop.js` decides is a shop. If a page has
offer-looking text (sale, % off, code, clearance, struck-through prices), it's sent to the brain's
`POST /sale` (`sales.py`), where the AI decides if there's a real sale, coupon, or promo code
(free-shipping and newsletter sign-up banners don't count). If yes, the brain remembers it for
that site's checkout (nothing is spoken on the page). The popup shows "Last sale spotted".
Needs the brain running (restart it after pulling); uses Gemini by default, keyword matching with
`LLM_PROVIDER=mock`. Test page: <http://localhost:8000/store/?sale=1>.

## Other pages

The content script only runs on `localhost:8000/store/*`. To hold checkout on another page, add
it to `content_scripts.matches` in `manifest.json` and mark its button with
`data-piggyback="place-order"` plus `data-item`, `data-price`, `data-category` (or any button
whose text is exactly "Place order" is caught, with the price read from an element marked
`data-piggyback="order-total"`).
