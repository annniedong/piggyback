# Piggyback browser

From the repository root, install the browser dependencies in your virtual environment:

```sh
python -m pip install -r bot/requirements.txt
python -m playwright install chromium
```

Start the existing brain server (`uvicorn brain:app --port 8000`, with its dependencies
installed), then launch:

```sh
python -m bot.browser --weekly-budget 150
```

Browse normally inside the opened Chromium window. All its tabs are monitored.
Close the window or press Ctrl+C to stop. Optional flags: `--start-url` and
`--api-url` (default `http://127.0.0.1:8000/alert`).

The browser posts `{type, website, value}` to `/alert` for:

- `shopping_visit`: each observed URL change on Amazon, Target, or Walmart, including subdomains.
- `ad_referral`: shopping URLs with `gclid`, `fbclid`, or `msclkid` query parameters.
- `over_budget`: a visible cart subtotal greater than the supplied weekly budget in USD.
- `cart_count`: a visible cart item count greater than five.
- `shopping_visits`: more than six shopping URL visits in a rolling seven-day window.

The brain stays silent and keeps these as context for the next checkout on that site. Threshold warnings
fire once while the rule remains active per tab; failed threshold deliveries retry
on the next poll. Failed visit and referral deliveries are logged without retries.

Visits are counted across tabs and held in memory for this browser session. A visit
means an observed URL change, including product-page navigation. Polling happens
approximately once per second; very fast redirects may be missed. This does not
monitor other browsers or persist login state or visit history between runs.

Cart extraction uses the retailer selectors in `browser.py` and only visible,
labeled values. Retailer markup can change; unknown values skip the corresponding
rule. The budget check compares the cart subtotal with the configured limit; it
does not subtract earlier purchases. No checkout or purchase is performed.

To test without the brain, add `--dry-run`: alerts are printed instead of sent.

```sh
python -m bot.browser --weekly-budget 150 --dry-run
```

Add `--chrome` to use your installed Google Chrome instead of Playwright's Chromium.
It is still a fresh, separate Chrome window (no logins or extensions); the bot cannot
watch your everyday Chrome windows.

Run checks with `python -m unittest bot.test_detector`.
