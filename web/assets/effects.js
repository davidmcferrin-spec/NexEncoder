(function () {
  "use strict";

  var RASTERS = {
    "1080i59.94": [1920, 1080],
    "1080i50": [1920, 1080],
    "1080p59.94": [1920, 1080],
    "1080p50": [1920, 1080],
    "720p59.94": [1280, 720],
    "720p50": [1280, 720]
  };
  var LABELS = ["Top left", "Top right", "Bottom left", "Bottom right"];
  var STILL_MS = 8000;
  var channel = "A";
  var formatName = "1080i59.94";
  var corners = [];
  var layers = [];
  var still = new Image();
  var drag = -1;
  var timer = 0;

  function raster() {
    return RASTERS[formatName] || [1920, 1080];
  }

  function identity() {
    var size = raster();
    return [[0, 0], [size[0], 0], [0, size[1]], [size[0], size[1]]];
  }

  var LEVELS = [-4, -3, -2, -1, 1, 2, 3, 4];

  function levelLabel(level) {
    return level < 0 ? "\u2212" + Math.abs(level) : "+" + level;
  }

  function emptyLayers() {
    var defaults = [1, 2, -1, -2];
    var rows = [];
    for (var i = 0; i < 4; i++) {
      rows.push({
        enabled: i === 0,
        level: defaults[i],
        type: "html",
        url: i === 0 ? "/cg/bug.html" : "",
        name: ""
      });
    }
    return rows;
  }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "NexEncoder"
      },
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok || payload.ok === false) {
          throw new Error(payload.detail || "request failed");
        }
        return payload;
      });
    });
  }

  function note(ok, text) {
    var good = document.getElementById("effect-ok");
    var bad = document.getElementById("effect-error");
    good.hidden = !ok;
    bad.hidden = ok;
    (ok ? good : bad).textContent = text;
  }

  function effectBody() {
    var used = [];
    layers.forEach(function (layer) {
      if (!layer.enabled) return;
      var source = layer.type === "sdi-key"
        ? { type: "sdi-key" }
        : layer.type === "ndi"
          ? { type: "ndi", name: layer.name }
          : { type: "html", url: layer.url };
      used.push({ level: Number(layer.level), source: source });
    });
    return {
      format: formatName,
      corners: corners.map(function (point) {
        var size = raster();
        return [point[0] / size[0], point[1] / size[1]];
      }),
      in_frames: clampFrames("in-frames"),
      out_frames: clampFrames("out-frames"),
      layers: used
    };
  }

  function clampFrames(id) {
    var value = Number(document.getElementById(id).value);
    if (!Number.isInteger(value) || value < 0) return 0;
    if (value > 600) return 600;
    return value;
  }

  function lerp(a, b, t) {
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
  }

  function draw() {
    var canvas = document.getElementById("squeeze");
    var size = raster();
    if (canvas.width !== size[0] || canvas.height !== size[1]) {
      canvas.width = size[0];
      canvas.height = size[1];
    }
    var ctx = canvas.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (still.complete && still.naturalWidth) {
      var slices = 24;
      for (var i = 0; i < slices; i++) {
        var t0 = i / slices;
        var t1 = (i + 1) / slices;
        var l0 = lerp(corners[0], corners[2], t0);
        var l1 = lerp(corners[0], corners[2], t1);
        var r0 = lerp(corners[1], corners[3], t0);
        var sh = still.naturalHeight / slices;
        var sy = t0 * still.naturalHeight;
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(l0[0], l0[1]);
        ctx.lineTo(r0[0], r0[1]);
        ctx.lineTo(lerp(corners[1], corners[3], t1)[0], lerp(corners[1], corners[3], t1)[1]);
        ctx.lineTo(l1[0], l1[1]);
        ctx.closePath();
        ctx.clip();
        ctx.setTransform(
          (r0[0] - l0[0]) / still.naturalWidth,
          (r0[1] - l0[1]) / still.naturalWidth,
          (l1[0] - l0[0]) / sh,
          (l1[1] - l0[1]) / sh,
          l0[0],
          l0[1]
        );
        ctx.drawImage(still, 0, sy, still.naturalWidth, sh, 0, 0, still.naturalWidth, sh);
        ctx.restore();
      }
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.strokeStyle = "#3ecf8e";
    ctx.lineWidth = Math.max(2, canvas.width / 480);
    ctx.beginPath();
    ctx.moveTo(corners[0][0], corners[0][1]);
    ctx.lineTo(corners[1][0], corners[1][1]);
    ctx.lineTo(corners[3][0], corners[3][1]);
    ctx.lineTo(corners[2][0], corners[2][1]);
    ctx.closePath();
    ctx.stroke();
    corners.forEach(function (point) {
      ctx.beginPath();
      ctx.arc(point[0], point[1], Math.max(8, canvas.width / 120), 0, Math.PI * 2);
      ctx.fillStyle = "#3ecf8e";
      ctx.fill();
    });
  }

  function previewUrl(url) {
    var parsed = new URL(url, window.location.href);
    if (parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost") {
      return window.location.origin + parsed.pathname + parsed.search + parsed.hash;
    }
    return parsed.href;
  }

  function paintLayers() {
    var behind = document.getElementById("behind-layers");
    var front = document.getElementById("front-layers");
    behind.innerHTML = "";
    front.innerHTML = "";
    layers.forEach(function (layer) {
      if (!layer.enabled) return;
      var node;
      if (layer.type === "html" && layer.url) {
        node = document.createElement("iframe");
        node.src = previewUrl(layer.url);
        node.title = "CG layer " + levelLabel(layer.level);
      } else if (layer.type === "sdi-key") {
        node = document.createElement("div");
        node.className = "sdi-plate";
        node.textContent = "SDI fill/key \u00b7 DeckLink Duo 2 (3) / (4)";
      } else if (layer.type === "ndi" && layer.name) {
        node = document.createElement("div");
        node.className = "sdi-plate";
        node.textContent = "NDI \u00b7 " + layer.name;
      }
      if (!node) return;
      node.style.zIndex = String(layer.level + 4);
      (layer.level < 0 ? behind : front).appendChild(node);
    });
  }

  function refreshStill() {
    var next = new Image();
    next.onload = function () {
      still = next;
      draw();
      document.getElementById("still-note").textContent = "Clean still from input " + channel + ".";
    };
    next.onerror = function () {
      document.getElementById("still-note").textContent = "No still yet. The channel writes one every 8 seconds once it is running.";
      draw();
    };
    next.src = "/api/channels/" + channel + "/still?t=" + Date.now();
  }

  function pointFromEvent(event) {
    var canvas = document.getElementById("squeeze");
    var rect = canvas.getBoundingClientRect();
    return [
      (event.clientX - rect.left) * canvas.width / rect.width,
      (event.clientY - rect.top) * canvas.height / rect.height
    ];
  }

  function bindCanvas() {
    var canvas = document.getElementById("squeeze");
    canvas.addEventListener("pointerdown", function (event) {
      var point = pointFromEvent(event);
      var limit = Math.max(18, canvas.width / 80);
      drag = -1;
      corners.forEach(function (corner, index) {
        if (Math.hypot(corner[0] - point[0], corner[1] - point[1]) < limit) drag = index;
      });
      if (drag >= 0) canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener("pointermove", function (event) {
      if (drag < 0) return;
      var point = pointFromEvent(event);
      corners[drag] = point;
      syncCornerInputs();
      draw();
    });
    canvas.addEventListener("pointerup", function () { drag = -1; });
  }

  function syncCornerInputs() {
    corners.forEach(function (point, index) {
      var x = document.getElementById("corner-x-" + index);
      var y = document.getElementById("corner-y-" + index);
      if (!x || document.activeElement === x) return;
      x.value = String(Math.round(point[0]));
      y.value = String(Math.round(point[1]));
    });
  }

  function buildCornerInputs() {
    var root = document.getElementById("corner-grid");
    root.innerHTML = "";
    LABELS.forEach(function (label, index) {
      var block = document.createElement("div");
      block.innerHTML = "<label>" + label + "</label>";
      var row = document.createElement("div");
      row.className = "grid-2";
      ["x", "y"].forEach(function (axis, axisIndex) {
        var input = document.createElement("input");
        input.type = "number";
        input.id = "corner-" + axis + "-" + index;
        input.addEventListener("input", function () {
          var value = Number(input.value);
          if (!Number.isFinite(value)) return;
          corners[index][axisIndex] = value;
          draw();
        });
        row.appendChild(input);
      });
      block.appendChild(row);
      root.appendChild(block);
    });
    syncCornerInputs();
  }

  function buildLayerCards() {
    var root = document.getElementById("layer-list");
    root.innerHTML = "";
    layers.forEach(function (layer, index) {
      var card = document.createElement("div");
      card.className = "layer-card";
      card.innerHTML = "<h3>Layer " + (index + 1) + "</h3>";
      card.appendChild(field("On", check(layer.enabled, function (value) { layer.enabled = value; paintLayers(); })));
      card.appendChild(field("Level", select(
        LEVELS.map(function (level) { return [String(level), levelLabel(level)]; }),
        String(layer.level),
        function (value) { layer.level = Number(value); paintLayers(); }
      )));
      card.appendChild(field("Source", select(
        [["html", "HTML5 URL"], ["ndi", "NDI"], ["sdi-key", "SDI fill/key"]],
        layer.type,
        function (value) { layer.type = value; buildLayerCards(); paintLayers(); }
      )));
      if (layer.type === "html") {
        card.appendChild(field("URL", text(layer.url, function (value) { layer.url = value; paintLayers(); }, "change")));
      } else if (layer.type === "ndi") {
        card.appendChild(field("NDI source", text(layer.name, function (value) { layer.name = value; paintLayers(); }, "change")));
      } else {
        var fixed = document.createElement("p");
        fixed.className = "muted";
        fixed.textContent = "Fill DeckLink Duo 2 (3), key DeckLink Duo 2 (4).";
        card.appendChild(fixed);
      }
      root.appendChild(card);
    });
  }

  function field(label, control) {
    var wrap = document.createElement("div");
    wrap.className = "field-block";
    var title = document.createElement("label");
    title.textContent = label;
    wrap.appendChild(title);
    wrap.appendChild(control);
    return wrap;
  }

  function check(value, onChange) {
    var input = document.createElement("input");
    input.type = "checkbox";
    input.checked = value;
    input.addEventListener("change", function () { onChange(input.checked); });
    return input;
  }

  function select(options, value, onChange) {
    var input = document.createElement("select");
    options.forEach(function (option) {
      var node = document.createElement("option");
      node.value = option[0];
      node.textContent = option[1];
      input.appendChild(node);
    });
    input.value = value;
    input.addEventListener("change", function () { onChange(input.value); });
    return input;
  }

  function text(value, onChange, eventName) {
    var input = document.createElement("input");
    input.type = "text";
    input.value = value || "";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.addEventListener(eventName || "input", function () { onChange(input.value); });
    return input;
  }

  function loadPreset(preset) {
    var size = raster();
    var authored = RASTERS[preset.format] || size;
    corners = preset.corners.map(function (point) {
      var x = point[0];
      var y = point[1];
      if (Math.abs(x) > 2 || Math.abs(y) > 2) {
        x = x / authored[0];
        y = y / authored[1];
      }
      return [x * size[0], y * size[1]];
    });
    if (corners.length !== 4) corners = identity();
    document.getElementById("in-frames").value = String(preset.in_frames || 0);
    document.getElementById("out-frames").value = String(preset.out_frames || 0);
    document.getElementById("preset-name").value = preset.name || "";
    layers = emptyLayers();
    (preset.layers || []).forEach(function (layer, index) {
      if (index > 3) return;
      var row = layers[index];
      row.enabled = true;
      row.level = layer.level;
      row.type = layer.source.type;
      if (row.type === "html") row.url = layer.source.url;
      if (row.type === "ndi") row.name = layer.source.name;
    });
    document.getElementById("squeeze").width = size[0];
    document.getElementById("squeeze").height = size[1];
    buildCornerInputs();
    buildLayerCards();
    draw();
    paintLayers();
  }

  function renderPresets(presets) {
    var list = document.getElementById("preset-list");
    list.innerHTML = "";
    presets.forEach(function (preset) {
      var item = document.createElement("li");
      var name = document.createElement("span");
      name.textContent = preset.name + " · " + preset.format;
      var recall = document.createElement("button");
      recall.type = "button";
      recall.className = "secondary";
      recall.textContent = "Recall";
      recall.addEventListener("click", function () {
        loadPreset(preset);
        post("/api/channels/" + channel + "/effects/recall", { preset: preset.name })
          .then(function (body) { note(true, recalled(body)); })
          .catch(function (err) { note(false, err.message); });
      });
      item.appendChild(name);
      item.appendChild(recall);
      list.appendChild(item);
    });
  }

  function recalled(body) {
    var text = "Recalled on " + channel + " in " + body.frames + " frames.";
    if (body.warning) text += " " + body.warning;
    return text;
  }

  function applyChannelFormat(config) {
    var spec = config && config.inputs && config.inputs[channel];
    if (spec && RASTERS[spec.format]) formatName = spec.format;
    if (!corners.length) corners = identity();
  }

  function boot() {
    layers = emptyLayers();
    corners = identity();
    buildCornerInputs();
    buildLayerCards();
    bindCanvas();
    fetch("/api/config").then(function (response) { return response.json(); }).then(function (body) {
      applyChannelFormat(body.config || {});
      corners = identity();
      buildCornerInputs();
      draw();
    }).catch(function () { draw(); });
    fetch("/api/effects/presets").then(function (response) { return response.json(); }).then(function (body) {
      renderPresets(body.presets || []);
      if (body.html_renderer === "none") {
        note(false, "No HTML renderer on this host. Squeeze and SDI key still recall; HTML layers stay transparent until WPE or Chromium is installed.");
      }
    }).catch(function () {});
    document.getElementById("effect-channel").addEventListener("change", function (event) {
      channel = event.target.value;
      fetch("/api/config").then(function (response) { return response.json(); }).then(function (body) {
        applyChannelFormat(body.config || {});
        corners = identity();
        buildCornerInputs();
        refreshStill();
      }).catch(function () { refreshStill(); });
    });
    document.getElementById("refresh-still").addEventListener("click", refreshStill);
    document.getElementById("in-frames").addEventListener("change", function () {
      document.getElementById("in-frames").value = String(clampFrames("in-frames"));
    });
    document.getElementById("out-frames").addEventListener("change", function () {
      document.getElementById("out-frames").value = String(clampFrames("out-frames"));
    });
    document.getElementById("save-preset").addEventListener("click", function () {
      var body = effectBody();
      body.name = document.getElementById("preset-name").value.trim();
      post("/api/effects/presets", body).then(function () {
        note(true, "Saved " + body.name + ".");
        return fetch("/api/effects/presets").then(function (response) { return response.json(); });
      }).then(function (payload) {
        renderPresets(payload.presets || []);
      }).catch(function (err) { note(false, err.message); });
    });
    document.getElementById("recall-effect").addEventListener("click", function () {
      post("/api/channels/" + channel + "/effects/recall", { effect: effectBody() })
        .then(function (body) { note(true, recalled(body)); })
        .catch(function (err) { note(false, err.message); });
    });
    document.getElementById("normalize-effect").addEventListener("click", function () {
      post("/api/channels/" + channel + "/effects/normalize", { frames: clampFrames("out-frames") })
        .then(function (body) {
          corners = identity();
          layers.forEach(function (layer) { layer.enabled = false; });
          buildCornerInputs();
          buildLayerCards();
          draw();
          paintLayers();
          note(true, "Normalizing " + channel + " in " + body.frames + " frames.");
        })
        .catch(function (err) { note(false, err.message); });
    });
    refreshStill();
    timer = window.setInterval(refreshStill, STILL_MS);
    draw();
    paintLayers();
  }

  if (document.getElementById("squeeze")) boot();
  window.addEventListener("pagehide", function () { window.clearInterval(timer); });
})();
