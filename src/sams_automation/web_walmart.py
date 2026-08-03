"""The Walmart page of the local web UI.

Its own page rather than a card on the Sam's Club one: the two flows share
nothing but a repo, and burying the run buttons under someone else's heading
made them impossible to find.

Runs go through the same job runner as everything else, so output streams to
this page's log and screenshots appear below it — which is what makes the
selector-tuning loop workable without a terminal.

Mounted by web.py at /walmart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from .web_sms import build_id


def walmart_status(config_path: str) -> dict[str, Any]:
    """What's configured, so the page can say what's missing before you press."""
    import yaml

    from .config import load_walmart_accounts

    try:
        accounts = load_walmart_accounts("walmart_accounts.csv")
        n_accounts, accounts_error = len(accounts), ""
    except Exception as e:
        n_accounts, accounts_error = 0, str(e)

    raw: dict[str, Any] = {}
    try:
        raw = yaml.safe_load(Path(config_path).read_text()) or {}
    except Exception:
        pass

    walmart = raw.get("walmart") or {}
    purchasing = raw.get("purchasing") or {}
    selectors = walmart.get("selectors") or {}
    imap_user = (walmart.get("imap") or {}).get("username") or ""

    proxy_file = (walmart.get("proxies") or {}).get("file") or "walmart_proxies.txt"
    try:
        n_proxies = sum(
            1
            for line in Path(proxy_file).read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    except OSError:
        n_proxies = 0

    # A placeholder is worse than a blank: it looks configured and isn't.
    imap_ready = bool(imap_user) and "you@gmail.com" not in imap_user

    todo = []
    if not n_accounts:
        todo.append("Add accounts to walmart_accounts.csv (email:password)")
    if not imap_ready:
        todo.append("Set walmart.imap username + Gmail app password in config.yaml")
    if not selectors.get("login_use_code"):
        todo.append(
            "Set walmart.selectors.login_use_code — until then it types the "
            "password instead of using the emailed code"
        )

    return {
        "build": build_id(),
        "accounts": n_accounts,
        "accounts_error": accounts_error,
        "imap_user": imap_user,
        "imap_ready": imap_ready,
        "proxies": n_proxies,
        "proxy_file": proxy_file,
        "dry_run": bool(purchasing.get("dry_run", True)),
        "purchasing_enabled": bool(purchasing.get("enabled", False)),
        "max_total_usd": purchasing.get("max_total_usd", 5.0),
        "channel": walmart.get("channel") or "chromium",
        "todo": todo,
    }


def register_walmart_routes(app: Flask, config_path: str, job, py_cli, root: Path) -> None:
    @app.get("/walmart")
    def walmart_page() -> Response:
        return Response(WALMART_HTML, mimetype="text/html")

    @app.get("/api/walmart/status")
    def api_walmart_status():
        status = walmart_status(config_path)
        shot_dir = root / "screenshots"
        shots: list[str] = []
        if shot_dir.is_dir():
            pngs = sorted(
                shot_dir.glob("walmart-*.png"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            shots = [p.name for p in pngs[:40]]
        status.update(
            running=job.running,
            kind=job.kind,
            lines=list(job.lines),
            screenshots=shots,
        )
        return jsonify(status)

    @app.post("/api/walmart/start")
    def api_walmart_start():
        args = ["walmart-add-phone"]
        limit = request.args.get("limit")
        if limit:
            args += ["--limit", str(int(limit))]
        if request.args.get("no_resume") == "1":
            args.append("--no-resume")
        started = job.start("walmart-add-phone", py_cli(*args), root)
        return jsonify(started=started, running=job.running)

    @app.post("/api/walmart/stop")
    def api_walmart_stop():
        job.stop()
        return jsonify(stopped=True)


WALMART_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Walmart — add phone numbers</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif; margin:0;
         background:#f5f6f8; color:#1b1b1f; }
  @media (prefers-color-scheme: dark){ body{ background:#16171a; color:#e8e8ea; } }
  header { background:#0071dc; color:#fff; padding:16px 22px;
           display:flex; justify-content:space-between; align-items:center;
           gap:12px; flex-wrap:wrap; }
  header h1 { margin:0; font-size:19px; }
  header a { color:#fff; font-size:13px; opacity:.9; margin-left:14px; }
  main { max-width:1080px; margin:0 auto; padding:20px; }
  .card { background:#fff; border-radius:12px; padding:18px; margin-bottom:18px;
          box-shadow:0 1px 3px rgba(0,0,0,.08); }
  @media (prefers-color-scheme: dark){ .card{ background:#232428; box-shadow:none; } }
  h2 { font-size:15px; margin:0 0 12px; }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  button { font-size:15px; font-weight:600; border:0; border-radius:9px;
           padding:11px 16px; cursor:pointer; }
  button.primary { background:#0071dc; color:#fff; }
  button.secondary { background:#e6e8eb; color:#1b1b1f; }
  @media (prefers-color-scheme: dark){ button.secondary{ background:#3a3b40; color:#e8e8ea; } }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .note { font-size:13px; opacity:.78; line-height:1.55; }
  .stat { display:inline-block; margin-right:26px; }
  .stat b { font-size:20px; display:block; }
  .stat span { font-size:12px; opacity:.7; }
  .mode { font-size:14px; font-weight:700; padding:11px 15px; border-radius:10px;
          margin-bottom:14px; }
  .mode.dry { background:#e3f0ff; color:#14418a; }
  .mode.live { background:#ffe0e0; color:#8c1c1c; }
  @media (prefers-color-scheme: dark){
    .mode.dry{ background:#16304f; color:#a8cdff; }
    .mode.live{ background:#4a1f1f; color:#ffb3b3; }
  }
  ul.todo { margin:8px 0 0; padding-left:20px; font-size:13px; opacity:.85; line-height:1.7; }
  #log { background:#0d0f12; color:#c7f0d0; font-family:ui-monospace, Menlo, monospace;
         font-size:12.5px; padding:14px; border-radius:9px; height:320px;
         overflow:auto; white-space:pre-wrap; }
  .pill { font-size:12px; padding:3px 9px; border-radius:20px; font-weight:600; }
  .pill.idle { background:#e6e8eb; color:#555; }
  .pill.run { background:#ffe9b3; color:#7a5a00; }
  @media (prefers-color-scheme: dark){ .pill.idle{ background:#3a3b40; color:#bbb; } }
  .shots { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:10px; }
  .shots figure { margin:0; }
  .shots img { width:100%; border-radius:8px; border:1px solid rgba(0,0,0,.12); cursor:zoom-in; }
  .shots figcaption { font-size:11px; opacity:.7; margin-top:3px; }
  .err { color:#b3261e; font-size:13px; }
  @media (prefers-color-scheme: dark){ .err{ color:#ff9c94; } }
</style>
</head>
<body>
<header>
  <h1>Walmart — add phone numbers</h1>
  <div>
    <a href="/sms">SMS providers &amp; stock</a>
    <a href="/">Sam's Club automation</a>
  </div>
</header>
<main>
  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div>
        <div class="stat"><b id="naccounts">–</b><span>accounts</span></div>
        <div class="stat"><b id="nproxies">–</b><span>proxies</span></div>
        <div class="stat"><b id="chan" style="font-size:15px">–</b><span>browser</span></div>
      </div>
      <span class="pill idle" id="pill">idle</span>
    </div>
    <p class="err" id="accterr" style="display:none"></p>
  </div>

  <div class="card">
    <div class="mode dry" id="mode">checking…</div>
    <div class="row">
      <button class="primary" id="b-one" onclick="start(1)">Run 1 account</button>
      <button class="primary" id="b-all" onclick="startAll()">Run all accounts</button>
      <button class="secondary" id="b-stop" onclick="stop()">Stop</button>
    </div>
    <div id="todo"></div>
  </div>

  <div class="card">
    <h2>Live progress</h2>
    <div id="log"></div>
  </div>

  <div class="card">
    <h2>Screenshots <span class="note" id="shotcount"></span></h2>
    <p class="note">One per step. When a run stops on a selector, the screenshot
      of that step is what identifies the right one — send it over along with
      the error line above.</p>
    <div class="shots" id="shots"></div>
  </div>

  <p class="note" style="text-align:center" id="build">&nbsp;</p>
</main>
<script>
const $ = id => document.getElementById(id);
async function jget(u){ const r = await fetch(u); return r.json(); }

async function start(limit){
  await fetch('/api/walmart/start' + (limit ? '?limit=' + limit : ''), {method:'POST'});
  tick();
}
async function startAll(){
  const s = await jget('/api/walmart/status');
  const warn = s.purchasing_enabled && !s.dry_run
    ? `This spends real money, up to $${s.max_total_usd} for the run.\\n\\n`
    : '';
  if (confirm(warn + `Run all ${s.accounts} account(s)?`)) start(null);
}
async function stop(){ await fetch('/api/walmart/stop', {method:'POST'}); tick(); }

let lastLen = -1;
async function tick(){
  const s = await jget('/api/walmart/status');

  $('naccounts').textContent = s.accounts;
  $('nproxies').textContent = s.proxies || '0';
  $('chan').textContent = s.channel;
  $('build').textContent = 'build ' + (s.build || '?');

  $('accterr').style.display = s.accounts_error ? 'block' : 'none';
  $('accterr').textContent = s.accounts_error || '';

  const mode = $('mode');
  if (s.dry_run || !s.purchasing_enabled){
    mode.className = 'mode dry';
    mode.textContent = 'DRY RUN — walks the whole flow, buys nothing';
  } else {
    mode.className = 'mode live';
    mode.textContent = `LIVE — real money, capped at $${s.max_total_usd} for the run`;
  }

  $('todo').innerHTML = s.todo.length
    ? '<p class="note" style="margin:12px 0 0"><b>Still to set up:</b></p><ul class="todo">'
      + s.todo.map(t => `<li>${t}</li>`).join('') + '</ul>'
    : '<p class="note" style="margin:12px 0 0">Everything is configured.</p>';

  const pill = $('pill');
  pill.textContent = s.running ? 'running' : 'idle';
  pill.className = 'pill ' + (s.running ? 'run' : 'idle');
  $('b-one').disabled = s.running || !s.accounts;
  $('b-all').disabled = s.running || !s.accounts;
  $('b-stop').disabled = !s.running;

  const log = $('log');
  log.textContent = s.lines.length ? s.lines.join('\\n') : 'No run yet.';
  if (s.lines.length !== lastLen){ log.scrollTop = log.scrollHeight; lastLen = s.lines.length; }

  $('shotcount').textContent = s.screenshots.length ? '(' + s.screenshots.length + ')' : '';
  $('shots').innerHTML = s.screenshots.map(n =>
    '<figure><a href="/screenshots/' + encodeURIComponent(n) + '" target="_blank">' +
    '<img src="/screenshots/' + encodeURIComponent(n) + '"></a>' +
    '<figcaption>' + n + '</figcaption></figure>').join('');
}

tick();
setInterval(tick, 1500);
</script>
</body>
</html>
"""
