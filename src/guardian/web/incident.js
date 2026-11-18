// Incident detail (Halo design, Incident.dc.html). Answers "what happened, and is it safe to resume?"

const main = Shell.start("incidents");
const uid = new URLSearchParams(location.search).get("uid") || "";
let data = null, video = null, logsText = null, newestUid = null;

const NOTE_TYPES = new Set(["camera_stop_warning", "camera_stopped", "camera_started", "autopause_armed", "autopause_cancelled",
  "autopause_not_needed", "autopause_failed", "guardian_paused_print", "ai_diagnosis"]);

function eventSummary(e) {
  const m = e.metadata || {};
  return m.summary || (m.message && m.message !== m.summary ? m.message : "") || "";
}
function eventRaw(e) {
  const m = e.metadata || {};
  if (m.message && m.summary && m.message !== m.summary) return m.message;
  if (m.exception && m.exception.message && m.exception.message !== m.summary) return m.exception.message;
  return "";
}

function postSecs() { return ((Shell.status || {}).blackbox || {}).post_s || 15; }

// ---- recording status banner ----
function statusBanner(inc, clip, s) {
  const st = inc.recording_status;
  const keepOff = s && (s.camera || {}).keepalive === "off";
  if (st === "recording") {
    const post = postSecs();
    const end = new Date(inc.last_trigger_utc).getTime() + post * 1000;
    const left = Math.max(0, Math.round((end - Date.now()) / 1000) + 6);
    const total = post + 6;
    return el("section", { class: "banner t-info", "aria-label": "Recording status" }, el("span", { class: "dot pulse", style: "margin-top:8px" }),
      el("div", { class: "grow" }, el("div", { class: "b-title" }, "Recording"),
        el("div", { class: "b-text" }, left > 0 ? "The clip is ready in about " + left + " s. This page updates by itself." : "Finishing the clip. This page updates by itself."),
        el("div", { class: "bar", style: "margin-top:10px;max-width:420px" }, el("i", { style: "width:" + Math.min(100, Math.round(100 * (total - left) / total)) + "%" }))));
  }
  const map = {
    complete: ["t-ok", "i-check", "Footage saved", "The clip runs from " + secs(clip.actual_pre_s) + " before the problem to " + secs(clip.actual_post_s) + " after it."],
    partial: ["t-warn", "i-alert", "Partial footage", inc.recording_notes || "Some footage is missing."],
    missing: ["t-crit", "i-off", "No footage for this incident", (inc.recording_notes || "Guardian had no pictures to save.") + " The timeline and printer log below are still complete."],
    failed: ["t-crit", "i-x", "The video could not be saved", inc.recording_notes || "Something went wrong while saving the clip."],
  };
  const [tone, icon, head, text] = map[st] || ["t-muted", "i-info", st, inc.recording_notes || ""];
  return el("section", { class: "banner " + tone, "aria-label": "Recording status" }, ic(icon),
    el("div", { class: "grow" }, el("div", { class: "b-title" }, head), el("div", { class: "b-text" }, text),
      st === "missing" && keepOff ? el("a", { class: "link-btn", style: "margin-top:8px", href: "/settings" }, "Turn on Auto camera in Settings", ic("i-chev-r", "sm")) : null));
}

// ---- video player ----
function player(inc, clip, events) {
  const has = !!clip.url && (inc.recording_status === "complete" || inc.recording_status === "partial");
  const screen = el("div", { class: "screen" });
  if (!has) {
    const rec = inc.recording_status === "recording";
    screen.append(el("div", { class: "no-video" }, ic(rec ? "i-clock" : "i-camera-off"),
      el("p", {}, el("strong", {}, rec ? "Recording" : inc.recording_status === "failed" ? "The video could not be saved" : "No video")),
      el("p", {}, rec ? "Guardian is still saving the footage after the problem." : "The printer camera was not sending pictures at this time.")));
    return el("section", { class: "player o1", "aria-label": "Incident video" }, screen);
  }
  video = el("video", { preload: "metadata", playsinline: true, src: clip.url + tokenQuery() });
  const tag = el("span", { class: "cam-tag" }, "Clip time 0:00");
  screen.append(video, tag);
  const playBtn = el("button", { type: "button", class: "pbtn", "aria-label": "Play" }, ic("i-play"));
  const ptime = el("span", { class: "ptime" }, "0:00 / 0:00");
  const played = el("div", { class: "played", style: "width:0%" });
  const head = el("div", { class: "head", style: "left:0%" });
  const trk = el("div", { class: "trk" }, played);
  const bar = el("div", { class: "tbar", role: "slider", tabindex: "0", "aria-label": "Video time. The problem happened at " + mmss(clip.marker_s || 0) + "." }, trk);
  const legendMiss = el("span", { hidden: true }, el("i", { class: "lg-miss" }), "Missing");
  const draw = () => {
    const d = video.duration || 0;
    if (!d) return;
    const pct = (x) => Math.max(0, Math.min(100, 100 * x / d)) + "%";
    played.style.width = pct(video.currentTime);
    head.style.left = pct(video.currentTime);
    ptime.textContent = mmss(video.currentTime) + " / " + mmss(d);
    tag.textContent = "Clip time " + mmss(video.currentTime);
    bar.setAttribute("aria-valuenow", Math.round(video.currentTime));
  };
  video.addEventListener("loadedmetadata", () => {
    const d = video.duration || 1;
    for (const e of events) {
      if (e.is_trigger || e.clip_offset_s == null || e.clip_offset_s < 0 || e.clip_offset_s > d) continue;
      bar.append(el("div", { class: "tick", style: "left:" + (100 * e.clip_offset_s / d) + "%" }));
    }
    if (clip.marker_s != null) bar.append(el("div", { class: "trig", style: "left:" + Math.min(100, 100 * clip.marker_s / d) + "%" }));
    // Footage missing at the end (partial post-roll): show it on the bar, honestly.
    const post = postSecs();
    if (inc.recording_status === "partial" && clip.actual_post_s != null && clip.actual_post_s < post - 0.5) {
      const miss = post - clip.actual_post_s;
      trk.append(el("div", { class: "miss", style: "width:" + Math.min(30, 100 * miss / (d + miss)) + "%" }));
      legendMiss.hidden = false;
      legendMiss.lastChild.textContent = "Missing, " + miss.toFixed(1) + " s";
    }
    bar.append(head);
    draw();
  });
  video.addEventListener("timeupdate", draw);
  video.addEventListener("play", () => { playBtn.replaceChildren(ic("i-pause")); playBtn.setAttribute("aria-label", "Pause"); });
  video.addEventListener("pause", () => { playBtn.replaceChildren(ic("i-play")); playBtn.setAttribute("aria-label", "Play"); });
  playBtn.addEventListener("click", () => (video.paused ? video.play() : video.pause()));
  const seekAt = (clientX) => {
    const r = trk.getBoundingClientRect();
    if (video.duration) video.currentTime = Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * video.duration;
  };
  bar.addEventListener("click", (ev) => seekAt(ev.clientX));
  bar.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowRight") video.currentTime += 1;
    if (ev.key === "ArrowLeft") video.currentTime -= 1;
  });
  return el("section", { class: "player o1", "aria-label": "Incident video" }, screen,
    el("div", { class: "pctl" }, playBtn, ptime, bar),
    el("div", { class: "legend" }, el("span", {}, el("i", { class: "lg-trig" }), "Problem happened at " + mmss(clip.marker_s || 0)),
      el("span", {}, el("i", { class: "lg-tick" }), "Other event"), legendMiss));
}
function seek(s) {
  if (!video || s == null) return;
  video.currentTime = Math.max(0, s - 1);
  video.scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---- timeline ----
function timeline(inc, events) {
  const clipTime = inc.recording_status === "complete" || inc.recording_status === "partial" || inc.recording_status === "recording";
  const items = events.map(e => {
    const note = NOTE_TYPES.has(e.event_type) || (!e.is_trigger && e.source === "guardian");
    const kind = e.is_trigger ? "trigger" : note ? "note" : "event";
    const tone = note ? "t-muted" : (SEV[e.severity] || SEV.info).tone;
    const raw = eventRaw(e);
    const b = el("button", { type: "button", class: "tl-i " + kind + " " + tone },
      el("span", { class: "tl-time" }, clipTime && e.clip_offset_s != null ? mmss(e.clip_offset_s) : clock(new Date(e.ts_utc), true)),
      el("span", { class: "tl-rail" }, el("span", { class: "tl-dot" })),
      el("span", { class: "tl-body" },
        el("span", { class: "tl-top" }, el("span", { class: "tl-name" }, label(e.event_type) + (e.repeat_count > 1 ? " (" + e.repeat_count + " times)" : "")),
          e.is_trigger && e.ts_utc === inc.first_trigger_utc ? badge("t-halo", null, "Problem started here") : null,
          !note && e.severity ? badge(tone, null, (SEV[e.severity] || SEV.info).text) : null),
        eventSummary(e) ? el("span", { class: "tl-sum" }, eventSummary(e)) : null,
        raw ? el("span", { class: "raw" }, raw) : null,
        el("span", { class: "tl-src" }, note ? "Note from Guardian" : source(e.source))));
    b.addEventListener("click", () => seek(e.clip_offset_s));
    return b;
  });
  return el("section", { class: "card o3", "aria-labelledby": "tl-h" },
    el("div", { class: "card-h" }, ic("i-list"), el("h2", { class: "card-title", id: "tl-h" }, "What happened, in order"),
      el("span", { class: "tiny muted right" }, clipTime ? "Clip time" : "Time of day")),
    el("div", { class: "card-b" }, el("div", { class: "tl" }, ...items),
      inc.recording_status === "recording" ? el("p", { class: "tiny muted", style: "margin-top:10px" }, "More events can still be added until the clip is saved.") : null));
}

// ---- printer logs ----
function logsBox() {
  const box = el("div", { class: "log", tabindex: "0", role: "region", "aria-label": "Printer log" });
  const fold = el("details", { class: "fold o5" },
    el("summary", {}, ic("i-file"), "Printer log lines for this time", ic("i-chev-d", "chev")),
    el("div", { class: "fold-b" }, box, el("p", { class: "tiny muted", style: "margin-top:8px" }, "Lines with !! or \"error\" are marked. This is the printer's own text.")));
  const fill = () => {
    if (logsText === null) { box.replaceChildren(el("span", { class: "ln" }, "Loading...")); return; }
    if (!logsText.available) { box.replaceChildren(el("span", { class: "ln" }, logsText.note)); return; }
    box.replaceChildren(...logsText.text.split("\n").map(l =>
      el("span", { class: "ln" + (/!!|error|exception/i.test(l) ? " bad" : "") }, l)));
  };
  fill();
  fold._fill = fill;
  return fold;
}

// ---- AI ----
function aiCard(f, s) {
  const ai = f.ai || {};
  const head = el("div", { class: "card-h" }, ic("i-sparkle"), el("h2", { class: "card-title", id: "ai-h" }, "AI diagnosis"),
    el("span", { class: "right" }, badge("t-ai", "i-info", "AI guess, can be wrong")));
  if (!(f.available && ai.available)) {
    const off = s && !(s.ai || {}).enabled;
    return el("section", { class: "card o2", "aria-labelledby": "ai-h" }, head, el("div", { class: "card-b" }, el("div", { class: "empty hatch" },
      el("div", { class: "strong" }, "No AI diagnosis"), el("div", {}, ai.note || f.note || "None for this incident."),
      off ? el("a", { class: "btn btn-secondary btn-sm", style: "justify-self:start", href: "/settings" }, "Turn on AI in Settings") : null)));
  }
  const a = ai.answer || {};
  const conf = String(a.confidence || "").toLowerCase();
  const safe = String(a.safe_to_resume || "unknown").toLowerCase();
  const list = (t, items, ordered) => Array.isArray(items) && items.length
    ? [el("h3", { class: "sub-h" }, t), el(ordered ? "ol" : "ul", { class: ordered ? "numlist" : "bul" }, ...items.map(x => el("li", {}, String(x))))] : [];
  const where = (s && (s.ai || {}).provider && (s.ai || {}).provider !== "local") ? "by an online AI service." : "on this computer. Nothing was sent to the internet.";
  return el("section", { class: "card o2", "aria-labelledby": "ai-h" }, head, el("div", { class: "card-b col gap-l" },
    el("p", { style: "font-size:17px;line-height:1.45" }, String(a.summary || "")),
    el("dl", { class: "kv" },
      el("dt", {}, "Likely cause"), el("dd", {}, String(a.likely_cause || "Not given")),
      el("dt", {}, "Confidence"), el("dd", {}, badge(conf === "high" ? "t-ok" : conf === "medium" ? "t-warn" : "t-muted", null, a.confidence || "Unknown")),
      el("dt", {}, "Safe to resume"), el("dd", {}, safe === "yes" ? badge("t-ok", "i-check", "Yes") : safe === "no" ? badge("t-crit", "i-x", "No") : badge("t-muted", "i-info", "Unknown"))),
    el("div", {}, ...list("Evidence", a.evidence, false)),
    Array.isArray(a.what_to_check) && a.what_to_check.length ? el("div", { class: "ai-box" }, ...list("What to check", a.what_to_check, true)) : null,
    el("p", { class: "tiny muted" }, "Written " + friendlyTime(ai.created_utc) + " by " + ai.model + " " + where)));
}
function picturesCard(pics) {
  if (!pics.length) return null;
  const url = (n) => "/api/incidents/" + encodeURIComponent(uid) + "/pictures/" + n + tokenQuery();
  const view = (n) => {
    const dlg = el("dialog", { class: "g-dialog g-viewer" }, el("img", { src: url(n), alt: "Picture the AI looked at" }),
      el("div", { class: "row", style: "justify-content:flex-end;margin-top:10px" },
        el("button", { type: "button", class: "btn btn-secondary", onclick: () => { dlg.close(); dlg.remove(); } }, "Close")));
    document.querySelector(".g-app").append(dlg);
    dlg.showModal();
  };
  return el("section", { class: "card o4", "aria-labelledby": "pic-h" },
    el("div", { class: "card-h" }, ic("i-sparkle"), el("h2", { class: "card-title", id: "pic-h" }, "Pictures the AI looked at")),
    el("div", { class: "card-b" }, el("div", { class: "thumbs" }, ...pics.map((n, i) =>
      el("button", { type: "button", class: "thumb", onclick: () => view(n) }, el("img", { src: url(n), alt: "Picture " + (i + 1), loading: "lazy" }),
        el("span", {}, "Picture " + (i + 1))))),
      el("p", { class: "tiny muted", style: "margin-top:10px" }, "Tap a picture to see it larger.")));
}

// ---- details and actions ----
function details(inc, clip) {
  const footage = inc.recording_status === "missing" ? "None" : inc.recording_status === "recording"
    ? "Started " + clock(new Date(inc.start_time_utc), true) + ", still recording"
    : clock(new Date(inc.start_time_utc), true) + " to " + (inc.end_time_utc ? clock(new Date(inc.end_time_utc), true) : "unknown");
  const secsText = inc.recording_status === "missing" ? "None"
    : secs(clip.actual_pre_s) + " before, " + secs(clip.actual_post_s) + " after";
  return el("section", { class: "card o6", "aria-labelledby": "det-h" },
    el("div", { class: "card-h" }, ic("i-info"), el("h2", { class: "card-title", id: "det-h" }, "Details")),
    el("div", { class: "card-b" }, el("dl", { class: "kv" },
      ...[["Source", source(inc.source)], ["File", inc.print_filename || "No print running"],
        ["Progress", inc.print_progress != null ? inc.print_progress + "%" : "unknown"], ["Toolhead", tool(inc.active_toolhead)],
        ["Footage", footage], ["Seconds saved", secsText]].flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]))));
}
function actions(inc) {
  const note = el("textarea", { id: "note", class: "input", placeholder: "Write anything you want to remember about this." }, inc.user_note || "");
  const sw = el("button", { type: "button", class: "sw", role: "switch", "aria-checked": inc.protected ? "true" : "false", "aria-label": "Protect this incident" },
    el("span", { class: "track" }, el("span", { class: "knob" })), el("span", { class: "sw-t" }, inc.protected ? "On" : "Off"));
  const recording = inc.recording_status === "recording";
  const del = el("button", { type: "button", class: "btn btn-danger-quiet", disabled: inc.protected || recording }, ic("i-trash"), "Delete");
  sw.addEventListener("click", async () => {
    const on = sw.getAttribute("aria-checked") !== "true";
    try { await api("/api/incidents/" + encodeURIComponent(uid), { method: "PATCH", body: JSON.stringify({ protected: on }) }); load(); }
    catch (e) { toast("Could not change it: " + e.message, "t-crit"); }
  });
  del.addEventListener("click", async () => {
    if (!await ask("Delete this incident and its video for good? This cannot be undone.", { ok: "Delete", danger: true })) return;
    try { await api("/api/incidents/" + encodeURIComponent(uid) + "?confirm=true", { method: "DELETE" }); location.href = "/incidents"; }
    catch (e) { toast("Could not delete: " + e.message, "t-crit"); }
  });
  return el("section", { class: "card o7", "aria-labelledby": "act-h" },
    el("div", { class: "card-h" }, ic("i-sliders"), el("h2", { class: "card-title", id: "act-h" }, "Your notes and actions")),
    el("div", { class: "card-b col gap-l" },
      el("div", { class: "fgroup" }, el("label", { for: "note" }, "Your note"), note),
      el("div", { class: "row" }, el("button", { type: "button", class: "btn btn-secondary", onclick: async () => {
        try { await api("/api/incidents/" + encodeURIComponent(uid), { method: "PATCH", body: JSON.stringify({ user_note: note.value }) }); toast("Note saved.", "t-ok"); }
        catch (e) { toast("Could not save: " + e.message, "t-crit"); }
      } }, "Save note")),
      el("div", { class: "field", style: "padding:12px 0" },
        el("div", {}, el("div", { class: "f-label" }, "Protect this incident"), el("div", { class: "f-hint" }, "Protected incidents are never deleted automatically.")),
        el("div", { class: "f-ctl" }, sw)),
      el("div", { class: "row" },
        el("a", { class: "btn btn-secondary", href: "/api/incidents/" + encodeURIComponent(uid) + "/export" + tokenQuery() }, ic("i-download"), "Export ZIP"), del),
      el("p", { class: "tiny muted" }, inc.protected ? "Delete is off while this incident is protected. Turn off Protect first."
        : recording ? "Delete is off while the clip is still recording." : "Delete asks you to confirm first. It cannot be undone.")));
}

// ---- page ----
function draw() {
  if (!data) return;
  const s = Shell.status;
  const inc = data.incident, clip = data.clip, events = data.events;
  document.title = title(inc.event_type) + " - U1 Guardian";
  const trig = events.find(e => e.is_trigger) || {};
  const m = trig.metadata || {};
  const change = m.from_tool && m.to_tool ? "Changing from " + m.from_tool + " to " + m.to_tool : null;
  const paused = s && (s.printer || {}).print_state === "paused" && newestUid === uid && (s.controls || {}).enabled;
  const logs = logsBox();
  const keepVideo = video && !video.paused ? video : null;
  if (keepVideo) return; // do not rebuild the page while the video is playing
  main.replaceChildren(...[
    el("div", {},
      el("a", { class: "link-btn", href: "/incidents" }, el("span", { style: "display:inline-flex;transform:rotate(180deg)" }, ic("i-chev-r", "sm")), "All incidents"),
      el("div", { class: "page-head", style: "margin-top:10px" }, el("div", { class: "grow", style: "min-width:260px" },
        el("div", { class: "row gap-s", style: "margin-bottom:8px" }, sevBadge(inc.severity, true),
          inc.protected ? badge("t-muted", "i-lock", "Protected", "lg") : null, el("span", { class: "tiny muted mono" }, inc.event_type)),
        el("h1", { class: "h-page" }, title(inc.event_type)),
        el("div", { class: "row small muted", style: "margin-top:10px;gap:6px 18px" },
          el("span", { class: "row nw gap-s" }, ic("i-clock", "sm"), friendlyTime(inc.first_trigger_utc, true)),
          inc.print_filename ? el("span", { class: "row nw gap-s" }, ic("i-file", "sm"), inc.print_filename) : null,
          inc.print_progress != null ? el("span", {}, inc.print_progress + "% done") : null,
          change ? el("span", {}, change) : inc.active_toolhead ? el("span", {}, "Using " + tool(inc.active_toolhead)) : null)))),
    paused ? el("section", { class: "banner t-warn", "aria-label": "Printer is paused", style: "align-items:center;flex-wrap:wrap" }, ic("i-pause"),
      el("div", { class: "grow", style: "min-width:200px" }, el("div", { class: "b-title" }, "The print is still paused."),
        el("div", { class: "b-text" }, "Look at the printer first. Then choose what to do.")),
      resumeControls("row")) : null,
    statusBanner(inc, clip, s),
    el("div", { class: "inc-grid" },
      el("div", { class: "inc-col" }, player(inc, clip, events), timeline(inc, events), logs),
      el("div", { class: "inc-col" }, aiCard(data.findings || {}, s), picturesCard((data.snapshots || {}).ai_pictures || []),
        details(inc, clip), actions(inc)))].filter(Boolean));
  if (logsText && logsText.available && /!!|error|exception/i.test(logsText.text)) logs.open = true;
}

async function load() {
  try {
    data = await api("/api/incidents/" + encodeURIComponent(uid));
  } catch (e) {
    main.replaceChildren(el("div", { class: "banner t-crit" }, ic("i-alert"), el("div", {}, el("div", { class: "b-title" }, "Could not load this incident"),
      el("div", { class: "b-text" }, e.message)), el("a", { class: "btn btn-secondary btn-sm", href: "/incidents" }, "All incidents")));
    return;
  }
  try { newestUid = ((await api("/api/incidents?limit=1")).incidents[0] || {}).uid; } catch (e) { newestUid = null; }
  try { logsText = await api("/api/incidents/" + encodeURIComponent(uid) + "/logs"); } catch (e) { logsText = { available: false, note: "Could not load the printer log: " + e.message }; }
  // The page is drawn again when logs or the AI diagnosis arrive. Keep the video where it was.
  const keep = video && video.currentTime > 0 ? { t: video.currentTime, playing: !video.paused && !video.ended } : null;
  video = null;
  draw();
  if (keep && video) {
    const v = video;
    v.addEventListener("loadedmetadata", () => {
      v.currentTime = keep.t;
      if (keep.playing) v.play().catch(() => {});
    }, { once: true });
  }
}

load();
Shell.onStatus(() => { if (data && data.incident.recording_status === "recording") load(); });
listen((ev) => { try { if (JSON.parse(ev.data).uid === uid) load(); } catch (e) { /* ignore */ } });
// Logs and the AI diagnosis arrive a little after the clip is ready. Check again for a while.
let tries = 0;
const later = setInterval(() => {
  tries += 1;
  if (tries > 12) { clearInterval(later); return; }
  if (video && !video.paused && !video.ended) return; // never redraw while someone is watching
  if (data && data.incident.recording_status !== "recording" && (!logsText || !logsText.available || !(data.findings || {}).available)) load();
}, 10000);
