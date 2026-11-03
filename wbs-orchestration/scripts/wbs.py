#!/usr/bin/env python3
"""Coordinate a WBS in SQLite and dispatch one ready task per run to a configured CLI.

Standard library only. See ../SKILL.md for the workflow and safety rules.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

WARN_EVERY = 5
NOTE_MAX = 500
EXIT_OK, EXIT_ERROR, EXIT_BLOCKED = 0, 1, 3
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TASK_KEYS = {"id", "title", "role", "description", "acceptance", "depends_on", "requires_approval"}
# A guard against obvious permission bypass, not a guarantee: review every configured argv.
BYPASS_MARKERS = ("dangerous", "bypass", "skip-permission", "yolo", "no-sandbox")

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    role TEXT NOT NULL,
    description TEXT NOT NULL,
    acceptance TEXT NOT NULL,
    requires_approval INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'awaiting_review', 'done', 'failed'))
);
CREATE TABLE deps (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    depends_on TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on)
);
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    worker TEXT NOT NULL,
    cli TEXT,
    cli_started INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT CHECK (outcome IN ('awaiting_review', 'done', 'failed')),
    exit_code INTEGER,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    task_id TEXT,
    attempt_id INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);
"""

READY_SQL = """
SELECT t.* FROM tasks t
WHERE t.status = 'pending' AND NOT EXISTS (
    SELECT 1 FROM deps d JOIN tasks p ON p.id = d.depends_on
    WHERE d.task_id = t.id AND p.status != 'done')
ORDER BY t.position
"""


class WbsError(Exception):
    """Invalid input or state. Reported as one line, without a traceback."""


class Blocked(Exception):
    """A failed task stops all claims until a person runs `retry`."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE takes the write lock before reading, so two claimers can
    never both see the same task as ready."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def add_event(conn, kind, task_id=None, attempt_id=None, detail=""):
    conn.execute(
        "INSERT INTO events (at, kind, task_id, attempt_id, detail) VALUES (?, ?, ?, ?, ?)",
        (now(), kind, task_id, attempt_id, detail),
    )


def emit(obj) -> None:
    print(json.dumps(obj, indent=2))


def load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise WbsError(f"cannot read JSON: {exc}") from exc


# ---- plan validation -------------------------------------------------------


def validate_plan(data):
    """Return (meta, tasks) from user-supplied plan JSON, or raise WbsError."""
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list) or not data["tasks"]:
        raise WbsError("plan must be an object with a non-empty 'tasks' list")
    plan = data.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("name"), str) or not plan["name"].strip():
        raise WbsError("plan.name must be a non-empty string")
    goal = plan.get("goal", "")
    if not isinstance(goal, str):
        raise WbsError("plan.goal must be a string")
    tasks, seen = [], set()
    for raw in data["tasks"]:
        if not isinstance(raw, dict):
            raise WbsError("each task must be an object")
        if set(raw) - TASK_KEYS:
            raise WbsError(f"task has unknown keys: {sorted(set(raw) - TASK_KEYS)}")
        tid = raw.get("id")
        if not isinstance(tid, str) or not ID_PATTERN.match(tid) or tid in seen:
            raise WbsError(f"invalid or duplicate task id: {tid!r}")
        seen.add(tid)
        role, title = raw.get("role"), raw.get("title")
        if not isinstance(role, str) or not ID_PATTERN.match(role):
            raise WbsError(f"task {tid}: invalid role {role!r}")
        if not isinstance(title, str) or not title.strip():
            raise WbsError(f"task {tid}: title is required")
        deps = raw.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps) or len(set(deps)) != len(deps):
            raise WbsError(f"task {tid}: depends_on must be a list of unique task ids")
        approval = raw.get("requires_approval", False)
        if not isinstance(approval, bool):
            raise WbsError(f"task {tid}: requires_approval must be true or false")
        texts = {key: raw.get(key, "") for key in ("description", "acceptance")}
        if not all(isinstance(v, str) for v in texts.values()):
            raise WbsError(f"task {tid}: description and acceptance must be strings")
        tasks.append({"id": tid, "title": title.strip(), "role": role, "depends_on": deps,
                      "requires_approval": approval, **texts})
    check_graph(tasks)
    return {"name": plan["name"].strip(), "goal": goal.strip()}, tasks


def check_graph(tasks):
    """Reject unknown dependencies and cycles (Kahn's algorithm)."""
    ids = {t["id"] for t in tasks}
    for t in tasks:
        unknown = [d for d in t["depends_on"] if d not in ids]
        if unknown:
            raise WbsError(f"task {t['id']}: unknown dependencies {unknown}")
    remaining = {t["id"]: set(t["depends_on"]) for t in tasks}
    while remaining:
        free = [tid for tid, deps in remaining.items() if not deps]
        if not free:
            raise WbsError(f"dependency cycle among tasks {sorted(remaining)}")
        for tid in free:
            del remaining[tid]
        for deps in remaining.values():
            deps.difference_update(free)


# ---- state transitions -----------------------------------------------------


def ready_tasks(conn):
    return conn.execute(READY_SQL).fetchall()


def idle_reason(conn) -> str:
    review = [r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE status = 'awaiting_review' ORDER BY position")]
    if review:
        return f"no ready task; awaiting review: {review}"
    open_count = conn.execute("SELECT COUNT(*) FROM tasks WHERE status != 'done'").fetchone()[0]
    return "all tasks done" if open_count == 0 else "no ready task"


def claim(conn, worker, cli=None, task_id=None, select=None):
    """Atomically claim one ready task. Returns (task_row, attempt_id) or (None, None).

    A CLI claim (cli is set) is refused while any CLI attempt in this database is
    still open, whatever its task: at most one CLI run per database. An attempt
    left open by a crash keeps blocking until a person closes it with `fail`, so
    an uncertain run is never retried automatically. Manual claims are not limited."""
    with transaction(conn):
        failed = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE status = 'failed' ORDER BY position")]
        if failed:
            raise Blocked(f"failed task(s) {failed}: inspect, fix, then `retry --task ID`")
        if cli is not None:
            open_cli = [(r["id"], r["task_id"]) for r in conn.execute(
                "SELECT id, task_id FROM attempts WHERE cli IS NOT NULL AND ended_at IS NULL ORDER BY id")]
            if open_cli:
                raise Blocked(f"open CLI attempt(s) {open_cli} (attempt, task): one CLI run per database at a time;"
                              " if that run is gone, inspect its work, then `fail --attempt ID`")
        ready = ready_tasks(conn)
        if task_id is not None:
            ready = [r for r in ready if r["id"] == task_id]
            if not ready:
                raise WbsError(f"task {task_id} is not ready (unknown, not pending, or prerequisites not done)")
        if select is not None:
            ready = [r for r in ready if select(r)]
        if not ready:
            return None, None
        task = ready[0]
        conn.execute("UPDATE tasks SET status = 'claimed' WHERE id = ?", (task["id"],))
        cur = conn.execute(
            "INSERT INTO attempts (task_id, worker, cli, started_at) VALUES (?, ?, ?, ?)",
            (task["id"], worker, cli, now()),
        )
        add_event(conn, "claim", task["id"], cur.lastrowid, f"worker={worker}")
        return task, cur.lastrowid


def finish(conn, attempt_id, outcome, note="", exit_code=None):
    """Close an open attempt and set its task to the outcome.

    A CLI attempt can only end as 'awaiting_review' or 'failed'; 'done' for it
    comes from an explicit `accept`."""
    note = note[:NOTE_MAX]
    with transaction(conn):
        row = conn.execute("SELECT task_id, cli, ended_at FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None or row["ended_at"] is not None:
            raise WbsError(f"attempt {attempt_id} is unknown or already finished")
        if outcome == "done" and row["cli"] is not None:
            raise WbsError(f"attempt {attempt_id} ran on a CLI; a reviewer must `accept` or `reject` the task")
        conn.execute(
            "UPDATE attempts SET ended_at = ?, outcome = ?, exit_code = ?, note = ? WHERE id = ?",
            (now(), outcome, exit_code, note, attempt_id),
        )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (outcome, row["task_id"]))
        add_event(conn, outcome, row["task_id"], attempt_id, note)
    return row["task_id"]


def mark_started(conn, attempt_id) -> None:
    """Record that the CLI process for this attempt really started (autocommit)."""
    conn.execute("UPDATE attempts SET cli_started = 1 WHERE id = ?", (attempt_id,))


def task_cli_calls(conn, task_id) -> int:
    """Number of CLI processes actually started for this task. Only one attempt
    per task can be open, so the count is stable under parallel runs."""
    return conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE task_id = ? AND cli_started = 1", (task_id,)
    ).fetchone()[0]


def review(conn, task_id, reviewer, verdict, note):
    """Apply a reviewer's explicit verdict to a task awaiting review.

    'accept' marks it done and releases its dependents; 'reject' marks it failed,
    which blocks scheduling until a person runs `retry`."""
    status = "done" if verdict == "accept" else "failed"
    with transaction(conn):
        changed = conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ? AND status = 'awaiting_review'", (status, task_id)).rowcount
        if changed != 1:
            raise WbsError(f"task {task_id} is not awaiting review")
        add_event(conn, verdict, task_id, detail=f"reviewer={reviewer}: {note}"[:NOTE_MAX])
    return status


# ---- CLI dispatch ----------------------------------------------------------


def load_cli(config_path: str, name: str) -> dict:
    """Validate the config and return the settings of the one CLI named on this run."""
    cfg = load_json(config_path)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("clis"), dict) or not isinstance(cfg.get("roles"), dict):
        raise WbsError("config needs 'clis' and 'roles' objects")
    entry = cfg["clis"].get(name)
    if not isinstance(entry, dict):
        raise WbsError(f"cli {name!r} is not in the config; only configured CLIs can run")
    argv = entry.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise WbsError(f"cli {name!r}: argv must be a non-empty list of strings")
    bypass = [a for a in argv if any(m in a.lower() for m in BYPASS_MARKERS)]
    if bypass:
        raise WbsError(f"cli {name!r}: refusing permission-bypass arguments {bypass}")
    timeout = entry.get("timeout_seconds", 1800)
    if not isinstance(timeout, int) or timeout <= 0:
        raise WbsError(f"cli {name!r}: timeout_seconds must be a positive integer")
    roles = {role for role, assigned in cfg["roles"].items() if assigned == name}
    if not roles:
        raise WbsError(f"cli {name!r}: no role is assigned to it in 'roles'")
    warning_dir = cfg.get("warning_dir")
    if not isinstance(warning_dir, str) or not warning_dir:
        raise WbsError("config needs 'warning_dir', a folder chosen by the user")
    return {"argv": argv, "timeout": timeout, "roles": roles,
            "warning_dir": Path(config_path).resolve().parent / warning_dir}


def build_prompt(conn, task) -> str:
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    deps = [r[0] for r in conn.execute(
        "SELECT depends_on FROM deps WHERE task_id = ? ORDER BY depends_on", (task["id"],))]
    lines = [
        "You are assigned exactly one task from a work breakdown structure.",
        f"Plan: {meta['name']}",
        f"Goal: {meta['goal'] or '(not stated)'}",
        f"Task {task['id']}: {task['title']}",
        f"Role: {task['role']}",
        f"Completed prerequisites: {', '.join(deps) or 'none'}",
        "",
        "Description:",
        task["description"] or "(none)",
        "",
        "Acceptance criteria:",
        task["acceptance"] or "(none)",
        "",
        "Rules:",
        "- Follow the AGENTS.md and other instructions of the project you work in.",
        "- Do not bypass permission or approval prompts. If an action needs user approval, stop and say so.",
        "- Do not print, store or send secrets or personal data.",
        "- Work only on this task. Report what you changed and how you verified it.",
    ]
    return "\n".join(lines) + "\n"


def invoke(argv, prompt, timeout, on_start):
    """Start the CLI without a shell, pass the prompt on stdin and discard its output.

    CLI stdout and stderr go to the null device: they are never printed or stored.
    on_start runs once the process exists, so only real starts are counted.
    Returns (started, exit_code or None, fixed note without output). There is no
    retry here: a failure or timeout is final for this run."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, encoding="utf-8")
    except OSError as exc:
        return False, None, f"cli could not start ({type(exc).__name__})"
    on_start()
    try:
        proc.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return True, None, f"timeout after {timeout}s"
    return True, proc.returncode, f"exit code {proc.returncode}"


def write_warning(folder: Path, task_id: str, calls: int, db_name: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"wbs-warning-{stamp}-{task_id}-call{calls:04d}.txt"
    with open(path, "x", encoding="utf-8") as f:
        f.write(
            "WBS orchestration: CLI call warning\n"
            f"database: {db_name}\n"
            f"task: {task_id}\n"
            f"task_cli_calls: {calls}\n"
            f"time_utc: {now()}\n"
            f"This file is written at every {WARN_EVERY}th CLI start of the same task. Check progress,\n"
            "cost and quota before letting the schedule continue. No prompts or CLI output are recorded here.\n"
        )
    print(f"WARNING: task {task_id} has started a CLI {calls} times; see {path}", file=sys.stderr)
    return str(path)


# ---- commands --------------------------------------------------------------


def cmd_init(args) -> int:
    meta, tasks = validate_plan(load_json(args.plan))
    if Path(args.db).exists():
        raise WbsError("database already exists; existing state is never overwritten")
    with closing(connect(args.db)) as conn, transaction(conn):
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta.items())
        conn.executemany(
            "INSERT INTO tasks (id, position, title, role, description, acceptance,"
            " requires_approval, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            [(t["id"], pos, t["title"], t["role"], t["description"], t["acceptance"],
              int(t["requires_approval"])) for pos, t in enumerate(tasks)],
        )
        conn.executemany(
            "INSERT INTO deps (task_id, depends_on) VALUES (?, ?)",
            [(t["id"], d) for t in tasks for d in t["depends_on"]],
        )
        add_event(conn, "init", detail=f"{len(tasks)} tasks")
    emit({"result": "initialized", "tasks": len(tasks)})
    return EXIT_OK


def cmd_status(conn, args) -> int:
    tasks = [dict(r) for r in conn.execute(
        "SELECT t.id, t.title, t.role, t.status, t.requires_approval,"
        " (SELECT COUNT(*) FROM attempts a WHERE a.task_id = t.id AND a.cli_started = 1) AS cli_calls"
        " FROM tasks t ORDER BY t.position")]
    emit({
        "plan": conn.execute("SELECT value FROM meta WHERE key = 'name'").fetchone()[0],
        "counts": dict(Counter(t["status"] for t in tasks)),
        "cli_invocations": sum(t["cli_calls"] for t in tasks),
        "ready": [r["id"] for r in ready_tasks(conn)],
        "tasks": tasks,
    })
    return EXIT_OK


def cmd_ready(conn, args) -> int:
    emit({"ready": [{"id": r["id"], "title": r["title"], "role": r["role"],
                     "requires_approval": bool(r["requires_approval"])} for r in ready_tasks(conn)]})
    return EXIT_OK


def cmd_claim(conn, args) -> int:
    task, attempt = claim(conn, args.worker, task_id=args.task)
    if task is None:
        emit({"result": "idle", "reason": idle_reason(conn)})
    else:
        emit({"result": "claimed", "task": task["id"], "attempt": attempt})
    return EXIT_OK


def cmd_complete(conn, args) -> int:
    emit({"result": "done", "task": finish(conn, args.attempt, "done", args.note)})
    return EXIT_OK


def cmd_fail(conn, args) -> int:
    emit({"result": "failed", "task": finish(conn, args.attempt, "failed", args.reason)})
    return EXIT_OK


def cmd_accept(conn, args) -> int:
    status = review(conn, args.task, args.reviewer, "accept", args.note)
    emit({"result": status, "task": args.task, "reviewer": args.reviewer})
    return EXIT_OK


def cmd_reject(conn, args) -> int:
    status = review(conn, args.task, args.reviewer, "reject", args.reason)
    emit({"result": status, "task": args.task, "reviewer": args.reviewer})
    return EXIT_OK


def cmd_retry(conn, args) -> int:
    with transaction(conn):
        changed = conn.execute(
            "UPDATE tasks SET status = 'pending' WHERE id = ? AND status = 'failed'", (args.task,)).rowcount
        if changed != 1:
            raise WbsError(f"task {args.task} is not in the failed state")
        add_event(conn, "retry", args.task)
    emit({"result": "pending", "task": args.task})
    return EXIT_OK


def cmd_run(conn, args) -> int:
    cli = load_cli(args.config, args.cli)
    task, attempt = claim(conn, f"cli:{args.cli}", cli=args.cli,
                          select=lambda r: r["role"] in cli["roles"] and not r["requires_approval"])
    if task is None:
        ready = [r["id"] for r in ready_tasks(conn)]
        reason = f"ready tasks need user approval or another cli: {ready}" if ready else idle_reason(conn)
        emit({"result": "idle", "reason": reason, "cli_invoked": False})
        return EXIT_OK
    started, exit_code, note = invoke(cli["argv"], build_prompt(conn, task), cli["timeout"],
                                      on_start=lambda: mark_started(conn, attempt))
    # Exit code 0 only means the CLI says it finished; a reviewer decides with accept/reject.
    outcome = "awaiting_review" if exit_code == 0 else "failed"
    finish(conn, attempt, outcome, note, exit_code)
    calls = task_cli_calls(conn, task["id"])
    warning = None
    if started and calls % WARN_EVERY == 0:
        warning = write_warning(cli["warning_dir"], task["id"], calls, Path(args.db).name)
    emit({"result": outcome, "task": task["id"], "attempt": attempt, "exit_code": exit_code,
          "cli_invoked": started, "task_cli_calls": calls, "warning_file": warning})
    return EXIT_OK if outcome == "awaiting_review" else EXIT_BLOCKED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WBS coordination in SQLite with one-task-per-run CLI dispatch.")
    parser.add_argument("--db", required=True, help="SQLite state file")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="create the database from a JSON plan")
    p.add_argument("--plan", required=True)
    p.set_defaults(func=cmd_init)
    sub.add_parser("status", help="summary and task list").set_defaults(func=cmd_status)
    sub.add_parser("ready", help="tasks whose prerequisites are done").set_defaults(func=cmd_ready)
    p = sub.add_parser("claim", help="claim one ready task for a person or agent")
    p.add_argument("--worker", required=True)
    p.add_argument("--task")
    p.set_defaults(func=cmd_claim)
    p = sub.add_parser("complete", help="mark a person's claimed attempt done (not for CLI attempts)")
    p.add_argument("--attempt", type=int, required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_complete)
    p = sub.add_parser("fail", help="mark a claimed attempt failed; stops scheduling")
    p.add_argument("--attempt", type=int, required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_fail)
    p = sub.add_parser("accept", help="reviewer accepts a task awaiting review; releases dependents")
    p.add_argument("--task", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_accept)
    p = sub.add_parser("reject", help="reviewer rejects a task awaiting review; stops scheduling")
    p.add_argument("--task", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_reject)
    p = sub.add_parser("retry", help="return a failed task to pending")
    p.add_argument("--task", required=True)
    p.set_defaults(func=cmd_retry)
    p = sub.add_parser("run", help="dispatch one ready task to the CLI named by --cli")
    p.add_argument("--config", required=True, help="JSON config with clis, roles and warning_dir")
    p.add_argument("--cli", required=True, help="the one configured CLI authorized for this run")
    p.set_defaults(func=cmd_run)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            return cmd_init(args)
        if not Path(args.db).is_file():
            raise WbsError("database not found; run init first")
        with closing(connect(args.db)) as conn:
            return args.func(conn, args)
    except Blocked as exc:
        emit({"result": "blocked", "reason": str(exc), "cli_invoked": False})
        return EXIT_BLOCKED
    except WbsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
