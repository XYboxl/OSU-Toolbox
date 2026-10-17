"""Generic background execution and event delivery for registered tools."""

import collections
import inspect
import threading
import uuid

from osu_toolbox.tools import (
    ParamValidationError,
    ToolCancelledError,
    ToolContext,
    persistent_params,
    sanitize_persisted_params,
)
from osu_toolbox import (
    ShellMenuManager,
    SHELL_MENU_ITEMS,
    SHELL_MENU_SETTING_PARAMS,
    default_shell_menu_settings,
    normalize_shell_menu_settings,
)

from .global_settings import default_global_settings, normalize_global_settings


_QUEUE_LIMIT = 1200
_DROPPABLE_EVENTS = frozenset(("log", "progress"))


class ToolRuntime:
    def __init__(
        self,
        registry,
        search_root,
        settings_store,
        output_root=None,
        shell_menu_manager=None,
    ):
        self._registry = registry
        self._search_root = search_root
        self._output_root = output_root or search_root
        self._settings_store = settings_store
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._settings_lock = threading.RLock()
        self._events = collections.deque()
        self._thread = None
        self._active_run = None
        self._cancel_event = None
        self._pending_confirmation = None
        load_state = getattr(self._settings_store, "load_state", None)
        if callable(load_state):
            persisted, self.settings_warning = load_state()
            raw_global_settings = persisted.get("global", {})
            raw_settings = persisted.get("tools", {})
        else:
            raw_settings, self.settings_warning = self._settings_store.load()
            raw_global_settings = {}
        shell_settings = {}
        if isinstance(raw_global_settings, dict):
            nested = raw_global_settings.get("shell_menu")
            if isinstance(nested, dict):
                shell_settings.update(nested)
            # Accept the early flat representation as a migration path.
            shell_keys = {item.key for item in SHELL_MENU_SETTING_PARAMS} | {"shell_menu_enabled"}
            shell_settings.update({
                key: raw_global_settings[key]
                for key in shell_keys
                if key in raw_global_settings
            })
        # Preserve an explicitly disabled legacy master switch during migration.
        if "shell_menu_enabled" in shell_settings and shell_settings["shell_menu_enabled"] is False:
            shell_settings = default_shell_menu_settings()
        self.global_settings = normalize_global_settings(raw_global_settings)
        self.shell_menu_settings = normalize_shell_menu_settings(shell_settings)
        self._shell_menu_manager = shell_menu_manager or ShellMenuManager()
        self._refresh_context()
        self._unknown_settings = {
            key: value
            for key, value in raw_settings.items()
            if key not in {item.spec.id for item in registry.list_definitions()}
        }
        self.settings = {}
        for definition in registry.list_definitions():
            tool_id = definition.spec.id
            self.settings[tool_id] = sanitize_persisted_params(
                raw_settings.get(tool_id),
                definition.spec.params,
            )

    def bootstrap_state(self):
        with self._settings_lock:
            with self._lock:
                active_run = dict(self._active_run) if self._active_run else None
            return {
                "settings": {key: dict(value) for key, value in self.settings.items()},
                "global_settings": dict(self.global_settings),
                "shell_menu_settings": dict(self.shell_menu_settings),
                "shell_menu": self._shell_menu_manager.status(),
                "settings_warning": self.settings_warning,
                "active_run": active_run,
            }

    def save_global_settings(self, raw_settings):
        with self._settings_lock:
            try:
                normalized = normalize_global_settings(raw_settings)
            except ParamValidationError as exc:
                return self._error("validation_error", str(exc), field=exc.key)
            warning = self._persist_settings(global_settings=normalized)
            if warning:
                return self._error("settings_save_failed", warning)
            self.global_settings = normalized
            self._refresh_context()
            return {
                "status": "ok",
                "global_settings": dict(self.global_settings),
                "message": "设置已保存",
            }

    def save_shell_menu_settings(self, raw_settings):
        with self._settings_lock:
            normalized = normalize_shell_menu_settings(raw_settings)
            result = self._shell_menu_manager.apply(normalized)
            if result.get("ok") is False and result.get("code") != "unsupported_platform":
                return self._error(
                    result.get("code", "registry_error"),
                    result.get("message", "右键菜单设置保存失败"),
                )
            warning = self._persist_settings(shell_menu_settings=normalized)
            if warning:
                return self._error("settings_save_failed", warning)
            self.shell_menu_settings = normalized
            result.update({
                "status": "warning" if result.get("code") == "unsupported_platform" else "ok",
                "shell_menu_settings": dict(self.shell_menu_settings),
                "message": result.get("message", "右键菜单设置已保存"),
            })
            return result

    def get_shell_menu_state(self):
        with self._settings_lock:
            return {
                "settings": dict(self.shell_menu_settings),
                "status": self._shell_menu_manager.status(),
            }

    def set_all_shell_menu(self, enabled):
        value = bool(enabled)
        settings = {
            spec.key: value
            for spec in SHELL_MENU_SETTING_PARAMS
        }
        return self.save_shell_menu_settings(settings)

    def set_shell_menu_item(self, operation, enabled):
        item = next(
            (candidate for candidate in SHELL_MENU_ITEMS if candidate.operation == operation),
            None,
        )
        if item is None:
            return self._error("unknown_shell_menu", "未注册的右键菜单项目")
        settings = dict(self.shell_menu_settings)
        settings[item.setting_key] = bool(enabled)
        return self.save_shell_menu_settings(settings)

    def shell_menu_status(self):
        return self._shell_menu_manager.status()

    def repair_shell_menu(self):
        with self._settings_lock:
            result = self._shell_menu_manager.repair(self.shell_menu_settings)
            result["status"] = (
                "warning" if result.get("supported") is False
                else "ok" if result.get("ok", True) else "error"
            )
            result["shell_menu_settings"] = dict(self.shell_menu_settings)
            return result

    def uninstall_shell_menu(self):
        with self._settings_lock:
            result = self._shell_menu_manager.uninstall_all()
            if result.get("ok", True):
                self.shell_menu_settings = default_shell_menu_settings()
                warning = self._persist_settings(shell_menu_settings=self.shell_menu_settings)
                if warning:
                    return self._error("settings_save_failed", warning)
            result["status"] = (
                "warning" if result.get("supported") is False
                else "ok" if result.get("ok", True) else "error"
            )
            result["shell_menu_settings"] = dict(self.shell_menu_settings)
            return result

    def reset_all_settings(self):
        if not self._start_lock.acquire(blocking=False):
            return self._error("task_preparing", "任务正在检查输入，请稍后再恢复默认设置")
        try:
            return self._reset_all_settings()
        finally:
            self._start_lock.release()

    def _reset_all_settings(self):
        with self._settings_lock:
            reset = getattr(self._settings_store, "reset", None)
            if callable(reset):
                warning = reset()
            else:
                warning = self._settings_store.save({})
            if warning:
                return self._error("settings_reset_failed", warning)

            self.global_settings = default_global_settings()
            self.shell_menu_settings = default_shell_menu_settings()
            shell_result = self._shell_menu_manager.uninstall_all()
            self._unknown_settings = {}
            self.settings = {
                definition.spec.id: {}
                for definition in self._registry.list_definitions()
            }
            self.settings_warning = None
            with self._lock:
                self._pending_confirmation = None
            self._refresh_context()
            result = {
                "status": "ok",
                "global_settings": dict(self.global_settings),
                "shell_menu_settings": dict(self.shell_menu_settings),
                "shell_menu": self._shell_menu_manager.status(),
                "settings": {key: {} for key in self.settings},
                "message": "所有设置已恢复到默认状态",
            }
            if shell_result.get("ok") is False:
                result.update({
                    "status": "warning",
                    "message": (
                        "设置已恢复到默认状态，但右键菜单卸载未完成："
                        f"{shell_result.get('message', '请使用修复功能重试')}"
                    ),
                })
            return result

    def prefill(self, tool_id, raw_params, changed_key):
        try:
            definition = self._registry.get(tool_id)
        except KeyError:
            return self._error("unknown_tool", "未注册的工具")

        prefill = getattr(definition.controller, "prefill", None)
        if not callable(prefill):
            return {"status": "ok", "params": {}, "warnings": []}
        try:
            result = prefill(raw_params, changed_key, self._context)
        except ParamValidationError as exc:
            return self._error("validation_error", str(exc), field=exc.key)
        except (OSError, TypeError, ValueError) as exc:
            return self._error(
                "validation_error",
                str(exc),
                field=changed_key,
            )

        known_keys = {param.key for param in definition.spec.params}
        params = {
            key: value
            for key, value in result.params.items()
            if key in known_keys
        }
        return {
            "status": "ok",
            "params": params,
            "warnings": list(result.warnings),
        }

    def start(self, tool_id, raw_params, confirmation_token=None):
        if not self._start_lock.acquire(blocking=False):
            return {
                "status": "busy",
                "code": "exclusive_run_active",
                "message": "已有工具任务正在准备或运行",
            }
        try:
            return self._start(tool_id, raw_params, confirmation_token)
        finally:
            self._start_lock.release()

    def _start(self, tool_id, raw_params, confirmation_token=None):
        try:
            definition = self._registry.get(tool_id)
        except KeyError:
            return self._error("unknown_tool", "未注册的工具")

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {
                    "status": "busy",
                    "code": "exclusive_run_active",
                    "message": "已有工具任务正在运行",
                }

        if confirmation_token:
            prepared = self._consume_confirmation(tool_id, confirmation_token)
            if prepared is None:
                return self._error("invalid_confirmation", "确认请求已失效，请重新提交")
            # Controllers use this private marker only after the one-time
            # confirmation token has been consumed.
            prepared.params["_allow_overwrite"] = True
        else:
            with self._lock:
                self._pending_confirmation = None
            try:
                prepared = definition.controller.prepare(raw_params, self._context)
            except ParamValidationError as exc:
                return self._error("validation_error", str(exc), field=exc.key)
            except (TypeError, ValueError) as exc:
                return self._error("validation_error", str(exc))
            except (OSError, RuntimeError) as exc:
                return self._error("prepare_failed", f"输入检查失败：{exc}")
            if prepared.confirmation is not None:
                token = uuid.uuid4().hex
                with self._lock:
                    self._pending_confirmation = {
                        "token": token,
                        "tool_id": tool_id,
                        "prepared": prepared,
                    }
                return {
                    "status": "confirmation_required",
                    "confirmation_token": token,
                    "confirmation": prepared.confirmation.as_dict(),
                }

        save_warning = self._save_settings(definition, prepared.params)
        warnings = list(prepared.warnings)
        if save_warning:
            warnings.append(save_warning)

        run_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        with self._lock:
            self._events.clear()
            self._active_run = {
                "run_id": run_id,
                "tool_id": tool_id,
                "state": "running",
            }
            self._cancel_event = cancel_event
            thread = threading.Thread(
                target=self._run,
                args=(run_id, definition, prepared, cancel_event),
                daemon=True,
            )
            self._thread = thread
        self._enqueue(run_id, tool_id, "state", {"state": "running", "message": "任务已开始"})
        for warning in warnings:
            self._enqueue(run_id, tool_id, "warning", str(warning))
        thread.start()
        return {"status": "started", "run_id": run_id, "warnings": warnings}

    def cancel(self, run_id=None):
        with self._lock:
            active_run = dict(self._active_run) if self._active_run else None
            thread_alive = bool(self._thread is not None and self._thread.is_alive())
            cancel_event = self._cancel_event
            if not active_run or not thread_alive or cancel_event is None:
                return self._error("no_active_run", "当前没有正在运行的任务")
            if run_id and active_run["run_id"] != run_id:
                return self._error("run_mismatch", "任务标识与当前任务不匹配")
            already_cancelling = active_run["state"] == "cancelling"
            cancel_event.set()
            self._active_run["state"] = "cancelling"
        if not already_cancelling:
            self._enqueue(
                active_run["run_id"],
                active_run["tool_id"],
                "state",
                {"state": "cancelling", "message": "正在取消并清理临时文件"},
            )
        return {"status": "cancelling", "run_id": active_run["run_id"]}

    def drain_events(self, run_id=None):
        events = []
        with self._lock:
            for _ in range(min(200, len(self._events))):
                event = self._events.popleft()
                if run_id and event["run_id"] != run_id:
                    continue
                events.append(event)
            active_run = dict(self._active_run) if self._active_run else None
        return {"events": events, "active_run": active_run}

    def is_running(self):
        with self._lock:
            return self._start_lock.locked() or bool(self._thread is not None and self._thread.is_alive())

    def active_run_id(self):
        with self._lock:
            return self._active_run["run_id"] if self._active_run else None

    def warn_active_run(self, message):
        with self._lock:
            active_run = dict(self._active_run) if self._active_run else None
        if active_run:
            self._enqueue(
                active_run["run_id"],
                active_run["tool_id"],
                "warning",
                message,
            )

    def _run(self, run_id, definition, prepared, cancel_event):
        tool_id = definition.spec.id

        has_warnings = False

        def emit_event(event_type, payload):
            nonlocal has_warnings
            if event_type == "warning":
                has_warnings = True
            self._enqueue(run_id, tool_id, event_type, payload)

        try:
            run_method = definition.controller.run
            parameters = inspect.signature(run_method).parameters
            cancel_parameter = parameters.get("cancel_event")
            if any(
                item.kind == inspect.Parameter.VAR_POSITIONAL
                for item in parameters.values()
            ):
                result = run_method(prepared, emit_event, cancel_event)
            elif cancel_parameter is not None:
                result = run_method(
                    prepared,
                    emit_event,
                    cancel_event=cancel_event,
                )
            else:
                result = run_method(prepared, emit_event)
            if cancel_event.is_set():
                raise ToolCancelledError("任务已取消")
            if result.kind != definition.spec.result_kind:
                raise ValueError(
                    f"工具 {tool_id} 返回了 {result.kind!r}，"
                    f"预期为 {definition.spec.result_kind!r}",
                )
            self._enqueue(run_id, tool_id, "result", result.as_dict())
            state = "done"
            message = "任务完成，请检查提示和结果" if has_warnings else "任务完成"
        except ToolCancelledError:
            state = "cancelled"
            message = "任务已取消，临时文件已清理"
        except Exception as exc:
            emit_event("log", f"错误：{exc}")
            state = "error"
            message = str(exc)
        with self._lock:
            if self._active_run and self._active_run["run_id"] == run_id:
                self._active_run["state"] = state
                self._cancel_event = None
        self._enqueue(run_id, tool_id, "state", {
            "state": state, "message": message, "has_warnings": has_warnings,
        })

    def _save_settings(self, definition, params):
        with self._settings_lock:
            tool_id = definition.spec.id
            saved = persistent_params(params, definition.spec.params)
            self.settings[tool_id] = saved
            return self._persist_settings()

    def _persist_settings(self, global_settings=None, shell_menu_settings=None):
        with self._settings_lock:
            all_settings = dict(self._unknown_settings)
            all_settings.update(self.settings)
            effective_global = dict(
                self.global_settings if global_settings is None else global_settings,
            )
            effective_shell = dict(
                self.shell_menu_settings if shell_menu_settings is None else shell_menu_settings,
            )
            effective_global["shell_menu"] = effective_shell
            save_state = getattr(self._settings_store, "save_state", None)
            if callable(save_state):
                return save_state(
                    effective_global,
                    all_settings,
                )
            return self._settings_store.save(all_settings)

    def _refresh_context(self):
        self._context = ToolContext(
            search_root=self._search_root,
            output_root=self._output_root,
            global_settings=dict(self.global_settings),
        )

    def _consume_confirmation(self, tool_id, token):
        with self._lock:
            pending = self._pending_confirmation
            self._pending_confirmation = None
        if not pending:
            return None
        if pending["tool_id"] != tool_id or pending["token"] != token:
            return None
        return pending["prepared"]

    def _enqueue(self, run_id, tool_id, event_type, payload):
        event = {
            "run_id": run_id,
            "tool_id": tool_id,
            "type": event_type,
            "payload": payload,
        }
        with self._lock:
            if event_type == "progress" and self._events:
                previous = self._events[-1]
                if previous["type"] == "progress" and previous["run_id"] == run_id:
                    self._events[-1] = event
                    return
            if len(self._events) >= _QUEUE_LIMIT:
                drop_index = next(
                    (
                        index
                        for index, queued in enumerate(self._events)
                        if queued["type"] in _DROPPABLE_EVENTS
                    ),
                    None,
                )
                if drop_index is None:
                    if event_type in _DROPPABLE_EVENTS:
                        return
                    self._events.popleft()
                else:
                    del self._events[drop_index]
            self._events.append(event)

    @staticmethod
    def _error(code, message, field=None):
        result = {"status": "error", "code": code, "message": message}
        if field:
            result["field"] = field
        return result
