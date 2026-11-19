// Tool changes (Halo design, ToolChanges.dc.html).

const main = Shell.start("toolchanges");
const RES = {
  ok: { tone: "t-ok", cls: "", icon: "i-check", label: "OK" },
  park_only: { tone: "t-warn", cls: "", icon: "i-alert", label: "Park only" },
  timeout: { tone: "t-crit", cls: "solid", icon: "i-alert", label: "Stuck" },
  wrong_tool: { tone: "t-warn", cls: "", icon: "i-alert", label: "Wrong tool" },
  bad_state: { tone: "t-warn", cls: "", icon: "i-alert", label: "Odd tool state" },
  error: { tone: "t-crit", cls: "", icon: "i-alert", label: "Printer error" },
  interrupted: { tone: "t-muted", cls: "dashed", icon: "i-off", label: "Interrupted" },
  open: { tone: "t-info", cls: "", icon: "i-swap", label: "In progress" },
};
const PLAIN = {
  park_only: "The old tool parked, but no new tool was picked up.", timeout: "The change did not finish in time.",
  wrong_tool: "The printer reported a different tool than the one asked for.", bad_state: "A tool reported a state Guardian did not expect.",
  error: "The printer reported an error during the change.", interrupted: "Guardian lost track of this change.",
};
let summary = null, failures = null, failPrint = null, rows = [], problemsOnly = false, pair = "", open = new Set();

const tiles = el("section", { class: "tiles", "aria-label": "Summary" });
const countsCard = el("section", { class: "card", "aria-labelledby": "res-h" });
const chartCard = el("section", { class: "card", "aria-labelledby": "ch-h" });
const failCard = el("section", { class: "card", "aria-labelledby": "fail-h" });
const logCard = el("section", { class: "card", "aria-labelledby": "log-h" });
main.append(el("div", { class: "page-head" }, el("div", {}, el("h1", { class: "h-page" }, "Tool changes"),
  el("p", { class: "h-sub" }, "The U1 swaps toolheads many times in one print. Guardian times and checks every swap."))),
tiles, countsCard, chartCard, failCard, logCard);

function tile(k, v, sub) { return el("div", { class: "card tile" }, el("div", { class: "t-k" }, k), el("div", { class: "t-v" }, v), el("div", { class: "t-s" }, sub)); }
function pairName(p) { return tool(p.previous_tool) + " to " + tool(p.requested_tool); }

function drawTop(s) {
  const t = (s && s.toolchanger) || {};
  const sm = summary || {};
  const slow = sm.slowest_recent;
  tiles.replaceChildren(
    tile("Changes in the last print", sm.total != null ? String(sm.total) : "0", sm.print_filename || "No print yet"),
    tile("Typical time", t.median_ms != null ? (t.median_ms / 1000).toFixed(1) + " s" : "Not measured", "Middle value of " + (t.good_changes_measured || 0) + " good changes"),
    tile("Counts as stuck after", t.timeout_s != null ? t.timeout_s + " s" : "unknown",
      t.timeout_source === "learned" ? "Learned from this printer" : t.timeout_source === "fixed" ? "Set in Settings" : "Default until enough changes are measured"),
    tile("Slowest recent change", slow ? (slow.duration_ms / 1000).toFixed(1) + " s" : "None yet", slow ? pairName(slow) + " at " + clock(new Date(slow.started_utc)) : ""));
  const order = ["ok", "park_only", "timeout", "wrong_tool", "bad_state", "error", "interrupted"];
  const counts = sm.counts || {};
  countsCard.replaceChildren(
    el("div", { class: "card-h" }, el("h2", { class: "card-title", id: "res-h" }, "Results of the " + (sm.total || 0) + " changes in the last print")),
    el("div", { class: "card-b" }, el("div", { class: "row gap-s" }, ...order.map(k => {
      const r = RES[k];
      return el("span", { class: "badge " + r.tone + " " + r.cls }, ic(r.icon), r.label + " ", el("strong", {}, String(counts[k] || 0)));
    }))));
}

function drawChart(s) {
  const t = (s && s.toolchanger) || {};
  const pts = ((summary || {}).points || []).filter(p => p.duration_ms && (!pair || pairName(p) === pair));
  const pairs = {};
  for (const p of (summary || {}).points || []) pairs[pairName(p)] = (pairs[pairName(p)] || 0) + 1;
  const top = Object.entries(pairs).sort((a, b) => b[1] - a[1]).slice(0, 3).map(x => x[0]);
  const seg = el("div", { class: "seg right", role: "radiogroup", "aria-label": "Tool pair" }, ...["", ...top].map(v => {
    const b = el("button", { type: "button", role: "radio", "aria-checked": v === pair ? "true" : "false" }, v || "All pairs");
    b.addEventListener("click", () => { pair = v; drawChart(Shell.status); });
    return b;
  }));
  const limit = t.timeout_s || 15, median = (t.median_ms || 0) / 1000;
  const W = 800, L = 48, R = 786, TOP = 30.4, BOT = 216;
  const k = (BOT - TOP) / limit;
  const y = (sec) => BOT - k * Math.min(sec, limit * 1.05);
  const svg = el("svg", { class: "chart", viewBox: "0 0 " + W + " 250", role: "img",
    "aria-label": "Dot chart of how long each tool change took. " + pts.length + " changes." });
  for (const frac of [0, 1 / 3, 2 / 3, 1]) {
    const sec = limit * frac;
    svg.append(el("line", { class: "gl", x1: L, x2: R, y1: y(sec).toFixed(1), y2: y(sec).toFixed(1) }),
      el("text", { x: L - 8, y: (y(sec) + 4).toFixed(1), "text-anchor": "end" }, Math.round(sec) + " s"));
  }
  if (median) svg.append(el("line", { class: "med", x1: L, x2: R, y1: y(median).toFixed(1), y2: y(median).toFixed(1) }),
    el("text", { x: R, y: (y(median) - 7).toFixed(1), "text-anchor": "end" }, "Typical " + median.toFixed(1) + " s"));
  svg.append(el("line", { class: "lim", x1: L, x2: R, y1: TOP, y2: TOP }),
    el("text", { x: R, y: TOP - 8, "text-anchor": "end" }, "Counts as stuck after " + limit + " s"));
  let ok = "", slow = "", bad = "";
  const n = pts.length;
  pts.forEach((p, i) => {
    const X = (L + 14 + (R - L - 28) * (n > 1 ? i / (n - 1) : 0.5)).toFixed(1);
    const sec = p.duration_ms / 1000, Y = y(sec);
    if (p.result !== "ok") bad += "M" + X + " " + (Y - 8).toFixed(1) + "l8 8l-8 8l-8-8z";
    else if (median && sec > median * 1.3 && sec - median > 0.8) slow += "M" + X + " " + Y.toFixed(1) + "h.01";
    else ok += "M" + X + " " + Y.toFixed(1) + "h.01";
  });
  svg.append(el("path", { class: "p-ok", d: ok || "M0 0" }), el("path", { class: "p-slow", d: slow || "M0 0" }), el("path", { class: "p-bad", d: bad || "M0 0" }));
  if (n) {
    const label = (i, anchor) => el("text", { x: (L + 14 + (R - L - 28) * (n > 1 ? i / (n - 1) : 0.5)).toFixed(1), y: 240, "text-anchor": anchor }, clock(new Date(pts[i].started_utc)));
    svg.append(label(0, "start"), label(n - 1, "end"));
    if (n > 2) svg.append(label(Math.floor(n / 2), "middle"));
  }
  const dot = (style, shape) => el("svg", { width: 14, height: 14, viewBox: "0 0 14 14", "aria-hidden": "true" }, shape === "d"
    ? el("path", { d: "M7 1l6 6-6 6-6-6z", style }) : el("circle", { cx: 7, cy: 7, r: shape, style }));
  chartCard.replaceChildren(
    el("div", { class: "card-h" }, el("h2", { class: "card-title", id: "ch-h" }, "How long each change took"), top.length > 1 ? seg : null),
    el("div", { class: "card-b" }, n ? svg : el("div", { class: "empty" }, el("div", { class: "strong" }, "No tool changes yet"),
      el("div", {}, "The chart fills in during your next print.")),
    el("div", { class: "row small muted", style: "margin-top:8px;gap:6px 20px" },
      el("span", { class: "row nw gap-s" }, dot("fill:var(--primary);opacity:.75", 3.5), "Normal"),
      el("span", { class: "row nw gap-s" }, dot("fill:var(--warn-solid)", 5), "Slower than usual"),
      el("span", { class: "row nw gap-s" }, dot("fill:var(--crit-solid)", "d"), "Problem")),
    el("p", { class: "note-t", style: "margin-top:8px" }, "Pick a pair to see if one tool is getting slower. A rising line of dots is an early warning.")));
}

// Failed changes: by tool, by print, and over time.
function ago2(iso) { return friendlyTime(iso, true); }
function drawFailures() {
  const f = failures;
  if (!f) { failCard.replaceChildren(); return; }
  const head = el("div", { class: "card-h" }, el("h2", { class: "card-title", id: "fail-h" }, "Failed changes"),
    el("span", { class: "small muted" }, f.total_failed + " failed of " + f.total_changes + " recorded"));
  if (!f.total_failed) {
    failCard.replaceChildren(head, el("div", { class: "card-b" }, el("div", { class: "empty" },
      el("div", { class: "strong" }, "No failed changes"), el("div", {}, "Every recorded change went fine."))));
    return;
  }
  // Prints that had failures, newest first, for the picker.
  const bad = f.prints.filter(p => p.failed);
  const names = bad.slice(0, 3).map(p => p.print_filename);
  // Start on the newest print that had failures (so a cluster is not squeezed by an old one). "All prints" is one click away.
  if (failPrint === null) failPrint = names[0] || "";
  else if (failPrint && !names.includes(failPrint)) failPrint = "";
  const seg = el("div", { class: "seg right", role: "radiogroup", "aria-label": "Print" }, ...["", ...names].map(v => {
    const b = el("button", { type: "button", role: "radio", "aria-checked": v === failPrint ? "true" : "false" },
      v ? (v.length > 26 ? v.slice(0, 24) + "..." : v) : "All prints");
    b.addEventListener("click", () => { failPrint = v; drawFailures(); });
    return b;
  }));
  // One tile per tool.
  const toolTiles = el("div", { class: "tiles", style: "margin-bottom:16px" }, ...f.tools.filter(t => t.tool !== "unknown").map(t =>
    tile(tool(t.tool), t.failed + " failed", t.changes ? t.percent + "% of " + t.changes + " changes that used it" : "No changes")));
  // Lane chart: one row per tool, one diamond per failure, left to right in time.
  const pts = f.points.filter(p => !failPrint || p.print_filename === failPrint);
  const lanes = [...new Set(f.tools.map(t => t.tool))].filter(t => t !== "unknown").sort();
  const W = 800, L = 48, R = 786, TOP = 18, ROW = 40, H = TOP + ROW * lanes.length + 30;
  const t0 = pts.length ? new Date(pts[0].started_utc).getTime() : 0;
  const t1 = pts.length ? new Date(pts[pts.length - 1].started_utc).getTime() : 0;
  const X = (iso) => (L + 14 + (R - L - 28) * (t1 > t0 ? (new Date(iso).getTime() - t0) / (t1 - t0) : 0.5)).toFixed(1);
  const svg = el("svg", { class: "chart", viewBox: "0 0 " + W + " " + H, role: "img",
    "aria-label": "Failed tool changes over time, one row per tool. " + pts.length + " failures." });
  lanes.forEach((t, i) => {
    const y = TOP + ROW * i + ROW / 2;
    svg.append(el("line", { class: "gl", x1: L, x2: R, y1: y, y2: y }), el("text", { x: L - 8, y: y + 4, "text-anchor": "end" }, tool(t)));
  });
  let d = "";
  for (const p of pts) {
    const i = lanes.indexOf(p.tool); if (i < 0) continue;
    const y = TOP + ROW * i + ROW / 2, x = X(p.started_utc);
    d += "M" + x + " " + (y - 8) + "l8 8l-8 8l-8-8z";
  }
  svg.append(el("path", { class: "p-bad", d: d || "M0 0" }));
  if (pts.length) {
    const lab = (iso, a, x) => el("text", { x, y: H - 8, "text-anchor": a }, friendlyTime(iso, true));
    svg.append(lab(pts[0].started_utc, "start", L), lab(pts[pts.length - 1].started_utc, "end", R));
  }
  // Table: one row per print.
  const th = (c, t) => el("th", { style: "text-align:left;padding:6px 10px;font-weight:600" }, t);
  const td = (t) => el("td", { style: "padding:6px 10px;vertical-align:top" }, t);
  const rows2 = f.prints.filter(p => p.failed && (!failPrint || p.print_filename === failPrint)).map(p => el("tr", {},
    td(el("span", { class: "strong" }, p.print_filename)), td(ago2(p.last_utc)),
    td(String(p.failed) + " of " + p.changes), td(p.one_in ? "1 in " + p.one_in : ""),
    td(p.minutes_between ? "about every " + p.minutes_between + " min" : p.failed_per_hour ? p.failed_per_hour + " per hour" : "once"),
    td(Object.entries(p.by_tool).sort().map(([t, n]) => tool(t) + " x" + n).join(", "))));
  failCard.replaceChildren(head,
    el("div", { class: "card-b" }, toolTiles,
      el("div", { class: "row", style: "justify-content:space-between;margin-bottom:8px" },
        el("div", { class: "strong" }, "When they happened"), names.length > 1 ? seg : null),
      pts.length ? svg : el("div", { class: "empty" }, el("div", { class: "strong" }, "No failures for this print")),
      el("div", { class: "strong", style: "margin:16px 0 6px" }, "By print"),
      el("div", { style: "overflow-x:auto" }, el("table", { class: "small", style: "width:100%;border-collapse:collapse" },
        el("thead", {}, el("tr", {}, th(0, "Print"), th(0, "Last change"), th(0, "Failed"), th(0, "How often"), th(0, "Pace"), th(0, "Tools"))),
        el("tbody", {}, ...rows2))),
      el("p", { class: "note-t", style: "margin-top:8px" },
        "A problem counts for the tool the printer names, like the one that failed to park. A rising pace in one print, or one tool failing again and again, points at the printer or that tool's dock, not the G-code.")));
}

function stepText(st) {
  const v = typeof st.value === "string" ? st.value : JSON.stringify(st.value);
  const m = /^(extruder\d*)\.(\w+)$/.exec(st.what);
  if (st.what === "start") return ["Change asked.", v.replace(/^\/\/ /, "")];
  if (st.what === "gcode") return ["Printer said:", v.replace(/^\/\/ /, "")];
  if (st.what === "toolhead.extruder") return ["Active tool.", "The printer now reports " + tool(v) + "."];
  if (m) return [tool(m[1]) + " " + m[2].replace(/_/g, " ") + ":", v];
  if (st.what === "timeout") return ["Timed out.", v];
  if (st.what === "error") return ["Printer error.", v];
  return [st.what + ":", v];
}

function drawLog() {
  const head = el("div", { class: "trow head", "aria-hidden": "true" }, el("div", { class: "c-tm" }, "Time"), el("div", { class: "c-tools" }, "Tools"),
    el("div", { class: "c-dur" }, "Took"), el("div", { class: "c-res" }, "Result"), el("div", { class: "c-err" }, "What the printer said"), el("div", { class: "c-inc" }, "Incident"));
  const median = ((Shell.status || {}).toolchanger || {}).median_ms || 0;
  const list = [head];
  for (const r of rows) {
    const res = RES[r.result || "open"] || RES.open;
    const isOpen = open.has(r.id);
    const steps = Array.isArray(r.transitions) ? r.transitions : [];
    const slow = r.result === "ok" && median && r.duration_ms > median * 1.3 && r.duration_ms - median > 800;
    const exp = el("button", { type: "button", class: "exp", "aria-expanded": isOpen ? "true" : "false" }, ic(isOpen ? "i-chev-d" : "i-chev-r"), friendlyTime(r.started_utc, true));
    exp.addEventListener("click", () => { isOpen ? open.delete(r.id) : open.add(r.id); drawLog(); });
    list.push(el("div", { class: "trow" + (isOpen ? " open" : "") },
      el("div", { class: "c-tm" }, exp),
      el("div", { class: "c-tools" }, tool(r.previous_tool) + " to " + tool(r.requested_tool)),
      el("div", { class: "c-dur" }, r.duration_ms != null ? (r.duration_ms / 1000).toFixed(1) + " s" : "unknown",
        slow ? el("div", { class: "tiny muted", style: "font-family:var(--font);font-weight:500" }, "Slower than usual") : null),
      el("div", { class: "c-res" }, el("span", { class: "badge " + res.tone + " " + res.cls }, ic(res.icon), res.label)),
      el("div", { class: "c-err" }, r.result && r.result !== "ok" ? [el("span", { class: "small" }, PLAIN[r.result] || ""), r.error ? el("span", { class: "raw" }, r.error) : null] : null),
      el("div", { class: "c-inc" }, r.incident_uid ? el("a", { class: "link-btn small", href: "/incident?uid=" + encodeURIComponent(r.incident_uid) }, "View incident", ic("i-chev-r", "sm")) : null)));
    if (isOpen) list.push(el("div", { class: "steps", role: "group", "aria-label": "Steps of this change" },
      el("div", { class: "sub-h" }, "Steps of this change"),
      ...(steps.length ? steps : [{ t_ms: 0, what: "none", value: "No steps were recorded." }]).map(st => {
        const [name, text] = stepText(st);
        const bad = st.what === "timeout" || st.what === "error";
        return el("div", { class: "step " + (bad ? "t-crit" : "t-ok") }, el("span", { class: "s-t" }, (st.t_ms / 1000).toFixed(1) + " s"),
          el("span", { class: "s-d" }), el("div", {}, el("strong", {}, name), " ", el("span", { class: "muted" }, text)));
      })));
  }
  const sw = el("button", { type: "button", class: "sw", role: "switch", "aria-checked": problemsOnly ? "true" : "false" },
    el("span", { class: "track" }, el("span", { class: "knob" })), el("span", { class: "sw-t", style: "min-width:0" }, "Problems only: " + (problemsOnly ? "On" : "Off")));
  sw.addEventListener("click", () => { problemsOnly = !problemsOnly; loadRows(); });
  logCard.replaceChildren(
    el("div", { class: "card-h", style: "padding-bottom:12px" }, el("h2", { class: "card-title", id: "log-h" }, "Change log"),
      el("span", { class: "small muted" }, problemsOnly ? "Showing " + rows.length + " problem changes" : "Showing the last " + rows.length), el("div", { class: "right" }, sw)),
    rows.length ? el("div", { class: "list" }, ...list)
      : el("div", { class: "card-b" }, el("div", { class: "empty" }, el("div", { class: "strong" }, problemsOnly ? "No problem changes" : "No tool changes yet"),
        el("div", {}, problemsOnly ? "Every recorded change went fine." : "They show up here during your next print."))));
}

async function loadFailures() {
  try { failures = await api("/api/toolchanges/failures"); } catch (e) { failures = null; }
  drawFailures();
}
async function loadRows() {
  try { rows = (await api("/api/toolchanges?limit=100" + (problemsOnly ? "&problems=true" : ""))).toolchanges; } catch (e) { rows = []; }
  drawLog();
}
async function loadSummary() {
  try { summary = await api("/api/toolchanges/summary"); } catch (e) { summary = null; }
  drawTop(Shell.status);
  drawChart(Shell.status);
}

Shell.onStatus((s) => drawTop(s));
loadSummary();
loadFailures();
loadRows();
setInterval(() => { loadSummary(); loadFailures(); if (!open.size) loadRows(); }, 20000);
