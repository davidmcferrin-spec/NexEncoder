(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function esc(text) {
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function note(text, bad) {
    var ok = $("vendor-note");
    var err = $("vendor-error");
    ok.hidden = bad || !text;
    err.hidden = !bad || !text;
    (bad ? err : ok).textContent = text || "";
  }

  function post(action, body) {
    return fetch("/admin/vendor.php?action=" + encodeURIComponent(action), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "NexEncoder" },
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok || payload.ok === false) throw new Error(payload.detail || "The vendor request failed");
        return payload;
      });
    });
  }

  function render(body) {
    var banner = $("vendor-banner");
    if (body.docker) {
      banner.hidden = true;
    } else {
      banner.hidden = false;
      banner.textContent = body.detail || "docker is not available on this host";
    }
    var sources = $("caption-sources");
    sources.innerHTML = "";
    ["A", "B"].forEach(function (channel) {
      var item = (body.captions || {})[channel] || {};
      var block = document.createElement("div");
      block.className = "unit-row";
      var source = item.source === "vendor" ? "vendor" : "vanc";
      block.innerHTML = "<span><strong>Channel " + channel + "</strong><span class=\"muted\">"
        + " captions " + esc(source)
        + " · UDP " + esc(item.caption_port || "")
        + " · SCTE " + esc(item.splice_port || "")
        + " · feed " + esc(item.feed || "")
        + "</span></span>";
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-secondary";
      button.textContent = source === "vendor" ? "Use VANC" : "Use vendor";
      button.addEventListener("click", function () {
        var next = source === "vendor" ? "vanc" : "vendor";
        fetch("/admin/caption-source.php?channel=" + channel, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Requested-With": "NexEncoder" },
          body: JSON.stringify({ source: next })
        }).then(function (response) { return response.json(); }).then(function (payload) {
          if (payload.ok === false) throw new Error(payload.detail || "Caption source was not saved");
          note("Channel " + channel + " captions are " + next, false);
          load();
        }).catch(function (error) { note(error.message, true); });
      });
      block.appendChild(button);
      sources.appendChild(block);
    });
    var list = $("vendor-list");
    list.innerHTML = "";
    (body.vendors || []).forEach(function (vendor) {
      var row = document.createElement("div");
      row.className = "unit-row";
      row.innerHTML = "<span><strong>" + esc(vendor.name) + "</strong><span class=\"muted\">"
        + esc(vendor.image) + " · channel " + esc(vendor.channel)
        + " · " + esc(vendor.active || "")
        + "</span></span>";
      var actions = document.createElement("span");
      actions.className = "actions";
      ["start", "stop", "remove"].forEach(function (action) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = action === "remove" || action === "stop" ? "btn btn-danger" : "btn";
        button.textContent = action.charAt(0).toUpperCase() + action.slice(1);
        button.addEventListener("click", function () {
          post(action, { name: vendor.name }).then(function () {
            note(vendor.name + " " + action, false);
            load();
          }).catch(function (error) { note(error.message, true); });
        });
        actions.appendChild(button);
      });
      row.appendChild(actions);
      list.appendChild(row);
    });
    if (!(body.vendors || []).length) {
      var empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No sidecars saved.";
      list.appendChild(empty);
    }
  }

  function load() {
    fetch("/admin/vendors.php").then(function (response) { return response.json(); }).then(render).catch(function () {
      $("vendor-banner").hidden = false;
      $("vendor-banner").textContent = "The vendor list did not answer";
    });
  }

  $("vendor-form").addEventListener("submit", function (event) {
    event.preventDefault();
    post("save", {
      name: $("vendor-name").value,
      image: $("vendor-image").value,
      channel: $("vendor-channel").value,
      args: $("vendor-args").value
    }).then(function () {
      note("Saved " + $("vendor-name").value, false);
      load();
    }).catch(function (error) { note(error.message, true); });
  });

  $("vendor-pull").addEventListener("click", function () {
    post("pull", { image: $("vendor-image").value }).then(function (payload) {
      note("Pulled " + (payload.image || $("vendor-image").value), false);
    }).catch(function (error) { note(error.message, true); });
  });

  load();
}());
