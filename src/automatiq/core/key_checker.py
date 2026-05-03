"""
API key validation for AutomatiQ.

Runs before any recorder or agent work begins to give the user a clear,
actionable error if their environment is missing keys or has invalid ones —
rather than failing deep inside the agent loop with a cryptic API error.
"""

import logging
import sys

import litellm

from . import config

logger = logging.getLogger(__name__)


def _validate_model(model: str) -> bool:
    """
    Validate that the environment has what it needs to call `model`.

    Uses litellm.validate_environment() to check all required env vars are
    present. This is the only reliable cross-provider check — litellm's
    check_valid_key() is only trustworthy for OpenAI and returns false
    negatives for Gemini, Anthropic, and others.

    Returns True if everything looks good, False otherwise (errors already printed).
    """
    result = litellm.validate_environment(model)

    missing = result.get("missing_keys", [])
    if missing:
        logger.error(f"Model '{model}' requires environment variables that are not set:")
        for key in missing:
            logger.debug(f"  {key}")
        logger.debug("Set them in your .env file or export them before running.")
        return False

    return True


def check_api_keys(*models: str) -> None:
    """
    Validate API keys for one or more models.

    Prints a summary and exits with code 1 if any model fails validation.
    Call this at the start of every subcommand, after config overrides are applied.

    Usage:
        check_api_keys(config.AGENT_MODEL)
        check_api_keys(config.AGENT_MODEL, config.RECORDER_AI_MODEL)
    """
    # When a custom base URL is set the requests go to a user-controlled
    # endpoint — standard provider API keys are not required.
    if config.API_BASE:
        logger.info(f"Custom endpoint ({config.API_BASE}) — skipping key validation.")
        return

    failed: list[str] = []
    seen: set[str] = set()

    for model in models:
        if model in seen:
            continue
        seen.add(model)

        ok = _validate_model(model)
        if not ok:
            failed.append(model)

    if failed:
        logger.error(f"{len(failed)} model(s) failed key validation — cannot continue.")
        for m in failed:
            logger.debug(f"  {m}")
        sys.exit(1)

    logger.info("[SUCCESS] API keys validated.")
