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
      <a href="effects.php"><span class="nav-ico">▣</span>Effects</a>
      <a class="is-active" href="metrics.php"><span class="nav-ico">◇</span>Metrics</a>
      <a href="services.php"><span class="nav-ico">⚙</span>Services</a>
      <a href="vendors.php"><span class="nav-ico">▤</span>Vendors</a>
    </nav>
    <div class="side-foot">HD-SDI · SRT / HLS</div>
  </aside>
  <main class="shell-main metrics-page">
    <div class="page-head">
      <div>
        <h1>Metrics</h1>
        <p class="lead">Host and encoder charts for the selected range. Auto-refresh keeps a preset window current.</p>
      </div>
    </div>
    <div class="metrics-bar">
      <span class="metrics-label">Range</span>
      <button type="button" class="secondary range is-selected" data-range="15m">15m</button>
      <button type="button" class="secondary range" data-range="1h">1h</button>
      <button type="button" class="secondary range" data-range="6h">6h</button>
      <button type="button" class="secondary range" data-range="24h">24h</button>
      <span class="metrics-rule" aria-hidden="true"></span>
      <label class="metrics-dt">From
        <input type="datetime-local" id="from-dt" aria-label="Custom range start">
      </label>
      <label class="metrics-dt">To
        <input type="datetime-local" id="to-dt" aria-label="Custom range end">
      </label>
      <button type="button" class="secondary" id="range-apply">Apply</button>
      <button type="button" class="secondary" id="range-clear">Clear</button>
      <label class="metrics-auto">
        <input type="checkbox" id="auto-refresh" checked> Auto-refresh
      </label>
      <span id="range-note" class="muted"></span>
      <span class="badge" id="prom-status">Checking Prometheus…</span>
    </div>
    <div class="banner" id="prom-banner" hidden></div>
    <div class="chart-grid" id="chart-grid"></div>
    <section class="panel">
      <h2>Input lock</h2>
      <div id="lock-timeline" class="lock-timeline"></div>
      <div class="lock-legend">
        <span><i class="lock-key locked"></i>locked</span>
        <span><i class="lock-key unlocked"></i>unlocked</span>
        <span><i class="lock-key nodata"></i>no data</span>
      </div>
      <p class="muted" id="lock-empty">No input samples in this range.</p>
    </section>
    <section class="panel">
      <h2>Inputs and outputs</h2>
      <div id="info-table"><p class="muted">No samples yet</p></div>
    </section>
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
    <p id="host-uptime" class="uptime-bar">Host uptime: <strong>—</strong></p>
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
