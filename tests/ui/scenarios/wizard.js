async function scenario() {
  await sleep(2500);
  const $ = id => document.getElementById(id);
  const out = {openedByItself: $("wizDlg").open, title1: $("wizTitle").textContent, networks: document.querySelectorAll("#wzNets .item").length};
  const radio = document.querySelectorAll("#wzNets input[type=radio]")[1];          // « Routeur-4G » : protégé, pas encore enregistré
  radio.checked = true; radio.dispatchEvent(new Event("change", {bubbles: true}));
  $("wizNext").click(); await sleep(200);                                           // sans mot de passe : refus
  out.errNoPw = $("wizErr").textContent;
  $("wzNetPw").value = "motdepasse-5g"; $("wizNext").click(); await sleep(700);
  out.title2 = $("wizTitle").textContent;
  $("wzSsid").value = ""; $("wzPw").value = ""; $("wizNext").click(); await sleep(200);   // champs vides : refus
  out.errEmpty = $("wizErr").textContent;
  $("wzSsid").value = "Ouifi"; $("wzPw").value = "motdepasse-box"; $("wizNext").click(); await sleep(300);
  out.title3 = $("wizTitle").textContent; out.warning = $("wizBody").textContent;
  const before = await (await fetch("/__log")).json();                               // qu'a-t-on envoyé au routeur jusqu'ici ?
  out.settingsBeforeFinish = before.filter(e => e.path === "/api/settings").length;
  out.connectPosted = before.some(e => e.path === "/api/wifi/connect");
  $("wizNext").click(); await sleep(800);                                            // « Terminer »
  out.closed = !$("wizDlg").open; out.alerts = window.__alerts;
  report(out);
}
