let fields = [], saved = {};

function readField(f) {
  const id = "s-" + f.key;
  if (f.kind === "bool") return document.getElementById(id).checked;
  if (f.kind === "choice") return document.getElementById(id).value;
  if (f.kind === "number") return Number(document.getElementById(id).value);
  if (f.kind === "number_or_empty") {
    const v = document.getElementById(id).value.trim();
    return v === "" ? null : Number(v);
  }
  if (f.kind === "multi") return Object.keys(f.options).filter(k => document.getElementById(id + "-" + k).checked);
  if (f.kind === "emails") return document.getElementById(id).value.split("\n").map(x => x.trim()).filter(Boolean);
  if (f.kind === "text") return document.getElementById(id).value.trim();
  return null;
}

function input(f, v) {
  const id = "s-" + f.key;
  if (f.kind === "bool") return el("input", { type: "checkbox", id, checked: v ? "checked" : null, onchange: dirty });
  if (f.kind === "choice") {
    const sel = el("select", { id, onchange: dirty },
      ...Object.entries(f.options).map(([k, label]) => el("option", { value: k, selected: k === v ? "selected" : null }, label)));
    return sel;
  }
  if (f.kind === "number" || f.kind === "number_or_empty") {
    return el("input", { type: "number", id, min: f.min, max: f.max, step: "any", value: v ?? "",
      placeholder: f.kind === "number_or_empty" ? "learn it" : null, oninput: dirty, style: "width:9em" });
  }
  if (f.kind === "multi") {
    return el("div", {}, ...Object.entries(f.options).map(([k, label]) =>
      el("label", { style: "display:block" },
        el("input", { type: "checkbox", id: id + "-" + k, checked: (v || []).includes(k) ? "checked" : null, onchange: dirty }),
        " " + label)));
  }
  if (f.kind === "text") return el("input", { type: "text", id, value: v ?? "", oninput: dirty, size: 34 });
  if (f.kind === "emails") return el("textarea", { id, rows: 3, cols: 32, oninput: dirty }, (v || []).join("\n"));
  return el("span", {}, String(v));
}

function changes() {
  const out = {};
  for (const f of fields) {
    const v = readField(f);
    if (JSON.stringify(v) !== JSON.stringify(saved[f.key])) out[f.key] = v;
  }
  return out;
}

function dirty() {
  const n = Object.keys(changes()).length;
  document.getElementById("save").disabled = n === 0;
  document.getElementById("reset").disabled = n === 0;
}

function render(writable) {
  const groups = {};
  for (const f of fields) (groups[f.group] = groups[f.group] || []).push(f);
  document.getElementById("form").replaceChildren(...Object.entries(groups).map(([name, list]) =>
    el("div", { class: "card" }, el("h2", {}, name),
      ...list.map(f => el("div", { style: "margin:8px 0" },
        el("div", {}, el("strong", {}, f.label)),
        input(f, saved[f.key]),
        f.help ? el("div", { class: "muted" }, f.help) : null)))));
  if (!writable) for (const n of document.querySelectorAll("#form input, #form select, #form textarea")) n.disabled = true;
  dirty();
}

function say(text, kind) {
  document.getElementById("msg").replaceChildren(text ? el("div", { class: "notice " + kind }, text) : "");
}

async function load() {
  try {
    const r = await api("/api/settings");
    fields = r.fields; saved = r.values;
    document.getElementById("where").textContent = "Saved in " + r.file;
    render(r.writable);
    if (!r.writable) say("Settings cannot be changed in this mode.", "warn");
  } catch (e) { say("Could not load settings: " + e.message, "bad"); }
}

document.getElementById("save").addEventListener("click", async (ev) => {
  const c = changes();
  if (c["ai.provider"] && c["ai.provider"] !== "local") {
    if (!confirm("With an online AI service, the camera pictures and printer logs for each check are sent to that company. Continue?")) return;
  }
  if (c["autopause.enabled"] === true || (c["camera.keepalive"] && c["camera.keepalive"] !== "off")) {
    // Both of these let Guardian send something to the printer. Say so plainly.
    if (!confirm("This lets Guardian send a request to the printer (turn the camera on, or pause a print). Save?")) return;
  }
  ev.target.disabled = true;
  try {
    const r = await api("/api/settings", { method: "PUT", body: JSON.stringify({ changes: c }) });
    saved = r.values; render(true);
    say(r.changed.length ? "Saved: " + r.changed.join(", ") : "Nothing changed.", "info");
    loadAi();
    // Every save with local AI on: if Ollama or the model is missing, offer to get it (it only asks then).
    if (r.values["ai.enabled"] === true) afterAiTurnedOn();
  } catch (e) { say("Not saved: " + e.message, "bad"); dirty(); }
});
document.getElementById("reset").addEventListener("click", () => { render(true); say("", ""); });

function gb(bytes) { return (bytes / 1e9).toFixed(1) + " GB"; }

async function loadAi() {
  const box = document.getElementById("ai-box");
  let r;
  try { r = await api("/api/ai/status"); }
  catch (e) { box.replaceChildren(el("p", { class: "muted" }, "Could not check the AI: " + e.message)); return; }
  const parts = [];
  if (r.provider && r.provider !== "local") {
    const name = r.provider === "anthropic" ? "Anthropic (Claude)" : "an OpenAI-compatible service" + (r.remote_url ? " at " + r.remote_url : "");
    parts.push(el("div", { class: "notice warn" }, "Online AI: the camera pictures and printer logs for each check are sent to " + name +
      ". Use local AI (Ollama) if you want everything to stay on your network."));
    parts.push(el("p", {}, "API key (" + r.api_key_env + "): ", r.api_key_found ? badge("found", "ok") : badge("missing", "bad")));
    if (!r.api_key_found) parts.push(el("p", { class: "muted" }, "Set the environment variable " + r.api_key_env +
      " to your key, or add a line " + r.api_key_env + "=your-key to the .env file next to config.yaml, then restart Guardian."));
    parts.push(el("p", {}, "Model: " + (r.model || "not set") + ". Connection: " + (r.remote_message || "not checked yet")));
    parts.push(el("p", { class: "muted" }, r.usable ? "The AI is ready." : !r.enabled ? "Turn on \"Use AI\" above to use it." : "Not ready yet. See above."));
    box.replaceChildren(...parts);
    return;
  }
  const inst = r.install || { plan: {}, state: {} };
  const ist = inst.state || {};
  if (ist.running || ist.error || (ist.done && !r.ollama_found)) {
    parts.push(el("div", { class: "notice " + (ist.error ? "bad" : "info") },
      ist.error ? "Installing Ollama did not work: " + ist.error : "Installing Ollama: " + (ist.step || "starting"),
      ist.output && ist.output.length ? el("pre", { style: "white-space:pre-wrap;font-size:12px;margin:6px 0 0" }, ist.output.slice(-4).join("\n")) : null));
    if (ist.running) setTimeout(loadAi, 2000);
  }
  if (!r.ollama_found) {
    parts.push(el("p", {}, "Ollama was not found at " + r.ollama_url + ". Ollama is a free program that runs AI models on your own computer. Nothing is sent to the internet."));
    if (inst.plan.can_install && !ist.running) {
      parts.push(el("p", {}, el("button", { type: "button", class: "primary", onclick: () => installOllama(false) }, "Install Ollama for me"),
        " ", el("span", { class: "muted" }, inst.plan.manual || "")));
    } else if (!inst.plan.can_install) {
      parts.push(el("p", {}, inst.plan.manual || ""), el("p", {}, el("a", { href: r.install_help, target: "_blank", rel: "noopener" }, r.install_help)));
    }
  } else {
    if (ist.done && !r.model_ready && !offeredModel) {
      offeredModel = true;
      const label = r.models[r.model] || r.model;
      setTimeout(() => download(r.model, label), 100);
    }
    parts.push(el("p", {}, "Ollama " + r.ollama_version + " found at " + r.ollama_url + "."));
    const list = Object.entries(r.models).map(([name, label]) => {
      const size = r.installed[name] ?? r.installed[name + ":latest"];
      const chosen = name === r.model;
      return el("div", { class: "row", style: "margin:6px 0" },
        el("span", {}, label + (chosen ? " (chosen)" : "")),
        size ? badge("downloaded, " + gb(size), "ok") : badge("not downloaded", "info"),
        size ? el("button", { type: "button", onclick: () => removeModel(name) }, "Remove") :
          el("button", { type: "button", class: chosen ? "primary" : "", disabled: r.download.running ? "disabled" : null,
            onclick: () => download(name, label) }, "Download"));
    });
    parts.push(...list);
    const d = r.download;
    if (d.running || d.error || d.done) {
      const txt = d.running ? "Downloading " + d.model + ": " + (d.percent != null ? d.percent + "% of " + gb(d.total_bytes) : (d.status || "starting")) :
        d.error ? "Download of " + d.model + " failed: " + d.error : "Download of " + d.model + " finished.";
      parts.push(el("div", { class: "notice " + (d.error ? "bad" : "info") }, txt));
      if (d.running) setTimeout(loadAi, 2000);
    }
    parts.push(el("p", { class: "muted" }, r.usable ? "The AI is ready." :
      !r.enabled ? "Turn on \"Use AI\" above to use it." : "Download the chosen model to start using the AI."));
  }
  box.replaceChildren(...parts);
}

let offeredModel = false;

async function installOllama(fromToggle) {
  const ask = (fromToggle ? "Local AI needs Ollama, and it is not installed on this computer. " : "") +
    "Install Ollama for you now? It is a free program from ollama.com. It is a large download (around 1 GB or more) " +
    "and takes a few minutes. After that you pick an AI model to download.";
  if (!confirm(ask)) return;
  try { await api("/api/ai/install", { method: "POST" }); }
  catch (e) { alert("Could not start the install: " + e.message); }
  loadAi();
}

async function afterAiTurnedOn() {
  let r;
  try { r = await api("/api/ai/status"); } catch (e) { return; }
  if ((r.download || {}).running || (((r.install || {}).state) || {}).running) return;  // already on its way
  if (r.provider && r.provider !== "local") {
    if (!r.api_key_found) alert("The online AI needs your API key. Put it in the environment variable " + r.api_key_env +
      " or in the .env file next to config.yaml, then restart Guardian.");
    return;
  }
  if (r.ollama_found) {
    if (!r.model_ready) download(r.model, r.models[r.model] || r.model);
    return;
  }
  const plan = (r.install || {}).plan || {};
  if (plan.can_install) installOllama(true);
  else alert("Local AI needs Ollama, and it is not installed. " + (plan.manual || "See https://ollama.com/download"));
}

async function download(name, label) {
  if (!confirm("Download " + label + "? It uses that much disk space. You can remove it here later.")) return;
  try { await api("/api/ai/download", { method: "POST", body: JSON.stringify({ model: name }) }); }
  catch (e) { alert("Could not start the download: " + e.message); }
  loadAi();
}

async function removeModel(name) {
  if (!confirm("Remove " + name + " from this computer? You can download it again later.")) return;
  try { await api("/api/ai/model", { method: "DELETE", body: JSON.stringify({ model: name }) }); }
  catch (e) { alert("Could not remove it: " + e.message); }
  loadAi();
}

document.getElementById("selftest").addEventListener("click", async (ev) => {
  ev.target.disabled = true;
  const out = document.getElementById("selftest-out");
  out.replaceChildren(el("li", { class: "muted" }, "Running..."));
  try {
    const r = await api("/api/selftest");
    out.replaceChildren(...r.checks.map(c => el("li", {}, badge(c.ok ? "ok" : "problem", c.ok ? "ok" : "bad"), " " + c.name + ": " + c.detail)));
  } catch (e) { out.replaceChildren(el("li", {}, "Could not run: " + e.message)); }
  ev.target.disabled = false;
});

load();
loadAi();
