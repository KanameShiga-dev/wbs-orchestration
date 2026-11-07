"""Tests for scripts/wbs.py. Standard library only; all data is fictional."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wbs.py"
sys.path.insert(0, str(SCRIPT.parent))
import wbs  # noqa: E402

SECRET = "fictional-secret-value-123"

# Stand-in for a real agent CLI: records what it received, then exits.
FAKE_CLI = """
import json, os, re, subprocess, sys, time
HEARTBEAT = "import sys, time\\nend = time.time() + 60\\nwhile time.time() < end:\\n    open(sys.argv[1], 'a').write('.')\\n    time.sleep(0.1)\\n"
prompt = sys.stdin.read()
with open(os.environ["FAKE_CLI_RECORD"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt}) + "\\n")
mode = os.environ.get("FAKE_CLI_MODE", "ok")
if mode == "leak":
    print("leaked " + os.environ["FAKE_SECRET"])
    print("leaked " + os.environ["FAKE_SECRET"], file=sys.stderr)
if mode == "hang":
    time.sleep(60)
if mode == "wait":
    deadline = time.time() + 60
    while not os.path.exists(os.environ["FAKE_CLI_RELEASE"]) and time.time() < deadline:
        time.sleep(0.05)
if mode.startswith("report-"):
    path = re.search(r"^Report file: (.+)$", prompt, re.M).group(1)
    with open(path, "w", encoding="utf-8") as f:
        f.write("STATUS: " + mode[len("report-"):] + "\\nFictional report body\\n")
if mode.startswith("spawn-"):
    # A grandchild that keeps writing until it is ended, like a helper an agent CLI starts.
    subprocess.Popen([sys.executable, "-c", HEARTBEAT, os.environ["FAKE_CLI_HEARTBEAT"]])
    if mode == "spawn-hang":
        time.sleep(60)
    else:
        time.sleep(1)
sys.exit(1 if mode == "fail" else 0)
"""


def task(tid, deps=(), role="implement", **extra):
    return {"id": tid, "title": f"Fictional task {tid}", "role": role, "depends_on": list(deps), **extra}


class WbsTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = str(self.dir / "state.sqlite3")
        self.record = self.dir / "calls.jsonl"
        self.warn_dir = self.dir / "warnings"
        self.release = self.dir / "release"
        self.fake_cli = self.dir / "fake_cli.py"
        self.fake_cli.write_text(FAKE_CLI, encoding="utf-8")
        self.heartbeat = self.dir / "heartbeat"

    def cli(self, *argv):
        """Run a command in-process; returns (exit_code, parsed JSON stdout or None)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = wbs.main(["--db", self.db, *argv])
        return code, (json.loads(out.getvalue()) if out.getvalue() else None)

    def init(self, tasks):
        plan = self.dir / "plan.json"
        plan.write_text(json.dumps({"plan": {"name": "Fictional plan"}, "tasks": tasks}), encoding="utf-8")
        self.assertEqual(self.cli("init", "--plan", str(plan))[0], wbs.EXIT_OK)

    def ready_ids(self):
        return [t["id"] for t in self.cli("ready")[1]["ready"]]

    def write_config(self, extra_args=(), argv=None, timeout=60, report_dir=None, **entry):
        config = {
            "warning_dir": str(self.warn_dir),
            "clis": {"cli-a": {"argv": argv or [sys.executable, str(self.fake_cli), *extra_args],
                               "timeout_seconds": timeout, **entry}},
            "roles": {"implement": "cli-a"},
        }
        if report_dir is not None:
            config["report_dir"] = report_dir
        path = self.dir / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return str(path)

    def run_args(self, config, mode="ok", cli="cli-a"):
        """Command line and environment of `wbs.py run` as a separate process, as a scheduler starts it."""
        env = dict(os.environ, FAKE_CLI_RECORD=str(self.record), FAKE_CLI_MODE=mode, FAKE_SECRET=SECRET,
                   FAKE_CLI_RELEASE=str(self.release), FAKE_CLI_HEARTBEAT=str(self.heartbeat))
        return [sys.executable, str(SCRIPT), "--db", self.db, "run", "--config", config, "--cli", cli], env

    def run_cli(self, config, mode="ok", cli="cli-a"):
        """Run `wbs.py run` as a separate process, as a scheduler would."""
        argv, env = self.run_args(config, mode, cli)
        return subprocess.run(argv, capture_output=True, encoding="utf-8", env=env, timeout=120, check=False)

    def calls(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def reject_and_retry(self, tid):
        """A reviewer rejects the task, then a person explicitly returns it to pending."""
        code, out = self.cli("reject", "--task", tid, "--reviewer", "person", "--reason", "Fictional finding")
        self.assertEqual((code, out["result"]), (wbs.EXIT_OK, "failed"))
        self.assertEqual(self.cli("retry", "--task", tid)[0], wbs.EXIT_OK)


class PlanTest(WbsTestCase):
    def test_rejects_unknown_dependency_and_cycle(self):
        bad_plans = [[task("A", deps=["Z"])], [task("A", deps=["B"]), task("B", deps=["A"])]]
        for tasks in bad_plans:
            with self.subTest(tasks=tasks), self.assertRaises(wbs.WbsError):
                wbs.validate_plan({"plan": {"name": "Fictional plan"}, "tasks": tasks})

    def test_init_never_overwrites_existing_state(self):
        self.init([task("A")])
        plan = self.dir / "plan.json"
        self.assertEqual(self.cli("init", "--plan", str(plan))[0], wbs.EXIT_ERROR)
        self.assertEqual(self.ready_ids(), ["A"])


class PrerequisiteTest(WbsTestCase):
    def test_task_is_ready_only_after_all_prerequisites_are_done(self):
        self.init([task("A"), task("B", deps=["A"]), task("C", deps=["A", "B"])])
        self.assertEqual(self.ready_ids(), ["A"])
        self.assertEqual(self.cli("claim", "--worker", "w1", "--task", "B")[0], wbs.EXIT_ERROR)
        _, a = self.cli("claim", "--worker", "w1")
        self.assertEqual(a["task"], "A")
        self.assertEqual(self.ready_ids(), [])  # claimed is not done
        self.cli("complete", "--attempt", str(a["attempt"]))
        self.assertEqual(self.ready_ids(), ["B"])
        _, b = self.cli("claim", "--worker", "w1")
        self.cli("complete", "--attempt", str(b["attempt"]))
        self.assertEqual(self.ready_ids(), ["C"])


class ConcurrencyTest(WbsTestCase):
    def test_parallel_claimers_never_take_the_same_task(self):
        self.init([task(f"T{i:02d}") for i in range(30)])
        claimed, errors, lock = [], [], threading.Lock()
        barrier = threading.Barrier(6)

        def worker(name):
            conn = wbs.connect(self.db)
            try:
                barrier.wait()
                while True:
                    row, _ = wbs.claim(conn, name)
                    if row is None:
                        return
                    with lock:
                        claimed.append(row["id"])
            except Exception as exc:  # reported by the assertion below
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(claimed), [f"T{i:02d}" for i in range(30)])
        _, status = self.cli("status")
        self.assertEqual(status["counts"], {"claimed": 30})


class SingleCliRunTest(WbsTestCase):
    def test_two_workers_on_two_ready_tasks_start_only_one_cli(self):
        self.init([task("A"), task("B")])
        argv, env = self.run_args(self.write_config(), mode="wait")
        procs = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", env=env)
                 for _ in range(2)]
        try:
            # The refused worker exits at once; the started stand-in waits for the release file.
            deadline = time.monotonic() + 60
            while all(p.poll() is None for p in procs):
                self.assertLess(time.monotonic(), deadline, "neither worker finished")
                time.sleep(0.05)
        finally:
            self.release.touch()
            outputs = [p.communicate(timeout=120) for p in procs]
        outs = [json.loads(stdout) for stdout, _ in outputs]
        results = sorted((p.returncode, o["result"], o["cli_invoked"]) for p, o in zip(procs, outs))
        self.assertEqual(results, [(wbs.EXIT_OK, "awaiting_review", True), (wbs.EXIT_BLOCKED, "blocked", False)])
        self.assertIn("open CLI attempt", next(o["reason"] for o in outs if o["result"] == "blocked"))
        self.assertEqual(len(self.calls()), 1)
        status = self.cli("status")[1]
        self.assertEqual((status["counts"], status["cli_invocations"]), ({"awaiting_review": 1, "pending": 1}, 1))

    def test_open_attempt_left_by_a_crash_blocks_later_runs_but_not_people(self):
        self.init([task("A"), task("B")])
        config = self.write_config()
        with contextlib.closing(wbs.connect(self.db)) as conn:
            # A run that died after its CLI started: the attempt stays open and its result is unknown.
            _, attempt = wbs.claim(conn, "cli:cli-a", cli="cli-a")
            wbs.mark_started(conn, attempt)
        proc = self.run_cli(config)  # B is ready, but no second CLI may start
        out = json.loads(proc.stdout)
        self.assertEqual((proc.returncode, out["result"], out["cli_invoked"]), (wbs.EXIT_BLOCKED, "blocked", False))
        _, b = self.cli("claim", "--worker", "person")  # manual work is not limited
        self.assertEqual(b["task"], "B")
        self.assertEqual(self.cli("complete", "--attempt", str(b["attempt"]))[0], wbs.EXIT_OK)
        self.assertEqual(self.run_cli(config).returncode, wbs.EXIT_BLOCKED)
        # A person closes the uncertain attempt; the task then needs an explicit retry.
        self.assertEqual(self.cli("fail", "--attempt", str(attempt), "--reason", "Fictional crash")[0], wbs.EXIT_OK)
        self.assertEqual(self.run_cli(config).returncode, wbs.EXIT_BLOCKED)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.cli("retry", "--task", "A")[0], wbs.EXIT_OK)
        out = json.loads(self.run_cli(config).stdout)
        self.assertEqual((out["result"], out["task"], out["task_cli_calls"]), ("awaiting_review", "A", 2))
        self.assertEqual(len(self.calls()), 1)


class ParallelTestCase(WbsTestCase):
    def parallel_config(self, total=2, a=1, b=1):
        config = {
            "warning_dir": str(self.warn_dir),
            "max_parallel": total,
            "clis": {"cli-a": {"argv": [sys.executable, str(self.fake_cli)], "max_parallel": a},
                     "cli-b": {"argv": [sys.executable, str(self.fake_cli)], "max_parallel": b}},
            "roles": {"implement": "cli-a", "review": "cli-b"},
        }
        path = self.dir / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return str(path)

    def start_run(self, config, mode="wait", cli="cli-a"):
        argv, env = self.run_args(config, mode, cli)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", env=env)
        self.addCleanup(self.end_run, proc)
        return proc

    def end_run(self, proc):
        self.release.touch()
        if proc.poll() is None:
            proc.communicate(timeout=120)
        for stream in (proc.stdout, proc.stderr):
            stream.close()

    def wait_for_calls(self, count):
        deadline = time.monotonic() + 60
        while len(self.calls()) < count:
            self.assertLess(time.monotonic(), deadline, "the CLI did not start")
            time.sleep(0.05)

    def finish_runs(self, *procs):
        self.release.touch()
        return [json.loads(proc.communicate(timeout=120)[0]) for proc in procs]


class ParallelRunTest(ParallelTestCase):
    def test_free_cli_starts_while_another_cli_works(self):
        self.init([task("A", writes=["a"]), task("B", role="review", writes=["b"])])
        config = self.parallel_config()
        first = self.start_run(config, cli="cli-a")
        self.wait_for_calls(1)
        second = self.start_run(config, cli="cli-b")
        self.wait_for_calls(2)
        status = self.cli("status")[1]
        self.assertEqual([(o["task"], o["cli"], o["running"]) for o in status["open_attempts"]],
                         [("A", "cli-a", True), ("B", "cli-b", True)])
        outs = self.finish_runs(first, second)
        self.assertEqual([(o["result"], o["task"]) for o in outs], [("awaiting_review", "A"), ("awaiting_review", "B")])
        self.assertEqual(self.cli("status")[1]["open_attempts"], [])

    def test_same_cli_waits_as_busy_at_its_limit(self):
        self.init([task("A", writes=["a"]), task("B", writes=["b"])])
        config = self.parallel_config(a=1)
        first = self.start_run(config)
        self.wait_for_calls(1)
        proc = self.run_cli(config)
        out = json.loads(proc.stdout)
        self.assertEqual((proc.returncode, out["result"], out["cli_invoked"]), (wbs.EXIT_OK, "busy", False))
        self.assertIn("limit of cli 'cli-a' reached (1 of 1)", out["reason"])
        self.assertEqual(len(self.calls()), 1)
        self.finish_runs(first)

    def test_database_limit_also_makes_a_run_wait(self):
        self.init([task("A", writes=["a"]), task("B", role="review", writes=["b"])])
        config = self.parallel_config(total=1)
        first = self.start_run(config)
        self.wait_for_calls(1)
        out = json.loads(self.run_cli(config, cli="cli-b").stdout)
        self.assertEqual(out["result"], "busy")
        self.assertIn("database limit reached (1 of 1)", out["reason"])
        self.finish_runs(first)

    def test_simultaneous_runs_of_one_cli_take_different_tasks(self):
        self.init([task(f"T{i}", writes=[f"f{i}"]) for i in range(5)])
        config = self.parallel_config(total=3, a=3)
        procs = [self.start_run(config) for _ in range(3)]
        self.wait_for_calls(3)
        outs = self.finish_runs(*procs)
        self.assertEqual(sorted(o["task"] for o in outs), ["T0", "T1", "T2"])
        self.assertEqual(self.cli("status")[1]["counts"], {"awaiting_review": 3, "pending": 2})

    def test_failure_stops_new_runs_but_a_running_cli_finishes(self):
        self.init([task("A", writes=["a"]), task("B", role="review", writes=["b"]), task("C", writes=["c"])])
        config = self.parallel_config()
        first = self.start_run(config)
        self.wait_for_calls(1)
        self.assertEqual(self.run_cli(config, mode="fail", cli="cli-b").returncode, wbs.EXIT_BLOCKED)
        proc = self.run_cli(config, cli="cli-b")
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["result"]), (wbs.EXIT_BLOCKED, "blocked"))
        (out,) = self.finish_runs(first)
        self.assertEqual((out["task"], out["result"]), ("A", "awaiting_review"))
        counts = self.cli("status")[1]["counts"]
        self.assertEqual(counts, {"awaiting_review": 1, "failed": 1, "pending": 1})

    def test_killed_run_is_reported_for_a_person_and_not_counted_as_busy(self):
        self.init([task("A", writes=["a"]), task("B", writes=["b"])])
        config = self.parallel_config(a=2)
        first = self.start_run(config)
        self.wait_for_calls(1)
        first.kill()
        first.wait()
        proc = self.run_cli(config)
        out = json.loads(proc.stdout)
        self.assertEqual((proc.returncode, out["result"], out["cli_invoked"]), (wbs.EXIT_BLOCKED, "blocked", False))
        self.assertIn("no longer running", out["reason"])
        self.assertEqual(self.cli("status")[1]["open_attempts"][0]["running"], False)
        self.assertEqual(self.cli("fail", "--attempt", "1", "--reason", "Fictional crash")[0], wbs.EXIT_OK)
        self.assertEqual(self.cli("retry", "--task", "A")[0], wbs.EXIT_OK)
        self.assertEqual(json.loads(self.run_cli(config).stdout)["result"], "awaiting_review")

    def test_reused_process_id_is_not_taken_for_the_run(self):
        self.init([task("A", writes=["a"]), task("B", writes=["b"])])
        with contextlib.closing(wbs.connect(self.db)) as conn:
            _, attempt = wbs.claim(conn, "cli:cli-a", cli="cli-a")
            self.assertEqual(self.cli("status")[1]["open_attempts"][0]["running"], True)
            conn.execute("UPDATE attempts SET runner_start = 'another process' WHERE id = ?", (attempt,))
        out = json.loads(self.run_cli(self.parallel_config(a=2)).stdout)
        self.assertEqual(out["result"], "blocked")

    def test_task_counts_and_warnings_stay_right_when_runs_overlap(self):
        self.init([task("A", writes=["a"]), task("B", role="review", writes=["b"])])
        config = self.parallel_config()
        for round_no in range(1, 6):
            procs = [self.start_run(config, mode="ok", cli=cli) for cli in ("cli-a", "cli-b")]
            outs = {o["task"]: o for o in self.finish_runs(*procs)}
            self.assertEqual({t: o["task_cli_calls"] for t, o in outs.items()}, {"A": round_no, "B": round_no})
            if round_no < 5:
                self.assertEqual(list(self.warn_dir.glob("*")), [])
                self.reject_and_retry("A")
                self.reject_and_retry("B")
        self.assertEqual(len(list(self.warn_dir.glob("*"))), 2)
        self.assertEqual(self.cli("status")[1]["cli_invocations"], 10)

    def test_approval_tasks_and_review_gate_are_unchanged(self):
        self.init([task("A", requires_approval=True), task("B", writes=[])])
        config = self.parallel_config()
        out = json.loads(self.run_cli(config).stdout)
        self.assertEqual((out["task"], out["result"]), ("B", "awaiting_review"))
        self.assertEqual(self.cli("complete", "--attempt", str(out["attempt"]))[0], wbs.EXIT_ERROR)
        out = json.loads(self.run_cli(config).stdout)
        self.assertEqual((out["result"], out["cli_invoked"]), ("idle", False))
        self.assertEqual(len(self.calls()), 1)

    def test_invalid_limits_are_config_errors_and_start_no_cli(self):
        self.init([task("A")])
        for total, own in ((2, 4), (2, 0), (2, True), (2, "2"), (0, 1), (True, 1)):
            config = self.parallel_config(total=total, a=own)
            with self.subTest(total=total, own=own):
                self.assertEqual(self.run_cli(config).returncode, wbs.EXIT_ERROR)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.cli("status")[1]["counts"], {"pending": 1})

    def test_older_database_is_refused_with_a_clear_error(self):
        self.init([task("A")])
        with contextlib.closing(wbs.connect(self.db)) as conn:
            conn.execute("ALTER TABLE attempts DROP COLUMN runner_pid")
        proc = self.run_cli(self.parallel_config())
        self.assertEqual(proc.returncode, wbs.EXIT_ERROR)
        self.assertIn("older wbs.py", proc.stderr)


class WritesTest(ParallelTestCase):
    def test_overlap_rules(self):
        cases = (
            (None, [], True), ([], None, True), (None, None, True), ([], [], False), ([], ["a"], False),
            (["src/a.py"], ["src/b.py"], False), (["src/a.py"], ["SRC/A.PY"], True), (["src"], ["src/a.py"], True),
            (["src/*.py"], ["src/a.py"], True), (["src/*.py"], ["docs/a.md"], False), (["*.py"], ["docs/a.md"], True),
            (["src/a*/b.py"], ["src/c/d.py"], True), (["docs/**"], ["src/a.py"], False),
        )
        for first, second, expected in cases:
            with self.subTest(first=first, second=second):
                self.assertEqual(wbs.writes_overlap(first, second), expected)

    def test_plan_rejects_invalid_writes_and_normalizes_slashes(self):
        for writes in ("src", [""], [1], ["/etc/x"], ["C:/x"], ["C:\\x"], ["../x"], ["a/../../x"], ["\\\\host\\x"]):
            with self.subTest(writes=writes), self.assertRaises(wbs.WbsError):
                wbs.validate_plan({"plan": {"name": "Fictional plan"}, "tasks": [task("A", writes=writes)]})
        _, tasks = wbs.validate_plan({"plan": {"name": "Fictional plan"}, "tasks": [task("A", writes=["src\\a.py"])]})
        self.assertEqual(tasks[0]["writes"], ["src/a.py"])

    def test_overlapping_task_waits_and_the_next_free_task_starts(self):
        self.init([task("A", writes=["src"]), task("B", writes=["src/b.py"]), task("C", writes=["docs"])])
        config = self.parallel_config(a=2)
        first = self.start_run(config)
        self.wait_for_calls(1)
        second = self.start_run(config)
        self.wait_for_calls(2)
        proc = self.run_cli(self.parallel_config(total=3, a=3))
        out = json.loads(proc.stdout)
        self.assertEqual((proc.returncode, out["result"]), (wbs.EXIT_OK, "busy"))
        self.assertIn("['B']", out["reason"])
        self.assertEqual([o["task"] for o in self.finish_runs(first, second)], ["A", "C"])

    def test_task_without_writes_runs_alone_in_both_directions(self):
        for tasks in ([task("A"), task("B", writes=["b"])], [task("A", writes=["a"]), task("B")]):
            with self.subTest(tasks=tasks):
                Path(self.db).unlink(missing_ok=True)
                self.record.unlink(missing_ok=True)
                self.release.unlink(missing_ok=True)
                self.init(tasks)
                config = self.parallel_config(a=2)
                first = self.start_run(config)
                self.wait_for_calls(1)
                self.assertEqual(json.loads(self.run_cli(config).stdout)["result"], "busy")
                self.assertEqual(len(self.calls()), 1)
                self.finish_runs(first)

    def test_empty_writes_runs_next_to_other_tasks_and_the_prompt_says_so(self):
        self.init([task("A", writes=["src"]), task("B", role="review", writes=[])])
        config = self.parallel_config()
        first = self.start_run(config)
        self.wait_for_calls(1)
        second = self.start_run(config, cli="cli-b")
        self.wait_for_calls(2)
        self.finish_runs(first, second)
        prompts = [call["prompt"] for call in self.calls()]
        self.assertIn("Change only these project paths: src.", prompts[0])
        self.assertIn("Do not change any project file.", prompts[1])

    def test_writes_are_ignored_without_parallel_limits(self):
        self.init([task("A", writes=["a"]), task("B", writes=["b"])])
        config = self.write_config()
        argv, env = self.run_args(config, mode="wait")
        first = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", env=env)
        self.addCleanup(self.end_run, first)
        self.wait_for_calls(1)
        proc = self.run_cli(config)
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["result"]), (wbs.EXIT_BLOCKED, "blocked"))
        self.finish_runs(first)


class NoWorkTest(WbsTestCase):
    def test_nothing_ready_or_all_done_never_starts_the_cli(self):
        self.init([task("A"), task("B", deps=["A"])])
        config = self.write_config()
        _, a = self.cli("claim", "--worker", "person")
        proc = self.run_cli(config)  # B still waits for A
        self.assertEqual(proc.returncode, wbs.EXIT_OK, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["reason"], "no ready task")
        self.cli("complete", "--attempt", str(a["attempt"]))
        _, b = self.cli("claim", "--worker", "person")
        self.cli("complete", "--attempt", str(b["attempt"]))
        out = json.loads(self.run_cli(config).stdout)
        self.assertEqual((out["reason"], out["cli_invoked"]), ("all tasks done", False))
        self.assertEqual(self.calls(), [])

    def test_approval_task_is_left_for_a_person(self):
        self.init([task("A", requires_approval=True)])
        out = json.loads(self.run_cli(self.write_config()).stdout)
        self.assertFalse(out["cli_invoked"])
        self.assertIn("approval", out["reason"])
        _, a = self.cli("claim", "--worker", "person", "--task", "A")
        self.assertEqual(self.cli("complete", "--attempt", str(a["attempt"]))[0], wbs.EXIT_OK)
        self.assertEqual(self.cli("status")[1]["counts"], {"done": 1})
        self.assertEqual(self.calls(), [])


class PerTaskCounterTest(WbsTestCase):
    def test_each_task_counts_its_own_cli_starts_and_warns_at_its_fifth(self):
        self.init([task("A"), task("B")])
        config = self.write_config()
        for round_no in range(1, 6):
            outs = [json.loads(self.run_cli(config).stdout) for _ in range(2)]
            self.assertEqual([(o["task"], o["task_cli_calls"]) for o in outs], [("A", round_no), ("B", round_no)])
            if round_no < 5:
                # Round 3 includes the fifth start overall; no single task has reached five yet.
                self.assertEqual(list(self.warn_dir.glob("*")), [])
                self.reject_and_retry("A")
                self.reject_and_retry("B")
        self.assertTrue(all(o["warning_file"] for o in outs))
        texts = sorted(p.read_text(encoding="utf-8") for p in self.warn_dir.glob("*"))
        self.assertEqual(len(texts), 2)
        for text, tid in zip(texts, ("A", "B")):
            self.assertIn(f"task: {tid}\n", text)
            self.assertIn("task_cli_calls: 5\n", text)
        tasks = self.cli("status")[1]["tasks"]
        self.assertEqual({t["id"]: t["cli_calls"] for t in tasks}, {"A": 5, "B": 5})
        self.assertEqual(len(self.calls()), 10)


class PrivacyAndPermissionTest(WbsTestCase):
    def test_prompt_goes_to_stdin_and_cli_output_is_neither_printed_nor_stored(self):
        self.init([task("A", description="Fictional description text")])
        proc = self.run_cli(self.write_config(), mode="leak")
        self.assertEqual(proc.returncode, wbs.EXIT_OK, proc.stderr)
        (call,) = self.calls()
        self.assertEqual(call["argv"], [])  # the prompt is not in the arguments
        self.assertIn("Fictional task A", call["prompt"])
        self.assertIn("Fictional description text", call["prompt"])
        for stream in (proc.stdout, proc.stderr):
            self.assertNotIn(SECRET, stream)
            self.assertNotIn("leaked", stream)
        self.assertNotIn(SECRET.encode(), Path(self.db).read_bytes())

    def test_bypass_arguments_and_unconfigured_cli_are_refused(self):
        self.init([task("A")])
        cases = ((["--dangerously-skip-permissions"], "cli-a"), ((), "cli-b"))
        for extra_args, cli in cases:
            with self.subTest(cli=cli):
                config = self.write_config(extra_args)
                self.assertEqual(self.run_cli(config, cli=cli).returncode, wbs.EXIT_ERROR)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.cli("status")[1]["counts"], {"pending": 1})

    def test_permissions_beyond_policy_a_and_unreviewed_wrapper_are_refused(self):
        refused = (["--sandbox", "danger-full-access"], ["-a", "never"], ["--ask-for-approval=never"],
                   ["--sandbox", "danger-full-access", "-a", "never"], ["-s", "workspace-write", "-s", "x", "-a", "never"],
                   ["--approve-for-me"], ["--permission-mode", "bypassPermissions"],
                   ["--permission-mode", "auto"], ["--permission-mode=auto"])
        for extra_args in refused:
            with self.subTest(args=extra_args), self.assertRaises(wbs.WbsError):
                wbs.load_cli(self.write_config(extra_args), "cli-a")
        allowed = (["--sandbox", "workspace-write", "--ask-for-approval", "on-request"],
                   ["--sandbox", "workspace-write", "-a", "never"], ["-s", "read-only", "--ask-for-approval=never"],
                   ["--permission-mode", "acceptEdits"], ["--permission-mode", "dontAsk"])
        for extra_args in allowed:
            with self.subTest(args=extra_args):
                self.assertTrue(wbs.load_cli(self.write_config(extra_args), "cli-a"))
        wrapper = [str(self.dir / "agent.cmd")]
        with self.assertRaises(wbs.WbsError):
            wbs.load_cli(self.write_config(argv=wrapper), "cli-a")
        self.assertTrue(wbs.load_cli(self.write_config(argv=wrapper, wrapper_reviewed=True), "cli-a"))


class ProcessTreeTest(WbsTestCase):
    def assert_heartbeat_stopped(self):
        self.assertTrue(self.heartbeat.exists(), "the stand-in never started its grandchild")
        size = self.heartbeat.stat().st_size
        time.sleep(1)
        self.assertEqual(self.heartbeat.stat().st_size, size, "a process the CLI started is still running")

    def test_timeout_ends_every_process_the_cli_started(self):
        self.init([task("A")])
        proc = self.run_cli(self.write_config(timeout=3), mode="spawn-hang")
        out = json.loads(proc.stdout)
        self.assertEqual((proc.returncode, out["result"], out["exit_code"]), (wbs.EXIT_BLOCKED, "failed", None))
        self.assert_heartbeat_stopped()

    def test_processes_left_after_a_normal_exit_are_ended(self):
        self.init([task("A")])
        out = json.loads(self.run_cli(self.write_config(), mode="spawn-exit").stdout)
        self.assertEqual(out["result"], "awaiting_review")
        self.assert_heartbeat_stopped()


class ReportTest(WbsTestCase):
    def test_report_status_decides_between_review_and_stop(self):
        self.init([task("A"), task("B")])
        config = self.write_config(report_dir=str(self.dir / "reports"))
        out = json.loads(self.run_cli(config, mode="report-done").stdout)
        self.assertEqual((out["result"], out["report"]), ("awaiting_review", "done"))
        self.assertIn("Fictional report body", Path(out["report_file"]).read_text(encoding="utf-8"))
        proc = self.run_cli(config, mode="report-needs-approval")
        out = json.loads(proc.stdout)
        self.assertEqual((proc.returncode, out["result"], out["exit_code"], out["report"]),
                         (wbs.EXIT_BLOCKED, "failed", 0, "needs-approval"))
        self.assertNotIn(b"Fictional report body", Path(self.db).read_bytes())  # only the status is kept

    def test_missing_report_is_flagged_but_still_goes_to_review(self):
        self.init([task("A")])
        out = json.loads(self.run_cli(self.write_config(report_dir=str(self.dir / "reports"))).stdout)
        self.assertEqual((out["result"], out["report"]), ("awaiting_review", "missing"))
        self.assertIn("Report file: ", self.calls()[0]["prompt"])


class FailureTest(WbsTestCase):
    def test_failed_call_stops_all_scheduling_until_retry(self):
        self.init([task("A"), task("B")])
        config = self.write_config()
        proc = self.run_cli(config, mode="fail")
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["result"]), (wbs.EXIT_BLOCKED, "failed"))
        proc = self.run_cli(config)  # B is independent but must not start
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["result"]), (wbs.EXIT_BLOCKED, "blocked"))
        self.assertEqual(self.cli("claim", "--worker", "person")[0], wbs.EXIT_BLOCKED)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.cli("retry", "--task", "A")[0], wbs.EXIT_OK)
        self.assertEqual(self.run_cli(config).returncode, wbs.EXIT_OK)
        self.assertEqual(len(self.calls()), 2)

    def test_cli_that_cannot_start_is_not_counted_and_blocks(self):
        self.init([task("A"), task("B")])
        config = self.write_config(argv=[str(self.dir / "missing-cli-program")])
        proc = self.run_cli(config)
        out = json.loads(proc.stdout)
        self.assertEqual((proc.returncode, out["result"], out["cli_invoked"], out["task_cli_calls"]),
                         (wbs.EXIT_BLOCKED, "failed", False, 0))
        self.assertEqual(self.run_cli(config).returncode, wbs.EXIT_BLOCKED)  # no automatic retry
        status = self.cli("status")[1]
        self.assertEqual((status["counts"], status["cli_invocations"]), ({"failed": 1, "pending": 1}, 0))
        self.assertEqual(list(self.warn_dir.glob("*")), [])

    def test_timeout_is_counted_fails_and_is_not_retried(self):
        self.init([task("A"), task("B")])
        config = self.write_config(timeout=2)
        proc = self.run_cli(config, mode="hang")
        out = json.loads(proc.stdout)
        self.assertEqual((proc.returncode, out["result"], out["cli_invoked"], out["task_cli_calls"]),
                         (wbs.EXIT_BLOCKED, "failed", True, 1))
        self.assertEqual(self.run_cli(config).returncode, wbs.EXIT_BLOCKED)
        self.assertLessEqual(len(self.calls()), 1)  # the stand-in may be killed before it records


class ReviewGateTest(WbsTestCase):
    def test_exit_zero_still_requires_explicit_acceptance(self):
        self.init([task("A"), task("B", deps=["A"])])
        config = self.write_config()
        proc = self.run_cli(config)
        out = json.loads(proc.stdout)
        self.assertEqual((proc.returncode, out["result"], out["exit_code"]), (wbs.EXIT_OK, "awaiting_review", 0))
        self.assertEqual(self.ready_ids(), [])  # exit code 0 does not release B
        self.assertEqual(self.cli("complete", "--attempt", str(out["attempt"]))[0], wbs.EXIT_ERROR)
        idle = json.loads(self.run_cli(config).stdout)
        self.assertFalse(idle["cli_invoked"])
        self.assertIn("awaiting review", idle["reason"])
        code, accepted = self.cli("accept", "--task", "A", "--reviewer", "person")
        self.assertEqual((code, accepted["result"]), (wbs.EXIT_OK, "done"))
        self.assertEqual(self.ready_ids(), ["B"])
        self.assertEqual(self.cli("accept", "--task", "A", "--reviewer", "person")[0], wbs.EXIT_ERROR)
        self.assertEqual(len(self.calls()), 1)

    def test_rejection_blocks_scheduling_until_explicit_retry(self):
        self.init([task("A"), task("B")])
        config = self.write_config()
        self.run_cli(config)
        code, out = self.cli("reject", "--task", "A", "--reviewer", "person", "--reason", "Fictional finding")
        self.assertEqual((code, out["result"]), (wbs.EXIT_OK, "failed"))
        proc = self.run_cli(config)  # B is independent but must not start
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["result"]), (wbs.EXIT_BLOCKED, "blocked"))
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.cli("retry", "--task", "A")[0], wbs.EXIT_OK)
        out = json.loads(self.run_cli(config).stdout)
        self.assertEqual((out["task"], out["task_cli_calls"]), ("A", 2))


if __name__ == "__main__":
    unittest.main()
