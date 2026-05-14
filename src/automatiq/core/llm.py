import json
import logging

import litellm

from . import config

logger = logging.getLogger(__name__)


def extract_message(exc) -> str:
    """Pull a readable summary from an exception, stripping litellm wrapper noise."""
    import re

    def _clean(raw):
        s = str(raw)
        s = re.sub(r"^(?:[\w\.]+:\s*)+", "", s)
        s = re.sub(r"^\w+Exception\s+\w+\s*-\s*", "", s)
        json_match = re.search(r"\{.*\}", s, re.DOTALL)
        if json_match:
            try:
                body = json.loads(json_match.group())
                if "error" in body:
                    err = body["error"]
                    if isinstance(err, dict) and "message" in err:
                        return err["message"]
                    return str(err)
                if "message" in body:
                    return body["message"]
            except json.JSONDecodeError:
                pass
        return s.split("\n")[0][:300]

    return _clean(exc)


def call_llm_blocking(msgs: list[dict], tools: list[dict]):
    """Blocking LLM call to litellm."""
    kwargs = dict(
        model=config.AGENT_MODEL,
        messages=msgs,
        tools=tools,
        tool_choice="auto",
        temperature=0.3,
    )
    if config.API_BASE:
        kwargs["api_base"] = config.API_BASE

    # Enable extended thinking/reasoning for models that support it
    if litellm.supports_reasoning(model=config.AGENT_MODEL):
        kwargs["reasoning_effort"] = "high"

    return litellm.completion(**kwargs)
