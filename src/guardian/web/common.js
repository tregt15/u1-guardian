// Shared helpers. No framework, no build step, nothing loaded from the internet.
// All text from the server goes in as plain text nodes, never as markup.

const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage may be blocked */ } },
};

function token() { return store.get("guardian_token") || ""; }
function tokenQuery(sep) { return token() ? (sep || "?") + "token=" + encodeURIComponent(token()) : ""; }

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

const SVG_NS = "http://www.w3.org/2000/svg";
const SVG_TAGS = new Set(["svg", "use", "circle", "path", "line", "text", "g", "rect", "ellipse"]);

function el(tag, attrs, ...kids) {
  const n = SVG_TAGS.has(tag) ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.setAttribute("class", v);
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
}

// An icon from /static/icons.svg (the Halo design icons).
function ic(name, cls) {
  return el("svg", { class: "ic" + (cls ? " " + cls : ""), "aria-hidden": "true" },
    el("use", { href: "/static/icons.svg#" + name }));
}

// ---- time ----
function parseTime(iso) { const d = new Date(iso); return isNaN(d) ? null : d; }
function clock(d, withSec) {
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: withSec ? "2-digit" : undefined });
}
// "Today 9:12 AM", "Yesterday 4:40 PM", "Mon 7:56 PM", or a date for older ones.
function friendlyTime(iso, withSec) {
  const d = parseTime(iso);
  if (!d) return "unknown";
  const now = new Date();
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  const t = clock(d, withSec);
  if (diff === 0) return "Today " + t;
  if (diff === 1) return "Yesterday " + t;
  if (diff > 1 && diff < 7) return d.toLocaleDateString([], { weekday: "short" }) + " " + t;
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: diff > 300 ? "numeric" : undefined }) + " " + t;
}
function ago(iso) {
  const d = parseTime(iso);
  if (!d) return "never";
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 90) return Math.max(0, Math.round(s)) + " s ago";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 86400 * 2) return Math.round(s / 3600) + " h ago";
  return friendlyTime(iso);
}
function secs(s, digits) { return (s === null || s === undefined) ? "unknown" : Number(s).toFixed(digits ?? 1) + " s"; }
function mmss(s) { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }

function listen(onEvent) {
  // Server-sent events for live updates. EventSource cannot send headers, so the token goes in the URL.
  try {
    const es = new EventSource("/api/events/stream" + tokenQuery());
    for (const t of ["incident.created", "incident.merged", "incident.finalized", "printer.connected", "printer.disconnected"]) {
      es.addEventListener(t, onEvent);
    }
    return es;
  } catch (e) { return null; }
}
