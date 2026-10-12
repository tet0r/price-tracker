const $ = id => document.getElementById(id);
const UNAVAILABLE = ["out_of_stock", "discontinued"];
let config = {}, pageUrl = "", product = null;

async function api(path, method = "GET", body) {
  const headers = body ? { "Content-Type": "application/json" } : {};
  if (config.password) headers.Authorization = "Basic " + btoa(`${config.username || "admin"}:${config.password}`);
  const res = await fetch(config.server + "/api" + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${res.status})`);
  }
  return res.json();
}

function money(value, currency) {
  if (value == null) return "—";
  try { if (currency) return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(value); } catch {}
  return value.toFixed(2);
}

const unitPrice = u => u.price / (u.units || 1);
const hostOf = url => { try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; } };

function showError(message) {
  $("status").hidden = true;
  $("error").textContent = message; $("error").hidden = false;
}

function render() {
  $("status").hidden = true; $("error").hidden = true;
  $("product").hidden = !product; $("track").hidden = !!product;
  if (!product) return;
  const priced = product.urls.filter(u => u.price != null);
  const buyable = priced.filter(u => !UNAVAILABLE.includes(u.availability));
  const best = (buyable.length ? buyable : priced).reduce((a, b) => !a || unitPrice(b) < unitPrice(a) ? b : a, null);
  const history = product.urls.flatMap(u => u.history.map(h => h[1] / (u.units || 1)));
  $("name").textContent = product.name || "Untitled product";
  $("price").textContent = best ? money(unitPrice(best), best.currency) : "No price yet";
  $("range").textContent = history.length > 1
    ? `Low ${money(Math.min(...history), best?.currency)} · High ${money(Math.max(...history), best?.currency)}` : "Tracking started";
  const here = hostOf(pageUrl);
  $("sources").replaceChildren(...product.urls.map(u => {
    const row = document.createElement("div"), store = document.createElement("span"), price = document.createElement("b");
    row.className = "source";
    store.textContent = u.store_name + (u === best && product.urls.length > 1 ? " ★" : "");
    if (hostOf(u.url) === here) store.classList.add("here");
    price.textContent = u.last_error ? "failed" : money(u.price, u.currency);
    row.append(store, price);
    return row;
  }));
}

async function run(button, label, action) {
  const text = button.textContent;
  button.disabled = true; button.textContent = label;
  try { await action(); } catch (e) { showError(e.message); }
  button.disabled = false; button.textContent = text;
}

$("track").addEventListener("click", e => run(e.target, "Fetching price…", async () => {
  product = await api("/products", "POST", { url: pageUrl });
  render();
}));
$("check").addEventListener("click", e => run(e.target, "Checking…", async () => {
  product = await api(`/products/${product.id}/check`, "POST");
  render();
}));
$("open").addEventListener("click", () => chrome.tabs.create({ url: config.server }));

function showSettings() {
  $("main").hidden = true; $("settings").hidden = false;
  $("server").value = config.server || ""; $("username").value = config.username || ""; $("password").value = config.password || "";
}
$("open-settings").addEventListener("click", showSettings);

$("settings").addEventListener("submit", async e => {
  e.preventDefault();
  let origin;
  try { origin = new URL($("server").value.trim()).origin; } catch { return alert("Enter the full address, e.g. http://localhost:8080"); }
  // The extension may only talk to the one server you name here
  if (!await chrome.permissions.request({ origins: [origin + "/*"] })) return;
  config = { server: origin, username: $("username").value.trim(), password: $("password").value };
  await chrome.storage.local.set(config);
  $("settings").hidden = true; $("main").hidden = false;
  start();
});

async function start() {
  $("status").hidden = false; $("status").textContent = "Checking…";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  pageUrl = tab?.url || "";
  if (!/^https?:/.test(pageUrl)) return showError("Open a product page to track it.");
  try { product = await api("/lookup?url=" + encodeURIComponent(pageUrl)); render(); }
  catch (e) { showError(`${e.message}. Check the address in Settings.`); }
}

chrome.storage.local.get(["server", "username", "password"]).then(stored => {
  config = stored;
  if (config.server) start(); else showSettings();
});
