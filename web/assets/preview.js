(function () {
  "use strict";

  var players = {};

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

  function drop(id, pc) {
    if (players[id] !== pc) return;
    delete players[id];
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
      video.hidden = false;
      if (wait) wait.hidden = true;
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
        log.textContent = rows.slice(-12).map(function (event) {
          return event.label + " " + event.text;
        }).join("\n");
      });
    }).catch(function () {});
  }

  connect();
  sideband();
  setInterval(function () {
    connect();
    sideband();
  }, 4000);
})();
