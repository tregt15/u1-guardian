// The page frame from the Halo design: header, navigation, connection light, banners, plus the shared words
// (event labels, badges) every page uses. Each page calls Shell.start("dashboard" | "incidents" | ...).

const LABELS = {
  print_paused_with_exception: "Print paused by a printer error",
  print_paused: "Print paused",
  print_error: "Print error",
  print_cancelled: "Print stopped",
  printer_exception: "Printer error",
  klipper_error_response: "Printer error message",
  klippy_shutdown: "Printer firmware stopped",
  klippy_disconnected: "Printer firmware stopped",
  filament_feed_error: "Filament feed problem",
  bed_not_clear: "Printer camera check: plate not clear",
  spaghetti_detected: "Printer camera check: spaghetti",
  residue_detected: "Printer camera check: residue",
  dirty_nozzle_detected: "Printer camera check: dirty nozzle",
  ai_bed_not_clear: "Guardian AI: plate not clear",
  ai_print_failure: "Guardian AI: print failure",
  toolchange_timeout: "Tool change: stuck",
  toolchange_wrong_tool: "Tool change: wrong tool",
  toolchange_bad_state: "Tool change: odd tool state",
  toolchange_error: "Tool change: printer error",
  toolchange_slowing: "Tool change: getting slower",
  print_stalled: "Print stopped moving",
  heater_not_reaching: "Heater cannot reach temperature",
  heater_temperature_drop: "Heater temperature dropped",
  manual: "Saved by hand",
  camera_stop_warning: "Camera stopping soon",
  camera_stopped: "Camera off",
  camera_started: "Camera on",
  autopause_armed: "Auto pause armed",
  autopause_cancelled: "Auto pause cancelled",
  autopause_not_needed: "Printer paused by itself",
  autopause_failed: "Auto pause could not reach the printer",
  guardian_paused_print: "Guardian paused the print",
  ai_diagnosis: "AI diagnosis written",
};
// Page titles for an incident (a little friendlier than the list labels).
const TITLES = {
  toolchange_timeout: "Tool change got stuck", toolchange_wrong_tool: "Tool change ended on the wrong tool",
  toolchange_bad_state: "Tool change ended in an odd state", toolchange_error: "Printer error during a tool change",
  toolchange_slowing: "Tool changes are getting slower", print_stalled: "The print stopped moving",
  manual: "Footage saved by hand",
};
function label(t) { return LABELS[t] || (t || "unknown").replace(/_/g, " "); }
function title(t) { return TITLES[t] || label(t); }

const SOURCES = { moonraker: "Printer", klipper: "Printer", printer_camera: "Note from Guardian", guardian: "Guardian",
  toolchanger: "Guardian", guardian_ai: "Guardian AI", snapmaker_ai: "Printer camera check", manual: "You" };
function source(s) { return SOURCES[s] || s || "unknown"; }

function tool(name) {
  if (!name) return "unknown";
  if (name === "extruder") return "T0";
  const m = /^extruder(\d+)$/.exec(name);
  return m ? "T" + m[1] : name;
}

const SEV = {
  critical: { tone: "t-crit", text: "Critical", icon: "i-alert", cls: "solid" },
  warning: { tone: "t-warn", text: "Warning", icon: "i-alert", cls: "" },
  info: { tone: "t-info", text: "Info", icon: "i-info", cls: "" },
};
const REC = {
  complete: { tone: "t-ok", text: "Complete", icon: "i-check" },
  partial: { tone: "t-warn", text: "Partial", icon: "i-alert" },
  missing: { tone: "t-crit", text: "Missing", icon: "i-off" },
  failed: { tone: "t-crit", text: "Failed", icon: "i-x" },
  recording: { tone: "t-info", text: "Recording", icon: null },
};

// A badge: tone like "t-ok", an icon name (or null), the text, extra classes.
function badge(tone, icon, text, cls) {
  return el("span", { class: "badge " + tone + (cls ? " " + cls : "") }, icon ? ic(icon) : null, text);
}
function sevBadge(sev, big) {
  const s = SEV[sev] || SEV.info;
  return badge(s.tone, s.icon, s.text, (s.cls + (big ? " lg" : "")).trim());
}
function recBadge(status) {
  const r = REC[status] || { tone: "t-muted", text: status || "unknown", icon: "i-info" };
  return el("span", { class: "badge " + r.tone }, status === "recording" ? el("span", { class: "dot pulse" }) : ic(r.icon), r.text);
}

// The Halo logo (inline, so it follows the theme colors).
function logo() {
  const s = el("svg", { class: "g-logo", viewBox: "0 0 48 48", fill: "none", "stroke-linecap": "round", "stroke-linejoin": "round",
    style: "color:var(--primary)", "aria-hidden": "true" });
  s.append(el("ellipse", { cx: 24, cy: 7, rx: 9, ry: 3.2, style: "stroke:var(--halo)", "stroke-width": 2.6 }));
  const g = el("g", { stroke: "currentColor", "stroke-width": 2.6 });
  for (const d of ["M17 37.4Q21.6 43.2 24 44Q26.4 43.2 31 37.4", "M13 27.6Q20.1 36.7 24 38Q27.9 36.7 35 27.6",
    "M9 17.8Q18.8 30.2 24 32Q29.2 30.2 39 17.8", "M5 7.9Q17.4 23.7 24 26Q30.6 23.7 43 7.9"]) g.append(el("path", { d }));
  s.append(g);
  return s;
}

// A small modal dialog. Returns a promise: true for OK, false for Cancel. `typed` asks the user to type a word.
function ask(message, { ok = "OK", danger = false, typed = null } = {}) {
  return new Promise((resolve) => {
    const input = typed ? el("input", { class: "input", type: "text", "aria-label": "Type " + typed, autocomplete: "off" }) : null;
    const okBtn = el("button", { type: "button", class: "btn " + (danger ? "btn-danger" : "btn-primary") }, ok);
    const close = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    const dlg = el("dialog", { class: "g-dialog" },
      el("p", {}, message),
      typed ? el("p", { class: "small muted" }, "Type " + typed + " to confirm.") : null,
      input,
      el("div", { class: "row", style: "justify-content:flex-end;margin-top:14px" },
        el("button", { type: "button", class: "btn btn-secondary", onclick: () => close(false) }, "Cancel"), okBtn));
    okBtn.addEventListener("click", () => {
      if (typed && input.value.trim().toUpperCase() !== typed) { input.focus(); return; }
      close(true);
    });
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(false); });
    document.querySelector(".g-app").append(dlg);
    dlg.showModal();
    (input || okBtn).focus();
  });
}

// A short message at the bottom of the screen.
function toast(text, tone) {
  const t = el("div", { class: "g-toast banner " + (tone || "t-info"), role: "status" }, el("div", { class: "b-text" }, text));
  document.querySelector(".g-app").append(t);
  setTimeout(() => t.remove(), 4500);
}

// Printer controls, shared by the dashboard and the incident page.
async function printerControl(action) {
  if (action === "cancel") {
    if (!await ask("Stop the print for good? It cannot be resumed after this.", { ok: "Stop print", danger: true, typed: "STOP" })) return false;
  } else if (action === "resume") {
    if (!await ask("Resume the print? Look at the printer and the build plate first.", { ok: "Resume print" })) return false;
  } else if (!await ask("Pause the print?", { ok: "Pause" })) return false;
  try {
    await api("/api/printer/" + action, { method: "POST", body: JSON.stringify({ confirm: true }) });
    toast(action === "pause" ? "Pause sent to the printer." : action === "resume" ? "Resume sent to the printer." : "Stop sent to the printer.", "t-ok");
    return true;
  } catch (e) { toast("Not sent: " + e.message, "t-crit"); return false; }
}

// Resume and Stop buttons for a paused print. After a resume is sent they turn into "Resuming".
// The note stays for 60 s, so a redraw cannot bring the buttons back and nobody can click twice.
let resumeSentAt = 0;
const resumingNow = () => Date.now() - resumeSentAt < 60000;
function resumeControls(cls, style) {
  const box = el("div", { class: cls, style: style || "" });
  const resuming = () => el("div", { class: "row gap-s", role: "status" }, ic("i-clock"), el("strong", {}, "Resuming the print..."));
  if (Date.now() - resumeSentAt < 60000) { box.append(resuming()); return box; }
  const go = el("button", { type: "button", class: "btn btn-primary" }, ic("i-play"), "Resume print");
  go.addEventListener("click", async () => {
    if (go.disabled) return;
    go.disabled = true;
    if (await printerControl("resume")) {
      resumeSentAt = Date.now();
      box.replaceChildren(resuming());
      Shell.poll();
    } else go.disabled = false;
  });
  box.append(go, el("button", { type: "button", class: "btn btn-danger-quiet", onclick: () => printerControl("cancel").then(() => Shell.poll()) }, ic("i-stop"), "Stop print"));
  return box;
}

// Reprint, print a file, upload a file. Each asks the person to confirm and to say the plate is empty.
function plateDialog(message, okText) {
  return new Promise((resolve) => {
    const box = el("input", { type: "checkbox", id: "plate-clear" });
    const okBtn = el("button", { type: "button", class: "btn btn-primary" }, okText);
    const pic = el("img", { class: "live", alt: "Camera picture of the build plate", style: "width:100%;border-radius:8px;margin:8px 0" });
    const note = el("p", { class: "small muted" }, "Raise the bed first, so you can see the plate in the picture.");
    const moveBtn = el("button", { type: "button", class: "btn btn-secondary" }, "Raise the bed so I can see the plate");
    const checkBtn = el("button", { type: "button", class: "btn btn-secondary" }, "Check the plate now");
    const refresh = () => { pic.src = "/api/camera/latest?t=" + Date.now() + tokenQuery("&"); };
    const timer = setInterval(refresh, 1500);
    refresh();
    const close = (v) => { clearInterval(timer); dlg.close(); dlg.remove(); resolve(v); };
    moveBtn.addEventListener("click", async () => {
      if (!await ask("Guardian will home the printer, move the toolhead to the back and raise the bed to 5 mm from the nozzles. "
        + "If a finished print is still on the plate, take it off first, or it can hit the toolheads. Continue?", { ok: "Raise the bed" })) return;
      moveBtn.disabled = true;
      note.textContent = "Moving the bed. This takes about a minute...";
      try {
        await api("/api/printer/view-bed", { method: "POST", body: JSON.stringify({ confirm: true }) });
        note.textContent = "The bed is up. Look at the picture. Is the plate empty?";
      } catch (e) { note.textContent = "The bed did not move: " + e.message; moveBtn.disabled = false; }
    });
    checkBtn.addEventListener("click", async () => {  // the same AI look Guardian does before it sends the start command
      checkBtn.disabled = true;
      note.textContent = "Looking at the plate. This can take a little while...";
      try { note.textContent = (await api("/api/printer/check-plate", { method: "POST" })).message; }
      catch (e) { note.textContent = "Could not check: " + e.message; }
      checkBtn.disabled = false;
    });
    const dlg = el("dialog", { class: "g-dialog", style: "max-width:560px" },
      el("p", {}, message), pic, note, el("div", { class: "row", style: "gap:8px;flex-wrap:wrap" }, moveBtn, checkBtn),
      el("p", { class: "tiny muted" }, "Before it sends the start command, Guardian also looks at the plate with the AI and will not start if it sees something on it or cannot see it."),
      el("label", { class: "row", style: "gap:8px;margin:10px 0" }, box, el("span", {}, "The build plate is empty and clear.")),
      el("div", { class: "row", style: "justify-content:flex-end;margin-top:14px" },
        el("button", { type: "button", class: "btn btn-secondary", onclick: () => close(false) }, "Cancel"), okBtn));
    okBtn.addEventListener("click", () => { if (!box.checked) { box.focus(); return; } close(true); });
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(false); });
    document.querySelector(".g-app").append(dlg);
    dlg.showModal();
    box.focus();
  });
}

async function startPrint(filename, override) {
  const what = filename ? filename : "the last file again";
  if (!override && !await plateDialog("Start printing " + what + "? Look at the printer first.", "Start print")) return false;
  if (!override) toast("Checking the plate before the start command...", "t-info");
  try {
    const r = await api("/api/printer/start", { method: "POST", body: JSON.stringify({ confirm: true, plate_clear: true, filename: filename || null, override: !!override }) });
    toast("Start sent to the printer: " + r.started, "t-ok");
    return true;
  } catch (e) {
    let info = null;
    try { info = JSON.parse(e.message); } catch (x) { /* a plain message */ }
    if (info && info.check) {  // the plate check stopped it. Nothing was sent to the printer.
      if (info.check.state === "not_clear" && await ask("Guardian did not start the print. " + info.message +
        " If you have looked and the plate really is clear, you can start anyway. Print anyway?", { ok: "Print anyway" })) return startPrint(filename, true);
      if (info.check.state !== "not_clear") toast("Not started. " + info.message, "t-crit");
      return false;
    }
    toast("Not started: " + e.message, "t-crit");
    return false;
  }
}

async function pickPrintFile() {
  let files;
  try { files = (await api("/api/printer/files")).files; } catch (e) { toast(e.message, "t-crit"); return false; }
  if (!files.length) { toast("The printer has no G-code files.", "t-info"); return false; }
  const picked = await new Promise((resolve) => {
    const close = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    const dlg = el("dialog", { class: "g-dialog" },
      el("p", {}, "Pick a file to print. Newest first."),
      el("div", { style: "max-height:50vh;overflow:auto;display:flex;flex-direction:column;gap:6px" },
        ...files.map(f => el("button", { type: "button", class: "btn btn-secondary", style: "justify-content:flex-start;text-align:left",
          onclick: () => close(f.path) }, f.path + (f.modified ? "  (" + new Date(f.modified * 1000).toLocaleString() + ")" : "")))),
      el("div", { class: "row", style: "justify-content:flex-end;margin-top:14px" },
        el("button", { type: "button", class: "btn btn-secondary", onclick: () => close(null) }, "Cancel")));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(null); });
    document.querySelector(".g-app").append(dlg);
    dlg.showModal();
  });
  return picked ? startPrint(picked) : false;
}

function uploadPrintFile() {
  return new Promise((resolve) => {
    const input = el("input", { type: "file", accept: ".gcode,.gco,.g", style: "display:none" });
    input.addEventListener("change", async () => {
      const f = input.files[0];
      input.remove();
      if (!f) { resolve(false); return; }
      if (!await ask("Send " + f.name + " to the printer? It will not start printing.", { ok: "Send file" })) { resolve(false); return; }
      toast("Sending " + f.name + "...", "t-info");
      try {
        await api("/api/printer/files/upload?confirm=true&name=" + encodeURIComponent(f.name), { method: "POST", body: f,
          headers: { "Content-Type": "application/octet-stream" } });
        toast(f.name + " is on the printer. Use Print a file to start it.", "t-ok");
        resolve(true);
      } catch (e) { toast("Not sent: " + e.message, "t-crit"); resolve(false); }
    });
    document.querySelector(".g-app").append(input);
    input.click();
  });
}

const THEMES = ["auto", "light", "dark"];  // auto follows the computer's setting

const Shell = {
  status: null,
  _subs: [],
  onStatus(fn) { this._subs.push(fn); if (this.status) fn(this.status); },

  start(active) {
    const app = document.querySelector(".g-app");
    // Theme: follows the system unless ?theme=light|dark|auto is in the address (remembered after that).
    const want = new URLSearchParams(location.search).get("theme");
    if (THEMES.includes(want)) store.set("guardian_theme", want);
    app.setAttribute("data-theme", this.theme());
    const nav = [["dashboard", "/", "i-grid", "Dashboard"], ["incidents", "/incidents", "i-list", "Incidents"],
      ["toolchanges", "/toolchanges", "i-swap", "Tool changes"], ["settings", "/settings", "i-sliders", "Settings"]];
    this.conn = el("span", { class: "conn t-muted" }, el("span", { class: "dot hollow" }),
      el("span", { class: "lbl-wide" }, "Connecting"), el("span", { class: "lbl-short" }, "..."));
    const header = el("header", { class: "g-header" }, el("div", { class: "g-header-in" },
      el("a", { class: "g-brand", href: "/" }, logo(), el("span", { class: "g-wordmark" }, "U1 Guardian")),
      el("nav", { class: "g-nav", "aria-label": "Main" }, ...nav.map(([id, href, icon, text]) =>
        el("a", { href, "aria-current": id === active ? "page" : null }, ic(icon), text))),
      el("div", { class: "g-actions" }, this.conn, this.themeBtn = el("button", { type: "button", class: "btn btn-secondary btn-sm theme-btn",
        onclick: () => this.setTheme(THEMES[(THEMES.indexOf(this.theme()) + 1) % THEMES.length]) }))));
    this.drawThemeBtn();
    this.banners = el("div", { class: "g-banners", hidden: true });
    this.main = el("main", { class: "g-page" });
    app.replaceChildren(header, this.banners, this.main);
    this.poll();
    setInterval(() => this.poll(), 5000);
    return this.main;
  },

  theme() { const t = store.get("guardian_theme"); return THEMES.includes(t) ? t : "auto"; },
  setTheme(t) {
    store.set("guardian_theme", t);
    document.querySelector(".g-app").setAttribute("data-theme", t);
    this.drawThemeBtn();
  },
  drawThemeBtn() {
    const t = this.theme();
    const [icon, text] = { auto: ["i-theme-auto", "Auto"], light: ["i-sun", "Light"], dark: ["i-moon", "Dark"] }[t];
    const next = THEMES[(THEMES.indexOf(t) + 1) % THEMES.length];
    this.themeBtn.setAttribute("aria-label", "Theme: " + text + ". Click for " + next + ".");
    this.themeBtn.setAttribute("title", "Theme: " + text + " (click to change)");
    this.themeBtn.replaceChildren(ic(icon, "sm"), el("span", { class: "lbl-wide" }, "Theme: " + text));
  },

  async poll() {
    try {
      const s = await api("/api/status");
      this.status = s;
      this.drawConn(s.printer || {});
      this.drawBanners(s);
      for (const fn of this._subs) { try { fn(s); } catch (e) { console.error(e); } }
    } catch (e) {
      this.drawConn(null, e.message);
    }
  },

  drawConn(p, err) {
    const ok = p && p.connected;
    const long = err ? "Guardian not reachable" : ok ? "Printer connected" : "Printer not connected";
    this.conn.className = "conn " + (ok ? "t-ok" : "t-crit");
    this.conn.replaceChildren(el("span", { class: "dot" + (ok ? "" : " hollow") }),
      el("span", { class: "lbl-wide" }, long), el("span", { class: "lbl-short" }, ok ? "Online" : "Offline"));
  },

  // Notices from /api/status (brief section 7). Shown on every page.
  drawBanners(s) {
    const c = s.camera || {}, p = s.printer || {}, t = s.selftest || {}, st = s.storage || {};
    const em = (s.notifications || {}).email || {};
    const printing = ["printing", "paused"].includes(p.print_state);
    const list = [];
    if (!p.connected) list.push(["t-crit", "i-wifi-off", "Guardian cannot reach the printer.",
      "Nothing is being checked or saved. Make sure the printer is on and on the same network as this computer."]);
    if (c.stops_in_s != null) list.push(["t-info", "i-camera", "The printer will stop the camera in " + c.stops_in_s + " s.",
      c.keepalive_active ? "Guardian will turn it back on right away so the footage keeps going."
        : "Auto camera is not active, so keep the camera view open in the Snapmaker app to keep footage coming."]);
    if (c.mode === "snapshot" && !c.viewer_active && printing && !c.keepalive_active)
      list.push(["t-warn", "i-camera-off", "No new camera pictures.", "Auto camera is off, so pictures only come while the Snapmaker app shows the camera view."]);
    if (c.mode === "snapshot" && !c.viewer_active && c.keepalive_active && c.keepalive_last_utc &&
      (Date.now() - new Date(c.keepalive_last_utc).getTime()) > 30000)
      list.push(["t-warn", "i-camera-off", "Guardian asked the printer to turn the camera on, but no picture came yet.", "Last try " + ago(c.keepalive_last_utc) + "."]);
    if (t.firmware_changed) list.push(["t-info", "i-info", "The printer firmware changed.",
      "Guardian's checks were made on " + t.firmware_changed[0] + ". The printer now runs " + t.firmware_changed[1] + ". Run the self-test in Settings."]);
    if (t.ok === false) list.push(["t-warn", "i-alert", "The self-test found problems.", (t.problems || []).map(x => x.name + ": " + x.detail).join(". ")]);
    for (const w of st.warnings || []) list.push(["t-warn", "i-disk", "Storage", w]);
    if (em.enabled && !em.configured) list.push(["t-warn", "i-mail", "Email alerts are on, but no Resend API key was found.", "Paste your key in Settings, Email alerts. Alerts are not sent until then."]);
    this.banners.hidden = list.length === 0;
    this.banners.replaceChildren(...list.map(([tone, icon, head, text]) =>
      el("div", { class: "banner " + tone, role: "status" }, ic(icon),
        el("div", { class: "grow" }, el("div", { class: "b-title" }, head), el("div", { class: "b-text" }, text)))));
  },
};

// One incident row from the design (dashboard and incidents list).
function incidentRow(i) {
  const note = i.recording_status === "recording" ? "The clip is ready soon."
    : i.recording_status !== "complete" && i.recording_notes ? i.recording_notes : "";
  return el("a", { class: "irow", href: "/incident?uid=" + encodeURIComponent(i.uid) },
    el("div", { class: "c-when" }, friendlyTime(i.first_trigger_utc)),
    el("div", { class: "c-what" },
      el("div", { class: "what-t" }, label(i.event_type),
        i.protected ? el("span", { class: "lockmark", title: "Protected. Never deleted automatically." }, ic("i-lock", "sm"),
          el("span", { class: "sr" }, "Protected")) : null),
      el("div", { class: "what-s" }, el("span", {}, source(i.source)), el("span", { class: "raw-n" }, i.event_type))),
    el("div", { class: "c-sev" }, sevBadge(i.severity)),
    el("div", { class: "c-print" }, el("div", { class: "fname" }, i.print_filename || "No print running"),
      i.print_progress != null ? el("div", { class: "muted tiny" }, i.print_progress + "% done") : null),
    el("div", { class: "c-rec" }, recBadge(i.recording_status), note ? el("div", { class: "rec-note" }, note) : null),
    el("div", { class: "c-go" }, ic("i-chev-r")));
}
function incidentHead() {
  return el("div", { class: "irow head", "aria-hidden": "true" }, el("div", { class: "c-when" }, "When"),
    el("div", { class: "c-what" }, "What happened"), el("div", { class: "c-sev" }, "Severity"), el("div", { class: "c-print" }, "Print"),
    el("div", { class: "c-rec" }, "Footage"), el("div", { class: "c-go" }));
}

// If part of a page breaks, say so instead of failing quietly.
function showPageError(msg) {
  const app = document.querySelector(".g-app");
  if (!app || document.getElementById("page-error")) return;
  app.append(el("div", { id: "page-error", class: "g-toast banner t-crit", role: "alert" }, ic("i-alert"),
    el("div", {}, el("div", { class: "b-title" }, "Part of this page did not load."), el("div", { class: "b-text" }, String(msg).slice(0, 300)))));
}
window.addEventListener("error", (e) => showPageError(e.message));
window.addEventListener("unhandledrejection", (e) => showPageError((e.reason && (e.reason.stack || e.reason.message)) || e.reason));
