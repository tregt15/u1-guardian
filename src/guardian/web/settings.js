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
  if (c["autopause.enabled"] === true || (c["camera.keepalive"] && c["camera.keepalive"] !== "off")) {
    // Both of these let Guardian send something to the printer. Say so plainly.
    if (!confirm("This lets Guardian send a request to the printer (turn the camera on, or pause a print). Save?")) return;
  }
  ev.target.disabled = true;
  try {
    const r = await api("/api/settings", { method: "PUT", body: JSON.stringify({ changes: c }) });
    saved = r.values; render(true);
    say(r.changed.length ? "Saved: " + r.changed.join(", ") : "Nothing changed.", "info");
  } catch (e) { say("Not saved: " + e.message, "bad"); dirty(); }
});
document.getElementById("reset").addEventListener("click", () => { render(true); say("", ""); });

load();
