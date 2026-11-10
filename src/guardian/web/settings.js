// Settings (Halo design, Settings.dc.html). The fields come from /api/settings, so the page always matches the server.

const main = Shell.start("settings");
const GROUP_ICON = { "Camera": "i-camera", "Printer controls": "i-pause", "Auto pause": "i-shield", "AI": "i-sparkle",
  "Tool changes": "i-swap", "Recording": "i-clock", "Email alerts": "i-mail" };
const RIGHT = new Set(["AI"]);
let fields = [], saved = {}, draft = {}, writable = true, aiStatus = null, offeredModel = false;

const unsavedEl = el("span", { class: "small strong", role: "status" });
const undoBtn = el("button", { type: "button", class: "btn btn-secondary" }, ic("i-undo"), "Undo");
const saveBtn = el("button", { type: "button", class: "btn btn-primary" }, "Save settings");
const left = el("div", { class: "set-col" }), right = el("div", { class: "set-col" });
const aiBox = el("div", { style: "margin-top:8px;border-top:1px solid var(--line);padding-top:18px" });
const testOut = el("div");
main.append(
  el("div", { class: "page-head" }, el("div", {}, el("h1", { class: "h-page" }, "Settings"),
    el("p", { class: "h-sub" }, "Changes only apply after you save them.")),
  el("div", { class: "savebar" }, unsavedEl, undoBtn, saveBtn)),
  el("div", { class: "set-grid" }, left, right));

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
function changed() { return Object.keys(draft).filter(k => !same(draft[k], saved[k])); }
function visible(f) {
  const prov = draft["ai.provider"] || "local";
  if (["ai.remote_model", "ai.api_key_env"].includes(f.key)) return prov !== "local";
  if (f.key === "ai.remote_url") return prov === "openai";
  if (["ai.model", "ai.ollama_url"].includes(f.key)) return prov === "local";
  return true;
}

// ---- one field ----
function control(f) {
  const v = draft[f.key];
  const set = (nv) => { draft[f.key] = nv; render(); };
  const dis = !writable;
  if (f.kind === "bool") {
    const b = el("button", { type: "button", class: "sw", role: "switch", "aria-checked": v ? "true" : "false", "aria-label": f.label, disabled: dis },
      el("span", { class: "track" }, el("span", { class: "knob" })), el("span", { class: "sw-t" }, v ? "On" : "Off"));
    b.addEventListener("click", () => set(!v));
    return b;
  }
  if (f.kind === "choice" || f.kind === "choice_num") {
    const opts = Object.entries(f.options);
    const cur = f.kind === "choice_num" ? String(v) : v;
    if (opts.length <= 3 && opts.every(([, t]) => t.length < 34)) {
      return el("div", { class: "seg", role: "radiogroup", "aria-label": f.label }, ...opts.map(([k, t]) => {
        const b = el("button", { type: "button", role: "radio", "aria-checked": k === cur ? "true" : "false", disabled: dis }, t);
        b.addEventListener("click", () => set(f.kind === "choice_num" ? Number(k) : k));
        return b;
      }));
    }
    const s = el("select", { class: "input", "aria-label": f.label, disabled: dis }, ...opts.map(([k, t]) => el("option", { value: k, selected: k === cur }, t)));
    s.addEventListener("change", () => set(f.kind === "choice_num" ? Number(s.value) : s.value));
    return s;
  }
  if (f.kind === "number" || f.kind === "number_or_empty") {
    const i = el("input", { class: "input" + (same(v, saved[f.key]) ? "" : " chg"), type: "number", step: "any", min: f.min, max: f.max,
      value: v ?? "", placeholder: f.kind === "number_or_empty" ? "Learn it" : null, "aria-label": f.label, disabled: dis });
    i.addEventListener("change", () => set(i.value.trim() === "" ? (f.kind === "number_or_empty" ? null : saved[f.key]) : Number(i.value)));
    return el("div", { class: "num" }, i, f.key.endsWith("_s") || f.key.endsWith("_every_s") ? el("span", { class: "unit" }, "seconds") : null);
  }
  if (f.kind === "multi") {
    return el("div", { class: "todo", role: "group", "aria-label": f.label }, ...Object.entries(f.options).map(([k, t]) => {
      const on = (v || []).includes(k);
      const b = el("button", { type: "button", class: "chk", role: "checkbox", "aria-checked": on ? "true" : "false", disabled: dis },
        el("span", { class: "box" }, ic("i-check")), el("span", { class: "c-t" }, t));
      b.addEventListener("click", () => set(on ? v.filter(x => x !== k) : [...(v || []), k]));
      return b;
    }));
  }
  if (f.kind === "emails") {
    const t = el("textarea", { class: "input", rows: 3, "aria-label": f.label, disabled: dis }, (v || []).join("\n"));
    t.addEventListener("change", () => set(t.value.split("\n").map(x => x.trim()).filter(Boolean)));
    return t;
  }
  const i = el("input", { class: "input", type: "text", value: v ?? "", "aria-label": f.label, disabled: dis });
  i.addEventListener("change", () => set(i.value.trim()));
  return i;
}
function field(f) {
  const optText = f.options ? Object.values(f.options).join("").length : 0;
  const stack = ["multi", "emails", "text"].includes(f.kind)
    || ((f.kind === "choice" || f.kind === "choice_num") && (Object.keys(f.options).length > 3 || optText > 22));
  const isChanged = !same(draft[f.key], saved[f.key]);
  return el("div", { class: "field" + (stack ? " stack" : "") },
    el("div", {}, el("div", { class: "f-label" }, f.label.replace(/ \(seconds\)$/, ""), isChanged ? el("span", { class: "chg-tag" }, "Changed") : null),
      f.help ? el("div", { class: "f-hint" }, f.help) : null),
    el("div", { class: "f-ctl", style: stack ? "width:100%" : null }, control(f)));
}

// ---- AI model box ----
function gb(bytes) { return (bytes / 1e9).toFixed(1) + " GB"; }
function drawAiBox() {
  const r = aiStatus;
  if (!r) { aiBox.replaceChildren(el("p", { class: "small muted" }, aiError ? "Could not check the AI: " + aiError : "Checking the AI...")); return; }
  const parts = [];
  if (r.provider && r.provider !== "local") {
    const name = r.provider === "anthropic" ? "Anthropic (Claude)" : "an OpenAI-compatible service" + (r.remote_url ? " at " + r.remote_url : "");
    parts.push(el("div", { class: "row", style: "margin-bottom:12px" }, el("h3", { class: "h-sec" }, "Online AI service"),
      el("span", { class: "right" }, r.usable ? badge("t-ok", "i-check", "Connected") : badge("t-warn", "i-alert", "Not ready"))));
    parts.push(el("div", { class: "banner t-warn", style: "margin-bottom:14px" }, ic("i-alert"), el("div", {},
      el("div", { class: "b-title" }, "Pictures and logs leave your network."),
      el("div", { class: "b-text" }, "For each check, the camera pictures and printer logs are sent to " + name + ". It may cost money per check. Local AI keeps everything here."))));
    const keyIn = el("input", { class: "input", type: "password", autocomplete: "off", "aria-label": "API key",
      placeholder: r.api_key_found ? "Type a new key to replace it" : "Paste your API key" });
    parts.push(el("div", { class: "field stack" },
      el("div", {}, el("div", { class: "f-label" }, "API key ", r.api_key_found ? badge("t-ok", "i-check", "Set (" + (r.api_key_hint || "") + ")") : badge("t-crit", "i-x", "Missing")),
        el("div", { class: "f-hint" }, r.api_key_source === "env" ? "Coming from the environment variable " + r.api_key_env + ". It wins over a key typed here."
          : "Kept in a separate file in Guardian's data folder. Never shown again, never in an export, only sent to the service you picked.")),
      el("div", { class: "row", style: "width:100%" }, keyIn,
        el("button", { type: "button", class: "btn btn-primary btn-sm", onclick: () => saveKey(keyIn.value) }, "Save key"),
        r.api_key_source === "settings" ? el("button", { type: "button", class: "btn btn-danger-quiet btn-sm", onclick: () => saveKey("") }, ic("i-trash", "sm"), "Remove key") : null)));
    parts.push(el("p", { class: "small muted" }, "Model: " + (r.model || "not set") + ". Connection: " + (r.remote_message || "not checked yet") + "."));
    aiBox.replaceChildren(...parts);
    return;
  }
  const inst = r.install || { plan: {}, state: {} }, ist = inst.state || {};
  const d = r.download || {};
  const head = !r.ollama_found ? badge("t-warn", "i-alert", "Ollama missing") : r.model_ready ? badge("t-ok", "i-check", "Ready")
    : d.running ? badge("t-info", "i-download", "Downloading") : badge("t-warn", "i-alert", "Model missing");
  parts.push(el("div", { class: "row", style: "margin-bottom:12px" }, el("h3", { class: "h-sec" }, "AI model"), el("span", { class: "right" }, head)));
  if (ist.running || ist.error) {
    parts.push(el("div", { class: "col", style: "margin-bottom:6px" },
      el("div", { class: "banner " + (ist.error ? "t-crit" : "t-info") }, ist.error ? ic("i-x") : el("span", { class: "dot pulse" }),
        el("div", {}, el("div", { class: "b-title" }, ist.error ? "Installing Ollama did not work." : "Installing Ollama"),
          el("div", { class: "b-text" }, ist.error || (ist.step || "Starting") + ". This can take a few minutes. You can leave this page and Guardian keeps going."))),
      (ist.output || []).length ? el("div", { class: "cmd", role: "log", "aria-label": "Last lines of installer output" }, ist.output.slice(-4).join("\n")) : null));
    if (ist.running) setTimeout(loadAi, 2000);
  }
  if (!r.ollama_found && !ist.running) {
    parts.push(el("div", { class: "banner t-warn", style: "margin-bottom:14px" }, ic("i-alert"), el("div", {},
      el("div", { class: "b-title" }, "Ollama not found."),
      el("div", { class: "b-text" }, "Ollama is a free program that runs the AI on this computer. Guardian needs it for the AI checks."))));
    parts.push(el("div", { class: "col", style: "margin-bottom:6px" },
      inst.plan.can_install ? el("div", { class: "row" }, el("button", { type: "button", class: "btn btn-primary", onclick: () => installOllama(false) }, ic("i-download"), "Install Ollama for me"),
        el("span", { class: "small muted" }, "Windows")) : null,
      !inst.plan.can_install ? el("div", { class: "small" }, inst.plan.manual || "") : el("div", { class: "small" }, "On Linux, run this in a terminal instead:"),
      el("div", { class: "cmd" }, "curl -fsSL https://ollama.com/install.sh | sh"),
      el("div", { class: "banner t-info" }, ic("i-info"), el("div", { class: "b-text" },
        "A Raspberry Pi is too small to run the AI. Run Ollama on a stronger computer on your network, then type its address above."))));
  }
  if (r.ollama_found) {
    for (const [name, lab] of Object.entries(r.models || {})) {
      const size = (r.installed || {})[name] ?? (r.installed || {})[name + ":latest"];
      const dl = d.running && d.model === name;
      parts.push(el("div", { class: "model" },
        el("div", {}, el("div", { class: "m-n" }, name + (name === r.model ? " (chosen)" : "")), el("div", { class: "m-s" }, lab)),
        el("div", { class: "row gap-s" },
          size ? badge("t-ok", "i-check", "Downloaded, " + gb(size)) : dl ? badge("t-info", "i-download", "Downloading") : badge("t-muted", "i-off", "Not downloaded"),
          size ? el("button", { type: "button", class: "btn btn-danger-quiet btn-sm", onclick: () => removeModel(name) }, ic("i-trash", "sm"), "Remove")
            : !dl ? el("button", { type: "button", class: "btn btn-secondary btn-sm", disabled: d.running, onclick: () => download(name, lab) }, ic("i-download", "sm"), "Download") : null),
        dl ? el("div", { style: "grid-column:1 / -1" }, el("div", { class: "bar", role: "progressbar", "aria-valuenow": d.percent || 0, "aria-valuemin": 0, "aria-valuemax": 100 },
          el("i", { style: "width:" + (d.percent || 0) + "%" })), el("div", { class: "tiny muted", style: "margin-top:4px" },
          d.percent != null ? d.percent + "% of " + gb(d.total_bytes) : (d.status || "Starting"))) : null));
    }
    if (d.error) parts.push(el("div", { class: "banner t-crit" }, ic("i-x"), el("div", { class: "b-text" }, "Download of " + d.model + " failed: " + d.error)));
    if (d.running) setTimeout(loadAi, 2000);
    if (ist.done && !r.model_ready && !offeredModel && !d.running) { offeredModel = true; setTimeout(() => download(r.model, (r.models || {})[r.model] || r.model), 100); }
  }
  aiBox.replaceChildren(...parts);
}

let aiError = null;
async function loadAi() {
  try { aiStatus = await api("/api/ai/status"); aiError = null; } catch (e) { aiStatus = null; aiError = e.message; }
  drawAiBox();
}
async function installOllama(fromSave) {
  const q = (fromSave ? "The AI needs Ollama, and it is not installed on this computer. " : "") +
    "Install Ollama for you now? It is a free program from ollama.com. It is a large download (around 1 GB or more) and takes a few minutes.";
  if (!await ask(q, { ok: "Install Ollama" })) return;
  try { await api("/api/ai/install", { method: "POST" }); } catch (e) { toast("Could not start the install: " + e.message, "t-crit"); }
  loadAi();
}
async function download(name, lab) {
  if (!await ask("Download " + lab + "? It uses that much disk space. You can remove it here later.", { ok: "Download" })) return;
  try { await api("/api/ai/download", { method: "POST", body: JSON.stringify({ model: name }) }); } catch (e) { toast("Could not start the download: " + e.message, "t-crit"); }
  loadAi();
}
async function removeModel(name) {
  if (!await ask("Remove " + name + " from this computer? You can download it again later.", { ok: "Remove", danger: true })) return;
  try { await api("/api/ai/model", { method: "DELETE", body: JSON.stringify({ model: name }) }); } catch (e) { toast("Could not remove it: " + e.message, "t-crit"); }
  loadAi();
}
async function saveKey(value) {
  if (!value.trim() && !await ask("Remove the saved API key?", { ok: "Remove", danger: true })) return;
  try {
    await api("/api/ai/key", { method: "PUT", body: JSON.stringify({ key: value.trim() || null }) });
    toast(value.trim() ? "API key saved." : "API key removed.", "t-ok");
  } catch (e) { toast("Key not saved: " + e.message, "t-crit"); }
  loadAi();
}
async function afterSaveWithAiOn() {
  await loadAi();
  const r = aiStatus;
  if (!r || (r.download || {}).running || ((r.install || {}).state || {}).running) return;
  if (r.provider && r.provider !== "local") {
    if (!r.api_key_found) toast("The online AI needs your API key. Paste it in the AI box and click Save key.", "t-warn");
    return;
  }
  if (r.ollama_found) { if (!r.model_ready) download(r.model, (r.models || {})[r.model] || r.model); return; }
  const plan = (r.install || {}).plan || {};
  if (plan.can_install) installOllama(true);
  else toast("The AI needs Ollama, and it is not installed. " + (plan.manual || "See ollama.com/download"), "t-warn");
}

// ---- self-test ----
function drawTest(r, running) {
  if (running) { testOut.replaceChildren(el("p", { class: "small muted" }, "Running...")); return; }
  if (!r) { testOut.replaceChildren(el("p", { class: "small muted" }, "Checks that everything Guardian reads on the printer is still there. Read only. Run it after a printer firmware update.")); return; }
  const bad = r.checks.filter(c => !c.ok).length;
  testOut.replaceChildren(
    el("p", { class: "small", style: "margin-bottom:6px" }, el("strong", {}, bad ? bad + " problem" + (bad > 1 ? "s" : "") + " found." : "All checks passed."),
      " ", el("span", { class: "muted" }, "Last run " + friendlyTime(r.ran_utc) + ".")),
    el("div", {}, ...r.checks.map(c => el("div", { class: "chk-r " + (c.ok ? "t-ok" : "t-crit") }, ic(c.ok ? "i-check" : "i-x"),
      el("div", {}, el("div", { class: "strong" }, c.name), el("div", { class: "small muted" }, c.detail)),
      badge(c.ok ? "t-ok" : "t-crit", null, c.ok ? "OK" : "Problem")))));
}
async function runTest(ev) {
  ev.target.disabled = true;
  drawTest(null, true);
  try { drawTest(await api("/api/selftest")); } catch (e) { testOut.replaceChildren(el("p", { class: "small" }, "Could not run: " + e.message)); }
  ev.target.disabled = false;
}

// ---- page ----
function render() {
  const groups = {};
  for (const f of fields) if (visible(f)) (groups[f.group] = groups[f.group] || []).push(f);
  const card = (name, list) => {
    const isAi = name === "AI";
    const isMail = name === "Email alerts";
    const em = ((Shell.status || {}).notifications || {}).email || {};
    return el("section", { class: "card", "aria-label": name },
      el("div", { class: "card-h" }, ic(GROUP_ICON[name] || "i-sliders"), el("h2", { class: "card-title" }, isAi ? "AI" : name),
        isAi ? el("span", { class: "right" }, badge("t-ai", "i-info", "AI guesses can be wrong"))
          : isMail ? el("span", { class: "right" }, em.configured ? badge("t-ok", "i-check", "API key found") : badge("t-warn", "i-alert", "No API key")) : null),
      el("div", { class: "card-b" }, ...list.map(field), isAi ? aiBox : null,
        isMail && em.enabled && em.configured ? el("div", { class: "row" }, el("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: async (ev) => {
          ev.target.disabled = true;
          try { await api("/api/notifications/test", { method: "POST", body: JSON.stringify({ channel: "email" }) }); toast("Test email sent. Check your inbox.", "t-ok"); }
          catch (e) { toast("Test failed: " + e.message, "t-crit"); }
          ev.target.disabled = false;
        } }, "Send test email")) : null));
  };
  left.replaceChildren(...Object.entries(groups).filter(([n]) => !RIGHT.has(n)).map(([n, l]) => card(n, l)));
  right.replaceChildren(...Object.entries(groups).filter(([n]) => RIGHT.has(n)).map(([n, l]) => card(n, l)),
    el("section", { class: "card", "aria-labelledby": "g-st" },
      el("div", { class: "card-h" }, ic("i-check"), el("h2", { class: "card-title", id: "g-st" }, "Self-test"),
        el("button", { type: "button", class: "btn btn-secondary btn-sm right", onclick: runTest }, "Run self-test")),
      el("div", { class: "card-b" }, testOut)));
  const n = changed().length;
  unsavedEl.textContent = n ? n + " change" + (n > 1 ? "s" : "") + " not saved" : "";
  for (const b of [undoBtn, saveBtn]) b.setAttribute("aria-disabled", n && writable ? "false" : "true");
  drawAiBox();
}

undoBtn.addEventListener("click", () => { if (!changed().length) return; draft = JSON.parse(JSON.stringify(saved)); render(); });
saveBtn.addEventListener("click", async () => {
  const keys = changed();
  if (!keys.length || !writable) return;
  const c = Object.fromEntries(keys.map(k => [k, draft[k]]));
  if (c["ai.provider"] && c["ai.provider"] !== "local" &&
    !await ask("With an online AI service, the camera pictures and printer logs for each check are sent to that company. Continue?", { ok: "Continue" })) return;
  if ((c["autopause.enabled"] === true || (c["camera.keepalive"] && c["camera.keepalive"] !== "off") || c["controls.enabled"] === true) &&
    !await ask("This lets Guardian send requests to the printer (turn the camera on, pause a print, or show printer controls). Save?", { ok: "Save" })) return;
  try {
    const r = await api("/api/settings", { method: "PUT", body: JSON.stringify({ changes: c }) });
    saved = r.values; draft = JSON.parse(JSON.stringify(saved));
    render();
    toast(r.changed.length ? "Saved." : "Nothing changed.", "t-ok");
    if (saved["ai.enabled"] === true) afterSaveWithAiOn(); else loadAi();
  } catch (e) { toast("Not saved: " + e.message, "t-crit"); }
});
window.addEventListener("beforeunload", (e) => { if (changed().length) { e.preventDefault(); e.returnValue = ""; } });

async function load() {
  try {
    const r = await api("/api/settings");
    fields = r.fields; saved = r.values; writable = r.writable;
    draft = JSON.parse(JSON.stringify(saved));
    render();
    if (!writable) toast("Settings cannot be changed in this mode.", "t-warn");
  } catch (e) { main.append(el("div", { class: "banner t-crit" }, ic("i-alert"), el("div", { class: "b-text" }, "Could not load settings: " + e.message))); }
}
drawTest(null);
load();
loadAi();
