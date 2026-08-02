"""A tiny local web UI for the automation.

Runs a small web server on your own machine and opens it in your browser, so
you drive everything with buttons instead of terminal commands. It shells out
to the same CLI (`python -m sams_automation ...`) in a subprocess and streams
its output to the page, so the behavior is identical to the command line — just
friendlier. Screenshots taken during a run show up live on the page.

Start it with:  python -m sams_automation serve
"""

from __future__ import annotations

import collections
import logging
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from .config import load_accounts, load_config
from .web_sms import register_sms_routes


class Job:
    """Tracks the one running subprocess (a run or a test) and its output."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lines: collections.deque[str] = collections.deque(maxlen=4000)
        self.kind: str = ""
        self.lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, kind: str, args: list[str], cwd: Path) -> bool:
        with self.lock:
            if self.running:
                return False
            self.kind = kind
            self.lines.clear()
            self.lines.append(f"$ {' '.join(args)}")
            self.proc = subprocess.Popen(
                args,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            threading.Thread(target=self._pump, daemon=True).start()
            return True

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))
        code = self.proc.wait()
        self.lines.append(f"--- finished (exit code {code}) ---")

    def stop(self) -> None:
        with self.lock:
            if self.running and self.proc:
                self.proc.terminate()


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sam's Club Automation</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background: #f5f6f8; color: #1b1b1f; }
  @media (prefers-color-scheme: dark) { body { background:#16171a; color:#e8e8ea; } }
  header { background: #0071dc; color: #fff; padding: 16px 22px; }
  header h1 { margin: 0; font-size: 19px; }
  header .sub { opacity: .85; font-size: 13px; margin-top: 3px; }
  main { max-width: 1000px; margin: 0 auto; padding: 20px; }
  .card { background: #fff; border-radius: 12px; padding: 18px; margin-bottom: 18px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  @media (prefers-color-scheme: dark){ .card{ background:#232428; box-shadow:none; } }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  button { font-size:15px; font-weight:600; border:0; border-radius:9px;
           padding:11px 16px; cursor:pointer; }
  button.primary { background:#0071dc; color:#fff; }
  button.secondary { background:#e6e8eb; color:#1b1b1f; }
  @media (prefers-color-scheme: dark){ button.secondary{ background:#3a3b40; color:#e8e8ea; } }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .stat { display:inline-block; margin-right:22px; }
  .stat b { font-size:20px; display:block; }
  .stat span { font-size:12px; opacity:.7; }
  #log { background:#0d0f12; color:#c7f0d0; font-family: ui-monospace, Menlo, monospace;
         font-size:12.5px; padding:14px; border-radius:9px; height:280px; overflow:auto;
         white-space:pre-wrap; }
  .pill { font-size:12px; padding:3px 9px; border-radius:20px; font-weight:600; }
  .pill.idle { background:#e6e8eb; color:#555; }
  .pill.run  { background:#ffe9b3; color:#7a5a00; }
  @media (prefers-color-scheme: dark){ .pill.idle{ background:#3a3b40; color:#bbb; } }
  .shots { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
           gap:10px; }
  .shots figure { margin:0; }
  .shots img { width:100%; border-radius:8px; border:1px solid rgba(0,0,0,.1); cursor:zoom-in; }
  .shots figcaption { font-size:11px; opacity:.7; margin-top:3px; word-break:break-all; }
  .note { font-size:13px; opacity:.8; line-height:1.5; }
  h2 { font-size:15px; margin:0 0 12px; }
</style>
</head>
<body>
<header>
  <h1>Sam's Club — Complimentary Membership Automation</h1>
  <div class="sub" id="cfgsub">loading…</div>
  <div class="sub"><a href="/sms" style="color:#fff">SMS providers &amp; Walmart stock &rarr;</a></div>
</header>
<main>
  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div>
        <div class="stat"><b id="nacct">–</b><span>accounts</span></div>
        <div class="stat"><b id="imapuser" style="font-size:14px">–</b><span>iCloud inbox</span></div>
      </div>
      <span class="pill idle" id="statuspill">idle</span>
    </div>
  </div>

  <div class="card" id="pwcard">
    <h2>iCloud app password</h2>
    <p class="note">Paste the <b>app-specific password</b> from appleid.apple.com
      (Sign-In and Security → App-Specific Passwords). Stored only in your local
      config file.</p>
    <div class="row">
      <input id="pw" type="password" placeholder="xxxx-xxxx-xxxx-xxxx"
             style="flex:1;min-width:220px;padding:11px;border-radius:9px;border:1px solid #ccc;font-size:15px">
      <button class="secondary" onclick="savePw()">Save password</button>
    </div>
    <p class="note" id="pwmsg"></p>
  </div>

  <div class="card">
    <h2>Actions</h2>
    <div class="row">
      <button class="secondary" onclick="post('/api/test-imap')" id="b-imap">Test email connection</button>
      <button class="primary" onclick="post('/api/run?limit=1')" id="b-run1">Run 1 account (test)</button>
      <button class="primary" onclick="if(confirm('Run all accounts?'))post('/api/run')" id="b-runall">Run all accounts</button>
      <button class="secondary" onclick="post('/api/stop')" id="b-stop">Stop</button>
    </div>
    <p class="note" id="hint">Start with <b>Test email connection</b>, then <b>Run 1 account</b>.
      When Chrome opens and shows a “press &amp; hold” box, solve it in that window — the run waits for you.</p>
  </div>

  <div class="card">
    <h2>Walmart — add a phone number</h2>
    <p class="note" id="wmstatus">checking…</p>
    <div class="row">
      <button class="primary" onclick="post('/api/walmart/run?limit=1')" id="b-wm1">Run 1 account</button>
      <button class="primary" onclick="if(confirm('Run every account?'))post('/api/walmart/run')" id="b-wmall">Run all accounts</button>
      <a href="/sms" class="note" style="margin-left:auto">SMS providers &amp; stock &rarr;</a>
    </div>
    <p class="note" id="wmhint"></p>
  </div>

  <div class="card">
    <h2>Live progress</h2>
    <div id="log"></div>
  </div>

  <div class="card" id="pagescard" style="display:none">
    <h2>Captured pages (send these to Claude)</h2>
    <p class="note">Right-click a link → <b>Download Linked File</b>, then send me the file.
      These are the real page contents I use to finish the selectors.</p>
    <div id="pages"></div>
  </div>

  <div class="card">
    <h2>Screenshots <span class="note" id="shotcount"></span></h2>
    <div class="shots" id="shots"></div>
  </div>

  <p class="note" style="text-align:center" id="build">&nbsp;</p>
</main>
<script>
async function post(url){ await fetch(url,{method:'POST'}); tick(); }
async function jget(url){ const r = await fetch(url); return r.json(); }
async function loadInfo(){
  const i = await jget('/api/info');
  document.getElementById('nacct').textContent = i.accounts;
  document.getElementById('imapuser').textContent = i.imap_user;
  document.getElementById('cfgsub').textContent =
     i.imap_user + '  •  ' + i.accounts + ' accounts  •  ' + (i.password_ready ? 'password set ✓' : '⚠ set your app password below');
  document.getElementById('pwcard').style.display = i.password_ready ? 'none' : 'block';
}
async function savePw(){
  const pw = document.getElementById('pw').value.trim();
  if(!pw){ return; }
  const r = await fetch('/api/set-password',{method:'POST',
     headers:{'Content-Type':'application/json'}, body: JSON.stringify({password:pw})});
  const j = await r.json();
  document.getElementById('pwmsg').textContent = j.ok ? 'Saved ✓' : ('Error: '+(j.error||'failed'));
  if(j.ok){ document.getElementById('pw').value=''; loadInfo(); }
}
async function loadWalmart(){
  const w = await jget('/api/walmart/info');
  document.getElementById('build').textContent =
    'build ' + (w.build || '?') +
    " — if the Walmart card is missing, stop the server (Ctrl-C) and run ./start.sh again";
  const missing = [];
  if (!w.accounts) missing.push('accounts (walmart_accounts.csv)');
  if (!w.imap_ready) missing.push('Gmail app password (walmart.imap)');
  if (!w.use_code) missing.push('login_use_code selector — will use the password instead');
  const bits = [
    `${w.accounts} account(s)`,
    w.proxies ? `${w.proxies} proxies` : 'no proxies',
    w.dry_run ? 'DRY RUN — nothing will be bought'
              : (w.purchasing_enabled ? '⚠ LIVE — real money' : 'purchasing off'),
  ];
  document.getElementById('wmstatus').innerHTML =
    bits.join(' · ') + (w.accounts_error ? ` — <span style="color:#b3261e">${w.accounts_error}</span>` : '');
  document.getElementById('wmhint').textContent = missing.length
    ? 'Still to set up: ' + missing.join('; ')
    : 'Ready. Output and screenshots appear below.';
  for (const b of ['b-wm1','b-wmall'])
    document.getElementById(b).disabled = !w.accounts;
}

let lastLen = 0;
async function tick(){
  const s = await jget('/api/status');
  const pill = document.getElementById('statuspill');
  pill.textContent = s.running ? ('running: '+s.kind) : 'idle';
  pill.className = 'pill ' + (s.running ? 'run':'idle');
  for (const b of ['b-imap','b-run1','b-runall','b-wm1','b-wmall'])
    document.getElementById(b).disabled = s.running;
  document.getElementById('b-stop').disabled = !s.running;
  const log = document.getElementById('log');
  log.textContent = s.lines.join('\\n');
  if (s.lines.length !== lastLen){ log.scrollTop = log.scrollHeight; lastLen = s.lines.length; }
  const shots = document.getElementById('shots');
  document.getElementById('shotcount').textContent = s.screenshots.length ? '('+s.screenshots.length+')' : '';
  shots.innerHTML = s.screenshots.map(n =>
     '<figure><a href="/screenshots/'+encodeURIComponent(n)+'" target="_blank">'+
     '<img src="/screenshots/'+encodeURIComponent(n)+'"></a>'+
     '<figcaption>'+n+'</figcaption></figure>').join('');
  const pages = s.pages || [];
  document.getElementById('pagescard').style.display = pages.length ? 'block' : 'none';
  document.getElementById('pages').innerHTML = pages.map(n =>
     '<div style="margin:4px 0"><a href="/screenshots/'+encodeURIComponent(n)+'" download>'+n+'</a></div>').join('');
}
loadInfo();
loadWalmart();
tick();
setInterval(loadWalmart, 5000);
setInterval(tick, 1500);
</script>
</body>
</html>
"""


class _QuietPolling(logging.Filter):
    """Drop the successful status-poll lines from the request log.

    Both pages poll a status endpoint every second or two, which floods the
    terminal you're supposed to be watching and buries anything that matters.
    Errors and every other request still get logged.
    """

    NOISY = ("GET /api/status", "GET /api/sms/status", "GET /favicon.ico")

    def filter(self, record: logging.LogRecord) -> bool:
        line = record.getMessage()
        return not (any(p in line for p in self.NOISY) and (" 200 " in line or " 404 " in line))


def create_app(config_path: str, accounts_path: str) -> Flask:
    app = Flask(__name__)
    job = Job()
    root = Path.cwd()
    register_sms_routes(app, config_path)
    logging.getLogger("werkzeug").addFilter(_QuietPolling())

    @app.get("/favicon.ico")
    def favicon() -> Response:
        # Empty 204 rather than a 404 on every page load.
        return Response(status=204)

    def _cfg():
        return load_config(config_path)

    @app.get("/")
    def index() -> Response:
        return Response(INDEX_HTML, mimetype="text/html")

    @app.get("/api/info")
    def info():
        try:
            cfg = _cfg()
            accts = load_accounts(accounts_path)
            pw = cfg.imap.password
            return jsonify(
                imap_user=cfg.imap.username,
                accounts=len(accts),
                password_ready=bool(pw) and "PUT-APP" not in pw and "xxxx" not in pw,
            )
        except Exception as e:
            return jsonify(imap_user="(config error)", accounts=0,
                           password_ready=False, error=str(e))

    @app.get("/api/status")
    def status():
        cfg_shot_dir = "screenshots"
        try:
            cfg_shot_dir = _cfg().browser.screenshot_dir
        except Exception:
            pass
        shot_dir = root / cfg_shot_dir
        shots: list[str] = []
        pages: list[str] = []
        if shot_dir.is_dir():
            pngs = sorted(shot_dir.glob("*.png"), key=lambda p: p.stat().st_mtime,
                          reverse=True)
            shots = [p.name for p in pngs[:60]]
            htmls = sorted(shot_dir.glob("*.html"), key=lambda p: p.stat().st_mtime,
                           reverse=True)
            pages = [p.name for p in htmls[:30]]
        return jsonify(running=job.running, kind=job.kind,
                       lines=list(job.lines), screenshots=shots, pages=pages)

    @app.get("/screenshots/<path:name>")
    def screenshot(name: str):
        try:
            shot_dir = root / _cfg().browser.screenshot_dir
        except Exception:
            shot_dir = root / "screenshots"
        return send_from_directory(str(shot_dir), name)

    def _py_cli(*extra: str) -> list[str]:
        return [sys.executable, "-m", "sams_automation", *extra,
                "--config", config_path, "--accounts", accounts_path]

    @app.post("/api/test-imap")
    def api_test_imap():
        ok = job.start("test-imap", _py_cli("test-imap"), root)
        return jsonify(started=ok)

    @app.post("/api/run")
    def api_run():
        args = ["run"]
        limit = request.args.get("limit")
        if limit:
            args += ["--limit", str(int(limit))]
        ok = job.start("run", _py_cli(*args), root)
        return jsonify(started=ok)

    @app.post("/api/walmart/run")
    def api_walmart_run():
        """Run the add-phone flow, streaming into the same log panel."""
        args = ["walmart-add-phone"]
        limit = request.args.get("limit")
        if limit:
            args += ["--limit", str(int(limit))]
        if request.args.get("no_resume") == "1":
            args.append("--no-resume")
        ok = job.start("walmart-add-phone", _py_cli(*args), root)
        return jsonify(started=ok)

    @app.get("/api/walmart/info")
    def api_walmart_info():
        """How much is set up, so the page can say what's missing."""
        import yaml

        from .config import load_walmart_accounts

        try:
            accounts = load_walmart_accounts("walmart_accounts.csv")
            n_accounts, accounts_error = len(accounts), ""
        except Exception as e:
            n_accounts, accounts_error = 0, str(e)

        raw = {}
        try:
            raw = yaml.safe_load(Path(config_path).read_text()) or {}
        except Exception:
            pass
        walmart = raw.get("walmart") or {}
        purchasing = raw.get("purchasing") or {}
        imap_user = ((walmart.get("imap") or {}).get("username") or "")
        proxy_file = (walmart.get("proxies") or {}).get("file") or "walmart_proxies.txt"
        try:
            n_proxies = sum(
                1 for line in Path(proxy_file).read_text().splitlines()
                if line.strip() and not line.strip().startswith("#")
            )
        except OSError:
            n_proxies = 0

        from .web_sms import build_id

        return jsonify(
            build=build_id(),
            accounts=n_accounts,
            accounts_error=accounts_error,
            imap_user=imap_user,
            imap_ready=bool(imap_user) and "you@gmail.com" not in imap_user,
            proxies=n_proxies,
            dry_run=bool(purchasing.get("dry_run", True)),
            purchasing_enabled=bool(purchasing.get("enabled", False)),
            use_code=bool((walmart.get("selectors") or {}).get("login_use_code")),
        )

    @app.post("/api/stop")
    def api_stop():
        job.stop()
        return jsonify(stopped=True)

    @app.post("/api/set-password")
    def api_set_password():
        import re

        data = request.get_json(silent=True) or {}
        pw = (data.get("password") or "").strip()
        if not pw or '"' in pw:
            return jsonify(ok=False, error="empty or contains a quote")
        try:
            path = Path(config_path)
            text = path.read_text()
            # imap.password is the only 'password:' line in the file (imap is the
            # first section; 'login_password' is a different key).
            new, n = re.subn(r'(?m)^(\s*password:\s*).*$',
                             lambda m: m.group(1) + '"' + pw + '"', text, count=1)
            if n == 0:
                return jsonify(ok=False, error="password line not found")
            path.write_text(new)
            return jsonify(ok=True)
        except Exception as e:
            return jsonify(ok=False, error=str(e))

    return app


def _free_port(preferred: int) -> int:
    """Return `preferred` if free, otherwise the next open port (or any free one)."""
    import socket

    for candidate in [preferred, *range(preferred + 1, preferred + 20)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(config_path: str, accounts_path: str, port: int = 8765,
          open_browser: bool = True) -> None:
    app = create_app(config_path, accounts_path)
    port = _free_port(port)  # skip past a leftover instance instead of crashing
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  Sam's Club automation:      {url}")
    print(f"  SMS providers / Walmart:   {url}sms")
    print("\n  Leave this window open. Close it (Ctrl-C) to stop.\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False)
