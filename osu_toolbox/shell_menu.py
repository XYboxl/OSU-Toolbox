'''Windows Explorer context-menu integration for lightweight commands.'''

from dataclasses import dataclass
import os
import subprocess
import sys

from .schema import GroupSpec, ParamSpec, coerce_params

SHELL_MENU_EXE_NAME = "osu-toolbox-menu.exe"
SHELL_MENU_SCOPE = "当前用户"
SHELL_MENU_KEY_PREFIX = "osu-toolbox"
SHELL_MENU_COMMAND_STORE_PATH = (
    "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\CommandStore\\shell"
)
# Registry locations for the folder menu.  ``Directory`` covers a right click
# on the folder icon, ``Directory\Background`` a right click on empty space
# inside an open folder; Explorer picks whichever is most specific.
DIRECTORY_KEY_PATH = "Software\\Classes\\Directory\\shell"
DIRECTORY_BACKGROUND_KEY_PATH = "Software\\Classes\\Directory\\Background\\shell"
FOLDER_TARGET = "folder"
FILE_ASSOCIATION_KEY_TEMPLATE = "Software\\Classes\\SystemFileAssociations\\{extension}\\shell"


@dataclass(frozen=True)
class ShellMenuChild:
    key: str
    title: str
    target_format: str = ""
    # ``True`` for the 变速 “自定义” entry: instead of a fixed speed it asks the
    # user for a multiplier when the command runs.
    prompt_speed: bool = False
    # Explicit ``--difficulty-marker`` value. ``None`` leaves the configured
    # setting in charge; ``""`` clears it, which is what a chart-convert entry
    # wants because the target chart has no per-difficulty name.
    difficulty_marker: str = None


@dataclass(frozen=True)
class ShellMenuItem:
    operation: str
    title: str
    extensions: tuple
    setting_key: str
    children: tuple = ()
    # ``True`` for the folder menu, which needs different registry locations,
    # a "%V" placeholder and its own child list.
    folder: bool = False
    folder_children: tuple = ()

    def children_for(self, target):
        """Return the submenu entries valid for one registry target."""
        return self.folder_children if target == FOLDER_TARGET else self.children

    @property
    def target_keys(self):
        """Return the registry targets: extensions, or ``folder``."""
        return (FOLDER_TARGET,) if self.folder else tuple(self.extensions)

    def submenu_key(self, child_key):
        """Return the child key, distinguishing the folder variant (``-dir``)."""
        for child in self.folder_children:
            if child.key == child_key:
                return f"{child.key}-dir"
        return child_key

    def _submenu_payload(self, executable_path):
        return [
            {
                "key": child.key,
                "title": child.title,
                "target_format": child.target_format,
                "prompt_speed": child.prompt_speed,
                "difficulty_marker": child.difficulty_marker,
                "command": shell_menu_command(
                    executable_path,
                    self.operation,
                    child.target_format,
                    prompt_speed=child.prompt_speed,
                    difficulty_marker=child.difficulty_marker,
                ),
            }
            for child in _unique_children(self.target_keys, self.children_for)
        ]

    def as_dict(self, executable_path=None, status=None):
        payload = {
            "operation": self.operation,
            "title": self.title,
            "extensions": list(self.extensions),
            "folder": self.folder,
            "file_types": ["目录"] if self.folder else [f"*{item}" for item in self.extensions],
            "filter": ";".join(f"*{item}" for item in self.extensions),
            "setting_key": self.setting_key,
            "scope": SHELL_MENU_SCOPE,
            "command": shell_menu_command(executable_path, self.operation),
            "argv": [
                resolve_shell_executable(executable_path),
                "--context", "shell", "--operation", self.operation,
                "%V" if self.folder else "%1",
            ],
            "submenu": self._submenu_payload(executable_path),
        }
        if status:
            payload.update(status)
        return payload


_ARCHIVE_CHILDREN = (
    ShellMenuChild("to-osz", "转换为 .osz 归档", ".osz"),
    ShellMenuChild("to-mcz", "转换为 .mcz 归档", ".mcz"),
)
_PLAIN_CHILDREN = (
    # An empty marker clears the configured difficulty marker: the converted
    # chart is a fresh file whose name already carries the target format, so a
    # `` {num}x``-style speed marker there would only confuse.
    ShellMenuChild("to-osu", "转换为 .osu", ".osu", difficulty_marker=""),
    ShellMenuChild("to-mc", "转换为 .mc", ".mc", difficulty_marker=""),
    ShellMenuChild("to-sm", "转换为 .sm", ".sm", difficulty_marker=""),
)
# Right-click speed presets. ``speed-<key>`` encodes the multiplier in the key.
SHELL_MENU_SPEED_PRESETS = (0.9, 0.95, 1.05, 1.1)
_SPEED_CHILDREN = tuple(
    ShellMenuChild(f"speed-{int(round(value * 100))}", f"变速 {value:g}x", f"{value:g}")
    for value in SHELL_MENU_SPEED_PRESETS
) + (ShellMenuChild("speed-custom", "自定义倍率…", prompt_speed=True),)
_ARCHIVE_SPEED_CHILDREN = tuple(
    ShellMenuChild(f"speed-{int(round(value * 100))}", f"变速 {value:g}x（归档）", f"{value:g}")
    for value in SHELL_MENU_SPEED_PRESETS
) + (ShellMenuChild("speed-custom", "自定义倍率…（归档）", prompt_speed=True),)

SHELL_MENU_ITEMS = (
    ShellMenuItem("replay-generate", "生成完美回放", (".osu", ".mc"), "shell_menu_replay_generate"),
    ShellMenuItem(
        "chart-convert", "转谱", (".osu", ".mc", ".sm"), "shell_menu_chart_convert",
        _PLAIN_CHILDREN + _ARCHIVE_CHILDREN,
    ),
    # Archives are also single files, so they get their own item carrying only
    # the archive targets.  It shares the chart-convert setting so one switch
    # controls the whole 转谱 menu.
    ShellMenuItem(
        "archive-convert", "转谱", (".osz", ".mcz"), "shell_menu_chart_convert",
        _ARCHIVE_CHILDREN,
    ),
    ShellMenuItem(
        "folder-convert", "转谱", (), "shell_menu_chart_convert",
        folder=True, folder_children=_ARCHIVE_CHILDREN,
    ),
    ShellMenuItem(
        "speed-change", "变速", (".osu", ".mc", ".sm"), "shell_menu_speed_change",
        _SPEED_CHILDREN,
    ),
    ShellMenuItem(
        "archive-speed-change", "变速", (".osz", ".mcz"), "shell_menu_speed_change",
        _ARCHIVE_SPEED_CHILDREN,
    ),
    ShellMenuItem(
        "folder-speed-change", "变速", (), "shell_menu_speed_change",
        folder=True, folder_children=_SPEED_CHILDREN,
    ),
)


def _unique_children(targets, children_for):
    """Yield each submenu child once, in first-seen order.

    A multi-extension item repeats its children per extension in the registry,
    but the exported schema should describe them a single time.
    """
    seen = set()
    for target in targets:
        for child in children_for(target):
            if child.key in seen:
                continue
            seen.add(child.key)
            yield child

SHELL_MENU_SETTING_GROUPS = (GroupSpec("shell_menu", "右键菜单"),)
# One switch per distinct setting key, so a shared key is applied only once.
_SHELL_MENU_SETTING_ENTRIES = []
for _item in SHELL_MENU_ITEMS:
    if all(entry[0] != _item.setting_key for entry in _SHELL_MENU_SETTING_ENTRIES):
        _SHELL_MENU_SETTING_ENTRIES.append((_item.setting_key, _item))
del _item


def _shell_menu_setting_description(entry):
    setting_key, item = entry
    if setting_key == "shell_menu_chart_convert":
        return "在本工具支持的谱面/归档文件以及文件夹的右键菜单中显示“转谱”"
    if setting_key == "shell_menu_speed_change":
        return "在谱面、归档和文件夹的右键菜单中显示“变速”"
    return f"在 {', '.join(item.extensions)} 文件的右键菜单中显示“{item.title}”"


SHELL_MENU_SETTING_PARAMS = tuple(
    ParamSpec(setting_key, item.title, "bool", False, "shell_menu",
              _shell_menu_setting_description((setting_key, item)), persist=True)
    for setting_key, item in _SHELL_MENU_SETTING_ENTRIES
)


def default_shell_menu_settings():
    return {spec.key: spec.default for spec in SHELL_MENU_SETTING_PARAMS}


def normalize_shell_menu_settings(raw):
    values = default_shell_menu_settings()
    if isinstance(raw, dict):
        try:
            values.update(coerce_params(SHELL_MENU_SETTING_PARAMS, raw))
        except (TypeError, ValueError):
            for spec in SHELL_MENU_SETTING_PARAMS:
                try:
                    values[spec.key] = coerce_params((spec,), raw)[spec.key]
                except (TypeError, ValueError):
                    values[spec.key] = spec.default
    return values


def _default_executable_path():
    if getattr(sys, "frozen", False):
        return os.path.abspath(os.path.join(os.path.dirname(sys.executable), SHELL_MENU_EXE_NAME))
    return os.path.abspath(os.path.join(os.path.dirname(sys.argv[0]), SHELL_MENU_EXE_NAME))


def resolve_shell_executable(executable_path=None):
    return os.path.abspath(os.fspath(executable_path or _default_executable_path()))


def shell_menu_command(
    executable_path,
    operation,
    target_format=None,
    prompt_speed=False,
    difficulty_marker=None,
):
    item = next((item for item in SHELL_MENU_ITEMS if item.operation == operation), None)
    if item is None:
        raise ValueError(f"未知右键菜单操作：{operation}")
    args = [resolve_shell_executable(executable_path), "--context", "shell", "--operation", operation]
    if target_format:
        target_format = str(target_format).lower()
    if prompt_speed:
        args.append("--prompt-speed")
    if target_format:
        args.extend(("--target-format", target_format))
    if difficulty_marker is not None:
        args.append(f"--difficulty-marker={difficulty_marker}")
    placeholder = "%V" if item.folder else "%1"
    return f'{subprocess.list2cmdline(args)} "{placeholder}"'


def shell_menu_schema(executable_path=None):
    executable = resolve_shell_executable(executable_path)
    return {
        "groups": [group.as_dict() for group in SHELL_MENU_SETTING_GROUPS],
        "params": [param.as_dict() for param in SHELL_MENU_SETTING_PARAMS],
        "items": [item.as_dict(executable) for item in SHELL_MENU_ITEMS],
        "scope": SHELL_MENU_SCOPE,
        "executable": executable,
    }


def _load_winreg():
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    return winreg


def _notify_shell():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


class ShellMenuManager:
    def __init__(self, executable_path=None, winreg_module=None, notify_shell=None):
        self.executable_path = resolve_shell_executable(executable_path)
        self._winreg = winreg_module if winreg_module is not None else _load_winreg()
        self._items = {item.operation: item for item in SHELL_MENU_ITEMS}
        self._notify_shell = notify_shell or (
            _notify_shell if winreg_module is None else lambda: None
        )

    @property
    def supported(self):
        return self._winreg is not None and hasattr(self._winreg, "HKEY_CURRENT_USER")

    def item(self, operation):
        try:
            return self._items[operation]
        except KeyError:
            raise ValueError(f"未知右键菜单操作：{operation}")

    def _require_target(self, operation, target):
        item = self.item(operation)
        target = str(target).lower()
        if target not in {key.lower() for key in item.target_keys}:
            raise ValueError(f"操作 {operation} 不支持文件类型 {target}")
        return item, target

    @staticmethod
    def _target_prefix(target):
        if target == FOLDER_TARGET:
            return DIRECTORY_KEY_PATH
        return FILE_ASSOCIATION_KEY_TEMPLATE.format(extension=target)

    def key_path(self, operation, extension):
        item, target = self._require_target(operation, extension)
        return f"{self._target_prefix(target)}\\{SHELL_MENU_KEY_PREFIX}-{item.operation}"

    def background_key_path(self, operation):
        """Return the ``Directory\\Background`` key for the folder menu."""
        item = self.item(operation)
        if not item.folder:
            raise ValueError(f"操作 {operation} 不是文件夹菜单")
        return f"{DIRECTORY_BACKGROUND_KEY_PATH}\\{SHELL_MENU_KEY_PREFIX}-{item.operation}"

    def command_key_path(self, operation, extension):
        return self.key_path(operation, extension) + r"\command"

    def submenu_key_path(self, operation, extension, child_key):
        item, target = self._require_target(operation, extension)
        if child_key not in {child.key for child in item.children_for(target)}:
            raise ValueError(f"未知的转谱子菜单项：{child_key}")
        return self.key_path(operation, target) + "\\shell\\" + item.submenu_key(child_key)

    @staticmethod
    def submenu_key_name(operation, child_key):
        """Return the unique legacy name used when cleaning old CommandStore entries."""
        return f"{SHELL_MENU_KEY_PREFIX}-{operation}-{child_key}"

    def command_store_submenu_key_path(self, operation, child_key):
        self.item(operation)
        return f"{SHELL_MENU_COMMAND_STORE_PATH}\\{self.submenu_key_name(operation, child_key)}"
    def submenu_command_key_path(self, operation, extension, child_key):
        return self.submenu_key_path(operation, extension, child_key) + r"\command"

    def expected_command(
        self, operation, target_format=None, prompt_speed=False, difficulty_marker=None
    ):
        return shell_menu_command(
            self.executable_path,
            operation,
            target_format,
            prompt_speed=prompt_speed,
            difficulty_marker=difficulty_marker,
        )

    def _extended_subcommands_key(self, item, target):
        """Return the ``ExtendedSubCommandsKey`` Windows resolves children from.

        Windows only accepts this value relative to ``HKEY_CLASSES_ROOT``, so
        the folder menu needs the ``Directory\\...`` prefix that file
        associations already carry implicitly.
        """
        suffix = f"shell\\{SHELL_MENU_KEY_PREFIX}-{item.operation}"
        if target == FOLDER_TARGET:
            return f"Directory\\{suffix}"
        return f"SystemFileAssociations\\{target}\\{suffix}"

    def status(self):
        executable_exists = os.path.isfile(self.executable_path)
        if not self.supported:
            return {"supported": False, "scope": SHELL_MENU_SCOPE, "executable": self.executable_path,
                    "executable_exists": executable_exists, "healthy": None, "state": "unsupported",
                    "message": "当前系统不支持 Windows 右键菜单注册",
                    "items": [item.as_dict(self.executable_path, {"enabled": False, "healthy": None, "state": "unsupported"}) for item in SHELL_MENU_ITEMS]}
        item_statuses = [self._item_status(item) for item in SHELL_MENU_ITEMS]
        states = {item["state"] for item in item_statuses}
        state = "needs_repair" if "needs_repair" in states else ("enabled" if "enabled" in states else ("disabled" if states == {"disabled"} else "needs_repair"))
        return {"supported": True, "scope": SHELL_MENU_SCOPE, "executable": self.executable_path,
                "executable_exists": executable_exists, "healthy": state in ("enabled", "disabled"),
                "state": state, "message": {
                    "enabled": "右键菜单已启用", "disabled": "右键菜单未启用",
                    "needs_repair": "右键菜单需要修复",
                }[state],
                "items": item_statuses}

    def apply(self, raw_settings):
        settings = normalize_shell_menu_settings(raw_settings)
        if not self.supported:
            result = self.status(); result.update({"ok": False, "code": "unsupported_platform"}); return result
        errors = []
        # Items share setting keys (转谱 covers files, archives and folders), so
        # any item whose key is enabled is registered.
        handled = set()
        for item in SHELL_MENU_ITEMS:
            enabled = settings.get(item.setting_key, False)
            if item.setting_key in handled:
                continue
            handled.add(item.setting_key)
            for candidate in self._items_with_setting(item.setting_key):
                try:
                    (self.enable if enabled else self.disable)(candidate.operation)
                except (OSError, ValueError) as exc:
                    errors.append(f"{candidate.title}：{exc}")
        try:
            self.remove_obsolete_registrations()
        except (OSError, ValueError) as exc:
            errors.append(f"清理旧版菜单：{exc}")
        self._notify_shell()
        result = self.status()
        if errors:
            result.update({"ok": False, "code": "registry_error", "message": "；".join(errors)})
        else:
            result["ok"] = result.get("state") != "needs_repair" and all(
                item["enabled"] == bool(settings.get(item["setting_key"], False))
                for item in result["items"]
            )
            if not result["ok"]:
                result.update({"code": "registry_verification_failed", "message": "右键菜单写入后校验失败，请确认入口程序存在并重试修复"})
        return result

    def _items_with_setting(self, setting_key):
        return [item for item in SHELL_MENU_ITEMS if item.setting_key == setting_key]

    def enable(self, operation):
        item = self.item(operation); self._require_supported()
        if not os.path.isfile(self.executable_path):
            raise FileNotFoundError(f"右键菜单入口不存在：{self.executable_path}")
        for target in item.target_keys:
            self._write_item(item, target)
        # Registering also clears anything an older version wrote that this
        # version no longer owns, so the menu cannot end up with duplicates.
        self.remove_obsolete_registrations()
        return self._item_status(item)

    def disable(self, operation):
        item = self.item(operation); self._require_supported()
        for target in item.target_keys:
            self._delete_tree(self.key_path(operation, target))
        if item.folder:
            self._delete_tree(self.background_key_path(operation))
        # CommandStore entries are shared by all extensions of an operation,
        # so remove them after the association verbs have been removed.
        for target in item.target_keys:
            for child in item.children_for(target):
                for child_key in {child.key, item.submenu_key(child.key)}:
                    self._delete_tree(self.command_store_submenu_key_path(operation, child_key))
        return self._item_status(item)

    def uninstall_all(self):
        if not self.supported: return self.status()
        errors = []
        for item in SHELL_MENU_ITEMS:
            try: self.disable(item.operation)
            except (OSError, ValueError) as exc: errors.append(f"{item.title}：{exc}")
        try:
            self.remove_obsolete_registrations()
        except (OSError, ValueError) as exc:
            errors.append(f"清理旧版菜单：{exc}")
        self._notify_shell()
        result = self.status()
        result["ok"] = not errors and result.get("state") == "disabled"
        if errors:
            result.update({"code": "registry_error", "message": "；".join(errors)})
        elif not result["ok"]:
            result.update({"code": "registry_verification_failed", "message": "右键菜单仍有残留，请重试卸载"})
        else:
            result["message"] = "右键菜单已卸载"
        return result

    def _managed_key_paths(self):
        """Return every registry key this code intends to own."""
        managed = set()
        for item in SHELL_MENU_ITEMS:
            for target in item.target_keys:
                managed.add(self.key_path(item.operation, target))
            if item.folder:
                managed.add(self.background_key_path(item.operation))
        return managed

    def remove_obsolete_registrations(self):
        """Delete keys left by an earlier version that registered elsewhere.

        Earlier releases put 转谱 on ``.osz``/``.mcz`` under the chart-convert
        operation; archives later moved to their own operation, so those keys
        survived as duplicates in the context menu.  Only keys carrying our own
        ``osu-toolbox-managed`` marker are removed, so a hand-written rule can
        never be deleted by accident.
        """
        managed = self._managed_key_paths()
        removed = []
        # Derived from the declared items rather than imported from the chart
        # module: this entry point must stay dependency-free for a cold start.
        extensions = sorted({extension for item in SHELL_MENU_ITEMS
                             for extension in item.extensions})
        parents = [DIRECTORY_KEY_PATH, DIRECTORY_BACKGROUND_KEY_PATH, SHELL_MENU_COMMAND_STORE_PATH]
        parents.extend(
            FILE_ASSOCIATION_KEY_TEMPLATE.format(extension=extension)
            for extension in extensions
        )
        for parent in parents:
            try:
                key = self._open_key(parent, self._reg_const("KEY_READ", 0x20019))
            except (FileNotFoundError, KeyError):
                continue
            try:
                names = self._subkey_names(key)
            finally:
                self._close(key)
            for name in names:
                if not str(name).startswith(SHELL_MENU_KEY_PREFIX):
                    continue
                path = f"{parent}\\{name}"
                if path in managed:
                    continue
                if not self._is_managed(path):
                    continue
                self._delete_tree(path)
                removed.append(path)
        return removed

    def _subkey_names(self, key):
        query = getattr(self._winreg, "QueryInfoKey", None)
        enum = getattr(self._winreg, "EnumKey", None)
        if not query or not enum:
            return []
        count = int(query(key)[0])
        return [enum(key, index) for index in range(count)]

    def _is_managed(self, path):
        try:
            key = self._open_key(path, self._reg_const("KEY_READ", 0x20019))
        except (FileNotFoundError, KeyError):
            return False
        try:
            marker = str(self._winreg.QueryValueEx(key, "osu-toolbox-managed")[0])
        except (FileNotFoundError, KeyError):
            return False
        finally:
            self._close(key)
        return marker == "1"

    def repair(self, raw_settings): return self.apply(raw_settings)

    def _require_supported(self):
        if not self.supported: raise OSError("当前系统不支持 Windows 右键菜单注册")

    def _reg_const(self, name, fallback=0): return getattr(self._winreg, name, fallback)

    def _open_key(self, path, access):
        try: return self._winreg.OpenKey(self._winreg.HKEY_CURRENT_USER, path, 0, access)
        except TypeError: return self._winreg.OpenKey(self._winreg.HKEY_CURRENT_USER, path)

    @staticmethod
    def _close(key):
        close = getattr(key, "Close", None) or getattr(key, "close", None)
        if close: close()

    def _create_key(self, path, access):
        create_ex = getattr(self._winreg, "CreateKeyEx", None)
        if create_ex:
            try: return create_ex(self._winreg.HKEY_CURRENT_USER, path, 0, access)
            except TypeError: pass
        create = getattr(self._winreg, "CreateKey", None)
        if not create: raise OSError("winreg 不支持创建键")
        return create(self._winreg.HKEY_CURRENT_USER, path)

    def _set_value(self, key, name, value):
        setter = getattr(self._winreg, "SetValueEx", None)
        if not setter: raise OSError("winreg 不支持 SetValueEx")
        setter(key, name, 0, self._reg_const("REG_SZ", 1), value)

    def _delete_value(self, key, name):
        deleter = getattr(self._winreg, "DeleteValue", None)
        if deleter:
            try:
                deleter(key, name)
            except FileNotFoundError:
                pass

    def _write_item(self, item, target):
        path = self.key_path(item.operation, target)
        # Remove obsolete registry layouts before writing the Windows cascade.
        self._delete_tree(path + r"\shell")
        self._delete_tree(path + r"\command")
        self._delete_tree(path + r"\ExtendedSubCommandsKey")
        for child in item.children_for(target):
            self._delete_tree(
                self.command_store_submenu_key_path(item.operation, item.submenu_key(child.key))
            )
        access = self._reg_const("KEY_WRITE", 0x20006)
        children = item.children_for(target)
        key = self._create_key(path, access)
        try:
            self._set_value(key, "", "" if children else item.title)
            self._set_value(key, "MUIVerb", item.title)
            self._set_value(key, "Icon", self.executable_path)
            self._set_value(key, "osu-toolbox-managed", "1")
            self._delete_value(key, "SubCommands")
            self._delete_value(key, "ExtendedSubCommandsKey")
            if children:
                self._set_value(
                    key, "ExtendedSubCommandsKey", self._extended_subcommands_key(item, target)
                )
                submenu_root = self._create_key(path + r"\shell", access)
                self._close(submenu_root)
                for child in children:
                    child_path = self.submenu_key_path(item.operation, target, child.key)
                    child_key = self._create_key(child_path, access)
                    try:
                        self._set_value(child_key, "", child.title)
                        self._set_value(child_key, "MUIVerb", child.title)
                        self._set_value(child_key, "Icon", self.executable_path)
                        self._set_value(child_key, "osu-toolbox-managed", "1")
                        child_command = self._create_key(child_path + r"\command", access)
                        try:
                            self._set_value(
                                child_command,
                                "",
                                self.expected_command(
                                    item.operation,
                                    child.target_format,
                                    prompt_speed=child.prompt_speed,
                                    difficulty_marker=child.difficulty_marker,
                                ),
                            )
                        finally:
                            self._close(child_command)
                    finally:
                        self._close(child_key)
            else:
                command = self._create_key(path + r"\command", access)
                try:
                    self._set_value(command, "", self.expected_command(item.operation))
                finally:
                    self._close(command)
        finally:
            self._close(key)
        if item.folder:
            # Also register the right click on empty space inside the folder.
            background = self.background_key_path(item.operation)
            self._delete_tree(background)
            background_key = self._create_key(background, access)
            try:
                self._set_value(background_key, "MUIVerb", item.title)
                self._set_value(background_key, "Icon", self.executable_path)
                self._set_value(background_key, "osu-toolbox-managed", "1")
                self._set_value(
                    background_key,
                    "ExtendedSubCommandsKey",
                    self._extended_subcommands_key(item, target),
                )
            finally:
                self._close(background_key)
    def _item_status(self, item):
        registered = []
        missing = []
        mismatched = []
        errors = []
        for target in item.target_keys:
            children = item.children_for(target)
            try:
                path = self.key_path(item.operation, target)
                key = self._open_key(path, self._reg_const("KEY_READ", 0x20019))
                try:
                    expected = {
                        "": "" if children else item.title,
                        "MUIVerb": item.title,
                        "Icon": self.executable_path,
                        "osu-toolbox-managed": "1",
                    }
                    if children:
                        expected["ExtendedSubCommandsKey"] = self._extended_subcommands_key(
                            item, target
                        )
                    metadata = all(
                        str(self._winreg.QueryValueEx(key, name)[0]).casefold()
                        == str(value).casefold()
                        for name, value in expected.items()
                    )
                except (OSError, KeyError):
                    metadata = False
                finally:
                    self._close(key)

                if children:
                    parent_match = True
                    children_match = True
                    for child in children:
                        child_command = self._open_key(
                            self.submenu_command_key_path(item.operation, target, child.key),
                            self._reg_const("KEY_READ", 0x20019),
                        )
                        try:
                            children_match = (
                                children_match
                                and str(self._winreg.QueryValueEx(child_command, "")[0]).casefold()
                                == self.expected_command(
                                    item.operation,
                                    child.target_format,
                                    prompt_speed=child.prompt_speed,
                                    difficulty_marker=child.difficulty_marker,
                                ).casefold()
                            )
                        finally:
                            self._close(child_command)
                else:
                    command = self._open_key(
                        path + r"\command", self._reg_const("KEY_READ", 0x20019)
                    )
                    try:
                        parent_match = (
                            str(self._winreg.QueryValueEx(command, "")[0]).casefold()
                            == self.expected_command(item.operation).casefold()
                        )
                    finally:
                        self._close(command)
                    children_match = True

                background_match = self._background_matches(item, target) if item.folder else True

                if metadata and parent_match and children_match and background_match:
                    registered.append(target)
                else:
                    mismatched.append(target)
            except (FileNotFoundError, KeyError):
                missing.append(target)
            except Exception as exc:
                errors.append(str(exc))
        executable_exists = os.path.isfile(self.executable_path)
        if errors or mismatched or (registered and missing):
            state = "needs_repair"
        elif registered and not executable_exists:
            state = "needs_repair"
            errors.append(f"\u53f3\u952e\u83dc\u5355\u5165\u53e3\u4e0d\u5b58\u5728\uff1a{self.executable_path}")
        elif registered:
            state = "enabled"
        else:
            state = "disabled"
        return item.as_dict(
            self.executable_path,
            {
                "enabled": state == "enabled",
                "healthy": state in ("enabled", "disabled"),
                "state": state,
                "registered_extensions": registered,
                "missing_extensions": missing,
                "mismatched_extensions": mismatched,
                "errors": errors,
                "executable_exists": executable_exists,
            },
        )

    def _background_matches(self, item, target):
        """Return whether the ``Directory\\Background`` sibling key is correct."""
        try:
            key = self._open_key(
                self.background_key_path(item.operation), self._reg_const("KEY_READ", 0x20019)
            )
        except (FileNotFoundError, KeyError):
            return False
        try:
            expected = {
                "MUIVerb": item.title,
                "Icon": self.executable_path,
                "osu-toolbox-managed": "1",
                "ExtendedSubCommandsKey": self._extended_subcommands_key(item, target),
            }
            return all(
                str(self._winreg.QueryValueEx(key, name)[0]).casefold()
                == str(value).casefold()
                for name, value in expected.items()
            )
        except (OSError, KeyError):
            return False
        finally:
            self._close(key)

    def _delete_tree(self, path):
        try:
            key = self._open_key(path, self._reg_const("KEY_READ", 0x20019))
        except (FileNotFoundError, KeyError):
            return
        try:
            for child_name in self._subkey_names(key):
                self._delete_tree(path + "\\" + child_name)
        finally:
            self._close(key)
        deleter = getattr(self._winreg, "DeleteKeyEx", None) or getattr(self._winreg, "DeleteKey", None)
        if not deleter:
            raise OSError("winreg 不支持删除键")
        try:
            try:
                deleter(self._winreg.HKEY_CURRENT_USER, path, 0, 0)
            except TypeError:
                deleter(self._winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            return
        try:
            key = self._open_key(path, self._reg_const("KEY_READ", 0x20019))
        except (FileNotFoundError, KeyError):
            return
        self._close(key)
        raise OSError(f"注册表项删除后仍然存在：{path}")

__all__=("SHELL_MENU_COMMAND_STORE_PATH","SHELL_MENU_EXE_NAME","SHELL_MENU_ITEMS","SHELL_MENU_SCOPE","SHELL_MENU_SETTING_GROUPS","SHELL_MENU_SETTING_PARAMS","ShellMenuChild","ShellMenuItem","ShellMenuManager","default_shell_menu_settings","normalize_shell_menu_settings","resolve_shell_executable","shell_menu_command","shell_menu_schema")
