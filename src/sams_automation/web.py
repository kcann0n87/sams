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
import csv
import io
import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from .config import (
    RESET_COLUMNS,
    TEMPLATE_COLUMNS,
    Account,
    ResetAccount,
    load_accounts,
    load_config,
    load_reset_accounts,
)

# Cap uploads so a stray huge file can't exhaust memory. An account list is tiny.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB


def _preview_rows(accounts: list[Account]) -> list[dict[str, str]]:
    """Account list -> rows safe to show in the browser (passwords masked)."""
    rows: list[dict[str, str]] = []
    for a in accounts:
        rows.append(
            {
                "primary_email": a.primary_email,
                "primary_password": "••••••" if a.primary_password else "—",
                "secondary_first": a.secondary_first,
                "secondary_last": a.secondary_last,
                "secondary_email": a.secondary_email,
                "phone": a.phone or "—",
                "secondary_password": "••••••" if a.secondary_password else "—",
            }
        )
    return rows


def _reset_preview_rows(resets: list[ResetAccount]) -> list[dict[str, str]]:
    """Reset list -> rows safe to show (new passwords masked)."""
    return [
        {"email": r.email, "new_password": "••••••" if r.new_password else "—"}
        for r in resets
    ]


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
  .drop { border:2px dashed #b7bcc4; border-radius:11px; padding:26px 16px;
          text-align:center; cursor:pointer; transition:.15s; }
  .drop:hover { border-color:#0071dc; background:rgba(0,113,220,.04); }
  .drop.over { border-color:#0071dc; background:rgba(0,113,220,.10); }
  .drop b { color:#0071dc; }
  @media (prefers-color-scheme: dark){ .drop{ border-color:#4a4d55; } }
  .tablewrap { overflow-x:auto; margin-top:12px; }
  table.preview { border-collapse:collapse; width:100%; font-size:12.5px; }
  table.preview th, table.preview td { text-align:left; padding:6px 9px;
          border-bottom:1px solid rgba(0,0,0,.09); white-space:nowrap; }
  @media (prefers-color-scheme: dark){ table.preview th, table.preview td{ border-color:rgba(255,255,255,.10);} }
  table.preview th { font-size:11px; text-transform:uppercase; letter-spacing:.03em; opacity:.65; }
  .msg.err { color:#c0392b; } .msg.ok { color:#1a8a3a; } .msg.warn { color:#9a6a00; }
  .badge { font-size:11px; font-weight:700; padding:2px 8px; border-radius:20px; }
  .badge.ok { background:#d6f5df; color:#177a38; }
  .badge.bad { background:#fbdcda; color:#b3261e; }
  .badge.wait { background:#e6e8eb; color:#555; }
  @media (prefers-color-scheme: dark){
    .badge.ok{ background:#1d4a2c; color:#8fe6a8; }
    .badge.bad{ background:#5a211d; color:#f3a8a1; }
    .badge.wait{ background:#3a3b40; color:#bbb; } }
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

  <div class="card">
    <h2>Account list (CSV)</h2>
    <p class="note">One row per membership: the <b>main account</b> login plus the
      <b>new member</b> to add. Each row is run end to end — the main account adds
      the member, then the new member activates and sets their own password.
      <a href="/api/template.csv" download>Download a blank template ↓</a></p>
    <div class="drop" id="drop" onclick="document.getElementById('file').click()">
      <input id="file" type="file" accept=".csv,text/csv" style="display:none"
             onchange="if(this.files[0])uploadFile(this.files[0])">
      <div><b>Choose a CSV</b> or drag &amp; drop it here</div>
      <div class="note" style="margin-top:6px">Passwords stay on this machine.</div>
    </div>
    <p class="msg" id="upmsg"></p>
    <div class="tablewrap" id="previewwrap" style="display:none">
      <table class="preview" id="preview"></table>
    </div>
  </div>

  <div class="card">
    <h2>Password resets (CSV)</h2>
    <p class="note">A separate list to <b>reset passwords</b> on family accounts.
      Two columns — <b>email</b> and the <b>new_password</b> you want it set to.
      For each one it runs Sam's forgot-password flow (emails a code, reads it,
      sets the new password).
      <a href="/api/resets-template.csv" download>Download a blank template ↓</a></p>
    <div class="drop" id="dropr" onclick="document.getElementById('filer').click()">
      <input id="filer" type="file" accept=".csv,text/csv" style="display:none"
             onchange="if(this.files[0])uploadResets(this.files[0])">
      <div><b>Choose a CSV</b> or drag &amp; drop it here</div>
      <div class="note" style="margin-top:6px">Passwords stay on this machine.</div>
    </div>
    <p class="msg" id="upmsgr"></p>
    <div class="tablewrap" id="resetpreviewwrap" style="display:none">
      <table class="preview" id="resetpreview"></table>
    </div>
    <div class="row" style="margin-top:12px">
      <button class="primary" onclick="post('/api/reset-run?limit=1')" id="b-reset1">Reset 1 account (test)</button>
      <button class="primary" onclick="if(confirm('Reset ALL passwords in the list?'))post('/api/reset-run')" id="b-resetall">Reset all passwords</button>
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

  <div class="card" id="pagescard" style="display:none">
    <h2>Captured pages (send these to Claude)</h2>
    <p class="note">Right-click a link → <b>Download Linked File</b>, then send me the file.
      These are the real page contents I use to finish the selectors.</p>
    <div id="pages"></div>
  </div>

  <div class="card" id="resultscard" style="display:none">
    <h2>Results <span class="note" id="resultsnote"></span></h2>
    <div class="tablewrap"><table class="preview" id="results"></table></div>
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
function escapeHtml(s){ return String(s).replace(/[&<>"]/g, c =>
   ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function renderPreview(data){
  const wrap = document.getElementById('previewwrap');
  const t = document.getElementById('preview');
  if(!data.rows || !data.rows.length){ wrap.style.display='none'; t.innerHTML=''; return; }
  const cols = data.columns || Object.keys(data.rows[0]);
  let html = '<thead><tr>' + cols.map(c => '<th>'+escapeHtml(c)+'</th>').join('') + '</tr></thead><tbody>';
  html += data.rows.map(r => '<tr>' + cols.map(c => '<td>'+escapeHtml(r[c] ?? '')+'</td>').join('') + '</tr>').join('');
  html += '</tbody>';
  t.innerHTML = html;
  wrap.style.display = 'block';
}
async function loadAccounts(){
  const d = await jget('/api/accounts');
  if(d.ok) renderPreview(d);
}
async function uploadFile(file){
  const msg = document.getElementById('upmsg');
  msg.className = 'msg'; msg.textContent = 'Checking '+file.name+' …';
  const fd = new FormData(); fd.append('file', file);
  let j;
  try { const r = await fetch('/api/upload-csv',{method:'POST',body:fd}); j = await r.json(); }
  catch(e){ msg.className='msg err'; msg.textContent='Upload failed: '+e; return; }
  if(!j.ok){ msg.className='msg err'; msg.textContent='✗ '+j.error; return; }
  msg.className = j.warning ? 'msg warn' : 'msg ok';
  msg.textContent = '✓ Loaded '+j.count+' account(s).' + (j.warning ? '  ⚠ '+j.warning : '');
  renderPreview(j);
  loadInfo();
}
const drop = document.getElementById('drop');
['dragenter','dragover'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.add('over'); }));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => {
  const f = e.dataTransfer.files[0]; if(f) uploadFile(f); });

function renderResetPreview(data){
  const wrap = document.getElementById('resetpreviewwrap');
  const t = document.getElementById('resetpreview');
  if(!data.rows || !data.rows.length){ wrap.style.display='none'; t.innerHTML=''; return; }
  const cols = data.columns || Object.keys(data.rows[0]);
  let html = '<thead><tr>' + cols.map(c => '<th>'+escapeHtml(c)+'</th>').join('') + '</tr></thead><tbody>';
  html += data.rows.map(r => '<tr>' + cols.map(c => '<td>'+escapeHtml(r[c] ?? '')+'</td>').join('') + '</tr>').join('');
  html += '</tbody>';
  t.innerHTML = html; wrap.style.display = 'block';
}
async function loadResets(){
  const d = await jget('/api/resets');
  if(d.ok) renderResetPreview(d);
}
async function uploadResets(file){
  const msg = document.getElementById('upmsgr');
  msg.className = 'msg'; msg.textContent = 'Checking '+file.name+' …';
  const fd = new FormData(); fd.append('file', file);
  let j;
  try { const r = await fetch('/api/upload-resets',{method:'POST',body:fd}); j = await r.json(); }
  catch(e){ msg.className='msg err'; msg.textContent='Upload failed: '+e; return; }
  if(!j.ok){ msg.className='msg err'; msg.textContent='✗ '+j.error; return; }
  msg.className='msg ok'; msg.textContent='✓ Loaded '+j.count+' account(s) to reset.';
  renderResetPreview(j);
}
const dropr = document.getElementById('dropr');
['dragenter','dragover'].forEach(ev => dropr.addEventListener(ev, e => {
  e.preventDefault(); dropr.classList.add('over'); }));
['dragleave','drop'].forEach(ev => dropr.addEventListener(ev, e => {
  e.preventDefault(); dropr.classList.remove('over'); }));
dropr.addEventListener('drop', e => {
  const f = e.dataTransfer.files[0]; if(f) uploadResets(f); });

let lastLen = 0;
async function tick(){
  const s = await jget('/api/status');
  const pill = document.getElementById('statuspill');
  pill.textContent = s.running ? ('running: '+s.kind) : 'idle';
  pill.className = 'pill ' + (s.running ? 'run':'idle');
  for (const b of ['b-imap','b-run1','b-runall','b-reset1','b-resetall']) document.getElementById(b).disabled = s.running;
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
  renderResults();
}
const STATUS_BADGE = { ok:'ok', no_code:'bad', flow_error:'bad', error:'bad' };
async function renderResults(){
  const d = await jget('/api/results');
  const card = document.getElementById('resultscard');
  const rows = (d && d.rows) || [];
  if(!rows.length){ card.style.display='none'; return; }
  card.style.display = 'block';
  const okN = rows.filter(r => r.status === 'ok').length;
  document.getElementById('resultsnote').textContent = '('+okN+'/'+rows.length+' done)';
  let html = '<thead><tr><th>main account</th><th>new member</th><th>status</th><th>detail</th><th>when</th></tr></thead><tbody>';
  html += rows.map(r => {
    const cls = STATUS_BADGE[r.status] || 'wait';
    const label = r.status === 'ok' ? 'done' : (r.status || '');
    return '<tr><td>'+escapeHtml(r.primary_email||'')+'</td><td>'+escapeHtml(r.secondary_email||'')+
      '</td><td><span class="badge '+cls+'">'+escapeHtml(label)+'</span></td><td>'+escapeHtml(r.detail||'')+
      '</td><td>'+escapeHtml((r.timestamp||'').replace('T',' ').replace('+00:00',' UTC'))+'</td></tr>';
  }).join('');
  html += '</tbody>';
  document.getElementById('results').innerHTML = html;
}
loadInfo();
loadAccounts();
loadResets();
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

    @app.post("/api/reset-run")
    def api_reset_run():
        args = ["reset", "--resets", "resets.csv"]
        limit = request.args.get("limit")
        if limit:
            args += ["--limit", str(int(limit))]
        ok = job.start("reset", _py_cli(*args), root)
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

    # -- account list: upload / preview / template -------------------------

    @app.get("/api/accounts")
    def api_accounts():
        """Current account list as a masked preview (empty if none yet)."""
        try:
            accts = load_accounts(accounts_path)
            return jsonify(
                ok=True, count=len(accts), columns=TEMPLATE_COLUMNS,
                rows=_preview_rows(accts),
            )
        except FileNotFoundError:
            return jsonify(ok=True, count=0, columns=TEMPLATE_COLUMNS, rows=[])
        except Exception as e:
            return jsonify(ok=False, error=str(e), rows=[])

    @app.get("/api/template.csv")
    def api_template():
        """Download a blank CSV with the right header (+ one example row)."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(TEMPLATE_COLUMNS)
        w.writerow(
            [
                "member1@example.com", "PrimaryPassw0rd!", "Jane", "Doe",
                "jane.doe@yourdomain.com", "3125550101", "NewMemberPass1!",
            ]
        )
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=accounts-template.csv"
            },
        )

    @app.post("/api/upload-csv")
    def api_upload_csv():
        """Validate an uploaded CSV and, if valid, make it the active account list.

        Accepts a multipart file field named 'file'. Validates by running it
        through the real loader; on success the previous list is backed up to
        accounts.csv.bak and the new one takes its place.
        """
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify(ok=False, error="No file was uploaded.")
        raw = f.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            return jsonify(ok=False, error="File is too large (max 2 MB).")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return jsonify(
                ok=False,
                error="File isn't UTF-8 text. Export it as a plain CSV.",
            )

        # Validate against the real loader before touching anything on disk.
        tmp = Path(tempfile.gettempdir()) / f"sams_upload_{id(raw)}.csv"
        try:
            tmp.write_text(text, encoding="utf-8")
            accts = load_accounts(tmp)
        except ValueError as e:
            # The loader names the temp path; show the uploaded file's name instead.
            msg = str(e).replace(str(tmp), f"'{f.filename}'")
            return jsonify(ok=False, error=msg)
        except Exception as e:
            return jsonify(ok=False, error=f"{type(e).__name__}: {e}")
        finally:
            tmp.unlink(missing_ok=True)

        if not accts:
            return jsonify(ok=False, error="The CSV has a header but no rows.")

        dest = Path(accounts_path)
        try:
            if dest.exists():
                dest.replace(dest.with_suffix(dest.suffix + ".bak"))
            dest.write_text(text, encoding="utf-8")
        except Exception as e:
            return jsonify(ok=False, error=f"Couldn't save the list: {e}")

        # Flag rows with no secondary_password — activation (phase 2) needs it.
        no_pw = sum(1 for a in accts if not a.secondary_password)
        warn = ""
        if no_pw:
            warn = (
                f"{no_pw} row(s) have no secondary_password. Phase 2 activation "
                "sets the new member's password, so add one per row unless your "
                "activation page doesn't ask for a password."
            )
        return jsonify(
            ok=True, count=len(accts), columns=TEMPLATE_COLUMNS,
            rows=_preview_rows(accts), warning=warn,
        )

    @app.get("/api/results")
    def api_results():
        """Per-account outcomes from results.csv (most recent run wins)."""
        path = root / "results.csv"
        if not path.exists():
            return jsonify(ok=True, rows=[])
        latest: dict[str, dict[str, str]] = {}
        try:
            with path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    key = row.get("secondary_email") or row.get("primary_email") or ""
                    latest[key] = row  # later rows overwrite earlier -> newest state
        except Exception as e:
            return jsonify(ok=False, error=str(e), rows=[])
        return jsonify(ok=True, rows=list(latest.values()))

    # -- password-reset list: upload / preview / template ------------------

    @app.get("/api/resets")
    def api_resets():
        try:
            rs = load_reset_accounts("resets.csv")
            return jsonify(ok=True, count=len(rs), columns=RESET_COLUMNS,
                           rows=_reset_preview_rows(rs))
        except FileNotFoundError:
            return jsonify(ok=True, count=0, columns=RESET_COLUMNS, rows=[])
        except Exception as e:
            return jsonify(ok=False, error=str(e), rows=[])

    @app.get("/api/resets-template.csv")
    def api_resets_template():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(RESET_COLUMNS)
        w.writerow(["member1@example.com", "NewPassw0rd1!"])
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=resets-template.csv"
            },
        )

    @app.post("/api/upload-resets")
    def api_upload_resets():
        """Validate an uploaded reset list and make it the active resets.csv."""
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify(ok=False, error="No file was uploaded.")
        raw = f.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            return jsonify(ok=False, error="File is too large (max 2 MB).")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return jsonify(
                ok=False, error="File isn't UTF-8 text. Export it as a plain CSV."
            )
        tmp = Path(tempfile.gettempdir()) / f"sams_resets_{id(raw)}.csv"
        try:
            tmp.write_text(text, encoding="utf-8")
            rs = load_reset_accounts(tmp)
        except ValueError as e:
            msg = str(e).replace(str(tmp), f"'{f.filename}'")
            return jsonify(ok=False, error=msg)
        except Exception as e:
            return jsonify(ok=False, error=f"{type(e).__name__}: {e}")
        finally:
            tmp.unlink(missing_ok=True)
        if not rs:
            return jsonify(ok=False, error="The CSV has a header but no rows.")
        dest = Path("resets.csv")
        try:
            if dest.exists():
                dest.replace(dest.with_suffix(dest.suffix + ".bak"))
            dest.write_text(text, encoding="utf-8")
        except Exception as e:
            return jsonify(ok=False, error=f"Couldn't save the list: {e}")
        return jsonify(ok=True, count=len(rs), columns=RESET_COLUMNS,
                       rows=_reset_preview_rows(rs))

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
    print(f"\n  Sam's Club automation is running at:  {url}")
    print("  Leave this window open. Close it (Ctrl-C) to stop.\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False)
