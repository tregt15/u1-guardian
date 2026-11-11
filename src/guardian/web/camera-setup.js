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
const msg = el("p", { class: "small muted" });

main.append(
  el("div", {}, el("a", { class: "link-btn", href: "/settings" }, el("span", { style: "display:inline-flex;transform:rotate(180deg)" }, ic("i-chev-r", "sm")), "Settings"),
    el("div", { class: "page-head", style: "margin-top:10px" }, el("div", {}, el("h1", { class: "h-page" }, "Camera setup"),
      el("p", { class: "h-sub" }, "Show Guardian where the camera looks, so it can compare the camera with the print file.")))),
  el("section", { class: "card" }, el("div", { class: "card-h" }, ic("i-info"), el("h2", { class: "card-title" }, "How it works")),
    el("div", { class: "card-b" }, el("ol", { class: "numlist" },
      el("li", {}, "Pick a picture where you can see the build plate. Guardian saves some by itself at the start of each print, at a few bed heights."),
      el("li", {}, "Click the 4 corners of the black build plate, in this order: back left, back right, front right, front left. If a corner is hidden, click where it would be."),
      el("li", {}, "Do the same on a second picture with the bed at a different height (for example the start of a print and 30 mm later)."),
      el("li", {}, "Press Save. Guardian draws the plate and the printed parts on the picture. If the parts are not on the drawing, press Turn or Mirror until they are.")),
      el("p", { class: "small muted", style: "margin-top:8px" }, "Guardian never moves the bed. To use the live picture now, raise the bed from the printer screen first."))),
  el("section", { class: "card" }, el("div", { class: "card-h" }, ic("i-camera"), el("h2", { class: "card-title" }, "Pictures"),
    el("button", { type: "button", class: "btn btn-secondary btn-sm right", onclick: saveLive }, ic("i-camera", "sm"), "Use the live picture now")),
    el("div", { class: "card-b" }, frameList, msg)),
  el("section", { class: "card" }, el("div", { class: "card-h" }, ic("i-eye"), el("h2", { class: "card-title" }, "Click the corners"),
    el("span", { class: "right" }, cornerBtns)),
    el("div", { class: "card-b" }, stage, el("div", { class: "row", style: "margin-top:12px" },
      el("button", { type: "button", class: "btn btn-primary", onclick: save }, "Save and check"),
      el("button", { type: "button", class: "btn btn-secondary", onclick: () => orient(1, null) }, "Turn the drawing"),
      el("button", { type: "button", class: "btn btn-secondary", onclick: () => orient(0, true) }, "Mirror the drawing"),
      el("button", { type: "button", class: "btn btn-secondary", onclick: () => { if (frame) { delete clicks[frame]; draw(); } } }, "Clear this picture"),
      el("button", { type: "button", class: "btn btn-danger-quiet", onclick: reset }, ic("i-trash", "sm"), "Start over")),
      fitBox)));

function drawCorners() {
  cornerBtns.replaceChildren(...CORNER_NAMES.map((n, i) => {
    const done = frame && clicks[frame] && clicks[frame][i];
    const b = el("button", { type: "button", role: "radio", "aria-checked": i === corner ? "true" : "false" }, done ? n + " \u2713" : n);
    b.addEventListener("click", () => { corner = i; drawCorners(); });
    return b;
  }));
}

function draw() {
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
    kids.push(el("circle", { cx: p[0], cy: p[1], r: 16, fill: "rgba(212,162,47,.35)", stroke: "#F2CD6E", "stroke-width": 4 }),
      el("text", { x: p[0] + 22, y: p[1] - 18, fill: "#FFFFFF", stroke: "#0C101F", "stroke-width": 6, "paint-order": "stroke",
        style: "font:700 44px system-ui,sans-serif" }, String(Number(i) + 1)));
  }
  svg.replaceChildren(...kids);
}

img.addEventListener("load", draw);
img.addEventListener("click", (ev) => {
  if (!frame) return;
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
  if (!fr.length) frameList.replaceChildren(el("div", { class: "empty" }, el("div", { class: "strong" }, "No pictures yet"),
    el("div", {}, "Guardian saves some at the start of your next print. Or raise the bed from the printer screen and press \"Use the live picture now\".")));
  msg.textContent = info ? "Bed height now: " + (info.bed_z_now != null ? info.bed_z_now.toFixed(1) + " mm" : "unknown") +
    (info.homed ? "." : ". The printer has not homed since the last print, so this height may not be exact.") : "";
}

function drawFit() {
  if (!info || !info.ready) { fitBox.replaceChildren(el("p", { class: "small muted", style: "margin-top:10px" }, "Not set up yet.")); return; }
  const good = info.rms_px != null && info.rms_px < 15;
  fitBox.replaceChildren(el("div", { class: "banner " + (good ? "t-ok" : "t-warn"), style: "margin-top:12px" }, ic(good ? "i-check" : "i-alert"),
    el("div", {}, el("div", { class: "b-title" }, "Set up. The clicks fit to about " + info.rms_px + " pixels."),
      el("div", { class: "b-text" }, (good ? "That is good. " : "That is a bit loose. Check the corners. ") +
        "Yellow is the plate. Blue lines are the bottoms of the printed parts and green dashes their tops. Turned " +
        (info.rotation * 90) + " degrees" + (info.mirror ? ", mirrored." : ".")))));
}

async function load() {
  try { info = await api("/api/camera-setup"); } catch (e) { msg.textContent = "Could not load: " + e.message; return; }
  clicks = {};
  for (const c of info.clicks || []) (clicks[c.frame] = clicks[c.frame] || {})[c.corner] = [c.u, c.v];
  drawFrames();
  drawFit();
  if (!frame && info.frames.length) pick((info.frames.find(f => clicks[f.name]) || info.frames[0]).name);
  else if (frame) pick(frame);
}

async function saveLive(ev) {
  ev.target.disabled = true;
  try { const m = await api("/api/camera-setup/frames", { method: "POST" }); toast("Saved the live picture (bed at " + m.bed_z + " mm).", "t-ok"); frame = m.name; await load(); }
  catch (e) { toast(e.message, "t-crit"); }
  ev.target.disabled = false;
}

async function save() {
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
