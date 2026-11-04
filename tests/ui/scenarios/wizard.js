async function scenario() {
  await sleep(2500);
  const out = {openedByItself: document.getElementById("wizDlg").open, title1: document.getElementById("wizTitle").textContent};
  document.getElementById("wzSsid").value = ""; document.getElementById("wzPw").value = "";
  document.getElementById("wizNext").click(); await sleep(200);                       // champs vides : refus
  out.errEmpty = document.getElementById("wizErr").textContent;
  document.getElementById("wzSsid").value = "MaBox"; document.getElementById("wzPw").value = "motdepasse-box";
  document.getElementById("wizNext").click(); await sleep(600);
  out.title2 = document.getElementById("wizTitle").textContent;
  out.networks = document.querySelectorAll("#wzNets .item").length;
  document.getElementById("wizNext").click(); await sleep(200);                       // rien choisi : on passe
  out.title3 = document.getElementById("wizTitle").textContent;
  document.getElementById("wizPrev").click(); await sleep(500);
  out.backToStep2 = document.getElementById("wizTitle").textContent;
  const radio = document.querySelectorAll('#wzNets input[type=radio]')[1]; radio.checked = true; radio.dispatchEvent(new Event("change", {bubbles: true}));
  out.pwShown = !document.getElementById("wzPwWrap").hidden;
  document.getElementById("wizNext").click(); await sleep(200);                       // mot de passe manquant
  out.errNoPw = document.getElementById("wizErr").textContent;
  document.getElementById("wzNetPw").value = "motdepasse-5g"; document.getElementById("wizNext").click(); await sleep(600);
  out.title3b = document.getElementById("wizTitle").textContent;
  out.connectedMsg = document.getElementById("wizBody").textContent.slice(0, 40);
  document.getElementById("wizNext").click(); await sleep(600);                       // « Terminer »
  out.closed = !document.getElementById("wizDlg").open;
  report(out);
}
