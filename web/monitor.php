<?php
declare(strict_types=1);
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');
?>
<!DOCTYPE html>
<html lang="en" data-theme-pref="system">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Monitor · NexEncoder</title>
  <script>
    (function(){try{var p=localStorage.getItem('nexapp-theme')||'system';
      var d=p==='dark'||(p==='system'&&matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light';
      document.documentElement.setAttribute('data-theme',d);
      document.documentElement.setAttribute('data-theme-pref',p);
    }catch(e){}})();
  </script>
  <link rel="stylesheet" href="assets/app.css">
  <link rel="stylesheet" href="assets/encoder.css">
</head>
<body class="page-shell">
  <header class="shell-top">
    <a class="shell-brand" href="index.php">NexEncoder</a>
    <div class="shell-top-actions">
      <button type="button" class="btn-text" id="theme-toggle" title="Theme">Theme</button>
    </div>
  </header>
  <aside class="shell-sidebar">
    <div class="side-profile">
      <span class="avatar avatar-fallback">E</span>
      <div class="side-profile-text">
        <strong>Local console</strong>
        <span>Operator</span>
      </div>
    </div>
    <nav class="side-nav" aria-label="Primary">
      <a href="index.php"><span class="nav-ico">▦</span>Dashboard</a>
      <a href="inputs.php"><span class="nav-ico">▤</span>Inputs</a>
      <a href="outputs.php"><span class="nav-ico">↗</span>Outputs</a>
      <a class="is-active" href="monitor.php"><span class="nav-ico">◉</span>Monitor</a>
      <a href="effects.php"><span class="nav-ico">▣</span>Effects</a>
      <a href="metrics.php"><span class="nav-ico">◇</span>Metrics</a>
      <a href="services.php"><span class="nav-ico">⚙</span>Services</a>
    </nav>
    <div class="side-foot">HD-SDI · SRT / HLS</div>
  </aside>
  <main class="shell-main">
    <div class="page-head">
      <div>
        <h1>Monitor</h1>
        <p class="lead">The program output, including squeeze, graphics, and bars. Under each picture: mute, volume, and level meters for this browser. The player uses the MediaMTX WebRTC endpoint on port 8889.</p>
      </div>
    </div>
    <div class="preview-grid">
      <section class="panel preview-card">
        <h2>Input A</h2>
        <div class="preview-frame" aria-label="Input A preview">
          <video id="video-A" autoplay playsinline muted hidden></video>
          <span class="preview-wait" id="wait-A">Waiting for the proxy</span>
          <div class="preview-caption" id="caption-A">CC1 · waiting</div>
        </div>
        <div class="preview-listen">
          <button type="button" class="preview-speaker is-muted" id="speaker-A" aria-pressed="false" aria-label="Unmute Input A" title="Unmute">
            <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
              <path d="M4 9.5v5h3.2L12 19V5L7.2 9.5H4z" fill="currentColor"/>
              <path class="speaker-wave" d="M15.5 9.2a4.2 4.2 0 010 5.6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
              <path class="speaker-wave" d="M17.8 7a7.2 7.2 0 010 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
              <path class="speaker-slash" d="M5 5l14 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>
            </svg>
          </button>
          <input type="range" id="volume-A" min="0" max="100" step="1" value="20" aria-label="Input A volume">
        </div>
        <div class="preview-vu" id="vu-A" role="group" aria-label="Input A audio levels">
          <div class="vu-row">
            <span class="vu-name">L</span>
            <div class="vu-track"><span class="vu-fill" id="vu-fill-A-L"></span><span class="vu-peak" id="vu-peak-A-L"></span></div>
          </div>
          <div class="vu-row">
            <span class="vu-name">R</span>
            <div class="vu-track"><span class="vu-fill" id="vu-fill-A-R"></span><span class="vu-peak" id="vu-peak-A-R"></span></div>
          </div>
          <div class="vu-scale" aria-hidden="true"><span>-60</span><span>-20</span><span>-12</span><span>0</span></div>
        </div>
        <div class="preview-meta">
          <span>SCTE-35</span>
          <span class="badge" id="scte-A">Idle</span>
        </div>
        <div class="log-box sideband-log" id="sideband-A" role="log" aria-label="Input A caption and splice log">Waiting for caption and splice events from the live path.</div>
      </section>
      <section class="panel preview-card">
        <h2>Input B</h2>
        <div class="preview-frame" aria-label="Input B preview">
          <video id="video-B" autoplay playsinline muted hidden></video>
          <span class="preview-wait" id="wait-B">Waiting for the proxy</span>
          <div class="preview-caption" id="caption-B">CC1 · waiting</div>
        </div>
        <div class="preview-listen">
          <button type="button" class="preview-speaker is-muted" id="speaker-B" aria-pressed="false" aria-label="Unmute Input B" title="Unmute">
            <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
              <path d="M4 9.5v5h3.2L12 19V5L7.2 9.5H4z" fill="currentColor"/>
              <path class="speaker-wave" d="M15.5 9.2a4.2 4.2 0 010 5.6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
              <path class="speaker-wave" d="M17.8 7a7.2 7.2 0 010 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
              <path class="speaker-slash" d="M5 5l14 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>
            </svg>
          </button>
          <input type="range" id="volume-B" min="0" max="100" step="1" value="20" aria-label="Input B volume">
        </div>
        <div class="preview-vu" id="vu-B" role="group" aria-label="Input B audio levels">
          <div class="vu-row">
            <span class="vu-name">L</span>
            <div class="vu-track"><span class="vu-fill" id="vu-fill-B-L"></span><span class="vu-peak" id="vu-peak-B-L"></span></div>
          </div>
          <div class="vu-row">
            <span class="vu-name">R</span>
            <div class="vu-track"><span class="vu-fill" id="vu-fill-B-R"></span><span class="vu-peak" id="vu-peak-B-R"></span></div>
          </div>
          <div class="vu-scale" aria-hidden="true"><span>-60</span><span>-20</span><span>-12</span><span>0</span></div>
        </div>
        <div class="preview-meta">
          <span>SCTE-35</span>
          <span class="badge" id="scte-B">Idle</span>
        </div>
        <div class="log-box sideband-log" id="sideband-B" role="log" aria-label="Input B caption and splice log">Waiting for caption and splice events from the live path.</div>
      </section>
    </div>
  </main>
  <footer class="shell-footer">
    <span>NexEncoder</span>
    <span class="footer-version">v0.1.0</span>
  </footer>
  <script src="assets/theme.js"></script>
  <script src="assets/console.js"></script>
  <script src="assets/preview.js"></script>
</body>
</html>
