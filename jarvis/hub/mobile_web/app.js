"use strict";
const $ = (id) => document.getElementById(id);
const enc = new TextEncoder();
const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
let me = null, selected = null, devices = [];

// ---- small helpers ---------------------------------------------------------
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), 3500);
}
function show(...ids) {
  for (const id of ["pair-view", "main-view", "help", "msg"]) $(id).hidden = !ids.includes(id);
}
function note(text) { $("msg").textContent = text; show("msg"); }
function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) k === "class" ? (e.className = v) : (e[k] = v);
  e.append(...kids); return e;
}
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"X-Jarvis": "1", "Content-Type": "application/json"},
                               body: JSON.stringify(body || {})});
  return {status: r.status, data: await r.json().catch(() => ({}))};
}
function allowed(cmd) {
  return (me?.can_send || []).some((p) => p === "*" || p === cmd || (p.endsWith("*") && cmd.startsWith(p.slice(0, -1))));
}

// ---- key storage (private key never leaves the browser) ---------------------------
function idb() {
  return new Promise((res, rej) => {
    const r = indexedDB.open("jarvis", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("kv");
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
}
async function kv(mode, fn) {
  const db = await idb();
  return new Promise((res, rej) => {
    const tx = db.transaction("kv", mode), req = fn(tx.objectStore("kv"));
    tx.oncomplete = () => res(req.result); tx.onerror = () => rej(tx.error);
  });
}
const loadDevice = () => kv("readonly", (s) => s.get("device"));
const saveDevice = (d) => kv("readwrite", (s) => s.put(d, "device"));

// ---- pairing --------------------------------------------------------------------
async function sasFor(token, spki) {
  const key = await crypto.subtle.importKey("raw", enc.encode(token), {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  const mac = new DataView(await crypto.subtle.sign("HMAC", key, spki));
  const n = mac.getUint32(0) % 1000000;
  return String(Math.floor(n / 1000)).padStart(3, "0") + "-" + String(n % 1000).padStart(3, "0");
}

async function doPair(token) {
  $("pair-go").disabled = true;
  const pair = await crypto.subtle.generateKey({name: "ECDSA", namedCurve: "P-256"}, false, ["sign", "verify"]);
  const spki = await crypto.subtle.exportKey("spki", pair.publicKey);
  $("sas").textContent = await sasFor(token, spki); $("sas-box").hidden = false;
  const r = await post("/m/pair", {token, pubkey: b64(spki), name: $("pair-name").value || "phone"});
  if (r.status !== 200) {
    $("pair-go").disabled = false; $("sas-box").hidden = true;
    return toast("Pairing failed: " + (r.data.error || r.status));
  }
  await saveDevice({device_id: r.data.device_id, privateKey: pair.privateKey, name: $("pair-name").value || "phone"});
  history.replaceState(null, "", "/m");
  start();
}

// ---- login (sign a challenge with the device key) -------------------------------------
async function login(dev) {
  const ch = await (await fetch("/m/challenge")).json();
  const msg = new Uint8Array([...enc.encode("jarvis-mobile-login\0" + ch.nonce + "\0" + dev.device_id)]);
  const sig = await crypto.subtle.sign({name: "ECDSA", hash: "SHA-256"}, dev.privateKey, msg);
  const r = await post("/m/login", {nonce: ch.nonce, device_id: dev.device_id, sig: b64(sig)});
  if (r.status !== 200) throw new Error(r.data.error || "login failed");
  return r.data;
}

async function api(method, path, body) {
  let r = method === "GET" ? await fetch(path) : await post(path, body);
  const status = r.status, data = r.data ?? (await r.json().catch(() => ({})));
  if (status === 401) {                       // session expired: sign in again silently, once
    const dev = await loadDevice(); me = await login(dev);
    r = method === "GET" ? await fetch(path) : await post(path, body);
    return r.data ?? (await r.json());
  }
  return data;
}

// ---- remote control -------------------------------------------------------------------
async function run(cmd, args) {
  if (!selected) return toast("Pick a device first");
  const r = await api("POST", "/m/api/send", {target: selected.id, cmd, args: args || {}});
  if (!r.ok) toast(r.error || "failed");
  return r;
}

const QUICK = [
  ["⏯ Play/Pause", "media.play_pause"], ["⏮ Prev", "media.previous"], ["⏭ Next", "media.next"],
  ["Vol −", "volume.step", {delta: -5}], ["Vol +", "volume.step", {delta: 5}], ["Mute", "volume.mute"],
  ["Ping", "ping"],
];
const KEYS = [["Enter", "enter"], ["⌫", "backspace"], ["Esc", "escape"], ["Tab", "tab"],
              ["←", "left"], ["↑", "up"], ["↓", "down"], ["→", "right"], ["Ctrl+C", "ctrl+c"], ["Ctrl+V", "ctrl+v"]];

function renderDevices() {
  const box = $("devices"); box.replaceChildren();
  if (!devices.length) box.append(el("p", {class: "hint", textContent: "No other devices are paired yet."}));
  for (const d of devices) {
    box.append(el("button", {class: "dev" + (selected?.id === d.id ? " sel" : ""), onclick: () => { selected = d; renderDevices(); renderRemote(); }},
      el("span", {class: "dot" + (d.online ? " on" : "")}), el("b", {textContent: d.name}),
      el("span", {class: "hint", textContent: d.online ? "online" : "offline"})));
  }
}
function renderRemote() {
  $("remote").hidden = !selected;
  if (!selected) return;
  $("remote-title").textContent = selected.name;
  const q = $("quick"); q.replaceChildren();
  for (const [label, cmd, args] of QUICK) q.append(el("button", {textContent: label, disabled: !allowed(cmd), onclick: () => run(cmd, args)}));
  const k = $("keys"); k.replaceChildren();
  for (const [label, key] of KEYS) k.append(el("button", {textContent: label, disabled: !allowed("input.key"), onclick: () => run("input.key", {key})}));
  for (const b of document.querySelectorAll("[data-cmd]")) { b.disabled = !allowed(b.dataset.cmd); b.onclick = () => run(b.dataset.cmd, JSON.parse(b.dataset.args)); }
  $("send-text").disabled = !allowed("input.type");
}
async function refreshDevices() {
  const r = await api("GET", "/m/api/devices");
  me = r.me; devices = r.devices;
  if (selected) selected = devices.find((d) => d.id === selected.id) || null;
  $("who").textContent = me.name;
  renderDevices(); renderRemote();
}

// touchpad: drag = move, tap = click. Moves are batched so we send ~25 commands/second at most.
function setupPad() {
  const pad = $("pad"); let last = null, moved = 0, t0 = 0, dx = 0, dy = 0, busy = false;
  const flush = async () => {
    if (busy || (!dx && !dy)) return;
    busy = true; const mx = dx, my = dy; dx = dy = 0;
    await run("input.move", {dx: mx, dy: my}); busy = false;
  };
  pad.onpointerdown = (e) => { pad.setPointerCapture(e.pointerId); last = [e.clientX, e.clientY]; moved = 0; t0 = Date.now(); };
  pad.onpointermove = (e) => {
    if (!last) return;
    const ddx = Math.round((e.clientX - last[0]) * 2), ddy = Math.round((e.clientY - last[1]) * 2);
    last = [e.clientX, e.clientY]; moved += Math.abs(ddx) + Math.abs(ddy); dx += ddx; dy += ddy;
    if (!pad.timer) pad.timer = setTimeout(() => { pad.timer = null; flush(); }, 40);
  };
  pad.onpointerup = () => { if (last && moved < 8 && Date.now() - t0 < 250) run("input.click", {button: "left"}); last = null; };
  pad.onpointercancel = () => { last = null; };
}

// ---- voice ----------------------------------------------------------------------------------
let voice = null;
async function setupVoice() {
  const st = await (await fetch("/m/api/voice/status")).json().catch(() => ({}));
  if (!st.available) { $("ptt").disabled = $("hf").disabled = true; $("voice-line").textContent = "Voice is not available on this hub (no speech engine installed)."; return; }
  const {Voice} = await import("/voice/voice.js");
  const line = (t) => { $("voice-line").textContent = t; };
  voice = new Voice({
    endpoint: "/m/api/voice",
    setSession: async (on, renew) => (await post("/m/api/voice/session", {on, renew: !!renew})).status === 200,
    getTarget: () => selected?.id,
    onLevel: (l) => { $("level").style.width = Math.min(100, l * 400) + "%"; },
    onState: (s) => {
      $("ptt").classList.toggle("live", s.on && s.mode === "ptt"); $("hf").classList.toggle("live", s.on && s.mode === "hands-free");
      $("hf").textContent = s.on && s.mode === "hands-free" ? "Stop listening" : "Hands-free";
      if (s.on) line(s.mode === "ptt" ? "Listening…" : 'Listening. Say "Jarvis, …"'); else { $("level").style.width = "0"; if (s.why && s.why !== "done") line("Microphone off: " + s.why); }
    },
    onResult: (r) => line((r.heard ? `Heard: "${r.heard}" — ` : "") + (r.reply || "")),
  });
  $("speak").onchange = () => { voice.speak = $("speak").checked; };
  const ptt = $("ptt");
  ptt.onpointerdown = (e) => { e.preventDefault(); try { ptt.setPointerCapture(e.pointerId); } catch (_) { /* some browsers refuse; releasing still works */ } voice.pttDown().catch((err) => line("Microphone: " + err.message)); };
  ptt.onpointerup = ptt.onpointercancel = () => voice.pttUp();
  ptt.oncontextmenu = (e) => e.preventDefault();
  $("hf").onclick = () => (voice.mode === "hands-free" ? voice.stop("stopped") : voice.startHandsFree().catch((err) => line("Microphone: " + err.message)));
}

// ---- start ----------------------------------------------------------------------------
async function start() {
  if (!window.isSecureContext || !crypto?.subtle) {
    note("This page needs a trusted HTTPS connection. Install the hub's certificate on this phone first.");
    return show("msg", "help");
  }
  const token = new URLSearchParams(location.hash.slice(1)).get("pair");
  const dev = await loadDevice();
  if (token && !dev) {
    $("pair-name").value = /iPhone|iPad/.test(navigator.userAgent) ? "iPhone" : /Android/.test(navigator.userAgent) ? "Android phone" : "phone";
    $("pair-go").onclick = () => doPair(token);
    return show("pair-view", "help");
  }
  if (!dev) { note("This phone is not paired. Scan the pairing QR code on the hub's dashboard."); return show("msg", "help"); }
  try { me = await login(dev); } catch (e) { return note("Sign-in failed: " + e.message + ". This phone may have been revoked on the hub."); }
  $("logout").hidden = false;
  show("main-view");
  setupVoice().catch(() => {});
  await refreshDevices();
  setInterval(() => refreshDevices().catch(() => {}), 5000);
}

$("logout").onclick = async () => { await voice?.stop("signed out"); await post("/m/api/logout"); location.reload(); };
$("send-text").onclick = async () => { const t = $("text"); if (t.value && (await run("input.type", {text: t.value})).ok) t.value = ""; };
setupPad();
start();
