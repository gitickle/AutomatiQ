from typing import Any

import yaml

from .schema import AgentStep, ModeEnum


# PyYAML: render multiline strings as literal blocks (|) instead of \n-escaped mess
def multiline_presenter(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.add_representer(str, multiline_presenter)
yaml.representer.SafeRepresenter.add_representer(str, multiline_presenter)


class AgentPromptManager:
    def __init__(
        self,
        name: str,
        system_prompt: str,
        rule_dict: dict[str, Any],
        shared_memory: list[AgentStep],
        current_mode: ModeEnum | None = None,
        mode_injections: dict[ModeEnum, str] | None = None,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.rule_dict = rule_dict
        self.steps = shared_memory  # shared mutable reference, not a copy
        self.current_mode = current_mode
        self.mode_injections = mode_injections or {}

    def switch_mode(self, new_mode: ModeEnum):
        self.current_mode = new_mode

    def add_step(self, step: AgentStep):
        self.steps.append(step)

    def _build_system_prompt(self) -> str:
        parts = [self.system_prompt]
        if self.current_mode and self.current_mode in self.mode_injections:
            parts.append(f"\n<current_mode>\n{self.mode_injections[self.current_mode]}\n</current_mode>")
        return "\n".join(parts)

    def compile(self) -> list[dict]:
        """
        Compiles shared memory into LLM-ready messages with O(1) per-step caching.
        Content is rendered as Markdown-wrapped YAML for better LLM readability.
        """
        compiled_prompt = [{"role": "system", "content": self._build_system_prompt()}]
        total_steps = len(self.steps)

        for index, step in enumerate(self.steps):
            stage = "latest" if index >= total_steps - 20 else "history"
            cache_key = f"{self.name}_{stage}_yaml"

            if cache_key not in step.meta.compilation_cache:
                TargetModel = self.rule_dict.get(stage, AgentStep)
                compressed_step = TargetModel(**step.model_dump(exclude_none=True))
                raw_dict = compressed_step.model_dump(exclude_none=True)
                content_dict = raw_dict.get("content", {})

                # YAML is easier for LLMs to parse than nested JSON
                if isinstance(content_dict, dict) and content_dict:
                    yaml_str = yaml.dump(content_dict, sort_keys=False, allow_unicode=True)
                    markdown_content = f"```yaml\n{yaml_str.strip()}\n```"
                else:
                    markdown_content = str(content_dict)

                step.meta.compilation_cache[cache_key] = {"role": raw_dict["role"], "content": markdown_content}

            compiled_prompt.append(step.meta.compilation_cache[cache_key])

        return compiled_prompt


class PromptFactory:
    @staticmethod
    def create_agent(agent_type: str, shared_memory: list[AgentStep] | None = None) -> AgentPromptManager:
        memory = shared_memory if shared_memory is not None else []

        if agent_type.lower() == "main":
            from .agent import create_agent

            return create_agent(shared_memory=memory)
        else:
            raise ValueError(f"Unknown agent type: {agent_type}")
