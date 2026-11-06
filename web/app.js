/* Home-screen app shell. APP_TOKEN arrives in the #t= fragment and stays in localStorage. */
(function () {
  if (location.hash && location.hash.indexOf("t=") !== -1) {
    history.replaceState(null, "", location.pathname + location.search);
  }
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  }
})();
