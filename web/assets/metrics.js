(function () {
  "use strict";

  var WINDOW_MS = 15 * 60 * 1000;
  var PALETTE = ["#2563eb", "#3ecf8e", "#f5a524", "#f87171", "#7c5cbf", "#0b7ea4"];
  var history = {};
  var cards = [];
  var promUp = false;
  var selectedName = "";
  var names = [];

  var lastLocal = [];

  function $(id) { return document.getElementById(id); }

  function esc(text) {
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
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

  function remember(name, labels, value, time) {
    if (!isFinite(value)) return;
    var key = seriesKey(name, labels);
    var points = history[key] || (history[key] = []);
    var stamp = time || Date.now();
    var last = points[points.length - 1];
    if (last && Math.abs(last.t - stamp) < 1000) last.v = value;
    else points.push({ t: stamp, v: value });
    var cutoff = Date.now() - WINDOW_MS;
    while (points.length && points[0].t < cutoff) points.shift();
  }

  function replaceSeries(name, labels, points) {
    history[seriesKey(name, labels)] = points.filter(function (point) {
      return isFinite(point.v) && Date.now() - point.t <= WINDOW_MS;
    });
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

  function draw(canvas, series, kind) {
    var width = canvas.clientWidth || 280;
    var height = 120;
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
    var minV = kind === "percent" ? 0 : Infinity;
    var maxV = kind === "percent" ? 100 : -Infinity;
    var minT = Infinity;
    var maxT = -Infinity;
    plotted.forEach(function (item) {
      item.points.forEach(function (point) {
        if (kind !== "percent") {
          if (point.v < minV) minV = point.v;
          if (point.v > maxV) maxV = point.v;
        }
        if (point.t < minT) minT = point.t;
        if (point.t > maxT) maxT = point.t;
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
    ctx.strokeStyle = line;
    ctx.lineWidth = 1;
    for (var g = 1; g <= 3; g++) {
      var y = 8 + ((height - 16) * g) / 4;
      ctx.beginPath();
      ctx.moveTo(8, y);
      ctx.lineTo(width - 8, y);
      ctx.stroke();
    }
    plotted.forEach(function (item, index) {
      ctx.beginPath();
      ctx.strokeStyle = PALETTE[index % PALETTE.length];
      ctx.lineWidth = 1.75;
      item.points.forEach(function (point, i) {
        var x = 8 + ((point.t - minT) / (maxT - minT)) * (width - 16);
        var yv = 8 + (1 - (point.v - minV) / (maxV - minV)) * (height - 16);
        if (i === 0) ctx.moveTo(x, yv);
        else ctx.lineTo(x, yv);
      });
      ctx.stroke();
    });
    return true;
  }

  function cardNote(card, text) {
    var note = document.querySelector('[data-note="' + card.id + '"]');
    if (note) note.textContent = text;
  }

  function renderCard(card, samples) {
    var value = document.querySelector('[data-value="' + card.id + '"]');
    var canvas = document.querySelector('[data-canvas="' + card.id + '"]');
    if (card.kind === "info") {
      var host = document.querySelector('[data-info="' + card.id + '"]');
      if (!samples.length) {
        host.innerHTML = '<p class="muted">No samples yet</p>';
        return;
      }
      host.innerHTML = samples.map(function (sample) {
        var bits = Object.keys(sample.labels).map(function (key) {
          return "<div><dt>" + esc(key) + "</dt><dd>" + esc(sample.labels[key]) + "</dd></div>";
        }).join("");
        return '<dl class="channel-meta">' + bits + "</dl>";
      }).join("");
      return;
    }
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
      value.textContent = samples.map(function (sample) {
        var prefix = Object.keys(sample.labels).length ? labelText(sample.labels) + " " : "";
        return prefix + formatNumber(sample.value, card);
      }).join(" · ");
    }
    var drew = canvas ? draw(canvas, series, card.kind) : false;
    cardNote(card, drew ? (card.kind === "counter" ? "Rate per second, last 15 minutes" : "Last 15 minutes") : "Waiting for a second sample");
  }

  function buildCards() {
    ["host", "encoder"].forEach(function (group) {
      var root = $(group + "-charts");
      root.innerHTML = "";
      cards.filter(function (card) { return card.group === group; }).forEach(function (card) {
        var article = document.createElement("article");
        article.className = "panel chart-card";
        var title = document.createElement("h2");
        title.textContent = card.title;
        article.appendChild(title);
        if (card.kind === "info") {
          var info = document.createElement("div");
          info.setAttribute("data-info", card.id);
          article.appendChild(info);
        } else {
          var value = document.createElement("div");
          value.className = "chart-value";
          value.setAttribute("data-value", card.id);
          value.textContent = "—";
          var canvas = document.createElement("canvas");
          canvas.setAttribute("data-canvas", card.id);
          canvas.setAttribute("aria-label", card.title + " chart");
          article.appendChild(value);
          article.appendChild(canvas);
        }
        var note = document.createElement("p");
        note.className = "muted chart-note";
        note.setAttribute("data-note", card.id);
        article.appendChild(note);
        root.appendChild(article);
      });
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
      ? "/api/prom/query_range?query=" + encodeURIComponent(query)
        + "&start=" + ((Date.now() - WINDOW_MS) / 1000)
        + "&end=" + (Date.now() / 1000)
        + "&step=15"
      : "/api/prom/query?query=" + encodeURIComponent(query);
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

  function refreshHost(card) {
    var queries = queriesFor(card);
    var attempt = 0;
    function next() {
      if (attempt >= queries.length) {
        renderCard(card, []);
        if (card.id === "gpu") cardNote(card, promUp ? "No GPU exporter is reporting" : "Waiting for Prometheus");
        return;
      }
      promGet("range", queries[attempt]).then(function (payload) {
        if (payload && payload.ok === false) {
          promUp = false;
          setProm(false, payload.detail);
          renderCard(card, []);
          return;
        }
        var rows = matrix(payload);
        attempt += 1;
        if (!rows || !rows.length || !rows.some(function (row) { return row.points.length; })) {
          next();
          return;
        }
        promUp = true;
        setProm(true, "");
        renderCard(card, applyMatrix(card, rows, true));
      }).catch(function () {
        promUp = false;
        setProm(false, "Prometheus is not reachable");
        renderCard(card, []);
      });
    }
    next();
  }

  function refreshEncoder(samples) {
    var byName = {};
    samples.forEach(function (sample) {
      remember(sample.name, sample.labels, sample.value);
      (byName[sample.name] || (byName[sample.name] = [])).push(sample);
    });
    cards.filter(function (card) { return card.group === "encoder"; }).forEach(function (card) {
      renderCard(card, byName[card.query] || []);
    });
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
      + ". Host charts use node_exporter through Prometheus. Encoder series below keep updating from this console.";
  }

  function renderNames() {
    var filter = ($("series-filter").value || "").toLowerCase();
    var list = $("series-list");
    var shown = names.filter(function (name) { return !filter || name.toLowerCase().indexOf(filter) !== -1; });
    $("series-count").textContent = shown.length + " of " + names.length;
    list.innerHTML = "";
    shown.slice(0, 400).forEach(function (name) {
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
      return;
    }
    var query = /_total$/.test(selectedName) ? "rate(" + selectedName + "[1m])" : selectedName;
    var kind = /_total$/.test(selectedName) ? "gauge" : "gauge";
    meta.textContent = query;
    if (/_info$/.test(selectedName)) {
      promGet("instant", selectedName).then(function (payload) {
        var rows = matrix(payload) || [];
        info.innerHTML = rows.length ? rows.map(function (row) {
          return "<dl class=\"channel-meta\">" + Object.keys(row.labels).map(function (key) {
            return "<div><dt>" + esc(key) + "</dt><dd>" + esc(row.labels[key]) + "</dd></div>";
          }).join("") + "</dl>";
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
      draw(canvas, series, kind);
      if (!series.length) meta.textContent = query + " — no samples yet";
    }).catch(function () {
      meta.textContent = "Prometheus is not reachable";
    });
  }

  function refreshNames(localNames) {
    fetch("/api/prom/names").then(function (response) { return response.json(); }).then(function (payload) {
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

  function tick() {
    fetch("/metrics").then(function (response) { return response.text(); }).then(function (text) {
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
    cards.filter(function (card) { return card.group === "host"; }).forEach(refreshHost);
    if (selectedName) refreshExplorer();
  }

  fetch("/api/catalog").then(function (response) { return response.json(); }).then(function (payload) {
    cards = payload.cards || [];
    buildCards();
    tick();
    setInterval(tick, 2000);
    setInterval(function () { refreshNames(lastLocal); }, 15000);
  }).catch(function () {
    setProm(false, "The console catalog did not answer");
  });

  $("series-filter").addEventListener("input", renderNames);
})();
