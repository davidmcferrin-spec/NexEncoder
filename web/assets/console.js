(function () {
  "use strict";

  var STORAGE_KEY = "nexencoder-config";
  var CHANNELS = ["A", "B"];
  var FORMATS = {
    "1080i59.94": "1080i 59.94",
    "1080i50": "1080i 50",
    "720p59.94": "720p 59.94",
    "720p50": "720p 50"
  };
  var MODES = ["srt-caller", "srt-listener", "hls-push", "hls-pull"];
  var DEFAULTS = {
    inputs: {
      A: { format: "1080i59.94", device: "DeckLink Duo 2 (1)" },
      B: { format: "1080i59.94", device: "DeckLink Duo 2 (2)" }
    },
    outputs: {
      A: { mode: "srt-listener", bitrate: 8, host: "", port: 9000, url: "" },
      B: { mode: "srt-listener", bitrate: 8, host: "", port: 9001, url: "" }
    }
  };
  var dirty = false;

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function normalize(saved) {
    var config = clone(DEFAULTS);
    if (!saved || typeof saved !== "object") return config;
    CHANNELS.forEach(function (id) {
      var input = saved.inputs && saved.inputs[id];
      if (input && Object.prototype.hasOwnProperty.call(FORMATS, input.format)) {
        config.inputs[id].format = input.format;
      }
      if (input && typeof input.device === "string" && input.device.trim() && input.device.length <= 128) {
        config.inputs[id].device = input.device.trim();
      }
      var output = saved.outputs && saved.outputs[id];
      if (!output) return;
      if (MODES.indexOf(output.mode) !== -1) config.outputs[id].mode = output.mode;
      var bitrate = Number(output.bitrate);
      if (Number.isInteger(bitrate) && bitrate >= 6 && bitrate <= 18) {
        config.outputs[id].bitrate = bitrate;
      }
      if (typeof output.host === "string") config.outputs[id].host = output.host.slice(0, 253);
      var port = Number(output.port);
      if (Number.isInteger(port) && port >= 1 && port <= 65535) {
        config.outputs[id].port = port;
      }
      if (typeof output.url === "string") config.outputs[id].url = output.url.slice(0, 500);
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

  function saveConfig(config) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
    fetch("/api/config", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "NexEncoder"
      },
      body: JSON.stringify(config)
    }).then(function (response) {
      if (!response.ok) throw new Error("save");
      return response.json();
    }).then(function (body) {
      if (body && body.config) localStorage.setItem(STORAGE_KEY, JSON.stringify(body.config));
    }).catch(function () {
      var node = document.querySelector(".save-row .error:not([hidden])");
      if (!node) return;
    });
  }

  function outputSummary(output) {
    if (output.mode === "srt-caller") {
      var target = output.host ? output.host + ":" + output.port : "address not set";
      return "SRT caller · " + target;
    }
    if (output.mode === "srt-listener") return "SRT listener · port " + output.port;
    if (output.mode === "hls-push") {
      return output.url ? "HLS push · " + output.url : "HLS push · URL not set";
    }
    return output.url ? "HLS pull · " + output.url : "HLS pull · local playlist";
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
      if (bitrate) bitrate.textContent = String(output.bitrate) + " Mbps";
    });
    document.querySelectorAll("[data-key='format']").forEach(function (select) {
      var id = select.getAttribute("data-channel");
      if (config.inputs[id]) select.value = config.inputs[id].format;
    });
    document.querySelectorAll("[data-key='device']").forEach(function (input) {
      var id = input.getAttribute("data-channel");
      if (config.inputs[id]) input.value = config.inputs[id].device;
    });
    document.querySelectorAll("[data-output-field]").forEach(function (input) {
      var id = input.getAttribute("data-channel");
      var key = input.getAttribute("data-key");
      if (config.outputs[id]) input.value = config.outputs[id][key];
    });
    CHANNELS.forEach(applyMode);
  }

  function fieldError(id, message) {
    var node = document.getElementById("error-" + id);
    if (!node) return;
    node.hidden = !message;
    node.textContent = message || "";
  }

  function setSaved(id, on) {
    var node = document.getElementById("saved-" + id);
    if (node) node.hidden = !on;
  }

  function applyMode(id) {
    var mode = config.outputs[id].mode;
    document.querySelectorAll("[data-channel-fields='" + id + "']").forEach(function (block) {
      var modes = (block.getAttribute("data-modes") || "").split(/\s+/);
      block.hidden = modes.indexOf(mode) === -1;
    });
    var urlLabel = document.querySelector("[data-url-label='" + id + "']");
    if (urlLabel) urlLabel.textContent = mode === "hls-push" ? "Push URL" : "Playlist URL";
    var urlInput = document.getElementById("url-" + id);
    if (urlInput) {
      urlInput.placeholder = mode === "hls-push"
        ? "https://origin.example/live/" + id.toLowerCase() + "/index.m3u8"
        : "http://this-host/live/" + id.toLowerCase() + "/index.m3u8";
    }
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
      saveConfig(config);
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
    var key = input.getAttribute("data-key");
    var output = config.outputs[id];
    if (key === "mode") {
      if (MODES.indexOf(input.value) === -1) return;
      output.mode = input.value;
      persist(id);
      return;
    }
    if (key === "bitrate") {
      var bitrate = Number(input.value);
      if (!Number.isInteger(bitrate) || bitrate < 6 || bitrate > 18) {
        fieldError(id, "Video bitrate stays between 6 and 18 Mbps.");
        setSaved(id, false);
        input.value = String(output.bitrate);
        return;
      }
      output.bitrate = bitrate;
    } else if (key === "port") {
      var port = Number(input.value);
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        fieldError(id, "Port stays between 1 and 65535.");
        setSaved(id, false);
        input.value = String(output.port);
        return;
      }
      output.port = port;
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

  document.querySelectorAll("[data-key='device']").forEach(function (input) {
    input.addEventListener("change", function () {
      var id = input.getAttribute("data-channel");
      var device = input.value.trim();
      if (!device || device.length > 128 || /[\r\n]/.test(device)) {
        fieldError(id, "DeckLink input needs the device name ffmpeg lists.");
        setSaved(id, false);
        input.value = config.inputs[id].device;
        return;
      }
      config.inputs[id].device = device;
      input.value = device;
      persist(id);
    });
  });

  document.querySelectorAll("[data-output-field]").forEach(function (input) {
    input.addEventListener("change", function () {
      readOutputField(input);
    });
  });

  var form = document.getElementById("config-form");
  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
    });
  }

  paint();

  fetch("/api/config").then(function (response) {
    if (!response.ok) throw new Error("config");
    return response.json();
  }).then(function (body) {
    if (dirty || !body || !body.config) return;
    config = normalize(body.config);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
    paint();
  }).catch(function () {});

  var deviceList = document.getElementById("devices");
  if (deviceList) {
    fetch("/api/devices").then(function (response) {
      if (!response.ok) throw new Error("devices");
      return response.json();
    }).then(function (body) {
      (body.devices || []).forEach(function (device) {
        var option = document.createElement("option");
        option.value = device.name;
        deviceList.appendChild(option);
      });
    }).catch(function () {});
  }
})();
