"""The control panel (ADR-25, docs/15).

Runs on the owner's machine, bound to loopback, using their AWS credentials to
assume the admin role. It is not deployed, has no public endpoint and costs
nothing. Every action it takes is the same `ops/` code the CLI scripts call, so
the two surfaces cannot drift.

Three security properties, each with a test:

- it refuses to bind to a non-loopback address without `--i-know`,
- no route works without a resolvable admin role,
- every mutating route is a POST carrying a CSRF token.

The CSRF token matters even on loopback: any page in the owner's browser can
POST to 127.0.0.1, and the panel can pause their agent.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from ops import actions, readers
from ops.admin import AdminSession, Stage, StageRefusalError

LOOPBACK = ("127.0.0.1", "::1", "localhost")


@dataclass
class PanelConfig:
    stage: Stage
    session: AdminSession
    csrf_token: str


class BindRefusedError(RuntimeError):
    """The panel refused to listen somewhere it should not."""


def check_bind(host: str, *, i_know: bool = False) -> None:
    """Refuse a non-loopback bind: the panel holds admin credentials."""
    if host in LOOPBACK or i_know:
        return
    raise BindRefusedError(
        f"refusing to bind to {host!r}. The panel assumes the admin role and has "
        "no authentication of its own. Pass --i-know if you really mean it."
    )


def create_app(stage: Stage, session: AdminSession, *, csrf_token: str | None = None) -> FastAPI:
    config = PanelConfig(
        stage=stage, session=session, csrf_token=csrf_token or secrets.token_urlsafe(32)
    )
    app = FastAPI(title=f"Juno — {stage.name}", docs_url=None, redoc_url=None)
    app.state.config = config

    def cfg() -> PanelConfig:
        return config

    def require_csrf(token: str = Form(...), c: PanelConfig = Depends(cfg)) -> None:
        # `compare_digest` because this is a secret comparison, even locally.
        if not secrets.compare_digest(token, c.csrf_token):
            raise HTTPException(status_code=403, detail="bad or missing CSRF token")

    # ------------------------------------------------------------- read pages
    @app.get("/", response_class=HTMLResponse)
    def index(c: PanelConfig = Depends(cfg)) -> str:
        overview = readers.overview(c.session, c.stage)
        rows = "".join(
            f"<tr><td>{name}</td><td>{'on' if enabled else 'off'}</td></tr>"
            for name, enabled in sorted(overview.flags.items())
        )
        counters = "".join(
            f"<tr><td>{k}</td><td>{v:,}</td></tr>" for k, v in sorted(overview.counters.items())
        )
        status = (
            f"<p class='paused'>Paused by {overview.paused_by}: {overview.pause_reason}</p>"
            if overview.paused
            else "<p class='running'>Running.</p>"
        )
        return _page(
            c,
            "Overview",
            f"""
            {status}
            <h2>Last diary entry</h2>
            <p class="when">{overview.last_diary_at}</p>
            <pre>{_escape(overview.last_diary) or "nothing yet"}</pre>
            <h2>Flags</h2><table>{rows}</table>
            <h2>Today</h2><table>{counters or "<tr><td>nothing spent yet</td></tr>"}</table>
            <p>{overview.open_conversations} open conversation(s),
               {overview.recent_level_two} recent Level 2 decision(s).</p>
            """,
        )

    @app.get("/diary", response_class=HTMLResponse)
    def diary_page(c: PanelConfig = Depends(cfg)) -> str:
        entries = readers.diary(c.session, c.stage, limit=50)
        body = "".join(
            f"<article><h3>{e['sk']} <small>{e['mode']}"
            f"{' · auto' if e['auto'] else ''}</small></h3>"
            f"<pre>{_escape(e['content'])}</pre></article>"
            for e in entries
        )
        return _page(c, "Diary", body or "<p>No entries yet.</p>")

    @app.get("/journal", response_class=HTMLResponse)
    def journal_page(c: PanelConfig = Depends(cfg)) -> str:
        entries = readers.journal(c.session, c.stage, limit=100)
        rows = "".join(
            f"<tr class='level-{e['level']}'><td>{e['sk'][:23]}</td>"
            f"<td>{e['level']}</td><td>{_escape(e['event_type'])}</td>"
            f"<td>{_escape(e['summary'])}</td><td>{_escape(e['by'])}</td></tr>"
            for e in entries
        )
        return _page(
            c,
            "Supervisor journal",
            f"<table><tr><th>when</th><th>level</th><th>event</th>"
            f"<th>summary</th><th>by</th></tr>{rows}</table>",
        )

    @app.get("/sessions", response_class=HTMLResponse)
    def sessions_page(c: PanelConfig = Depends(cfg)) -> str:
        rows = "".join(
            f"<tr><td><a href='/sessions/{s['trace_id']}'>{s['trace_id'][:12]}</a></td>"
            f"<td>{s['kind']}</td><td>{s['calls']}</td>"
            f"<td>{s['input_tokens']:,}</td><td>{s['output_tokens']:,}</td>"
            f"<td>{s['cached_tokens']:,}</td></tr>"
            for s in readers.sessions(c.session, c.stage)
        )
        return _page(
            c,
            "Sessions",
            f"<table><tr><th>trace</th><th>kind</th><th>calls</th><th>in</th>"
            f"<th>out</th><th>cached</th></tr>{rows}</table>",
        )

    @app.get("/sessions/{trace_id}", response_class=HTMLResponse)
    def session_detail(trace_id: str, c: PanelConfig = Depends(cfg)) -> str:
        entry = next(
            (s for s in readers.sessions(c.session, c.stage) if s["trace_id"] == trace_id), None
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="no such session")
        records = readers.session_records(c.session, c.stage, entry["s3_prefix"])
        blocks = []
        for record in records:
            response = record.get("response") or {}
            reasoning = response.get("reasoning_content") or ""
            blocks.append(
                f"<article><h3>call {record['seq']} — {record['model']}</h3>"
                + (
                    f"<details><summary>reasoning ({len(reasoning)} chars)</summary>"
                    f"<pre class='reasoning'>{_escape(reasoning)}</pre></details>"
                    if reasoning
                    else ""
                )
                + f"<pre>{_escape(str(response.get('content') or ''))}</pre></article>"
            )
        return _page(c, f"Session {trace_id[:12]}", "".join(blocks))

    @app.get("/memory", response_class=HTMLResponse)
    def memory_page(
        # `?type=` stays in the URL; the Python name does not shadow the builtin.
        memory_type: str | None = Query(None, alias="type"),
        c: PanelConfig = Depends(cfg),
    ) -> str:
        rows = "".join(
            f"<tr class='{'tombstone' if m['tombstone'] else ''}'>"
            f"<td>{m['type']}</td><td>{m['importance']}</td><td>{m['access_count']}</td>"
            f"<td>{_escape(m['content'][:200])}</td></tr>"
            for m in readers.memories(c.session, c.stage, memory_type=memory_type)
        )
        return _page(
            c,
            "Memory",
            f"<table><tr><th>type</th><th>imp</th><th>reads</th>"
            f"<th>content</th></tr>{rows}</table>",
        )

    @app.get("/storage", response_class=HTMLResponse)
    def storage_page(c: PanelConfig = Depends(cfg)) -> str:
        sizes = readers.storage_sizes(c.session, c.stage)
        tables = "".join(
            f"<tr><td>{n}</td><td>{v['items']:,}</td><td>{v['bytes']:,}</td></tr>"
            for n, v in sizes["tables"].items()
        )
        prefixes = "".join(
            f"<tr><td>{n}</td><td>{v['objects']:,}</td><td>{v['bytes']:,}</td></tr>"
            for n, v in sizes["prefixes"].items()
        )
        return _page(
            c,
            "Storage",
            f"<h2>Tables</h2><table><tr><th>table</th><th>items</th><th>bytes</th></tr>"
            f"{tables}</table><h2>Bucket</h2>"
            f"<table><tr><th>prefix</th><th>objects</th><th>bytes</th></tr>{prefixes}</table>",
        )

    @app.get("/proposals", response_class=HTMLResponse)
    def proposals_page(c: PanelConfig = Depends(cfg)) -> str:
        rows = "".join(
            f"<tr><td>{p['created_at'][:19]}</td><td>{_escape(p['title'])}</td>"
            f"<td>{p['status']}</td>"
            f"<td>{('<a href=' + p['pr_url'] + '>PR</a>') if p['pr_url'] else ''}</td></tr>"
            for p in readers.proposals(c.session, c.stage)
        )
        return _page(
            c,
            "Proposals",
            f"<table><tr><th>when</th><th>title</th><th>status</th><th></th></tr>{rows}</table>",
        )

    # ------------------------------------------------------------- mutations
    @app.post("/actions/pause")
    def do_pause(
        reason: str = Form(...),
        _: None = Depends(require_csrf),
        c: PanelConfig = Depends(cfg),
    ) -> RedirectResponse:
        actions.pause(c.session, c.stage, reason=reason)
        return RedirectResponse("/", status_code=303)

    @app.post("/actions/resume")
    def do_resume(
        ack: str = Form(""),
        _: None = Depends(require_csrf),
        c: PanelConfig = Depends(cfg),
    ) -> RedirectResponse:
        try:
            actions.resume(c.session, c.stage, ack=ack or None)
        except StageRefusalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse("/", status_code=303)

    @app.post("/actions/flag")
    def do_flag(
        name: str = Form(...),
        enabled: str = Form(...),
        _: None = Depends(require_csrf),
        c: PanelConfig = Depends(cfg),
    ) -> RedirectResponse:
        try:
            actions.set_flag(c.session, c.stage, name, enabled == "true")
        except StageRefusalError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/", status_code=303)

    return app


def _page(config: PanelConfig, title: str, body: str) -> str:
    nav = "".join(
        f"<a href='{href}'>{label}</a>"
        for href, label in (
            ("/", "Overview"),
            ("/diary", "Diary"),
            ("/journal", "Journal"),
            ("/sessions", "Sessions"),
            ("/memory", "Memory"),
            ("/proposals", "Proposals"),
            ("/storage", "Storage"),
        )
    )
    return f"""<!doctype html>
<title>{title} — Juno ({config.stage.name})</title>
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 62rem;
         padding: 1rem; color: #1a1a1a; }}
 nav a {{ margin-right: 1rem; }}
 table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
 td, th {{ border-bottom: 1px solid #ddd; padding: .35rem .5rem;
           text-align: left; vertical-align: top; }}
 pre {{ white-space: pre-wrap; background: #f6f6f6; padding: .75rem; border-radius: 4px; }}
 .paused {{ color: #a00; font-weight: 600; }}
 .running {{ color: #060; }}
 .level-2 {{ background: #fee; }} .level-1 {{ background: #ffd; }}
 .tombstone {{ opacity: .5; text-decoration: line-through; }}
 .reasoning {{ background: #eef; font-size: 13px; }}
 .when {{ color: #666; font-size: 13px; }}
 article {{ margin: 1.5rem 0; }}
</style>
<nav>{nav}</nav>
<h1>{title}</h1>
{body}
<input type="hidden" id="csrf" value="{config.csrf_token}">
"""


def _escape(text: str) -> str:
    """The agent's own output is rendered here; it must never become markup."""
    import html

    return html.escape(text or "")
