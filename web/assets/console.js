(function () {
  "use strict";

  var STORAGE_KEY = "nexencoder-config";
  var CHANNELS = ["A", "B"];
  var FORMATS = {
    "1080i59.94": "1080i 59.94",
    "1080i50": "1080i 50",
    "1080p59.94": "1080p 59.94",
    "1080p60": "1080p 60",
    "1080p50": "1080p 50",
    "720p59.94": "720p 59.94",
    "720p50": "720p 50"
  };
  var SOURCES = ["sdi", "srt"];
  var WAITING = ["slate", "loop", "bars"];
  var ENDPOINTS = {
    A: { sdi: "DeckLink Duo (1)", srt: "SRT listener · port 9001" },
    B: { sdi: "DeckLink Duo (2)", srt: "SRT listener · port 9002" }
  };
  var MODES = ["srt-caller", "srt-listener", "hls-push", "hls-pull", "sdi"];
  var MODE_LABELS = {
    "srt-caller": "SRT caller",
    "srt-listener": "SRT listener",
    "hls-push": "HLS push",
    "hls-pull": "HLS pull",
    "sdi": "SDI"
  };
  var SDI_DEVICE = { A: "DeckLink Duo (3)", B: "DeckLink Duo (4)" };
  var SLOTS = ["1", "2", "3", "4"];
  var MAX_OUTPUTS = 4;
  var RESERVED_PORTS = { 9001: true, 9002: true };
  var DEFAULT_PORTS = { A: 9000, B: 9101 };
  var DEFAULTS = {
    inputs: {
      A: { format: "1080i59.94", device: "DeckLink Duo (1)", source: "sdi", waiting: "slate", loop: "", bitrate: 8 },
      B: { format: "1080i59.94", device: "DeckLink Duo (2)", source: "sdi", waiting: "slate", loop: "", bitrate: 8 }
    },
    outputs: {
      A: [{ id: "1", mode: "srt-listener", host: "", port: 9000, url: "" }],
      B: [{ id: "1", mode: "srt-listener", host: "", port: 9101, url: "" }]
    }
  };
  var dirty = false;
  var bars = { A: false, B: false };

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function asDestinations(raw) {
    if (Array.isArray(raw)) return raw.filter(function (item) { return item && typeof item === "object"; }).slice(0, MAX_OUTPUTS);
    if (raw && typeof raw === "object") return [raw];
    return [];
  }

  function legalBitrate(value) {
    var bitrate = Number(value);
    return Number.isInteger(bitrate) && bitrate >= 6 && bitrate <= 18 ? bitrate : null;
  }

  function cleanDestination(raw, fallbackPort) {
    var dest = { id: "", mode: "srt-listener", host: "", port: fallbackPort, url: "" };
    var slot = raw && raw.id != null ? String(raw.id) : "";
    if (SLOTS.indexOf(slot) !== -1) dest.id = slot;
    if (raw && MODES.indexOf(raw.mode) !== -1) dest.mode = raw.mode;
    if (raw && typeof raw.host === "string") dest.host = raw.host.slice(0, 253);
    var port = Number(raw && raw.port);
    if (Number.isInteger(port) && port >= 1 && port <= 65535) dest.port = port;
    if (dest.mode === "srt-listener" && dest.port === 9001) dest.port = 9101;
    if (dest.mode === "srt-listener" && dest.port === 9002) dest.port = 9102;
    if (raw && typeof raw.url === "string") dest.url = raw.url.slice(0, 1000);
    if (dest.mode === "srt-caller") {
      var caller = parseCallerUrl(dest.url);
      if (caller) {
        dest.url = caller.url;
        dest.host = caller.host;
        dest.port = caller.port;
      } else if (dest.url) {
        dest.url = "";
      }
    } else if (dest.mode === "srt-listener" || dest.mode === "sdi") {
      dest.url = "";
      if (dest.mode === "sdi") dest.host = "";
    }
    return dest;
  }

  function assignSlots(items) {
    var seen = {};
    items.forEach(function (item) {
      if (SLOTS.indexOf(item.id) === -1 || seen[item.id]) item.id = "";
      else seen[item.id] = true;
    });
    var free = SLOTS.filter(function (slot) { return !seen[slot]; });
    items.forEach(function (item) {
      if (!item.id) item.id = free.shift();
    });
  }

  function listenerPorts(skipKey) {
    var used = { 9001: true, 9002: true };
    CHANNELS.forEach(function (id) {
      (config.outputs[id] || []).forEach(function (output) {
        if (output.mode !== "srt-listener") return;
        if (skipKey && skipKey === id + ":" + output.id) return;
        used[output.port] = true;
      });
    });
    return used;
  }

  function nextListenerPort(skipKey) {
    var used = listenerPorts(skipKey);
    var port = 9200;
    while (used[port] && port < 65535) port += 1;
    return port;
  }

  function claimListenerPort(port, skipKey) {
    var used = listenerPorts(skipKey);
    if (port === 9001) port = 9101;
    if (port === 9002) port = 9102;
    if (used[port] || RESERVED_PORTS[port]) return nextListenerPort(skipKey);
    return port;
  }

  function normalize(saved) {
    var config = clone(DEFAULTS);
    if (!saved || typeof saved !== "object") return config;
    CHANNELS.forEach(function (id) {
      var input = saved.inputs && saved.inputs[id];
      if (input && Object.prototype.hasOwnProperty.call(FORMATS, input.format)) {
        config.inputs[id].format = input.format;
      }
      if (input && SOURCES.indexOf(input.source) !== -1) config.inputs[id].source = input.source;
      if (input && WAITING.indexOf(input.waiting) !== -1) config.inputs[id].waiting = input.waiting;
      if (input && typeof input.loop === "string") config.inputs[id].loop = input.loop.slice(0, 500);
      var items = asDestinations(saved.outputs && saved.outputs[id]);
      var bitrate = legalBitrate(input && input.bitrate);
      if (bitrate === null) {
        items.some(function (item) {
          bitrate = legalBitrate(item.bitrate);
          return bitrate !== null;
        });
      }
      if (bitrate !== null) config.inputs[id].bitrate = bitrate;
      if (!items.length) return;
      var cleaned = items.map(function (item) { return cleanDestination(item, DEFAULT_PORTS[id]); });
      assignSlots(cleaned);
      var seenSdi = false;
      config.outputs[id] = cleaned.filter(function (output) {
        if (output.mode !== "sdi") return true;
        if (seenSdi) return false;
        seenSdi = true;
        output.device = SDI_DEVICE[id];
        return true;
      });
    });
    var used = { 9001: true, 9002: true };
    CHANNELS.forEach(function (id) {
      config.outputs[id].forEach(function (output) {
        if (output.mode !== "srt-listener") return;
        if (used[output.port]) {
          var port = 9200;
          while (used[port] && port < 65535) port += 1;
          output.port = port;
        }
        used[output.port] = true;
      });
    });
    return config;
  }

  function loadConfig() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return clone(DEFAULTS);
      return normalize(JSON.parse(raw));
    } catch (err) {
      return clone(DEFAULTS);
    }
  }

  function saveConfig(config, id) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
    fetch("/admin/config.php", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "NexEncoder"
      },
      body: JSON.stringify(config)
    }).then(function (response) {
      if (!response.ok) {
        return response.json().then(function (body) {
          throw new Error((body && body.detail) || "The encoder did not save this.");
        });
      }
      return response.json();
    }).then(function (body) {
      if (body && body.config) {
        config = normalize(body.config);
        localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
        paint();
      }
    }).catch(function (err) {
      fieldError(id, err.message || "The encoder did not save this.");
      setSaved(id, false);
    });
  }

  function parseCallerUrl(value) {
    var text = String(value || "").trim();
    if (!text || text.length > 1000 || /\s/.test(text)) return null;
    var parsed;
    try {
      parsed = new URL(text);
    } catch (err) {
      return null;
    }
    if (parsed.protocol !== "srt:" || parsed.hash) return null;
    var port = Number(parsed.port);
    if (!parsed.hostname || !Number.isInteger(port) || port < 1 || port > 65535) return null;
    return { url: text, host: parsed.hostname, port: port };
  }

  function callerUrl(output) {
    if (output.url && /^srt:\/\//i.test(output.url)) return output.url;
    if (!output.host) return "";
    var host = output.host.indexOf(":") === -1 ? output.host : "[" + output.host + "]";
    return "srt://" + host + ":" + output.port;
  }

  function redactCaller(url) {
    return String(url).replace(/^(srt:\/\/)([^/@:\s]+)(:[^@/\s]*)?@/i, "$1$2:REDACTED@");
  }

  function summarizeOne(output) {
    if (output.mode === "srt-caller") {
      var target = callerUrl(output);
      return target ? "SRT caller · " + redactCaller(target) : "SRT caller · URL not set";
    }
    if (output.mode === "srt-listener") return "SRT listener · port " + output.port;
    if (output.mode === "hls-push") {
      return output.url ? "HLS push · " + output.url : "HLS push · URL not set";
    }
    if (output.mode === "sdi") return "SDI · " + (output.device || "DeckLink");
    return output.url ? "HLS pull · " + output.url : "HLS pull · local playlist";
  }

  function outputSummary(outputs) {
    return (outputs || []).map(summarizeOne).join(" · ");
  }

  function setThemeLabel() {
    var button = document.getElementById("theme-toggle");
    if (!button || !window.NexAppTheme) return;
    var names = { system: "System", light: "Light", dark: "Dark" };
    var pref = window.NexAppTheme.currentPref();
    var label = names[pref] || "Theme";
    button.textContent = label;
    button.title = "Theme is " + label + ". Click to change.";
  }

  var themeButton = document.getElementById("theme-toggle");
  if (themeButton) {
    themeButton.addEventListener("click", function () {
      if (window.NexAppTheme) window.NexAppTheme.cycle();
      setThemeLabel();
    });
  }
  setThemeLabel();

  var config = loadConfig();

  function paint() {
    document.querySelectorAll("[data-channel-summary]").forEach(function (node) {
      var id = node.getAttribute("data-channel-summary");
      var input = config.inputs[id];
      var output = config.outputs[id];
      if (!input || !output) return;
      var format = node.querySelector("[data-field='format']");
      var delivery = node.querySelector("[data-field='output']");
      var bitrate = node.querySelector("[data-field='bitrate']");
      if (format) format.textContent = FORMATS[input.format] || input.format;
      if (delivery) delivery.textContent = outputSummary(output);
      if (bitrate) bitrate.textContent = String(input.bitrate) + " Mbps";
    });
    document.querySelectorAll("[data-key='format']").forEach(function (select) {
      var id = select.getAttribute("data-channel");
      if (config.inputs[id]) select.value = config.inputs[id].format;
    });
    document.querySelectorAll("[data-key='source']").forEach(function (select) {
      var id = select.getAttribute("data-channel");
      if (config.inputs[id]) select.value = config.inputs[id].source;
    });
    document.querySelectorAll("[data-key='waiting']").forEach(function (select) {
      var id = select.getAttribute("data-channel");
      if (config.inputs[id]) select.value = config.inputs[id].waiting;
    });
    document.querySelectorAll("[data-key='loop']").forEach(function (input) {
      var id = input.getAttribute("data-channel");
      if (config.inputs[id]) input.value = config.inputs[id].loop;
    });
    document.querySelectorAll("[data-key='bitrate']").forEach(function (input) {
      var id = input.getAttribute("data-channel");
      if (config.inputs[id]) input.value = String(config.inputs[id].bitrate);
    });
    CHANNELS.forEach(function (id) {
      var endpoint = document.getElementById("endpoint-" + id);
      var input = config.inputs[id];
      if (endpoint && input) endpoint.textContent = ENDPOINTS[id][input.source] || ENDPOINTS[id].sdi;
      var row = document.querySelector("[data-loop-row='" + id + "']");
      if (row && input) row.hidden = input.waiting !== "loop";
      var button = document.getElementById("bars-" + id);
      if (button) button.textContent = bars[id] ? "Bars with ident: on" : "Bars with ident: off";
    });
    renderOutputs();
    if (outputHealth) paintOutputs(outputHealth);
  }

  function fieldNodes(id, name) {
    return document.querySelectorAll("[data-field-" + name + "='" + id + "']");
  }

  function fieldError(id, message) {
    fieldNodes(id, "error").forEach(function (node) {
      node.hidden = !message;
      node.textContent = message || "";
    });
  }

  function setSaved(id, on) {
    fieldNodes(id, "saved").forEach(function (node) {
      node.hidden = !on;
    });
  }

  function findOutput(channelId, slot) {
    var list = config.outputs[channelId] || [];
    for (var i = 0; i < list.length; i += 1) {
      if (list[i].id === slot) return list[i];
    }
    return null;
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function labeledField(labelText, control) {
    var block = el("div", "field-block");
    var label = el("label", "", labelText);
    label.htmlFor = control.id;
    block.appendChild(label);
    block.appendChild(control);
    return block;
  }

  function outputCard(channelId, output) {
    var card = el("div", "output-card");
    card.setAttribute("data-output-card", channelId + ":" + output.id);
    var head = el("div", "output-card-head");
    var title = el("span", "output-card-title");
    var dot = el("span", "status-dot");
    dot.setAttribute("data-output-dot", channelId + ":" + output.id);
    title.appendChild(dot);
    title.appendChild(el("strong", "", "Output " + output.id));
    head.appendChild(title);
    if (config.outputs[channelId].length > 1) {
      var remove = el("button", "btn-text", "Remove");
      remove.type = "button";
      remove.setAttribute("data-remove-output", channelId);
      remove.setAttribute("data-output-id", output.id);
      head.appendChild(remove);
    }
    card.appendChild(head);

    var mode = el("select");
    mode.id = "mode-" + channelId + "-" + output.id;
    mode.setAttribute("data-output-field", "");
    mode.setAttribute("data-channel", channelId);
    mode.setAttribute("data-output-id", output.id);
    mode.setAttribute("data-key", "mode");
    MODES.forEach(function (value) {
      var option = el("option", "", MODE_LABELS[value]);
      option.value = value;
      mode.appendChild(option);
    });
    mode.value = output.mode;
    card.appendChild(labeledField("Delivery", mode));

    if (output.mode === "srt-caller") {
      var caller = el("input");
      caller.id = "url-" + channelId + "-" + output.id;
      caller.type = "text";
      caller.autocomplete = "off";
      caller.spellcheck = false;
      caller.placeholder = "srt://srt.example.com:5000/live?mode=caller";
      caller.value = callerUrl(output);
      caller.setAttribute("data-output-field", "");
      caller.setAttribute("data-channel", channelId);
      caller.setAttribute("data-output-id", output.id);
      caller.setAttribute("data-key", "url");
      card.appendChild(labeledField("SRT URL", caller));
    }
    if (output.mode === "srt-listener") {
      var port = el("input");
      port.id = "port-" + channelId + "-" + output.id;
      port.type = "number";
      port.min = "1";
      port.max = "65535";
      port.step = "1";
      port.inputMode = "numeric";
      port.value = String(output.port);
      port.setAttribute("data-output-field", "");
      port.setAttribute("data-channel", channelId);
      port.setAttribute("data-output-id", output.id);
      port.setAttribute("data-key", "port");
      card.appendChild(labeledField("Port", port));
    }
    if (output.mode === "hls-push" || output.mode === "hls-pull") {
      var url = el("input");
      url.id = "url-" + channelId + "-" + output.id;
      url.type = "url";
      url.autocomplete = "off";
      url.spellcheck = false;
      url.placeholder = output.mode === "hls-push"
        ? "https://origin.example/live/" + channelId.toLowerCase() + "/index.m3u8"
        : "http://this-host/live/" + channelId.toLowerCase() + "/" + output.id + "/index.m3u8";
      url.value = output.url;
      url.setAttribute("data-output-field", "");
      url.setAttribute("data-channel", channelId);
      url.setAttribute("data-output-id", output.id);
      url.setAttribute("data-key", "url");
      card.appendChild(labeledField(output.mode === "hls-push" ? "Push URL" : "Playlist URL", url));
    }
    if (output.mode === "sdi") {
      card.appendChild(el(
        "p",
        "muted",
        "Playback connector " + (SDI_DEVICE[channelId] || output.device || "DeckLink") + ". 608/708 and SCTE-104 are written into the VANC."
      ));
    }
    return card;
  }

  function renderOutputs() {
    CHANNELS.forEach(function (id) {
      var list = document.querySelector("[data-output-list='" + id + "']");
      if (!list) return;
      list.textContent = "";
      (config.outputs[id] || []).forEach(function (output) {
        list.appendChild(outputCard(id, output));
      });
      var add = document.querySelector("[data-add-output='" + id + "']");
      if (add) add.hidden = (config.outputs[id] || []).length >= MAX_OUTPUTS;
    });
  }

  function nextSlot(channelId) {
    var used = {};
    (config.outputs[channelId] || []).forEach(function (output) { used[output.id] = true; });
    for (var i = 0; i < SLOTS.length; i += 1) {
      if (!used[SLOTS[i]]) return SLOTS[i];
    }
    return "";
  }

  function validUrl(value) {
    if (value === "") return true;
    try {
      var url = new URL(value);
      return url.protocol === "http:" || url.protocol === "https:";
    } catch (err) {
      return false;
    }
  }

  function persist(id) {
    try {
      dirty = true;
      saveConfig(config, id);
      fieldError(id, "");
      setSaved(id, true);
      paint();
    } catch (err) {
      fieldError(id, "This browser blocked saving the setting.");
      setSaved(id, false);
    }
  }

  function readOutputField(input) {
    var id = input.getAttribute("data-channel");
    var slot = input.getAttribute("data-output-id");
    var key = input.getAttribute("data-key");
    var output = findOutput(id, slot);
    if (!output) return;
    if (key === "mode") {
      if (MODES.indexOf(input.value) === -1) return;
      if (input.value === "sdi") {
        var taken = (config.outputs[id] || []).some(function (item) {
          return item.mode === "sdi" && item.id !== output.id;
        });
        if (taken) {
          input.value = output.mode;
          fieldError(id, "This input already has an SDI output.");
          setSaved(id, false);
          return;
        }
        output.host = "";
        output.url = "";
        output.device = SDI_DEVICE[id];
      }
      output.mode = input.value;
      if (output.mode === "srt-listener") {
        output.port = claimListenerPort(output.port, id + ":" + output.id);
        output.url = "";
      } else if (output.mode !== "srt-caller" && /^srt:\/\//i.test(output.url || "")) {
        output.url = "";
      }
      persist(id);
      return;
    }
    if (key === "port") {
      var port = Number(input.value);
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        fieldError(id, "Port stays between 1 and 65535.");
        setSaved(id, false);
        input.value = String(output.port);
        return;
      }
      if (output.mode === "srt-listener") port = claimListenerPort(port, id + ":" + output.id);
      output.port = port;
      input.value = String(port);
    } else if (key === "url" && output.mode === "srt-caller") {
      var callerText = input.value.trim();
      if (!callerText) {
        output.url = "";
        output.host = "";
        input.value = "";
      } else {
        var caller = parseCallerUrl(callerText);
        if (!caller) {
          fieldError(id, "SRT caller URL must look like srt://host:port. A path and query can follow the port.");
          setSaved(id, false);
          input.value = callerUrl(output);
          return;
        }
        output.url = caller.url;
        output.host = caller.host;
        output.port = caller.port;
        input.value = caller.url;
      }
    } else if (key === "host") {
      var host = input.value.trim();
      if (host.length > 253 || /\s/.test(host)) {
        fieldError(id, "Listener address is a hostname or IP, with no spaces.");
        setSaved(id, false);
        input.value = output.host;
        return;
      }
      output.host = host;
      input.value = host;
    } else if (key === "url") {
      var url = input.value.trim();
      if (url.length > 500 || !validUrl(url)) {
        fieldError(id, "Use an http or https URL.");
        setSaved(id, false);
        input.value = output.url;
        return;
      }
      output.url = url;
      input.value = url;
    }
    persist(id);
  }

  document.querySelectorAll("[data-key='format']").forEach(function (select) {
    select.addEventListener("change", function () {
      var id = select.getAttribute("data-channel");
      if (!Object.prototype.hasOwnProperty.call(FORMATS, select.value)) return;
      config.inputs[id].format = select.value;
      persist(id);
    });
  });

  document.querySelectorAll("[data-key='source']").forEach(function (select) {
    select.addEventListener("change", function () {
      var id = select.getAttribute("data-channel");
      if (SOURCES.indexOf(select.value) === -1) return;
      config.inputs[id].source = select.value;
      persist(id);
      pollLock();
    });
  });

  document.querySelectorAll("[data-key='waiting']").forEach(function (select) {
    select.addEventListener("change", function () {
      var id = select.getAttribute("data-channel");
      if (WAITING.indexOf(select.value) === -1) return;
      config.inputs[id].waiting = select.value;
      persist(id);
    });
  });

  document.querySelectorAll("[data-key='bitrate']").forEach(function (input) {
    input.addEventListener("change", function () {
      var id = input.getAttribute("data-channel");
      var bitrate = legalBitrate(input.value);
      if (bitrate === null) {
        fieldError(id, "Video bitrate stays between 6 and 18 Mbps.");
        setSaved(id, false);
        input.value = String(config.inputs[id].bitrate);
        return;
      }
      config.inputs[id].bitrate = bitrate;
      persist(id);
    });
  });

  document.querySelectorAll("[data-key='loop']").forEach(function (input) {
    input.addEventListener("change", function () {
      var id = input.getAttribute("data-channel");
      var loop = input.value.trim();
      if (loop.length > 500 || /[\r\n]/.test(loop)) {
        fieldError(id, "Loop file is a path on this encoder, 500 characters or fewer.");
        setSaved(id, false);
        input.value = config.inputs[id].loop;
        return;
      }
      config.inputs[id].loop = loop;
      input.value = loop;
      persist(id);
    });
  });

  CHANNELS.forEach(function (id) {
    var auto = document.getElementById("auto-" + id);
    if (!auto) return;
    auto.addEventListener("click", function () {
      auto.disabled = true;
      var note = document.getElementById("auto-note-" + id);
      if (note) note.textContent = "Looking…";
      fetch("/admin/detect.php", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "NexEncoder"
        },
        body: JSON.stringify({ channel: id })
      }).then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, body: body || {} };
        });
      }).then(function (result) {
        var body = result.body;
        if (!result.ok || body.ok === false) {
          throw new Error(body.detail || "Auto did not find a format.");
        }
        if (body.config) {
          config = normalize(body.config);
          localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
        } else if (body.format && config.inputs[id]) {
          config.inputs[id].format = body.format;
        }
        dirty = true;
        fieldError(id, "");
        setSaved(id, true);
        if (note) note.textContent = body.detail || "";
        paint();
      }).catch(function (err) {
        if (note) note.textContent = "";
        fieldError(id, err.message || "Auto did not find a format.");
        setSaved(id, false);
      }).then(function () {
        auto.disabled = false;
      });
    });
  });

  document.querySelectorAll("[data-restart]").forEach(function (restart) {
    restart.addEventListener("click", function () {
      var id = restart.getAttribute("data-restart");
      if (CHANNELS.indexOf(id) === -1) return;
      if (!window.confirm("Restart channel " + id + "? The output drops until the pipeline is back.")) return;
      var notes = document.querySelectorAll("[data-restart-note='" + id + "']");
      notes.forEach(function (status) { status.textContent = "Restarting…"; });
      fetch("/admin/service.php?unit=" + encodeURIComponent("nexencoder-channel@" + id) + "&action=restart", {
        method: "POST",
        headers: { "X-Requested-With": "NexEncoder" }
      }).then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, body: body };
        });
      }).then(function (result) {
        var text = result.ok ? "Channel " + id + " is restarting." : ((result.body && result.body.detail) || "Restart failed");
        notes.forEach(function (status) { status.textContent = text; });
        if (!result.ok) fieldError(id, (result.body && result.body.detail) || "Restart failed");
      }).catch(function () {
        notes.forEach(function (status) { status.textContent = ""; });
        fieldError(id, "The console did not answer.");
      });
    });
  });

  CHANNELS.forEach(function (id) {
    var button = document.getElementById("bars-" + id);
    if (!button) return;
    button.addEventListener("click", function () {
      var enabled = !bars[id];
      fetch("/admin/bars.php", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "NexEncoder"
        },
        body: JSON.stringify({ channel: id, enabled: enabled })
      }).then(function (response) {
        if (!response.ok) throw new Error("bars");
        return response.json();
      }).then(function (body) {
        bars[id] = !!(body && body.enabled);
        var note = document.getElementById("bars-note-" + id);
        if (note) {
          note.hidden = false;
          note.textContent = bars[id] ? "Bars with ident are on the output." : "Bars with ident are off.";
        }
        paint();
      }).catch(function () {
        fieldError(id, "Bars with ident did not change.");
      });
    });
  });

  document.querySelectorAll("[data-output-list]").forEach(function (list) {
    list.addEventListener("change", function (event) {
      var input = event.target;
      if (!input || !input.hasAttribute("data-output-field")) return;
      readOutputField(input);
    });
    list.addEventListener("click", function (event) {
      var button = event.target.closest("[data-remove-output]");
      if (!button || !list.contains(button)) return;
      var id = button.getAttribute("data-remove-output");
      var slot = button.getAttribute("data-output-id");
      var outputs = config.outputs[id] || [];
      if (outputs.length <= 1) return;
      config.outputs[id] = outputs.filter(function (output) { return output.id !== slot; });
      persist(id);
    });
  });

  document.querySelectorAll("[data-add-output]").forEach(function (button) {
    button.addEventListener("click", function () {
      var id = button.getAttribute("data-add-output");
      var outputs = config.outputs[id];
      if (!outputs || outputs.length >= MAX_OUTPUTS) return;
      var slot = nextSlot(id);
      if (!slot) return;
      outputs.push({
        id: slot,
        mode: "srt-listener",
        host: "",
        port: nextListenerPort(),
        url: ""
      });
      persist(id);
    });
  });

  var form = document.getElementById("config-form");
  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
    });
  }

  document.querySelectorAll("[data-open-dialog]").forEach(function (button) {
    button.addEventListener("click", function () {
      var dialog = document.getElementById(button.getAttribute("data-open-dialog"));
      if (dialog && typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
    });
  });
  document.querySelectorAll("dialog.channel-dialog").forEach(function (dialog) {
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });
    dialog.querySelectorAll("[data-close-dialog]").forEach(function (button) {
      button.addEventListener("click", function () { dialog.close(); });
    });
  });

  paint();
  pollLock();
  pollOutputs();
  setInterval(pollLock, 5000);
  setInterval(pollOutputs, 5000);

  function paintLock(id, device, known) {
    var badge = document.getElementById("lock-" + id);
    var row = document.querySelector("[data-channel-summary='" + id + "'] [data-field='signal']");
    var input = config.inputs[id];
    var text = "Lock unknown";
    var kind = "";
    if (input && input.source === "srt") {
      text = "SRT";
    } else if (known && device && device.input_locked) {
      text = device.input_mode && device.input_mode !== "unknown" ? ("Locked · " + device.input_mode) : "Locked";
      kind = "badge-ok";
    } else if (known && device) {
      text = "No signal";
      kind = "badge-bad";
    }
    if (badge) {
      badge.className = kind ? ("badge " + kind) : "badge";
      badge.textContent = text;
    }
    if (row) row.textContent = text;
  }

  var outputHealth = null;
  var HEALTH_WORD = { green: "Up", orange: "Check", red: "Down" };
  var HEALTH_TITLE = {
    connected: "Connected",
    loss: "Connected, with loss or retransmits",
    peer: "Peer is up, statistics are stale",
    sending: "Encode is sending. The origin receipt is not confirmed",
    playing: "SDI playback is running",
    down: "Not connected",
    dns: "Hostname does not resolve"
  };

  function dnsLine(row) {
    if (row.dns === "ok" && row.host && row.addresses && row.addresses.length) {
      return row.host + " → " + row.addresses.join(", ");
    }
    if (row.dns === "failed" && row.host) return row.host + " does not resolve";
    if (row.dns === "address" && row.addresses && row.addresses.length) return row.addresses.join(", ");
    return "";
  }

  function applyDot(dot, row) {
    dot.className = "status-dot status-dot-" + (row.state || "red");
    var word = HEALTH_WORD[row.state] || "Down";
    var reason = HEALTH_TITLE[row.detail] || word;
    dot.title = reason;
    dot.setAttribute("aria-label", word + ". " + reason);
  }

  function healthItem(row) {
    var item = document.createElement("li");
    var dot = document.createElement("span");
    applyDot(dot, row);
    var copy = document.createElement("span");
    copy.className = "output-health-copy";
    var label = document.createElement("span");
    label.textContent = summarizeOne(row);
    copy.appendChild(label);
    var resolved = dnsLine(row);
    if (resolved) {
      var line = document.createElement("span");
      line.className = "muted output-health-dns";
      line.textContent = resolved;
      copy.appendChild(line);
    }
    item.appendChild(dot);
    item.appendChild(copy);
    return item;
  }

  function paintOutputs(body) {
    outputHealth = body;
    ["A", "B"].forEach(function (id) {
      var list = document.getElementById("output-health-" + id);
      if (!list) return;
      var rows = (body && body.channels && body.channels[id]) || [];
      list.textContent = "";
      rows.forEach(function (row) {
        list.appendChild(healthItem(row));
        var dot = document.querySelector("[data-output-dot='" + id + ":" + row.id + "']");
        if (dot) applyDot(dot, row);
      });
    });
  }

  function pollOutputs() {
    if (!document.getElementById("output-health-A") && !document.getElementById("output-health-B")) return;
    fetch("/live/outputs.php").then(function (response) {
      if (!response.ok) throw new Error("outputs");
      return response.json();
    }).then(function (body) {
      if (body && body.ok) paintOutputs(body);
    }).catch(function () {});
  }

  function pollLock() {
    if (!document.getElementById("lock-A") && !document.querySelector("[data-field='signal']")) return;
    fetch("/live/decklink.php").then(function (response) {
      if (!response.ok) throw new Error("decklink");
      return response.json();
    }).then(function (body) {
      var devices = body && body.devices ? body.devices : [];
      var known = !!(body && body.ok);
      ["A", "B"].forEach(function (id) {
        var name = ENDPOINTS[id].sdi;
        var match = null;
        devices.forEach(function (device) {
          if (device && device.name === name) match = device;
        });
        paintLock(id, match, known);
      });
    }).catch(function () {
      ["A", "B"].forEach(function (id) { paintLock(id, null, false); });
    });
  }

  fetch("/admin/config.php").then(function (response) {
    if (!response.ok) throw new Error("config");
    return response.json();
  }).then(function (body) {
    if (dirty || !body || !body.config) return;
    config = normalize(body.config);
    if (body.bars) {
      bars.A = !!body.bars.A;
      bars.B = !!body.bars.B;
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
    paint();
    pollLock();
  }).catch(function () {});
})();
