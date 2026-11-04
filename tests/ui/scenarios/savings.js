async function scenario() {
  await sleep(2500);
  const out = {options: [...document.querySelectorAll("#limDev option")].map(o => o.value)};
  document.getElementById("limDev").value = "02:00:00:00:00:02"; document.getElementById("limDown").value = "3"; document.getElementById("limUp").value = "1";
  document.getElementById("limAdd").click(); await sleep(200);
  out.limitsShown = document.querySelectorAll("#limList .item").length;
  document.querySelector('#limList button[data-lim="0"]').click(); await sleep(100);        // retire le premier plafond
  out.afterRemove = document.querySelectorAll("#limList .item").length;
  document.getElementById("adsOn").checked = true; document.getElementById("sqmOn").checked = true;
  document.getElementById("sqmDown").value = "80"; document.getElementById("sqmUp").value = "20";
  document.querySelector("#savingsForm button.primary").click(); await sleep(600);
  document.getElementById("usPlan").value = "100"; document.getElementById("usAlert").value = "90"; document.getElementById("usDay").value = "15";
  document.querySelector("#usageForm button.primary").click(); await sleep(600);
  report(out);
}
