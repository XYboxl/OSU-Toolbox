"""Generic parameter schema shared by registered tools.

The spec classes are written by hand instead of with ``@dataclass``: this module
sits on the hot path of every entry point (including the Explorer right-click
menu), and ``dataclasses`` drags in ``inspect`` + ``ast`` + ``dis`` for about
65 ms of interpreter time.  Each class is therefore immutable, comparable and
hashable by its own fields, with no library cost at import.
"""

import math

from typing import Any, Dict, Iterable, Optional, Tuple


SUPPORTED_PARAM_KINDS = frozenset((
    "bool",
    "choice",
    "float",
    "int",
    "optional_float",
    "optional_int",
    "path",
    "text",
))



class _Spec:
    """Shared comparison and repr behaviour for the parameter specs.

    The specs are treated as read-only constants by every caller; equality,
    hashing and ``repr`` match what ``@dataclass(frozen=True)`` produced.
    """

    __slots__ = ()

    def __repr__(self):
        fields = ", ".join(
            f"{name}={getattr(self, name)!r}" for name in self._fields()
        )
        return f"{type(self).__name__}({fields})"

    def __eq__(self, other):
        if type(other) is not type(self):
            return NotImplemented
        return all(
            getattr(self, name) == getattr(other, name) for name in self._fields()
        )

    def __hash__(self):
        return hash((type(self).__name__, *(getattr(self, name) for name in self._fields())))

    def _fields(self):
        raise NotImplementedError


class ChoiceSpec(_Spec):
    __slots__ = ("key", "label", "value", "aliases")

    def __init__(self, key, label, value, aliases=()):
        self.key = key
        self.label = label
        self.value = value
        self.aliases = tuple(aliases)

    @staticmethod
    def _fields():
        return ("key", "label", "value", "aliases")

    def as_dict(self):
        return {"key": self.key, "label": self.label, "value": self.value}


class GroupSpec(_Spec):
    __slots__ = ("key", "label", "collapsible")

    def __init__(self, key, label, collapsible=False):
        self.key = key
        self.label = label
        self.collapsible = collapsible

    @staticmethod
    def _fields():
        return ("key", "label", "collapsible")

    def as_dict(self):
        return {
            "key": self.key,
            "label": self.label,
            "collapsible": self.collapsible,
        }


class ParamSpec(_Spec):
    __slots__ = (
        "key", "label", "kind", "default", "group", "help", "unit",
        "minimum", "maximum", "choices", "dialog_mode", "filters",
        "required", "persist", "multiple", "placeholder",
    )

    def __init__(
        self,
        key,
        label,
        kind,
        default,
        group,
        help,
        unit="",
        minimum=None,
        maximum=None,
        choices=(),
        dialog_mode=None,
        filters=(),
        required=False,
        persist=True,
        multiple=False,
        placeholder="",
    ):
        self.key = key
        self.label = label
        self.kind = kind
        self.default = default
        self.group = group
        self.help = help
        self.unit = unit
        self.minimum = minimum
        self.maximum = maximum
        self.choices = tuple(choices)
        self.dialog_mode = dialog_mode
        self.filters = tuple(filters)
        self.required = required
        self.persist = persist
        self.multiple = multiple
        # Shown inside an empty text input as a format hint (for example the
        # marker pattern ``{num}x``).  Never persisted: it is display only.
        self.placeholder = placeholder

    @staticmethod
    def _fields():
        return (
            "key", "label", "kind", "default", "group", "help", "unit",
            "minimum", "maximum", "choices", "dialog_mode", "filters",
            "required", "persist", "multiple", "placeholder",
        )

    def as_dict(self):
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "group": self.group,
            "help": self.help,
            "unit": self.unit,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": [choice.as_dict() for choice in self.choices],
            "dialog_mode": self.dialog_mode,
            "filters": [list(item) for item in self.filters],
            "required": self.required,
            "persist": self.persist,
            "multiple": self.multiple,
            "placeholder": self.placeholder,
        }


class ParamValidationError(ValueError):
    def __init__(self, key, message):
        super().__init__(message)
        self.key = key


def coerce_param(spec, value):
    if value is None:
        value = spec.default
    if spec.kind in ("path", "text"):
        text = str(value or "").strip()
        return text or None
    if spec.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "1", "yes", "on"):
                return True
            if normalized in ("false", "0", "no", "off", ""):
                return False
        raise ParamValidationError(spec.key, "请输入有效的开关值")
    if spec.kind == "choice":
        if spec.multiple:
            if value is None or value == "":
                return None
            values = value if isinstance(value, (list, tuple, set)) else [value]
            selected = []
            for item in values:
                matched = None
                for choice in spec.choices:
                    alias_match = any(
                        type(item) is type(alias) and item == alias
                        for alias in choice.aliases
                    )
                    if item == choice.key or item == choice.value or alias_match:
                        matched = choice.value
                        break
                if matched is None:
                    raise ParamValidationError(spec.key, f"{spec.label}包含不支持的选项")
                if matched not in selected:
                    selected.append(matched)
            return selected
        for choice in spec.choices:
            alias_match = any(
                type(value) is type(alias) and value == alias
                for alias in choice.aliases
            )
            if value == choice.key or value == choice.value or alias_match:
                return choice.value
        raise ParamValidationError(spec.key, f"{spec.label}包含不支持的选项")
    if spec.kind in ("optional_int", "optional_float"):
        if value is None or str(value).strip() == "":
            return None
        parser = int if spec.kind == "optional_int" else float
        expected = "整数" if parser is int else "数字"
        try:
            parsed = parser(value)
            if parser is float and not math.isfinite(parsed):
                raise ValueError("non-finite number")
            return parsed
        except (TypeError, ValueError, OverflowError):
            raise ParamValidationError(spec.key, f"{spec.label}必须是{expected}")
    if spec.kind not in ("int", "float"):
        return value
    if value is None or str(value).strip() == "":
        value = spec.default
    parser = int if spec.kind == "int" else float
    expected = "整数" if parser is int else "数字"
    try:
        parsed = parser(value)
        if parser is float and not math.isfinite(parsed):
            raise ValueError("non-finite number")
    except (TypeError, ValueError, OverflowError):
        raise ParamValidationError(spec.key, f"{spec.label}必须是{expected}")
    if spec.minimum is not None:
        parsed = max(spec.minimum, parsed)
    if spec.maximum is not None:
        parsed = min(spec.maximum, parsed)
    return int(parsed) if spec.kind == "int" else float(parsed)


def coerce_params(specs, raw):
    raw = raw if isinstance(raw, dict) else {}
    return {
        spec.key: coerce_param(spec, raw.get(spec.key, spec.default))
        for spec in specs
    }


def default_params(specs):
    return {spec.key: spec.default for spec in specs}


def schema_json(specs):
    return [spec.as_dict() for spec in specs]


def sanitize_persisted_params(raw, specs):
    raw = raw if isinstance(raw, dict) else {}
    result = {}
    for spec in specs:
        if not spec.persist or spec.key not in raw:
            continue
        try:
            result[spec.key] = coerce_param(spec, raw[spec.key])
        except ParamValidationError:
            continue
    return result


def persistent_params(params, specs: Iterable[ParamSpec]):
    return {
        spec.key: params[spec.key]
        for spec in specs
        if spec.persist and spec.key in params
    }
