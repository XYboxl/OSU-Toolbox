"""Stable contracts for registered toolbox tools."""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

from osu_toolbox.schema import GroupSpec, ParamSpec


SUPPORTED_RESULT_KINDS = frozenset(("text", "key_value", "table", "file"))
SUPPORTED_CONCURRENCY = frozenset(("exclusive",))

EventEmitter = Callable[[str, Any], None]


class ToolCancelledError(RuntimeError):
    """Raised by a controller after cooperative task cancellation."""


@dataclass(frozen=True)
class ToolSpec:
    id: str
    title: str
    description: str
    category: str
    action_label: str
    groups: Tuple[GroupSpec, ...]
    params: Tuple[ParamSpec, ...]
    result_kind: str
    concurrency: str = "exclusive"

    def as_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "action_label": self.action_label,
            "groups": [group.as_dict() for group in self.groups],
            "params": [param.as_dict() for param in self.params],
            "result_kind": self.result_kind,
            "concurrency": self.concurrency,
        }


@dataclass(frozen=True)
class ToolContext:
    search_root: str
    output_root: Optional[str] = None
    global_settings: Dict[str, Any] = field(default_factory=dict)
    invocation: str = "gui"


@dataclass(frozen=True)
class ConfirmationRequest:
    title: str
    message: str
    confirm_label: str = "继续"

    def as_dict(self):
        return {
            "title": self.title,
            "message": self.message,
            "confirm_label": self.confirm_label,
        }


@dataclass(frozen=True)
class PreparedRun:
    params: Dict[str, Any]
    warnings: Tuple[str, ...] = ()
    confirmation: Optional[ConfirmationRequest] = None


@dataclass(frozen=True)
class PrefillResult:
    params: Dict[str, Any]
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolResult:
    kind: str
    data: Any

    def as_dict(self):
        return {"kind": self.kind, "data": self.data}


class ToolController(Protocol):
    def prepare(self, raw_params, context: ToolContext) -> PreparedRun:
        ...

    def run(
        self,
        prepared: PreparedRun,
        emit_event: EventEmitter,
        cancel_event=None,
    ) -> ToolResult:
        ...


@dataclass(frozen=True)
class ToolDefinition:
    spec: ToolSpec
    controller: ToolController
