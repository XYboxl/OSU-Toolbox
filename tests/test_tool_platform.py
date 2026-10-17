"""Registry and runtime acceptance tests for the toolbox platform."""

import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from osu_toolbox import ParamValidationError, parse_osu_file
from osu_toolbox.gui.bridge import Api
from osu_toolbox.schema import GroupSpec, ParamSpec, coerce_params
from osu_toolbox.tools import (
    ConfirmationRequest,
    PreparedRun,
    ToolDefinition,
    ToolCancelledError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_default_registry,
)


class _MemorySettingsStore:
    def __init__(self, loaded=None, warning=None, save_warning=None):
        self.loaded = loaded or {}
        self.warning = warning
        self.save_warning = save_warning
        self.saved = None

    def load(self):
        return dict(self.loaded), self.warning

    def save(self, settings):
        self.saved = {
            tool_id: dict(values)
            for tool_id, values in settings.items()
        }
        return self.save_warning


def _simple_spec(tool_id="sample", result_kind="text"):
    return ToolSpec(
        id=tool_id,
        title="测试工具",
        description="用于验证平台契约",
        category="测试",
        action_label="开始测试",
        groups=(GroupSpec("input", "输入"),),
        params=(ParamSpec("value", "值", "text", "ok", "input", "测试值"),),
        result_kind=result_kind,
    )


class _ImmediateController:
    def __init__(self, confirmation=False, log_count=1):
        self.confirmation = confirmation
        self.log_count = log_count

    def prepare(self, raw_params, context):
        params = coerce_params(_simple_spec().params, raw_params)
        request = None
        if self.confirmation and params["value"] == "confirmed":
            request = ConfirmationRequest("确认测试", "是否继续？", "继续")
        return PreparedRun(params, confirmation=request)

    def run(self, prepared, emit_event):
        for index in range(self.log_count):
            emit_event("log", f"line {index}")
            emit_event("progress", {"fraction": min(1.0, index / max(1, self.log_count))})
        return ToolResult("text", prepared.params["value"])


class _BlockingController(_ImmediateController):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, prepared, emit_event):
        self.started.set()
        emit_event("log", "blocking")
        emit_event("progress", {"fraction": 0.25})
        if not self.release.wait(2.0):
            raise RuntimeError("test release timed out")
        return ToolResult("text", prepared.params["value"])


class _CancellableController(_ImmediateController):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()

    def run(self, prepared, emit_event, cancel_event=None):
        self.started.set()
        while cancel_event is not None and not cancel_event.wait(0.01):
            pass
        raise ToolCancelledError("cancelled by test")


class _BeatmapSummaryController:
    PARAMS = (
        ParamSpec(
            "beatmap_path", "谱面", "path", "", "input", ".osu 谱面文件",
            required=True, persist=False, dialog_mode="open_file",
            filters=(("谱面", "*.osu"),),
        ),
    )

    def prepare(self, raw_params, context):
        params = coerce_params(self.PARAMS, raw_params)
        path = params["beatmap_path"]
        if not path or not os.path.isfile(path):
            raise ParamValidationError("beatmap_path", "谱面不存在")
        params["beatmap_path"] = os.path.abspath(path)
        return PreparedRun(params)

    def run(self, prepared, emit_event):
        beatmap = parse_osu_file(prepared.params["beatmap_path"])
        emit_event("log", f"Parsed: {beatmap.title}")
        return ToolResult("key_value", {
            "标题": beatmap.title,
            "艺术家": beatmap.artist,
            "键数": int(round(beatmap.circle_size)),
            "物件数": len(beatmap.hit_objects),
        })


def _wait_for_terminal(api, run_id, timeout=3.0):
    deadline = time.time() + timeout
    events = []
    while time.time() < deadline:
        batch = api.drain_events(run_id)
        events.extend(batch["events"])
        states = [
            event["payload"]["state"]
            for event in events
            if event["type"] == "state"
        ]
        if "done" in states or "error" in states or "cancelled" in states:
            return events
        time.sleep(0.01)
    raise AssertionError("tool run did not finish")


class ToolRegistryTests(unittest.TestCase):
    def test_default_registry_contains_shipped_tools(self):
        registry = build_default_registry()
        self.assertEqual(
            [spec.id for spec in registry.list_specs()],
            ["render", "replay-generate", "replay-edit", "chart-convert", "speed-change"],
        )

    def test_registry_rejects_ambiguous_definitions(self):
        definition = ToolDefinition(_simple_spec(), _ImmediateController())
        registry = ToolRegistry((definition,))
        with self.assertRaisesRegex(ValueError, "duplicate tool id"):
            registry.register(definition)
        with self.assertRaisesRegex(ValueError, "invalid tool id"):
            ToolRegistry((ToolDefinition(replace(_simple_spec(), id="Bad ID"), _ImmediateController()),))
        bad_param = replace(_simple_spec(), params=(
            ParamSpec("value", "值", "text", "", "missing", "测试值"),
        ))
        with self.assertRaisesRegex(ValueError, "unknown group"):
            ToolRegistry((ToolDefinition(bad_param, _ImmediateController()),))
        with self.assertRaisesRegex(ValueError, "unsupported concurrency"):
            ToolRegistry((ToolDefinition(replace(_simple_spec(), concurrency="shared"), _ImmediateController()),))
        with self.assertRaisesRegex(ValueError, "no prepare method"):
            ToolRegistry((ToolDefinition(_simple_spec("missing-controller"), object()),))

    def test_unregister_returns_registry_to_previous_state(self):
        registry = build_default_registry()
        registry.register(ToolDefinition(_simple_spec(), _ImmediateController()))
        self.assertEqual(
            [spec.id for spec in registry.list_specs()],
            ["render", "replay-generate", "replay-edit", "chart-convert", "speed-change", "sample"],
        )
        registry.unregister("sample")
        self.assertEqual(
            [spec.id for spec in registry.list_specs()],
            ["render", "replay-generate", "replay-edit", "chart-convert", "speed-change"],
        )


class ToolRuntimeTests(unittest.TestCase):
    def test_a_submission_is_exclusive_while_its_preflight_is_running(self):
        entered = threading.Event()
        release = threading.Event()

        class PreparingController(_ImmediateController):
            def prepare(self, raw_params, context):
                if raw_params.get("value") == "first":
                    entered.set()
                    if not release.wait(2.0):
                        raise RuntimeError("test preflight timed out")
                return super().prepare(raw_params, context)

        definition = ToolDefinition(_simple_spec(), PreparingController())
        api = Api(str(ROOT), settings_store=_MemorySettingsStore(),
                  registry=ToolRegistry((definition,)))
        first_results = []
        first = threading.Thread(
            target=lambda: first_results.append(api.start_tool("sample", {"value": "first"})),
        )
        first.start()
        try:
            self.assertTrue(entered.wait(1.0))
            self.assertTrue(api._runtime.is_running())
            self.assertEqual(api.reset_all_settings()["code"], "task_preparing")
            second = api.start_tool("sample", {"value": "second"})
            self.assertEqual(second["status"], "busy")
        finally:
            release.set()
            first.join(3.0)
        self.assertFalse(first.is_alive())
        self.assertEqual(first_results[0]["status"], "started")
        events = _wait_for_terminal(api, first_results[0]["run_id"])
        self.assertEqual(
            [event["payload"] for event in events if event["type"] == "result"],
            [{"kind": "text", "data": "first"}],
        )

    def test_io_errors_during_preflight_have_a_readable_response_and_allow_retry(self):
        class FailingController(_ImmediateController):
            def prepare(self, raw_params, context):
                if raw_params.get("value") == "fail":
                    raise PermissionError("denied")
                return super().prepare(raw_params, context)

        api = Api(str(ROOT), settings_store=_MemorySettingsStore(),
                  registry=ToolRegistry((ToolDefinition(_simple_spec(), FailingController()),)))
        failed = api.start_tool("sample", {"value": "fail"})
        self.assertEqual(failed["code"], "prepare_failed")
        self.assertIn("denied", failed["message"])
        self.assertFalse(api._runtime.is_running())
        started = api.start_tool("sample", {"value": "retry"})
        self.assertEqual(started["status"], "started")
        _wait_for_terminal(api, started["run_id"])

    def test_partial_results_do_not_report_an_unqualified_success(self):
        class WarningController(_ImmediateController):
            def run(self, prepared, emit_event):
                emit_event("warning", "one item was not processed")
                return ToolResult("text", "partial output")

        api = Api(str(ROOT), settings_store=_MemorySettingsStore(),
                  registry=ToolRegistry((ToolDefinition(_simple_spec(), WarningController()),)))
        started = api.start_tool("sample", {})
        events = _wait_for_terminal(api, started["run_id"])
        terminal = [event["payload"] for event in events if event["type"] == "state"][-1]
        self.assertEqual(terminal["state"], "done")
        self.assertTrue(terminal["has_warnings"])

    def test_global_saves_finish_in_order_and_keep_disk_and_memory_consistent(self):
        entered, release, second_saved = threading.Event(), threading.Event(), threading.Event()

        class BlockingStore:
            def __init__(self):
                self.saved = None
                self.calls = 0
            def load_state(self):
                return {"global": {}, "tools": {}}, None
            def save_state(self, settings, tools):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("test timeout")
                else:
                    second_saved.set()
                self.saved = dict(settings)
                return None

        store = BlockingStore()
        api = Api(str(ROOT), settings_store=store, registry=ToolRegistry())
        first = threading.Thread(target=lambda: api.save_global_settings({"speed_keep_pitch": True}))
        second = threading.Thread(target=lambda: api.save_global_settings({"speed_keep_pitch": False}))
        first.start()
        try:
            self.assertTrue(entered.wait(1))
            second.start()
            self.assertFalse(second_saved.wait(0.05))
        finally:
            release.set()
            first.join(3)
            if second.ident:
                second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(api._runtime.global_settings["speed_keep_pitch"])
        self.assertFalse(store.saved["speed_keep_pitch"])

    def test_exclusive_run_reports_busy_and_emits_result(self):
        controller = _BlockingController()
        definition = ToolDefinition(_simple_spec(), controller)
        store = _MemorySettingsStore({"future": {"mode": "kept"}})
        api = Api(str(ROOT), settings_store=store, registry=ToolRegistry((definition,)))

        started = api.start_tool("sample", {"value": "result"})
        self.assertEqual(started["status"], "started")
        self.assertTrue(controller.started.wait(1.0))
        busy = api.start_tool("sample", {"value": "second"})
        self.assertEqual(busy["status"], "busy")
        controller.release.set()
        events = _wait_for_terminal(api, started["run_id"])

        self.assertIn("log", {event["type"] for event in events})
        self.assertIn("progress", {event["type"] for event in events})
        results = [event["payload"] for event in events if event["type"] == "result"]
        self.assertEqual(results, [{"kind": "text", "data": "result"}])
        self.assertTrue(api._handle_closing())
        self.assertEqual(store.saved["sample"], {"value": "result"})
        self.assertEqual(store.saved["future"], {"mode": "kept"})

    def test_cancel_api_emits_cancelled_without_result(self):
        controller = _CancellableController()
        definition = ToolDefinition(_simple_spec(), controller)
        api = Api(
            str(ROOT),
            settings_store=_MemorySettingsStore(),
            registry=ToolRegistry((definition,)),
        )

        started = api.start_tool("sample", {"value": "result"})
        self.assertTrue(controller.started.wait(1.0))
        mismatch = api.cancel_tool("wrong-run")
        self.assertEqual(mismatch["code"], "run_mismatch")
        cancelling = api.cancel_tool(started["run_id"])
        self.assertEqual(cancelling["status"], "cancelling")
        events = _wait_for_terminal(api, started["run_id"])

        states = [
            event["payload"]["state"]
            for event in events
            if event["type"] == "state"
        ]
        self.assertIn("cancelling", states)
        self.assertIn("cancelled", states)
        self.assertNotIn("result", {event["type"] for event in events})
        self.assertTrue(api._handle_closing())

    def test_confirmation_token_is_one_time_and_tool_bound(self):
        definition = ToolDefinition(_simple_spec(), _ImmediateController(confirmation=True))
        api = Api(
            str(ROOT),
            settings_store=_MemorySettingsStore(),
            registry=ToolRegistry((definition,)),
        )
        requested = api.start_tool("sample", {"value": "confirmed"})
        self.assertEqual(requested["status"], "confirmation_required")
        self.assertEqual(requested["confirmation"]["title"], "确认测试")
        token = requested["confirmation_token"]
        started = api.start_tool("sample", {}, token)
        self.assertEqual(started["status"], "started")
        _wait_for_terminal(api, started["run_id"])
        reused = api.start_tool("sample", {}, token)
        self.assertEqual(reused["code"], "invalid_confirmation")

    def test_new_submission_invalidates_previous_confirmation(self):
        definition = ToolDefinition(_simple_spec(), _ImmediateController(confirmation=True))
        api = Api(
            str(ROOT),
            settings_store=_MemorySettingsStore(),
            registry=ToolRegistry((definition,)),
        )
        requested = api.start_tool("sample", {"value": "confirmed"})
        token = requested["confirmation_token"]
        replacement = api.start_tool("sample", {"value": "plain"})
        self.assertEqual(replacement["status"], "started")
        _wait_for_terminal(api, replacement["run_id"])
        stale = api.start_tool("sample", {}, token)
        self.assertEqual(stale["code"], "invalid_confirmation")

    def test_terminal_events_survive_a_full_queue(self):
        definition = ToolDefinition(
            _simple_spec(),
            _ImmediateController(log_count=1400),
        )
        api = Api(
            str(ROOT),
            settings_store=_MemorySettingsStore(),
            registry=ToolRegistry((definition,)),
        )
        started = api.start_tool("sample", {"value": "done"})
        events = _wait_for_terminal(api, started["run_id"])
        event_types = [event["type"] for event in events]
        self.assertIn("result", event_types)
        self.assertIn("state", event_types)
        terminal_index = max(
            index
            for index, event in enumerate(events)
            if event["type"] == "state" and event["payload"]["state"] == "done"
        )
        self.assertNotIn("progress", event_types[terminal_index + 1:])

    def test_unknown_tool_has_stable_error(self):
        api = Api(
            str(ROOT),
            settings_store=_MemorySettingsStore(),
            registry=ToolRegistry(),
        )
        result = api.start_tool("missing", {})
        self.assertEqual(result, {
            "status": "error",
            "code": "unknown_tool",
            "message": "未注册的工具",
        })


class TemporaryRealToolTests(unittest.TestCase):
    def test_register_run_and_unregister_real_beatmap_summary(self):
        controller = _BeatmapSummaryController()
        summary = ToolDefinition(
            ToolSpec(
                id="beatmap-summary",
                title="谱面摘要",
                description="读取谱面并显示基础信息",
                category="谱面",
                action_label="读取谱面",
                groups=(GroupSpec("input", "输入"),),
                params=controller.PARAMS,
                result_kind="key_value",
            ),
            controller,
        )
        registry = build_default_registry()
        registry.register(summary)

        with tempfile.TemporaryDirectory() as temp_dir:
            beatmap_path = Path(temp_dir) / "summary.osu"
            beatmap_path.write_text(
                "osu file format v14\n\n"
                "[General]\nMode:3\n\n"
                "[Metadata]\nTitle:Platform Test\nArtist:Difficile\n"
                "Creator:Test\nVersion:4K\n\n"
                "[Difficulty]\nCircleSize:4\n\n"
                "[HitObjects]\n64,192,1000,1,0,0:0:0:0:\n",
                encoding="utf-8",
            )
            api = Api(
                temp_dir,
                settings_store=_MemorySettingsStore(),
                registry=registry,
            )
            bootstrap = api.get_bootstrap()
            self.assertEqual(
                [tool["id"] for tool in bootstrap["tools"]],
            ["render", "replay-generate", "replay-edit", "chart-convert", "speed-change", "beatmap-summary"],
            )
            started = api.start_tool("beatmap-summary", {
                "beatmap_path": str(beatmap_path),
            })
            self.assertEqual(started["status"], "started")
            events = _wait_for_terminal(api, started["run_id"])

        result = next(event["payload"] for event in events if event["type"] == "result")
        self.assertEqual(result["kind"], "key_value")
        self.assertEqual(result["data"]["标题"], "Platform Test")
        self.assertEqual(result["data"]["艺术家"], "Difficile")
        self.assertEqual(result["data"]["键数"], 4)
        self.assertEqual(result["data"]["物件数"], 1)

        registry.unregister("beatmap-summary")
        self.assertEqual(
            [spec.id for spec in registry.list_specs()],
            ["render", "replay-generate", "replay-edit", "chart-convert", "speed-change"],
        )


if __name__ == "__main__":
    unittest.main()
