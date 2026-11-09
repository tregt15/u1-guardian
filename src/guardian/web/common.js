// Shared helpers. No framework, no build step.
// All text from the server goes in as plain text nodes, never as markup.

const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage may be blocked */ } },
};

function token() { return store.get("guardian_token") || ""; }

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (token()) headers["X-Guardian-Token"] = token();
  if (opts.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, Object.assign({}, opts, { headers }));
  if (resp.status === 401) {
    const t = prompt("This Guardian needs an access token:");
    if (t) { store.set("guardian_token", t.trim()); return api(path, opts); }
    throw new Error("access token needed");
  }
  if (!resp.ok) {
    let msg = resp.status + " " + resp.statusText;
    try { const j = await resp.json(); if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (e) {}
    throw new Error(msg);
  }
  return resp.json();
}

function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
}

function badge(text, kind) { return el("span", { class: "badge b-" + kind }, text); }

const STATUS_KIND = { complete: "ok", recording: "info", partial: "warn", missing: "bad", failed: "bad" };
const SEVERITY_KIND = { info: "info", warning: "warn", critical: "bad" };

function localTime(iso) {
  if (!iso) return "unknown";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString() + " (" + iso.replace("T", " ").replace("Z", " UTC") + ")";
}
function shortTime(iso) { const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(); }
function secs(s) { return (s === null || s === undefined) ? "?" : Number(s).toFixed(1) + " s"; }
function mmss(s) { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }
function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  return s < 90 ? Math.round(s) + " s ago" : s < 5400 ? Math.round(s / 60) + " min ago" : Math.round(s / 3600) + " h ago";
}

function listen(onEvent) {
  // Server-sent events for live updates. EventSource cannot send headers, so the token goes in the URL.
  try {
    const url = "/api/events/stream" + (token() ? "?token=" + encodeURIComponent(token()) : "");
    const es = new EventSource(url);
    for (const t of ["incident.created", "incident.merged", "incident.finalized", "printer.connected", "printer.disconnected"]) {
      es.addEventListener(t, onEvent);
    }
    return es;
  } catch (e) { return null; }
}
