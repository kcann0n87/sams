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
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from .config import load_accounts, load_config


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
    <h2>Live progress</h2>
    <div id="log"></div>
  </div>

  <div class="card">
    <h2>Screenshots <span class="note" id="shotcount"></span></h2>
    <div class="shots" id="shots"></div>
  </div>
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
let lastLen = 0;
async function tick(){
  const s = await jget('/api/status');
  const pill = document.getElementById('statuspill');
  pill.textContent = s.running ? ('running: '+s.kind) : 'idle';
  pill.className = 'pill ' + (s.running ? 'run':'idle');
  for (const b of ['b-imap','b-run1','b-runall']) document.getElementById(b).disabled = s.running;
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
}
loadInfo();
tick();
setInterval(tick, 1500);
</script>
</body>
</html>
"""


def create_app(config_path: str, accounts_path: str) -> Flask:
    app = Flask(__name__)
    job = Job()
    root = Path.cwd()

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
        if shot_dir.is_dir():
            files = sorted(shot_dir.glob("*.png"), key=lambda p: p.stat().st_mtime,
                           reverse=True)
            shots = [p.name for p in files[:60]]
        return jsonify(running=job.running, kind=job.kind,
                       lines=list(job.lines), screenshots=shots)

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


def serve(config_path: str, accounts_path: str, port: int = 8765,
          open_browser: bool = True) -> None:
    app = create_app(config_path, accounts_path)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  Sam's Club automation is running at:  {url}")
    print("  Leave this window open. Close it (Ctrl-C) to stop.\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False)
