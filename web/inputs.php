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
  <title>Inputs · NexEncoder</title>
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
      <a class="is-active" href="inputs.php"><span class="nav-ico">▤</span>Inputs</a>
      <a href="outputs.php"><span class="nav-ico">↗</span>Outputs</a>
      <a href="monitor.php"><span class="nav-ico">◉</span>Monitor</a>
      <a href="effects.php"><span class="nav-ico">▣</span>Effects</a>
      <a href="metrics.php"><span class="nav-ico">◇</span>Metrics</a>
      <a href="services.php"><span class="nav-ico">⚙</span>Services</a>
    </nav>
    <div class="side-foot">HD-SDI · SRT / HLS</div>
  </aside>
  <main class="shell-main">
    <div class="page-head">
      <div>
        <h1>Inputs</h1>
        <p class="lead">Two inputs. Each one is SDI or an SRT listener. The output stays up when the signal is gone.</p>
      </div>
    </div>
    <form id="config-form" class="grid-2">
      <section class="panel">
        <h2>Input A</h2>
        <p class="muted" id="endpoint-A">DeckLink Duo (1)</p>
        <p class="save-row"><span class="badge" id="lock-A">Lock unknown</span></p>
        <label for="source-A">Source</label>
        <select id="source-A" data-channel="A" data-key="source">
          <option value="sdi">SDI</option>
          <option value="srt">SRT listener</option>
        </select>
        <label for="format-A">Raster</label>
        <select id="format-A" data-channel="A" data-key="format">
          <option value="1080i59.94">1080i 59.94</option>
          <option value="1080i50">1080i 50</option>
          <option value="1080p59.94">1080p 59.94</option>
          <option value="1080p50">1080p 50</option>
          <option value="720p59.94">720p 59.94</option>
          <option value="720p50">720p 50</option>
        </select>
        <p class="save-row">
          <button type="button" class="btn-text" id="auto-A">Auto</button>
          <span class="muted" id="auto-note-A"></span>
        </p>
        <label for="waiting-A">Waiting picture</label>
        <select id="waiting-A" data-channel="A" data-key="waiting">
          <option value="slate">Waiting for Input</option>
          <option value="loop">Video loop</option>
          <option value="bars">Color bars</option>
        </select>
        <div data-loop-row="A" hidden>
          <label for="loop-A">Loop file</label>
          <input id="loop-A" data-channel="A" data-key="loop" type="text" autocomplete="off" spellcheck="false" placeholder="/var/lib/nexencoder/loops/a.mp4">
        </div>
        <p class="save-row">
          <button type="button" class="btn-text" id="bars-A">Bars with ident: off</button>
          <span class="ok" id="bars-note-A" hidden></span>
          <button type="button" class="btn-text" id="restart-A">Restart channel</button>
          <span class="muted" id="restart-note-A"></span>
          <span class="ok" id="saved-A" hidden>Saved. Restart the channel service to apply it.</span>
          <span class="error" id="error-A" role="alert" hidden></span>
        </p>
      </section>
      <section class="panel">
        <h2>Input B</h2>
        <p class="muted" id="endpoint-B">DeckLink Duo (2)</p>
        <p class="save-row"><span class="badge" id="lock-B">Lock unknown</span></p>
        <label for="source-B">Source</label>
        <select id="source-B" data-channel="B" data-key="source">
          <option value="sdi">SDI</option>
          <option value="srt">SRT listener</option>
        </select>
        <label for="format-B">Raster</label>
        <select id="format-B" data-channel="B" data-key="format">
          <option value="1080i59.94">1080i 59.94</option>
          <option value="1080i50">1080i 50</option>
          <option value="1080p59.94">1080p 59.94</option>
          <option value="1080p50">1080p 50</option>
          <option value="720p59.94">720p 59.94</option>
          <option value="720p50">720p 50</option>
        </select>
        <p class="save-row">
          <button type="button" class="btn-text" id="auto-B">Auto</button>
          <span class="muted" id="auto-note-B"></span>
        </p>
        <label for="waiting-B">Waiting picture</label>
        <select id="waiting-B" data-channel="B" data-key="waiting">
          <option value="slate">Waiting for Input</option>
          <option value="loop">Video loop</option>
          <option value="bars">Color bars</option>
        </select>
        <div data-loop-row="B" hidden>
          <label for="loop-B">Loop file</label>
          <input id="loop-B" data-channel="B" data-key="loop" type="text" autocomplete="off" spellcheck="false" placeholder="/var/lib/nexencoder/loops/b.mp4">
        </div>
        <p class="save-row">
          <button type="button" class="btn-text" id="bars-B">Bars with ident: off</button>
          <span class="ok" id="bars-note-B" hidden></span>
          <button type="button" class="btn-text" id="restart-B">Restart channel</button>
          <span class="muted" id="restart-note-B"></span>
          <span class="ok" id="saved-B" hidden>Saved. Restart the channel service to apply it.</span>
          <span class="error" id="error-B" role="alert" hidden></span>
        </p>
      </section>
    </form>
  </main>
  <footer class="shell-footer">
    <span>NexEncoder</span>
    <span class="footer-version">v0.1.0</span>
  </footer>
  <script src="assets/theme.js"></script>
  <script src="assets/console.js"></script>
</body>
</html>
