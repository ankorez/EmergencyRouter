"use strict";
// Consommation, qualité de la connexion, test de débit, économies de données, outils et assistant de configuration.
// Utilise $, esc, api, toast et kv de app.js ; reçoit « erouter:ready » (connecté) et « erouter:status » (toutes les 5 s).

const fmtBytes = n => n >= 1e9 ? (n / 1e9).toFixed(2).replace(".", ",") + " Go" : n >= 1e6 ? (n / 1e6).toFixed(1).replace(".", ",") + " Mo"
  : n >= 1e3 ? Math.round(n / 1e3) + " Ko" : n + " o";
const fmtNum = (v, unit, digits = 0) => v === null || v === undefined ? "—" : String(Number(v).toFixed(digits)).replace(".", ",") + " " + unit;
const fmtDate = ts => new Date(ts * 1000).toLocaleString("fr-FR", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"});

let settings = null;          // réglages complets, relus à chaque enregistrement
let clients = [];
let lastSpeed = null;
let speedTimer = null;

// ---------- Consommation ----------
async function loadUsage() {
  const u = await api("/api/usage");
  $("#usClock").hidden = u.clock;
  if (!u.clock) { $("#usToday").textContent = "—"; $("#usPeriod").textContent = "—"; return; }
  $("#usToday").textContent = fmtBytes(u.today.rx + u.today.tx);
  $("#usTodaySub").textContent = `↓ ${fmtBytes(u.today.rx)} · ↑ ${fmtBytes(u.today.tx)}`;
  const total = u.period.rx + u.period.tx;
  $("#usPeriodLabel").textContent = "Depuis le " + u.period.start.split("-").reverse().join("/");
  $("#usPeriod").textContent = fmtBytes(total);
  $("#usPeriodSub").textContent = u.plan_bytes ? `sur ${fmtBytes(u.plan_bytes)} (${String(u.percent).replace(".", ",")} %)` : `↓ ${fmtBytes(u.period.rx)} · ↑ ${fmtBytes(u.period.tx)}`;
  const bar = $("#usProgress");
  bar.hidden = !u.plan_bytes;
  bar.value = Math.min(100, u.percent || 0);
  bar.className = u.percent >= 100 ? "bad" : u.percent >= (settings?.usage.alert_percent ?? 80) ? "warn" : "";
  $("#usDevices").innerHTML = u.devices.map(d => `<tr><td>${esc(d.name || d.ip)}<div class="muted small">${esc(d.mac)}</div></td><td>${fmtBytes(d.down)}</td><td>${fmtBytes(d.up)}</td><td>${fmtBytes(d.down + d.up)}</td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">Pas encore de consommation par appareil.</td></tr>`;
}
$("#usageReset").onclick = async () => {
  if (!confirm("Remettre tous les compteurs de consommation à zéro ?")) return;
  await api("/api/usage/reset", {}).catch(e => toast(e.message));
  loadUsage();
};
$("#usageForm").onsubmit = async e => {
  e.preventDefault();
  await saveSettings({usage: {plan_gb: +$("#usPlan").value || 0, alert_percent: +$("#usAlert").value || 80, reset_day: +$("#usDay").value || 1}}, "Forfait enregistré");
  loadUsage();
};

// ---------- Courbes ----------
function drawChart(svg, points, key, opts = {}) {
  const w = 300, h = 60;
  const vals = points.map(p => p[key]).filter(v => v !== null && v !== undefined);
  svg.replaceChildren();
  const ns = "http://www.w3.org/2000/svg";
  const add = (tag, attrs) => { const el = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v); svg.append(el); return el; };
  add("line", {class: "grid", x1: 0, y1: h - 1, x2: w, y2: h - 1});
  if (!vals.length) return;
  const t0 = points[0].t, t1 = points[points.length - 1].t || t0 + 1;
  const max = Math.max(opts.max ?? 0, ...vals) || 1;
  const x = t => ((t - t0) / Math.max(1, t1 - t0)) * w;
  const y = v => h - 3 - (v / max) * (h - 8);
  const line = (k, cls) => {
    const pts = points.filter(p => p[k] !== null && p[k] !== undefined).map(p => `${x(p.t).toFixed(1)},${y(p[k]).toFixed(1)}`);
    if (pts.length) add("polyline", {class: cls, points: pts.join(" ")});
  };
  if (opts.loss) points.filter(p => p.loss > 0).forEach(p => add("rect", {class: "loss", x: x(p.t).toFixed(1), y: 0, width: 3, height: h}));
  line(key, "line");
  if (opts.second) line(opts.second, "line2");
}
async function loadHistory() {
  const {points} = await api("/api/history");
  $("#chEmpty").hidden = points.length > 1;
  drawChart($("#chLat"), points, "ms", {loss: true});
  drawChart($("#chSig"), points, "signal", {max: 100});
  drawChart($("#chRate"), points, "down", {second: "up"});
  const last = points[points.length - 1];
  const worst = Math.max(0, ...points.map(p => p.ms ?? 0));
  const lost = points.length ? Math.round(points.reduce((s, p) => s + p.loss, 0) / points.length) : 0;
  $("#chLatNow").textContent = last ? `${fmtNum(last.ms, "ms")} · max ${fmtNum(worst, "ms")} · pertes ${lost} %` : "";
  $("#chSigNow").textContent = last ? fmtNum(last.signal, "%") : "";
  $("#chRateNow").textContent = last ? `↓ ${fmtNum(last.down, "Mbit/s", 1)} · ↑ ${fmtNum(last.up, "Mbit/s", 1)}` : "";
}

// ---------- Test de débit ----------
function renderSpeed(s) {
  lastSpeed = s.result || lastSpeed;
  $("#speedBtn").disabled = s.running;
  $("#speedBtn").textContent = s.running ? "Test en cours…" : "Lancer le test";
  const r = s.result;
  $("#spDown").textContent = r ? fmtNum(r.down_mbit, "Mbit/s", 1) : "—";
  $("#spUp").textContent = r ? fmtNum(r.up_mbit, "Mbit/s", 1) : "—";
  $("#spPing").textContent = r ? fmtNum(r.ping_ms, "ms", 0) : "—";
  $("#spStatus").textContent = s.running ? `Étape : ${s.stage}…` : r ? (r.error || `Gigue ${fmtNum(r.jitter_ms, "ms", 1)} · pertes ${fmtNum(r.loss_pct, "%")} · ${fmtBytes(r.bytes_used)} consommés`) : "";
  $("#spHistory").innerHTML = s.history.slice(1, 6).map(h => `<div class="item"><span class="name">${fmtDate(h.at)}</span><span class="tag">↓ ${fmtNum(h.down_mbit, "", 0)}</span><span class="tag">↑ ${fmtNum(h.up_mbit, "", 0)}</span><span class="tag">${fmtNum(h.ping_ms, "ms")}</span></div>`).join("");
  $("#speedSqm").hidden = !(lastSpeed && lastSpeed.down_mbit && lastSpeed.up_mbit);
}
async function pollSpeed() {
  clearTimeout(speedTimer);
  try {
    const s = await api("/api/speedtest");
    renderSpeed(s);
    if (s.running) speedTimer = setTimeout(pollSpeed, 1500);
  } catch (e) { /* session expirée : api() relance la connexion */ }
}
$("#speedBtn").onclick = async () => {
  if (!confirm("Le test consomme environ 35 Mo de ton forfait. Continuer ?")) return;
  try { renderSpeed(await api("/api/speedtest", {})); pollSpeed(); } catch (e) { toast(e.message); }
};
$("#speedSqm").onclick = () => {
  $("#sqmDown").value = Math.max(1, Math.floor(lastSpeed.down_mbit * 0.85));
  $("#sqmUp").value = Math.max(1, Math.floor(lastSpeed.up_mbit * 0.85));
  $("#sqmOn").checked = true;
  $("#savingsForm").scrollIntoView({behavior: "smooth", block: "center"});
  toast("Débits préremplis : enregistre pour activer l'anti-latence");
};

// ---------- Économies et latence ----------
let limits = [];
function renderLimits() {
  const name = mac => (clients.find(c => c.mac === mac)?.hostname) || mac;
  $("#limList").innerHTML = limits.map((l, i) => `<div class="item"><span class="name">${esc(name(l.mac))}</span>
    <span class="tag">↓ ${l.down_mbit || "∞"}</span><span class="tag">↑ ${l.up_mbit || "∞"}</span><button type="button" class="danger" data-lim="${i}">Retirer</button></div>`).join("")
    || '<div class="muted small">Aucun plafond.</div>';
  const used = new Set(limits.map(l => l.mac));
  $("#limDev").innerHTML = clients.filter(c => !used.has(c.mac)).map(c => `<option value="${esc(c.mac)}">${esc(c.hostname || c.ip)} (${esc(c.ip)})</option>`).join("")
    || '<option value="">Aucun appareil disponible</option>';
}
$("#limList").onclick = e => { if (e.target.dataset.lim !== undefined) { limits.splice(+e.target.dataset.lim, 1); renderLimits(); } };
$("#limAdd").onclick = () => {
  const mac = $("#limDev").value, down = +$("#limDown").value || 0, up = +$("#limUp").value || 0;
  if (!mac) return toast("Choisis un appareil connecté");
  if (!down && !up) return toast("Indique au moins un débit (0 = pas de limite)");
  if (limits.length >= 16) return toast("16 plafonds au maximum");
  limits.push({mac, down_mbit: down, up_mbit: up});
  renderLimits();
};
$("#savingsForm").onsubmit = async e => {
  e.preventDefault();
  await saveSettings({savings: {block_ads: $("#adsOn").checked, limits},
    sqm: {enabled: $("#sqmOn").checked, down_mbit: +$("#sqmDown").value || 0, up_mbit: +$("#sqmUp").value || 0}}, "Réglages enregistrés");
};

// ---------- Réglages ----------
async function saveSettings(body, message) {
  try { await api("/api/settings", body); toast(message); await loadSettingsAll(); return true; }
  catch (e) { toast(e.message); return false; }
}
async function loadSettingsAll() {
  settings = await api("/api/settings");
  $("#usPlan").value = settings.usage.plan_gb; $("#usAlert").value = settings.usage.alert_percent; $("#usDay").value = settings.usage.reset_day;
  $("#adsOn").checked = settings.savings.block_ads;
  limits = settings.savings.limits.map(l => ({...l}));
  $("#sqmOn").checked = settings.sqm.enabled; $("#sqmDown").value = settings.sqm.down_mbit; $("#sqmUp").value = settings.sqm.up_mbit;
  renderLimits();
}
async function loadClients() {
  clients = (await api("/api/clients")).clients;
  renderLimits();
}

// ---------- Outils ----------
$("#toolsBtn").onclick = () => { $("#toolsErr").textContent = ""; $("#toolsDlg").showModal(); };
$("#toolsClose").onclick = () => $("#toolsDlg").close();
$("#toolWizard").onclick = () => { $("#toolsDlg").close(); openWizard(); };
$("#toolQr").onclick = () => { $("#toolsDlg").close(); openQr(); };
$("#restoreFile").onchange = async e => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    if (!confirm("Remplacer la configuration actuelle par cette sauvegarde ?")) return;
    const r = await api("/api/restore", data);
    $("#toolsDlg").close(); toast("Configuration restaurée");
    if (r.relogin) boot(); else { await loadSettingsAll(); refresh(); }
  } catch (err) { $("#toolsErr").textContent = err instanceof SyntaxError ? "Ce fichier n'est pas une sauvegarde valide." : err.message; }
};
function openQr() {
  $("#qrText").textContent = `Réseau « ${settings?.ap.ssid ?? ""} » : scanne ce code avec l'appareil photo d'un téléphone.`;
  $("#qrImg").src = "/api/qr.svg?" + Date.now();
  $("#qrDlg").showModal();
}
$("#qrClose").onclick = () => $("#qrDlg").close();

// ---------- Assistant de premier démarrage ----------
// IMPORTANT : on est connecté à cet assistant PAR le hotspot du routeur. Changer son nom ou son mot de passe coupe donc la
// connexion : c'est la toute dernière chose que fait l'assistant. La 5G d'abord (elle peut aussi couper le Wi-Fi quelques
// secondes : le hotspot change de canal pour suivre l'iPhone), le hotspot à la fin.
const pause = ms => new Promise(r => setTimeout(r, ms));
const wiz = {step: 0, ssid: "", password: "", networks: [], picked: "", connected: false, shown: false};
const WIZ_TITLES = ["1/3 · La connexion 5G", "2/3 · Le hotspot", "3/3 · Terminé"];
const hotspotChanges = () => !settings || wiz.ssid !== settings.ap.ssid || wiz.password !== settings.ap.password;
function stepper() { return `<div class="step">${[0, 1, 2].map(i => `<i class="${i <= wiz.step ? "on" : ""}"></i>`).join("")}</div>`; }
function renderWizard() {
  $("#wizTitle").textContent = WIZ_TITLES[wiz.step];
  $("#wizErr").textContent = "";
  $("#wizPrev").hidden = wiz.step === 0;
  $("#wizNext").disabled = false;
  $("#wizNext").textContent = wiz.step === 2 ? "Terminer" : "Suivant";
  $("#wizSkip").hidden = wiz.step === 2;
  let html = stepper();
  if (wiz.step === 0) {
    html += `<p class="muted small">Choisis le partage de connexion de ton téléphone (idéalement en 2,4 GHz) et <b>garde l'écran « Partage de connexion » ouvert</b> sur le téléphone pendant la recherche. Le Wi-Fi du routeur peut se couper quelques secondes pendant la connexion : l'assistant attend.</p>
      <div class="list" id="wzNets">${wiz.networks.map(n => `<label class="item"><input type="radio" name="wzNet" value="${esc(n.ssid)}" ${wiz.picked === n.ssid ? "checked" : ""}>
        <span class="name">${esc(n.ssid)}</span><span class="tag">${n.freq > 4000 ? "5 GHz" : "2,4 GHz"}</span>${n.in_use ? '<span class="tag ok">connecté</span>' : ""}</label>`).join("")
        || `<div class="muted">${wiz.scanning ? "Recherche des réseaux…" : "Aucun réseau trouvé : active le partage de connexion puis actualise."}</div>`}</div>
      <button type="button" id="wzScan">Actualiser la liste</button>
      <label id="wzPwWrap" hidden>Mot de passe du réseau<input id="wzNetPw" type="password" autocomplete="off" maxlength="63"></label>
      <p class="muted small">Tu peux aussi passer cette étape et la faire plus tard.</p>`;
  } else if (wiz.step === 1) {
    html += `<p class="muted small">Le hotspot que ce routeur diffuse. Pour que tes appareils s'y connectent tout seuls, utilise le <b>même nom et le même mot de passe que ta box fibre</b>. Il ne sera appliqué qu'à la fin.</p>
      <label>Nom du Wi-Fi (SSID)<input id="wzSsid" maxlength="32" value="${esc(wiz.ssid)}"></label>
      <label>Mot de passe (8 caractères minimum)<input id="wzPw" type="text" autocomplete="off" maxlength="63" value="${esc(wiz.password)}"></label>`;
  } else {
    html += `<p>${wiz.connected ? "La 5G est connectée." : "La 5G n'est pas encore connectée : tu pourras la choisir plus tard dans « Réseaux Wi-Fi »."}</p>` +
      (hotspotChanges() ? `<p class="banner">En terminant, le hotspot devient <b>« ${esc(wiz.ssid)} »</b> avec le mot de passe que tu as choisi. <b>Ton appareil sera déconnecté</b> : reconnecte-toi à ce Wi-Fi, puis rouvre cette page.</p>` : `<p class="muted small">Le hotspot reste « ${esc(wiz.ssid)} ».</p>`) +
      `<p class="muted small">Besoin d'un accès SSH ? Bouton « SSH » en haut de la page. Le bouton « Outils » regroupe le QR code du Wi-Fi, la sauvegarde et le diagnostic.</p>
      <button type="button" id="wzQr">Afficher le QR code du Wi-Fi actuel</button>`;
  }
  $("#wizBody").innerHTML = html;
  if (wiz.step === 0) {
    $("#wzScan").onclick = () => scanForWizard(true);
    $("#wizBody").onchange = e => { if (e.target.name === "wzNet") { wiz.picked = e.target.value; const n = wiz.networks.find(x => x.ssid === wiz.picked); $("#wzPwWrap").hidden = !(n && n.secured && !n.saved); } };
  }
  if (wiz.step === 2) $("#wzQr").onclick = () => { $("#wizDlg").close(); openQr(); };
}
async function scanForWizard(rescan) {
  wiz.scanning = true; renderWizard();
  try {
    const d = await api("/api/wifi/scan" + (rescan ? "?rescan=1" : ""), undefined, rescan ? 70000 : 25000);
    wiz.networks = d.networks.map(n => ({...n, saved: d.saved.includes(n.ssid)}));
    if (!wiz.networks.length && !rescan) { wiz.scanning = false; return scanForWizard(true); }     // aucun résultat en cache : on lance une vraie recherche
  } catch (e) { wiz.scanning = false; renderWizard(); $("#wizErr").textContent = e.message; return; }
  wiz.scanning = false; renderWizard();
}
async function waitForUplink(ssid, seconds) {
  for (let i = 0; i < seconds / 2; i++) {
    await pause(2000);
    try { const s = await api("/api/status", undefined, 5000); if (s.uplink.connected && s.uplink.ssid === ssid) return true; } catch (e) { /* le Wi-Fi du routeur revient */ }
  }
  return false;
}
async function wizardNext() {
  $("#wizErr").textContent = "";
  if (wiz.step === 0) {
    if (wiz.picked && !wiz.connected) {
      const n = wiz.networks.find(x => x.ssid === wiz.picked);
      const pw = n && n.secured && !n.saved ? $("#wzNetPw").value : null;
      if (n && n.secured && !n.saved && !pw) { $("#wizErr").textContent = "Saisis le mot de passe du réseau."; $("#wzPwWrap").hidden = false; return; }
      $("#wizNext").disabled = true; $("#wizNext").textContent = "Connexion…";
      try { await api("/api/wifi/connect", {ssid: wiz.picked, password: pw}, 90000); wiz.connected = true; }
      catch (e) {
        if (e.network) wiz.connected = await waitForUplink(wiz.picked, 40);      // coupure attendue : on attend que le routeur revienne
        if (!wiz.connected) { $("#wizErr").textContent = e.network ? "Connexion non confirmée : vérifie le mot de passe et que le partage de connexion est actif." : e.message; renderWizardKeep(); return; }
      }
    }
    wiz.step = 1; renderWizard(); return;
  }
  if (wiz.step === 1) {
    wiz.ssid = $("#wzSsid").value.trim(); wiz.password = $("#wzPw").value;
    if (!wiz.ssid || wiz.password.length < 8) { $("#wizErr").textContent = "Un nom et un mot de passe de 8 caractères minimum."; return; }
    wiz.step = 2; renderWizard(); return;
  }
  await finishWizard();
}
function renderWizardKeep() { const err = $("#wizErr").textContent; renderWizard(); $("#wizErr").textContent = err; }
async function finishWizard() {
  const changed = hotspotChanges();
  $("#wizNext").disabled = true; $("#wizNext").textContent = "Application…";
  try { await api("/api/settings", {ap: {ssid: wiz.ssid, password: wiz.password, enabled: true}, setup: {done: true}}); }
  catch (e) {
    if (!e.network) { $("#wizErr").textContent = e.message; $("#wizNext").disabled = false; $("#wizNext").textContent = "Terminer"; return; }
    // coupure attendue : le hotspot vient de changer de nom
  }
  $("#wizDlg").close();
  if (changed) alert(`Le hotspot s'appelle maintenant « ${wiz.ssid} ».\nReconnecte ton appareil à ce Wi-Fi avec le nouveau mot de passe, puis rouvre cette page.`);
  else { loadSettingsAll(); refresh(); }
}
function openWizard() {
  Object.assign(wiz, {step: 0, ssid: settings?.ap.ssid ?? "", password: settings?.ap.password ?? "", picked: "", connected: false, networks: []});
  renderWizard(); $("#wizDlg").showModal(); scanForWizard(false);
}
$("#wizForm").onsubmit = e => { e.preventDefault(); wizardNext(); };
$("#wizPrev").onclick = () => { wiz.step = Math.max(0, wiz.step - 1); if (wiz.step === 0 && !wiz.networks.length) scanForWizard(false); else renderWizard(); };
$("#wizSkip").onclick = () => { if (wiz.step < 2) { wiz.step += 1; renderWizard(); } };
$("#wizDlg").addEventListener("cancel", e => { e.preventDefault(); });   // pas de fermeture par Échap : « Passer » est explicite

// ---------- Cycle de vie ----------
let timers2 = [];
document.addEventListener("erouter:ready", async () => {
  timers2.forEach(clearInterval); timers2 = [];
  await Promise.allSettled([loadSettingsAll(), loadClients(), loadUsage(), loadHistory(), pollSpeed()]);
  timers2.push(setInterval(loadUsage, 15000), setInterval(loadHistory, 60000), setInterval(loadClients, 20000));
  if (settings && !settings.setup.done && !wiz.shown) { wiz.shown = true; openWizard(); }
});
