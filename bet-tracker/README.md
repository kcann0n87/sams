# Fairway Ledger — PGA Bet Tracker

A single-file web app for logging your weekly PGA sports bets and tracking how
you're actually doing — profit/loss, ROI, win rate, and charts, updated
automatically as you enter bets.

## Comes pre-loaded with your 2026 season

The tracker opens already populated with your full betting history imported from
your spreadsheet — 284 bets across 26 tournaments (Sony Open → ISCO
Championship), split into separate **To Win** (outright) and **Top 5** entries
per golfer so you can compare how each market performs. The imported totals
reconcile exactly to your sheet: **net −$216.41**, ~$21,876 staked, 16 wins.

This history loads once, the first time you open the app. After that your own
edits and new bets take over — clearing all data won't bring the seed back. The
raw import also lives in `history.json` if you ever want to re-import it.

## How to use it

Just open `index.html` in any web browser (double-click it, or drag it into a
browser tab). No install, no server, no account.

- **Log a bet** — click *Log a bet* and fill in the date, tournament, bet type,
  selection (the golfer), sportsbook, stake, and American odds. Set the result
  to *Pending* now and update it to *Won* / *Lost* / *Push* once it settles.
- **Payout math is automatic** — enter American odds (`+650`, `-160`) and the
  app computes your to-win amount and profit/loss for you.
- **Dashboard** — net profit, ROI, win rate, pending exposure, biggest win, and
  a running bankroll curve are all at the top.
- **Grouped by tournament** — every week's bets are grouped together with their
  own staked / P/L subtotal.
- **Filter & sort** — by type, sportsbook, result, or search text.

## Your data

Everything is stored **privately in your own browser** (localStorage) — nothing
is sent anywhere. Because of that, the data lives on the one device/browser you
enter it in.

Use the **Data ▾** menu to:
- **Export backup (JSON)** — save a file you can re-import later or on another
  device. Do this periodically so you never lose your history.
- **Export CSV** — open your bets in Excel / Google Sheets.
- **Import backup** — restore from a JSON export.
- **Load sample bets** — see how it looks with data before entering your own.

## Bet types supported

Outright Winner, Top 5 / 10 / 20, Make/Miss Cut, Matchup (2-ball), 3-Ball,
First Round Leader, Each-Way, Prop, Parlay, and Other. For cash-outs or
each-way bets where the payout isn't a simple win/loss, enter the **Actual
return** field and the app uses that exact amount.

## Possible next steps

This version keeps data in the browser. If you'd like to track across multiple
devices (phone + laptop) or share it, the natural next step is a small hosted
backend with a login. Happy to build that when you want it.
