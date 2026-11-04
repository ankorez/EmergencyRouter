async function scenario() {
  await sleep(2500);
  const $ = id => document.getElementById(id);
  const radio = document.querySelectorAll("#wzNets input[type=radio]")[1];
  radio.checked = true; radio.dispatchEvent(new Event("change", {bubbles: true}));
  $("wzNetPw").value = "motdepasse-5g"; $("wizNext").click();                       // la réponse ne reviendra jamais (Wi-Fi coupé)
  await sleep(6000);                                                                 // l'assistant sonde le routeur toutes les 2 s
  report({title: $("wizTitle").textContent, err: $("wizErr").textContent});
}
