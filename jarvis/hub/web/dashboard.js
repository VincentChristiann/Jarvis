"use strict";
const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: {"X-Jarvis": "1", "Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { document.body.textContent = "Session expired. Run `jarvis hub dashboard` again."; throw new Error("auth"); }
  return r.json();
}

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), 3500);
}

function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) k === "class" ? (e.className = v) : (e[k] = v);
  for (const kid of kids) e.append(kid);
  return e;
}

function btn(label, cls, fn) {
  return el("button", {textContent: label, class: cls || "", onclick: fn});
}

async function sendCmd(id, cmd, risky, args) {
  if (risky && !confirm(`Run ${cmd} on this device?`)) return;
  const r = await api("POST", "/admin/send", {target: id, cmd, args: args || {}});
  toast(r.ok ? `${cmd}: ok` : `${cmd}: ${r.error}`);
}

function renderDevices(list) {
  const box = $("devices"); box.replaceChildren();
  if (!list.length) box.append(el("p", {class: "hint", textContent: "No devices yet. Pair one below."}));
  for (const d of list) {
    const row = el("div", {class: "row"},
      el("span", {class: "name"},
        el("span", {class: "dot" + (d.online ? " on" : "")}),
        el("b", {textContent: d.name}),
        el("span", {class: "hint", textContent: `  ${d.class} · ${d.role}${d.revoked ? " · REVOKED" : ""}`})));
    if (!d.revoked) {
      row.append(btn("Ping", "", () => sendCmd(d.id, "ping")));
      row.append(btn("⏯", "", () => sendCmd(d.id, "media.play_pause")));
      row.append(btn("⏭", "", () => sendCmd(d.id, "media.next")));
      row.append(btn("Vol −", "", () => sendCmd(d.id, "volume.step", false, {delta: -5})));
      row.append(btn("Vol +", "", () => sendCmd(d.id, "volume.step", false, {delta: 5})));
      row.append(btn("Lock", "", () => sendCmd(d.id, "system.lock", true)));
      row.append(btn("Revoke", "danger", async () => {
        if (!confirm(`Revoke ${d.name}? It will be disconnected and must re-pair.`)) return;
        await api("POST", "/admin/revoke", {id: d.id}); refresh();
      }));
    }
    box.append(row);
  }
}

function renderPending(list) {
  $("pending-card").hidden = !list.length;
  const box = $("pending"); box.replaceChildren();
  for (const p of list) {
    box.append(el("div", {class: "row"},
      el("span", {class: "name"}, el("b", {textContent: p.name}),
        el("span", {class: "hint", textContent: `  ${p.platform} · ${p.class} · ${p.role} · ${p.ip}`})),
      el("span", {class: "sas", textContent: p.sas}),
      btn("Approve", "go", async () => { await api("POST", "/admin/resolve", {id: p.id, approve: true}); refresh(); }),
      btn("Reject", "danger", async () => { await api("POST", "/admin/resolve", {id: p.id, approve: false}); refresh(); })));
  }
}

function renderConfirms(list) {
  $("confirm-card").hidden = !list.length;
  const box = $("confirms"); box.replaceChildren();
  for (const c of list) {
    box.append(el("div", {class: "row"},
      el("span", {class: "name", textContent: `${c.sender} wants to run ${c.cmd} on ${c.target}`}),
      btn("Allow", "go", async () => { await api("POST", "/admin/confirm", {id: c.id, approve: true}); refresh(); }),
      btn("Deny", "danger", async () => { await api("POST", "/admin/confirm", {id: c.id, approve: false}); refresh(); })));
  }
}

function renderGrants(list) {
  $("grant-card").hidden = !list.length;
  const box = $("grants"); box.replaceChildren();
  for (const g of list) {
    box.append(el("div", {class: "row"},
      el("span", {class: "name", textContent: `${g.sender} → ${g.target}: ${g.family}.*  (${Math.floor(g.expires_in / 60)}m ${g.expires_in % 60}s left)`}),
      btn("Close now", "danger", async () => { await api("POST", "/admin/grant-close", {id: g.id}); refresh(); })));
  }
}

function renderAudit(a) {
  const chain = $("chain");
  chain.textContent = a.ok ? "audit log intact" : "AUDIT LOG TAMPERED";
  chain.className = "pill " + (a.ok ? "ok" : "bad");
  const box = $("audit"); box.replaceChildren();
  for (const e of a.entries.slice().reverse()) {
    const {ts, event, ...rest} = e;
    const detail = Object.entries(rest).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");
    box.append(el("div", {textContent: `${new Date(ts * 1000).toLocaleTimeString()}  ${event}  ${detail}`}));
  }
}

async function refresh() {
  try {
    const [devs, pend, conf, audit, grants] = await Promise.all([
      api("GET", "/admin/devices"), api("GET", "/admin/pending"),
      api("GET", "/admin/confirmations"), api("GET", "/admin/audit"), api("GET", "/admin/grants")]);
    renderDevices(devs); renderPending(pend); renderConfirms(conf); renderAudit(audit); renderGrants(grants);
  } catch (e) { /* auth failure already shown */ }
}

let ttlTimer;
$("pair-btn").onclick = async () => {
  const r = await api("POST", "/admin/invite", {role: $("role").value, kind: $("kind").value});
  if (r.error) return toast(r.error);
  $("qr").src = r.qr; $("link").value = r.url; $("pair-out").hidden = false;
  $("ca-box").hidden = $("kind").value !== "web";
  $("ca-url").textContent = r.ca_url; $("ca-fp").value = r.ca_fp;
  let left = r.expires_in; $("ttl").textContent = left;
  clearInterval(ttlTimer);
  ttlTimer = setInterval(() => {
    left -= 1; $("ttl").textContent = Math.max(left, 0);
    if (left <= 0) { clearInterval(ttlTimer); $("pair-out").hidden = true; $("link").value = ""; }
  }, 1000);
};

refresh(); setInterval(refresh, 2000);
