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
  <title>Vendors · NexEncoder</title>
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
      <a href="services.php"><span class="nav-ico">⚙</span>Services</a>
      <a class="is-active" href="vendors.php"><span class="nav-ico">▤</span>Vendors</a>
    </nav>
    <div class="side-foot">HD-SDI · SRT / HLS</div>
  </aside>
  <main class="shell-main">
    <div class="page-head">
      <div>
        <h1>Vendors</h1>
        <p class="lead">A vendor container runs beside the channel on the host network. It can read the program feed and send SCTE-35 or captions. It is not in the encode path. Stopping it leaves the channel on air. The contract is <code>docs/VENDOR.md</code>.</p>
      </div>
    </div>
    <div class="banner" id="vendor-banner" hidden></div>
    <section class="panel" id="caption-panel">
      <h2>Caption source</h2>
      <p class="muted">VANC keeps SDI captions. Vendor accepts datagrams on the caption port. Starting a sidecar selects Vendor for its channel. Stopping the last sidecar for that channel returns to VANC.</p>
      <div class="actions" id="caption-sources"></div>
    </section>
    <section class="panel">
      <h2>Add a sidecar</h2>
      <form id="vendor-form" class="token-form">
        <label for="vendor-name">Name</label>
        <input id="vendor-name" maxlength="31" autocomplete="off" spellcheck="false" required pattern="[a-z][a-z0-9-]{0,30}">
        <label for="vendor-image">Image</label>
        <input id="vendor-image" maxlength="201" autocomplete="off" spellcheck="false" required>
        <label for="vendor-channel">Channel</label>
        <select id="vendor-channel">
          <option value="A">A</option>
          <option value="B">B</option>
        </select>
        <label for="vendor-args">Arguments</label>
        <input id="vendor-args" maxlength="500" autocomplete="off" spellcheck="false" placeholder="optional, passed to the image">
        <button type="submit" id="vendor-save">Save</button>
        <button type="button" class="secondary" id="vendor-pull">Pull image</button>
      </form>
      <p class="error" id="vendor-error" role="alert" hidden></p>
      <p class="ok" id="vendor-note" hidden></p>
    </section>
    <section class="panel">
      <h2>Sidecars</h2>
      <div id="vendor-list"></div>
    </section>
  </main>
  <footer class="shell-footer">
    <span>NexEncoder</span>
    <span class="footer-version">v0.1.0</span>
  </footer>
  <script src="assets/theme.js"></script>
  <script src="assets/console.js"></script>
  <script src="assets/vendors.js"></script>
</body>
</html>
