#!/usr/bin/env python3
"""Generate site/index.html — the static logpile.ai landing page.

Bakes real aggregates from logpile.db (the author's live index) into a
self-contained HTML file. No fabricated numbers: every figure is queried at
build time and the page is stamped with the as-of date. Deploy with
scripts/deploy_landing.sh (Cloudflare Worker).

This is intentionally an aggregate publication, independent of per-session
visibility: totals and daily cadence include the author's private, unlisted,
and public sessions, while no transcript content or session metadata is
emitted.  Keep the landing page's existing fine-print disclosure in sync with
that policy.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "logpile.db"
OUT = ROOT / "site" / "index.html"

GITHUB = "https://github.com/MaxGhenis/logpile"


def query(db: sqlite3.Connection):
    overview = db.execute(
        """SELECT COUNT(*) AS sessions,
                  SUM(native_total_output_tokens) AS out_tokens,
                  COUNT(DISTINCT repo_name) AS repos,
                  MIN(substr(first_timestamp, 1, 10)) AS since
           FROM session_catalog"""
    ).fetchone()
    days = db.execute(
        """SELECT day,
                  COUNT(DISTINCT session_id) AS sessions,
                  SUM(native_total_output_tokens) AS out_tokens
           FROM session_daily_effective
           WHERE day <= date('now')
           GROUP BY day ORDER BY day DESC LIMIT 14"""
    ).fetchall()
    return overview, days


def fmt_b(n: float | None) -> str:
    n = n or 0
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.0f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return f"{n:.0f}"


def build() -> None:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    (ov, days) = query(db)
    db.close()

    today = dt.date.today().strftime("%b %-d, %Y")
    max_out = max((d["out_tokens"] or 0) for d in days) or 1
    ledger_rows = "\n".join(
        f"""      <div class="lrow" style="--i:{i}">
        <span class="lday">{html.escape(d["day"])}</span>
        <span class="lbar"><span style="width:{max(3, round(100 * (d["out_tokens"] or 0) / max_out))}%"></span></span>
        <span class="lnum">{fmt_b(d["out_tokens"])}<em> out</em></span>
        <span class="lses">{d["sessions"]:,}<em> sessions</em></span>
      </div>"""
        for i, d in enumerate(days)
    )

    stats_line = (
        f"{ov['sessions']:,} sessions · {fmt_b(ov['out_tokens'])} output tokens · "
        f"{ov['repos']:,} repos — the author's index, as of {today}."
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Logpile — the record of agentic work</title>
<meta name="description" content="A local-first archive of every Claude Code and Codex session on your machine — indexed into SQLite, readable as transcripts, publishable with secret scanning.">
<meta property="og:title" content="Logpile — the record of agentic work">
<meta property="og:description" content="Agents do the work. Logpile keeps the record. Local-first archive of Claude Code and Codex sessions.">
<meta property="og:url" content="https://logpile.ai">
<link rel="icon" href="data:image/svg+xml,{html.escape('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect x="4" y="6" width="20" height="5" rx="2.5" fill="%23f59e0b"/><rect x="4" y="14" width="24" height="5" rx="2.5" fill="%23f59e0b"/><rect x="4" y="22" width="17" height="5" rx="2.5" fill="%23f59e0b"/></svg>')}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Plus+Jakarta+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0c0a09; --surface: #1c1917; --raised: #292524;
    --border: #44403c; --border-dim: #292524;
    --text: #fafaf9; --dim: #a8a29e; --faint: #78716c;
    --amber: #f59e0b; --amber-dim: #d97706; --amber-hot: #fbbf24;
    --glow: rgba(245, 158, 11, 0.12);
    --brand: "Playfair Display", Georgia, serif;
    --body: "Plus Jakarta Sans", system-ui, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, monospace;
  }}
  * {{ box-sizing: border-box; margin: 0; }}
  body {{
    background: var(--bg); color: var(--text); font-family: var(--body);
    line-height: 1.6; -webkit-font-smoothing: antialiased;
  }}
  body::before {{
    content: ""; position: fixed; inset: 0; z-index: 50; pointer-events: none; opacity: .025;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
    background-size: 128px 128px;
  }}
  a {{ color: var(--amber); text-decoration: none; }}
  a:hover {{ color: var(--amber-hot); }}
  a:focus-visible, button:focus-visible {{ outline: 2px solid var(--amber); outline-offset: 2px; border-radius: 4px; }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 0 24px; }}

  header {{ display: flex; align-items: center; justify-content: space-between; padding: 22px 0; }}
  .brand {{ display: flex; align-items: center; gap: 10px; color: var(--text); }}
  .mark {{ display: flex; flex-direction: column; gap: 3px; width: 22px; }}
  .mark span {{ display: block; height: 4px; border-radius: 2px; background: var(--amber); }}
  .mark span:nth-child(1) {{ width: 18px; }}
  .mark span:nth-child(2) {{ width: 22px; margin-left: 1px; }}
  .mark span:nth-child(3) {{ width: 16px; margin-left: 3px; }}
  .brand b {{ font-family: var(--brand); font-size: 1.3rem; font-weight: 700; letter-spacing: -.01em; }}
  nav a {{ color: var(--dim); font-size: .9rem; margin-left: 22px; }}

  .hero {{ display: grid; grid-template-columns: 1.05fr .95fr; gap: 56px; align-items: center; padding: 64px 0 72px; }}
  .hero > * {{ min-width: 0; }}
  .eyebrow {{ font-family: var(--mono); font-size: .72rem; letter-spacing: .14em; text-transform: uppercase; color: var(--amber-dim); margin-bottom: 18px; }}
  h1 {{ font-family: var(--brand); font-weight: 900; font-size: clamp(2.1rem, 4.6vw, 3.3rem); line-height: 1.12; letter-spacing: -.015em; margin-bottom: 18px; }}
  h1 .keep {{ color: var(--amber); }}
  .sub {{ color: var(--dim); font-size: 1.02rem; max-width: 34em; margin-bottom: 26px; }}
  .install {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 66px 14px 16px; font-family: var(--mono); font-size: .78rem; color: var(--text); position: relative; overflow-x: auto; scrollbar-width: none; }}
  .install::-webkit-scrollbar {{ display: none; }}
  .install code {{ display: block; white-space: pre; }}
  .install .p {{ color: var(--amber-dim); user-select: none; }}
  .copy {{ position: absolute; top: 9px; right: 9px; background: var(--raised); color: var(--dim); border: 1px solid var(--border); border-radius: 6px; font: 500 .7rem var(--mono); padding: 4px 9px; cursor: pointer; }}
  .copy:hover {{ color: var(--amber); border-color: var(--amber); }}
  .trust {{ font-size: .82rem; color: var(--faint); margin-top: 13px; }}
  .trust b {{ color: var(--dim); font-weight: 600; }}
  .statline {{ font-family: var(--mono); font-size: .74rem; color: var(--faint); margin-top: 26px; }}

  .ledger {{ background: var(--surface); border: 1px solid var(--border-dim); border-radius: 12px; padding: 18px 18px 12px; }}
  .ltitle {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 2px 12px; font-family: var(--mono); font-size: .68rem; letter-spacing: .12em; text-transform: uppercase; color: var(--dim); margin-bottom: 12px; }}
  .ltitle > span {{ white-space: nowrap; }}
  .ltitle em {{ white-space: nowrap; }}
  .ltitle em {{ color: var(--faint); font-style: normal; letter-spacing: 0; text-transform: none; }}
  .lrow {{ display: grid; grid-template-columns: 84px 1fr 86px 100px; gap: 10px; align-items: center; padding: 5.5px 0; border-top: 1px solid var(--border-dim); font-family: var(--mono); font-size: .74rem; opacity: 0; animation: rise .45s ease-out forwards; animation-delay: calc(var(--i) * 55ms); }}
  .lday {{ color: var(--faint); }}
  .lbar {{ height: 6px; background: var(--raised); border-radius: 3px; overflow: hidden; }}
  .lbar span {{ display: block; height: 100%; background: linear-gradient(90deg, var(--amber), var(--amber-dim)); border-radius: 3px; }}
  .lnum {{ color: var(--text); text-align: right; }}
  .lses {{ color: var(--dim); text-align: right; }}
  .lrow em {{ color: var(--faint); font-style: normal; }}
  @keyframes rise {{ from {{ opacity: 0; transform: translateY(7px); }} to {{ opacity: 1; transform: none; }} }}
  @media (prefers-reduced-motion: reduce) {{ .lrow {{ animation: none; opacity: 1; }} }}

  .pillars {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; padding-bottom: 72px; }}
  .pillar {{ background: var(--surface); border: 1px solid var(--border-dim); border-radius: 12px; padding: 22px; }}
  .pillar h2 {{ font-family: var(--brand); font-size: 1.25rem; font-weight: 700; margin-bottom: 9px; }}
  .pillar h2 span {{ font-family: var(--mono); font-size: .68rem; color: var(--amber-dim); display: block; letter-spacing: .13em; text-transform: uppercase; margin-bottom: 7px; }}
  .pillar p {{ color: var(--dim); font-size: .9rem; }}
  .pillar code {{ font-family: var(--mono); font-size: .82em; color: var(--amber); }}

  footer {{ border-top: 1px solid var(--border-dim); padding: 26px 0 40px; display: flex; flex-wrap: wrap; gap: 8px 26px; align-items: baseline; justify-content: space-between; color: var(--faint); font-size: .84rem; }}
  footer .fine {{ font-family: var(--mono); font-size: .72rem; }}

  @media (max-width: 1360px) {{
    .install {{ font-size: .72rem; }}
  }}
  @media (max-width: 900px) {{
    .hero {{ grid-template-columns: 1fr; gap: 34px; padding: 40px 0 52px; }}
    .pillars {{ grid-template-columns: 1fr; }}
    .lrow {{ grid-template-columns: 74px 1fr 74px 84px; }}
  }}
  @media (max-width: 560px) {{
    .lrow {{ grid-template-columns: 84px 1fr 80px; }}
    .lses {{ display: none; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <a class="brand" href="https://logpile.ai"><span class="mark"><span></span><span></span><span></span></span><b>Logpile</b></a>
    <nav>
      <a href="{GITHUB}">GitHub</a>
      <a href="{GITHUB}#quick-start">Install</a>
    </nav>
  </header>

  <section class="hero">
    <div>
      <div class="eyebrow">local-first · Claude Code + Codex</div>
      <h1>Agents do the work. <span class="keep">Logpile keeps the record.</span></h1>
      <p class="sub">Every session your coding agents run is work product — and it's sitting in JSONL files you'll never open. Logpile indexes all of it into a searchable local archive: transcripts you can read, filter by what they did, and publish with secret scanning.</p>
      <div class="install">
        <button class="copy" data-cmd="git clone {GITHUB} && cd logpile
./logpile.sh sync && ./logpile.sh serve" onclick="navigator.clipboard.writeText(this.dataset.cmd);this.textContent='Copied';setTimeout(()=>this.textContent='Copy',1200)">Copy</button>
        <code><span class="p">$</span> git clone {GITHUB} && cd logpile</code>
        <code><span class="p">$</span> ./logpile.sh sync && ./logpile.sh serve</code>
      </div>
      <p class="trust"><b>Local-first.</b> Nothing leaves your machine unless you publish it. MIT-licensed.</p>
      <p class="statline">{stats_line}</p>
    </div>
    <div class="ledger" aria-label="The author's daily session ledger">
      <div class="ltitle"><span>The record — 14 days</span><em>live index · {today}</em></div>
{ledger_rows}
    </div>
  </section>

  <section class="pillars">
    <div class="pillar">
      <h2><span>01 · keep</span>Every session, indexed</h2>
      <p>One command turns the session files Claude Code and Codex leave on disk into a SQLite archive — with token accounting that dedups Codex fork replays and cross-file resumes, so the numbers are right.</p>
    </div>
    <div class="pillar">
      <h2><span>02 · read</span>Transcripts, not log soup</h2>
      <p>Sessions render as readable transcripts you can search and filter by what they actually did — wrote files, ran tests, committed — and by origin: your work vs. delegated agents vs. pipelines.</p>
    </div>
    <div class="pillar">
      <h2><span>03 · publish</span>Proof, when you choose</h2>
      <p>A review queue scans every transcript for secrets and PII before it can go public. Published sessions build an operator profile: the receipts for how you actually work with agents.</p>
    </div>
  </section>

  <footer>
    <span>Built by <a href="https://maxghenis.com">Max Ghenis</a> · <a href="{GITHUB}">github.com/MaxGhenis/logpile</a> · MIT</span>
    <span class="fine">All figures on this page are read from the author's live index at build time.</span>
  </footer>
</div>
</body>
</html>
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page)
    print(f"wrote {OUT} ({len(page):,} bytes)")
    print(json.dumps({"sessions": ov["sessions"], "out_tokens": ov["out_tokens"], "repos": ov["repos"]}))


if __name__ == "__main__":
    build()
