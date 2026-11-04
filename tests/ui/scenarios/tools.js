async function scenario() {
  await sleep(2000);
  document.getElementById("toolsBtn").click(); await sleep(200);
  const toolsOpen = document.getElementById("toolsDlg").open;
  const links = [...document.querySelectorAll("#toolsDlg a.btn")].map(a => a.getAttribute("href"));
  document.getElementById("toolQr").click(); await sleep(300);
  const qrOpen = document.getElementById("qrDlg").open, toolsClosed = !document.getElementById("toolsDlg").open;
  const qrSrc = document.getElementById("qrImg").getAttribute("src"), qrText = document.getElementById("qrText").textContent;
  report({toolsOpen, links, qrOpen, toolsClosed, qrSrc, qrText});
}
