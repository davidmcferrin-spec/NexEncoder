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
  <title>Outputs · NexEncoder</title>
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
      <a class="is-active" href="outputs.php"><span class="nav-ico">↗</span>Outputs</a>
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
        <h1>Outputs</h1>
        <p class="lead">Each input delivers H.264 at 6–18 Mbps and AAC stereo, as SRT or HLS.</p>
      </div>
    </div>
    <form id="config-form" class="stack">
      <section class="panel">
        <h2>Input A</h2>
        <div class="grid-2">
          <div>
            <label for="mode-A">Delivery</label>
            <select id="mode-A" data-output-field data-channel="A" data-key="mode">
              <option value="srt-caller">SRT caller</option>
              <option value="srt-listener">SRT listener</option>
              <option value="hls-push">HLS push</option>
              <option value="hls-pull">HLS pull</option>
            </select>
          </div>
          <div>
            <label for="bitrate-A">Video bitrate (Mbps)</label>
            <input id="bitrate-A" data-output-field data-channel="A" data-key="bitrate" type="number" min="6" max="18" step="1" inputmode="numeric">
          </div>
        </div>
        <div class="field-block" data-channel-fields="A" data-modes="srt-caller" hidden>
          <label for="host-A">Listener address</label>
          <input id="host-A" data-output-field data-channel="A" data-key="host" type="text" autocomplete="off" spellcheck="false" placeholder="srt.example.com">
        </div>
        <div class="field-block" data-channel-fields="A" data-modes="srt-caller srt-listener">
          <label for="port-A">Port</label>
          <input id="port-A" data-output-field data-channel="A" data-key="port" type="number" min="1" max="65535" step="1" inputmode="numeric">
        </div>
        <div class="field-block" data-channel-fields="A" data-modes="hls-push hls-pull" hidden>
          <label for="url-A" data-url-label="A">URL</label>
          <input id="url-A" data-output-field data-channel="A" data-key="url" type="url" autocomplete="off" spellcheck="false" placeholder="https://origin.example/live/a/index.m3u8">
        </div>
        <p class="save-row">
          <span class="ok" id="saved-A" hidden>Saved. Restart the channel service to apply it.</span>
          <span class="error" id="error-A" role="alert" hidden></span>
        </p>
      </section>
      <section class="panel">
        <h2>Input B</h2>
        <div class="grid-2">
          <div>
            <label for="mode-B">Delivery</label>
            <select id="mode-B" data-output-field data-channel="B" data-key="mode">
              <option value="srt-caller">SRT caller</option>
              <option value="srt-listener">SRT listener</option>
              <option value="hls-push">HLS push</option>
              <option value="hls-pull">HLS pull</option>
            </select>
          </div>
          <div>
            <label for="bitrate-B">Video bitrate (Mbps)</label>
            <input id="bitrate-B" data-output-field data-channel="B" data-key="bitrate" type="number" min="6" max="18" step="1" inputmode="numeric">
          </div>
        </div>
        <div class="field-block" data-channel-fields="B" data-modes="srt-caller" hidden>
          <label for="host-B">Listener address</label>
          <input id="host-B" data-output-field data-channel="B" data-key="host" type="text" autocomplete="off" spellcheck="false" placeholder="srt.example.com">
        </div>
        <div class="field-block" data-channel-fields="B" data-modes="srt-caller srt-listener">
          <label for="port-B">Port</label>
          <input id="port-B" data-output-field data-channel="B" data-key="port" type="number" min="1" max="65535" step="1" inputmode="numeric">
        </div>
        <div class="field-block" data-channel-fields="B" data-modes="hls-push hls-pull" hidden>
          <label for="url-B" data-url-label="B">URL</label>
          <input id="url-B" data-output-field data-channel="B" data-key="url" type="url" autocomplete="off" spellcheck="false" placeholder="https://origin.example/live/b/index.m3u8">
        </div>
        <p class="save-row">
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
