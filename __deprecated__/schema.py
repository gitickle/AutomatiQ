from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ModeEnum(str, Enum):
    reading = "reading"
    testing = "testing"
    building = "building"


class ToolEnum(str, Enum):
    execute_ipython = "execute_ipython"
    switch_mode = "switch_mode"
    message_to_user = "message_to_user"
    final_submit = "final_submit"


class PythonScript(BaseModel):
    ipython_script: str

    @field_validator("ipython_script")
    def validate_syntax(cls, v: str) -> str:
        code = v.strip()
        if not code:
            raise ValueError("Script cannot be empty.")

        import ast

        # Allow internal magics
        if code.startswith("%reset") or code.startswith("%restore") or code.startswith("%view_output"):
            return code

        # Strip simple IPy magics for syntax checking
        lines = []
        for line in code.splitlines():
            if line.strip().startswith("!") or line.strip().startswith("%"):
                continue
            lines.append(line)
        clean_code = "\n".join(lines)

        try:
            ast.parse(clean_code)
        except SyntaxError as e:
            if "unexpected EOF" in str(e) or "unterminated string literal" in str(e) or "unclosed" in str(e):
                raise ValueError(f"Incomplete code: the cell expects more lines. Error: {e}") from e
            raise ValueError(f"Syntax error: the IPython code is invalid. Error: {e}") from e
        return v


class Message(BaseModel):
    message_to_user: str
    does_it_contain_the_final_script: bool


class ModeSwitchRequest(BaseModel):
    target_mode: ModeEnum
    context: str


class AssistantResponse(BaseModel):
    thought_process: str
    tool: ToolEnum
    tool_content: PythonScript | Message | ModeSwitchRequest

    @field_validator("thought_process")
    def validate_thought_process(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("thought_process cannot be empty")
        if len(v) < 40:
            raise ValueError("thought_process is too short")
        return v


AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_ipython",
            "description": "Execute a python or ipython script in the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": (
                            "Your internal reasoning for this turn, written as markdown. "
                            "Write 1-3 sentences: what you observed, what you believe, "
                            "what you will do next and why. MUST NOT be empty."
                        ),
                    },
                    "ipython_script": {
                        "type": "string",
                        "description": (
                            "A single IPython cell to execute. Supports standard Python plus IPython features: "
                            "magic commands (%cd, %timeit, %%timeit, %run, %store, %macro, %edit, %prun), "
                            "shell access via ! prefix (!ls, var = !cmd), "
                            "dynamic introspection (obj?, obj??), "
                            "namespace search with wildcards (%psearch, *pattern*), "
                            "auto-parentheses (%autocall: sin 3 -> sin(3)), "
                            "$ variable expansion in shell commands (!echo $myvar), "
                            "and custom sandbox commands: "
                            "%reset (wipe all variables, imports, and history — fresh kernel), "
                            "%restore (re-run all previously successful cells to recover state after a crash or reset), "
                            "%view_output Cell_N [--offset Y] (page through long output of a past cell). "
                            "Code must be syntactically complete — no dangling blocks or unclosed brackets."
                        ),
                    },
                },
                "required": ["thought_process", "ipython_script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_mode",
            "description": "Switch the agent's current mode (reading, testing, building).",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "Your internal reasoning for this turn, written as markdown.",
                    },
                    "target_mode": {
                        "type": "string",
                        "enum": ["reading", "testing", "building"],
                        "description": "The mode you want to switch to.",
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "A research memo for yourself in the next mode. Write down what "
                            "you were investigating, observed, confident about, etc."
                        ),
                    },
                },
                "required": ["thought_process", "target_mode", "context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "final_submit",
            "description": "Deliver a message to the user, and optionally submit the final script.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought_process": {
                        "type": "string",
                        "description": "Your internal reasoning for this turn, written as markdown.",
                    },
                    "message_to_user": {"type": "string", "description": "The message to display to the user."},
                    "is_final_script": {
                        "type": "boolean",
                        "description": "Set to True ONLY when this message contains the final deliverable script.",
                    },
                },
                "required": ["thought_process", "message_to_user", "is_final_script"],
            },
        },
    },
]


# Standard inputs remain
class UserMessage(BaseModel):
    message_from_user: str


class ToolResponse(BaseModel):
    tool_response: str


class ModeNotification(BaseModel):
    mode_switched: str


class Input(BaseModel):
    input: UserMessage | ToolResponse | ModeNotification


class StepMetadata(BaseModel):
    compilation_cache: dict[str, dict] = Field(default_factory=dict)


class AgentStep(BaseModel):
    role: Literal["user", "assistant"]
    content: Input | dict  # Accept a raw dict for the assistant's tool call data

    meta: StepMetadata = Field(default_factory=StepMetadata, exclude=True)
