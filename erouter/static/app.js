"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
let timers = [];
let saved = [];

async function api(path, body, timeoutMs = 25000) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);      // jamais de requête suspendue indéfiniment
  let r;
  try { r = await fetch(path, {...opt, signal: ctl.signal}); }
  catch (e) {
    throw new Error("Le routeur ne répond pas.");
  } finally { clearTimeout(timer); }
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== "/api/login") { boot(); throw new Error("Session expirée"); }
  if (!r.ok) throw new Error(j.error || "Erreur " + r.status);
  return j;
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 3500);
}
const kv = rows => rows.map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join("");
const fmtUp = s => s > 86400 ? Math.floor(s / 86400) + " j " + Math.floor(s % 86400 / 3600) + " h"
  : s > 3600 ? Math.floor(s / 3600) + " h " + Math.floor(s % 3600 / 60) + " min" : Math.floor(s / 60) + " min";
const bars = p => `<span class="bars" aria-label="signal ${p} %">${[25, 50, 75, 100].map((t, i) =>
  `<i class="h${i + 1}${p >= t - 12 ? " on" : ""}"></i>`).join("")}</span>`;

async function boot() {
  timers.forEach(clearInterval); timers = [];
  const s = await fetch("/api/session").then(r => r.json());
  $("#login").hidden = s.authenticated; $("#app").hidden = !s.authenticated;
  if (!s.authenticated) {
    $("#loginHint").textContent = s.setup_needed
      ? "Premier démarrage : choisis un mot de passe administrateur (8 caractères min.)."
      : "Connecte-toi pour administrer le routeur.";
    $("#loginPw").autocomplete = s.setup_needed ? "new-password" : "current-password";
    return;
  }
  await Promise.allSettled([refresh(), loadSettings(), loadNetworks(false)]);
  document.dispatchEvent(new CustomEvent("erouter:ready"));
  timers.push(setInterval(refresh, 5000), setInterval(() => loadNetworks(false), 15000));
}

$("#loginForm").onsubmit = async e => {
  e.preventDefault(); $("#loginErr").textContent = "";
  try { await api("/api/login", {password: $("#loginPw").value}); $("#loginPw").value = ""; boot(); }
  catch (err) { $("#loginErr").textContent = err.message; }
};
$("#logoutBtn").onclick = async () => { await api("/api/logout", {}); boot(); };

let ssh = null;
function renderSsh(x) {
  if (!x) return;
  ssh = x;
  $("#sshBtn").textContent = x.active ? "SSH : activé" : x.enabled ? "SSH : arrêté" : "SSH : éteint";
}
$("#sshBtn").onclick = () => {
  if (!ssh) return;
  $("#sshUser").value = ssh.user; $("#sshOn").checked = ssh.enabled; $("#sshPw").value = ""; $("#sshErr").textContent = "";
  $("#sshStatus").textContent = ssh.active ? "Le serveur SSH est en marche."
    : ssh.enabled ? "Activé, mais le serveur n'a pas pu démarrer." : "Le serveur SSH est éteint.";
  $("#sshPw").placeholder = ssh.password_set ? "Laisser vide pour ne pas le changer" : "Choisis un mot de passe (12 caractères min.)";
  $("#sshHelp").textContent = `Joignable uniquement depuis le réseau local (Ethernet ou borne Wi-Fi derrière le switch) : ssh ${ssh.user}@192.168.1.1. ` +
    "Changer le mot de passe écrit brièvement sur la carte (une seconde).";
  $("#sshDlg").showModal();
};
$("#sshCancel").onclick = () => $("#sshDlg").close();
$("#sshForm").onsubmit = async e => {
  e.preventDefault(); $("#sshErr").textContent = "";
  try {
    const r = await api("/api/ssh", {enabled: $("#sshOn").checked, password: $("#sshPw").value});
    $("#sshDlg").close(); toast(r.enabled ? "SSH activé" : "SSH désactivé"); setTimeout(refresh, 3000);
  } catch (err) { $("#sshErr").textContent = err.message; }   // dans la fenêtre : un toast serait caché derrière elle
};
function renderBuild(b) {
  if (!b) return;
  const parts = ["EmergencyRouter " + b.version];
  if (b.built) parts.push("image du " + b.built);
  if (b.commit) parts.push("version " + b.commit);
  if (b.os) parts.push(b.os);
  $("#buildInfo").textContent = parts.join(" · ");
}
function renderTls(t) {
  const el = $("#tlsInfo");
  el.hidden = !(t && t.ready);
  el.textContent = t && t.ready ? "Certificat HTTPS auto-signé · empreinte SHA-256 : " + t.fingerprint : "";
}
async function refresh() {
  const s = await api("/api/status");
  const u = s.uplink;
  $("#upDot").className = "dot " + (u.internet ? "ok" : u.connected ? "warn" : "bad");
  $("#upTitle").textContent = u.internet ? "En ligne" : u.connected ? "Connecté, sans Internet" : "Hors ligne";
  $("#upSub").textContent = u.connected ? u.ssid : "Choisis un réseau ci-dessous";
  $("#upKv").innerHTML = u.connected ? kv([["Signal", (u.signal ?? "?") + " %"],
    ["Canal", `${u.channel} (${(u.freq / 1000).toFixed(1)} GHz)`], ["IP", u.ip || "—"]]) : "";
  const l = s.lan;
  $("#lanDot").className = "dot " + (l.link === false ? "bad" : l.link ? "ok" : "");
  $("#lanTitle").textContent = l.link === false ? "Câble absent" : l.link ? "Câble branché" : "Inconnu";
  $("#lanSub").textContent = l.interface || "";
  $("#lanKv").innerHTML = kv([["Routeur", l.address], ["Appareils", l.clients], ["Uptime", fmtUp(s.uptime)]]);
  const w = $("#warnings"); w.hidden = !s.warnings.length; w.innerHTML = s.warnings.map(esc).join("<br>");
  renderSsh(s.ssh);
  renderBuild(s.build);
  renderTls(s.tls);
  const c = await api("/api/clients");
  $("#clients").innerHTML = c.clients.map(x => `<tr><td>${esc(x.hostname || "—")}</td><td>${esc(x.ip)}</td><td>${esc(x.mac)}</td></tr>`).join("")
    || `<tr><td colspan="3" class="muted">Aucun appareil</td></tr>`;
}

async function loadNetworks(rescan) {
  const box = $("#networks");
  if (rescan) { $("#scanBtn").disabled = true; $("#scanBtn").textContent = "Scan…"; }
  try {
    const d = await api("/api/wifi/scan" + (rescan ? "?rescan=1" : ""));
    saved = d.saved;
    box.innerHTML = d.networks.map(n => `<div class="item">${bars(n.signal)}<span class="name">${esc(n.ssid)}</span>
      <span class="tag">${n.freq > 4000 ? "5 GHz" : "2,4 GHz"}</span>${n.secured ? "<span aria-label=\"protégé\">🔒</span>" : ""}
      ${n.in_use ? '<span class="tag ok">connecté</span>' : `<button data-join="${esc(n.ssid)}" data-sec="${n.secured}">Connecter</button>`}
      ${saved.includes(n.ssid) ? `<button class="danger" data-forget="${esc(n.ssid)}">Oublier</button>` : ""}</div>`).join("")
      || '<div class="muted">Aucun réseau trouvé. Clique sur « Actualiser ».</div>';
  } catch (e) { if (rescan) toast(e.message); }
  $("#scanBtn").disabled = false; $("#scanBtn").textContent = "Actualiser";
}
$("#scanBtn").onclick = () => loadNetworks(true);
$("#networks").onclick = async e => {
  const j = e.target.dataset.join, f = e.target.dataset.forget;
  if (f) { await api("/api/wifi/forget", {ssid: f}).catch(x => toast(x.message)); loadNetworks(false); }
  if (j) {
    const known = saved.includes(j), sec = e.target.dataset.sec === "true";
    if (!sec || known) return join(j, null);
    $("#joinTitle").textContent = j; $("#joinPw").value = ""; $("#joinDlg").dataset.ssid = j; $("#joinDlg").showModal();
  }
};
$("#joinCancel").onclick = () => $("#joinDlg").close();
$("#joinForm").onsubmit = () => join($("#joinDlg").dataset.ssid, $("#joinPw").value);
async function join(ssid, password) {
  toast("Connexion à " + ssid + "…");
  try { await api("/api/wifi/connect", {ssid, password}); toast("Connecté à " + ssid); }
  catch (e) { toast(e.message); }
  refresh(); loadNetworks(false);
}

async function loadSettings() {
  const s = await api("/api/settings");
  $("#lanAddr").value = s.lan.address; $("#lanStart").value = s.lan.dhcp_start; $("#lanEnd").value = s.lan.dhcp_end;
  $("#lanDns").value = s.lan.dns.join(", ");
}
async function save(body) {
  try { await api("/api/settings", body); toast("Enregistré, application en cours…"); setTimeout(refresh, 7000); }
  catch (e) { toast(e.message); }
}
$("#lanForm").onsubmit = e => {
  e.preventDefault();
  save({lan: {address: $("#lanAddr").value, dhcp_start: $("#lanStart").value, dhcp_end: $("#lanEnd").value,
    dns: $("#lanDns").value.split(",").map(s => s.trim()).filter(Boolean)}});
};

$("#pwBtn").onclick = () => $("#pwDlg").showModal();
$("#pwCancel").onclick = () => $("#pwDlg").close();
$("#pwForm").onsubmit = async () => {
  try { await api("/api/password", {current: $("#pwCur").value, new: $("#pwNew").value}); toast("Mot de passe modifié"); }
  catch (e) { toast(e.message); }
  $("#pwCur").value = $("#pwNew").value = "";
};
boot();
