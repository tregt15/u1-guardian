// Incidents list (Halo design, Incidents.dc.html).

const main = Shell.start("incidents");
const PAGE = 25;
let shown = 0, total = 0, sev = "";

const count = el("div", { class: "small muted", style: "padding-bottom:12px" });
const rows = el("div", { class: "list" }, incidentHead());
const more = el("button", { type: "button", class: "btn btn-secondary", hidden: true, onclick: () => load(false) }, "Show more");
const typeSel = el("select", { id: "f-type", class: "input", style: "min-width:230px" },
  ...[["", "All types"], ["toolchange", "Tool change problems"], ["printer", "Printer errors"], ["camera", "Camera checks"],
    ["ai", "Guardian AI"], ["manual", "Saved by hand"]].map(([v, t]) => el("option", { value: v }, t)));
const fileIn = el("input", { id: "f-file", class: "input", type: "search", placeholder: "for example sheep10" });

const segBtns = [["", "All"], ["info", "Info"], ["warning", "Warning"], ["critical", "Critical"]].map(([v, t]) => {
  const b = el("button", { type: "button", role: "radio", "aria-checked": v === "" ? "true" : "false" }, t);
  b.addEventListener("click", () => {
    sev = v;
    for (const x of segBtns) x.setAttribute("aria-checked", x === b ? "true" : "false");
    load(true);
  });
  return b;
});

main.append(
  el("div", { class: "page-head" }, el("div", {}, el("h1", { class: "h-page" }, "Incidents"),
    el("p", { class: "h-sub" }, "Each time Guardian saw a problem, with the footage from just before it."))),
  el("section", { class: "card", "aria-label": "Incident list" },
    el("div", { class: "filters" },
      el("div", { class: "fgroup" }, el("span", { class: "fl", id: "sev-l" }, "Severity"),
        el("div", { class: "seg", role: "radiogroup", "aria-labelledby": "sev-l" }, ...segBtns)),
      el("div", { class: "fgroup" }, el("label", { for: "f-type" }, "Type"), typeSel),
      el("div", { class: "fgroup grow" }, el("label", { for: "f-file" }, "File name contains"), fileIn),
      count),
    rows,
    el("div", { class: "more" }, more)));

typeSel.addEventListener("change", () => load(true));
let typing = null;
fileIn.addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(() => load(true), 350); });

async function load(reset) {
  if (reset) { shown = 0; rows.replaceChildren(incidentHead()); }
  const q = new URLSearchParams({ limit: PAGE, offset: shown });
  if (sev) q.set("severity", sev);
  if (typeSel.value) q.set("group", typeSel.value);
  if (fileIn.value.trim()) q.set("q", fileIn.value.trim());
  try {
    const r = await api("/api/incidents?" + q);
    total = r.total;
    rows.append(...r.incidents.map(incidentRow));
    shown += r.incidents.length;
    count.textContent = total ? "Showing " + shown + " of " + total : "";
    more.hidden = shown >= total;
    if (!total) rows.append(el("div", { class: "card-b" }, el("div", { class: "empty" },
      el("div", { class: "strong" }, "No incidents found"),
      el("div", {}, sev || typeSel.value || fileIn.value ? "Try other filters." : "When Guardian sees a problem, it shows up here."))));
  } catch (e) { count.textContent = "Could not load incidents: " + e.message; }
}

load(true);
listen(() => load(true));
