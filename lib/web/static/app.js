// fcvm web console. Plain JS, no build step. Data from the API is untrusted:
// it only ever reaches the DOM as text (h() uses textContent), never as HTML.
"use strict";

// --- tiny DOM helper ------------------------------------------------------------------
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const svgNS = "http://www.w3.org/2000/svg";
function s(tag, attrs, text) {
  const el = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  return el;
}
const $ = (id) => document.getElementById(id);

// --- formatting -----------------------------------------------------------------------
function bytes(n) {
  if (n == null) return "–";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
}
const rate = (n) => (n == null ? "–" : `${bytes(n)}/s`);
const pct = (n) => (n == null ? "–" : `${n.toFixed(n < 10 ? 1 : 0)}%`);
function ago(iso) {
  if (!iso) return "–";
  const sec = (Date.now() - new Date(iso).getTime()) / 1000;
  if (sec < 60) return "just now";
  if (sec < 3600) return `${Math.floor(sec / 60)} min ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} h ago`;
  return `${Math.floor(sec / 86400)} d ago`;
}
function netLabel(vm) {
  const n = vm.net || {};
  if (n.mode === "none") return "none";
  if (n.mode === "restricted") return `allow: ${(n.allow || []).join(", ")}`;
  return "full";
}

// --- API ------------------------------------------------------------------------------
async function api(method, path, body) {
  const res = await fetch(`/api/${path}`, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined, credentials: "same-origin",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}
function toast(msg, err) {
  const t = h("div", { class: `toast${err ? " err" : ""}` }, msg);
  $("toasts").append(t);
  setTimeout(() => t.remove(), err ? 9000 : 4000);
}
async function act(label, fn) {
  try {
    const r = await fn();
    if (label) toast(label);
    refresh();
    return r;
  } catch (e) {
    toast(e.message, true);
  }
}

// --- state & routing -------------------------------------------------------------------
const state = { vms: [], images: [], snapshots: [], volumes: [], host: null, jobs: [], presets: [] };
let cleanup = [];           // per-page teardown (terminals, timers)
let route = { page: "dashboard", arg: null, tab: null };

function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const page = parts[0] || "dashboard";
  return { page, arg: parts[1] || null, tab: parts[2] || null };
}
window.addEventListener("hashchange", () => { route = parseRoute(); render(true); });

async function loadAll() {
  const [vms, images, snapshots, volumes, host, jobs] = await Promise.all([
    api("GET", "vms"), api("GET", "images"), api("GET", "snapshots"), api("GET", "volumes"),
    api("GET", "host"), api("GET", "jobs"),
  ]);
  Object.assign(state, { vms, images, snapshots, volumes, host, jobs });
}

async function refresh() {
  try {
    await loadAll();
  } catch (e) {
    if (/token|401/.test(e.message)) {
      $("main").replaceChildren(h("div", { class: "card" }, h("h1", {}, "Not signed in"),
        h("p", { class: "sub" }, "Open the URL printed by `fcvm serve`: it carries the access token.")));
      return;
    }
    toast(e.message, true);
    return;
  }
  chrome();
  render(false);
}

function chrome() {
  const run = state.vms.filter((v) => v.state === "running").length;
  $("n-instances").textContent = `${run}/${state.vms.length}`;
  $("n-images").textContent = state.images.length;
  $("n-snapshots").textContent = state.snapshots.length || "";
  $("n-volumes").textContent = state.volumes.length || "";
  const hst = state.host;
  if (hst) {
    $("host").textContent = `${hst.hostname} · ${hst.cpus} CPUs · kernel ${hst.kernel.replace("vmlinux-", "")}`;
    const active = state.jobs.filter((j) => j.status === "running");
    $("foot").replaceChildren(active.length ? h("div", { class: "jobs" },
      active.map((j) => h("div", { class: "job" }, "⟳ ", j.title))) : "");
  }
  for (const a of document.querySelectorAll("nav a")) {
    a.classList.toggle("active", a.dataset.page === (route.page === "instance" ? "instances" : route.page));
  }
}

// Pages redraw on every refresh, except parts that must persist (terminals, forms).
let lastPageKey = null;
function render(navigated) {
  const key = `${route.page}/${route.arg}/${route.tab}`;
  if (navigated || key !== lastPageKey) {
    cleanup.forEach((f) => f());
    cleanup = [];
    lastPageKey = key;
    $("main").replaceChildren();
  }
  const pages = { dashboard, instances, images, snapshots, volumes };
  if (route.page === "instances" && route.arg) return instancePage(route.arg, route.tab || "overview", navigated);
  (pages[route.page] || dashboard)(navigated);
}

// --- charts -------------------------------------------------------------------------------
// Line chart over the last 10 minutes: 2px lines, recessive grid, crosshair + one
// tooltip listing every series, a legend and end-of-line labels for 2 series.
function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}
// Nice maximum in binary units (KiB, MiB, ...) so byte axes read 256 MiB, not 268 MB.
function niceMaxBytes(v) {
  if (!(v > 0)) return 1024;
  let u = 1;
  while (v / u >= 1024) u *= 1024;
  return niceMax(v / u) * u;
}
function chart(title, series, format, opts = {}) {
  const wrap = h("div", { class: "card chart" });
  const current = h("span", { class: "current" });
  wrap.append(h("div", { class: "title" }, h("h2", {}, title), current));
  const svg = s("svg", { role: "img", "aria-label": title });
  wrap.append(svg);
  if (series.length > 1) {
    wrap.append(h("div", { class: "legend" }, series.map((sr, i) =>
      h("span", {}, h("span", { class: "key", style: `background: var(--series-${i + 1})` }), sr.name))));
  }
  const tip = h("div", { class: "tooltip", hidden: true });
  wrap.append(tip);

  const last = series.map((sr) => [...sr.points].reverse().find((p) => p.v != null));
  current.textContent = series.map((sr, i) => (series.length > 1 ? `${sr.name} ${format(last[i]?.v)}` : format(last[i]?.v))).join(" · ");

  requestAnimationFrame(() => {
    const W = Math.max(svg.clientWidth || 360, 200), H = 150;
    const m = { l: 52, r: series.length > 1 ? 70 : 12, t: 8, b: 20 };
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const now = Date.now() / 1000, t0 = now - 600;
    const vals = series.flatMap((sr) => sr.points.map((p) => p.v)).filter((v) => v != null);
    const top = Math.max(opts.min || 0, ...vals, 0) * 1.05;
    const ymax = opts.binary ? niceMaxBytes(top) : niceMax(top);
    const x = (t) => m.l + ((t - t0) / (now - t0)) * (W - m.l - m.r);
    const y = (v) => H - m.b - (v / ymax) * (H - m.t - m.b);
    for (const f of [0, 0.5, 1]) {
      svg.append(s("line", { class: "gridline", x1: m.l, x2: W - m.r, y1: y(ymax * f), y2: y(ymax * f) }));
      svg.append(s("text", { class: "axis", x: m.l - 6, y: y(ymax * f) + 4, "text-anchor": "end" }, format(ymax * f)));
    }
    for (const [label, t] of [["10 min ago", t0], ["5 min", now - 300], ["now", now]]) {
      svg.append(s("text", { class: "axis", x: x(t), y: H - 4,
        "text-anchor": t === t0 ? "start" : t === now ? "end" : "middle" }, label));
    }
    // end-of-line labels, pushed apart so they never overlap (13px apart)
    const labels = series.length > 1 ? series.map((sr, i) => ({ name: sr.name, y: last[i] ? y(last[i].v) + 4 : null }))
      .filter((l) => l.y != null).sort((a, b) => a.y - b.y) : [];
    for (let i = 1; i < labels.length; i++) labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 13);
    const over = labels.length ? labels[labels.length - 1].y - (H - m.b + 4) : 0;
    if (over > 0) labels.forEach((l) => (l.y -= over));
    series.forEach((sr, i) => {
      let d = "", pen = false;
      for (const p of sr.points) {
        if (p.v == null || p.t < t0) { pen = false; continue; }
        d += `${pen ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`;
        pen = true;
      }
      if (d) svg.append(s("path", { class: `line s${i + 1}`, d }));
    });
    for (const l of labels) svg.append(s("text", { class: "dlabel", x: W - m.r + 6, y: l.y }, l.name));
    // hover layer: crosshair snaps to the nearest sample; one tooltip for all series
    const hair = s("line", { class: "hair", y1: m.t, y2: H - m.b, visibility: "hidden" });
    const dots = series.map((_, i) => s("circle", { class: `dot${i + 1}`, r: 4, visibility: "hidden" }));
    svg.append(hair, ...dots);
    const hit = s("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" });
    svg.append(hit);
    const times = series[0].points.map((p) => p.t).filter((t) => t >= t0);
    hit.addEventListener("pointermove", (ev) => {
      if (!times.length) return;
      const r = svg.getBoundingClientRect();
      const px = ((ev.clientX - r.left) / r.width) * W;
      const tt = t0 + ((px - m.l) / (W - m.l - m.r)) * (now - t0);
      const t = times.reduce((a, b) => (Math.abs(b - tt) < Math.abs(a - tt) ? b : a));
      hair.setAttribute("x1", x(t)); hair.setAttribute("x2", x(t)); hair.setAttribute("visibility", "visible");
      tip.replaceChildren(h("div", { class: "t" }, new Date(t * 1000).toLocaleTimeString()));
      series.forEach((sr, i) => {
        const p = sr.points.find((q) => q.t === t);
        if (p && p.v != null) {
          dots[i].setAttribute("cx", x(t)); dots[i].setAttribute("cy", y(p.v)); dots[i].setAttribute("visibility", "visible");
        } else dots[i].setAttribute("visibility", "hidden");
        tip.append(h("div", { class: "r" }, h("span", { class: "key", style: `background: var(--series-${i + 1})` }),
          h("strong", {}, format(p?.v)), series.length > 1 ? h("span", {}, sr.name) : ""));
      });
      tip.hidden = false;
      const left = (x(t) / W) * r.width;
      tip.style.left = `${Math.min(left + 12, r.width - tip.offsetWidth)}px`;
      tip.style.top = "36px";
    });
    hit.addEventListener("pointerleave", () => {
      tip.hidden = true; hair.setAttribute("visibility", "hidden");
      dots.forEach((d) => d.setAttribute("visibility", "hidden"));
    });
  });
  return wrap;
}

// --- dashboard -------------------------------------------------------------------------------
function tile(label, value, note) {
  return h("div", { class: "card tile" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value),
    note ? h("div", { class: "note" }, note) : "");
}
function dashboard() {
  const hst = state.host;
  if (!hst) return;
  const running = state.vms.filter((v) => v.state === "running");
  const hl = hst.history[hst.history.length - 1] || {};
  const memUsed = hl.mem_total - hl.mem_available;
  const vmRss = running.reduce((a, v) => a + (v.mem_used_bytes || 0), 0);
  const vmAlloc = running.reduce((a, v) => a + v.mem_mib * 1048576, 0);
  const cpuOf = (name) => hst.vm_history[name]?.[0]?.cpu_pct;
  $("main").replaceChildren(
    h("h1", {}, "Dashboard"),
    h("p", { class: "sub" }, "This host and the microVMs on it. Updates every 2 seconds."),
    h("div", { class: "grid tiles" },
      tile("Instances", `${running.length} running`, `${state.vms.length} total`),
      tile("Host CPU", pct(hl.cpu_pct), `load ${hst.load.map((l) => l.toFixed(2)).join(" ")}`),
      tile("Host memory", bytes(memUsed), `of ${bytes(hl.mem_total)}`),
      tile("VM memory in use", bytes(vmRss), `${bytes(vmAlloc)} allocated to running VMs`),
      tile("Disk free", bytes(hst.disk_free), `of ${bytes(hst.disk_total)}`),
      tile("Network slots", `${hst.taps_full + hst.taps_restricted}`, `${hst.taps_full} full · ${hst.taps_restricted} restricted`)),
    h("div", { class: "grid charts" },
      chart("Host CPU", [{ name: "CPU", points: hst.history.map((p) => ({ t: p.t, v: p.cpu_pct })) }], pct, { min: 10 }),
      chart("Host memory used", [{ name: "used", points: hst.history.map((p) => ({ t: p.t, v: p.mem_total - p.mem_available })) }], bytes, { binary: true })),
    h("div", { class: "card", style: "margin-top:14px" }, h("h2", {}, "Running instances"),
      running.length ? h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Image"), h("th", {}, "IP"),
          h("th", { class: "num" }, "CPU"), h("th", { class: "num" }, "Memory (used / allocated)"), h("th", {}, "Network"))),
        h("tbody", {}, running.map((v) => h("tr", {},
          h("td", {}, h("a", { href: `#/instances/${encodeURIComponent(v.name)}` }, v.name)),
          h("td", {}, v.image), h("td", { class: "mono" }, v.ip || "–"),
          h("td", { class: "num" }, pct(cpuOf(v.name))),
          h("td", { class: "num" }, `${bytes(v.mem_used_bytes)} / ${v.mem_mib} MiB`),
          h("td", {}, netLabel(v))))))
        : h("div", { class: "empty" }, "No running instances. ", h("a", { href: "#", onclick: (e) => { e.preventDefault(); launchDialog(); } }, "Launch one"), ".")));
}

// --- instances -----------------------------------------------------------------------------
function badge(st) {
  return h("span", { class: `badge ${st}` }, h("span", { class: "dot" }), st);
}
function vmActions(v, compact) {
  const b = (label, fn, cls) => h("button", { class: `${compact ? "small " : ""}${cls || ""}`, onclick: fn }, label);
  const name = v.name;
  const out = [];
  if (v.state === "running") {
    out.push(b("Shell", () => (location.hash = `#/instances/${encodeURIComponent(name)}/shell`)));
    out.push(b("Stop", () => act(`Stopped ${name}`, () => api("POST", `vms/${name}/stop`))));
  } else {
    out.push(b("Start", () => act(`Started ${name}`, () => api("POST", `vms/${name}/start`))));
  }
  out.push(b("Delete", () => {
    if (confirm(`Delete ${name}? Its writable layer is removed (images and volumes are kept).`)) {
      act(`Deleted ${name}`, () => api("DELETE", `vms/${name}`)).then(() => {
        if (route.arg === name) location.hash = "#/instances";
      });
    }
  }, "danger"));
  return out;
}
function instances() {
  $("main").replaceChildren(
    h("div", { class: "head" }, h("div", {}, h("h1", {}, "Instances"),
      h("p", { class: "sub" }, "Firecracker microVMs. App VMs run their image's command; system VMs boot systemd.")),
      h("span", { class: "spacer" }), h("button", { class: "primary", onclick: () => launchDialog() }, "+ Launch instance")),
    h("div", { class: "card" }, state.vms.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Name", "State", "Image", "IP", "vCPU", "Memory", "Network", "Created", ""].map((c, i) =>
        h("th", { class: i === 4 ? "num" : "" }, c)))),
      h("tbody", {}, state.vms.map((v) => h("tr", {},
        h("td", {}, h("a", { href: `#/instances/${encodeURIComponent(v.name)}` }, v.name)),
        h("td", {}, badge(v.state === "exited" ? `exited` : v.state), v.exit_code != null ? h("span", { class: "hint" }, ` (${v.exit_code})`) : ""),
        h("td", {}, v.image, " ", h("span", { class: "tag" }, v.type)),
        h("td", { class: "mono" }, v.ip || "–"),
        h("td", { class: "num" }, v.vcpus),
        h("td", { class: "nowrap" }, v.state === "running" ? `${bytes(v.mem_used_bytes)} / ${v.mem_mib} MiB` : `${v.mem_mib} MiB`),
        h("td", {}, netLabel(v)),
        h("td", { class: "nowrap" }, ago(v.created)),
        h("td", { class: "actions" }, h("div", { class: "row", style: "justify-content:flex-end" }, vmActions(v, true)))))))
      : h("div", { class: "empty" }, "No instances yet.")));
}

let statsTable = false;
async function instancePage(name, tab, navigated) {
  const v = state.vms.find((x) => x.name === name);
  if (!v) {
    $("main").replaceChildren(h("div", { class: "card empty" }, `No instance named ${name}. `, h("a", { href: "#/instances" }, "Back to instances")));
    return;
  }
  const tabs = ["overview", "shell", "console", "logs", "network"];
  let body = $("instance-body");
  if (navigated || !body) {
    body = h("div", { id: "instance-body" });
    $("main").replaceChildren(
      h("div", { class: "head" }, h("div", {}, h("h1", { id: "vm-title" }), h("p", { class: "sub", id: "vm-sub" })),
        h("span", { class: "spacer" }), h("div", { class: "row", id: "vm-actions" })),
      h("div", { class: "tabs" }, tabs.map((t) => h("button", {
        class: t === tab ? "active" : "", onclick: () => (location.hash = `#/instances/${encodeURIComponent(name)}/${t}`),
      }, t[0].toUpperCase() + t.slice(1)))),
      body);
  }
  $("vm-title").replaceChildren(v.name, " ", badge(v.state));
  $("vm-sub").textContent = `${v.image} · ${v.type} · ${v.vcpus} vCPU · ${v.mem_mib} MiB · ${v.ip || "no network"}`;
  const extra = [];
  if (v.state === "running") {
    extra.push(h("button", { onclick: () => act(`Restarted ${name}`, () => api("POST", `vms/${name}/restart`)) }, "Restart"));
    extra.push(h("button", { onclick: () => {
      const snap = prompt("Snapshot name (the VM keeps running; it pauses about a second):", `${name}-snap`);
      if (snap) act(`Snapshot ${snap} saved`, () => api("POST", `vms/${name}/snapshot`, { name: snap }));
    } }, "Snapshot"));
  } else {
    extra.push(h("button", { onclick: () => {
      const img = prompt("Save this VM's changes as a new image named:", `${name}-image`);
      if (img) act(`Image ${img} created`, () => api("POST", `vms/${name}/commit`, { image: img }));
    } }, "Commit to image"));
  }
  $("vm-actions").replaceChildren(...extra, ...vmActions(v, false));

  if (tab === "overview") {
    const stats = await api("GET", `vms/${name}/stats`).catch(() => ({ samples: [] }));
    const sm = stats.samples;
    const pts = (k) => sm.map((p) => ({ t: p.t, v: p[k] }));
    const details = h("dl", { class: "kv" },
      ...[["Image chain", (v.image_chain || []).join(" → ")], ["Type", v.type], ["IP", v.ip || "–"],
        ["Network", netLabel(v)], ["Ports", (v.ports || []).join(", ") || "–"],
        ["Volumes", [...(v.volumes || []), ...(v.shares || []).map((x) => `${x.host} → ${x.path}${x.ro ? " (ro)" : ""}`)].join(", ") || "–"],
        ["Disk", `${bytes(v.disk_used_bytes)} (${v.disk_mode})`], ["Process", v.pid ? `pid ${v.pid}` : "–"],
        ["Created", v.created ? new Date(v.created).toLocaleString() : "–"],
        ...(v.restored_from ? [["Forked from", v.restored_from]] : [])].flatMap(([k, val]) => [h("dt", {}, k), h("dd", {}, val)]));
    const table = statsTable ? h("div", { class: "card", style: "margin-top:14px" }, h("h2", {}, "Recent samples"),
      h("table", {}, h("thead", {}, h("tr", {}, ["Time", "CPU", "Memory", "Disk read", "Disk write", "Net in", "Net out"].map((c, i) => h("th", { class: i ? "num" : "" }, c)))),
        h("tbody", {}, sm.slice(-15).reverse().map((p) => h("tr", {}, h("td", {}, new Date(p.t * 1000).toLocaleTimeString()),
          h("td", { class: "num" }, pct(p.cpu_pct)), h("td", { class: "num" }, bytes(p.rss)),
          h("td", { class: "num" }, rate(p.disk_read_bps)), h("td", { class: "num" }, rate(p.disk_write_bps)),
          h("td", { class: "num" }, rate(p.net_rx_bps)), h("td", { class: "num" }, rate(p.net_tx_bps))))))) : "";
    body.replaceChildren(
      h("div", { class: "grid", style: "grid-template-columns: minmax(280px, 1fr) 2fr; align-items:start" },
        h("div", { class: "card" }, h("h2", {}, "Details"), details),
        v.state === "running" ? h("div", {},
          h("div", { class: "grid charts" },
            chart("CPU", [{ name: "CPU", points: pts("cpu_pct") }], pct, { min: 10 }),
            chart("Memory in use", [{ name: "memory", points: pts("rss") }], bytes, { binary: true, min: 64 * 1048576 }),
            chart("Disk I/O", [{ name: "read", points: pts("disk_read_bps") }, { name: "write", points: pts("disk_write_bps") }], rate, { binary: true, min: 64 * 1024 }),
            chart("Network", [{ name: "in", points: pts("net_rx_bps") }, { name: "out", points: pts("net_tx_bps") }], rate, { binary: true, min: 64 * 1024 })),
          h("div", { class: "row", style: "margin-top:8px" }, h("button", { class: "small", onclick: () => { statsTable = !statsTable; render(false); } },
            statsTable ? "Hide table" : "Show as table"),
            h("span", { class: "hint" }, "CPU is of one core (100% = one vCPU busy). Memory is what the host actually backs.")),
          table)
          : h("div", { class: "card empty" }, "Stats appear while the instance is running.")));
  } else if (tab === "shell" || tab === "console") {
    if (!navigated && $("term")) return;       // keep the live terminal across refreshes
    if (v.state !== "running") {
      body.replaceChildren(h("div", { class: "card empty" }, "The instance is not running."));
      return;
    }
    const note = tab === "shell"
      ? `An interactive shell${v.type === "app" ? " as the image's user" : " as root"}, over the exec agent (like fcvm shell). Exiting it leaves the VM running.`
      : "The serial console (like fcvm console): boot messages and, for system images, a login prompt. Keystrokes go to the VM.";
    const user = h("input", { placeholder: "user (optional)", size: 14 });
    const box = h("div", { class: "term-wrap", id: "term" });
    const reconnect = h("button", { class: "small", onclick: () => { cleanup.forEach((f) => f()); cleanup = []; start(); } }, "Reconnect");
    body.replaceChildren(h("div", { class: "term-bar" }, note, h("span", { class: "spacer", style: "flex:1" }),
      tab === "shell" ? user : "", reconnect), box);
    const start = () => cleanup.push(terminal(box, `/ws/vms/${encodeURIComponent(name)}/${tab}`, tab === "shell", user.value.trim()));
    start();
  } else if (tab === "logs") {
    const r = await api("GET", `vms/${name}/logs`).catch((e) => ({ log: e.message }));
    const pre = $("logpre") || h("pre", { class: "log", id: "logpre" });
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent = r.log.replace(/\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)?|\r/g, "") || "(no output yet)";
    if (!body.contains(pre)) body.replaceChildren(h("p", { class: "hint" }, "Console output of the current or last boot."), pre);
    if (atBottom || navigated) pre.scrollTop = pre.scrollHeight;
  } else if (tab === "network") {
    const r = v.net?.mode === "restricted" ? await api("GET", `vms/${name}/egress`).catch((e) => ({ text: e.message })) : null;
    body.replaceChildren(h("div", { class: "card" }, h("h2", {}, "Network"),
      h("dl", { class: "kv" }, h("dt", {}, "Mode"), h("dd", {}, v.net?.mode || "full"), h("dt", {}, "IP"), h("dd", {}, v.ip || "–"),
        h("dt", {}, "Published ports"), h("dd", {}, (v.ports || []).join(", ") || "–")),
      r ? [h("h2", { style: "margin-top:16px" }, "Egress policy and recent requests"), h("pre", { class: "log" }, r.text)]
        : h("p", { class: "hint" }, v.net?.mode === "none" ? "This instance has no network card."
          : "Full network: outbound traffic goes through NAT, unfiltered. Create an instance with an allowlist to see an egress log here.")));
  }
}

// --- terminals ------------------------------------------------------------------------------
function terminal(box, url, isShell, user) {
  box.replaceChildren();
  const cs = getComputedStyle(document.documentElement);
  const term = new Terminal({ cursorBlink: true, fontFamily: cs.getPropertyValue("--mono"), fontSize: 13,
    theme: { background: "#0f0f0e" }, scrollback: 5000 });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(box);
  fit.fit();
  const ws = new WebSocket(`ws://${location.host}${url}`);
  ws.binaryType = "arraybuffer";
  const enc = new TextEncoder();
  ws.onopen = () => {
    if (isShell) ws.send(JSON.stringify({ rows: term.rows, cols: term.cols, user }));
    else term.write("\x1b[2m[serial console connected: recent output first, then live. An idle app VM prints nothing here; use the Shell tab to get a prompt.]\x1b[0m\r\n");
    term.focus();
  };
  ws.onmessage = (ev) => term.write(typeof ev.data === "string" ? ev.data : new Uint8Array(ev.data));
  ws.onclose = () => term.write("\r\n\x1b[2m[disconnected]\x1b[0m\r\n");
  term.onData((d) => ws.readyState === 1 && ws.send(enc.encode(d)));
  term.onResize(({ rows, cols }) => isShell && ws.readyState === 1 && ws.send(JSON.stringify({ resize: [rows, cols] })));
  const ro = new ResizeObserver(() => fit.fit());
  ro.observe(box);
  return () => { ro.disconnect(); ws.close(); term.dispose(); };
}

// --- images, snapshots, volumes -------------------------------------------------------------
function images() {
  const ref = h("input", { placeholder: "nginx:latest, ghcr.io/org/app:tag, or a local .tar / OCI path", style: "flex:1; min-width:280px" });
  const nm = h("input", { placeholder: "name (optional)", size: 18 });
  const recent = state.jobs.filter((j) => j.kind === "import").slice(0, 3);
  if (!$("img-form")) {
    $("main").replaceChildren(
      h("h1", {}, "Images"),
      h("p", { class: "sub" }, "Every image boots as a Firecracker microVM. Imported container images are app images; the Ubuntu base is a system image."),
      h("div", { class: "card", id: "img-form", style: "margin-bottom:14px" }, h("h2", {}, "Import an image"),
        h("div", { class: "row" }, ref, nm, h("button", { class: "primary", onclick: () => {
          if (!ref.value.trim()) return;
          act(`Importing ${ref.value.trim()}…`, () => api("POST", "images", { ref: ref.value.trim(), name: nm.value.trim() }));
          ref.value = ""; nm.value = "";
        } }, "Import")),
        h("div", { id: "img-jobs", style: "margin-top:8px" })),
      h("div", { class: "card", id: "img-table" }));
  }
  $("img-jobs").replaceChildren(...recent.map((j) => h("div", { class: "row" },
    badge(j.status), h("span", {}, j.title), h("span", { class: "progress" }, j.log[j.log.length - 1] || ""))));
  $("img-table").replaceChildren(state.images.length ? h("table", {},
    h("thead", {}, h("tr", {}, ["Name", "Type", "Size", "Source", "Used by", ""].map((c, i) => h("th", { class: i === 2 ? "num" : "" }, c)))),
    h("tbody", {}, state.images.map((im) => h("tr", {},
      h("td", {}, im.name), h("td", {}, h("span", { class: "tag" }, im.type)),
      h("td", { class: "num" }, bytes(im.disk_used_bytes)),
      h("td", {}, im.parent ? `layer on ${im.parent}` : im.ref || "–"),
      h("td", {}, im.used_by.length ? im.used_by.join(", ") : h("span", { class: "hint" }, "–")),
      h("td", { class: "actions" }, h("div", { class: "row", style: "justify-content:flex-end" },
        h("button", { class: "small", onclick: () => launchDialog(im.name) }, "Launch"),
        h("button", { class: "small danger", disabled: im.used_by.length > 0, onclick: () => {
          if (confirm(`Delete image ${im.name}?`)) act(`Deleted ${im.name}`, () => api("DELETE", `images/${im.name}`));
        } }, "Delete")))))))
    : h("div", { class: "empty" }, "No images. Import one above."));
}

function snapshots() {
  $("main").replaceChildren(
    h("h1", {}, "Snapshots"),
    h("p", { class: "sub" }, "A snapshot holds a running VM's memory, processes and disk. Forks start from that exact moment in about 0.1 s."),
    h("div", { class: "card" }, state.snapshots.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Name", "Source", "Image", "Memory on disk", "Disk", "Created", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, state.snapshots.map((sn) => h("tr", {},
        h("td", {}, sn.name), h("td", {}, sn.source), h("td", {}, sn.image),
        h("td", {}, `${bytes(sn.mem_bytes_on_disk)} of ${sn.mem_mib} MiB`), h("td", {}, bytes(sn.disk_bytes)),
        h("td", {}, ago(sn.created)),
        h("td", { class: "actions" }, h("div", { class: "row", style: "justify-content:flex-end" },
          h("button", { class: "small", onclick: () => {
            const n = prompt("How many forks?", "1");
            if (n && +n > 0) act(`Forked ${n} from ${sn.name}`, () => api("POST", `snapshots/${sn.name}/fork`, { count: +n }));
          } }, "Fork"),
          h("button", { class: "small danger", onclick: () => {
            if (confirm(`Delete snapshot ${sn.name}? Running forks are not affected.`)) act(`Deleted ${sn.name}`, () => api("DELETE", `snapshots/${sn.name}`));
          } }, "Delete")))))))
      : h("div", { class: "empty" }, "No snapshots. Open a running instance and choose Snapshot.")));
}

function volumes() {
  $("main").replaceChildren(
    h("h1", {}, "Volumes"),
    h("p", { class: "sub" }, "Named persistent disks, attached at launch as NAME:/path. They outlive the instances that use them."),
    h("div", { class: "card" }, state.volumes.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Name", "Size", "Used", "Instances", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, state.volumes.map((vo) => h("tr", {},
        h("td", {}, vo.name), h("td", {}, bytes(vo.size_bytes)), h("td", {}, bytes(vo.used_bytes)),
        h("td", {}, vo.vms.join(", ") || "–"),
        h("td", { class: "actions" }, h("button", { class: "small danger", disabled: vo.vms.length > 0, onclick: () => {
          if (confirm(`Delete volume ${vo.name} and its data?`)) act(`Deleted ${vo.name}`, () => api("DELETE", `volumes/${vo.name}`));
        } }, "Delete"))))))
      : h("div", { class: "empty" }, "No volumes. Add one at launch, e.g. data:/var/lib/data.")));
}

// --- launch dialog --------------------------------------------------------------------------
async function launchDialog(image) {
  if (!state.presets.length) state.presets = await api("GET", "presets").catch(() => []);
  const imgs = state.images;
  if (!imgs.length) { toast("Import an image first.", true); return; }
  const sel = h("select", {}, imgs.map((im) => h("option", { value: im.name, selected: im.name === image }, `${im.name} (${im.type})`)));
  const name = h("input", { required: true, placeholder: "e.g. web-1" });
  const vcpus = h("input", { type: "number", min: 1, max: 32, value: 2 });
  const mem = h("input", { type: "number", min: 128, step: 128, value: 1024 });
  let net = "full";
  const allow = new Set();
  const custom = h("input", { placeholder: "extra hosts: example.com, *.corp.example" });
  const netBox = h("div", {});
  const netRadios = h("div", { class: "chips" }, ["full", "restricted", "none"].map((n) =>
    h("button", { type: "button", class: `chip${n === net ? " on" : ""}`, onclick: (e) => {
      net = n; for (const c of netRadios.children) c.classList.toggle("on", c === e.currentTarget); drawNet();
    } }, { full: "Full (NAT)", restricted: "Allowlist", none: "No network" }[n])));
  function drawNet() {
    netBox.replaceChildren(net !== "restricted" ? h("span", { class: "hint" },
      net === "full" ? "Outbound traffic to anywhere, through NAT." : "No network card at all. exec/shell still work.") :
      h("div", { style: "display:grid; gap:8px" }, h("div", { class: "chips" }, state.presets.map((p) =>
        h("button", { type: "button", class: `chip${allow.has(p.name) ? " on" : ""}`, title: p.hosts.join(" "),
          onclick: (e) => { allow.has(p.name) ? allow.delete(p.name) : allow.add(p.name); e.currentTarget.classList.toggle("on"); } }, p.name))),
        custom));
  }
  drawNet();
  const ports = h("input", { placeholder: "8080:80, 127.0.0.1:5432:5432" });
  const vols = h("textarea", { rows: 2, placeholder: "data:/var/lib/data\n/home/me/project:/work" });
  const mode = h("select", {}, h("option", { value: "image" }, "Run the image's command"),
    h("option", { value: "idle" }, "Stay idle (for shell / exec)"), h("option", { value: "custom" }, "Run a custom command"));
  const cmd = h("input", { placeholder: "shell command", disabled: true });
  mode.addEventListener("change", () => (cmd.disabled = mode.value !== "custom"));
  const appOnly = h("div", { class: "full", style: "display:grid; gap:6px" },
    h("label", {}, "Process", mode), cmd);
  const syncType = () => { appOnly.hidden = imgs.find((i) => i.name === sel.value)?.type !== "app"; };
  sel.addEventListener("change", syncType);
  syncType();
  const err = h("div", { class: "hint", style: "color: var(--critical)" });
  const go = h("button", { class: "primary", type: "submit" }, "Launch");
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    go.disabled = true; go.textContent = "Launching…"; err.textContent = "";
    const body = {
      name: name.value.trim(), image: sel.value, vcpus: +vcpus.value, mem_mib: +mem.value, network: net,
      allow: [...allow, ...custom.value.split(",").map((x) => x.trim()).filter(Boolean)],
      ports: ports.value.split(",").map((x) => x.trim()).filter(Boolean),
      volumes: vols.value.split("\n").map((x) => x.trim()).filter(Boolean),
      idle: mode.value === "idle", command: mode.value === "custom" ? cmd.value : "",
    };
    try {
      await api("POST", "vms", body);
      bd.remove();
      toast(`Launched ${body.name}`);
      location.hash = `#/instances/${encodeURIComponent(body.name)}`;
      refresh();
    } catch (x) {
      err.textContent = x.message; go.disabled = false; go.textContent = "Launch";
    }
  } },
    h("h1", {}, "Launch instance"), h("p", { class: "sub" }, "A new Firecracker microVM on this host."),
    h("div", { class: "form" },
      h("label", {}, "Name", name), h("label", {}, "Image", sel),
      h("label", {}, "vCPUs", vcpus), h("label", {}, "Memory (MiB)", mem),
      h("div", { class: "full" }, h("label", {}, "Network"), netRadios, h("div", { style: "margin-top:8px" }, netBox)),
      h("label", { class: "full" }, h("span", {}, "Published ports ", h("span", { class: "hint" }, "HOST:GUEST, comma-separated")), ports),
      h("label", { class: "full" }, h("span", {}, "Volumes and host directories ", h("span", { class: "hint" }, "one per line: NAME:/path or /host/dir:/path[:ro]")), vols),
      appOnly),
    err,
    h("div", { class: "foot" }, h("button", { type: "button", onclick: () => bd.remove() }, "Cancel"), go));
  const bd = h("div", { class: "backdrop", onclick: (e) => e.target === bd && bd.remove() }, h("div", { class: "card modal" }, form));
  document.body.append(bd);
  name.focus();
}

// --- boot -------------------------------------------------------------------------------------
$("launch").addEventListener("click", () => launchDialog());
$("theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("fcvm-theme", document.documentElement.dataset.theme); } catch (e) { /* private mode */ }
  render(true);
});
try { const t = localStorage.getItem("fcvm-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* ignore */ }
route = parseRoute();
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 2500);
