// Dashboard (Halo design, Dashboard.dc.html). Answers "is my print OK?" at a glance.

const main = Shell.start("dashboard");
const parts = {
  urgent: el("div"), top: el("section", { class: "dash-top" }), paused: el("div"),
  cards: el("section", { class: "cards", "aria-label": "Status" }), recent: el("div"),
};
main.append(parts.urgent, parts.top, parts.paused, parts.cards, parts.recent);

let extra = { incidents: [], latest: null, latestUid: null, mails: [], tcProblem: null };
let camImg = null, camStarted = 0;
const checklist = new Map();

function heroState(s) {
  const p = s.printer || {};
  if (!p.connected) return "offline";
  if (p.print_state === "printing") return "printing";
  if (p.print_state === "paused") return "paused";
  return "idle";
}

// ---- auto pause countdown ----
function drawUrgent(s) {
  const ap = s.autopause || {};
  if (ap.pending_in_s == null) { parts.urgent.replaceChildren(); return; }
  parts.urgent.replaceChildren(el("section", { class: "urgent", role: "alert", "aria-labelledby": "ap-h" },
    el("div", { class: "u-count" }, el("div", {}, el("div", { class: "u-num" }, ap.pending_in_s), el("div", { class: "u-unit" }, "seconds"))),
    el("div", {}, el("h2", { id: "ap-h" }, "Guardian will pause the print in " + ap.pending_in_s + " s unless the printer pauses itself."),
      el("p", {}, el("strong", {}, "Reason: "), ap.pending_reason || "a problem was found")),
    el("div", { class: "u-act row" },
      el("button", { type: "button", class: "btn btn-secondary", onclick: async () => {
        try { await api("/api/autopause/cancel", { method: "POST" }); toast("This pause was cancelled.", "t-ok"); Shell.poll(); }
        catch (e) { toast(e.message, "t-crit"); }
      } }, "Cancel this pause"),
      el("button", { type: "button", class: "btn btn-danger", onclick: () => printerControl("pause").then(() => Shell.poll()) },
        ic("i-pause"), "Pause now"))));
}

// ---- hero ----
function ring(pct, tone, mid, sub, icon, dashed) {
  const R = 70, C = 2 * Math.PI * R, th = pct * 3.6 * Math.PI / 180;
  const svg = el("svg", { viewBox: "0 0 168 168", "aria-hidden": "true" },
    el("circle", { class: "r-bg", cx: 84, cy: 84, r: 70 }),
    el("circle", { class: "r-fg", cx: 84, cy: 84, r: 70, "stroke-dasharray": (C * pct / 100).toFixed(1) + " " + C.toFixed(1), style: "opacity:" + (pct > 0 ? 1 : 0) }),
    el("circle", { class: "r-bead", cx: (84 + R * Math.cos(th)).toFixed(1), cy: (84 + R * Math.sin(th)).toFixed(1), r: 8, style: "opacity:" + (pct > 0 ? 1 : 0) }),
    el("circle", { class: "r-dash", cx: 84, cy: 84, r: 70, style: "opacity:" + (dashed ? 1 : 0) }));
  return el("div", { class: "ring " + tone }, svg, el("div", { class: "r-mid" },
    icon ? ic(icon, "r-ic") : el("div", { class: "r-num" }, mid), el("div", { class: "r-sub" }, sub)));
}

function pills(s, state) {
  const c = s.camera || {}, a = s.ai || {}, ap = s.autopause || {}, t = s.toolchanger || {};
  const OK = (text) => ["", "t-ok", "i-check", text], OFF = (text) => ["off", "t-muted", "i-off", text];
  const BAD = (text, tone, icon) => ["bad", tone, icon || "i-alert", text];
  if (state === "offline") return [OFF("Printer events: not running"), OFF("Footage: not saving"), OFF("Tool changes: not running"),
    OFF("AI: no pictures"), OFF("Auto pause: cannot act")];
  const printing = state === "printing" || state === "paused";
  const out = [OK("Printer events")];
  if (c.mode !== "snapshot") out.push(c.running ? OK("Footage saving") : BAD("Camera stopped", "t-crit"));
  else if (!printing && c.keepalive !== "always") out.push(OFF("Footage: starts with a print"));
  else out.push(c.viewer_active ? OK(state === "paused" ? "Footage saved" : "Footage saving") : BAD("No camera pictures", "t-warn", "i-camera-off"));
  if (!t.enabled) out.push(OFF("Tool changes: not watched"));
  else if (!printing) out.push(OFF("Tool changes: no print"));
  else out.push((t.in_progress || []).length ? OK("Tool change " + t.in_progress[0]) : OK("Tool changes timed"));
  if (!a.enabled) out.push(OFF("AI: off"));
  else if (!a.usable) out.push(BAD("AI not ready", "t-ai", "i-info"));
  else out.push(OK(printing ? "AI checking" : "AI ready"));
  if (!ap.enabled) out.push(OFF("Auto pause: off"));
  else if (ap.pending_in_s != null) out.push(BAD("Auto pause armed", "t-warn"));
  else out.push(OK("Auto pause on"));
  return out;
}

function drawHero(s) {
  const p = s.printer || {}, state = heroState(s), ap = s.autopause || {};
  const pct = p.print_progress || 0;
  const words = { printing: "Printing", paused: "Paused", offline: "Not connected" };
  const idleWord = { complete: "Finished", cancelled: "Stopped", error: "Error" }[p.print_state] || "Idle";
  const word = words[state] || idleWord;
  const pl = pills(s, state);
  const bad = pl.filter(x => x[0] === "bad").length, running = pl.filter(x => x[0] === "").length;
  let sum, sumTone, sumIcon;
  if (state === "offline") [sum, sumTone, sumIcon] = ["Nothing is being checked or saved right now.", "t-crit", "i-alert"];
  else if (ap.pending_in_s != null) [sum, sumTone, sumIcon] = ["A problem was found. Guardian is about to pause the print.", "t-crit", "i-alert"];
  else if (state === "paused") [sum, sumTone, sumIcon] = ["The print is paused. Look at the printer before you resume.", "t-warn", "i-alert"];
  else if (state === "printing") [sum, sumTone, sumIcon] = bad ? [bad + " check" + (bad > 1 ? "s need" : " needs") + " a look. See below.", "t-warn", "i-alert"]
    : ["No problems found. " + running + " of " + pl.length + " checks are running.", "t-ok", "i-check"];
  else [sum, sumTone, sumIcon] = ["Guardian is watching the printer. It starts saving footage when a print begins.", "t-muted", "i-info"];
  const activeTool = state === "offline" ? null : p.active_toolhead;
  const toolText = state === "offline" ? "Toolhead unknown" : !activeTool ? "No toolhead in use"
    : state === "paused" ? "Stopped on " + tool(activeTool) : state === "printing" ? "Using " + tool(activeTool) : "Last used " + tool(activeTool);
  const ctl = (s.controls || {}).enabled && p.connected;
  const conn = p.connected ? badge("t-ok", "i-wifi", "Printer connected") : badge("t-crit", "i-wifi-off", "Printer not connected");
  const hero = el("section", { class: "card hero", "aria-labelledby": "hero-state" },
    state === "printing" || state === "paused"
      ? ring(pct, state === "paused" ? "rc-warn" : "rc-primary", pct + "%", state === "paused" ? "paused" : "done")
      : ring(0, "rc-muted", "", state === "offline" ? "No signal" : "Ready", state === "offline" ? "i-wifi-off" : "i-shield", state === "offline"),
    el("div", {}, conn,
      el("h1", { class: "hero-state", id: "hero-state" }, word),
      el("div", { class: "hero-file" }, state === "offline" ? "Guardian cannot see the printer, so it does not know what the printer is doing."
        : (state === "printing" || state === "paused") ? (p.print_filename || "unknown file") : "No print is running."),
      el("div", { class: "tools", role: "group", "aria-label": "Active toolhead" },
        ...[0, 1, 2, 3].map(i => el("span", { class: "tchip " + (tool(activeTool) === "T" + i ? (state === "paused" ? "hold" : state === "printing" ? "on" : "") : "") }, "T" + i)),
        el("span", { class: "small muted" }, toolText)),
      el("div", { class: "hero-sum " + sumTone }, ic(sumIcon), el("span", {}, sum)),
      el("div", { class: "checks" }, ...pl.map(([cls, tone, icon, text]) => el("span", { class: "pill-check " + cls + " " + tone }, ic(icon), text))),
      ctl && state === "printing" ? el("div", { class: "ctrl" },
        el("button", { type: "button", class: "btn btn-secondary", onclick: () => printerControl("pause").then(() => Shell.poll()) }, ic("i-pause"), "Pause"),
        el("button", { type: "button", class: "btn btn-danger-quiet", onclick: () => printerControl("cancel").then(() => Shell.poll()) }, ic("i-stop"), "Stop")) : null,
      ctl && state === "paused" ? el("div", { class: "ctrl" }, el("a", { class: "btn btn-primary", href: "#paused" }, "Review and resume")) : null));
  return hero;
}

// ---- live camera ----
function camWanted(s) {
  const c = s.camera || {}, p = s.printer || {};
  return p.connected && c.mode === "snapshot" && (["printing", "paused"].includes(p.print_state) || c.keepalive === "always");
}
function liveImg() {
  // One stream per page. The server ends a stream after 10 minutes, so open a new one before that.
  if (!camImg || Date.now() - camStarted > 9 * 60 * 1000) {
    camStarted = Date.now();
    camImg = el("img", { class: "live", alt: "Live picture from the printer camera", src: "/api/camera/stream?t=" + camStarted + tokenQuery("&") });
  }
  return camImg;
}
function camBox(s) {
  const c = s.camera || {}, p = s.printer || {};
  if (!camWanted(s)) {
    camImg = null;
    const text = !p.connected ? "No picture. Guardian cannot reach the printer."
      : c.mode !== "snapshot" ? "This camera source has no live view here."
        : c.keepalive === "off" ? "The camera only gives pictures while the Snapmaker app shows it. Auto camera is off."
          : "The camera is off while the printer is idle. Guardian turns it on when a print starts.";
    return el("div", { class: "cam" }, el("div", { class: "cam-off" }, ic(p.connected ? "i-camera-off" : "i-wifi-off"), el("p", {}, text)));
  }
  const fresh = c.viewer_active;
  return el("div", { class: "cam" }, liveImg(),
    el("span", { class: "cam-tag" + (fresh ? "" : " stale") }, el("span", { class: "dot" + (fresh ? " pulse" : "") }),
      fresh ? "Live" : "No new pictures"));
}
function camNote(s) {
  const c = s.camera || {};
  const auto = { off: "Off", printing: "During prints", always: "Always" }[c.keepalive] || "Off";
  if (!camWanted(s)) return "Auto camera is set to \"" + auto + "\".";
  return c.viewer_active ? "About 1 to 2 pictures a second. The last 30 s are kept in memory in case something goes wrong."
    : "No new pictures right now. The picture stays on the last one until the camera gives new ones.";
}
function drawTop(s) {
  const state = heroState(s);
  const nodes = [drawHero(s)];
  if (state !== "paused") {
    const c = s.camera || {};
    nodes.push(el("section", { class: "card", "aria-labelledby": "cam-h" },
      el("div", { class: "card-h" }, ic("i-camera"), el("h2", { class: "card-title", id: "cam-h" }, "Live camera"),
        el("span", { class: "small muted right" }, c.last_frame_utc ? "Picture " + ago(c.last_frame_utc) : "No picture yet")),
      el("div", { class: "card-b" }, camBox(s), el("p", { class: "cam-note" }, camNote(s)))));
  }
  parts.top.className = "dash-top" + (state === "paused" ? " solo" : "");
  parts.top.replaceChildren(...nodes);
}

// ---- paused panel ----
function drawPaused(s) {
  const p = s.printer || {};
  if (heroState(s) !== "paused") { parts.paused.replaceChildren(); return; }
  const inc = extra.incidents[0], d = extra.latest;
  const ai = d && d.findings && d.findings.ai && d.findings.ai.available ? d.findings.ai.answer : null;
  const trig = d && d.events ? d.events.find(e => e.is_trigger) : null;
  const why = (trig && trig.metadata && (trig.metadata.summary || trig.metadata.message)) || p.message || (inc ? label(inc.event_type) : "Not known.");
  const raw = p.message && p.message !== why ? p.message : (trig && trig.metadata && trig.metadata.message !== why ? trig.metadata.message : null);
  const items = ["Look at the printer. Is anything knocked over or tangled?", "Make sure the filament moves freely.",
    "Clear anything off the plate that should not be there."];
  const ctl = (s.controls || {}).enabled;
  const safe = ai ? String(ai.safe_to_resume || "unknown").toLowerCase() : null;
  parts.paused.replaceChildren(el("section", { class: "card paused", id: "paused", "aria-labelledby": "paused-h" },
    el("div", { class: "paused-h" }, ic("i-pause", "lg"), el("h2", { id: "paused-h" }, "Print is paused"),
      inc ? badge("t-warn", null, "Paused " + ago(inc.first_trigger_utc), "lg") : null,
      inc ? el("a", { class: "link-btn right", href: "/incident?uid=" + encodeURIComponent(inc.uid) }, "Open the full incident", ic("i-chev-r", "sm")) : null),
    el("div", { class: "paused-b" },
      el("div", {}, camBox(s), el("p", { class: "cam-note" }, "The footage from before the pause is saved in the incident.")),
      el("div", { class: "col gap-l" },
        el("div", {}, el("h3", { class: "sub-h" }, "Why it paused"), el("p", {}, why), raw ? el("span", { class: "raw" }, raw) : null),
        ai ? el("div", { class: "ai-box" },
          el("div", { class: "row", style: "margin-bottom:10px" }, badge("t-ai", "i-sparkle", "AI guess, can be wrong"),
            el("span", { class: "tiny muted" }, "Safe to resume?"),
            safe === "yes" ? badge("t-ok", "i-check", "Yes") : safe === "no" ? badge("t-crit", "i-x", "No") : badge("t-muted", "i-info", "Unknown")),
          el("h3", { class: "sub-h" }, "What to check"),
          el("ol", { class: "numlist" }, ...(Array.isArray(ai.what_to_check) ? ai.what_to_check : []).map(x => el("li", {}, String(x))))) : null,
        el("div", {}, el("h3", { class: "sub-h" }, "Before you resume"),
          el("div", { class: "todo", role: "group", "aria-label": "Checklist" }, ...items.map((t, i) => {
            const b = el("button", { type: "button", class: "chk", role: "checkbox", "aria-checked": checklist.get(i) ? "true" : "false" },
              el("span", { class: "box" }, ic("i-check")), el("span", { class: "c-t" }, t));
            b.addEventListener("click", () => { checklist.set(i, !checklist.get(i)); b.setAttribute("aria-checked", checklist.get(i) ? "true" : "false"); });
            return b;
          }))),
        ctl ? el("div", { class: "ctrl", style: "margin-top:0" },
          el("button", { type: "button", class: "btn btn-primary", onclick: () => printerControl("resume").then(() => Shell.poll()) }, ic("i-play"), "Resume print"),
          el("button", { type: "button", class: "btn btn-danger-quiet", onclick: () => printerControl("cancel").then(() => Shell.poll()) }, ic("i-stop"), "Stop print"))
          : el("p", { class: "small muted" }, "Resume or stop on the printer screen or in the app. Printer controls are off in Settings.")))));
}

// ---- status cards ----
const TC_RESULT = { timeout: "stuck", wrong_tool: "wrong tool", bad_state: "odd tool state", error: "printer error", interrupted: "interrupted", park_only: "park only" };
function cap(t) { return t ? t.charAt(0).toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : ".") : ""; }
function mailText(m) {
  const word = { sent: "Sent", failed: "Failed", skipped: "Not sent" }[m.status] || m.status;
  return word + ", " + friendlyTime(m.ts_utc) + (m.error ? ". " + cap(m.error) : "");
}
function card(icon, titleText, b, rows, footer) {
  return el("article", { class: "card" },
    el("div", { class: "card-h" }, ic(icon), el("h2", { class: "card-title" }, titleText), el("span", { class: "right" }, b)),
    el("div", { class: "card-b" }, el("dl", { class: "kv" }, ...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]))),
    footer ? el("div", { class: "card-f" }, footer) : null);
}
function drawCards(s) {
  const c = s.camera || {}, p = s.printer || {}, t = s.toolchanger || {}, a = s.ai || {}, ap = s.autopause || {};
  const g = s.guardian || {}, st = s.storage || {}, test = s.selftest || {}, em = (s.notifications || {}).email || {};
  const auto = { off: "Off", printing: "During prints", always: "Always" }[c.keepalive] || "Off";
  const printing = ["printing", "paused"].includes(p.print_state);
  const camBadge = !p.connected ? badge("t-crit", "i-camera-off", "No pictures")
    : c.stops_in_s != null ? badge("t-warn", "i-alert", "Stops in " + c.stops_in_s + " s")
      : c.viewer_active ? badge("t-ok", "i-check", "Saving footage") : printing ? badge("t-warn", "i-camera-off", "No pictures") : badge("t-muted", "i-off", "Off");
  const tcProblem = extra.tcProblem;
  const recent = (a.checks || {}).recent || [];
  const last = recent[0];
  const mails = extra.mails;
  const cards = [
    card("i-camera", "Camera", camBadge, [
      ["Source", c.mode === "snapshot" ? "Printer camera" : c.mode === "stream" ? "Video stream" : "Test video"],
      ["Footage", c.viewer_active ? "Keeping the last 30 s" : !p.connected ? "Not saving. Printer not connected." : printing ? "No pictures coming in" : "Not saving. No print is running."],
      ["Printer camera", c.stops_in_s != null ? "Stops in " + c.stops_in_s + " s" : c.session === "on" ? "On" : c.session === "off" ? "Off" : "Not known yet"],
      ["Auto camera", auto],
      ["Last turned on", c.keepalive_last_utc ? friendlyTime(c.keepalive_last_utc) + ". " + cap(c.keepalive_last_reason || "") : "Not by Guardian yet"]]),
    card("i-swap", "Tool changes", !t.enabled ? badge("t-muted", "i-off", "Off") : !p.connected ? badge("t-muted", "i-off", "Not checking")
      : (t.in_progress || []).length ? badge("t-info", "i-swap", "Changing") : badge("t-ok", "i-check", "Normal"), [
      ["Typical time", t.median_ms != null ? (t.median_ms / 1000).toFixed(1) + " s" : "Not measured yet"],
      ["Counts as stuck", t.timeout_s != null ? t.timeout_s + " s, " + (t.timeout_source === "learned" ? "learned from " + t.good_changes_measured + " changes" : t.timeout_source === "fixed" ? "set in Settings" : "default until " + t.learn_count + " changes are measured") : "unknown"],
      ["In progress", (t.in_progress || []).length ? t.in_progress.join(", ") : printing ? "None right now" : "None. No print is running."],
      ["Last problem", tcProblem ? friendlyTime(tcProblem.started_utc) + ". " + tool(tcProblem.previous_tool) + " to " + tool(tcProblem.requested_tool) + ": " + (TC_RESULT[tcProblem.result] || tcProblem.result) : "None"]]),
    card("i-sparkle", a.provider && a.provider !== "local" ? "AI (online)" : "Local AI",
      !a.enabled ? badge("t-muted", "i-off", "Off") : a.usable ? badge("t-ok", "i-check", "Ready") : badge("t-ai", "i-info", "Not ready"), [
      ["Model", a.model || "not set"],
      ["Last check", last ? (last.check === "bed" ? "Plate check" : "Failure check") + ", " + ago(last.ts_utc) : "None yet"],
      ["Result", last ? last.verdict + (last.answer && last.answer.description ? ". It saw \"" + last.answer.description + "\"" : "") : a.enabled ? (a.usable ? "Waiting for a print" : (a.remote_message || "Ollama or the model is missing. See Settings.")) : "Turn it on in Settings"],
      ["Reminder", "This is an AI guess. It can be wrong."]]),
    card("i-pause", "Auto pause", !ap.enabled ? badge("t-muted", "i-off", "Off") : !p.connected ? badge("t-warn", "i-alert", "Cannot act")
      : ap.pending_in_s != null ? badge("t-crit", "i-alert", "Armed") : badge("t-ok", "i-check", "On"), [
      ["Waits", ap.grace_s + " s, then pauses"],
      ["Pauses for", (ap.triggers || []).length + " kinds of problem (see Settings)"],
      ["Right now", ap.pending_in_s != null ? "Pauses in " + ap.pending_in_s + " s" : !p.connected ? "Guardian cannot pause a printer it cannot reach." : "Not armed"],
      ["Last pause by Guardian", ap.last_pause_utc ? ago(ap.last_pause_utc) : "Never"]]),
    card("i-shield", "Guardian", test.ok === false ? badge("t-warn", "i-alert", "Self-test problems") : test.firmware_changed ? badge("t-warn", "i-alert", "Firmware changed")
      : (s.incidents || {}).open ? badge("t-warn", "i-alert", (s.incidents || {}).open + " open incident") : badge("t-ok", "i-check", "All checks passed"), [
      ["Version", g.version || "unknown"], ["Open incidents", String((s.incidents || {}).open ?? 0)],
      ["Disk free", st.free_gb != null ? st.free_gb + " GB" : "unknown"],
      ["Storage used", st.incidents_gb != null ? st.incidents_gb + " GB of " + st.max_total_gb + " GB" : "unknown"],
      ["Self-test", test.ran_utc ? (test.ok ? "Passed " : "Problems found ") + friendlyTime(test.ran_utc) : "Not run yet"],
      ["Printer firmware", test.firmware || "unknown"]]),
    card("i-mail", "Email alerts", !em.enabled ? badge("t-muted", "i-off", "Off") : em.configured ? badge("t-ok", "i-check", "On") : badge("t-warn", "i-alert", "No API key"), [
      ["API key", em.configured ? "Found" : "Missing"], ["Sent to", em.to_count + " address" + (em.to_count === 1 ? "" : "es")],
      ["Last send", mails[0] ? mailText(mails[0]) : "None yet"],
      ["Before that", mails[1] ? mailText(mails[1]) : "None"]],
      em.enabled && em.configured ? el("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: async (ev) => {
        ev.target.disabled = true;
        try { await api("/api/notifications/test", { method: "POST", body: JSON.stringify({ channel: "email" }) }); toast("Test email sent. Check your inbox.", "t-ok"); }
        catch (e) { toast("Test failed: " + e.message, "t-crit"); }
        ev.target.disabled = false; loadExtra();
      } }, "Send test email") : null),
  ];
  parts.cards.replaceChildren(...cards);
}

// ---- recent incidents ----
function drawRecent() {
  const list = extra.incidents;
  parts.recent.replaceChildren(el("section", { class: "card", "aria-labelledby": "ri-h" },
    el("div", { class: "card-h", style: "padding-bottom:12px" }, el("h2", { class: "card-title", id: "ri-h" }, "Recent incidents"),
      el("a", { class: "link-btn right", href: "/incidents" }, "See all incidents", ic("i-chev-r", "sm"))),
    list.length ? el("div", { class: "list" }, incidentHead(), ...list.map(incidentRow))
      : el("div", { class: "card-b" }, el("div", { class: "empty" }, el("div", { class: "strong" }, "No incidents yet"),
        el("div", {}, "When Guardian sees a problem, the footage and timeline show up here.")))));
}

async function loadExtra() {
  try {
    const r = await api("/api/incidents?limit=3");
    extra.incidents = r.incidents;
    const s = Shell.status;
    if (s && heroState(s) === "paused" && r.incidents[0] && extra.latestUid !== r.incidents[0].uid + r.incidents[0].recording_status) {
      extra.latest = await api("/api/incidents/" + encodeURIComponent(r.incidents[0].uid));
      extra.latestUid = r.incidents[0].uid + r.incidents[0].recording_status;
    }
  } catch (e) { /* the page still works */ }
  try { extra.mails = (await api("/api/notifications/log?limit=2")).log; } catch (e) { extra.mails = []; }
  try { extra.tcProblem = ((await api("/api/toolchanges?problems=true&limit=1")).toolchanges || [])[0] || null; } catch (e) { extra.tcProblem = null; }
  drawRecent();
  if (Shell.status) draw(Shell.status);
}

function draw(s) {
  drawUrgent(s);
  drawTop(s);
  drawPaused(s);
  drawCards(s);
}

let lastState = null;
Shell.onStatus((s) => {
  const st = heroState(s);
  if (st !== lastState) { lastState = st; loadExtra(); }
  draw(s);
});
loadExtra();
setInterval(loadExtra, 30000);
listen(() => loadExtra());
// The auto pause countdown should tick every second, not every 5 s.
setInterval(() => { const s = Shell.status; if (s && (s.autopause || {}).pending_in_s != null) Shell.poll(); }, 1000);
