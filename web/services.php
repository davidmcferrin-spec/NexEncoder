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
  <title>Services · NexEncoder</title>
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
      <a href="metrics.php"><span class="nav-ico">◇</span>Metrics</a>
      <a class="is-active" href="services.php"><span class="nav-ico">⚙</span>Services</a>
      <a href="vendors.php"><span class="nav-ico">▤</span>Vendors</a>
    </nav>
    <div class="side-foot">HD-SDI · SRT / HLS</div>
  </aside>
  <main class="shell-main">
    <div class="page-head">
      <div>
        <h1>Services</h1>
        <p class="lead">Start, stop, restart, enable, and disable the encoder units. The journal follows the selected unit. Clear log hides this unit's older lines. Clear All deletes the host journal for every service and does not download a zip. The audit log is kept and is included in the support zip.</p>
      </div>
    </div>
    <div class="support-bundle-bar">
      <label for="bundle-hours">Support bundle</label>
      <select id="bundle-hours" title="Journal window for every encoder service">
        <option value="24">Last 24 hours</option>
        <option value="36">Last 36 hours</option>
        <option value="48">Last 48 hours</option>
        <option value="72">Last 72 hours</option>
        <option value="all">All</option>
      </select>
      <button type="button" class="btn" id="download-bundle" title="Download journals, host details, and config as a zip">Download zip</button>
      <button type="button" class="btn btn-danger" id="clear-all" title="Delete the host journal for every service">Clear All</button>
      <span id="bundle-status" class="muted"></span>
    </div>
    <section class="panel" id="token-panel">
      <h2>API tokens</h2>
      <p class="muted">Bearer tokens for the effect and encoder API on port 8790. A new token is shown once. Revoke stops it. Delete removes the row. The secret is not written to the audit log.</p>
      <form id="token-form" class="token-form">
        <label for="token-label">Label</label>
        <input id="token-label" maxlength="40" autocomplete="off" spellcheck="false" required>
        <button type="submit" id="token-create">Create token</button>
      </form>
      <div id="token-once" hidden>
        <p class="ok" id="token-secret"></p>
        <p class="save-row">
          <button type="button" id="token-copy">Copy token</button>
          <button type="button" class="secondary" id="token-hide">Hide token</button>
        </p>
      </div>
      <p class="error" id="token-error" role="alert" hidden></p>
      <ul class="preset-list" id="token-list"></ul>
    </section>
    <div class="banner" id="svc-banner" hidden></div>
    <div class="ops-layout">
      <section class="panel">
        <h2>Units</h2>
        <div id="unit-list"></div>
      </section>
      <section class="panel">
        <h2 id="journal-title">Journal</h2>
        <div class="actions">
          <button type="button" class="btn btn-secondary" data-action="start">Start</button>
          <button type="button" class="btn btn-danger" data-action="stop">Stop</button>
          <button type="button" class="btn" data-action="restart">Restart</button>
          <button type="button" class="btn btn-secondary" data-action="enable">Enable</button>
          <button type="button" class="btn btn-secondary" data-action="disable">Disable</button>
          <button type="button" class="btn btn-secondary" id="copy-journal">Copy log</button>
          <button type="button" class="btn btn-danger" id="clear-journal">Clear log</button>
          <span id="action-status" class="muted"></span>
        </div>
        <div class="log-box" id="journal" role="log" aria-live="polite"></div>
      </section>
    </div>
  </main>
  <footer class="shell-footer">
    <span>NexEncoder</span>
    <span class="footer-version">v0.1.0</span>
  </footer>
  <script src="assets/theme.js"></script>
  <script src="assets/console.js"></script>
  <script src="assets/services.js"></script>
</body>
</html>
