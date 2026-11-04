async function scenario() {
  await sleep(2000);
  document.getElementById("toolsBtn").click(); await sleep(200);
  const toolsOpen = document.getElementById("toolsDlg").open;
  const links = [...document.querySelectorAll("#toolsDlg a.btn")].map(a => a.getAttribute("href"));
  const buttons = [...document.querySelectorAll("#toolsDlg button")].map(b => b.textContent);
  document.getElementById("toolsClose").click(); await sleep(200);
  report({toolsOpen, links, buttons, toolsClosed: !document.getElementById("toolsDlg").open});
}
