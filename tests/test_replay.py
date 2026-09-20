"""只读回放：时间线记录、工况物化、关键标记与只读隔离。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
import urllib.error
import urllib.request

from flashsmelter.console import ConsoleApp, ConsoleServer
from flashsmelter.errors import IntegrityError, LatchEngagedError, ReadOnlyError, ValidationError
from flashsmelter.replay import TIMELINE_STREAM, ReplaySession
from flashsmelter.runtime import iso_from_epoch
from flashsmelter.store import ReadOnlyStore

from .helpers import make_app, make_root, start_furnace


def _ignite(app) -> None:
    app.burner.ignite("tester", fuel_pressure_kpa=200.0, air_flow_nm3h=5200.0)


class ReplayTimelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_state_persists_are_recorded_on_timeline(self) -> None:
        self.assertEqual(0, self.app.replay().length())
        _ignite(self.app)
        session = self.app.replay()
        self.assertEqual(1, session.length())
        steps = session.steps()
        self.assertEqual(1, len(steps))
        step = steps[0]
        self.assertEqual("snapshot", step["kind"])
        self.assertEqual("burner", step["component"])
        self.assertEqual("ignite", step["action"])
        self.assertEqual("tester", step["actor"])
        self.assertEqual("ok", step["outcome"])
        self.assertFalse(step["critical"])

    def test_materialized_state_matches_each_step(self) -> None:
        _ignite(self.app)
        self.app.clock.advance(5)
        self.app.burner.confirm_flame("tester")
        self.app.clock.advance(5)
        self.app.burner.stabilize("tester")
        session = self.app.replay()
        self.assertEqual(3, session.length())
        self.assertEqual({}, session.state_at(0)["components"])
        self.assertEqual("preheating", session.state_at(1)["components"]["burner"]["snapshot"]["state"])
        self.assertEqual("ignited", session.state_at(2)["components"]["burner"]["snapshot"]["state"])
        self.assertEqual("stable", session.state_at(3)["components"]["burner"]["snapshot"]["state"])
        self.assertTrue(session.state_at(3)["read_only"])
        with self.assertRaises(ValidationError):
            session.state_at(4)

    def test_full_startup_materializes_all_components(self) -> None:
        start_furnace(self.app)
        session = self.app.replay()
        view = session.state_at(session.length())
        for name in ("furnace", "burner", "oxygen", "waste"):
            self.assertIn(name, view["components"])
        self.assertEqual("oxygen_ready", view["components"]["furnace"]["snapshot"]["state"])

    def test_step_at_time_locates_by_clock(self) -> None:
        base = self.app.clock.timestamp()
        _ignite(self.app)
        self.app.clock.advance(10)
        self.app.burner.confirm_flame("tester")
        session = self.app.replay()
        self.assertEqual(0, session.step_at_time(iso_from_epoch(base - 1)))
        self.assertEqual(1, session.step_at_time(iso_from_epoch(base + 5)))
        self.assertEqual(2, session.step_at_time(iso_from_epoch(base + 10)))
        self.assertEqual(2, session.step_at_time(iso_from_epoch(base + 999)))
        view = session.state_at_time(iso_from_epoch(base + 5))
        self.assertEqual(1, view["step"])
        self.assertEqual("preheating", view["components"]["burner"]["snapshot"]["state"])

    def test_rejected_attempt_is_recorded_as_critical(self) -> None:
        _ignite(self.app)
        self.app.clock.advance(5)
        self.app.burner.trip("tester", reason="燃料压力骤降")
        with self.assertRaises(LatchEngagedError):
            _ignite(self.app)
        session = self.app.replay()
        events = session.steps()
        attempts = [event for event in events if event["kind"] == "attempt"]
        self.assertEqual(1, len(attempts))
        attempt = attempts[0]
        self.assertEqual("rejected", attempt["outcome"])
        self.assertEqual("latch-engaged", attempt["reason"])
        self.assertTrue(attempt["critical"])
        self.assertEqual(2, attempt["after_step"])
        trip_step = next(event for event in events if event["kind"] == "snapshot" and event["action"] == "trip")
        self.assertTrue(trip_step["critical"])
        self.assertIn("verb:trip", trip_step["reasons"])
        critical = session.steps(critical_only=True)
        self.assertTrue(critical)
        self.assertTrue(all(event["critical"] for event in critical))
        self.assertFalse(any(event["action"] == "ignite" and event["kind"] == "snapshot" for event in critical))

    def test_manual_marks_flag_steps_and_persist(self) -> None:
        _ignite(self.app)
        self.app.mark_replay_step(step=1, note="点火后燃料压力开始漂移", actor="investigator")
        session = self.app.replay()
        step = session.steps()[0]
        self.assertTrue(step["critical"])
        self.assertIn("marked", step["reasons"])
        self.assertEqual("点火后燃料压力开始漂移", step["marks"][0]["note"])
        marks = session.marks()
        self.assertEqual(1, len(marks))
        self.assertEqual(1, marks[0]["step"])
        self.assertEqual("investigator", marks[0]["actor"])
        with self.assertRaises(ValidationError):
            self.app.mark_replay_step(step=0, note="x", actor="a")
        with self.assertRaises(ValidationError):
            self.app.mark_replay_step(step=1, note="  ", actor="a")

    def test_replay_is_structurally_read_only(self) -> None:
        _ignite(self.app)
        readonly = ReadOnlyStore(self.app.store)
        with self.assertRaises(ReadOnlyError):
            readonly.put("a/b", {"x": 1})
        with self.assertRaises(ReadOnlyError):
            readonly.commit_intent("a/b", {"x": 1})
        with self.assertRaises(ReadOnlyError):
            readonly.append("a/b", {"x": 1})
        with self.assertRaises(ValidationError):
            ReplaySession(self.app.store, self.app.namespace)  # type: ignore[arg-type] 故意传可写库
        before_docs = self.app.store.snapshot()
        before_audit = self.app.audit.length()
        before_timeline = self.app.store.stream_length(TIMELINE_STREAM)
        session = self.app.replay()
        session.steps()
        session.state_at(1)
        session.state_at_time(iso_from_epoch(self.app.clock.timestamp()))
        session.marks()
        self.assertEqual(before_docs, self.app.store.snapshot())
        self.assertEqual(before_audit, self.app.audit.length())
        self.assertEqual(before_timeline, self.app.store.stream_length(TIMELINE_STREAM))

    def test_empty_timeline_replays_cleanly(self) -> None:
        session = self.app.replay()
        self.assertEqual(0, session.length())
        self.assertEqual([], session.steps())
        self.assertEqual([], session.marks())
        view = session.state_at(0)
        self.assertEqual(0, view["step"])
        self.assertEqual({}, view["components"])
        self.assertEqual(0, session.step_at_time(iso_from_epoch(self.app.clock.timestamp())))

    def test_tampered_timeline_is_detected(self) -> None:
        _ignite(self.app)
        path = self.app.store.journal_root / "timeline" / "snapshots.jsonl"
        entry = json.loads(path.read_text(encoding="utf-8").strip())
        entry["payload"]["payload"]["state"] = "tampered"
        path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            self.app.replay()


class ReplayCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root("flashsmelter-replay-cli-")

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "flashsmelter", "--root", str(self.root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )

    def test_replay_via_cli(self) -> None:
        ignited = self._run(
            "call", "burner.ignite", "--param", "fuel_pressure_kpa=200", "--param", "air_flow_nm3h=5200"
        )
        self.assertEqual(0, ignited.returncode, ignited.stderr)
        steps = self._run("replay", "steps")
        self.assertEqual(0, steps.returncode, steps.stderr)
        payload = json.loads(steps.stdout)
        self.assertEqual(1, payload["length"])
        self.assertEqual("ignite", payload["events"][0]["action"])
        show = self._run("replay", "show", "--step", "1")
        self.assertEqual(0, show.returncode, show.stderr)
        view = json.loads(show.stdout)
        self.assertEqual("preheating", view["components"]["burner"]["snapshot"]["state"])
        self.assertTrue(view["read_only"])
        mark = self._run("replay", "mark", "--step", "1", "--note", "关键一步", "--actor", "investigator")
        self.assertEqual(0, mark.returncode, mark.stderr)
        marks = self._run("replay", "marks")
        self.assertEqual(1, json.loads(marks.stdout)["count"])
        critical = self._run("replay", "steps", "--critical-only")
        self.assertEqual(1, json.loads(critical.stdout)["count"])

    def test_replay_without_subcommand_prints_help(self) -> None:
        completed = self._run("replay")
        self.assertEqual(1, completed.returncode)
        self.assertIn("steps", completed.stdout)


class ReplayConsoleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = make_app()
        _ignite(cls.app)
        cls.console = ConsoleApp(cls.app)
        cls.server = ConsoleServer(cls.console, host="127.0.0.1", port=0)
        cls.host, cls.port = cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def _request(self, method: str, path: str):
        url = f"http://{self.host}:{self.port}{path}"
        request = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_replay_endpoints(self) -> None:
        status, payload = self._request("GET", "/api/replay/steps")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["length"])
        status, view = self._request("GET", "/api/replay/state?step=1")
        self.assertEqual(200, status)
        self.assertEqual("preheating", view["components"]["burner"]["snapshot"]["state"])
        status, marks = self._request("GET", "/api/replay/marks")
        self.assertEqual(200, status)
        self.assertEqual(0, marks["count"])

    def test_replay_endpoints_reject_writes(self) -> None:
        for path in ("/api/replay/steps", "/api/replay/state", "/api/replay/marks"):
            status, payload = self._request("POST", path)
            self.assertEqual(405, status)
            self.assertEqual("method-not-allowed", payload["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
