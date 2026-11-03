#!/usr/bin/env python3
"""Render a read-only HTML status page from a wbs.py SQLite database.

Standard library only. See ../SKILL.md. The page shows task ids, titles, roles,
statuses, dependency ids and progress counters. It never reads events, attempt
notes, descriptions or acceptance criteria, and never writes to the database.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from wbs import READY_SQL

EXIT_OK, EXIT_ERROR = 0, 1
STATUSES = ("pending", "claimed", "awaiting_review", "done", "failed")
STYLE = (
    "body{font-family:system-ui,sans-serif;margin:1.5rem;color:#1b1b1b;background:#fff}"
    "table{border-collapse:collapse;margin:.5rem 0 1.5rem}"
    "th,td{border:1px solid #c8c8c8;padding:.3rem .6rem;text-align:left;vertical-align:top}"
    "thead th{background:#f0f0f0}td.num{text-align:right}progress{width:16rem}"
    "tr.s-done td{color:#1d5e2f}tr.s-awaiting_review td{color:#7a4b00}"
    "tr.s-failed td{color:#9b1c1c;font-weight:600}"
)


class RenderError(Exception):
    """Invalid input. Reported as one line, without a traceback."""


def read_snapshot(db_path: str) -> dict:
    """Read everything the page shows in one read-only transaction."""
    path = Path(db_path)
    if not path.is_file():
        raise RenderError("database not found")
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")  # one consistent view even while a `run` writes
        plan = conn.execute("SELECT value FROM meta WHERE key = 'name'").fetchone()[0]
        tasks = [dict(r) for r in conn.execute(
            "SELECT t.id, t.title, t.role, t.status, t.requires_approval,"
            " (SELECT COUNT(*) FROM attempts a WHERE a.task_id = t.id AND a.cli_started = 1) AS cli_calls"
            " FROM tasks t ORDER BY t.position")]
        deps = {}
        for r in conn.execute("SELECT task_id, depends_on FROM deps ORDER BY depends_on"):
            deps.setdefault(r["task_id"], []).append(r["depends_on"])
        ready = [r["id"] for r in conn.execute(READY_SQL)]
        conn.execute("COMMIT")
    for t in tasks:
        t["depends_on"] = deps.get(t["id"], [])
    return {"plan": plan, "tasks": tasks, "ready": ready,
            "counts": Counter(t["status"] for t in tasks),
            "cli_invocations": sum(t["cli_calls"] for t in tasks)}


def render_html(snap: dict, generated_at: str) -> str:
    """Every value from the database passes through html.escape."""
    e = html.escape
    total, done = len(snap["tasks"]), snap["counts"]["done"]
    plan = e(snap["plan"])
    ready = e(", ".join(snap["ready"])) or "none"
    cli_total = snap["cli_invocations"]
    counts = "\n".join(f'<tr><th scope="row">{s}</th><td class="num">{snap["counts"][s]}</td></tr>'
                       for s in STATUSES)
    rows = "\n".join(
        f'<tr data-task="{e(t["id"])}" class="s-{e(t["status"])}"><td>{e(t["id"])}</td>'
        f'<td>{e(t["title"])}</td><td>{e(t["role"])}</td><td>{e(t["status"])}</td>'
        f'<td>{e(", ".join(t["depends_on"])) or "-"}</td>'
        f'<td>{"yes" if t["requires_approval"] else "no"}</td><td class="num">{t["cli_calls"]}</td></tr>'
        for t in snap["tasks"])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WBS status: {plan}</title>
<style>{STYLE}</style>
</head>
<body>
<h1>WBS status: {plan}</h1>
<p>Snapshot taken {e(generated_at)} (UTC). This page does not refresh itself.</p>
<h2>Progress</h2>
<p><progress value="{done}" max="{total}"></progress> Done {done} of {total} tasks</p>
<table>
<thead><tr><th scope="col">Status</th><th scope="col">Tasks</th></tr></thead>
<tbody>
{counts}
</tbody>
</table>
<p>Ready: {ready}</p>
<p>CLI starts (all tasks): {cli_total}</p>
<h2>Tasks</h2>
<table>
<thead><tr><th scope="col">ID</th><th scope="col">Title</th><th scope="col">Role</th><th scope="col">Status</th>
<th scope="col">Depends on</th><th scope="col">Needs approval</th><th scope="col">CLI starts</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>
"""


def write_atomic(dest: Path, text: str) -> None:
    """Write to a temporary file beside dest, then rename it over dest."""
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except BaseException:
        os.unlink(tmp)  # leave no partial file behind, then report the original error
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render a read-only HTML status page from a WBS database.")
    parser.add_argument("--db", required=True, help="SQLite state file created by wbs.py init")
    parser.add_argument("--output", required=True, help="HTML file to write; its folder must already exist")
    args = parser.parse_args(argv)
    output = Path(args.output)
    try:
        if not output.parent.is_dir():
            raise RenderError("output folder does not exist")
        snap = read_snapshot(args.db)
        if output.exists() and os.path.samefile(output, args.db):
            raise RenderError("output path is the database file")
        write_atomic(output, render_html(snap, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (sqlite3.Error, OSError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps({"result": "rendered", "output": str(output), "tasks": len(snap["tasks"])}, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
