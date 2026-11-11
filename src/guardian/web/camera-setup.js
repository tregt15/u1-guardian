// Camera setup: click the 4 plate corners on pictures at two bed heights, so Guardian can compare the camera with the
// print file. Shown as part of Settings.

const main = Shell.start("settings");
const CORNER_NAMES = ["1. Back left", "2. Back right", "3. Front right", "4. Front left"];
let info = null, frame = null, corner = 0, clicks = {}, overlay = [];

const frameList = el("div", { class: "thumbs" });
const stage = el("div", { style: "position:relative;line-height:0;border-radius:12px;overflow:hidden;background:#1E222B" });
const img = el("img", { alt: "Setup picture", style: "width:100%;display:block;cursor:crosshair" });
const svg = el("svg", { style: "position:absolute;inset:0;width:100%;height:100%;pointer-events:none" });
stage.append(img, svg);
const cornerBtns = el("div", { class: "seg", role: "radiogroup", "aria-label": "Which corner" });
const fitBox = el("div");
const noFrame = el("p", { class: "small muted" }, "Pick a picture above first. Then click the corners on it here.");
const bedBox = el("div");
const lockNote = el("div", { class: "banner t-info", style: "margin-bottom:12px" }, ic("i-lock"),
  el("div", { class: "b-text" }, "Clicking is locked until the bed is at the very top, so the plate is in full view."));
let asked = false, movePoll = null;
const msg = el("p", { class: "small muted" });

main.append(
  el("div", {}, el("a", { class: "link-btn", href: "/settings" }, el("span", { style: "display:inline-flex;transform:rotate(180deg)" }, ic("i-chev-r", "sm")), "Settings"),
    el("div", { class: "page-head", style: "margin-top:10px" }, el("div", {}, el("h1", { class: "h-page" }, "Camera setup"),
      el("p", { class: "h-sub" }, "Show Guardian where the camera looks, so it can compare the camera with the print file.")))),
  el("section", { class: "card" }, el("div", { class: "card-h" }, ic("i-info"), el("h2", { class: "card-title" }, "How it works")),
    el("div", { class: "card-b" }, el("ol", { class: "numlist" },
      el("li", {}, "When you open this page, Guardian homes the printer and raises the bed to the top, after you confirm the plate is empty. It saves two pictures on the way up."),
      el("li", {}, "Click the 4 corners of the black build plate, in this order: back left, back right, front right, front left. If a corner is hidden or off the picture, press \"Skip this corner\" instead of guessing. You need at least 6 clicks in all."),
      el("li", {}, "Do the same on a second picture with the bed at a different height (for example the start of a print and 30 mm later)."),
      el("li", {}, "Press Save. Guardian draws the plate and the printed parts on the picture. If the parts are not on the drawing, press Turn or Mirror until they are.")),
      el("p", { class: "small muted", style: "margin-top:8px" }, "Guardian only moves the bed here, only when no print is running, and only after you confirm the plate is empty."))),
  bedBox,
  el("section", { class: "card" }, el("div", { class: "card-h" }, ic("i-camera"), el("h2", { class: "card-title" }, "Pictures"),
    el("button", { type: "button", class: "btn btn-secondary btn-sm right", onclick: saveLive }, ic("i-camera", "sm"), "Use the live picture now")),
    el("div", { class: "card-b" }, frameList, msg)),
  el("section", { class: "card" }, el("div", { class: "card-h" }, ic("i-eye"), el("h2", { class: "card-title" }, "Click the corners"),
    el("span", { class: "right" }, cornerBtns)),
    el("div", { class: "card-b" }, lockNote, noFrame, stage, el("div", { class: "row", style: "margin-top:12px" },
      el("button", { type: "button", class: "btn btn-primary", onclick: save }, "Save and check"),
      el("button", { type: "button", class: "btn btn-secondary", onclick: () => orient(1, null) }, "Turn the drawing"),
      el("button", { type: "button", class: "btn btn-secondary", onclick: () => orient(0, true) }, "Mirror the drawing"),
      el("button", { type: "button", class: "btn btn-secondary", onclick: skipCorner }, "Skip this corner (can't see it)"),
      el("button", { type: "button", class: "btn btn-secondary", onclick: () => { if (frame) { delete clicks[frame]; draw(); } } }, "Clear this picture"),
      el("button", { type: "button", class: "btn btn-danger-quiet", onclick: reset }, ic("i-trash", "sm"), "Start over")),
      fitBox)));

// A corner that is hidden by the toolhead or off the picture is better left out than guessed. The fit needs only
// 6 clicks in all, from 2 different bed heights.
function skipCorner() {
  if (!frame) return;
  if (clicks[frame]) delete clicks[frame][corner];
  corner = (corner + 1) % 4;
  draw(); drawFrames();
}

function clickCount() {
  return Object.values(clicks).reduce((n, cs) => n + Object.keys(cs).length, 0);
}

function errOf(f, i) {
  const c = ((info && info.clicks) || []).find(k => k.frame === f && k.corner === Number(i));
  return c ? c.err_px : null;
}

function drawCorners() {
  cornerBtns.replaceChildren(...CORNER_NAMES.map((n, i) => {
    const done = frame && clicks[frame] && clicks[frame][i];
    const b = el("button", { type: "button", role: "radio", "aria-checked": i === corner ? "true" : "false" }, done ? n + " \u2713" : n);
    b.addEventListener("click", () => { corner = i; drawCorners(); });
    return b;
  }));
}

function draw() {
  const top = !!(info && info.bed_at_top);
  lockNote.hidden = top;
  img.style.cursor = top ? "crosshair" : "not-allowed";
  img.style.opacity = top ? "1" : ".55";
  stage.hidden = !frame;
  cornerBtns.hidden = !frame;
  noFrame.hidden = !!frame;
  drawCorners();
  const w = img.naturalWidth || 1920, h = img.naturalHeight || 1080;
  svg.setAttribute("viewBox", "0 0 " + w + " " + h);
  svg.setAttribute("preserveAspectRatio", "none");
  const kids = [];
  for (const ln of overlay) {
    const d = ln.pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("");
    const color = ln.kind === "plate" ? "#F2CD6E" : ln.kind === "part" ? "#93A5FF" : "#6FD3B5";
    kids.push(el("path", { d, fill: "none", stroke: color, "stroke-width": ln.kind === "plate" ? 5 : 3, "stroke-dasharray": ln.kind === "part_top" ? "10 8" : null }));
  }
  const mine = (frame && clicks[frame]) || {};
  for (const [i, p] of Object.entries(mine)) {
    const e = info && info.ready ? errOf(frame, i) : null;
    const loose = e != null && info.rms_px != null && e > Math.max(15, info.rms_px * 1.3);  // fits worse than most
    const edge = p[0] < w * 0.01 || p[0] > w * 0.99 || p[1] < h * 0.01 || p[1] > h * 0.99;  // pinned to the picture edge
    kids.push(el("circle", { cx: p[0], cy: p[1], r: 16, fill: loose || edge ? "rgba(220,70,70,.35)" : "rgba(212,162,47,.35)",
        stroke: loose || edge ? "#FF7B7B" : "#F2CD6E", "stroke-width": 4 }),
      el("text", { x: p[0] + 22, y: p[1] - 18, fill: "#FFFFFF", stroke: "#0C101F", "stroke-width": 6, "paint-order": "stroke",
        style: "font:700 44px system-ui,sans-serif" }, String(Number(i) + 1) + (e != null ? " (" + Math.round(e) + " px off)" : "")));
  }
  svg.replaceChildren(...kids);
}

img.addEventListener("load", draw);
img.addEventListener("click", (ev) => {
  if (!frame || !(info && info.bed_at_top)) return;  // locked until the bed is at the very top
  const r = img.getBoundingClientRect();
  const u = (ev.clientX - r.left) / r.width * (img.naturalWidth || 1920);
  const v = (ev.clientY - r.top) / r.height * (img.naturalHeight || 1080);
  (clicks[frame] = clicks[frame] || {})[corner] = [u, v];
  corner = (corner + 1) % 4;
  draw();
});

async function pick(name) {
  frame = name;
  corner = clicks[name] ? [0, 1, 2, 3].find(i => !clicks[name][i]) ?? 0 : 0;
  img.src = "/api/camera-setup/frames/" + name + "?t=" + Date.now() + tokenQuery("&");
  try { overlay = (await api("/api/camera-setup/overlay/" + name)).lines; } catch (e) { overlay = []; }
  drawFrames();
  draw();
}

function drawFrames() {
  const fr = (info && info.frames) || [];
  frameList.replaceChildren(...fr.map(f => el("button", { type: "button", class: "thumb", onclick: () => pick(f.name),
    style: f.name === frame ? "outline:3px solid var(--primary);outline-offset:2px" : null },
    el("img", { src: "/api/camera-setup/frames/" + f.name + tokenQuery(), alt: "Picture at bed height " + f.bed_z + " mm", loading: "lazy" }),
    el("span", {}, "Bed at " + f.bed_z + " mm" + (clicks[f.name] ? " \u00b7 " + Object.keys(clicks[f.name]).length + " of 4 clicked" : "")))));
  if (!fr.length) frameList.replaceChildren(el("div", { class: "empty", style: "grid-column:1 / -1" }, el("div", { class: "strong" }, "No pictures yet"),
    el("div", {}, "Guardian saves some at the start of your next print. Or raise the bed from the printer screen and press \"Use the live picture now\".")));
  msg.textContent = info ? "Bed height now: " + (info.bed_z_now != null ? info.bed_z_now.toFixed(1) + " mm" : "unknown") +
    (info.homed ? "." : ". The printer has not homed since the last print, so this height may not be exact.") : "";
}

function drawFit() {
  if (!info || !info.ready) { fitBox.replaceChildren(el("p", { class: "small muted", style: "margin-top:10px" }, "Not set up yet.")); return; }
  const good = info.rms_px != null && info.rms_px < 15;
  fitBox.replaceChildren(el("div", { class: "banner " + (good ? "t-ok" : "t-warn"), style: "margin-top:12px" }, ic(good ? "i-check" : "i-alert"),
    el("div", {}, el("div", { class: "b-title" }, "Set up. The clicks fit to about " + info.rms_px + " pixels."),
      el("div", { class: "b-text" }, (good ? "That is good. " : "That is a bit loose. Red dots fit worst, or sit on the picture edge. Skip any corner you cannot really see, then save again. ") +
        "Yellow is the plate. Blue lines are the bottoms of the printed parts and green dashes their tops. Turned " +
        (info.rotation * 90) + " degrees" + (info.mirror ? ", mirrored." : ".")))));
}

function drawBed() {
  const m = (info && info.move) || {};
  if (m.running) {
    bedBox.replaceChildren(el("div", { class: "banner t-info" }, el("span", { class: "dot pulse", style: "margin-top:8px" }),
      el("div", {}, el("div", { class: "b-title" }, "Moving the bed: " + (m.step || "starting")),
        el("div", { class: "b-text" }, "Keep hands out of the printer. This takes about a minute."))));
    return;
  }
  if (m.state === "error") {
    bedBox.replaceChildren(el("div", { class: "banner t-crit" }, ic("i-alert"), el("div", { class: "grow" },
      el("div", { class: "b-title" }, "Guardian stopped moving the bed."), el("div", { class: "b-text" }, m.error || "")),
      el("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: askMove }, "Try again")));
    return;
  }
  if (info && !info.bed_at_top) {
    const why = !info.moves_allowed ? "Moving the bed for the setup is turned off in Settings, Printer controls. You can also raise the bed from the printer screen."
      : ["printing", "paused"].includes(info.print_state) ? "A print is " + info.print_state + ". Finish it and clear the plate first."
        : "The bed is not at the top yet.";
    bedBox.replaceChildren(el("div", { class: "banner t-warn" }, ic("i-alert"), el("div", { class: "grow" },
      el("div", { class: "b-title" }, "The plate is not in view."), el("div", { class: "b-text" }, why)),
      info.moves_allowed && !["printing", "paused"].includes(info.print_state) ? el("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: askMove }, "Try again") : null));
    return;
  }
  bedBox.replaceChildren();
}

async function askMove() {
  const lp = (info && info.last_print) || {};
  const last = lp.file ? "The last print was " + lp.file + (lp.height_mm ? " (" + lp.height_mm + " mm tall)" : "") + ". Make sure it is off the plate. " : "";
  const ok = await ask("To set up the camera, Guardian will home the printer and raise the bed to the top. " + last +
    "Anything left on the build plate would hit the toolhead. Keep hands out of the printer.", { ok: "Plate is empty, go", typed: "EMPTY" });
  if (!ok) { drawBed(); return; }
  try { await api("/api/camera-setup/move", { method: "POST", body: JSON.stringify({ plate_empty: true }) }); }
  catch (e) { toast(e.message, "t-crit"); return; }
  pollMove();
}

function pollMove() {
  clearInterval(movePoll);
  movePoll = setInterval(async () => {
    try { info = await api("/api/camera-setup"); } catch (e) { return; }
    drawBed(); draw();
    if (!info.move.running) {
      clearInterval(movePoll);
      if (info.move.state === "done") { toast("The bed is at the top. Click the 4 corners on each picture.", "t-ok"); frame = null; load(); }
    }
  }, 2000);
  drawBed();
}

async function load() {
  try { info = await api("/api/camera-setup"); } catch (e) { msg.textContent = "Could not load: " + e.message; return; }
  drawBed();
  if (info.move.running) pollMove();
  else if (!info.bed_at_top && !asked && info.moves_allowed && !["printing", "paused"].includes(info.print_state)) { asked = true; askMove(); }
  clicks = {};
  for (const c of info.clicks || []) (clicks[c.frame] = clicks[c.frame] || {})[c.corner] = [c.u, c.v];
  drawFrames();
  drawFit();
  const pref = info.frames.find(f => f.name === "setup_05") || info.frames.find(f => clicks[f.name]) || info.frames[0];
  if (!frame && pref) pick(pref.name);
  else if (frame) pick(frame);
}

async function saveLive(ev) {
  ev.target.disabled = true;
  try { const m = await api("/api/camera-setup/frames", { method: "POST" }); toast("Saved the live picture (bed at " + m.bed_z + " mm).", "t-ok"); frame = m.name; await load(); }
  catch (e) { toast(e.message, "t-crit"); }
  ev.target.disabled = false;
}

async function save() {
  const n = clickCount();
  if (n < 6) { toast("You have " + n + " clicks. The fit needs at least 6, from 2 pictures at different bed heights.", "t-warn"); return; }
  const list = [];
  for (const [f, cs] of Object.entries(clicks)) for (const [i, p] of Object.entries(cs)) list.push({ frame: f, corner: Number(i), u: p[0], v: p[1] });
  try { info = await api("/api/camera-setup", { method: "PUT", body: JSON.stringify({ clicks: list }) }); toast("Saved.", "t-ok"); }
  catch (e) { toast("Not saved: " + e.message, "t-crit"); return; }
  drawFit();
  if (frame) pick(frame);
}

async function orient(turn, mirror) {
  if (!info || !info.ready) { toast("Click the corners and save first.", "t-warn"); return; }
  const body = { rotation: (info.rotation + turn) % 4, mirror: mirror ? !info.mirror : info.mirror };
  try { info = await api("/api/camera-setup", { method: "PUT", body: JSON.stringify(body) }); } catch (e) { toast(e.message, "t-crit"); return; }
  drawFit();
  if (frame) pick(frame);
}

async function reset() {
  if (!await ask("Remove the camera setup and all clicks? The saved pictures stay.", { ok: "Start over", danger: true })) return;
  try { await api("/api/camera-setup", { method: "DELETE" }); } catch (e) { toast(e.message, "t-crit"); }
  frame = null; overlay = []; await load(); draw();
}

load();

draw();
