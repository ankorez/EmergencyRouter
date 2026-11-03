"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
let timers = [];
let saved = [];
let fsState = {enabled: false};

async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
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
  timers.push(setInterval(refresh, 5000), setInterval(() => loadNetworks(false), 15000));
}

$("#loginForm").onsubmit = async e => {
  e.preventDefault(); $("#loginErr").textContent = "";
  try { await api("/api/login", {password: $("#loginPw").value}); $("#loginPw").value = ""; boot(); }
  catch (err) { $("#loginErr").textContent = err.message; }
};
$("#logoutBtn").onclick = async () => { await api("/api/logout", {}); boot(); };

const DATA_STATE = {ok: "Partition de données saine.", repaired: "Partition de données réparée automatiquement au démarrage.",
  reset: "Partition de données recréée au démarrage : configuration remise à zéro.", none: ""};

function renderFs(f) {
  fsState = f;
  const dot = $("#fsDot"), btn = $("#fsBtn");
  $("#fsData").textContent = f.available ? (DATA_STATE[f.data] ?? "") : "";
  if (!f.available) {
    dot.className = "dot warn"; $("#fsTitle").textContent = "Non disponible";
    $("#fsSub").textContent = "Pas de partition de données";
    $("#fsHelp").textContent = "La protection de la carte (système en lecture seule) demande l'image EmergencyRouter, qui a une partition de données séparée.";
    btn.hidden = true;
    return;
  }
  btn.hidden = false;
  if (f.locked) {
    dot.className = "dot ok"; $("#fsTitle").textContent = "Protégée";
    $("#fsSub").textContent = "Système en lecture seule";
    $("#fsHelp").textContent = "Tu peux couper le courant sans risque : le système n'est plus jamais écrit, seule la petite partition de données l'est quand tu enregistres un réglage.";
    btn.textContent = "Désactiver la protection";
  } else if (f.enabled) {
    dot.className = "dot warn"; $("#fsTitle").textContent = f.rescue ? "Mode secours" : "En attente";
    $("#fsSub").textContent = f.rescue ? "Verrouillage ignoré (erouter.unlock)" : "Verrouillage en cours";
    $("#fsHelp").textContent = f.rescue ? "Retire « erouter.unlock » de cmdline.txt pour réactiver la protection."
      : "Le système passera en lecture seule dès que le démarrage est terminé.";
    btn.textContent = "Désactiver la protection";
  } else {
    dot.className = "dot bad"; $("#fsTitle").textContent = "Non protégée";
    $("#fsSub").textContent = "Lecture-écriture";
    $("#fsHelp").textContent = "Le système peut être abîmé par une coupure de courant. Active la protection quand la configuration est terminée.";
    btn.textContent = "Activer la protection";
  }
}
function renderTls(t) {
  const el = $("#tlsInfo");
  el.hidden = !(t && t.ready);
  el.textContent = t && t.ready ? "Certificat HTTPS auto-signé · empreinte SHA-256 : " + t.fingerprint : "";
}
$("#fsBtn").onclick = async () => {
  const enable = !fsState.enabled;
  if (!enable && !confirm("Désactiver la protection de la carte SD ?")) return;
  try { renderFs(await api("/api/fs", {enabled: enable})); toast(enable ? "Protection activée" : "Protection désactivée"); }
  catch (e) { toast(e.message); }
};

async function refresh() {
  const s = await api("/api/status");
  const u = s.uplink;
  $("#upDot").className = "dot " + (u.internet ? "ok" : u.connected ? "warn" : "bad");
  $("#upTitle").textContent = u.internet ? "En ligne" : u.connected ? "Connecté, sans Internet" : "Hors ligne";
  $("#upSub").textContent = u.connected ? u.ssid : "Choisis un réseau ci-dessous";
  $("#upKv").innerHTML = u.connected ? kv([["Signal", (u.signal ?? "?") + " %"],
    ["Canal", `${u.channel} (${(u.freq / 1000).toFixed(1)} GHz)`], ["IP", u.ip || "—"]]) : "";
  const a = s.ap;
  $("#apDot").className = "dot " + (!a.enabled ? "" : a.active ? "ok" : "bad");
  $("#apTitle").textContent = !a.enabled ? "Désactivé" : a.active ? "Actif" : "Arrêté";
  $("#apSub").textContent = a.enabled ? a.ssid : "";
  $("#apKv").innerHTML = kv([["Canal", a.channel], ["Routeur", s.lan.address], ["Appareils", s.lan.clients], ["Uptime", fmtUp(s.uptime)]]);
  const w = $("#warnings"); w.hidden = !s.warnings.length; w.innerHTML = s.warnings.map(esc).join("<br>");
  renderFs(s.fs);
  renderTls(s.tls);
  const c = await api("/api/clients");
  $("#clients").innerHTML = c.clients.map(x => `<tr><td>${esc(x.hostname || "—")}</td><td>${esc(x.ip)}</td><td>${esc(x.mac)}</td>` +
    `<td><span class="tag">${x.wifi ? "Wi-Fi" : "Ethernet"}</span></td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">Aucun appareil</td></tr>`;
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
  $("#apSsid").value = s.ap.ssid; $("#apPw").value = s.ap.password; $("#apCountry").value = s.ap.country;
  $("#apChan").value = s.ap.fallback_channel; $("#apOn").checked = s.ap.enabled;
  $("#lanAddr").value = s.lan.address; $("#lanStart").value = s.lan.dhcp_start; $("#lanEnd").value = s.lan.dhcp_end;
  $("#lanDns").value = s.lan.dns.join(", ");
}
async function save(body) {
  try { await api("/api/settings", body); toast("Enregistré, application en cours…"); setTimeout(refresh, 7000); }
  catch (e) { toast(e.message); }
}
$("#apForm").onsubmit = e => {
  e.preventDefault();
  save({ap: {ssid: $("#apSsid").value, password: $("#apPw").value, country: $("#apCountry").value.toUpperCase(),
    fallback_channel: +$("#apChan").value, enabled: $("#apOn").checked}});
};
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
