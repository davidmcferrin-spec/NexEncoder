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

  function note(id, ok, text) {
    var node = $(id);
    node.className = ok ? "ok" : "error";
    node.textContent = text;
  }

  function saveBlob(blob, name) {
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function fileName(response, fallback) {
    var header = response.headers.get("Content-Disposition") || "";
    var match = /filename="([^"]+)"/.exec(header);
    return match ? match[1] : fallback;
  }

  function copyWithSelection(text) {
    var ok = false;
    function onCopy(event) {
      if (!event.clipboardData) return;
      event.clipboardData.setData("text/plain", text);
      event.preventDefault();
      ok = true;
    }
    document.addEventListener("copy", onCopy);
    try {
      ok = document.execCommand("copy") || ok;
    } catch (err) {
      ok = false;
    }
    document.removeEventListener("copy", onCopy);
    return ok;
  }

  $("copy-journal").addEventListener("click", function () {
    var text = $("journal").textContent || "";
    if (!text.trim()) {
      note("action-status", false, "Journal is empty");
      return;
    }
    if (copyWithSelection(text)) {
      note("action-status", true, "Log copied");
      return;
    }
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      note("action-status", false, "Copy failed");
      return;
    }
    navigator.clipboard.writeText(text).then(function () {
      note("action-status", true, "Log copied");
    }, function () {
      note("action-status", false, "Copy failed");
    });
  });

  $("clear-journal").addEventListener("click", function () {
    if (!window.confirm("Clear the visible log for " + selected + "? Older lines stay on disk until Clear All.")) {
      return;
    }
    note("action-status", true, "Clearing log…");
    fetch("/admin/journal-clear.php?unit=" + encodeURIComponent(selected), {
      method: "POST",
      headers: { "X-Requested-With": "NexEncoder" }
    }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok || !body.ok) {
          throw new Error(body.detail || "Could not clear the log");
        }
        return body;
      });
    }).then(function () {
      $("journal").textContent = "";
      follow(selected);
      note("action-status", true, "Log cleared");
    }).catch(function (err) {
      note("action-status", false, err.message || "Could not clear the log");
    });
  });

  function setBundleBusy(busy) {
    $("download-bundle").disabled = busy;
    $("clear-all").disabled = busy;
  }

  $("download-bundle").addEventListener("click", function () {
    var hours = $("bundle-hours").value;
    note("bundle-status", true, "Building zip…");
    setBundleBusy(true);
    fetch("/admin/support.php?hours=" + encodeURIComponent(hours), {
      method: "POST",
      headers: { "X-Requested-With": "NexEncoder" }
    }).then(function (response) {
      if (!response.ok) {
        return response.json().then(function (body) {
          throw new Error(body.detail || "Support zip failed");
        });
      }
      var name = fileName(response, "nexencoder-support-" + hours + ".zip");
      return response.blob().then(function (blob) { saveBlob(blob, name); });
    }).then(function () {
      note("bundle-status", true, "Support zip downloaded");
    }).catch(function (err) {
      note("bundle-status", false, err.message || "Support zip failed");
    }).then(function () {
      setBundleBusy(false);
    });
  });

  $("clear-all").addEventListener("click", function () {
    if (!window.confirm("Delete the host journal for every service? Services keep running. The audit log is kept.")) {
      return;
    }
    note("bundle-status", true, "Clearing journal…");
    setBundleBusy(true);
    fetch("/admin/journal-clear.php", {
      method: "POST",
      headers: { "X-Requested-With": "NexEncoder" }
    }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok || !body.ok) {
          throw new Error(body.detail || "Journal was not cleared");
        }
        return body;
      });
    }).then(function () {
      $("journal").textContent = "";
      follow(selected);
      note("bundle-status", true, "Journal cleared");
    }).catch(function (err) {
      note("bundle-status", false, err.message || "Journal was not cleared");
    }).then(function () {
      setBundleBusy(false);
    });
  });

  document.querySelectorAll("[data-action]").forEach(function (button) {
    button.addEventListener("click", function () { run(button.getAttribute("data-action")); });
  });
  $("journal").addEventListener("scroll", function () {
    followTail = this.scrollHeight - this.scrollTop - this.clientHeight < 32;
  });

  load();
  follow(selected);
  setInterval(load, 2000);
  loadTokens();

  var shownSecret = "";

  function tokenNote(ok, text) {
    var error = $("token-error");
    if (!error) return;
    error.hidden = ok || !text;
    error.textContent = ok ? "" : text;
  }

  function tokenRequest(body) {
    var options = { headers: { "Accept": "application/json" } };
    if (body) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers["X-Requested-With"] = "NexEncoder";
      options.body = JSON.stringify(body);
    }
    return fetch("/admin/tokens.php", options).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok || !payload.ok) throw new Error(payload.detail || "Token request failed");
        return payload;
      });
    });
  }

  function renderTokens(tokens) {
    var list = $("token-list");
    if (!list) return;
    list.textContent = "";
    (tokens || []).forEach(function (token) {
      var item = document.createElement("li");
      var text = document.createElement("span");
      var when = token.created ? new Date(token.created * 1000).toLocaleString() : "existing";
      text.textContent = token.label + " · " + token.state + " · " + when + " · " + token.last4;
      var actions = document.createElement("span");
      actions.className = "preset-actions";
      if (token.state === "active") {
        var revoke = document.createElement("button");
        revoke.type = "button";
        revoke.className = "secondary";
        revoke.textContent = "Revoke";
        revoke.addEventListener("click", function () {
          if (!window.confirm("Revoke token " + token.label + "? Callers using it will be refused.")) return;
          tokenRequest({ action: "revoke", id: token.id }).then(function (body) {
            renderTokens(body.tokens);
            tokenNote(true, "");
          }).catch(function (err) { tokenNote(false, err.message); });
        });
        actions.appendChild(revoke);
      }
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn-text";
      remove.textContent = "Delete";
      remove.addEventListener("click", function () {
        if (!window.confirm("Delete token " + token.label + "?")) return;
        tokenRequest({ action: "delete", id: token.id }).then(function (body) {
          renderTokens(body.tokens);
          tokenNote(true, "");
        }).catch(function (err) { tokenNote(false, err.message); });
      });
      actions.appendChild(remove);
      item.appendChild(text);
      item.appendChild(actions);
      list.appendChild(item);
    });
  }

  function loadTokens() {
    tokenRequest(null).then(function (body) {
      renderTokens(body.tokens);
    }).catch(function (err) { tokenNote(false, err.message); });
  }

  function showSecret(secret) {
    shownSecret = secret;
    var once = $("token-once");
    var line = $("token-secret");
    line.textContent = "Copy this token now. It is shown once: " + secret;
    once.hidden = false;
  }

  var tokenForm = $("token-form");
  if (tokenForm) {
    tokenForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var label = $("token-label").value.trim();
      tokenRequest({ action: "create", label: label }).then(function (body) {
        $("token-label").value = "";
        showSecret(body.token.secret || "");
        renderTokens(body.tokens);
        tokenNote(true, "");
      }).catch(function (err) { tokenNote(false, err.message); });
    });
  }
  var copyToken = $("token-copy");
  if (copyToken) {
    copyToken.addEventListener("click", function () {
      if (!shownSecret || !navigator.clipboard || !navigator.clipboard.writeText) return;
      navigator.clipboard.writeText(shownSecret).catch(function () {});
    });
  }
  var hideToken = $("token-hide");
  if (hideToken) {
    hideToken.addEventListener("click", function () {
      shownSecret = "";
      $("token-secret").textContent = "";
      $("token-once").hidden = true;
    });
  }
})();
