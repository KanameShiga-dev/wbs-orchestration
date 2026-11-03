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
import json, os, sys, time
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

    def write_config(self, extra_args=(), argv=None, timeout=60):
        config = {
            "warning_dir": str(self.warn_dir),
            "clis": {"cli-a": {"argv": argv or [sys.executable, str(self.fake_cli), *extra_args],
                               "timeout_seconds": timeout}},
            "roles": {"implement": "cli-a"},
        }
        path = self.dir / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return str(path)

    def run_args(self, config, mode="ok", cli="cli-a"):
        """Command line and environment of `wbs.py run` as a separate process, as a scheduler starts it."""
        env = dict(os.environ, FAKE_CLI_RECORD=str(self.record), FAKE_CLI_MODE=mode, FAKE_SECRET=SECRET,
                   FAKE_CLI_RELEASE=str(self.release))
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
