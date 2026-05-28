import base64
import logging
import os
from datetime import datetime

import yaml

from . import config

logger = logging.getLogger(__name__)


def compress_history(messages: list[dict], cutoff_turn=10) -> list[dict]:
    """
    Truncates massive tool outputs, strips deep thinking blocks, and manages
    provider-specific signatures to save context window.
    """
    compressed = []
    # threshold_idx separates the recent active window from older messages
    threshold_idx = max(0, len(messages) - cutoff_turn)
    cell_counter = 0

    # 1. Determine provider from current agent configuration
    agent_model = getattr(config, "AGENT_MODEL", "").lower()
    provider = ""
    if "/" in agent_model:
        provider = agent_model.split("/")[0]

    is_gemini = provider in ("gemini", "google", "vertex_ai") or "gemini" in agent_model
    is_anthropic = provider in ("anthropic", "vertexai") or "claude" in agent_model

    # Standard Google-recommended dummy signature to bypass validation on older turns
    dummy_sig = base64.b64encode(b"skip_thought_signature_validator").decode()

    # 2. First pass (Gemini only): Map tool call IDs containing '__thought__' to their dummy-sig versions
    # for older messages so we keep the IDs perfectly matching in both assistant and tool messages.
    id_mapping = {}
    if is_gemini:
        for i, msg in enumerate(messages):
            if i < threshold_idx:
                if msg.get("role") == "assistant" and "tool_calls" in msg:
                    for tc in msg["tool_calls"]:
                        tc_id = tc.get("id")
                        if tc_id and "__thought__" in tc_id:
                            base_id = tc_id.split("__thought__")[0]
                            dummy_id = f"{base_id}__thought__{dummy_sig}"
                            id_mapping[tc_id] = dummy_id

    # 3. Second pass: Rebuild the messages list with provider-aware pruning
    for i, msg in enumerate(messages):
        role = msg.get("role")
        is_exec = False

        if role == "tool" and msg.get("name") == "execute_ipython":
            content_str = str(msg.get("content", ""))
            is_failed_val = content_str.startswith("SYSTEM: Tool Validation Error") or content_str.startswith(
                "SYSTEM: Validation failed repeatedly"
            )
            is_dup = content_str.startswith("SYSTEM: You have submitted the exact same description")
            if not is_failed_val and not is_dup:
                cell_counter += 1
                is_exec = True

        # Process assistant messages
        if role == "assistant":
            # Strip deep thinking fields, reasoning_content, and provider_specific_fields
            clean_msg = {
                "role": "assistant",
                "content": msg.get("content") or "",
            }

            # Provider-aware thinking block preservation
            if is_anthropic:
                # For Anthropic, we MUST preserve 'thinking_blocks' to avoid 400 Bad Request
                if "thinking_blocks" in msg:
                    clean_msg["thinking_blocks"] = msg["thinking_blocks"]

            # Clean tool calls if present, mapping older IDs to dummy signatures (Gemini-only)
            if "tool_calls" in msg:
                clean_tool_calls = []
                for tc in msg["tool_calls"]:
                    tc_copy = dict(tc)
                    tc_id = tc_copy.get("id")

                    if is_gemini:
                        if tc_id in id_mapping:
                            tc_copy["id"] = id_mapping[tc_id]
                        elif i < threshold_idx and tc_id and "__thought__" in tc_id:
                            base_id = tc_id.split("__thought__")[0]
                            tc_copy["id"] = f"{base_id}__thought__{dummy_sig}"

                        # Clean signature inside provider fields for older turns
                        if i < threshold_idx:
                            tc_copy.pop("provider_specific_fields", None)
                        else:
                            if "provider_specific_fields" in tc_copy:
                                tc_copy["provider_specific_fields"] = dict(tc_copy["provider_specific_fields"])
                    else:
                        # Non-Gemini models don't use thought signatures, pop metadata fields
                        tc_copy.pop("provider_specific_fields", None)

                    clean_tool_calls.append(tc_copy)
                clean_msg["tool_calls"] = clean_tool_calls

            compressed.append(clean_msg)
            continue

        # Process tool messages
        if role == "tool":
            tool_call_id = msg.get("tool_call_id")
            clean_tool_call_id = tool_call_id

            if is_gemini:
                if tool_call_id in id_mapping:
                    clean_tool_call_id = id_mapping[tool_call_id]
                elif i < threshold_idx and tool_call_id and "__thought__" in tool_call_id:
                    base_id = tool_call_id.split("__thought__")[0]
                    clean_tool_call_id = f"{base_id}__thought__{dummy_sig}"

            # If it's an older message and the output is large, truncate it
            if i < threshold_idx:
                content_str = str(msg.get("content", ""))
                if len(content_str) > 1000:
                    if msg.get("name") == "execute_ipython" and is_exec:
                        trunc_msg = f"use `%view_output Cell_{cell_counter}` to view output of this cell"
                    else:
                        trunc_msg = "<Truncated older tool output to save tokens>"

                    compressed.append(
                        {
                            "role": "tool",
                            "tool_call_id": clean_tool_call_id,
                            "name": msg.get("name"),
                            "content": trunc_msg,
                        }
                    )
                    continue

            # For recent tool messages, keep the clean ID but full content
            compressed.append(
                {
                    "role": "tool",
                    "tool_call_id": clean_tool_call_id,
                    "name": msg.get("name"),
                    "content": msg.get("content"),
                }
            )
            continue

        # For user/system messages, append as-is
        compressed.append(msg)

    return compressed


def export_session_logs(messages: list[dict], session_name: str = "unknown"):
    """Write session logs to output/history/."""

    class _SessionDumper(yaml.Dumper):
        pass

    def multiline_presenter(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    _SessionDumper.add_representer(str, multiline_presenter)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_dir = os.path.join(str(config.HISTORY_DIR), f"{session_name}_{timestamp}")
    os.makedirs(target_dir, exist_ok=True)

    # Save the full trace
    uncompressed_path = os.path.join(target_dir, "messages_full.yaml")
    with open(uncompressed_path, "w", encoding="utf-8") as f:
        yaml.dump(messages, f, Dumper=_SessionDumper, sort_keys=False, allow_unicode=True)
    logger.info(f"Saved full session history to {uncompressed_path}")

    # Save the compressed version exactly as the LLM saw it
    compressed = compress_history(messages, cutoff_turn=20)
    compressed_path = os.path.join(target_dir, "messages_compressed.yaml")
    with open(compressed_path, "w", encoding="utf-8") as f:
        yaml.dump(compressed, f, Dumper=_SessionDumper, sort_keys=False, allow_unicode=True)
    logger.info(f"Saved compressed session history to {compressed_path}")
