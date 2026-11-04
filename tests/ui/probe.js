// Injecté en tête de page par le faux serveur : enregistre toute erreur JavaScript.
window.__errors = [];
window.addEventListener("error", e => window.__errors.push(String(e.message)));
window.addEventListener("unhandledrejection", e => window.__errors.push("rejet : " + String(e.reason)));
window.confirm = () => true;
window.sleep = ms => new Promise(r => setTimeout(r, ms));
window.report = data => {                           // le test lit ce bloc dans le DOM final
  const pre = document.createElement("pre"); pre.id = "__result";
  pre.textContent = JSON.stringify({...data, errors: window.__errors}); document.body.append(pre);
};
