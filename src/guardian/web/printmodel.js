// What the camera should see right now: the print so far (read from the G-code up to where the printer is) drawn as 3 mm
// blocks, seen through the camera fit from Camera setup. Shown next to the live picture on the dashboard.
// Plain canvas, no libraries. The fit ignores lens bending, so edges can be off by some pixels (see the fit error).

const PrintModel = (() => {
  const PLATE = 270;

  function orient(x, y, rot, mirror) {  // same as orient() in camera/calibration.py
    if (mirror) x = PLATE - x;
    for (let i = 0; i < ((rot % 4) + 4) % 4; i++) [x, y] = [y, PLATE - x];
    return [x, y];
  }
  function project(P, x, y, z) {
    const w = P[2][0] * x + P[2][1] * y + P[2][2] * z + P[2][3];
    if (Math.abs(w) < 1e-9) return null;
    return [(P[0][0] * x + P[0][1] * y + P[0][2] * z + P[0][3]) / w, (P[1][0] * x + P[1][1] * y + P[1][2] * z + P[1][3]) / w, w];
  }
  function shade(hex, f) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || "").trim());
    const n = m ? parseInt(m[1], 16) : 0xE6E6E6;
    const c = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(v => Math.max(0, Math.min(255, Math.round(v * f))));
    return "rgb(" + c.join(",") + ")";
  }

  // Draw the model on a canvas whose size is w x h picture pixels. Returns a short text for the caption.
  function render(canvas, m, w, h) {
    canvas.width = w; canvas.height = h;
    const g = canvas.getContext("2d");
    g.clearRect(0, 0, w, h);
    g.fillStyle = "#14171D"; g.fillRect(0, 0, w, h);
    const P = m.P, bz = m.bed_z == null ? 0 : m.bed_z, c = m.cell_mm;
    const to = (px, py, z) => { const o = orient(px, py, m.rotation, m.mirror); return project(P, o[0], o[1], z - bz); };
    // The plate.
    const pl = [[0, 0], [PLATE, 0], [PLATE, PLATE], [0, PLATE]].map(p => to(p[0], p[1], 0));
    if (pl.every(Boolean)) {
      g.beginPath(); pl.forEach((p, i) => (i ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1]))); g.closePath();
      g.fillStyle = "#23272F"; g.fill(); g.strokeStyle = "rgba(227,179,65,.8)"; g.lineWidth = Math.max(1, w / 400); g.stroke();
    }
    const cam = Plate3D.cameraCentre(P);
    if (!cam) return "The camera fit is not usable.";
    // Blocks in a grid, so a side is drawn only where the next block is lower.
    const grid = new Map();
    for (const [cx, cy, hh, t] of m.cells) grid.set(cy * 1000 + cx, [hh, t]);
    // Draw far blocks first so near ones cover them. Distance is measured in the plate frame the camera fit uses.
    const list = m.cells.map(([cx, cy, hh, t]) => {
      const o = orient((cx + 0.5) * c, (cy + 0.5) * c, m.rotation, m.mirror);
      return { cx, cy, hh, t, o, d: Math.hypot(o[0] - cam[0], o[1] - cam[1]) };
    }).sort((a, b) => b.d - a.d);
    const quad = (pts, fill) => {
      if (!pts.every(Boolean)) return;
      g.beginPath(); pts.forEach((p, i) => (i ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1]))); g.closePath();
      g.fillStyle = fill; g.fill(); g.strokeStyle = fill; g.lineWidth = 0.6; g.stroke();
    };
    const sides = [[0, -1], [1, 0], [0, 1], [-1, 0]];  // the neighbour each side faces
    const edge = [[0, 0, 1, 0], [1, 0, 1, 1], [0, 1, 1, 1], [0, 0, 0, 1]];  // that side's edge, in block corners
    const top = Math.max(1, m.max_z);
    for (const b of list) {
      const col = m.colors.length ? m.colors[((b.t % m.colors.length) + m.colors.length) % m.colors.length] : "#E6E6E6";
      const x0 = b.cx * c, y0 = b.cy * c;
      sides.forEach(([dx, dy], i) => {
        const nb = grid.get((b.cy + dy) * 1000 + (b.cx + dx));
        const lower = nb ? nb[0] : 0;
        if (lower >= b.hh) return;
        const [ex0, ey0, ex1, ey1] = edge[i];
        const a = orient(x0 + ex0 * c, y0 + ey0 * c, m.rotation, m.mirror), z = orient(x0 + ex1 * c, y0 + ey1 * c, m.rotation, m.mirror);
        // Facing the camera? The outward normal points away from the block centre.
        const mid = [(a[0] + z[0]) / 2, (a[1] + z[1]) / 2], out = [mid[0] - b.o[0], mid[1] - b.o[1]];
        if (out[0] * (cam[0] - mid[0]) + out[1] * (cam[1] - mid[1]) <= 0) return;
        quad([project(P, a[0], a[1], lower - bz), project(P, z[0], z[1], lower - bz), project(P, z[0], z[1], b.hh - bz), project(P, a[0], a[1], b.hh - bz)],
          shade(col, 0.55 + 0.1 * i));
      });
      const f = 0.8 + 0.2 * (b.hh / top);
      quad([[0, 0], [1, 0], [1, 1], [0, 1]].map(([ex, ey]) => to(x0 + ex * c, y0 + ey * c, b.hh)), shade(col, f));
    }
    return m.cells.length + " blocks";
  }

  function mount() {
    const canvas = el("canvas", { style: "width:100%;height:auto;display:block;border-radius:10px;background:#14171D" });
    const note = el("p", { class: "cam-note" });
    const box = el("div", { class: "cam", style: "position:relative;line-height:0" }, canvas,
      el("span", { class: "cam-tag" }, el("span", { class: "dot" }), "Should look like"));
    const node = el("div", {}, box, note);
    let model = null, size = [1280, 720];

    function paint(s) {
      const text = (t) => { note.textContent = t; };
      if (!model || !model.ready || !model.P) { canvas.width = size[0]; canvas.height = size[1]; canvas.getContext("2d").clearRect(0, 0, 1, 1); text("Not drawn yet. Set up the camera first (Settings, Camera setup)."); return; }
      if (!model.cells.length) { render(canvas, Object.assign({}, model, { cells: [] }), size[0], size[1]); text("Waiting for the first layer in the print file."); return; }
      render(canvas, model, size[0], size[1]);
      const p = (s && s.printer) || {};
      text("Drawn from the print file up to where the printer is now" + (p.print_progress != null ? " (" + Math.round(p.print_progress) + "% done, " : " (") + model.max_z +
        " mm tall). It should look like the live picture. The fit is off by about " + (model.rms_px != null ? Math.round(model.rms_px) + " pixels" : "some pixels") + ".");
    }

    return {
      node,
      setSize(w, h) { if (w > 0 && h > 0 && (w !== size[0] || h !== size[1])) { size = [w, h]; paint(null); } },
      async refresh(s) {
        try { model = await api("/api/print/model"); } catch (e) { return; }
        paint(s);
      },
      repaint(s) { if (model) paint(s); },
    };
  }

  return { mount, render };
})();
