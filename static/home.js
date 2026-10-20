(function () {
  function localDay() {
    var d = new Date();
    var m = String(d.getMonth() + 1);
    var day = String(d.getDate());
    if (m.length < 2) m = "0" + m;
    if (day.length < 2) day = "0" + day;
    return d.getFullYear() + "-" + m + "-" + day;
  }
  var day = localDay();
  var cur = document.cookie.split("; ").filter(function (part) {
    return part.indexOf("docket_day=") === 0;
  })[0];
  var have = cur ? cur.slice("docket_day=".length) : "";
  if (have !== day) {
    document.cookie = "docket_day=" + day + "; path=/; max-age=172800; samesite=lax";
    if (!have) location.reload();
  }

  function copyText(btn, text) {
    if (!btn || !navigator.clipboard) return;
    navigator.clipboard.writeText(text).then(function () {
      var prev = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = prev; }, 1200);
    }).catch(function () {});
  }
  function bind(btnId, nodeId) {
    var btn = document.getElementById(btnId);
    var node = document.getElementById(nodeId);
    if (!btn || !node) return;
    btn.addEventListener("click", function () {
      var path = (node.textContent || "").trim();
      var absolute = path.charAt(0) === "/" ? window.location.origin + path : path;
      copyText(btn, absolute);
    });
  }
  bind("copy-owner", "owner");
  bind("copy-share", "share");

  var title = document.getElementById("title") || document.getElementById("name");
  document.addEventListener("keydown", function (ev) {
    var tag = (ev.target && ev.target.tagName) || "";
    if (ev.key === "/" && tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") {
      ev.preventDefault();
      if (title) title.focus();
    }
  });
})();
