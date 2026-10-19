(function () {
  "use strict";

  var RANGES = { "15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "6h": 6 * 60 * 60 * 1000, "24h": 24 * 60 * 60 * 1000 };
  var RANGE_NOTE = {
    "15m": "Last 15 minutes",
    "1h": "Last hour",
    "6h": "Last 6 hours",
    "24h": "Last 24 hours"
  };
  var MAX_CUSTOM_MS = 7 * 24 * 60 * 60 * 1000;
  var PALETTE = ["#2563eb", "#3ecf8e", "#f5a524", "#f87171", "#7c5cbf", "#0b7ea4"];
  var AUTO_KEY = "nexencoder-metrics-auto";
  var history = {};
  var cards = [];
  var promUp = false;
  var selectedName = "";
  var names = [];
  var lastLocal = [];
  var lastRendered = {};
  var lastExplorer = null;
  var range = "15m";
  var customFrom = null;
  var customTo = null;
  var localTimer = null;
  var promTimer = null;
  var promBusy = false;
  var promAgain = false;

  function $(id) { return document.getElementById(id); }

  function esc(text) {
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function autoPref() {
    try {
      var raw = localStorage.getItem(AUTO_KEY);
      if (raw === null || raw === "") return true;
      return raw === "1";
    } catch (err) {
      return true;
    }
  }

  function windowBounds() {
    if (customFrom != null && customTo != null) return { start: customFrom, end: customTo };
    var span = RANGES[range] || RANGES["15m"];
    var end = Date.now();
    return { start: end - span, end: end };
  }

  function inWindow(stamp) {
    var bounds = windowBounds();
    return stamp >= bounds.start && stamp <= bounds.end + 5000;
  }

  function stepSeconds(spanMs) {
    var step = Math.max(15, Math.round(spanMs / 1000 / 400));
    var choices = [15, 30, 60, 120, 300, 600, 900];
    for (var i = 0; i < choices.length; i++) {
      if (choices[i] >= step) return choices[i];
    }
    return 900;
  }

  function rangeNote() {
    if (customFrom != null && customTo != null) return fmtAxis(customFrom) + " – " + fmtAxis(customTo);
    return RANGE_NOTE[range] || RANGE_NOTE["15m"];
  }

  function fmtAxis(ms) {
    var when = new Date(ms);
    var bounds = windowBounds();
    var sameDay = when.toDateString() === new Date().toDateString() && (bounds.end - bounds.start) < 36 * 60 * 60 * 1000;
    if (sameDay) return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return when.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function labelText(labels) {
    var parts = [];
    Object.keys(labels || {}).forEach(function (key) {
      if (key !== "__name__" && key !== "job" && key !== "instance") parts.push(labels[key]);
    });
    return parts.join(" ") || "value";
  }

  function seriesKey(name, labels) {
    var keys = Object.keys(labels || {}).filter(function (key) { return key !== "__name__"; }).sort();
    return name + "\n" + keys.map(function (key) { return key + "=" + labels[key]; }).join(",");
  }

  function parseKey(key) {
    var parts = key.split("\n");
    var labels = {};
    (parts[1] || "").split(",").forEach(function (part) {
      if (!part) return;
      var eq = part.indexOf("=");
      if (eq === -1) return;
      labels[part.slice(0, eq)] = part.slice(eq + 1);
    });
    return { name: parts[0], labels: labels };
  }

  function remember(name, labels, value, time) {
    if (!isFinite(value)) return;
    var stamp = time || Date.now();
    if (!inWindow(stamp)) return;
    var key = seriesKey(name, labels);
    var points = history[key] || (history[key] = []);
    var last = points[points.length - 1];
    if (last && Math.abs(last.t - stamp) < 1000) last.v = value;
    else points.push({ t: stamp, v: value });
    trim(points);
  }

  function trim(points) {
    var bounds = windowBounds();
    while (points.length && points[0].t < bounds.start) points.shift();
    while (points.length && points[points.length - 1].t > bounds.end + 5000) points.pop();
  }

  function replaceSeries(name, labels, points) {
    var key = seriesKey(name, labels);
    var incoming = points.filter(function (point) {
      return isFinite(point.v) && inWindow(point.t);
    });
    var newest = incoming.length ? incoming[incoming.length - 1].t : 0;
    var extra = (history[key] || []).filter(function (point) {
      return point.t > newest && inWindow(point.t);
    });
    history[key] = incoming.concat(extra);
  }

  function pointsFor(name, labels) {
    return history[seriesKey(name, labels)] || [];
  }

  function parseExposition(text) {
    var samples = [];
    text.split("\n").forEach(function (line) {
      if (!line || line.charAt(0) === "#") return;
      var match = line.match(/^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{([^}]*)\})?\s+([^\s]+)$/);
      if (!match) return;
      var labels = {};
      if (match[3]) {
        match[3].replace(/([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"/g, function (_, key, value) {
          labels[key] = value.replace(/\\n/g, "\n").replace(/\\"/g, '"').replace(/\\\\/g, "\\");
          return "";
        });
      }
      var numeric = Number(match[4]);
      if (!isFinite(numeric)) return;
      samples.push({ name: match[1], labels: labels, value: numeric });
    });
    return samples;
  }

  function formatNumber(value, card) {
    if (!isFinite(value)) return "—";
    if (card.kind === "percent") return value.toFixed(1) + "%";
    if (card.kind === "duration") return formatDuration(value);
    if (card.unit === "bps") return (value / 1e6).toFixed(2) + " Mbps";
    if (card.unit === "s") return value.toFixed(3) + " s";
    if (Math.abs(value) >= 100) return String(Math.round(value));
    if (Number.isInteger(value)) return String(value);
    return String(Math.round(value * 1000) / 1000);
  }

  function formatDuration(seconds) {
    var whole = Math.max(0, Math.floor(seconds));
    var days = Math.floor(whole / 86400);
    var hours = Math.floor((whole % 86400) / 3600);
    var minutes = Math.floor((whole % 3600) / 60);
    if (days) return days + "d " + hours + "h";
    if (hours) return hours + "h " + minutes + "m";
    return minutes + "m";
  }

  function rates(points) {
    var out = [];
    for (var i = 1; i < points.length; i++) {
      var dt = (points[i].t - points[i - 1].t) / 1000;
      if (dt <= 0) continue;
      out.push({ t: points[i].t, v: Math.max(0, (points[i].v - points[i - 1].v) / dt) });
    }
    return out;
  }

  function isChart(card) {
    return card.kind !== "info" && card.id !== "uptime" && card.id !== "input-up";
  }

  function draw(canvas, series, kind) {
    var width = canvas.clientWidth || 280;
    var height = canvas.clientHeight || 240;
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    var styles = getComputedStyle(document.documentElement);
    ctx.fillStyle = styles.getPropertyValue("--nx-surface").trim() || "#fff";
    ctx.fillRect(0, 0, width, height);
    var plotted = series.map(function (item) {
      var pts = kind === "counter" ? rates(item.points) : item.points;
      return { label: item.label, points: pts };
    }).filter(function (item) { return item.points.length; });
    if (!plotted.length) return false;
    var bounds = windowBounds();
    var minT = bounds.start;
    var maxT = bounds.end;
    var minV = kind === "percent" ? 0 : Infinity;
    var maxV = kind === "percent" ? 100 : -Infinity;
    plotted.forEach(function (item) {
      item.points.forEach(function (point) {
        if (kind !== "percent") {
          if (point.v < minV) minV = point.v;
          if (point.v > maxV) maxV = point.v;
        }
      });
    });
    if (kind !== "percent") {
      if (minV === maxV) {
        minV = Math.min(0, minV);
        maxV = maxV + (maxV === 0 ? 1 : Math.abs(maxV) * 0.1);
      }
      var pad = (maxV - minV) * 0.08;
      minV -= pad;
      maxV += pad;
    }
    if (maxT === minT) maxT = minT + 1000;
    var line = styles.getPropertyValue("--nx-line").trim() || "#e5e7eb";
    var muted = styles.getPropertyValue("--nx-muted").trim() || "#667085";
    ctx.strokeStyle = line;
    ctx.lineWidth = 1;
    ctx.fillStyle = muted;
    ctx.font = "11px ui-monospace, Consolas, monospace";
    for (var g = 1; g <= 3; g++) {
      var y = 16 + ((height - 36) * g) / 4;
      ctx.beginPath();
      ctx.moveTo(8, y);
      ctx.lineTo(width - 8, y);
      ctx.stroke();
    }
    ctx.fillText(fmtAxis(minT), 8, height - 6);
    var endLabel = fmtAxis(maxT);
    ctx.fillText(endLabel, Math.max(8, width - 8 - ctx.measureText(endLabel).width), height - 6);
    plotted.forEach(function (item, index) {
      ctx.beginPath();
      ctx.strokeStyle = PALETTE[index % PALETTE.length];
      ctx.lineWidth = 1.75;
      var moved = false;
      item.points.forEach(function (point) {
        var x = 8 + ((point.t - minT) / (maxT - minT)) * (width - 16);
        var yv = 8 + (1 - (point.v - minV) / (maxV - minV)) * (height - 28);
        if (!moved) {
          ctx.moveTo(x, yv);
          moved = true;
        } else ctx.lineTo(x, yv);
      });
      if (moved) ctx.stroke();
    });
    return true;
  }

  function cardNote(card, text) {
    var note = document.querySelector('[data-note="' + card.id + '"]');
    if (note) note.textContent = text;
  }

  var INFO_TITLES = {
    input: "Input",
    output: "Output",
    format: "Format",
    scan: "Scan",
    audio: "Audio",
    mode: "Delivery",
    raster: "Raster",
    field_rate_hz: "Field rate"
  };

  function infoValue(key, value) {
    if (key === "raster") return String(value).replace("x", "\u00d7");
    if (key === "field_rate_hz") return value + " Hz";
    return value;
  }

  function infoList(labels) {
    return "<dl class=\"channel-meta\">" + Object.keys(labels).map(function (key) {
      if (key === "__name__") return "";
      var title = INFO_TITLES[key] || key;
      return "<div><dt>" + esc(title) + "</dt><dd>" + esc(infoValue(key, labels[key])) + "</dd></div>";
    }).join("") + "</dl>";
  }

  function renderCard(card, samples) {
    lastRendered[card.id] = samples;
    var value = document.querySelector('[data-value="' + card.id + '"]');
    var canvas = document.querySelector('[data-canvas="' + card.id + '"]');
    if (!samples.length) {
      if (value) value.textContent = "—";
      cardNote(card, card.group === "host" && !promUp ? "Waiting for Prometheus" : "No samples yet");
      if (canvas) draw(canvas, [], card.kind);
      return;
    }
    var series = samples.map(function (sample) {
      return { label: labelText(sample.labels), points: pointsFor(sample.name, sample.labels) };
    });
    if (value) {
      var live = inWindow(Date.now());
      value.textContent = samples.map(function (sample) {
        var points = pointsFor(sample.name, sample.labels);
        var shownValue = !live && points.length ? points[points.length - 1].v : sample.value;
        var prefix = Object.keys(sample.labels).length ? labelText(sample.labels) + " " : "";
        return prefix + formatNumber(shownValue, card);
      }).join(" · ");
    }
    var drew = canvas ? draw(canvas, series, card.kind) : false;
    var note = card.kind === "counter" ? "Rate per second · " + rangeNote() : rangeNote();
    cardNote(card, drew ? note : "Waiting for a second sample");
  }

  function renderUptime(samples) {
    var el = $("host-uptime");
    if (!el) return;
    var value = samples.length ? samples[0].value : null;
    if (!isFinite(value)) {
      el.innerHTML = "Host uptime: <strong>unavailable</strong>";
      return;
    }
    el.innerHTML = "Host uptime: <strong>" + formatDuration(value) + "</strong>";
  }

  function renderLock() {
    var wrap = $("lock-timeline");
    var empty = $("lock-empty");
    if (!wrap) return;
    var bounds = windowBounds();
    var span = Math.max(1, bounds.end - bounds.start);
    var groups = {};
    Object.keys(history).forEach(function (key) {
      var parsed = parseKey(key);
      if (parsed.name !== "nexencoder_input_up") return;
      var input = parsed.labels.input || "input";
      groups[input] = history[key];
    });
    var keys = Object.keys(groups).sort();
    wrap.innerHTML = "";
    if (!keys.length) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    keys.forEach(function (input) {
      var points = groups[input].slice().sort(function (a, b) { return a.t - b.t; });
      var deltas = [];
      for (var i = 1; i < points.length; i++) deltas.push((points[i].t - points[i - 1].t) / 1000);
      deltas.sort(function (a, b) { return a - b; });
      var median = deltas.length ? deltas[Math.floor(deltas.length / 2)] : 15;
      var gapMs = Math.max(60, median * 3) * 1000;
      var segs = [];
      points.forEach(function (point) {
        var state = point.v >= 0.5 ? "locked" : "unlocked";
        var endMs = point.t + gapMs;
        var prev = segs[segs.length - 1];
        if (prev && prev.state === state && point.t - prev.rawEnd <= gapMs) {
          prev.rawEnd = point.t;
          prev.end = endMs;
        } else {
          if (prev) prev.end = Math.min(prev.end, point.t);
          segs.push({ state: state, start: point.t, rawEnd: point.t, end: endMs });
        }
      });
      if (segs.length) segs[segs.length - 1].end = Math.min(segs[segs.length - 1].end, bounds.end);
      var row = document.createElement("div");
      row.className = "lock-row";
      var label = document.createElement("span");
      label.className = "lock-label";
      label.textContent = "Input " + input;
      var strip = document.createElement("div");
      strip.className = "lock-strip";
      segs.forEach(function (seg) {
        var el = document.createElement("span");
        el.className = "lock-seg " + seg.state;
        var left = Math.max(0, (seg.start - bounds.start) / span * 100);
        var width = Math.max(0.15, (seg.end - seg.start) / span * 100);
        el.style.left = left.toFixed(3) + "%";
        el.style.width = Math.min(width, 100 - left).toFixed(3) + "%";
        el.title = seg.state + "\n" + fmtAxis(seg.start) + " – " + fmtAxis(seg.end);
        strip.appendChild(el);
      });
      row.appendChild(label);
      row.appendChild(strip);
      wrap.appendChild(row);
    });
    var axis = document.createElement("div");
    axis.className = "lock-axis";
    var start = document.createElement("span");
    start.textContent = fmtAxis(bounds.start);
    var end = document.createElement("span");
    end.textContent = fmtAxis(bounds.end);
    axis.appendChild(start);
    axis.appendChild(end);
    wrap.appendChild(axis);
  }

  function channelLabels(samples, name, key) {
    var found = {};
    samples.forEach(function (sample) {
      if (sample.name !== name) return;
      var id = sample.labels[key] || "";
      if (id) found[id] = sample.labels;
    });
    return found;
  }

  function cell(labels, key) {
    if (!labels || labels[key] == null || labels[key] === "") return "—";
    return infoValue(key, labels[key]);
  }

  function renderInfo(samples) {
    var host = $("info-table");
    if (!host) return;
    var inputs = channelLabels(samples, "nexencoder_input_info", "input");
    var outputs = channelLabels(samples, "nexencoder_output_info", "output");
    var ids = {};
    Object.keys(inputs).forEach(function (id) { ids[id] = true; });
    Object.keys(outputs).forEach(function (id) { ids[id] = true; });
    var keys = Object.keys(ids).sort();
    if (!keys.length) {
      host.innerHTML = '<p class="muted">No samples yet</p>';
      return;
    }
    var rows = keys.map(function (id) {
      var input = inputs[id] || {};
      var output = outputs[id] || {};
      return "<tr><td>" + esc(id) + "</td><td>" + esc(cell(input, "format")) + "</td><td>"
        + esc(cell(input, "scan")) + "</td><td>" + esc(cell(input, "audio")) + "</td><td>"
        + esc(cell(input, "raster")) + "</td><td>" + esc(cell(input, "field_rate_hz")) + "</td><td>"
        + esc(cell(output, "mode")) + "</td></tr>";
    }).join("");
    host.innerHTML = '<table class="metrics-table"><thead><tr><th>Channel</th><th>Format</th><th>Scan</th><th>Audio</th><th>Raster</th><th>Field rate</th><th>Delivery</th></tr></thead><tbody>'
      + rows + "</tbody></table>";
  }

  function buildCards() {
    var root = $("chart-grid");
    root.innerHTML = "";
    cards.filter(isChart).forEach(function (card) {
      var article = document.createElement("article");
      article.className = "panel chart-card";
      var head = document.createElement("div");
      head.className = "chart-head";
      var title = document.createElement("h2");
      title.textContent = card.title;
      var value = document.createElement("div");
      value.className = "chart-value";
      value.setAttribute("data-value", card.id);
      value.textContent = "—";
      head.appendChild(title);
      head.appendChild(value);
      var canvas = document.createElement("canvas");
      canvas.setAttribute("data-canvas", card.id);
      canvas.setAttribute("aria-label", card.title + " chart");
      var note = document.createElement("p");
      note.className = "muted chart-note";
      note.setAttribute("data-note", card.id);
      article.appendChild(head);
      article.appendChild(canvas);
      article.appendChild(note);
      root.appendChild(article);
    });
  }

  function matrix(payload) {
    if (!payload || payload.status !== "success" || !payload.data) return null;
    return (payload.data.result || []).map(function (series) {
      var raw = series.values || (series.value ? [series.value] : []);
      return {
        labels: series.metric || {},
        points: raw.map(function (pair) { return { t: Number(pair[0]) * 1000, v: Number(pair[1]) }; })
          .filter(function (point) { return isFinite(point.v); })
      };
    });
  }

  function promGet(kind, query) {
    var url = kind === "range"
      ? (function () {
        var bounds = windowBounds();
        return "/live/prom.php?kind=query_range&query=" + encodeURIComponent(query)
          + "&start=" + (bounds.start / 1000)
          + "&end=" + (bounds.end / 1000)
          + "&step=" + stepSeconds(bounds.end - bounds.start);
      }())
      : "/live/prom.php?kind=query&query=" + encodeURIComponent(query);
    return fetch(url).then(function (response) { return response.json(); });
  }

  function queriesFor(card) {
    if (card.queries && card.queries.length) return card.queries;
    return [card.query];
  }

  function applyMatrix(card, rows, replace) {
    var samples = [];
    rows.forEach(function (row) {
      var labels = {};
      Object.keys(row.labels).forEach(function (key) {
        if (key !== "__name__") labels[key] = row.labels[key];
      });
      var name = row.labels.__name__ || card.query;
      var last = row.points[row.points.length - 1];
      if (replace) replaceSeries(name, labels, row.points);
      else if (last) remember(name, labels, last.v, last.t);
      if (last) samples.push({ name: name, labels: labels, value: last.v });
    });
    return samples;
  }

  function showCard(card, samples) {
    if (card.id === "uptime") renderUptime(samples);
    else if (card.id === "input-up") renderLock();
    else if (isChart(card)) renderCard(card, samples);
  }

  function refreshCard(card) {
    var queries = queriesFor(card);
    var attempt = 0;
    function next() {
      if (attempt >= queries.length) {
        showCard(card, []);
        if (card.id === "gpu") cardNote(card, promUp ? "No GPU exporter is reporting" : "Waiting for Prometheus");
        return Promise.resolve();
      }
      return promGet("range", queries[attempt]).then(function (payload) {
        if (payload && payload.ok === false) {
          promUp = false;
          setProm(false, payload.detail);
          showCard(card, []);
          return;
        }
        var rows = matrix(payload);
        attempt += 1;
        if (!rows || !rows.length || !rows.some(function (row) { return row.points.length; })) return next();
        promUp = true;
        setProm(true, "");
        showCard(card, applyMatrix(card, rows, true));
      }).catch(function () {
        promUp = false;
        setProm(false, "Prometheus is not reachable");
        showCard(card, []);
      });
    }
    return next();
  }

  function refreshEncoder(samples) {
    var byName = {};
    samples.forEach(function (sample) {
      remember(sample.name, sample.labels, sample.value);
      (byName[sample.name] || (byName[sample.name] = [])).push(sample);
    });
    cards.filter(function (card) { return card.group === "encoder" && isChart(card); }).forEach(function (card) {
      var rows = byName[card.query] || [];
      if (!rows.length && promUp) {
        var kept = Object.keys(history).some(function (key) { return key.split("\n")[0] === card.query; });
        if (kept) return;
      }
      renderCard(card, rows);
    });
    renderInfo(samples);
    renderLock();
  }

  function setProm(up, detail) {
    var badge = $("prom-status");
    var banner = $("prom-banner");
    if (up) {
      badge.className = "badge badge-ok";
      badge.textContent = "Prometheus connected";
      banner.hidden = true;
      return;
    }
    badge.className = "badge badge-warn";
    badge.textContent = "Prometheus offline";
    banner.hidden = false;
    banner.textContent = (detail || "Prometheus is not reachable")
      + ". Host charts use node_exporter through Prometheus. Encoder charts keep updating from this console.";
  }

  function renderNames() {
    var filter = ($("series-filter").value || "").toLowerCase();
    var list = $("series-list");
    var shownNames = names.filter(function (name) { return !filter || name.toLowerCase().indexOf(filter) !== -1; });
    $("series-count").textContent = shownNames.length + " of " + names.length;
    list.innerHTML = "";
    shownNames.slice(0, 400).forEach(function (name) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-secondary btn-sm" + (name === selectedName ? " is-selected" : "");
      button.textContent = name;
      button.addEventListener("click", function () {
        selectedName = name;
        renderNames();
        refreshExplorer();
      });
      list.appendChild(button);
    });
  }

  function refreshExplorer() {
    var meta = $("explorer-meta");
    var canvas = $("explorer-chart");
    var info = $("explorer-info");
    if (!selectedName) {
      meta.textContent = "Select a series to chart it.";
      info.innerHTML = "";
      lastExplorer = null;
      return;
    }
    var query = /_total$/.test(selectedName) ? "rate(" + selectedName + "[1m])" : selectedName;
    meta.textContent = query + " · " + rangeNote();
    if (/_info$/.test(selectedName)) {
      promGet("instant", selectedName).then(function (payload) {
        var rows = matrix(payload) || [];
        info.innerHTML = rows.length ? rows.map(function (row) {
          return infoList(row.labels);
        }).join("") : "<p class=\"muted\">No samples yet</p>";
        draw(canvas, [], "gauge");
      });
      return;
    }
    info.innerHTML = "";
    promGet("range", query).then(function (payload) {
      var rows = matrix(payload);
      if (!rows) {
        meta.textContent = (payload && payload.detail) || (payload && payload.error) || "No samples yet";
        return;
      }
      var series = rows.map(function (row) { return { label: labelText(row.labels), points: row.points }; });
      lastExplorer = { series: series, kind: "gauge" };
      draw(canvas, series, "gauge");
      if (!series.length) meta.textContent = query + " — no samples yet";
    }).catch(function () {
      meta.textContent = "Prometheus is not reachable";
    });
  }

  function refreshNames(localNames) {
    fetch("/live/prom.php?kind=names").then(function (response) { return response.json(); }).then(function (payload) {
      var remote = payload && payload.status === "success" && Array.isArray(payload.data) ? payload.data : [];
      var merged = {};
      remote.concat(localNames).forEach(function (name) { merged[name] = true; });
      names = Object.keys(merged).sort();
      renderNames();
    }).catch(function () {
      names = localNames.slice().sort();
      renderNames();
    });
  }

  function tickLocal() {
    fetch("/live/metrics.php").then(function (response) { return response.text(); }).then(function (text) {
      var samples = parseExposition(text);
      refreshEncoder(samples);
      var localNames = [];
      var seen = {};
      samples.forEach(function (sample) {
        if (!seen[sample.name]) {
          seen[sample.name] = true;
          localNames.push(sample.name);
        }
      });
      lastLocal = localNames;
      if (!names.length) refreshNames(localNames);
      else {
        localNames.forEach(function (name) {
          if (names.indexOf(name) === -1) names.push(name);
        });
        names.sort();
      }
    }).catch(function () {
      setProm(false, "The console metrics endpoint did not answer");
    });
  }

  function tickProm() {
    if (promBusy) {
      promAgain = true;
      return;
    }
    promBusy = true;
    promAgain = false;
    Promise.all(cards.filter(function (card) { return card.kind !== "info"; }).map(refreshCard)).then(function () {
      promBusy = false;
      renderLock();
      if (promAgain) tickProm();
    }, function () {
      promBusy = false;
      if (promAgain) tickProm();
    });
    if (selectedName) refreshExplorer();
  }

  function setRangeNote(text) {
    var note = $("range-note");
    if (note) note.textContent = text || "";
  }

  function selectPreset(next) {
    range = next;
    customFrom = null;
    customTo = null;
    $("from-dt").value = "";
    $("to-dt").value = "";
    document.querySelectorAll("button.range").forEach(function (button) {
      button.classList.toggle("is-selected", button.dataset.range === range);
    });
    setRangeNote("");
    Object.keys(history).forEach(function (key) { trim(history[key]); });
    tickLocal();
    tickProm();
  }

  function arm() {
    if (localTimer) clearInterval(localTimer);
    if (promTimer) clearInterval(promTimer);
    localTimer = null;
    promTimer = null;
    if (!$("auto-refresh").checked) return;
    localTimer = setInterval(tickLocal, 2000);
    promTimer = setInterval(tickProm, 15000);
  }

  function fromDatetimeLocal(value) {
    if (!value) return null;
    var ms = Date.parse(value);
    return isFinite(ms) ? ms : null;
  }

  var autoBox = $("auto-refresh");
  if (autoBox) autoBox.checked = autoPref();

  fetch("/admin/catalog.php").then(function (response) { return response.json(); }).then(function (payload) {
    cards = payload.cards || [];
    buildCards();
    tickLocal();
    tickProm();
    arm();
    setInterval(function () { refreshNames(lastLocal); }, 15000);
  }).catch(function () {
    setProm(false, "The console catalog did not answer");
  });

  $("series-filter").addEventListener("input", renderNames);
  document.querySelectorAll("button.range").forEach(function (button) {
    button.addEventListener("click", function () { selectPreset(button.dataset.range); });
  });
  $("range-apply").addEventListener("click", function () {
    var from = fromDatetimeLocal($("from-dt").value);
    var to = fromDatetimeLocal($("to-dt").value);
    if (from == null || to == null) {
      setRangeNote("Set both from and to");
      return;
    }
    if (from >= to) {
      setRangeNote("From must be earlier than to");
      return;
    }
    if ((to - from) > MAX_CUSTOM_MS) {
      setRangeNote("Custom window must be at most 7 days");
      return;
    }
    customFrom = from;
    customTo = to;
    document.querySelectorAll("button.range").forEach(function (button) { button.classList.remove("is-selected"); });
    setRangeNote("");
    Object.keys(history).forEach(function (key) { trim(history[key]); });
    tickLocal();
    tickProm();
  });
  $("range-clear").addEventListener("click", function () {
    if (!RANGES[range]) range = "15m";
    selectPreset(range);
  });
  $("auto-refresh").addEventListener("change", function () {
    try { localStorage.setItem(AUTO_KEY, autoBox.checked ? "1" : "0"); } catch (err) { /* private mode */ }
    arm();
    if (autoBox.checked) {
      tickLocal();
      tickProm();
    }
  });
  window.addEventListener("resize", function () {
    cards.filter(isChart).forEach(function (card) {
      if (lastRendered[card.id]) renderCard(card, lastRendered[card.id]);
    });
    if (lastExplorer) draw($("explorer-chart"), lastExplorer.series, lastExplorer.kind);
  });
})();
