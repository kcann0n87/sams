"""The SMS-provider page of the local web UI.

Entering a dozen API keys is miserable on the command line, so this is a form
for it, plus a live availability table and a watch mode that keeps polling and
alerts when a Walmart pool refills.

Keys are written to `sms_keys.json` (git-ignored, chmod 600) and are never sent
back to the browser — the page only ever learns that a key is *set* and its
last four characters. The server binds to 127.0.0.1, same as the rest of the UI.

Mounted by web.py at /sms.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from .config import load_sms_settings, save_sms_key


class SmsCheckJob:
    """Runs a provider sweep on a worker thread and holds the latest result.

    Checks take tens of seconds across a dozen providers, which is far too long
    to block a request, and watch mode needs them to keep running while nobody
    is looking at the page.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.results: list[dict[str, Any]] = []
        self.finished_at: float | None = None
        self.started_at: float | None = None
        self.error: str | None = None
        self.poll_count = 0
        # Offer identities seen in stock, so the page can highlight new ones.
        self.seen_in_stock: set[str] = set()
        self.last_restock: float | None = None
        self.restock_offers: list[str] = []

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, config_path: str, us_only: bool, terms: list[str] | None) -> bool:
        with self.lock:
            if self.running:
                return False
            self.started_at = time.time()
            self.error = None
            self.thread = threading.Thread(
                target=self._run, args=(config_path, us_only, terms), daemon=True
            )
            self.thread.start()
            return True

    def _run(self, config_path: str, us_only: bool, terms: list[str] | None) -> None:
        import dataclasses

        from .sms_providers import DEFAULT_TERMS, build_providers, check_all

        try:
            settings = load_sms_settings(config_path)
            providers, problems = build_providers(settings)
            reports = check_all(
                providers, terms or list(DEFAULT_TERMS), us_only=us_only
            )
            results = [dataclasses.asdict(r) for r in reports]
            for r, report in zip(results, reports):
                r["total_stock"] = report.total_stock
                for offer, src in zip(r["offers"], report.offers):
                    offer["in_stock"] = src.in_stock
                    offer["key"] = _offer_key(src)
            if problems:
                results.append(
                    {
                        "provider": "(config)",
                        "ok": False,
                        "error": "; ".join(problems),
                        "offers": [],
                        "notes": [],
                        "total_stock": None,
                    }
                )

            current = {
                o["key"]
                for r in results
                if r.get("ok")
                for o in r["offers"]
                if o["in_stock"]
            }
            with self.lock:
                fresh = current - self.seen_in_stock
                # Not on the very first sweep: everything is "new" then, which
                # would fire a restock alert for a pool that was always full.
                if fresh and self.poll_count > 0:
                    self.last_restock = time.time()
                    self.restock_offers = sorted(fresh)
                self.seen_in_stock = current
                self.results = results
                self.poll_count += 1
                self.finished_at = time.time()
        except Exception as e:
            with self.lock:
                self.error = f"{type(e).__name__}: {e}"
                self.finished_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "results": self.results,
                "error": self.error,
                "finished_at": self.finished_at,
                "poll_count": self.poll_count,
                "last_restock": self.last_restock,
                "restock_offers": self.restock_offers,
            }


def _offer_key(offer: Any) -> str:
    return f"{offer.provider}/{offer.service_code}/{offer.country}/{offer.operator}"


def build_id() -> str:
    """Short git revision of the running code.

    Shown in the page footer: the server doesn't reload on its own, so "I don't
    see the new thing" is nearly always a stale process rather than a missing
    feature, and this makes that visible instead of guesswork.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            timeout=3,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _mask(value: str) -> str:
    """Show enough to recognise a key, never enough to use it."""
    value = (value or "").strip()
    if not value:
        return ""
    return f"••••{value[-4:]}" if len(value) > 4 else "••••"


def provider_rows(config_path: str) -> list[dict[str, Any]]:
    """Every known provider, whether it has a key, and where that key came from."""
    import os

    from .sms_providers import (
        ACTIVATE_ALIASES,
        DEAD_PROVIDERS,
        KNOWN_ACTIVATE_HOSTS,
        PROVIDERS,
    )

    settings = load_sms_settings(config_path)
    rows: list[dict[str, Any]] = []

    def add(name: str, env: str, kind: str, needs_key: bool, needs_username: bool) -> None:
        entry = settings.get(name) or {}
        configured = str(entry.get("api_key") or "").strip()
        from_env = os.environ.get(env, "").strip() if env else ""
        key = configured or from_env
        rows.append(
            {
                "name": name,
                "env": env,
                "kind": kind,
                "needs_key": needs_key,
                "needs_username": needs_username,
                "username": str(entry.get("username") or ""),
                "has_key": bool(key) or not needs_key,
                "masked": _mask(key),
                "source": "saved" if configured else ("environment" if from_env else ""),
                "enabled": entry.get("enabled") is not False,
                "dead": name in DEAD_PROVIDERS,
                "note": DEAD_PROVIDERS.get(name, ""),
            }
        )

    for name, cls in PROVIDERS.items():
        add(name, cls.env_key, "adapter", cls.needs_key, bool(cls.env_username))
    for name, spec in KNOWN_ACTIVATE_HOSTS.items():
        if name in ACTIVATE_ALIASES:
            continue  # same site as a built-in adapter listed above
        add(name, spec["env"], "activate", True, False)

    # Providers discovered by probing. Without this they'd be saved and working
    # but invisible on the page that's supposed to list what's configured.
    listed = {r["name"] for r in rows}
    for name, entry in settings.items():
        if not isinstance(entry, dict) or not entry.get("protocol") or name in listed:
            continue
        add(name, "", f"custom · {entry['protocol']}", True, False)
        rows[-1]["base_url"] = str(entry.get("base_url") or "")
    for name, note in DEAD_PROVIDERS.items():
        rows.append(
            {
                "name": name, "env": "", "kind": "dead", "needs_key": True,
                "needs_username": False, "username": "", "has_key": False,
                "masked": "", "source": "", "enabled": False, "dead": True, "note": note,
            }
        )

    rows.sort(key=lambda r: (r["dead"], not r["has_key"], r["name"]))
    return rows


def register_sms_routes(app: Flask, config_path: str) -> None:
    job = SmsCheckJob()

    @app.get("/sms")
    def sms_page() -> Response:
        return Response(SMS_HTML, mimetype="text/html")

    @app.get("/api/sms/providers")
    def api_sms_providers():
        rows = provider_rows(config_path)
        return jsonify(
            providers=rows,
            ready=sum(1 for r in rows if r["has_key"] and not r["dead"]),
            build=build_id(),
            has_add_form=True,
        )

    @app.post("/api/sms/key")
    def api_sms_key():
        data = request.get_json(silent=True) or {}
        name = str(data.get("provider") or "").strip()
        if not name:
            return jsonify(ok=False, error="no provider given")
        try:
            save_sms_key(
                name,
                api_key=data.get("api_key"),
                username=data.get("username"),
                enabled=data.get("enabled"),
            )
        except OSError as e:
            return jsonify(ok=False, error=str(e))
        return jsonify(ok=True)

    @app.post("/api/sms/probe")
    def api_sms_probe():
        """Identify an unknown provider's protocol, and save it if one matches."""
        from .sms_providers import probe

        data = request.get_json(silent=True) or {}
        name = str(data.get("name") or "").strip()
        base_url = str(data.get("base_url") or "").strip().rstrip("/")
        api_key = str(data.get("api_key") or "").strip()
        if not name or not base_url:
            return jsonify(ok=False, error="name and base URL are both required")
        if not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url

        calls: list[dict[str, Any]] = []
        results = probe(base_url, api_key, name=name, capture=calls)
        winner = next((r for r in results if r.sells_walmart), None) or next(
            (r for r in results if r.ok), None
        )

        saved = False
        if winner is not None:
            save_sms_key(
                name, api_key=api_key, protocol=winner.protocol, base_url=base_url
            )
            saved = True

        return jsonify(
            ok=True,
            saved=saved,
            protocol=winner.protocol if winner else None,
            sells_walmart=bool(winner and winner.sells_walmart),
            tried=[
                {"protocol": r.protocol, "ok": r.ok, "detail": r.detail,
                 "walmart": len(r.walmart)}
                for r in results
            ],
            # api_key is stripped from captured params by record_http, so this
            # is safe to show in the browser and safe for the user to share.
            calls=calls,
        )

    @app.post("/api/sms/check")
    def api_sms_check():
        started = job.start(
            config_path,
            us_only=request.args.get("all_countries") != "1",
            terms=None,
        )
        return jsonify(started=started, running=job.running)

    @app.get("/api/sms/status")
    def api_sms_status():
        return jsonify(job.snapshot())

    return None


SMS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SMS providers — Walmart availability</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif; margin:0;
         background:#f5f6f8; color:#1b1b1f; }
  @media (prefers-color-scheme: dark){ body{ background:#16171a; color:#e8e8ea; } }
  header { background:#0071dc; color:#fff; padding:16px 22px;
           display:flex; justify-content:space-between; align-items:center; gap:12px;
           flex-wrap:wrap; }
  header h1 { margin:0; font-size:19px; }
  header a { color:#fff; font-size:13px; opacity:.9; }
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
  button.small { font-size:13px; padding:8px 12px; }
  @media (prefers-color-scheme: dark){ button.secondary{ background:#3a3b40; color:#e8e8ea; } }
  button:disabled { opacity:.5; cursor:not-allowed; }
  input[type=text], input[type=password] { padding:10px; border-radius:8px;
     border:1px solid #ccc; font-size:14px; flex:1; min-width:150px;
     background:transparent; color:inherit; }
  table { width:100%; border-collapse:collapse; font-size:13.5px; }
  th { text-align:left; font-size:11.5px; text-transform:uppercase; letter-spacing:.4px;
       opacity:.6; padding:6px 8px; border-bottom:1px solid rgba(128,128,128,.25); }
  td { padding:8px; border-bottom:1px solid rgba(128,128,128,.14); vertical-align:middle; }
  .prov { font-weight:600; }
  .env { font-family:ui-monospace, Menlo, monospace; font-size:11px; opacity:.6; }
  .tag { font-size:10.5px; padding:2px 7px; border-radius:20px; font-weight:700;
         text-transform:uppercase; letter-spacing:.3px; }
  .tag.ok { background:#d6f5df; color:#136c33; }
  .tag.none { background:#eceef1; color:#666; }
  .tag.dead { background:#ffdcdc; color:#8c1c1c; }
  .tag.env { background:#dbeafe; color:#1e40af; }
  @media (prefers-color-scheme: dark){
    .tag.none{ background:#3a3b40; color:#bbb; } .tag.ok{ background:#14432a; color:#7ee2a8; }
    .tag.dead{ background:#4a1f1f; color:#ffb3b3; } .tag.env{ background:#1e3a5f; color:#93c5fd; }
  }
  .stock { font-weight:700; }
  .stock.yes { color:#137333; } .stock.no { color:#999; }
  @media (prefers-color-scheme: dark){ .stock.yes{ color:#7ee2a8; } }
  .note { font-size:13px; opacity:.75; line-height:1.55; }
  .err { color:#b3261e; font-size:13px; }
  @media (prefers-color-scheme: dark){ .err{ color:#ff9c94; } }
  .banner { background:#137333; color:#fff; padding:14px 18px; border-radius:10px;
            font-weight:600; margin-bottom:16px; display:none; }
  .muted { opacity:.55; }
  .verdict { font-size:15px; font-weight:700; padding:13px 16px; border-radius:10px;
             margin-bottom:16px; }
  .verdict.yes { background:#d6f5df; color:#0f5c2b; }
  .verdict.no  { background:#eceef1; color:#4a4a4a; }
  @media (prefers-color-scheme: dark){
    .verdict.yes{ background:#14432a; color:#8fe9b3; }
    .verdict.no { background:#31323a; color:#c9c9cf; }
  }
  .flash { animation: flash 1.6s ease-out; }
  @keyframes flash { from { background:#ffe9b3; } to { background:transparent; } }
  details summary { cursor:pointer; font-size:13px; opacity:.8; }
</style>
</head>
<body>
<header>
  <h1>SMS providers — Walmart availability</h1>
  <a href="/">&larr; back to automation</a>
</header>
<main>
  <div class="banner" id="banner"></div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div class="row">
        <button class="primary" id="b-check" onclick="checkNow()">Check now</button>
        <label class="note"><input type="checkbox" id="watch" onchange="toggleWatch()"> Keep watching</label>
        <label class="note"><input type="checkbox" id="allc"> All countries</label>
      </div>
      <div class="note" id="lastrun">never checked</div>
    </div>
    <p class="note" id="hint" style="margin-bottom:0">
      Watch mode re-checks every 60s and alerts the moment a pool refills.
      Leave this tab open — US Walmart stock appears in short windows.</p>
  </div>

  <div class="card">
    <h2>Availability</h2>
    <div id="results"><p class="note">Press <b>Check now</b>.</p></div>
  </div>

  <div class="card">
    <h2>API keys <span class="note" id="readycount"></span></h2>
    <p class="note">Saved to <code>sms_keys.json</code> in this folder — git-ignored,
      readable only by you, never sent anywhere but the provider it belongs to.
      Keys already exported in your shell are picked up automatically.</p>
    <table><thead><tr>
      <th>Provider</th><th>Key</th><th>Status</th><th></th>
    </tr></thead><tbody id="provs"></tbody></table>
  </div>

  <div class="card">
    <h2>Add a provider that isn't listed</h2>
    <p class="note">For a site with no adapter yet — Foones, or anything else.
      Give it a name, its API base URL and your key, and it tries each protocol
      it knows to work out which one the site speaks. If one matches it's saved
      and appears above; nothing is purchased either way.</p>
    <div class="row">
      <input type="text" id="np-name" placeholder="name (e.g. foones)" style="max-width:180px">
      <input type="text" id="np-url" placeholder="https://api.foones.com">
      <input type="password" id="np-key" placeholder="API key" autocomplete="off">
      <button class="primary" id="b-detect" onclick="detectProvider()">Detect</button>
    </div>
    <div id="probeout" style="margin-top:14px"></div>
  </div>

  <p class="note" style="text-align:center" id="build">&nbsp;</p>
</main>
<script>
const $ = id => document.getElementById(id);
let watchTimer = null, lastRestock = null, notifiedFirst = false;

async function jget(u){ const r = await fetch(u); return r.json(); }

async function loadProviders(){
  const d = await jget('/api/sms/providers');
  $('readycount').textContent = '(' + d.ready + ' ready)';
  $('build').textContent = 'build ' + (d.build || '?') +
      ' — if this card is missing features, stop the server (Ctrl-C) and run ./start.sh again';
  $('provs').innerHTML = d.providers.map(p => {
    if (p.dead) return `<tr class="muted"><td class="prov">${p.name}</td>
      <td colspan="2" class="note">${p.note}</td>
      <td><span class="tag dead">defunct</span></td></tr>`;
    const status = !p.needs_key ? '<span class="tag ok">no key needed</span>'
      : p.source === 'saved' ? `<span class="tag ok">saved ${p.masked}</span>`
      : p.source === 'environment' ? `<span class="tag env">from $${p.env}</span>`
      : '<span class="tag none">not set</span>';
    const user = p.needs_username
      ? `<input type="text" id="u-${p.name}" placeholder="account email" value="${p.username||''}">` : '';
    return `<tr>
      <td class="prov">${p.name}<div class="env">${p.env||''}</div></td>
      <td><div class="row">
        <input type="password" id="k-${p.name}" placeholder="paste API key" autocomplete="off">
        ${user}</div></td>
      <td>${status}</td>
      <td><button class="secondary small" onclick="saveKey('${p.name}')">Save</button></td>
    </tr>`;
  }).join('');
}

async function saveKey(name){
  const keyEl = $('k-'+name), userEl = $('u-'+name);
  const body = { provider: name };
  if (keyEl && keyEl.value.trim()) body.api_key = keyEl.value.trim();
  if (userEl) body.username = userEl.value.trim();
  if (!body.api_key && !('username' in body)) return;
  const r = await fetch('/api/sms/key', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if (keyEl) keyEl.value = '';
  if (!j.ok) alert('Save failed: ' + (j.error||'unknown'));
  loadProviders();
}

async function detectProvider(){
  const name = $('np-name').value.trim(), url = $('np-url').value.trim();
  const key = $('np-key').value.trim();
  if (!name || !url){ $('probeout').innerHTML = '<div class="err">Name and base URL are required.</div>'; return; }
  $('b-detect').disabled = true; $('b-detect').textContent = 'Detecting…';
  $('probeout').innerHTML = '<p class="note">Trying each known protocol…</p>';
  try {
    const r = await fetch('/api/sms/probe', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, base_url: url, api_key: key})});
    const j = await r.json();
    if (!j.ok){ $('probeout').innerHTML = `<div class="err">${j.error}</div>`; return; }

    const tried = j.tried.map(t =>
      `<tr class="${t.ok?'':'muted'}"><td>${t.protocol}</td>
       <td>${t.ok ? (t.walmart ? `<span class="tag ok">${t.walmart} Walmart offer(s)</span>` : 'responded, no Walmart service') : ''}</td>
       <td class="note">${t.ok ? '' : t.detail}</td></tr>`).join('');

    let head;
    if (j.saved && j.sells_walmart)
      head = `<div class="verdict yes">✅ Match — speaks <b>${j.protocol}</b> and lists Walmart. Saved.</div>`;
    else if (j.saved)
      head = `<div class="verdict no">Speaks <b>${j.protocol}</b> but lists no Walmart service. Saved anyway.</div>`;
    else
      head = `<div class="verdict no">⛔ No match — this site speaks an API we don't know yet.
        The requests below are what it was asked; a <b>401/403</b> means the path exists and
        only the auth style is wrong, while 404 or HTML everywhere means the API is at a
        different base URL. Your key is not included, so this is safe to share.</div>`;

    const calls = (j.calls||[]).map(c =>
      `<div style="margin:8px 0"><code class="env">${c.ok?'ok ':'ERR'} ${c.method} ${c.url}</code>
       <div class="note" style="margin-left:12px">${(c.detail||'').replace(/</g,'&lt;')}</div></div>`).join('');

    $('probeout').innerHTML = head +
      `<table><thead><tr><th>Protocol</th><th>Result</th><th></th></tr></thead>
       <tbody>${tried}</tbody></table>` +
      (j.saved ? '' : `<details style="margin-top:12px"><summary>Every request tried (${(j.calls||[]).length})</summary>${calls}</details>`);
    if (j.saved){ $('np-key').value = ''; loadProviders(); }
  } finally {
    $('b-detect').disabled = false; $('b-detect').textContent = 'Detect';
  }
}

async function checkNow(){
  const all = $('allc').checked ? '?all_countries=1' : '';
  await fetch('/api/sms/check' + all, {method:'POST'});
  poll();
}

function toggleWatch(){
  if ($('watch').checked){
    if (window.Notification && Notification.permission === 'default') Notification.requestPermission();
    checkNow();
    watchTimer = setInterval(checkNow, 60000);
  } else {
    clearInterval(watchTimer); watchTimer = null;
  }
}

function alertRestock(offers){
  const msg = offers.length + ' Walmart offer(s) back in stock';
  $('banner').textContent = '🔔 ' + msg + ' — ' + offers.join(', ');
  $('banner').style.display = 'block';
  try { new Audio('data:audio/wav;base64,UklGRl9vT19XQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=').play(); } catch(e){}
  if (window.Notification && Notification.permission === 'granted')
    new Notification('Walmart numbers in stock', { body: offers.join('\\n') });
}

function summaryLine(rs){
  // The whole question is "is there stock right now", so answer it up top
  // rather than making the pools be read out of four separate tables.
  let inStock = [], totalQty = 0, offering = 0, empty = 0, noService = 0, errors = 0;
  for (const r of rs){
    if (!r.ok){ errors++; continue; }
    if (!r.offers.length){ noService++; continue; }
    offering++;
    const live = r.offers.filter(o => o.in_stock);
    if (live.length) inStock.push(...live); else empty++;
    for (const o of live) if (o.count != null) totalQty += o.count;
  }
  if (inStock.length){
    const priced = inStock.filter(o => o.price != null)
                          .sort((a,b) => a.price - b.price)[0];
    const qty = totalQty ? `${totalQty} number(s)` : `${inStock.length} offer(s)`;
    const best = priced
      ? ` · cheapest ${priced.price.toFixed(2)} ${priced.currency} at ${priced.provider}`
      : '';
    return `<div class="verdict yes">✅ In stock — ${qty} available${best}</div>`;
  }
  const bits = [];
  if (empty) bits.push(`${empty} provider(s) offer Walmart but the pools are empty`);
  if (noService) bits.push(`${noService} don't list it at all`);
  if (errors) bits.push(`${errors} errored`);
  return `<div class="verdict no">⛔ No Walmart stock right now` +
         (bits.length ? ` — ${bits.join(', ')}` : '') +
         `<div class="note" style="margin-top:6px;color:inherit;opacity:.85">` +
         `Tick <b>Keep watching</b> to get alerted the moment a pool refills.</div></div>`;
}

function renderReports(rs){
  if (!rs.length) return '<p class="note">No providers configured — add a key below.</p>';
  return summaryLine(rs) + rs.map(r => {
    if (!r.ok) return `<div style="margin-bottom:14px"><span class="prov">${r.provider}</span>
      <div class="err">${r.error||'failed'}</div></div>`;
    const rows = r.offers.map(o => `<tr class="${o.in_stock?'':'muted'}">
        <td>${o.service}</td><td>${o.country}</td><td>${o.operator}</td>
        <td>${o.price==null?'n/a':o.price.toFixed(2)+' '+o.currency}</td>
        <td class="stock ${o.in_stock?'yes':'no'}">${o.count==null?'—':o.count}</td>
        <td>${o.success_rate==null?'':o.success_rate.toFixed(0)+'%'}</td></tr>`).join('');
    const notes = (r.notes||[]).map(n => `<div class="note">${n}</div>`).join('');
    const summary = r.total_stock==null ? 'stock not reported' : r.total_stock + ' in stock';
    return `<div style="margin-bottom:18px">
      <span class="prov">${r.provider}</span> <span class="note">— ${summary}</span>
      ${notes}
      ${r.offers.length ? `<table><thead><tr><th>Service</th><th>Country</th><th>Operator</th>
        <th>Price</th><th>Qty</th><th>Rate</th></tr></thead><tbody>${rows}</tbody></table>` : ''}
    </div>`;
  }).join('');
}

async function poll(){
  const s = await jget('/api/sms/status');
  $('b-check').disabled = s.running;
  $('b-check').textContent = s.running ? 'Checking…' : 'Check now';
  if (s.finished_at){
    const when = new Date(s.finished_at*1000).toLocaleTimeString();
    $('lastrun').textContent = `last checked ${when} · ${s.poll_count} poll(s)`;
  }
  if (s.error) $('results').innerHTML = `<div class="err">${s.error}</div>`;
  else if (s.results.length) $('results').innerHTML = renderReports(s.results);
  if (s.last_restock && s.last_restock !== lastRestock){
    lastRestock = s.last_restock;
    alertRestock(s.restock_offers || []);
    $('results').classList.add('flash');
    setTimeout(()=>$('results').classList.remove('flash'), 1700);
  }
  if (s.running) setTimeout(poll, 1200);
}

loadProviders();
poll();
</script>
</body>
</html>
"""
