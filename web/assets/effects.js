(function () {
  "use strict";

  var RASTERS = {
    "1080i59.94": [1920, 1080],
    "1080i50": [1920, 1080],
    "1080p59.94": [1920, 1080],
    "1080p60": [1920, 1080],
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
  var storedPresets = [];

  function raster() {
    return RASTERS[formatName] || [1920, 1080];
  }

  function identity() {
    var size = raster();
    return [[0, 0], [size[0], 0], [0, size[1]], [size[0], size[1]]];
  }

  var LAYER_SLOTS = 2;
  var LEVELS = [-4, -3, -2, -1, 1, 2, 3, 4];

  function levelLabel(level) {
    return level < 0 ? "\u2212" + Math.abs(level) : "+" + level;
  }

  function emptyLayers() {
    var defaults = [1, 2];
    var rows = [];
    for (var i = 0; i < LAYER_SLOTS; i++) {
      rows.push({
        enabled: i === 0,
        level: defaults[i],
        url: i === 0 ? "/cg/bug.html" : ""
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
      used.push({ level: Number(layer.level), source: { type: "html", url: layer.url } });
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

  function sizeToRaster(node) {
    var size = raster();
    node.style.width = size[0] + "px";
    node.style.height = size[1] + "px";
  }

  function fitStage() {
    var stage = document.getElementById("stage");
    var inner = document.getElementById("stage-scale");
    if (!stage || !inner) return;
    var size = raster();
    var width = stage.clientWidth;
    var height = stage.clientHeight;
    if (!width || !height) return;
    var scale = Math.min(width / size[0], height / size[1]);
    var offsetX = (width - size[0] * scale) / 2;
    var offsetY = (height - size[1] * scale) / 2;
    inner.style.width = size[0] + "px";
    inner.style.height = size[1] + "px";
    inner.style.transform = "translate(" + offsetX + "px, " + offsetY + "px) scale(" + scale + ")";
    sizeToRaster(document.getElementById("behind-layers"));
    sizeToRaster(document.getElementById("front-layers"));
    sizeToRaster(document.getElementById("squeeze"));
  }

  function paintLayers() {
    var behind = document.getElementById("behind-layers");
    var front = document.getElementById("front-layers");
    var size = raster();
    behind.innerHTML = "";
    front.innerHTML = "";
    layers.forEach(function (layer) {
      if (!layer.enabled) return;
      var node;
      if (layer.url) {
        node = document.createElement("iframe");
        node.src = previewUrl(layer.url);
        node.title = "CG layer " + levelLabel(layer.level);
        node.setAttribute("allowtransparency", "true");
        node.setAttribute("width", String(size[0]));
        node.setAttribute("height", String(size[1]));
        node.style.colorScheme = "normal";
        node.style.backgroundColor = "transparent";
      }
      if (!node) return;
      sizeToRaster(node);
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
    next.src = "/live/still.php?channel=" + channel + "&t=" + Date.now();
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
      card.appendChild(field("URL", text(layer.url, function (value) { layer.url = value; paintLayers(); }, "change")));
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
    var slot = 0;
    (preset.layers || []).forEach(function (layer) {
      if (slot >= LAYER_SLOTS) return;
      if (!layer.source || layer.source.type !== "html") return;
      var row = layers[slot];
      slot += 1;
      row.enabled = true;
      row.level = layer.level;
      row.url = layer.source.url;
    });
    document.getElementById("squeeze").width = size[0];
    document.getElementById("squeeze").height = size[1];
    buildCornerInputs();
    buildLayerCards();
    fitStage();
    draw();
    paintLayers();
  }

  function renderPresets(presets) {
    storedPresets = presets || [];
    var list = document.getElementById("preset-list");
    list.innerHTML = "";
    storedPresets.forEach(function (preset) {
      var item = document.createElement("li");
      var name = document.createElement("span");
      name.textContent = preset.name + " · " + preset.format;
      var recall = document.createElement("button");
      recall.type = "button";
      recall.className = "secondary";
      recall.textContent = "Recall";
      recall.addEventListener("click", function () {
        loadPreset(preset);
        post("/admin/recall.php?channel=" + channel, { preset: preset.name })
          .then(function (body) { note(true, recalled(body)); })
          .catch(function (err) { note(false, err.message); });
      });
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn-text";
      remove.textContent = "Delete";
      remove.addEventListener("click", function () {
        if (!window.confirm("Delete preset " + preset.name + "? The picture stays until you recall or normalize.")) return;
        post("/admin/preset.php", { delete: true, name: preset.name }).then(function (body) {
          renderPresets(body.presets || []);
          note(true, "Deleted " + preset.name + ".");
        }).catch(function (err) { note(false, err.message); });
      });
      var actions = document.createElement("span");
      actions.className = "preset-actions";
      actions.appendChild(recall);
      actions.appendChild(remove);
      item.appendChild(name);
      item.appendChild(actions);
      list.appendChild(item);
    });
  }

  function effectSlug(name) {
    var cleaned = "";
    var underscore = false;
    var text = String(name || "").trim().toLowerCase();
    var i;
    for (i = 0; i < text.length; i++) {
      var ch = text.charAt(i);
      var code = text.charCodeAt(i);
      var keep = code < 128 && ((code >= 48 && code <= 57) || (code >= 97 && code <= 122) || ch === "-");
      if (keep) {
        cleaned += ch;
        underscore = false;
      } else if (cleaned && !underscore) {
        cleaned += "_";
        underscore = true;
      } else {
        underscore = !!cleaned;
      }
    }
    return cleaned.replace(/^_+|_+$/g, "");
  }

  function urlLine(label, method, url) {
    var row = document.createElement("p");
    var title = document.createElement("strong");
    var code = document.createElement("code");
    row.className = "api-url";
    title.textContent = label;
    code.textContent = method + " " + url;
    row.appendChild(title);
    row.appendChild(code);
    return row;
  }

  function paintHelp() {
    var host = window.location.hostname || "this-host";
    var origin = window.location.protocol + "//" + host + ":8790";
    var box = document.getElementById("api-help-body");
    var counts = {};
    box.textContent = "";
    var intro = document.createElement("p");
    intro.textContent = "Send Authorization: Bearer and a token from Services. The examples below do not include a token.";
    box.appendChild(intro);
    storedPresets.forEach(function (preset) {
      var slug = effectSlug(preset.name);
      counts[slug] = (counts[slug] || 0) + 1;
    });
    storedPresets.forEach(function (preset) {
      var slug = effectSlug(preset.name);
      var block = document.createElement("section");
      var title = document.createElement("h3");
      title.textContent = preset.name;
      block.appendChild(title);
      if (counts[slug] > 1) {
        var warn = document.createElement("p");
        warn.className = "error";
        warn.textContent = "The slug " + slug + " is shared by more than one preset, so these URLs will be refused.";
        block.appendChild(warn);
      }
      ["a", "b"].forEach(function (id) {
        block.appendChild(urlLine("Recall on " + id.toUpperCase(), "POST", origin + "/api/noc/v1/outputs/" + id + "/effects/recall/" + slug));
        block.appendChild(urlLine("On air " + id.toUpperCase(), "GET", origin + "/api/noc/v1/outputs/" + id + "/effects/" + slug));
      });
      box.appendChild(block);
    });
    if (!storedPresets.length) {
      var empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "Save a preset and the recall and on-air URLs appear here.";
      box.appendChild(empty);
    }
    ["a", "b"].forEach(function (id) {
      box.appendChild(urlLine("Clear " + id.toUpperCase(), "POST", origin + "/api/noc/v1/outputs/" + id + "/effects/clear"));
      box.appendChild(urlLine("Restart " + id.toUpperCase(), "POST", origin + "/api/noc/v1/outputs/" + id + "/restart"));
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
    fetch("/admin/config.php").then(function (response) { return response.json(); }).then(function (body) {
      applyChannelFormat(body.config || {});
      corners = identity();
      buildCornerInputs();
      fitStage();
      paintLayers();
      draw();
    }).catch(function () { fitStage(); draw(); });
    fetch("/admin/preset.php").then(function (response) { return response.json(); }).then(function (body) {
      renderPresets(body.presets || []);
      if (body.html_renderer === "none") {
        note(false, "No HTML renderer on this host. Squeeze still recalls; HTML layers stay transparent until WPE or Chromium is installed.");
      }
    }).catch(function () {});
    document.getElementById("effect-channel").addEventListener("change", function (event) {
      channel = event.target.value;
      fetch("/admin/config.php").then(function (response) { return response.json(); }).then(function (body) {
        applyChannelFormat(body.config || {});
        corners = identity();
        buildCornerInputs();
        fitStage();
        paintLayers();
        refreshStill();
      }).catch(function () { fitStage(); refreshStill(); });
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
      post("/admin/preset.php", body).then(function () {
        note(true, "Saved " + body.name + ".");
        return fetch("/admin/preset.php").then(function (response) { return response.json(); });
      }).then(function (payload) {
        renderPresets(payload.presets || []);
      }).catch(function (err) { note(false, err.message); });
    });
    document.getElementById("recall-effect").addEventListener("click", function () {
      post("/admin/recall.php?channel=" + channel, { effect: effectBody() })
        .then(function (body) { note(true, recalled(body)); })
        .catch(function (err) { note(false, err.message); });
    });
    document.getElementById("normalize-effect").addEventListener("click", function () {
      post("/admin/normalize.php?channel=" + channel, { frames: clampFrames("out-frames") })
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
    if (window.ResizeObserver) {
      new ResizeObserver(function () { fitStage(); }).observe(document.getElementById("stage"));
    } else {
      window.addEventListener("resize", fitStage);
    }
    refreshStill();
    timer = window.setInterval(refreshStill, STILL_MS);
    fitStage();
    draw();
    paintLayers();
    var help = document.getElementById("api-help");
    if (help) {
      help.addEventListener("click", function () {
        paintHelp();
        var dialog = document.getElementById("api-help-dialog");
        if (dialog && typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
      });
    }
  }

  if (document.getElementById("squeeze")) boot();
  window.addEventListener("pagehide", function () { window.clearInterval(timer); });
})();
