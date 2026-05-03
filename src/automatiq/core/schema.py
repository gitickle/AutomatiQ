from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ToolEnum(str, Enum):
    message_to_user = "message_to_user"
    execute_ipython = "execute_ipython"
    switch_mode = "switch_mode"


class ModeEnum(str, Enum):
    reading = "reading"
    testing = "testing"
    building = "building"


class Message(BaseModel):
    message_to_user: str
    does_it_contain_the_final_script: bool = Field(
        default=False,
        description=(
            "Set to True ONLY when this message contains the final deliverable "
            "script. The script will be executed in a fresh, isolated environment "
            "and validated against the session dump before the user sees it. "
            "If validation fails, you will receive a detailed failure report — "
            "fix the issues and resubmit."
        ),
    )


class PythonScript(BaseModel):
    ipython_script: str = Field(
        description=(
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
        )
    )

    @field_validator("ipython_script")
    @classmethod
    def validate_syntax(cls, v: str) -> str:
        stripped = v.strip()
        # Sandbox-handled magics bypass IPython's transformer
        if any(stripped.startswith(m) for m in ("%reset", "%restore", "%view_output")):
            return v
        from IPython.core.inputtransformer2 import TransformerManager

        tm = TransformerManager()
        status, _ = tm.check_complete(v + "\n")
        if status == "invalid":
            raise ValueError("Syntax error: the IPython code is invalid.")
        if status == "incomplete":
            raise ValueError("Incomplete code: the cell expects more lines (unclosed block, bracket, or string).")
        # check_complete passed — compile the transformed Python to catch deeper errors
        try:
            compile(tm.transform_cell(v), "<cell>", "exec")
        except SyntaxError as e:
            raise ValueError(f"Syntax error: {e.msg} at line {e.lineno}") from e
        return v


class ModeSwitchRequest(BaseModel):
    target_mode: ModeEnum = Field(description="The mode you want to switch to.")
    context: str = Field(
        description=(
            "A research memo for yourself in the next mode. Write down: "
            "what you were investigating, what you observed (with cell references), "
            "what you're confident about vs. what's still uncertain, "
            "what didn't work, what questions are still open, "
            "and what you'd look at next."
        )
    )


class AssistantResponse(BaseModel):
    thought_process: str = Field(
        description=(
            "Your internal reasoning for this turn, written as markdown. "
            "Write 1-3 sentences: what you observed, what you believe, "
            "what you will do next and why. MUST NOT be empty."
        )
    )
    tool: ToolEnum
    tool_content: Message | PythonScript | ModeSwitchRequest

    @field_validator("thought_process")
    @classmethod
    def validate_thought_process(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError(
                "thought_process cannot be empty. Provide at least one "
                "concrete reasoning sentence describing what you are about to do and why."
            )
        if len(v.strip()) < 40:
            raise ValueError(
                f"thought_process is too short ({len(v.strip())} chars). "
                "Write a real sentence explaining your reasoning — "
                "what you learned, what you're trying next, and why."
            )
        return v


class UserMessage(BaseModel):
    message_from_user: str


class ToolResponse(BaseModel):
    tool_response: str


class ModeNotification(BaseModel):
    mode_switched: str


class Input(BaseModel):
    input: UserMessage | ToolResponse | ModeNotification


class StepMetadata(BaseModel):
    """Holds internal state. Will NOT be dumped to the LLM due to exclude=True."""

    compilation_cache: dict[str, dict] = Field(default_factory=dict)


class AgentStep(BaseModel):
    role: Literal["user", "assistant"]
    content: Input | AssistantResponse

    # Invisible to Instructor/OpenAI — manages its own compilation cache
    meta: StepMetadata = Field(default_factory=StepMetadata, exclude=True)
