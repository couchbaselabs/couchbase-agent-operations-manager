// Sets data-theme before first paint so there's no flash of the wrong
// theme. Light is this appliance's default, full stop - the operating
// system's dark preference is deliberately NOT consulted, so a first visit
// looks the same on every machine. Dark is only ever applied when someone
// has explicitly asked for it with the toggle. Keep the storage key and
// fallback logic in sync with ui/src/store/theme.ts.
//
// This lives as an external file (rather than inline in index.html, where
// it used to be) so the dashboard's Content-Security-Policy can use a
// plain `script-src 'self'` with no 'unsafe-inline' - see
// ui/nginx.conf.template.
(function () {
  try {
    var stored = localStorage.getItem("agent-operations-theme");
    document.documentElement.dataset.theme = stored === "dark" ? "dark" : "light";
  } catch (e) {
    document.documentElement.dataset.theme = "light";
  }
})();
