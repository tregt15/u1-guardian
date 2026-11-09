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
    fill(document.getElementById("camera"), [
      ["Source", c.mode || "unknown"],
      ["Capture", c.running ? badge("running", "ok") : badge("stopped", "bad")],
      ["Last new frame", c.last_frame_utc ? ago(c.last_frame_utc) : "none yet"],
      ["Footage now", c.mode === "snapshot" ? (viewing ? badge("being saved", "ok") : badge("not available", "warn")) : "n/a"],
          ]);
    fill(document.getElementById("guardian"), [
      ["Version", g.version],
      ["Open incidents", String((s.incidents || {}).open ?? 0)],
      ["Disk free", st.free_gb != null ? st.free_gb + " GB" : "unknown"],
      ["Incidents use", st.incidents_gb + " GB of " + st.max_total_gb + " GB"],
    ]);
    const w = document.getElementById("warnings");
    const notes = [...(st.warnings || [])];
    if (c.mode === "snapshot" && !viewing) {
      notes.push("The U1 camera only gives pictures while the Snapmaker app or the printer screen is showing the camera. Nobody is viewing now, so an incident right now would have no footage.");
    }
    if (c.last_error) notes.push("Camera: " + c.last_error);
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

refreshStatus(); loadIncidents(true);
setInterval(refreshStatus, 5000);
listen(() => loadIncidents(true));
