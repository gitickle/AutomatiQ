from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(Enum):
    STEP_START = "step_start"  # payload: {"step": int, "prompt_tokens": int}
    THOUGHT = "thought"  # payload: {"text": str}
    TOOL_MESSAGE = "tool_message"  # payload: {"text": str}
    MODE_SWITCH = "mode_switch"  # payload: {"mode": str}
    CODE_EXEC = "code_exec"  # payload: {"script": str}
    CODE_OUTPUT = "code_output"  # payload: {"output": str}
    AGENT_DONE = "agent_done"  # payload: {}


@dataclass
class AgentEvent:
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
