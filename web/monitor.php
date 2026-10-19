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
        <p class="lead">The program output, including squeeze, graphics, and bars. The player uses the MediaMTX WebRTC endpoint on port 8889.</p>
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
