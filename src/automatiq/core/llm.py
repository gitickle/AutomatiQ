import difflib
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


def _known_models_for_provider(provider: str) -> list[str]:
    """Return LiteLLM's known model names for a given provider prefix, stripped of the prefix."""
    try:
        models = litellm.models_by_provider.get(provider, [])
        prefix = f"{provider}/"
        return sorted([m[len(prefix) :] if m.startswith(prefix) else m for m in models])
    except Exception:
        return []


def _build_model_help(model: str) -> str:
    """Build a helpful, actionable error message for an invalid model string."""
    if "/" not in model:
        providers = sorted(getattr(litellm, "provider_list", []))
        sample = ", ".join(providers[:8])
        return (
            f"Invalid model string '{model}'. "
            f"Expected format: 'provider/model-name' "
            f"(e.g. 'gemini/gemini-2.5-flash', 'openai/gpt-4o'). "
            f"Known providers include: {sample}. "
            f"Update [models] agent in ~/.automatiq/config.toml."
        )

    provider, raw_model = model.split("/", 1)

    if provider == "github_copilot":
        known = _known_models_for_provider(provider)
        suggestions = difflib.get_close_matches(raw_model, known, n=3, cutoff=0.3)
        hint = f" Did you mean: {', '.join(f'github_copilot/{m}' for m in suggestions)}?" if suggestions else ""
        return (
            f"Unknown GitHub Copilot model '{model}'.{hint} "
            f"GitHub Copilot uses OAuth device flow — no API key needed, "
            f"but the model name must match exactly. "
            f"Known Copilot models: {', '.join(f'github_copilot/{m}' for m in known[:6]) or 'check LiteLLM docs'}."
        )

    known = _known_models_for_provider(provider)
    if not known:
        providers = sorted(getattr(litellm, "provider_list", []))
        suggestions = difflib.get_close_matches(provider, providers, n=3, cutoff=0.4)
        hint = f" Did you mean provider: {', '.join(suggestions)}?" if suggestions else ""
        return f"Unknown provider prefix '{provider}' in model '{model}'.{hint} Expected format: 'provider/model-name'."

    suggestions = difflib.get_close_matches(raw_model, known, n=3, cutoff=0.3)
    if suggestions:
        return (
            f"Unknown model '{model}'. Did you mean: "
            f"{', '.join(f'{provider}/{m}' for m in suggestions)}? "
            f"Check your config.toml or --model flag."
        )

    preview = ", ".join(f"{provider}/{m}" for m in known[:6])
    return f"Unknown model '{model}' for provider '{provider}'. Some known models: {preview}."


def _is_model_error(exc: Exception) -> bool:
    msg = extract_message(exc).lower()
    needles = [
        "llm provider not provided",
        "unable to map your input to a model",
        "invalid model",
        "model not found",
        "unknown model",
        "unsupported model",
        "model is not supported",
    ]
    return any(n in msg for n in needles)


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

    try:
        return litellm.completion(**kwargs)
    except Exception as exc:
        if _is_model_error(exc):
            raise ValueError(_build_model_help(config.AGENT_MODEL)) from exc
        raise
