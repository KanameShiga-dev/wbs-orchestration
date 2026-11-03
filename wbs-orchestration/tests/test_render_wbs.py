"""Tests for scripts/render_wbs.py. Standard library only; all data is fictional."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import render_wbs  # noqa: E402
import wbs  # noqa: E402

PLAN_NAME = "Fictional <b>plan</b>"
TITLE = "Fictional <script>alert(\"x\")</script> & 'q'"
NOTE = "Fictional completion note"


class RenderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = str(self.dir / "state.sqlite3")
        self.out = self.dir / "status.html"

    def run_wbs(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(wbs.main(["--db", self.db, *argv]), wbs.EXIT_OK)
        return json.loads(out.getvalue())

    def render(self, db=None, output=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = render_wbs.main(["--db", db or self.db, "--output", str(output or self.out)])
        return code, out.getvalue(), err.getvalue()

    def make_db(self):
        """Fictional state: T1 done, T2 claimed, T3 waits for T1 and T2, T4 ready."""
        tasks = [
            {"id": "T1", "title": TITLE, "role": "implement"},
            {"id": "T2", "title": "Fictional task T2", "role": "review", "depends_on": ["T1"]},
            {"id": "T3", "title": "Fictional task T3", "role": "implement", "depends_on": ["T1", "T2"]},
            {"id": "T4", "title": "Fictional task T4", "role": "owner", "requires_approval": True},
        ]
        plan = self.dir / "plan.json"
        plan.write_text(json.dumps({"plan": {"name": PLAN_NAME}, "tasks": tasks}), encoding="utf-8")
        self.run_wbs("init", "--plan", str(plan))
        first = self.run_wbs("claim", "--worker", "person")
        self.run_wbs("complete", "--attempt", str(first["attempt"]), "--note", NOTE)
        self.run_wbs("claim", "--worker", "person", "--task", "T2")

    def page(self):
        return self.out.read_text(encoding="utf-8")

    def test_page_escapes_text_and_lists_every_task_in_plan_order(self):
        self.make_db()
        code, stdout, _ = self.render()
        self.assertEqual((code, json.loads(stdout)["tasks"]), (render_wbs.EXIT_OK, 4))
        page = self.page()
        self.assertNotIn("<script", page)
        self.assertNotIn("<b>", page)
        self.assertIn("Fictional &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &#x27;q&#x27;", page)
        self.assertIn("<title>WBS status: Fictional &lt;b&gt;plan&lt;/b&gt;</title>", page)
        rows = {line.split('"')[1]: line for line in page.splitlines() if line.startswith("<tr data-task=")}
        self.assertEqual(list(rows), ["T1", "T2", "T3", "T4"])
        expected = {"T1": ("implement", "done", "-", "no"), "T2": ("review", "claimed", "T1", "no"),
                    "T3": ("implement", "pending", "T1, T2", "no"), "T4": ("owner", "pending", "-", "yes")}
        for tid, (role, status, deps, approval) in expected.items():
            with self.subTest(task=tid):
                self.assertIn(f"<td>{role}</td><td>{status}</td><td>{deps}</td><td>{approval}</td>", rows[tid])
        self.assertNotIn(NOTE, page)  # attempt notes and events are never read
        self.assertNotIn("worker=", page)

    def test_progress_counters_match_the_database(self):
        self.make_db()
        self.assertEqual(self.render()[0], render_wbs.EXIT_OK)
        page = self.page()
        self.assertIn('<progress value="1" max="4"></progress> Done 1 of 4 tasks', page)
        counts = {"pending": 2, "claimed": 1, "awaiting_review": 0, "done": 1, "failed": 0}
        for status, number in counts.items():
            self.assertIn(f'<th scope="row">{status}</th><td class="num">{number}</td>', page)
        self.assertIn("<p>Ready: T4</p>", page)
        self.assertIn("<p>CLI starts (all tasks): 0</p>", page)

    def test_database_is_unchanged_and_existing_output_is_replaced_whole(self):
        self.make_db()
        before = Path(self.db).read_bytes()
        self.out.write_text("old page", encoding="utf-8")
        self.assertEqual(self.render()[0], render_wbs.EXIT_OK)
        self.assertEqual(Path(self.db).read_bytes(), before)
        self.assertTrue(self.page().startswith("<!DOCTYPE html>"))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["plan.json", "state.sqlite3", "status.html"])

    def test_missing_database_fails_and_writes_nothing(self):
        code, stdout, stderr = self.render(db=str(self.dir / "missing.sqlite3"))
        self.assertEqual((code, stdout), (render_wbs.EXIT_ERROR, ""))
        self.assertIn("database not found", stderr)
        self.assertEqual(list(self.dir.iterdir()), [])  # no output, no temporary file, no new database

    def test_output_that_is_the_database_is_rejected_before_writing(self):
        self.make_db()
        (self.dir / "sub").mkdir()
        alias = self.dir / "alias.sqlite3"
        os.link(self.db, alias)  # hard link: a second existing name for the same database file
        before = Path(self.db).read_bytes()
        names = sorted(p.name for p in self.dir.iterdir())
        for output in (self.db, self.dir / "sub" / ".." / "state.sqlite3", alias):
            with self.subTest(output=str(output)):
                code, stdout, stderr = self.render(output=output)
                self.assertEqual((code, stdout), (render_wbs.EXIT_ERROR, ""))
                self.assertIn("output path is the database file", stderr)
                self.assertEqual(Path(self.db).read_bytes(), before)
                self.assertEqual(sorted(p.name for p in self.dir.iterdir()), names)  # no temporary file

    def test_script_runs_as_a_separate_process(self):
        self.make_db()
        proc = subprocess.run([sys.executable, str(SCRIPTS / "render_wbs.py"), "--db", self.db,
                               "--output", str(self.out)], capture_output=True, encoding="utf-8",
                              timeout=60, check=False)
        self.assertEqual(proc.returncode, render_wbs.EXIT_OK, proc.stderr)
        self.assertIn("Fictional task T4", self.page())


if __name__ == "__main__":
    unittest.main()
