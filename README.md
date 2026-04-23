<p align="center">
  <img src="assets/automatiq_banner.svg" alt="AutomatiQ" width="600">
</p>

<p align="center">
  <em>Your <span style="color:#00FFC8;font-weight:bold">activity</span>, into <span style="color:#FF009E;font-weight:bold">automation</span>.</em>
</p>

<p align="center">
  <a href="https://discord.gg/8j7dFWMMDA">
    <img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat&logo=discord&logoColor=white" alt="Discord">
  </a>
</p>

# AutomatiQ

> **Alpha (v0.1.0)** => Work in progress. Things will break, change, and improve.

Record a browser session, and an AI agent reverse-engineers it into a standalone Python script.

## Install

```bash
git clone https://github.com/StoneSteel27/AutomatiQ.git
cd AutomatiQ
pip install uv
uv pip install -e .
```

### Dev setup

```bash
uv pip install -e ".[dev]"
pre-commit install
```

This installs `ruff` and `pre-commit` hooks (lint + format on every commit).

## Configuration

On first run, AutomatiQ creates `~/.automatiq/config.toml` with commented defaults. Edit it to override models, timeouts, recording settings, etc.

```toml
[models]
agent    = "gemini/gemini-3-flash-preview"
recorder = "gemini/gemini-3.1-flash-lite-preview"
# base_url = "http://localhost:11434/v1"   # Ollama / LM Studio / vLLM

[agent]
max_steps       = 60
sandbox_timeout = 60

[recording]
fps                   = 3
segment_pad           = 2
merge_gap_threshold   = 1.5
max_frames_per_prompt = 8
```

Priority: **CLI flag** > `~/.automatiq/config.toml` > built-in defaults.

Set your API key in a `.env` file at the project root (any [litellm](https://docs.litellm.ai/docs/providers)-supported model works):

```
GEMINI_API_KEY=your-key-here
```

## Run

```bash
# Record a session, then have the agent build a scraper
automatiq run https://example.com

# Or run each step separately
automatiq record https://example.com   # just record
automatiq agent                         # build scraper from last recording
```

CLI flags override config:

```bash
automatiq run https://example.com --model openai/gpt-4o --max-steps 80
```

## What it does

1. **Record:** Opens Chrome, captures your browsing (video, network requests, user actions).
2. **Agent:** An LLM investigator reads the session dump, experiments in a sandboxed IPython environment, and produces a working scraping script.

## Requirements

- Python 3.11+
- A supported LLM API key (Gemini, OpenAI, OpenRouter, or any OpenAI-compatible endpoint via `--base-url`)

## License

MIT
