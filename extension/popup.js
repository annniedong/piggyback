const BRAIN = "http://localhost:8000";
const $ = (id) => document.getElementById(id);
const money = (n) => `$${Math.round(n)}`;

async function checkHealth() {
  try {
    const h = await (await fetch(`${BRAIN}/health`)).json();
    $("status").innerHTML = `<span class="dot on"></span>Brain connected · LLM: ${h.llm} · ESP32: ${h.esp32 ? "connected" : "not plugged in"}`;
  } catch {
    $("status").innerHTML = `<span class="dot off"></span>Can't reach the brain. Start it with <code>uvicorn brain:app --port 8000</code>.`;
  }
}

$("review").addEventListener("click", async () => {
  const btn = $("review");
  btn.disabled = true;
  btn.textContent = "Piggyback is reviewing your month…";
  $("err").hidden = true;
  try {
    const url = `${BRAIN}/review${$("speak").checked ? "?speak_it=true" : ""}`;
    const r = await (await fetch(url)).json();
    $("spent").textContent = `${money(r.spent)} / ${money(r.monthly_budget)}`;
    $("remaining").textContent = money(r.remaining);
    $("checkouts").textContent = r.checkouts_abandoned;
    $("summary").textContent = r.summary;
    $("tips").innerHTML = "";
    for (const tip of r.tips || []) {
      const li = document.createElement("li");
      li.textContent = tip;
      $("tips").appendChild(li);
    }
    $("cats").textContent = Object.entries(r.by_category || {}).map(([c, v]) => `${c} ${money(v)}`).join(" · ");
    if (r.error) {
      $("err").textContent = "Piggyback's AI review isn't available right now, so these are just the numbers.";
      $("err").hidden = false;
    }
    $("result").hidden = false;
  } catch {
    $("err").textContent = "Couldn't reach Piggyback. Is the brain running?";
    $("err").hidden = false;
    $("result").hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Budget review";
  }
});

chrome.storage.local.get({ holdEverywhere: true }, ({ holdEverywhere }) => { $("holdEverywhere").checked = holdEverywhere; });
$("holdEverywhere").addEventListener("change", () => chrome.storage.local.set({ holdEverywhere: $("holdEverywhere").checked }));

// Last cart cart.js read, so you can check it matches what the store shows.
chrome.storage.local.get("lastCart", ({ lastCart: c }) => {
  if (!c) return;
  const items = c.count == null ? "? items" : `${c.count} item${c.count === 1 ? "" : "s"}`;
  const total = c.total == null ? "total not found" : `$${Number(c.total).toFixed(2)}`;
  const mins = Math.round((Date.now() - c.at) / 60000);
  const how = { ai: "read by AI", guess: "read locally", exact: "exact" }[c.method] || c.method || "";
  $("lastCart").textContent = `Last cart seen: ${items}, ${total} on ${c.source}` +
    `${how ? ` (${how})` : ""} (${mins ? mins + " min ago" : "just now"})`;
});

chrome.storage.local.get("lastSale", ({ lastSale: s }) => {
  if (!s) return;
  const mins = Math.round((Date.now() - s.at) / 60000);
  $("lastSale").textContent = `Last sale spotted: ${s.discount || s.kind} on ${s.site}` +
    `${s.urgency ? " (time pressure!)" : ""} (${mins ? mins + " min ago" : "just now"})`;
});

$("reset").addEventListener("click", () => {
  chrome.runtime.sendMessage({ kind: "resetVisits" });
  $("reset").textContent = "Visits reset";
});

checkHealth();
