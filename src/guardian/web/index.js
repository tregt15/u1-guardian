const PAGE = 25;
let shown = 0, total = 0;

function fill(dl, pairs) {
  dl.replaceChildren(...pairs.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    document.getElementById("conn").textContent = "";
    const p = s.printer || {}, c = s.camera || {}, g = s.guardian || {}, st = s.storage || {};
    fill(document.getElementById("printer"), [
      ["Link", p.connected ? badge("connected", "ok") : badge("not connected", "bad")],
      ["State", p.print_state || "unknown"],
      ["File", p.print_filename || "none"],
      ["Progress", p.print_progress != null ? p.print_progress + "% (by layer)" : "unknown"],
      ["Tool head", p.active_toolhead || "unknown"],
    ]);
    const viewing = c.viewer_active;
    const printing = ["printing", "paused"].includes(p.print_state);
    fill(document.getElementById("camera"), [
      ["Source", c.mode || "unknown"],
      ["Capture", c.running ? badge("running", "ok") : badge("stopped", "bad")],
      ["Last new frame", c.last_frame_utc ? ago(c.last_frame_utc) : "none yet"],
      ["Footage now", c.mode !== "snapshot" ? "n/a" : viewing ? badge("being saved", "ok") :
        c.keepalive_active ? badge("turning camera on", "warn") :
        !printing ? badge("not recording (no print)", "info") : badge("not available", "warn")],
      ["Auto camera", el("span", {}, ({ off: "off", printing: "on during prints", always: "always on" })[c.keepalive] || "off",
        c.keepalive_last_utc ? el("div", { class: "muted" }, "last asked " + ago(c.keepalive_last_utc) + ": " + c.keepalive_last_reason) : null)],
      ["Printer camera", el("span", {},
        c.stops_in_s != null ? badge("stops in " + c.stops_in_s + " s", "warn") :
          c.session == null ? "no news since Guardian started" :
          c.session === "on" ? badge("on", "ok") : badge("off", "warn"),
        c.session_changed_utc ? el("div", { class: "muted" }, "changed " + ago(c.session_changed_utc)) : null)],
          ]);
    const t = s.toolchanger || {};
    const timeoutWhy = { learned: "learned from your printer", fixed: "set in config", default: "default until " + t.learn_count + " good changes are measured" };
    fill(document.getElementById("tools"), t.enabled === false ? [["Watching", badge("off", "warn")]] : [
      ["Watching", badge("on", "ok")],
      ["Now", (t.in_progress || []).length ? "changing " + t.in_progress.join(", ") : "no change in progress"],
      ["Typical time", t.median_ms != null ? (t.median_ms / 1000).toFixed(1) + " s" : "not measured yet"],
      ["Counts as stuck after", t.timeout_s != null ? t.timeout_s + " s (" + (timeoutWhy[t.timeout_source] || t.timeout_source) + ")" : "unknown"],
    ]);
    fill(document.getElementById("guardian"), [
      ["Version", g.version],
      ["Open incidents", String((s.incidents || {}).open ?? 0)],
      ["Auto pause", el("span", {}, (s.autopause || {}).enabled ? badge("on", "ok") : badge("off", "info"),
        (s.autopause || {}).last_pause_utc ? el("div", { class: "muted" }, "last paused " + ago(s.autopause.last_pause_utc)) : null)],
      ["Disk free", st.free_gb != null ? st.free_gb + " GB" : "unknown"],
      ["Incidents use", st.incidents_gb + " GB of " + st.max_total_gb + " GB"],
    ]);
    const em = (s.notifications || {}).email || {};
    fill(document.getElementById("email"), [
      ["Alerts", em.enabled ? badge("on", "ok") : badge("off", "warn")],
      ["API key", em.enabled ? (em.configured ? badge("found", "ok") : badge("missing", "bad")) : "not needed"],
      ["Sends to", em.enabled ? em.to_count + " address(es)" : "n/a"],
    ]);
    document.getElementById("email-test").hidden = !(em.enabled && em.configured);
    const w = document.getElementById("warnings");
    const notes = [...(st.warnings || [])];
    if (c.mode === "snapshot" && !viewing) {
      if (c.keepalive_active) {
        const tried = c.keepalive_last_utc ? (Date.now() - new Date(c.keepalive_last_utc).getTime()) / 1000 : 0;
        if (tried > 30) notes.push("Guardian asked the printer to turn the camera on, but no new picture has come in yet. Last try " + ago(c.keepalive_last_utc) + ".");
      } else if (printing) {
        notes.push("A print is running but no new camera picture" + (c.last_frame_utc ? " since " + ago(c.last_frame_utc) : " yet") +
          ". Auto camera is off, so pictures only come while the Snapmaker app shows the camera view. An incident right now would have no footage.");
      }
    }
    if (c.stops_in_s != null && !c.keepalive_active) {
      notes.push("The printer will stop the camera in about " + c.stops_in_s + " s. Auto camera is not active, " +
        "so keep the camera view open in the Snapmaker app to keep footage coming.");
    }
    const ap = s.autopause || {};
    if (ap.pending_in_s != null) {
      notes.push("Guardian will pause the print in about " + ap.pending_in_s + " s unless the printer pauses itself. Reason: " + ap.pending_reason);
    }
    if (c.last_error) notes.push("Camera: " + c.last_error);
    if (em.enabled && !em.configured) notes.push("Email alerts are on but no Resend API key was found. Set your own key (see the README). Alerts will not be sent.");
    w.replaceChildren(...notes.map(n => el("div", { class: "notice warn" }, n)));
  } catch (e) {
    document.getElementById("conn").textContent = "Cannot reach Guardian: " + e.message;
  }
}

function row(i) {
  const link = el("a", { href: "/incident?uid=" + encodeURIComponent(i.uid) }, shortTime(i.first_trigger_utc));
  const prog = i.print_filename ? i.print_filename + (i.print_progress != null ? " (" + i.print_progress + "%)" : "") : "";
  return el("tr", {},
    el("td", {}, link),
    el("td", {}, i.event_type, el("div", { class: "muted" }, i.source)),
    el("td", {}, badge(i.severity, SEVERITY_KIND[i.severity] || "info")),
    el("td", {}, prog),
    el("td", {}, badge(i.recording_status, STATUS_KIND[i.recording_status] || "info"),
      i.recording_status !== "complete" && i.recording_notes ? el("div", { class: "muted" }, i.recording_notes) : null),
  );
}

async function loadIncidents(reset) {
  if (reset) { shown = 0; document.getElementById("rows").replaceChildren(); }
  const q = new URLSearchParams({ limit: PAGE, offset: shown });
  for (const [id, key] of [["f-sev", "severity"], ["f-type", "type"], ["f-q", "q"]]) {
    const v = document.getElementById(id).value.trim();
    if (v) q.set(key, v);
  }
  try {
    const r = await api("/api/incidents?" + q);
    total = r.total;
    document.getElementById("rows").append(...r.incidents.map(row));
    shown += r.incidents.length;
    document.getElementById("empty").hidden = total > 0;
    document.getElementById("count").textContent = total ? "Showing " + shown + " of " + total + "." : "";
    document.getElementById("more").hidden = shown >= total;
  } catch (e) {
    document.getElementById("count").textContent = "Could not load incidents: " + e.message;
  }
}

document.getElementById("f-go").addEventListener("click", () => loadIncidents(true));
document.getElementById("more").addEventListener("click", () => loadIncidents(false));
document.getElementById("manual").addEventListener("click", async (ev) => {
  const note = prompt("Save the footage around now as an incident. Optional note:", "");
  if (note === null) return;
  ev.target.disabled = true;
  try {
    const r = await api("/api/incidents", { method: "POST", body: JSON.stringify({ note: note || null }) });
    alert(r.action === "created" ? "Incident created. The clip is ready after the post-roll finishes." : "Added to the open incident.");
    loadIncidents(true);
  } catch (e) { alert("Could not create incident: " + e.message); }
  ev.target.disabled = false;
});

async function loadEmailLog() {
  try {
    const r = await api("/api/notifications/log?limit=5");
    document.getElementById("email-log").replaceChildren(...r.log.map(n =>
      el("li", {}, shortTime(n.ts_utc) + " " + n.kind + ": " + n.status + (n.error ? " (" + n.error + ")" : ""))));
  } catch (e) { /* the log is optional */ }
}
document.getElementById("email-test").addEventListener("click", async (ev) => {
  ev.target.disabled = true;
  try { await api("/api/notifications/test", { method: "POST", body: JSON.stringify({ channel: "email" }) }); alert("Test email sent. Check your inbox."); }
  catch (e) { alert("Test failed: " + e.message); }
  ev.target.disabled = false; loadEmailLog();
});

const TOOL_RESULT = { timeout: "did not finish in time", wrong_tool: "wrong tool", bad_state: "tool states looked wrong",
  error: "printer error", interrupted: "interrupted" };
async function loadToolProblems() {
  try {
    const r = await api("/api/toolchanges?problems=true&limit=5");
    const c = r.counts || {};
    const good = c.ok || 0, bad = Object.entries(c).filter(([k]) => !["ok", "open", "park_only"].includes(k)).reduce((a, [, n]) => a + n, 0);
    const head = el("li", { style: "list-style:none;margin-left:-18px" }, good + " good, " + bad + " with problems (all time).");
    const items = r.toolchanges.map(tc => {
      const label = toolLabel(tc.previous_tool) + " -> " + toolLabel(tc.requested_tool);
      const text = shortTime(tc.started_utc) + " " + label + ": " + (TOOL_RESULT[tc.result] || tc.result);
      return el("li", {}, tc.incident_uid ? el("a", { href: "/incident?uid=" + encodeURIComponent(tc.incident_uid) }, text) : text,
        tc.error ? el("div", {}, tc.error) : null);
    });
    document.getElementById("tool-problems").replaceChildren(head, ...items);
  } catch (e) { /* optional */ }
}
function toolLabel(n) { return !n ? "?" : n === "extruder" ? "T0" : /^extruder\d+$/.test(n) ? "T" + n.slice(8) : n; }

refreshStatus(); loadIncidents(true); loadEmailLog(); loadToolProblems();
setInterval(loadToolProblems, 30000);
setInterval(refreshStatus, 5000);
listen(() => { loadIncidents(true); loadToolProblems(); });
