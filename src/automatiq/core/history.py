import logging
import os

import yaml

from . import config

logger = logging.getLogger(__name__)


def compress_history(messages: list[dict], cutoff_turn=20) -> list[dict]:
    """Truncates massive tool outputs from older messages to save tokens."""
    if len(messages) <= cutoff_turn:
        return messages

    compressed = []
    # System prompt is index 0
    # Everything before (len(messages) - cutoff_turn) gets compressed if it's a huge tool output
    threshold_idx = len(messages) - cutoff_turn
    cell_counter = 0

    for i, msg in enumerate(messages):
        is_exec = False

        if msg.get("role") == "tool" and msg.get("name") == "execute_ipython":
            content_str = str(msg.get("content", ""))

            is_failed_val = content_str.startswith("SYSTEM: Tool Validation Error") or content_str.startswith(
                "SYSTEM: Validation failed repeatedly"
            )
            is_dup = content_str.startswith("SYSTEM: You have submitted the exact same description")

            if not is_failed_val and not is_dup:
                cell_counter += 1
                is_exec = True

        if i < threshold_idx and msg.get("role") == "tool":
            content_str = str(msg.get("content", ""))

            # If the tool output is large, truncate it to save context window
            if len(content_str) > 1000:
                if msg.get("name") == "execute_ipython" and is_exec:
                    trunc_msg = f"use `%view_output Cell_{cell_counter}` to view output of this cell"
                else:
                    trunc_msg = "<Truncated older tool output to save tokens>"

                compressed.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.get("tool_call_id"),
                        "name": msg.get("name"),
                        "content": trunc_msg,
                    }
                )
                continue
        compressed.append(msg)
    return compressed


def export_session_logs(messages: list[dict]):
    """Write session logs to output/history/."""

    class _SessionDumper(yaml.Dumper):
        pass

    def multiline_presenter(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    _SessionDumper.add_representer(str, multiline_presenter)
    history_dir = str(config.HISTORY_DIR)
    os.makedirs(history_dir, exist_ok=True)

    # Save the full trace
    uncompressed_path = os.path.join(history_dir, "messages_full.yaml")
    with open(uncompressed_path, "w", encoding="utf-8") as f:
        yaml.dump(messages, f, Dumper=_SessionDumper, sort_keys=False, allow_unicode=True)
    logger.info(f"Saved full session history to {uncompressed_path}")

    # Save the compressed version exactly as the LLM saw it
    compressed = compress_history(messages, cutoff_turn=20)
    compressed_path = os.path.join(history_dir, "messages_compressed.yaml")
    with open(compressed_path, "w", encoding="utf-8") as f:
        yaml.dump(compressed, f, Dumper=_SessionDumper, sort_keys=False, allow_unicode=True)
    logger.info(f"Saved compressed session history to {compressed_path}")
