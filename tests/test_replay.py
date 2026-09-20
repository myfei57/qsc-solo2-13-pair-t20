"""工况回放：逐帧捕获、关键步标记、按时间定位与只读约束。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from flashsmelter.console import ConsoleApp
from flashsmelter.errors import FlashSmelterError, NotFoundError, ValidationError
from flashsmelter.replay import ReadOnlyStore
from flashsmelter.runtime import iso_from_epoch

from .helpers import make_app, make_root, run_heat, start_furnace

ALL_COMPONENTS = ("furnace", "burner", "oxygen", "conc", "settler", "slag", "matte", "conv", "waste")


def run_cli(*args: str, root):
    return subprocess.run(
        [sys.executable, "-m", "flashsmelter", "--root", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


class ReplayCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_every_action_leaves_a_frame_with_full_conditions(self) -> None:
        self.app.burner.ignite("tester", fuel_pressure_kpa=200.0, air_flow_nm3h=5200.0)
        self.assertEqual(1, self.app.replay.length())
        frame = self.app.replay.frame(1)
        self.assertEqual("burner.ignite", frame["trigger"]["qualified"])
        self.assertEqual("ok", frame["trigger"]["outcome"])
        self.assertEqual(1, frame["trigger"]["audit_seq"])
        self.assertEqual("preheating", frame["conditions"]["burner"]["state"])
        for name in ALL_COMPONENTS:
            self.assertIn(name, frame["conditions"])
        self.assertIn("heat_id", frame["heat"])
        self.assertIn("generation", frame)

    def test_frame_is_point_in_time_snapshot(self) -> None:
        self.app.burner.ignite("tester", fuel_pressure_kpa=200.0, air_flow_nm3h=5200.0)
        self.app.burner.confirm_flame("tester")
        self.app.burner.stabilize("tester")
        # 每一步定格当时的工况，不随后续动作变化
        self.assertEqual("preheating", self.app.replay.frame(1)["conditions"]["burner"]["state"])
        self.assertEqual("ignited", self.app.replay.frame(2)["conditions"]["burner"]["state"])
        self.assertEqual("stable", self.app.replay.frame(3)["conditions"]["burner"]["state"])
        self.assertEqual("preheating", self.app.replay.frame(1)["conditions"]["burner"]["state"])

    def test_first_frame_has_no_transition_baseline(self) -> None:
        self.app.burner.ignite("tester", fuel_pressure_kpa=200.0, air_flow_nm3h=5200.0)
        self.assertEqual([], self.app.replay.frame(1)["critical"])

    def test_rejected_action_is_captured_and_marked(self) -> None:
        with self.assertRaises(FlashSmelterError):
            self.app.conc.inject("tester", rate_tph=100.0, tons=10.0)
        frame = self.app.replay.frame(1)
        self.assertEqual("rejected", frame["trigger"]["outcome"])
        self.assertEqual("cold", frame["conditions"]["furnace"]["state"])
        rules = [mark["rule"] for mark in frame["critical"]]
        self.assertIn("action-rejected", rules)

    def test_fault_latch_is_marked(self) -> None:
        self.app.burner.ignite("tester", fuel_pressure_kpa=200.0, air_flow_nm3h=5200.0)
        self.app.burner.trip("tester", reason="燃料压力突降")
        frame = self.app.replay.frame(2)
        self.assertEqual("fault_latched", frame["conditions"]["burner"]["state"])
        rules = [mark["rule"] for mark in frame["critical"]]
        self.assertIn("fault-latch", rules)

    def test_furnace_transition_is_marked(self) -> None:
        start_furnace(self.app)
        overview = self.app.replay.overview()
        self.assertGreater(overview["frames"], 1)
        critical = self.app.replay.critical()
        rules = {mark["rule"] for step in critical for mark in step["critical"]}
        self.assertIn("furnace-transition", rules)
        last = self.app.replay.frame(overview["frames"])
        self.assertEqual("oxygen_ready", last["conditions"]["furnace"]["state"])
        # 每个动作一帧，与审计流水一一对应
        self.assertEqual(self.app.audit.length(), self.app.replay.length())

    def test_limit_breach_is_marked(self) -> None:
        self.app.waste.start("tester", drum_level=0.6)
        self.app.waste.update("tester", drum_level=0.2, exhaust_temp_c=300.0, steam_flow_tph=10.0)
        frame = self.app.replay.frame(2)
        self.assertEqual("latched", frame["conditions"]["waste"]["state"])
        rules = [mark["rule"] for mark in frame["critical"]]
        self.assertIn("fault-latch", rules)
        self.assertIn("drum-level-low", rules)


class ReplaySeekTest(unittest.TestCase):
    def test_seek_by_time(self) -> None:
        app = make_app()
        t0 = app.clock.timestamp()
        app.burner.ignite("tester", fuel_pressure_kpa=200.0, air_flow_nm3h=5200.0)
        app.clock.advance(100.0)
        app.burner.confirm_flame("tester")
        app.clock.advance(100.0)
        app.burner.stabilize("tester")
        self.assertEqual(1, app.replay.seek(iso_from_epoch(t0 + 50))["seq"])
        self.assertEqual(2, app.replay.seek(iso_from_epoch(t0 + 150))["seq"])
        self.assertEqual(3, app.replay.seek(iso_from_epoch(t0 + 500))["seq"])
        with self.assertRaises(NotFoundError):
            app.replay.seek(iso_from_epoch(t0 - 1))

    def test_frame_seq_must_be_positive(self) -> None:
        app = make_app()
        app.burner.ignite("tester", fuel_pressure_kpa=200.0, air_flow_nm3h=5200.0)
        with self.assertRaises(ValidationError):
            app.replay.frame(0)
        with self.assertRaises(NotFoundError):
            app.replay.frame(99)


class ReplayMarkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.app.burner.ignite("tester", fuel_pressure_kpa=200.0, air_flow_nm3h=5200.0)

    def test_manual_mark(self) -> None:
        mark = self.app.mark_replay_step(frame_seq=1, note="点火参数偏离规程", actor="investigator")
        self.assertEqual(1, mark["frame_seq"])
        step = self.app.replay.steps()[-1]
        self.assertEqual("点火参数偏离规程", step["marks"][0]["note"])
        self.assertEqual("investigator", step["marks"][0]["actor"])
        # 人工标记后该步进入关键步列表
        self.assertIn(1, [item["seq"] for item in self.app.replay.critical()])
        # 标记本身进审计，但不产生新帧
        self.assertEqual(1, len(self.app.audit_events(action="replay.mark")))
        self.assertEqual(1, self.app.replay.length())

    def test_mark_requires_existing_frame_and_note(self) -> None:
        with self.assertRaises(NotFoundError):
            self.app.mark_replay_step(frame_seq=99, note="不存在的帧", actor="investigator")
        with self.assertRaises(ValidationError):
            self.app.mark_replay_step(frame_seq=1, note="   ", actor="investigator")


class ReplayReadOnlyTest(unittest.TestCase):
    def test_replay_never_writes_to_live_state(self) -> None:
        app = make_app()
        start_furnace(app)
        run_heat(app)
        store = app.store
        streams_before = {stream: store.stream_length(stream) for stream in store.list_streams()}
        data_before = store.snapshot()
        versions_before = store.versions()
        # 把所有回放读路径都走一遍
        overview = app.replay.overview()
        app.replay.steps(limit=100)
        app.replay.critical()
        app.replay.frame(1)
        app.replay.frame(overview["frames"])
        app.replay.seek(overview["last_at"])
        app.replay.marks()
        # 现场状态（文档与流水）一个字节都不能变
        self.assertEqual(streams_before, {stream: store.stream_length(stream) for stream in store.list_streams()})
        self.assertEqual(data_before, store.snapshot())
        self.assertEqual(versions_before, store.versions())

    def test_readonly_facade_has_no_write_methods(self) -> None:
        app = make_app()
        readonly = ReadOnlyStore(app.store)
        for name in ("append", "put", "commit_intent"):
            self.assertFalse(hasattr(readonly, name), name)


class ReplayConsoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.console = ConsoleApp(self.app)
        start_furnace(self.app)

    def test_overview_and_steps(self) -> None:
        response = self.console.handle("GET", "/api/replay")
        self.assertEqual(200, response.status)
        self.assertTrue(response.payload["replay"]["readonly"])
        self.assertGreater(response.payload["replay"]["frames"], 0)
        response = self.console.handle("GET", "/api/replay/steps", query={"critical": "true"})
        self.assertEqual(200, response.status)
        self.assertGreater(response.payload["count"], 0)
        self.assertIn("furnace", response.payload["steps"][0]["states"])

    def test_frame_and_seek(self) -> None:
        response = self.console.handle("GET", "/api/replay/steps/1")
        self.assertEqual(200, response.status)
        self.assertEqual(1, response.payload["frame"]["seq"])
        self.assertIn("conditions", response.payload["frame"])
        overview = self.console.handle("GET", "/api/replay").payload["replay"]
        response = self.console.handle("GET", "/api/replay/seek", query={"at": overview["last_at"]})
        self.assertEqual(200, response.status)
        self.assertEqual(overview["frames"], response.payload["frame"]["seq"])
        response = self.console.handle("GET", "/api/replay/steps/9999")
        self.assertEqual(404, response.status)

    def test_mark_via_http_only_appends_marks(self) -> None:
        frames_before = self.app.replay.length()
        response = self.console.handle(
            "POST", "/api/replay/marks", body={"frame_seq": 1, "note": "关键一步", "actor": "调查员"}
        )
        self.assertEqual(200, response.status)
        self.assertEqual(frames_before, self.app.replay.length())
        response = self.console.handle("GET", "/api/replay/marks")
        self.assertEqual(200, response.status)
        self.assertEqual(1, response.payload["count"])

    def test_replay_query_routes_reject_writes(self) -> None:
        response = self.console.handle("POST", "/api/replay/steps/1", body={})
        self.assertEqual(405, response.status)
        response = self.console.handle("POST", "/api/replay", body={})
        self.assertEqual(405, response.status)


class ReplayCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root("flashsmelter-replay-cli-")

    def test_replay_and_mark_commands(self) -> None:
        ignite = run_cli(
            "call", "burner.ignite", "--param", "fuel_pressure_kpa=200", "--param", "air_flow_nm3h=5200", root=self.root
        )
        self.assertEqual(0, ignite.returncode, ignite.stderr)
        listing = run_cli("replay", root=self.root)
        self.assertEqual(0, listing.returncode, listing.stderr)
        payload = json.loads(listing.stdout)
        self.assertEqual(1, payload["overview"]["frames"])
        self.assertEqual("burner.ignite", payload["steps"][0]["action"])
        frame = run_cli("replay", "--at", "1", root=self.root)
        self.assertEqual("preheating", json.loads(frame.stdout)["frame"]["conditions"]["burner"]["state"])
        mark = run_cli("replay-mark", "1", "--note", "点火参数偏离规程", root=self.root)
        self.assertEqual(0, mark.returncode, mark.stderr)
        critical = run_cli("replay", "--critical", root=self.root)
        self.assertEqual(1, json.loads(critical.stdout)["count"])
        missing = run_cli("replay", "--at", "99", root=self.root)
        self.assertEqual(1, missing.returncode)


if __name__ == "__main__":
    unittest.main()
