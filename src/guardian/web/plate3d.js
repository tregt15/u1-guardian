// A small 3D view of the build plate and the camera, worked out from the camera setup fit.
// Plain canvas, no libraries. Drag to turn it, double click to reset. Everything here is drawn from the saved fit (the 3x4
// matrix P), so it shows what Guardian believes. If the camera looks like it floats in the wrong place, the fit is off.
// Units are mm. Z is up. The plate top is at Z = -bed_z (the bed moves down when bed_z grows). The corners are in the
// clicked corner frame: 1 back left, 2 back right, 3 front right, 4 front left.

const Plate3D = (() => {
  const PLATE = 270, HEIGHTS = [30, 7, 5];
  const CORNERS = [[0, 0], [PLATE, 0], [PLATE, PLATE], [0, PLATE]];
  const NAMES = ["1", "2", "3", "4"];

  // Camera centre from P = [M | p4]: C = -M^-1 p4, solved with Cramer's rule. Returns null if M is flat.
  function cameraCentre(P) {
    const m = P.map(r => r.slice(0, 3)), b = P.map(r => -r[3]);
    const det = a => a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0]) + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]);
    const d = det(m);
    if (!isFinite(d) || Math.abs(d) < 1e-12) return null;
    return [0, 1, 2].map(c => det(m.map((row, i) => row.map((v, j) => (j === c ? b[i] : v)))) / d);
  }

  function mount(info) {
    const canvas = el("canvas", { width: 640, height: 400, style: "width:100%;height:auto;border-radius:12px;background:#1E222B;cursor:grab;touch-action:none" });
    const note = el("p", { class: "small muted", style: "margin-top:8px" });
    let yaw = -0.7, pitch = 0.5, zoom = 1, drag = null;

    function scene() {
      const P = info && info.P;
      const cam = P ? cameraCentre(P) : null;
      const bz = info && info.bed_z_now != null ? info.bed_z_now : null;
      return { cam, bz };
    }

    function draw() {
      const g = canvas.getContext("2d"), W = canvas.width, H = canvas.height;
      g.clearRect(0, 0, W, H);
      const { cam, bz } = scene();
      const heights = bz != null && !HEIGHTS.includes(Math.round(bz)) ? HEIGHTS.concat([Math.round(bz)]) : HEIGHTS;
      const pts = CORNERS.map(c => [c[0], c[1], 0]).concat(CORNERS.map(c => [c[0], c[1], -Math.max(...heights)]));
      if (cam) pts.push(cam);
      const mid = [PLATE / 2, PLATE / 2, -15];
      const rad = Math.max(120, ...pts.map(p => Math.hypot(p[0] - mid[0], p[1] - mid[1], p[2] - mid[2])));
      const sc = (Math.min(W, H) * 0.42 / rad) * zoom;
      const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
      const proj = p => {  // turn about Z (yaw), then tip (pitch); Z stays up on the screen
        const x = p[0] - mid[0], y = p[1] - mid[1], z = p[2] - mid[2];
        const x1 = x * cy - y * sy, y1 = x * sy + y * cy;
        return [W / 2 + x1 * sc, H / 2 + (-z * cp + y1 * sp) * sc, y1 * cp + z * sp];
      };
      const line = (a, b, col, w, dash) => {
        const p = proj(a), q = proj(b);
        g.beginPath(); g.setLineDash(dash || []); g.strokeStyle = col; g.lineWidth = w || 1.5; g.moveTo(p[0], p[1]); g.lineTo(q[0], q[1]); g.stroke();
      };
      const poly = (z, col, w, fill, dash) => {
        const q = CORNERS.map(c => proj([c[0], c[1], z]));
        g.beginPath(); g.setLineDash(dash || []); q.forEach((p, i) => (i ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1]))); g.closePath();
        if (fill) { g.fillStyle = fill; g.fill(); }
        g.strokeStyle = col; g.lineWidth = w; g.stroke();
      };
      const text = (s, p, col, dx, dy) => { const q = proj(p); g.setLineDash([]); g.fillStyle = col; g.font = "12px sans-serif"; g.fillText(s, q[0] + (dx || 6), q[1] + (dy || -6)); };

      // Rails the bed rides on: a line down from each corner of the plate at its top position.
      CORNERS.forEach(c => line([c[0], c[1], 0], [c[0], c[1], -Math.max(...heights)], "rgba(255,255,255,.12)", 1, [3, 4]));
      // The plate at each saved bed height. The top one (Z 0 above the plate) is the real picture the camera sees at the top.
      heights.slice().sort((a, b) => b - a).forEach(h => {
        const cur = bz != null && Math.round(bz) === h;
        poly(-h, cur ? "#E3B341" : "rgba(227,179,65,.35)", cur ? 2.5 : 1, cur ? "rgba(227,179,65,.18)" : null, cur ? null : [5, 4]);
        text(h + " mm", [PLATE, 0, -h], cur ? "#E3B341" : "rgba(227,179,65,.7)", 8, 4);
      });
      CORNERS.forEach((c, i) => text(NAMES[i], [c[0], c[1], 0], "#fff", -4, -8));
      // The camera, the rays to the 4 corners, and where it points.
      if (cam) {
        CORNERS.forEach(c => line(cam, [c[0], c[1], 0], "rgba(88,166,255,.45)", 1));
        const f = proj(cam);
        g.setLineDash([]); g.fillStyle = "#58A6FF"; g.beginPath(); g.arc(f[0], f[1], 6, 0, 7); g.fill();
        text("Camera", cam, "#58A6FF", 10, 4);
      }
      // A 50 mm block at the middle of the plate, as a size check.
      const b = 50, o = [PLATE / 2 - b / 2, PLATE / 2 - b / 2];
      const bx = [[o[0], o[1]], [o[0] + b, o[1]], [o[0] + b, o[1] + b], [o[0], o[1] + b]];
      bx.forEach((c, i) => { const n = bx[(i + 1) % 4]; line([c[0], c[1], 0], [n[0], n[1], 0], "#7EE787", 1.5); line([c[0], c[1], b], [n[0], n[1], b], "#7EE787", 1.5); line([c[0], c[1], 0], [c[0], c[1], b], "#7EE787", 1.5); });
      text("50 mm block", [o[0] + b, o[1], b], "#7EE787", 6, -2);
      // Axes at corner 1.
      line([0, 0, 0], [60, 0, 0], "#FF7B72", 2); text("X", [60, 0, 0], "#FF7B72");
      line([0, 0, 0], [0, 60, 0], "#79C0FF", 2); text("Y", [0, 60, 0], "#79C0FF");
      line([0, 0, 0], [0, 0, 60], "#D2A8FF", 2); text("Z", [0, 0, 60], "#D2A8FF");

      note.textContent = !info || !info.ready ? "Not set up yet. Save your corner clicks and the plate and camera show here."
        : !cam ? "The saved fit is not usable for a 3D view."
          : "Yellow is the plate at each saved bed height (solid is the bed now" + (bz != null ? ", " + (Math.round(bz * 10) / 10) + " mm" : "") + "). Blue is the camera, worked out from your clicks, with lines to the 4 corners. Camera is about " +
            Math.round(Math.hypot(cam[0] - PLATE / 2, cam[1] - PLATE / 2, cam[2])) + " mm from the middle of the plate. Drag to turn, scroll to zoom, double click to reset." +
            (info.using_default ? " This is the built in default setup." : "");
    }

    canvas.addEventListener("pointerdown", e => { drag = [e.clientX, e.clientY]; canvas.setPointerCapture(e.pointerId); canvas.style.cursor = "grabbing"; });
    canvas.addEventListener("pointerup", () => { drag = null; canvas.style.cursor = "grab"; });
    canvas.addEventListener("pointermove", e => {
      if (!drag) return;
      yaw += (e.clientX - drag[0]) * 0.01;
      pitch = Math.max(-1.4, Math.min(1.4, pitch + (e.clientY - drag[1]) * 0.01));
      drag = [e.clientX, e.clientY]; draw();
    });
    canvas.addEventListener("wheel", e => { e.preventDefault(); zoom = Math.max(0.4, Math.min(4, zoom * (e.deltaY < 0 ? 1.1 : 0.9))); draw(); }, { passive: false });
    canvas.addEventListener("dblclick", () => { yaw = -0.7; pitch = 0.5; zoom = 1; draw(); });

    const card = el("section", { class: "card" }, el("div", { class: "card-h" }, ic("i-eye"), el("h2", { class: "card-title" }, "3D view of the plate")),
      el("div", { class: "card-b" }, canvas, note));
    return { card, update(i) { info = i; draw(); } };
  }

  return { mount };
})();
