const uid = new URLSearchParams(location.search).get("uid") || "";
let data = null, video = null, track = null;

function showError(msg) {
  const e = document.getElementById("error");
  e.textContent = msg; e.hidden = false;
}

function tokenQuery() { return token() ? "?token=" + encodeURIComponent(token()) : ""; }

function seek(t) {
  if (!video || t === null || t === undefined) return;
  video.currentTime = Math.max(0, t);
  video.play().catch(() => {});
}

function renderMarkers() {
  if (!track || !video || !isFinite(video.duration) || !video.duration) return;
  track.replaceChildren();
  for (const e of data.events) {
    if (e.clip_offset_s === null || e.clip_offset_s < 0 || e.clip_offset_s > video.duration) continue;
    const pct = (100 * e.clip_offset_s / video.duration).toFixed(2);
    const t = el("div", { class: "tick" + (e.is_trigger ? "" : " minor"), style: "left:" + pct + "%",
      title: mmss(e.clip_offset_s) + " " + e.event_type });
    track.append(t);
  }
}

function render() {
  const inc = data.incident, clip = data.clip;
  document.getElementById("uid").textContent = inc.uid;
  document.title = inc.event_type + " - U1 Guardian";

  document.getElementById("head").replaceChildren(
    el("strong", {}, inc.event_type),
    badge(inc.severity, SEVERITY_KIND[inc.severity] || "info"),
    badge("recording: " + inc.recording_status, STATUS_KIND[inc.recording_status] || "info"),
    el("span", { class: "muted" }, localTime(inc.first_trigger_utc)),
  );

  // Honest recording status: say clearly when footage is not complete.
  const note = document.getElementById("recnote");
  note.replaceChildren();
  if (inc.recording_status === "recording") {
    note.append(el("div", { class: "notice info" }, "Still recording. Reload in a minute for the clip."));
  } else if (inc.recording_status === "complete" && inc.recording_notes) {
    note.append(el("div", { class: "notice info" }, inc.recording_notes));
  } else if (inc.recording_status !== "complete") {
    note.append(el("div", { class: "notice " + (inc.recording_status === "partial" ? "warn" : "bad") },
      "This footage is NOT complete (" + inc.recording_status + "). ", inc.recording_notes || "No reason was recorded."));
  }

  // Video and trigger marker
  const player = document.getElementById("player");
  player.replaceChildren();
  if (clip.url) {
    video = el("video", { controls: "controls", preload: "metadata", src: clip.url + tokenQuery() });
    track = el("div", { class: "track", title: "Red = trigger. Orange = other events." });
    player.append(video, track,
      el("p", { class: "muted" }, "Red mark: when the trigger happened" +
        (clip.latency_offset_ms ? " (shifted " + clip.latency_offset_ms + " ms for camera delay)" : "") +
        ". Clip pre-roll " + secs(clip.actual_pre_s) + ", post-roll " + secs(clip.actual_post_s) + " (actual, not the settings)."),
      el("p", { class: "muted" }, "If the video does not play in your browser, use Export ZIP and open it in a video player."));
    video.addEventListener("loadedmetadata", () => { renderMarkers(); if (clip.marker_s != null) video.currentTime = Math.max(0, clip.marker_s - 3); });
  } else {
    player.append(el("div", { class: "notice warn" }, "There is no clip for this incident."));
  }

  // Timeline (click to jump)
  const tl = document.getElementById("timeline");
  tl.replaceChildren(...data.events.map(e => el("li", { onclick: () => seek(e.clip_offset_s) },
    el("span", { class: "t" }, e.clip_offset_s != null ? mmss(e.clip_offset_s) : "--"),
    el("span", {}, (e.is_trigger ? "Trigger: " : "") + e.event_type,
      e.repeat_count > 1 ? " (repeated " + e.repeat_count + " times)" : "",
      el("div", { class: "muted" }, e.source + (e.severity ? " / " + e.severity : "") + " / " + shortTime(e.ts_utc),
        e.metadata && (e.metadata.summary || e.metadata.message) ? " / " + (e.metadata.summary || e.metadata.message) : ""))
  )));

  // Details
  const d = [
    ["Source", inc.source],
    ["Print file", inc.print_filename || "unknown"],
    ["Progress", inc.print_progress != null ? inc.print_progress + "% (by layer)" : "unknown"],
    ["Tool head", inc.active_toolhead || "unknown"],
    ["Footage", shortTime(inc.start_time_utc) + " to " + (inc.end_time_utc ? shortTime(inc.end_time_utc) : "?")],
    ["First trigger", localTime(inc.first_trigger_utc)],
    ["Last trigger", localTime(inc.last_trigger_utc)],
    ["Actual pre-roll", secs(inc.actual_pre_s)],
    ["Actual post-roll", secs(inc.actual_post_s)],
  ];
  document.getElementById("details").replaceChildren(...d.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));

  document.getElementById("findings").replaceChildren(
    el("p", { class: "muted" }, data.findings.note || ""),
    el("p", {}, "Confirmed facts and possible causes will be listed here separately once the diagnostic engine exists. Until then, use the timeline and the video."));

  document.getElementById("note").value = inc.user_note || "";
  document.getElementById("protect").checked = !!inc.protected;
  document.getElementById("export").href = "/api/incidents/" + encodeURIComponent(uid) + "/export" + tokenQuery();
}

async function load() {
  if (!uid) { showError("No incident chosen."); return; }
  try { data = await api("/api/incidents/" + encodeURIComponent(uid)); render(); }
  catch (e) { showError("Could not load this incident: " + e.message); }
}

document.getElementById("save").addEventListener("click", async () => {
  try { await api("/api/incidents/" + encodeURIComponent(uid), { method: "PATCH", body: JSON.stringify({ user_note: document.getElementById("note").value }) }); alert("Saved."); }
  catch (e) { alert("Could not save: " + e.message); }
});
document.getElementById("protect").addEventListener("change", async (ev) => {
  try { await api("/api/incidents/" + encodeURIComponent(uid), { method: "PATCH", body: JSON.stringify({ protected: ev.target.checked }) }); }
  catch (e) { alert("Could not change protection: " + e.message); ev.target.checked = !ev.target.checked; }
});
document.getElementById("delete").addEventListener("click", async () => {
  if (!confirm("Delete this incident and its video for good?")) return;
  try { await api("/api/incidents/" + encodeURIComponent(uid) + "?confirm=true", { method: "DELETE" }); location.href = "/"; }
  catch (e) { alert("Could not delete: " + e.message); }
});

async function loadLogs() {
  try {
    const r = await api("/api/incidents/" + encodeURIComponent(uid) + "/logs");
    document.getElementById("logs-sum").textContent = r.available ? "Printer logs for this time (click to open)" : r.note;
    document.getElementById("logs").textContent = r.text || "";
    return r.available;
  } catch (e) { document.getElementById("logs-sum").textContent = "Could not load printer logs: " + e.message; return true; }
}

load();
loadLogs().then(ok => { if (!ok) { const t = setInterval(async () => { if (await loadLogs()) clearInterval(t); }, 10000); } });
// While recording, check again so the page updates by itself.
setInterval(() => { if (data && data.incident.recording_status === "recording") load(); }, 5000);
