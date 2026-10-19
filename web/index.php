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
  <title>Dashboard · NexEncoder</title>
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
      <a class="is-active" href="index.php"><span class="nav-ico">▦</span>Dashboard</a>
      <a href="effects.php"><span class="nav-ico">▣</span>Effects</a>
      <a href="metrics.php"><span class="nav-ico">◇</span>Metrics</a>
      <a href="services.php"><span class="nav-ico">⚙</span>Services</a>
      <a href="vendors.php"><span class="nav-ico">▤</span>Vendors</a>
    </nav>
    <div class="side-foot">HD-SDI · SRT / HLS</div>
  </aside>
  <main class="shell-main">
    <div class="page-head">
      <div>
        <h1>Dashboard</h1>
        <p class="lead">Each input's program picture, including squeeze, graphics, and bars. Input and Output open that channel's settings. The picture stays quiet until you unmute it; the meters start with the page when the browser allows.</p>
      </div>
      <div class="page-head-actions">
        <button type="button" class="secondary" id="enable-meters" hidden>Enable meters</button>
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
        <div class="channel-actions">
          <button type="button" class="btn-text" id="bars-A">Bars with ident: off</button>
          <span class="ok" id="bars-note-A" hidden></span>
          <button type="button" class="secondary" data-open-dialog="input-dialog-A">Input</button>
          <button type="button" class="secondary" data-open-dialog="output-dialog-A">Output</button>
        </div>
        <p class="channel-status">
          <span class="badge" id="lock-A">Lock unknown</span>
          <span class="channel-status-scte"><span>SCTE-35</span><span class="badge" id="scte-A">Idle</span></span>
        </p>
        <ul class="output-health" id="output-health-A" aria-label="Input A outputs"></ul>
        <div class="log-box sideband-log" id="sideband-A" role="log" aria-label="Input A caption and splice log">Waiting for caption and splice events from the live path.</div>
        <dialog class="channel-dialog" id="input-dialog-A" aria-labelledby="input-dialog-title-A">
          <form method="dialog">
            <div class="channel-dialog-head">
              <h2 id="input-dialog-title-A">Input A</h2>
              <button type="button" class="btn-text" data-close-dialog>Close</button>
            </div>
            <p class="muted" id="endpoint-A">DeckLink Duo (1)</p>
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
              <option value="1080p60">1080p 60</option>
              <option value="1080p50">1080p 50</option>
              <option value="720p59.94">720p 59.94</option>
              <option value="720p50">720p 50</option>
            </select>
            <label for="bitrate-A">Video bitrate (Mbps)</label>
            <input id="bitrate-A" data-channel="A" data-key="bitrate" type="number" min="6" max="18" step="1" inputmode="numeric">
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
              <button type="button" class="btn-text" id="restart-A" data-restart="A">Restart channel</button>
              <span class="muted" id="restart-note-A" data-restart-note="A"></span>
              <span class="ok" data-field-saved="A" hidden>Saved. Restart the channel service to apply it.</span>
              <span class="error" data-field-error="A" role="alert" hidden></span>
            </p>
          </form>
        </dialog>
        <dialog class="channel-dialog" id="output-dialog-A" aria-labelledby="output-dialog-title-A">
          <form method="dialog">
            <div class="channel-dialog-head">
              <h2 id="output-dialog-title-A">Output A</h2>
              <button type="button" class="btn-text" data-close-dialog>Close</button>
            </div>
            <p class="muted">One encode for this input. Each output sends that same stream. Four is the limit.</p>
            <div id="outputs-A" data-output-list="A"></div>
            <p class="save-row">
              <button type="button" class="secondary" data-add-output="A">Add output</button>
            </p>
            <p class="save-row">
              <button type="button" class="btn-text" data-restart="A">Restart channel</button>
              <span class="muted" data-restart-note="A"></span>
              <span class="ok" data-field-saved="A" hidden>Saved. Restart the channel service to apply it.</span>
              <span class="error" data-field-error="A" role="alert" hidden></span>
            </p>
          </form>
        </dialog>
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
        <div class="channel-actions">
          <button type="button" class="btn-text" id="bars-B">Bars with ident: off</button>
          <span class="ok" id="bars-note-B" hidden></span>
          <button type="button" class="secondary" data-open-dialog="input-dialog-B">Input</button>
          <button type="button" class="secondary" data-open-dialog="output-dialog-B">Output</button>
        </div>
        <p class="channel-status">
          <span class="badge" id="lock-B">Lock unknown</span>
          <span class="channel-status-scte"><span>SCTE-35</span><span class="badge" id="scte-B">Idle</span></span>
        </p>
        <ul class="output-health" id="output-health-B" aria-label="Input B outputs"></ul>
        <div class="log-box sideband-log" id="sideband-B" role="log" aria-label="Input B caption and splice log">Waiting for caption and splice events from the live path.</div>
        <dialog class="channel-dialog" id="input-dialog-B" aria-labelledby="input-dialog-title-B">
          <form method="dialog">
            <div class="channel-dialog-head">
              <h2 id="input-dialog-title-B">Input B</h2>
              <button type="button" class="btn-text" data-close-dialog>Close</button>
            </div>
            <p class="muted" id="endpoint-B">DeckLink Duo (2)</p>
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
              <option value="1080p60">1080p 60</option>
              <option value="1080p50">1080p 50</option>
              <option value="720p59.94">720p 59.94</option>
              <option value="720p50">720p 50</option>
            </select>
            <label for="bitrate-B">Video bitrate (Mbps)</label>
            <input id="bitrate-B" data-channel="B" data-key="bitrate" type="number" min="6" max="18" step="1" inputmode="numeric">
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
              <button type="button" class="btn-text" id="restart-B" data-restart="B">Restart channel</button>
              <span class="muted" id="restart-note-B" data-restart-note="B"></span>
              <span class="ok" data-field-saved="B" hidden>Saved. Restart the channel service to apply it.</span>
              <span class="error" data-field-error="B" role="alert" hidden></span>
            </p>
          </form>
        </dialog>
        <dialog class="channel-dialog" id="output-dialog-B" aria-labelledby="output-dialog-title-B">
          <form method="dialog">
            <div class="channel-dialog-head">
              <h2 id="output-dialog-title-B">Output B</h2>
              <button type="button" class="btn-text" data-close-dialog>Close</button>
            </div>
            <p class="muted">One encode for this input. Each output sends that same stream. Four is the limit.</p>
            <div id="outputs-B" data-output-list="B"></div>
            <p class="save-row">
              <button type="button" class="secondary" data-add-output="B">Add output</button>
            </p>
            <p class="save-row">
              <button type="button" class="btn-text" data-restart="B">Restart channel</button>
              <span class="muted" data-restart-note="B"></span>
              <span class="ok" data-field-saved="B" hidden>Saved. Restart the channel service to apply it.</span>
              <span class="error" data-field-error="B" role="alert" hidden></span>
            </p>
          </form>
        </dialog>
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
