(function () {
  "use strict";

  var players = {};
  var audio = {};
  var pendingStreams = {};
  var audioFailed = {};
  var sharedCtx = null;
  var shown = {};
  var hold = {};
  var meterBuf = null;

  function waitForIce(pc) {
    if (pc.iceGatheringState === "complete") return Promise.resolve();
    return new Promise(function (resolve) {
      var timer = setTimeout(finish, 800);
      function finish() {
        clearTimeout(timer);
        pc.removeEventListener("icegatheringstatechange", onChange);
        resolve();
      }
      function onChange() {
        if (pc.iceGatheringState === "complete") finish();
      }
      pc.addEventListener("icegatheringstatechange", onChange);
    });
  }

  function prefMuted(id) {
    try {
      var raw = localStorage.getItem("nexencoder-audio-muted-" + id);
      if (raw === null || raw === "") return true;
      return raw === "1";
    } catch (err) {
      return true;
    }
  }

  function setPrefMuted(id, muted) {
    try { localStorage.setItem("nexencoder-audio-muted-" + id, muted ? "1" : "0"); } catch (err) { /* private mode */ }
  }

  function prefVolume(id) {
    try {
      var raw = localStorage.getItem("nexencoder-audio-volume-" + id);
      if (raw === null || raw === "") return 0.2;
      var value = Number(raw);
      if (!isFinite(value)) return 0.2;
      return Math.max(0, Math.min(1, value));
    } catch (err) {
      return 0.2;
    }
  }

  function setPrefVolume(id, value) {
    var next = Math.max(0, Math.min(1, value));
    if (!isFinite(next)) next = 0.2;
    try { localStorage.setItem("nexencoder-audio-volume-" + id, String(next)); } catch (err) { /* private mode */ }
    return next;
  }

  function paintListen(id) {
    var button = document.getElementById("speaker-" + id);
    var slider = document.getElementById("volume-" + id);
    var muted = prefMuted(id);
    var volume = prefVolume(id);
    if (slider && document.activeElement !== slider) slider.value = String(Math.round(volume * 100));
    if (!button) return;
    button.classList.toggle("is-muted", muted);
    button.classList.toggle("is-live", !muted);
    button.setAttribute("aria-pressed", muted ? "false" : "true");
    button.setAttribute("aria-label", (muted ? "Unmute" : "Mute") + " Input " + id);
    button.title = muted ? "Unmute" : "Mute";
  }

  function applyGain(id) {
    var node = audio[id];
    if (!node) return;
    node.master.gain.value = prefMuted(id) ? 0 : prefVolume(id);
  }

  function metersRunning() {
    return !!(sharedCtx && sharedCtx.state === "running");
  }

  function paintMeterGate() {
    var button = document.getElementById("enable-meters");
    if (!button) return;
    var Factory = window.AudioContext || window.webkitAudioContext;
    button.hidden = !Factory || metersRunning();
  }

  function audioContext() {
    if (sharedCtx) return sharedCtx;
    var Factory = window.AudioContext || window.webkitAudioContext;
    if (!Factory) return null;
    sharedCtx = new Factory();
    sharedCtx.onstatechange = function () { paintMeterGate(); };
    return sharedCtx;
  }

  function resetMeters(id) {
    ["L", "R"].forEach(function (channel) {
      var fill = document.getElementById("vu-fill-" + id + "-" + channel);
      var peak = document.getElementById("vu-peak-" + id + "-" + channel);
      if (fill) fill.style.width = "0%";
      if (peak) peak.style.left = "0%";
      delete shown[id + channel];
      delete hold[id + channel];
    });
  }

  function releaseAudio(id) {
    var node = audio[id];
    if (!node) {
      resetMeters(id);
      return;
    }
    ["source", "splitter", "merger", "master"].forEach(function (key) {
      try { node[key].disconnect(); } catch (err) { /* already disconnected */ }
    });
    node.analysers.forEach(function (analyser) {
      try { analyser.disconnect(); } catch (err) { /* already disconnected */ }
    });
    delete audio[id];
    resetMeters(id);
  }

  function buildGraph(id, stream) {
    var ctx = sharedCtx;
    if (!ctx || !stream) return;
    var tracks = stream.getAudioTracks();
    if (!tracks.length) return;
    var prev = audio[id];
    if (prev && prev.streamId === stream.id) return;
    if (audioFailed[id] === stream.id) return;
    releaseAudio(id);
    try {
      var source = ctx.createMediaStreamSource(new MediaStream(tracks));
      try {
        source.channelCount = 2;
        source.channelCountMode = "explicit";
        source.channelInterpretation = "discrete";
      } catch (err) { /* the stream source may ignore the count */ }
      var splitter = ctx.createChannelSplitter(2);
      try {
        splitter.channelCount = 2;
        splitter.channelCountMode = "explicit";
        splitter.channelInterpretation = "discrete";
      } catch (err) { /* optional */ }
      var merger = ctx.createChannelMerger(2);
      var master = ctx.createGain();
      source.connect(splitter);
      var analysers = [0, 1].map(function (index) {
        var analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        analyser.smoothingTimeConstant = 0.45;
        try {
          analyser.channelCount = 1;
          analyser.channelCountMode = "explicit";
        } catch (err) { /* optional */ }
        splitter.connect(analyser, index);
        analyser.connect(merger, 0, index);
        return analyser;
      });
      merger.connect(master);
      master.connect(ctx.destination);
      audio[id] = {
        source: source,
        splitter: splitter,
        merger: merger,
        master: master,
        analysers: analysers,
        streamId: stream.id
      };
      applyGain(id);
      delete audioFailed[id];
    } catch (err) {
      audioFailed[id] = stream.id;
      releaseAudio(id);
    }
  }

  function resumeAudio() {
    var ctx = audioContext();
    if (!ctx) return Promise.resolve();
    var ready = ctx.state === "suspended" ? ctx.resume() : Promise.resolve();
    return ready.then(function () {
      ["A", "B"].forEach(function (id) {
        if (pendingStreams[id]) buildGraph(id, pendingStreams[id]);
      });
      paintMeterGate();
    }).catch(function () {
      paintMeterGate();
    });
  }

  function setMuted(id, muted) {
    if (!muted && prefVolume(id) === 0) setPrefVolume(id, 0.2);
    setPrefMuted(id, muted);
    applyGain(id);
    paintListen(id);
  }

  function setVolume(id, linear) {
    setPrefVolume(id, linear);
    applyGain(id);
    paintListen(id);
  }

  function dbFromPeak(peak) {
    if (peak <= 0.00001) return -60;
    return Math.max(-60, Math.min(0, 20 * Math.log10(peak)));
  }

  function tickMeters() {
    ["A", "B"].forEach(function (id) {
      var node = audio[id];
      ["L", "R"].forEach(function (channel, index) {
        var fill = document.getElementById("vu-fill-" + id + "-" + channel);
        var peakEl = document.getElementById("vu-peak-" + id + "-" + channel);
        var level = -60;
        if (node && node.analysers[index] && sharedCtx && sharedCtx.state === "running") {
          var analyser = node.analysers[index];
          if (!meterBuf || meterBuf.length !== analyser.fftSize) meterBuf = new Float32Array(analyser.fftSize);
          analyser.getFloatTimeDomainData(meterBuf);
          var peak = 0;
          for (var i = 0; i < meterBuf.length; i++) {
            var sample = Math.abs(meterBuf[i]);
            if (sample > peak) peak = sample;
          }
          level = dbFromPeak(peak);
        }
        var key = id + channel;
        var current = shown[key] == null ? -60 : shown[key];
        current = level > current ? level : Math.max(level, current - 0.8);
        shown[key] = current;
        if (fill) fill.style.width = ((current + 60) / 60 * 100) + "%";
        var now = performance.now();
        var held = hold[key];
        if (!held || level >= held.db || now > held.until) {
          hold[key] = { db: level, until: now + 1200 };
          held = hold[key];
        }
        if (peakEl) peakEl.style.left = ((held.db + 60) / 60 * 100) + "%";
      });
    });
    requestAnimationFrame(tickMeters);
  }

  function drop(id, pc) {
    if (players[id] !== pc) return;
    delete players[id];
    delete pendingStreams[id];
    delete audioFailed[id];
    releaseAudio(id);
    try { pc.close(); } catch (err) { /* already closed */ }
    var video = document.getElementById("video-" + id);
    var wait = document.getElementById("wait-" + id);
    if (video) {
      video.srcObject = null;
      video.hidden = true;
    }
    if (wait) {
      wait.hidden = false;
      wait.textContent = "Waiting for the proxy";
    }
  }

  function play(id, url) {
    var video = document.getElementById("video-" + id);
    var wait = document.getElementById("wait-" + id);
    if (!video || !url || players[id]) return;
    var pc = new RTCPeerConnection();
    players[id] = pc;
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });
    pc.ontrack = function (event) {
      var stream = event.streams[0];
      if (stream && video.srcObject !== stream) video.srcObject = stream;
      video.muted = true;
      video.hidden = false;
      if (wait) wait.hidden = true;
      if (stream) {
        pendingStreams[id] = stream;
        if (sharedCtx) buildGraph(id, stream);
      }
      event.track.onended = function () { drop(id, pc); };
    };
    pc.onconnectionstatechange = function () {
      var state = pc.connectionState;
      if (state === "failed" || state === "disconnected" || state === "closed") drop(id, pc);
    };
    pc.createOffer().then(function (offer) {
      return pc.setLocalDescription(offer);
    }).then(function () {
      return waitForIce(pc);
    }).then(function () {
      return fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/sdp" },
        body: pc.localDescription.sdp
      });
    }).then(function (response) {
      if (!response.ok) throw new Error("preview " + response.status);
      return response.text();
    }).then(function (sdp) {
      return pc.setRemoteDescription({ type: "answer", sdp: sdp });
    }).catch(function () {
      drop(id, pc);
    });
  }

  function connect() {
    fetch("/admin/preview.php").then(function (response) {
      if (!response.ok) throw new Error("preview");
      return response.json();
    }).then(function (body) {
      var channels = body.channels || {};
      ["A", "B"].forEach(function (id) {
        if (channels[id] && channels[id].whep) play(id, channels[id].whep);
      });
    }).catch(function () {});
  }

  function sideband() {
    fetch("/live/sideband.php").then(function (response) {
      if (!response.ok) throw new Error("sideband");
      return response.json();
    }).then(function (body) {
      var events = body.events || [];
      var latest = { A: null, B: null };
      var splice = { A: "Idle", B: "Idle" };
      events.forEach(function (event) {
        if (event.kind === "caption") latest[event.channel] = event;
        if (event.kind === "scte104" || event.kind === "scte35") splice[event.channel] = event.text || "Splice";
      });
      ["A", "B"].forEach(function (id) {
        var caption = document.getElementById("caption-" + id);
        var badge = document.getElementById("scte-" + id);
        var log = document.getElementById("sideband-" + id);
        var rows = events.filter(function (event) { return event.channel === id; });
        if (caption) {
          caption.textContent = latest[id]
            ? (latest[id].label + " · " + latest[id].text)
            : "CC1 · waiting";
        }
        if (badge) badge.textContent = splice[id];
        if (!log) return;
        if (!rows.length) {
          log.textContent = "Waiting for caption and splice events from the live path.";
          return;
        }
        log.textContent = rows.slice(-4).map(function (event) {
          return event.label + " " + event.text;
        }).join("\n");
      });
    }).catch(function () {});
  }

  ["A", "B"].forEach(function (id) {
    var video = document.getElementById("video-" + id);
    if (video) {
      video.muted = true;
      video.addEventListener("volumechange", function () {
        if (!video.muted) video.muted = true;
      });
    }
    paintListen(id);
    var speaker = document.getElementById("speaker-" + id);
    var slider = document.getElementById("volume-" + id);
    if (speaker) {
      speaker.addEventListener("click", function () {
        resumeAudio().then(function () {
          setMuted(id, !prefMuted(id));
        });
      });
    }
    if (slider) {
      slider.addEventListener("input", function () {
        resumeAudio();
        setVolume(id, Number(slider.value) / 100);
      });
    }
  });

  var enableMeters = document.getElementById("enable-meters");
  if (enableMeters) {
    enableMeters.addEventListener("click", function () { resumeAudio(); });
  }
  document.addEventListener("pointerdown", function () { resumeAudio(); });
  requestAnimationFrame(tickMeters);
  resumeAudio();
  connect();
  sideband();
  setInterval(function () {
    connect();
    sideband();
  }, 4000);
})();
