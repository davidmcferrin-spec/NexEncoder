(function () {
  "use strict";

  var selected = "nexencoder-console.service";
  var source = null;
  var followTail = true;
  var systemctl = false;

  function $(id) { return document.getElementById(id); }

  function esc(text) {
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function badge(active) {
    if (active === "active") return "badge badge-ok";
    if (active === "failed") return "badge badge-bad";
    if (active === "unavailable") return "badge badge-warn";
    return "badge";
  }

  function render(body) {
    systemctl = !!body.systemctl;
    var banner = $("svc-banner");
    if (systemctl) {
      banner.hidden = true;
    } else {
      banner.hidden = false;
      banner.textContent = body.detail || "systemctl is not available on this host";
    }
    document.querySelectorAll("[data-action]").forEach(function (button) {
      button.disabled = !systemctl;
    });
    var list = $("unit-list");
    list.innerHTML = "";
    (body.units || []).forEach(function (unit) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "unit-row" + (unit.name === selected ? " is-selected" : "");
      button.innerHTML = "<span><strong>" + esc(unit.name) + "</strong><span class=\"muted\">"
        + esc(unit.description || unit.sub || "") + "</span></span>"
        + "<span class=\"" + badge(unit.active) + "\">" + esc(unit.active) + "</span>";
      button.addEventListener("click", function () {
        selected = unit.name;
        follow(unit.name);
        render(body);
      });
      list.appendChild(button);
      if (unit.error) {
        var err = document.createElement("p");
        err.className = "error";
        err.textContent = unit.error;
        list.appendChild(err);
      }
    });
  }

  function load() {
    fetch("/admin/services.php").then(function (response) { return response.json(); }).then(render).catch(function () {
      $("svc-banner").hidden = false;
      $("svc-banner").textContent = "The console service list did not answer";
    });
  }

  function append(line) {
    var log = $("journal");
    log.textContent += (log.textContent ? "\n" : "") + line;
    var lines = log.textContent.split("\n");
    if (lines.length > 500) log.textContent = lines.slice(lines.length - 500).join("\n");
    if (followTail) log.scrollTop = log.scrollHeight;
  }

  function follow(unit) {
    if (source) source.close();
    var log = $("journal");
    log.textContent = "";
    $("journal-title").textContent = unit;
    source = new EventSource("/live/log.php?unit=" + encodeURIComponent(unit));
    source.onmessage = function (event) { append(event.data); };
    source.addEventListener("error", function (event) {
      if (event.data) {
        append(event.data);
        source.close();
      }
    });
  }

  function run(action) {
    var destructive = action === "stop" || action === "restart" || action === "disable";
    if (destructive && !window.confirm(action + " " + selected + "?")) return;
    $("action-status").textContent = action + "…";
    fetch("/admin/service.php?unit=" + encodeURIComponent(selected) + "&action=" + encodeURIComponent(action), {
      method: "POST",
      headers: { "X-Requested-With": "NexEncoder" }
    }).then(function (response) { return response.json().then(function (body) { return { ok: response.ok, body: body }; }); })
      .then(function (result) {
        $("action-status").textContent = result.ok ? action + " sent" : (result.body.detail || "Action failed");
        if (!result.ok) $("action-status").className = "error";
        else $("action-status").className = "ok";
        load();
      })
      .catch(function () {
        $("action-status").className = "error";
        $("action-status").textContent = "The console did not answer";
      });
  }

  document.querySelectorAll("[data-action]").forEach(function (button) {
    button.addEventListener("click", function () { run(button.getAttribute("data-action")); });
  });
  $("journal").addEventListener("scroll", function () {
    followTail = this.scrollHeight - this.scrollTop - this.clientHeight < 32;
  });

  load();
  follow(selected);
  setInterval(load, 2000);
})();
