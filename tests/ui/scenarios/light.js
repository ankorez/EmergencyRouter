// Pour les captures d'écran : force le thème clair en supprimant la règle « sombre » (le navigateur sans écran suit le système).
async function scenario() {
  await sleep(300);
  for (const sheet of document.styleSheets) {
    for (let i = sheet.cssRules.length - 1; i >= 0; i--) {
      const rule = sheet.cssRules[i];
      if (rule instanceof CSSMediaRule && rule.conditionText.includes("prefers-color-scheme")) sheet.deleteRule(i);
    }
  }
}
