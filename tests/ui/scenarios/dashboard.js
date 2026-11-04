async function scenario() {
  await sleep(2500);
  const text = id => (document.getElementById(id) || {}).textContent;
  report({
    usToday: text("usToday"), usPeriod: text("usPeriod"), usPeriodSub: text("usPeriodSub"),
    progress: document.getElementById("usProgress").value, progressClass: document.getElementById("usProgress").className, progressHidden: document.getElementById("usProgress").hidden,
    devices: document.querySelectorAll("#usDevices tr").length,
    latLines: document.querySelectorAll("#chLat polyline").length, rateLines: document.querySelectorAll("#chRate polyline").length,
    lossBars: document.querySelectorAll("#chLat rect.loss").length, latNow: text("chLatNow"),
    spDown: text("spDown"), spUp: text("spUp"), spPing: text("spPing"), spHistory: document.querySelectorAll("#spHistory .item").length,
    speedSqmHidden: document.getElementById("speedSqm").hidden,
    limList: text("limList"), limOptions: document.querySelectorAll("#limDev option").length,
    buttons: [...document.querySelectorAll("header button")].map(b => b.textContent),
    wizardOpen: document.getElementById("wizDlg").open, warnings: text("warnings"),
  });
}
