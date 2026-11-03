---
name: wbs-orchestration
description: "Plan a multi-step change as a dependency-ordered WBS, keep its state in a local SQLite file, and dispatch at most one ready task per run to a CLI the user configured and named for that run. Exit code 0 from a CLI only marks a task awaiting review; the reviewer the user assigned (a person, or an agent such as Codex) must accept or reject it. Use for multi-step changes that agent CLIs carry out under user review and approval. Not for single-step edits."
---

# WBS orchestration

Split a change into a work breakdown structure (WBS) with dependencies, keep its
state in SQLite with `scripts/wbs.py`, and dispatch at most one ready task per
`run` to a CLI that the user configured and named on that command line. A CLI
never moves a task to `done` by itself. A reviewer must run `accept` or `reject`
on each CLI task.

## Scope

In scope:
- Writing an implementation plan and a WBS with the user, then turning it into
  a JSON plan.
- Creating the state database, dispatching tasks to configured CLIs, and
  recording claims, attempts, review verdicts and events.
- Stopping all scheduling when a task fails, until a person runs `retry`.
- Allowing at most one open CLI attempt per database. Manual claims are not
  limited by it.

Out of scope:
- Installing CLIs or storing credentials. This package is a skill,
  standard-library Python scripts and one PowerShell helper. It installs
  nothing. A scheduled job exists only if the user runs
  `scripts/register_schedule.ps1` (see "Scheduling").
- Judging whether a task's work is correct. `wbs.py` records the CLI's exit
  code, nothing more. The assigned reviewer judges the work.
- Merging, publishing, deploying, changing data or spending money. Model each
  of these as a `requires_approval` task for a person.
- Editing a plan after `init`. No command changes tasks, dependencies or
  descriptions. To change the plan, create a new database from a revised plan.

## Ground rules

- Stay project and provider neutral. Do not assume a CLI, model, vendor,
  repository layout or project. Get the plan content, CLI commands and role
  mapping from the user.
- The project's AGENTS.md and other instructions override this skill. Read them
  before planning. Every dispatched prompt tells the CLI to follow the
  instructions of the project it works in.
- Approvals stay with the user. Never add permission-bypass or auto-approve
  flags to a CLI command. `wbs.py` refuses any argument that contains
  `dangerous`, `bypass`, `skip-permission`, `yolo` or `no-sandbox` (case is
  ignored). That check is a guard, not a guarantee, so review every configured
  argument list with the user.
- Keep secrets and personal data out of plans, notes, review reasons, config and
  the database. CLIs read their own credentials from wherever they normally keep
  them. `wbs.py` passes its environment to the CLI unchanged. It does not store
  prompts, CLI output or environment values.
- Use fictional data when trying a configuration for the first time.

## Project-specific configuration

Each project has its own files. Agree on their locations with the user:

| File | Content | Notes |
|---|---|---|
| Plan JSON | Plan name, goal, tasks | Format in `examples/plan.example.json` |
| State database | SQLite file given with `--db` | Created by `init`. `init` never overwrites an existing database |
| Config JSON | `clis`, `roles`, `warning_dir` | Format in `examples/config.example.json` |
| Warning folder | Warning files from `run` | `warning_dir` in the config. A relative path resolves from the config file's folder |

Plan rules, checked by `init`:
- Top level: `plan.name` must be a non-empty string. `plan.goal` is an
  optional string. `tasks` must be a non-empty list.
- Allowed task keys: `id`, `title`, `role`, `description`, `acceptance`,
  `depends_on`, `requires_approval`. Any other key is rejected.
- `id` and `role` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`. Ids must be
  unique. `title` must not be empty.
- `depends_on` is a list of unique, existing task ids. Cycles are rejected.
- `requires_approval` is `true` or `false` (default `false`). `description` and
  `acceptance` are strings (default empty).

Config rules, checked by `run`. Only the CLI entry named by `--cli` is checked:
- `clis.NAME.argv` must be a non-empty list of non-empty strings with no bypass
  markers.
- `clis.NAME.timeout_seconds` must be a positive integer. The default is 1800.
- At least one role in `roles` must map to `NAME`.
- `warning_dir` must be a non-empty string.

No CLI handles a role that is not mapped in `roles`. People handle those roles
with `claim` and `complete`.

`argv` runs without a shell, so pipes, redirection, variable expansion and
shell aliases do not work. The CLI must read its prompt from stdin. The CLI
inherits the working directory and environment of the `wbs.py` process. Start
`run` from the directory where the CLI should work.

## Workflow

1. **Plan.** Read the project instructions and the request. Write the plan:
   goal, scope, non-goals, risks, and how completion is verified.
2. **WBS.** Split the plan into tasks that fit in one CLI session. Write each
   task's description and acceptance criteria so the task makes sense on its
   own (see "Fresh CLI sessions"). Follow each significant implementation task
   with a review task for a different role. Add a `requires_approval: true`
   task wherever the user must decide (merge, publish, data changes,
   spending). Show the WBS to the user and get agreement before `init`.
3. **Initialize.** Save the WBS as JSON and run `init`.
4. **Configure.** Write the config with the user. Map roles to CLI names. Leave
   person-only roles unmapped.
5. **Run.** Each `run --cli NAME` claims at most one ready task, starts the
   CLI once, and records the result. While any CLI attempt in the database is
   still open, for any task and any CLI name, `run` starts no CLI and exits
   `blocked`. Repeat by hand or from a scheduler the user sets up.
6. **Review.** A task that exits with code 0 becomes `awaiting_review`. The
   reviewer the user assigned (a person, or an agent such as Codex) inspects
   the work in the project, then runs `accept` (the task becomes `done` and
   releases its dependents) or `reject` (the task becomes `failed` and
   scheduling stops).
7. **Approve.** A person handles `requires_approval` tasks with
   `claim --worker NAME --task ID`, then `complete` or `fail`.
8. **Recover.** Any `failed` task blocks every further `claim` and `run`.
   Inspect it, fix the cause, then run `retry --task ID`. An open CLI attempt
   left by an interrupted `run` blocks every further `run` until a person
   investigates it, runs `fail --attempt N`, then `retry --task ID`.

## Task states

| State | Entered by | Leaves by |
|---|---|---|
| `pending` | `init`, `retry` | `claim` or `run` when all prerequisites are `done` |
| `claimed` | `claim`, `run` | `complete`, `fail`, or the end of the CLI call |
| `awaiting_review` | `run` when the CLI exits with code 0 | `accept` or `reject` |
| `done` | `complete` (non-CLI attempts only), `accept` | No transition out |
| `failed` | `fail`, `reject`, `run` when the CLI fails | `retry` |

A task is ready when it is `pending` and every prerequisite is `done`. A
prerequisite that is `claimed` or `awaiting_review` does not release its
dependents. Tasks without a dependency between them can each be
`awaiting_review` at the same time.

## Commands

Put `--db PATH` before the subcommand. Successful commands print JSON to stdout.
Errors print one line, `error: ...`, to stderr and no JSON.

| Command | Effect |
|---|---|
| `init --plan FILE` | Create the database from a plan. Fails if the database file already exists |
| `status` | Plan name, counts per state, ready ids, total CLI starts (`cli_invocations`), and each task with its own `cli_calls` |
| `ready` | Ready tasks with id, title, role and `requires_approval` |
| `claim --worker NAME [--task ID]` | Claim the first ready task, or the given ready task, for a person or agent. No role filter. Prints the attempt id |
| `complete --attempt N [--note TEXT]` | Mark a non-CLI attempt `done`. Refused for attempts started by `run` |
| `fail --attempt N --reason TEXT` | Mark any open attempt `failed`, including one left open by an interrupted `run`. Stops scheduling |
| `accept --task ID --reviewer NAME [--note TEXT]` | `awaiting_review` → `done`. Releases dependents |
| `reject --task ID --reviewer NAME --reason TEXT` | `awaiting_review` → `failed`. Stops scheduling |
| `retry --task ID` | `failed` → `pending` |
| `run --config FILE --cli NAME` | Dispatch one ready task to the named CLI |

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Success. This includes `"result": "idle"` and `run` ending in `awaiting_review` |
| `1` | Invalid input or state: bad plan or config, database exists or is missing, task not ready or not awaiting review, attempt unknown or already finished, `complete` on a CLI attempt |
| `2` | Usage error from argument parsing |
| `3` | `"result": "blocked"` from `claim` or `run` because a task is `failed`, from `run` because a CLI attempt is open, or `run` whose CLI call ended `failed` |

Examples with the bundled examples. Replace `<skill>` with this skill's
directory. Run `run` from the project directory.

```sh
python <skill>/scripts/wbs.py --db state.sqlite3 init --plan plan.json
python <skill>/scripts/wbs.py --db state.sqlite3 status
python <skill>/scripts/wbs.py --db state.sqlite3 run --config config.json --cli builder
python <skill>/scripts/wbs.py --db state.sqlite3 accept --task T1 --reviewer owner --note "Design note checked"
python <skill>/scripts/wbs.py --db state.sqlite3 reject --task T2 --reviewer owner --reason "No test for the no-match case"
python <skill>/scripts/wbs.py --db state.sqlite3 retry --task T2
python <skill>/scripts/wbs.py --db state.sqlite3 claim --worker owner --task T4
python <skill>/scripts/wbs.py --db state.sqlite3 complete --attempt 7 --note "Merge approved"
```

In the example config, `planner` and `implementer` map to `builder`,
`reviewer` maps to `checker`, and `owner` is unmapped. T1 to T3 therefore run
on CLIs. Each of them, including the review task T3, waits in
`awaiting_review` until the assigned reviewer (a person or an agent) accepts
it. T4 needs approval, so a person claims and completes it.

## What `run` does

In this order:

1. Loads the config and checks the entry named by `--cli`. Any config error
   exits with code 1 before a task is claimed.
2. In one `BEGIN IMMEDIATE` transaction, checks for failed tasks and open CLI
   attempts, then claims a task. If any task is `failed`, or any CLI attempt
   (for any task, from any CLI name) has not ended, it prints
   `"result": "blocked"` with `"cli_invoked": false` and exits with code 3.
   Otherwise it claims the first ready task in plan order whose role maps to
   this CLI and whose `requires_approval` is `false`. So at most one CLI runs
   per database at a time. Attempts opened by `claim` do not count.
3. If no task matches, it prints `"result": "idle"` with `"cli_invoked": false`
   and exits with code 0. The `reason` is one of the following:
   - `ready tasks need user approval or another cli: [...]`
   - `no ready task; awaiting review: [...]`
   - `all tasks done`
   - `no ready task`
4. Starts the CLI once, from `argv`, without a shell, and writes the prompt to
   stdin as UTF-8. stdout and stderr go to the null device. CLI output is
   discarded, and is never printed or stored. There is no automatic retry.
5. Records the outcome on the attempt and the task:

| CLI result | Task state | `exit_code` | Counted as a CLI start | `run` exit code |
|---|---|---|---|---|
| Exit code 0 | `awaiting_review` | `0` | Yes | `0` |
| Non-zero exit code | `failed` | That code | Yes | `3` |
| Timeout (`timeout_seconds`) | `failed`, process killed | `null` | Yes | `3` |
| Could not start (for example, executable not found) | `failed` | `null` | No | `3` |

6. Counts the CLI processes that actually started for this task, across all
   of its attempts and retries. When that per-task count reaches a multiple of
   5 (5, 10, ...), `run` writes
   `wbs-warning-<UTC stamp>-<task>-call<NNNN>.txt` to `warning_dir` and prints
   a `WARNING:` line to stderr. The file contains the database file name, the
   task id, the count and the time, but no prompts or CLI output. A warning does
   not stop scheduling. It asks the user to check progress, cost and quota. The
   warning file is written after the outcome is saved, and `wbs.py` does not
   catch errors from writing it.
7. Prints `result`, `task`, `attempt`, `exit_code`, `cli_invoked`,
   `task_cli_calls` and `warning_file`.

The prompt contains the plan name and goal, the task id, title and role, the
ids of the completed prerequisites, the description, the acceptance criteria,
and fixed rules. The rules tell the CLI to follow the project instructions, not
to bypass approvals, not to expose secrets, to work only on this task, and to
report what it changed and how it verified it. That report is part of the
discarded output, so reviewers judge the changes in the project, not the CLI's
own summary.

Notes given to `complete` and `fail`, and the reviewer name with the note or
reason given to `accept` and `reject`, are stored, truncated to 500 characters.
Keep them free of secrets and personal data.

## Review and approval boundaries

- Exit code 0 means only that the CLI exited normally. The work is not done
  until a reviewer runs `accept`. Running `complete` on a CLI attempt is
  refused.
- A review task run by a CLI is also only `awaiting_review`. Its verdict does
  not accept the implementation task. Every CLI task needs its own `accept`.
- `accept` and `reject` only apply to tasks in `awaiting_review`. Any other
  state exits with code 1.
- `run` never dispatches a `requires_approval` task. `claim` has no role or
  approval filter, and `complete` on a claimed task sets `done` with no review
  step. Use `claim` and `complete` only for work a person actually did or
  approved.
- `reject` and every CLI failure stop all scheduling, including independent
  tasks, until `retry`. Only run `retry` after the cause has been fixed.
- The reviewer who runs `accept` or `reject` is the one the user assigned: a
  person, or an agent such as Codex. The reviewer name is stored, so use one
  that shows who reviewed (for example `--reviewer codex`). An agent reviewer
  inspects the work in its own session, never as part of a scheduled `run`.
- A schedule, a repeated `run`, or an agent the user did not assign as
  reviewer must never run `accept` or `reject`. `retry` and approval steps
  stay with the user.

## Fresh CLI sessions

Each `run` starts a new CLI process with one prompt. Nothing from earlier
attempts is passed to it: no earlier output, no review notes, no rejection
reason. After `reject` and `retry`, the CLI gets the same prompt as before.

- Configure `argv` so that each call starts a new session. Do not use options
  that resume or continue an earlier session.
- Write descriptions that stand on their own. Name the files, artifacts or
  branches the task reads and writes.
- To pass review findings to a retried task, record them in a project file that
  the task's description already names. You cannot change the description after
  `init`.
- Ask in the acceptance criteria for durable evidence in the project, such as
  tests, files or commits, because the CLI's console report is discarded.

## Scheduling

- `run` handles at most one task per execution. To process a WBS, call it
  repeatedly, by hand or from a scheduler the user creates and owns. On
  Windows, `scripts/register_schedule.ps1` can register that job (see
  "Registering a schedule on Windows"). Nothing is registered unless the user
  runs it.
- Start the scheduled command in the project directory, with full paths to
  `wbs.py`, the database and the config, and with `--cli` naming the one CLI
  the user authorized for that job.
- Interpret exit codes in the job:
  - `0` with `idle`: nothing to do, or waiting for review or approval.
  - `0` with `awaiting_review`: a reviewer is needed.
  - `3`: a task is `failed`, a CLI attempt is still open, or this run's CLI
    call failed. A person must act. Further runs start no CLI until the
    cause is cleared, but notify the user instead of hiding the code.
  - `1`: fix the config or state.
- Only one CLI attempt per database can be open at a time. A `run` that
  starts while another is open, even for a different task or CLI name, exits
  `blocked` (code 3) without starting a CLI. Independent tasks therefore run
  one after another on CLIs. Manual `claim` attempts do not count toward this
  limit and are not blocked by it, so a person or agent working on a claimed
  task can edit the same working tree while a CLI runs. Coordinate that by
  hand, for example with separate worktrees.
- Act on warning files: check progress, cost and quota before you let the
  schedule continue.
- If `wbs.py` is interrupted during a CLI call, the attempt stays open and the
  task stays `claimed`. That open attempt blocks every later `run`, for all
  tasks, and nothing closes or retries it automatically. Manual `claim` is not
  blocked. A person investigates the work the interrupted CLI left, closes the
  attempt with `fail --attempt N --reason TEXT` (the task becomes `failed`),
  fixes the cause, then runs `retry --task ID`. No command lists attempts.
  Take the attempt id from the `blocked` reason of a later `run`, which names
  the open attempt and task ids, or read the `attempts` table with a read-only
  SQLite query.

## HTML status report

`scripts/render_wbs.py` writes one self-contained HTML page from a database.
It opens the database read-only (`mode=ro`) and changes nothing in it.

```sh
python <skill>/scripts/render_wbs.py --db state.sqlite3 --output wbs-status.html
```

- The page shows the plan name, counts per state, done of total, ready ids,
  total CLI starts, and per task its id, title, role, state, dependency ids,
  `requires_approval` and `cli_calls`. All database text is HTML-escaped. The
  page has no scripts and loads nothing external.
- It never reads `events`, attempt notes, descriptions or acceptance criteria.
  Titles and the plan name are shown, so keep them free of secrets.
- The user chooses the output path, and its folder must exist. The page is
  written to a temporary file in that folder and renamed over the
  destination, so a reader never sees half a page. An existing file at that
  path is replaced, except the database itself: if `--output` names the same
  file as `--db`, the script stops with `error: output path is the database
  file`, exit code 1, and writes nothing.
- The page is a snapshot stamped with its UTC time. It does not refresh.
- Exit codes: `0` rendered (JSON with `output` and `tasks` on stdout), `1`
  database or output folder missing, output path is the database, database
  unreadable or write failed, `2` usage error.

## Registering a schedule on Windows

`scripts/register_schedule.ps1` registers one Windows scheduled task per call,
using only built-in ScheduledTasks cmdlets. Register the dispatch job and the
report job as two tasks with different names. Every name and path is an
argument. The script paths come from the script's own folder.

```powershell
.\register_schedule.ps1 -TaskName "WBS example run" -PythonExe C:\path\to\python.exe `
  -DbPath C:\path\to\state.sqlite3 -ConfigPath C:\path\to\config.json -CliName builder `
  -WorkingDirectory C:\path\to\project -IntervalMinutes 30 -TimeLimitMinutes 60
.\register_schedule.ps1 -TaskName "WBS example report" -PythonExe C:\path\to\python.exe `
  -DbPath C:\path\to\state.sqlite3 -OutputPath C:\path\to\wbs-status.html -IntervalMinutes 10
```

- `-WhatIf` shows what would be registered without registering it.
- A task with the same name in the same `-TaskPath` (default `\`) is never
  replaced. The script stops with an error instead.
- The task runs as the current user, only while that user is logged on, with
  limited rights. No password is asked for or stored.
- `-MultipleInstances IgnoreNew`: a start is skipped while the previous run of
  the same task is still going. The first start is one minute after
  registration, then one every `-IntervalMinutes`.
- `-TimeLimitMinutes` (default 120) stops a run that takes longer. Set it above
  the CLI's `timeout_seconds`. A run stopped mid-CLI leaves an open attempt
  that blocks every later run, for all tasks, until a person investigates it,
  runs `fail`, then `retry` (see "Scheduling").
- Tasks do not start on battery power (the Task Scheduler default). A running
  task is not stopped when the computer switches to battery.
- `-CliName` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.
- The script does not change the execution policy and is not meant to be run
  with `-ExecutionPolicy Bypass`. If the policy blocks it, the user decides.
- Task Scheduler stores each exit code as the last run result (`0x3` when
  blocked). Nothing notifies the user, so check the result or the report.
- A console window can appear at each start. `pythonw.exe` hides the Python
  window, but a CLI may still open its own.
- Remove a task with `Unregister-ScheduledTask -TaskName NAME -TaskPath \`.

## Tests

From this skill's directory:

```sh
python -m unittest discover -s tests -v
```

The tests use fictional data and a stand-in CLI that the test writes itself.
They cover:
- Plan validation and `init` refusing to overwrite.
- Prerequisite ordering, and parallel claims never taking the same task.
- No CLI start when nothing is ready or everything is done.
- Approval tasks left for a person.
- The per-task count and the warning at each task's fifth start.
- The prompt on stdin, and CLI output that is neither printed nor stored.
- Refusal of bypass arguments and unconfigured CLIs.
- Failure stopping all scheduling until `retry`.
- A CLI that cannot start, which is not counted.
- A timeout, which is counted and not retried.
- The review gate: exit code 0 needs `accept`, `complete` is refused, and
  `reject` blocks until `retry`.
- `render_wbs.py`: escaped titles and plan name, every task row in plan
  order, progress counters, no notes or events, an unchanged database, a
  whole-file replacement of the output, and a missing database that fails
  without writing anything.

The tests do not run any real agent CLI and are not production validation. Try
a real configuration on a small fictional plan before relying on it.

`register_schedule.ps1` has no automated test. Check it with `-WhatIf`, then
register it with a fictional plan and confirm the task in Task Scheduler.
