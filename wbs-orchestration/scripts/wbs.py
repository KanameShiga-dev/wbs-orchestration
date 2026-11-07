#!/usr/bin/env python3
"""Coordinate a WBS in SQLite and dispatch one ready task per run to a configured CLI.

Standard library only. See ../SKILL.md for the workflow and safety rules.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing, contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

WARN_EVERY = 5
NOTE_MAX = 500
EXIT_OK, EXIT_ERROR, EXIT_BLOCKED = 0, 1, 3
MAX_PARALLEL_PER_CLI = 3
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TASK_KEYS = {"id", "title", "role", "description", "acceptance", "depends_on", "requires_approval", "writes",
             "checkpoint"}
WILDCARDS = set("*?[")
# A guard against obvious permission bypass, not a guarantee: review every configured argv.
# "danger" also covers values such as "danger-full-access", not only "--dangerously-...".
BYPASS_MARKERS = ("danger", "bypass", "skip-permission", "yolo", "no-sandbox", "approve-for-me")
# Permission policy A: a CLI may write inside the project without asking; anything else the
# CLI itself must refuse or stop on. These permission modes approve more than that.
PERMISSION_MODE_FLAG = re.compile(r"^--permission-mode$")
BROAD_PERMISSION_MODES = {"auto", "bypasspermissions"}
# Flags whose value "never" turns approval prompts off ("-a never", "--ask-for-approval=never").
# That is only allowed together with an explicit sandbox that confines the CLI to the project.
APPROVAL_FLAG = re.compile(r"^(-a|--[a-z-]*approval[a-z-]*)$")
SANDBOX_FLAG = re.compile(r"^(-s|--sandbox)$")
CONFINED_SANDBOXES = {"read-only", "workspace-write"}
# A script as argv[0] can add flags that this check never sees.
WRAPPER_SUFFIXES = (".cmd", ".bat", ".ps1", ".sh", ".vbs")
REPORT_STATUS = re.compile(r"^STATUS:\s*(done|needs-approval|blocked)\s*$", re.IGNORECASE)
REVIEW_VERDICT = re.compile(r"^VERDICT:\s*(accept|reject|needs-human)\s*$", re.IGNORECASE)

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
    writes TEXT,
    checkpoint INTEGER NOT NULL DEFAULT 0,
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
    runner_pid INTEGER,
    runner_start TEXT,
    parallelism INTEGER,
    profile_args TEXT,
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

OPEN_CLI_SQL = """
SELECT a.id, a.task_id, a.cli, a.runner_pid, a.runner_start, t.writes
FROM attempts a JOIN tasks t ON t.id = a.task_id
WHERE a.cli IS NOT NULL AND a.ended_at IS NULL
ORDER BY a.id
"""


class WbsError(Exception):
    """Invalid input or state. Reported as one line, without a traceback."""


class Blocked(Exception):
    """A person must act: a task failed, or an open CLI attempt cannot be trusted."""


class Busy(Exception):
    """A parallel limit or a write overlap makes this run wait. Nothing is wrong."""


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
        approval, checkpoint = raw.get("requires_approval", False), raw.get("checkpoint", False)
        if not isinstance(approval, bool) or not isinstance(checkpoint, bool):
            raise WbsError(f"task {tid}: requires_approval and checkpoint must be true or false")
        texts = {key: raw.get(key, "") for key in ("description", "acceptance")}
        if not all(isinstance(v, str) for v in texts.values()):
            raise WbsError(f"task {tid}: description and acceptance must be strings")
        tasks.append({"id": tid, "title": title.strip(), "role": role, "depends_on": deps,
                      "requires_approval": approval, "checkpoint": checkpoint,
                      "writes": validate_writes(tid, raw), **texts})
    check_graph(tasks)
    return {"name": plan["name"].strip(), "goal": goal.strip()}, tasks


def validate_writes(tid, raw):
    """Return the task's write patterns with forward slashes, or None when the key is absent.

    None and [] differ: None means the writes are unknown, so the task runs alone;
    [] means the task writes no project file."""
    if "writes" not in raw:
        return None
    writes = raw["writes"]
    if not isinstance(writes, list) or not all(isinstance(p, str) and p.strip() for p in writes):
        raise WbsError(f"task {tid}: writes must be a list of non-empty paths or globs")
    normalized = [p.strip().replace("\\", "/") for p in writes]
    for pattern in normalized:
        path = PureWindowsPath(pattern)
        if path.drive or path.root or ".." in path.parts:
            raise WbsError(f"task {tid}: writes entry {pattern!r} must stay inside the working directory")
    return normalized


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


def write_scope(pattern) -> tuple:
    """The folders and file a write pattern is certain to stay under, compared without case."""
    scope = []
    for part in PurePosixPath(pattern).parts:
        if WILDCARDS & set(part):
            break
        scope.append(part.casefold())
    return tuple(scope)


def writes_overlap(first, second) -> bool:
    """Whether two write lists may touch the same file. None (unknown writes) overlaps everything.

    Globs are compared by the part before the first wildcard, so a doubtful pair counts as overlapping."""
    if first is None or second is None:
        return True
    scopes = [write_scope(p) for p in second]
    for scope in map(write_scope, first):
        for other in scopes:
            shared = min(len(scope), len(other))
            if scope[:shared] == other[:shared]:
                return True
    return False


def stored_json(value):
    return None if value is None else json.loads(value)


def profile_args_for(profiles, parallelism) -> list:
    """Arguments of the profile for this parallelism, or of the nearest lower one; none if there is none."""
    usable = [int(key) for key in profiles if int(key) <= parallelism]
    return profiles[str(max(usable))]["args"] if usable else []


def attempt_running(attempt) -> bool:
    """Whether the `wbs.py run` that opened this attempt still exists. Unknown counts as not running."""
    return (attempt["runner_start"] is not None
            and process_start(attempt["runner_pid"]) == attempt["runner_start"])


def check_parallel_limits(open_cli, cli, limits) -> None:
    """Raise Blocked for an interrupted attempt, Busy when a limit is reached."""
    interrupted = [(a["id"], a["task_id"]) for a in open_cli if not attempt_running(a)]
    if interrupted:
        raise Blocked(f"open CLI attempt(s) {interrupted} (attempt, task) are no longer running:"
                      " inspect their work, then `fail --attempt ID`")
    running = [(a["id"], a["task_id"], a["cli"]) for a in open_cli]
    if len(running) >= limits["total"]:
        raise Busy(f"database limit reached ({len(running)} of {limits['total']});"
                   f" open attempts (attempt, task, cli): {running}")
    own = [a for a in running if a[2] == cli]
    if len(own) >= limits["cli"]:
        raise Busy(f"limit of cli {cli!r} reached ({len(own)} of {limits['cli']});"
                   f" open attempts (attempt, task, cli): {running}")


def claim(conn, worker, cli=None, task_id=None, select=None, limits=None, profiles=None):
    """Atomically claim one ready task. Returns (task_row, attempt_id) or (None, None).

    A CLI claim (cli is set) with no limits is refused while any CLI attempt in
    this database is still open, whatever its task. With limits, it may start
    next to running attempts, inside the limits and without a write overlap. An
    attempt left open by a crash keeps blocking until a person closes it with
    `fail`, so an uncertain run is never retried automatically. Manual claims are
    not limited. A CLI attempt records its parallelism (open attempts of this CLI,
    including itself) and the profile arguments that go with it."""
    runner_pid = os.getpid() if cli is not None else None
    runner_start = process_start(runner_pid) if cli is not None else None
    with transaction(conn):
        failed = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE status = 'failed' ORDER BY position")]
        if failed:
            raise Blocked(f"failed task(s) {failed}: inspect, fix, then `retry --task ID`")
        open_cli = conn.execute(OPEN_CLI_SQL).fetchall() if cli is not None else []
        if open_cli and limits is None:
            shown = [(a["id"], a["task_id"]) for a in open_cli]
            raise Blocked(f"open CLI attempt(s) {shown} (attempt, task): one CLI run per database at a time;"
                          " if that run is gone, inspect its work, then `fail --attempt ID`")
        if limits is not None:
            check_parallel_limits(open_cli, cli, limits)
        ready = ready_tasks(conn)
        if task_id is not None:
            ready = [r for r in ready if r["id"] == task_id]
            if not ready:
                raise WbsError(f"task {task_id} is not ready (unknown, not pending, or prerequisites not done)")
        if select is not None:
            ready = [r for r in ready if select(r)]
        if not ready:
            return None, None
        if limits is not None and open_cli:
            free = [r for r in ready if not any(
                writes_overlap(stored_json(r["writes"]), stored_json(a["writes"])) for a in open_cli)]
            if not free:
                raise Busy(f"ready task(s) {[r['id'] for r in ready]} may write what running task(s)"
                           f" {[a['task_id'] for a in open_cli]} write")
            ready = free
        task = ready[0]
        parallelism, profile_args = None, None
        if cli is not None:
            parallelism = sum(a["cli"] == cli for a in open_cli) + 1
            profile_args = json.dumps(profile_args_for(profiles or {}, parallelism))
        conn.execute("UPDATE tasks SET status = 'claimed' WHERE id = ?", (task["id"],))
        cur = conn.execute(
            "INSERT INTO attempts (task_id, worker, cli, runner_pid, runner_start, parallelism, profile_args,"
            " started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task["id"], worker, cli, runner_pid, runner_start, parallelism, profile_args, now()),
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


def load_config(config_path: str) -> dict:
    cfg = load_json(config_path)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("clis"), dict) or not isinstance(cfg.get("roles"), dict):
        raise WbsError("config needs 'clis' and 'roles' objects")
    return cfg


def check_entry(cfg: dict, name: str) -> dict:
    """Validate one CLI entry, for a worker or a reviewer, and return how to start it."""
    entry = cfg["clis"].get(name)
    if not isinstance(entry, dict):
        raise WbsError(f"cli {name!r} is not in the config; only configured CLIs can run")
    argv = entry.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise WbsError(f"cli {name!r}: argv must be a non-empty list of strings")
    bypass = bypass_arguments(argv)
    if bypass:
        raise WbsError(f"cli {name!r}: refusing permission-bypass arguments {bypass}")
    if Path(argv[0]).suffix.lower() in WRAPPER_SUFFIXES and entry.get("wrapper_reviewed") is not True:
        raise WbsError(f"cli {name!r}: argv[0] is a script whose own arguments are not checked;"
                       " review it with the user, then set \"wrapper_reviewed\": true")
    timeout = entry.get("timeout_seconds", 1800)
    if not isinstance(timeout, int) or timeout <= 0:
        raise WbsError(f"cli {name!r}: timeout_seconds must be a positive integer")
    return {"argv": argv, "timeout": timeout, "profiles": parallel_profiles(entry, argv, name)}


def load_cli(config_path: str, name: str) -> dict:
    """Validate the config and return the settings of the one CLI named on this run."""
    cfg = load_config(config_path)
    settings = check_entry(cfg, name)
    roles = {role for role, assigned in cfg["roles"].items() if assigned == name}
    if not roles:
        raise WbsError(f"cli {name!r}: no role is assigned to it in 'roles'")
    warning_dir = cfg.get("warning_dir")
    if not isinstance(warning_dir, str) or not warning_dir:
        raise WbsError("config needs 'warning_dir', a folder chosen by the user")
    report_dir = cfg.get("report_dir")
    if report_dir is not None and (not isinstance(report_dir, str) or not report_dir):
        raise WbsError("'report_dir' must be a non-empty string when given")
    return {**settings, "roles": roles,
            "limits": parallel_limits(cfg, cfg["clis"][name], name),
            "warning_dir": Path(config_path).resolve().parent / warning_dir,
            # Relative to the project (the working directory of `run`), so the CLI may write there.
            "report_dir": Path(report_dir).resolve() if report_dir else None}


def parallel_limits(cfg, entry, name):
    """{'total': n, 'cli': n}, or None when the config sets neither key: one CLI attempt at a time."""
    if "max_parallel" not in cfg and "max_parallel" not in entry:
        return None
    total, own = cfg.get("max_parallel", 1), entry.get("max_parallel", 1)
    if type(total) is not int or total < 1:
        raise WbsError("max_parallel must be a positive integer")
    if type(own) is not int or not 1 <= own <= MAX_PARALLEL_PER_CLI:
        raise WbsError(f"cli {name!r}: max_parallel must be an integer from 1 to {MAX_PARALLEL_PER_CLI}")
    return {"total": total, "cli": own}


def parallel_profiles(entry, argv, name) -> dict:
    """Validate parallel_profiles. Each profile is checked joined to argv, because the
    permission checks depend on combinations such as an approval flag and a sandbox."""
    profiles = entry.get("parallel_profiles", {})
    if not isinstance(profiles, dict):
        raise WbsError(f"cli {name!r}: parallel_profiles must be an object")
    allowed_keys = {str(n) for n in range(1, MAX_PARALLEL_PER_CLI + 1)}
    for key, profile in profiles.items():
        if key not in allowed_keys:
            raise WbsError(f"cli {name!r}: parallel_profiles key {key!r} must be one of {sorted(allowed_keys)}")
        args = profile.get("args") if isinstance(profile, dict) and set(profile) == {"args"} else None
        if not isinstance(args, list) or not args or not all(isinstance(a, str) and a for a in args):
            raise WbsError(f"cli {name!r}: parallel_profiles[{key!r}] must be {{\"args\": [non-empty strings]}}")
        bypass = bypass_arguments(argv + args)
        if bypass:
            raise WbsError(f"cli {name!r}: refusing permission-bypass arguments {bypass} with profile {key!r}")
    return profiles


def load_progress(config_path: str, conn) -> tuple:
    """Validate the config of `advance`. Returns (worker CLIs, reviewer names, reviewer CLIs, progress)."""
    cfg = load_config(config_path)
    progress = cfg.get("progress", {})
    mode = progress.get("mode", "manual") if isinstance(progress, dict) else None
    if mode not in ("checkpoint", "auto"):
        raise WbsError("advance needs progress.mode set to 'checkpoint' or 'auto' in the config")
    if unknown := set(progress) - {"mode", "reviewers", "checkpoints", "ignore_dirs"}:
        raise WbsError(f"progress has unknown keys: {sorted(unknown)}")
    if not all(isinstance(name, str) for name in cfg["roles"].values()):
        raise WbsError("each value in 'roles' must be a CLI name")
    clis = {name: load_cli(config_path, name) for name in dict.fromkeys(cfg["roles"].values())}
    if not clis:
        raise WbsError("advance needs at least one role mapped to a CLI")
    if any(cli["report_dir"] is None for cli in clis.values()):
        raise WbsError("advance needs 'report_dir': the reviewer writes its verdict there")
    reviewers = progress.get("reviewers")
    if not isinstance(reviewers, dict) or not all(isinstance(r, str) for r in reviewers.values()):
        raise WbsError("progress.reviewers must map each worker CLI to a reviewer CLI")
    for name in clis:
        reviewer = reviewers.get(name)
        if not isinstance(reviewer, str) or reviewer == name:
            raise WbsError(f"progress.reviewers: cli {name!r} needs a reviewer that is a different CLI")
    ignore_dirs = progress.get("ignore_dirs", [])
    if not isinstance(ignore_dirs, list) or not all(isinstance(d, str) and d for d in ignore_dirs):
        raise WbsError("progress.ignore_dirs must be a list of folder names")
    return (clis, reviewers, {name: check_entry(cfg, name) for name in set(reviewers.values())},
            {"mode": mode, "ignore_dirs": set(ignore_dirs), **load_checkpoints(progress, mode, conn)})


def load_checkpoints(progress, mode, conn) -> dict:
    """Validate the checkpoints. A wrong name would silently hand a person's review to an agent, so it is an error."""
    points = progress.get("checkpoints", {})
    if mode == "auto" and points:
        raise WbsError("progress.checkpoints needs mode 'checkpoint'; mode 'auto' has none")
    if not isinstance(points, dict) or set(points) - {"tasks", "roles", "every"}:
        raise WbsError("progress.checkpoints may only have 'tasks', 'roles' and 'every'")
    tasks, roles, every = points.get("tasks", []), points.get("roles", []), points.get("every")
    rows = conn.execute("SELECT id, role, checkpoint FROM tasks").fetchall()
    for label, names, known in (("tasks", tasks, {r["id"] for r in rows}), ("roles", roles, {r["role"] for r in rows})):
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names) or set(names) - known:
            raise WbsError(f"progress.checkpoints.{label} must list names from the plan")
    if every is not None and (type(every) is not int or every < 1):
        raise WbsError("progress.checkpoints.every must be a positive integer")
    if mode == "checkpoint" and not (tasks or roles or every or any(r["checkpoint"] for r in rows)):
        raise WbsError("mode 'checkpoint' needs at least one checkpoint in the config or the plan")
    return {"tasks": set(tasks), "roles": set(roles), "every": every}


def flag_values(argv, flag_pattern) -> list:
    """(argument as written, lower-case value) for each use of a flag, as --flag=value or --flag value."""
    found = []
    for i, arg in enumerate(argv):
        flag, eq, value = arg.partition("=")
        if not flag_pattern.match(flag.lower()):
            continue
        if not eq:
            value = argv[i + 1] if i + 1 < len(argv) else ""
        found.append((arg if eq else f"{arg} {value}", value.lower()))
    return found


def bypass_arguments(argv) -> list:
    """Arguments that turn permission or approval checks off beyond permission policy A."""
    found = [a for a in argv if any(m in a.lower() for m in BYPASS_MARKERS)]
    found += [shown for shown, value in flag_values(argv, PERMISSION_MODE_FLAG) if value in BROAD_PERMISSION_MODES]
    sandboxes = [value for _, value in flag_values(argv, SANDBOX_FLAG)]
    if not sandboxes or not all(value in CONFINED_SANDBOXES for value in sandboxes):
        found += [shown for shown, value in flag_values(argv, APPROVAL_FLAG) if value == "never"]
    return found


def read_status_line(path: Path, pattern) -> str:
    """Read only the first line of a report or verdict; the rest stays in the project for the reviewer."""
    try:
        # utf-8-sig: Windows tools often start a text file with a byte order mark.
        with open(path, encoding="utf-8-sig") as f:
            first = f.readline(200)
    except (OSError, UnicodeDecodeError):
        return "missing"
    match = pattern.match(first.strip())
    return match.group(1).lower() if match else "unrecognized"


def build_prompt(conn, task, report=None) -> str:
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
    writes = stored_json(task["writes"])
    if writes is not None:
        lines.append("- Other tasks may be working in this folder at the same time. "
                     + (f"Change only these project paths: {', '.join(writes)}." if writes
                        else "Do not change any project file.")
                     + (" The report file named below is allowed." if report is not None else ""))
    if report is not None:
        lines += [
            f"- Before you exit, write your report to the file named on the next line. Its first line must be"
            " exactly one of: STATUS: done / STATUS: needs-approval / STATUS: blocked.",
            f"Report file: {report}",
            "- If an action needs user approval, do not do it: write STATUS: needs-approval with the reason, then exit.",
        ]
    return "\n".join(lines) + "\n"


def build_review_prompt(conn, task, worker_report: Path, verdict_file: Path) -> str:
    plan = conn.execute("SELECT value FROM meta WHERE key = 'name'").fetchone()[0]
    return "\n".join([
        "You review exactly one finished task from a work breakdown structure.",
        f"Plan: {plan}",
        f"Task {task['id']}: {task['title']}",
        "",
        "Description:",
        task["description"] or "(none)",
        "",
        "Acceptance criteria:",
        task["acceptance"] or "(none)",
        "",
        "Rules:",
        "- Follow the AGENTS.md and other instructions of the project you work in.",
        "- Judge the changes in the project against the acceptance criteria.",
        f"- The worker's own report, if it wrote one, is at {worker_report}. It is a claim, not evidence.",
        "- Do not change, create or delete any project file except the verdict file. Any other change voids the review.",
        "- Do not bypass permission or approval prompts. Do not print, store or send secrets or personal data.",
        "- Before you exit, write the verdict file. Its first line must be exactly one of:"
        " VERDICT: accept / VERDICT: reject / VERDICT: needs-human. Give your reasons below that line.",
        "- If you are unsure, or a person should decide, write VERDICT: needs-human.",
        f"Verdict file: {verdict_file}",
    ]) + "\n"


def posix_process_start(pid):
    """A value that identifies a living process across pid reuse, or None if it cannot be read.

    Minimal systems may have no `ps`. Default runs must still work there, so this is
    "unknown", which parallel runs treat as not running."""
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                                capture_output=True, encoding="utf-8", check=False)
    except OSError:
        return None
    return result.stdout.strip() or None


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, wintypes.LPDWORD)
    _kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, *[wintypes.LPFILETIME] * 4)

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259

    def process_start(pid):
        """A value that identifies a living process across pid reuse, or None if it cannot be read.

        Not os.kill(pid, 0): on Windows that ends the process. An ended process can
        still be opened while another handle to it exists, so the exit code is checked."""
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            times = [wintypes.FILETIME() for _ in range(4)]
            if (not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE
                    or not _kernel32.GetProcessTimes(handle, *map(ctypes.byref, times))):
                return None
            return f"{times[0].dwHighDateTime}:{times[0].dwLowDateTime}"
        finally:
            _kernel32.CloseHandle(handle)

    def _job_for(proc):
        """A job that ends every process in it when its last handle closes, with proc assigned to it."""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not (_kernel32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                  ctypes.byref(info), ctypes.sizeof(info))
                # Popen keeps the process handle in the private _handle attribute on Windows.
                and _kernel32.AssignProcessToJobObject(job, int(proc._handle))):
            _kernel32.CloseHandle(job)
            return None
        return job
else:
    process_start = posix_process_start


class ProcessTree:
    """End a CLI together with every process it started.

    Windows: the CLI joins a Job Object with KILL_ON_JOB_CLOSE, so closing the job,
    or wbs.py itself being killed, ends the whole tree. A process the CLI starts in
    the moment before it joins the job is not covered. If the job cannot be set
    up, `taskkill /T /F` is used on timeout instead.
    POSIX: the CLI leads its own session, and the whole process group gets SIGKILL."""

    def __init__(self, proc):
        self.proc, self.job = proc, None
        self.method = "process group"
        if os.name == "nt":
            self.job = _job_for(proc)
            self.method = "job object" if self.job else "taskkill /T"

    def kill(self) -> None:
        if self.job:
            _kernel32.TerminateJobObject(self.job, 1)
        elif os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.kill()
        self.proc.wait()

    def close(self) -> None:
        """End anything the CLI left running, also after a normal exit."""
        if self.job:
            _kernel32.CloseHandle(self.job)
            self.job = None
        elif os.name != "nt":
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGKILL)


def invoke(argv, prompt, timeout, on_start):
    """Start the CLI without a shell, pass the prompt on stdin and discard its output.

    CLI stdout and stderr go to the null device: they are never printed or stored.
    on_start runs once the process exists, so only real starts are counted.
    Every process the CLI started is ended before this returns (see ProcessTree).
    Returns (started, exit_code or None, fixed note without output). There is no
    retry here: a failure or timeout is final for this run."""
    extra = {} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, encoding="utf-8", **extra)
    except OSError as exc:
        return False, None, f"cli could not start ({type(exc).__name__})"
    tree = ProcessTree(proc)
    try:
        on_start()
        try:
            proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            tree.kill()
            return True, None, f"timeout after {timeout}s; ended by {tree.method}"
        return True, proc.returncode, f"exit code {proc.returncode}"
    finally:
        tree.close()


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
            " requires_approval, writes, checkpoint, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            [(t["id"], pos, t["title"], t["role"], t["description"], t["acceptance"],
              int(t["requires_approval"]), None if t["writes"] is None else json.dumps(t["writes"]),
              int(t["checkpoint"])) for pos, t in enumerate(tasks)],
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
        "open_attempts": [
            {"attempt": a["id"], "task": a["task_id"], "worker": a["worker"], "cli": a["cli"],
             "started_at": a["started_at"], "running": attempt_running(a) if a["cli"] is not None else None}
            for a in conn.execute("SELECT * FROM attempts WHERE ended_at IS NULL ORDER BY id")],
        "cli_attempts": [
            {"attempt": a["id"], "task": a["task_id"], "cli": a["cli"], "parallelism": a["parallelism"],
             "profile_args": stored_json(a["profile_args"]), "outcome": a["outcome"]}
            for a in conn.execute("SELECT * FROM attempts WHERE cli IS NOT NULL ORDER BY id")],
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


def claim_for_cli(conn, name, cli):
    return claim(conn, f"cli:{name}", cli=name, limits=cli["limits"], profiles=cli["profiles"],
                 select=lambda r: r["role"] in cli["roles"] and not r["requires_approval"])


def cmd_run(conn, args) -> int:
    cli = load_cli(args.config, args.cli)
    task, attempt = claim_for_cli(conn, args.cli, cli)
    if task is None:
        ready = [r["id"] for r in ready_tasks(conn)]
        reason = f"ready tasks need user approval or another cli: {ready}" if ready else idle_reason(conn)
        emit({"result": "idle", "reason": reason, "cli_invoked": False})
        return EXIT_OK
    out = execute_attempt(conn, task, attempt, cli, Path(args.db).name)
    emit(out)
    return EXIT_OK if out["result"] == "awaiting_review" else EXIT_BLOCKED


def execute_attempt(conn, task, attempt, cli, db_name) -> dict:
    """Start the CLI for a claimed attempt and record the outcome. Returns what `run` prints."""
    report = None
    if cli["report_dir"] is not None:
        cli["report_dir"].mkdir(parents=True, exist_ok=True)
        report = cli["report_dir"] / f"{task['id']}-attempt{attempt}.md"
        # A new database restarts the numbering: a report left by an earlier plan must never count.
        report.unlink(missing_ok=True)
    recorded = conn.execute("SELECT parallelism, profile_args FROM attempts WHERE id = ?", (attempt,)).fetchone()
    profile_args = stored_json(recorded["profile_args"])
    started, exit_code, note = invoke(cli["argv"] + profile_args, build_prompt(conn, task, report), cli["timeout"],
                                      on_start=lambda: mark_started(conn, attempt))
    report_status = read_status_line(report, REPORT_STATUS) if report is not None and exit_code == 0 else None
    if report_status:
        note += f"; report: {report_status}"
    # Exit code 0 only means the CLI says it finished; a reviewer decides with accept/reject.
    # A CLI that reports it stopped for approval or was blocked fails, so a person looks at once.
    stopped = report_status in ("needs-approval", "blocked")
    outcome = "awaiting_review" if exit_code == 0 and not stopped else "failed"
    finish(conn, attempt, outcome, note, exit_code)
    calls = task_cli_calls(conn, task["id"])
    warning = None
    if started and calls % WARN_EVERY == 0:
        warning = write_warning(cli["warning_dir"], task["id"], calls, db_name)
    return {"result": outcome, "task": task["id"], "attempt": attempt, "exit_code": exit_code,
            "cli_invoked": started, "task_cli_calls": calls, "warning_file": warning,
            "report": report_status, "report_file": str(report) if report is not None else None,
            "parallelism": recorded["parallelism"], "profile_args": profile_args}


def execute_in_thread(db_path, task, attempt, cli) -> dict:
    with closing(connect(db_path)) as conn:
        return execute_attempt(conn, task, attempt, cli, Path(db_path).name)


def awaiting_review(conn) -> list:
    return conn.execute("SELECT * FROM tasks WHERE status = 'awaiting_review' ORDER BY position").fetchall()


def needs_person(task, progress) -> bool:
    return progress["mode"] == "checkpoint" and bool(
        task["checkpoint"] or task["id"] in progress["tasks"] or task["role"] in progress["roles"])


def project_snapshot(skip, ignore_dirs) -> dict:
    """Size and modification time of each file under the working directory. Git would not see a
    second change to a file the worker left uncommitted, so files are compared directly."""
    root = Path.cwd().resolve()
    found = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git" and d not in ignore_dirs and Path(folder, d) not in skip]
        for name in files:
            path = Path(folder, name)
            if path not in skip:
                info = path.lstat()
                found[str(path.relative_to(root))] = (info.st_size, info.st_mtime_ns)
    return found


def review_with_agent(conn, task, clis, reviewers, reviewer_clis, progress, db_path) -> tuple:
    """Run the assigned reviewer once and apply its verdict. Returns (stop kind or None, detail, reviewer label)."""
    attempt = conn.execute("SELECT id, cli FROM attempts WHERE task_id = ? AND cli IS NOT NULL"
                           " ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
    reviewer = reviewers.get(attempt["cli"])
    if reviewer is None or reviewer == attempt["cli"]:
        raise WbsError(f"task {task['id']}: cli {attempt['cli']!r} has no reviewer in progress.reviewers")
    entry = reviewer_clis[reviewer]
    worker = clis.get(attempt["cli"], next(iter(clis.values())))
    report_dir = worker["report_dir"]
    report_dir.mkdir(parents=True, exist_ok=True)
    earlier = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'agent_review' AND task_id = ?",
                           (task["id"],)).fetchone()[0]
    verdict_file = report_dir / f"{task['id']}-attempt{attempt['id']}-review{earlier + 1}.md"
    # A new database restarts the numbering: a verdict left by an earlier plan must never count.
    verdict_file.unlink(missing_ok=True)
    db = Path(db_path).resolve()
    skip = {report_dir, worker["warning_dir"].resolve(), db, *(Path(f"{db}{s}") for s in ("-journal", "-wal", "-shm"))}
    before = project_snapshot(skip, progress["ignore_dirs"])
    argv = entry["argv"] + profile_args_for(entry["profiles"], 1)
    prompt = build_review_prompt(conn, task, report_dir / f"{task['id']}-attempt{attempt['id']}.md", verdict_file)
    _, exit_code, note = invoke(argv, prompt, entry["timeout"], on_start=lambda: None)
    after = project_snapshot(skip, progress["ignore_dirs"])
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    verdict = read_status_line(verdict_file, REVIEW_VERDICT) if exit_code == 0 else None
    label = f"{reviewer}-auto"
    add_event(conn, "agent_review", task["id"], attempt["id"],
              f"reviewer={label}; args={profile_args_for(entry['profiles'], 1)}; verdict={verdict}; "
              f"changed_files={len(changed)}"[:NOTE_MAX])
    if exit_code != 0:
        return "review_failed", f"reviewer of task {task['id']} failed: {note}", label
    if changed:
        return ("review_changed_files", f"reviewer of task {task['id']} changed {len(changed)} file(s), for example"
                f" {changed[:5]}; verdict not used", label)
    if verdict not in ("accept", "reject"):
        return "review_undecided", f"reviewer of task {task['id']} gave verdict {verdict!r}: a person must review", label
    note = f"verdict file {verdict_file.name}"
    review(conn, task["id"], label, verdict, note)
    if verdict == "reject":
        return "rejected", f"task {task['id']} rejected by {label}: see {verdict_file.name}", label
    return None, "", label


def cmd_advance(conn, args) -> int:
    """Repeat dispatch and agent review until a checkpoint, an approval task or anything unusual."""
    if args.max_steps < 1:
        raise WbsError("--max-steps must be a positive integer")
    clis, reviewers, reviewer_clis, progress = load_progress(args.config, conn)
    steps, stops, started, accepted = 0, [], [], []

    def stop(kind, reason, abnormal=False):
        stops.append({"stop": kind, "reason": reason, "abnormal": abnormal})

    workers = sum(cli["limits"]["cli"] if cli["limits"] else 1 for cli in clis.values())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        running = {}
        while True:
            if not stops:
                waiting = [t["id"] for t in awaiting_review(conn) if needs_person(t, progress)]
                if waiting:
                    stop("checkpoint", f"task(s) {waiting} wait for a person's review")
            if not stops:
                try:
                    for name, cli in clis.items():
                        # Without limits a claim is refused while any attempt is open, so one at a time.
                        while steps < args.max_steps and not (cli["limits"] is None and running):
                            try:
                                task, attempt = claim_for_cli(conn, name, cli)
                            except Busy:
                                break
                            if task is None:
                                break
                            running[pool.submit(execute_in_thread, args.db, task, attempt, cli)] = task["id"]
                            steps += 1
                except Blocked as exc:
                    stop("blocked", str(exc), abnormal=True)
            if running:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    del running[future]
                    out = future.result()
                    started.append({key: out[key] for key in (
                        "task", "attempt", "result", "exit_code", "report", "parallelism", "warning_file")})
                    if out["result"] != "awaiting_review":
                        stop("failed", f"task {out['task']} ended failed: inspect, fix, then `retry`", abnormal=True)
                    elif out["warning_file"]:
                        stop("call_warning", f"task {out['task']} reached {out['task_cli_calls']} CLI starts:"
                             " check progress, cost and quota", abnormal=True)
                continue
            if stops:
                break
            # A review compares project files, so nothing else may be writing.
            if conn.execute(OPEN_CLI_SQL).fetchone():
                stop("other_run", "another run has an open CLI attempt; advance again when it has ended")
                break
            reviewable = awaiting_review(conn)
            if reviewable:
                if steps >= args.max_steps:
                    stop("max_steps", f"reached --max-steps {args.max_steps}")
                    break
                steps += 1
                kind, detail, label = review_with_agent(conn, reviewable[0], clis, reviewers, reviewer_clis,
                                                        progress, args.db)
                if kind:
                    stop(kind, detail, abnormal=True)
                else:
                    accepted.append({"task": reviewable[0]["id"], "reviewer": label})
                    if progress["every"] and len(accepted) % progress["every"] == 0:
                        stop("every", f"{len(accepted)} task(s) accepted by agents: a person checks progress,"
                             " then runs advance again")
                continue
            ready = ready_tasks(conn)
            mapped = {role for cli in clis.values() for role in cli["roles"]}
            if any(r["role"] in mapped and not r["requires_approval"] for r in ready):
                stop("max_steps", f"reached --max-steps {args.max_steps}")
            elif ready:
                stop("person_needed", f"ready task(s) {[r['id'] for r in ready]} need a person")
            else:
                reason = idle_reason(conn)
                stop("done" if reason == "all tasks done" else "idle", reason)
            break
    final = next((s for s in stops if s["abnormal"]), stops[0])
    add_event(conn, "advance_stop", detail=f"stop={final['stop']}; auto_accepted={len(accepted)}; steps={steps}")
    emit({"result": "stopped", **final, "steps": steps, "auto_accepted": accepted, "started": started})
    return EXIT_BLOCKED if final["abnormal"] else EXIT_OK


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
    p = sub.add_parser("advance", help="repeat run and agent review, in checkpoint or auto mode, until a stop")
    p.add_argument("--config", required=True, help="JSON config with progress.mode and progress.reviewers")
    p.add_argument("--max-steps", type=int, required=True, help="most CLI starts, worker and reviewer together")
    p.set_defaults(func=cmd_advance)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            return cmd_init(args)
        if not Path(args.db).is_file():
            raise WbsError("database not found; run init first")
        with closing(connect(args.db)) as conn:
            if not any(column["name"] == "checkpoint" for column in conn.execute("PRAGMA table_info(tasks)")):
                raise WbsError("database was created by an older wbs.py; finish it with that version"
                               " or create a new database with init")
            return args.func(conn, args)
    except Busy as exc:
        emit({"result": "busy", "reason": str(exc), "cli_invoked": False})
        return EXIT_OK
    except Blocked as exc:
        emit({"result": "blocked", "reason": str(exc), "cli_invoked": False})
        return EXIT_BLOCKED
    except WbsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
