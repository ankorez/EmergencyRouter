async function scenario() {
  await sleep(2000);
  const out = {before: document.getElementById("spDown").textContent};
  document.getElementById("speedBtn").click(); await sleep(300);
  out.runningLabel = document.getElementById("speedBtn").textContent; out.disabled = document.getElementById("speedBtn").disabled;
  await sleep(5000);
  out.after = document.getElementById("spDown").textContent; out.finalLabel = document.getElementById("speedBtn").textContent;
  document.getElementById("speedSqm").click(); await sleep(200);
  out.sqmDown = document.getElementById("sqmDown").value; out.sqmUp = document.getElementById("sqmUp").value; out.sqmOn = document.getElementById("sqmOn").checked;
  report(out);
}
