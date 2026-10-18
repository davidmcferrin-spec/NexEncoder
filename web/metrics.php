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
  <title>Metrics · NexEncoder</title>
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
      <a href="monitor.php"><span class="nav-ico">◉</span>Monitor</a>
      <a href="effects.php"><span class="nav-ico">▣</span>Effects</a>
      <a class="is-active" href="metrics.php"><span class="nav-ico">◇</span>Metrics</a>
      <a href="services.php"><span class="nav-ico">⚙</span>Services</a>
    </nav>
    <div class="side-foot">HD-SDI · SRT / HLS</div>
  </aside>
  <main class="shell-main">
    <div class="page-head">
      <div>
        <h1>Metrics</h1>
        <p class="lead">CPU, memory, disk, GPU, uptime, and every encoder series. Charts refresh every two seconds.</p>
      </div>
      <div class="page-head-actions">
        <span class="badge" id="prom-status">Checking Prometheus…</span>
      </div>
    </div>
    <div class="banner" id="prom-banner" hidden></div>
    <h2>Host</h2>
    <div class="chart-grid" id="host-charts"></div>
    <h2>Inputs and outputs</h2>
    <div class="chart-grid" id="encoder-charts"></div>
    <section class="panel">
      <div class="page-head">
        <h2>Prometheus series</h2>
        <span class="muted" id="series-count"></span>
      </div>
      <div class="search-bar">
        <input id="series-filter" type="search" placeholder="Filter series" aria-label="Filter Prometheus series">
      </div>
      <div class="series-list" id="series-list"></div>
      <canvas id="explorer-chart" aria-label="Selected series chart"></canvas>
      <div id="explorer-info"></div>
      <p class="muted" id="explorer-meta">Select a series to chart it.</p>
    </section>
  </main>
  <footer class="shell-footer">
    <span>NexEncoder</span>
    <span class="footer-version">v0.1.0</span>
  </footer>
  <script src="assets/theme.js"></script>
  <script src="assets/console.js"></script>
  <script src="assets/metrics.js"></script>
</body>
</html>
