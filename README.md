<p align="center">
  <img src="https://raw.githubusercontent.com/StoneSteel27/AutomatiQ/main/assets/automatiq_banner.svg" alt="AutomatiQ" width="600">
</p>

<p align="center">
  <em>Your <span style="color:#00FFC8;font-weight:bold">activity</span>, into <span style="color:#FF009E;font-weight:bold">automation</span>.</em>
</p>

<p align="center">
  <a href="https://discord.gg/8j7dFWMMDA">
    <img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord">
  </a>
  <img src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-violet?style=flat-square" alt="License">
</p>
<p align="center">
  <a href="https://github.com/StoneSteel27/AutomatiQ/actions/workflows/test.yaml">
    <img src="https://img.shields.io/github/actions/workflow/status/StoneSteel27/AutomatiQ/test.yaml?branch=main&label=tests&style=flat-square" alt="Test Status">
  </a>
  <a href="https://github.com/StoneSteel27/AutomatiQ/actions/workflows/lint.yaml">
    <img src="https://img.shields.io/github/actions/workflow/status/StoneSteel27/AutomatiQ/lint.yaml?branch=main&label=lint&style=flat-square" alt="Lint Status">
  </a>
</p>

# AutomatiQ

> **Alpha** — Things will break and change. Read [VISION.md](https://github.com/StoneSteel27/AutomatiQ/blob/main/VISION.md) to understand why Automatiq exists and where it's headed.

AutomatiQ watches you browse, then an AI agent reverse-engineers your session
into a standalone Python automation/extraction script; no manual inspection needed.

## What it does

```
1. RECORD                      2. COMPILE                        3. AGENT
   Browse a website     ==>       AI analyses video      ==>        LLM investigates,
   normally                       clips & network                   writes & tests
                                  requests                          a Python script
```

1. **Record** — Opens Chrome, captures your browsing (screen video, network
   requests, user actions). Press `Ctrl+C` when you're done.
2. **Compile** — Vision AI analyses video clips around each action; network
   requests are decoded, deduplicated, and structured into a workspace dump.
3. **Agent** — An LLM investigator reads the workspace, experiments in a
   sandboxed IPython environment, and iteratively produces a working script.

## Quick start

```bash
pip install automatiq
```

Set your API key (any [litellm](https://docs.litellm.ai/docs/providers)-supported model):

```
GEMINI_API_KEY=your-key-here
```

Run:

```bash
automatiq run https://example.com
```

That's it. Browse the site, press `Ctrl+C`, and the agent takes over.

## Keyboard shortcuts

| Phase | Key | Action |
|:-----:|:---:|:------:|
| Recording | `Ctrl+C` | Stop recording and save session |
| Compilation | `Esc` | Skip AI analysis for remaining segments |
| Compilation | `y` / `n` | Confirm or deny the skip prompt |
| Agent | `q` | Quit the agent session |
| Agent | `Esc` | Cancel current LLM call or code execution |

`Ctrl+C` force-quits at any phase.

## CLI options

| Flag | Description |
|------|-------------|
| `--model MODEL` | LiteLLM model string for the agent |
| `--recorder-model MODEL` | Vision model for video-clip analysis |
| `--base-url URL` | Custom OpenAI-compatible API endpoint |
| `--max-steps N` | Maximum agent loop iterations (default: 60) |
| `--sandbox-timeout SEC` | Seconds per IPython cell (default: 60) |
| `--output-dir PATH` | Root directory for all output (default: ./output) |
| `--no-banner` | Skip the startup animation |
| `--verbose` | Show detailed diagnostic output |
| `-V`, `--version` | Show version |
| `-h`, `--help` | Show help message |

## How it works

- **Browser capture** — Chrome is launched with CDP instrumentation. Every
  network request, response body, cookie, and user interaction (clicks, typing,
  navigation) is recorded with timestamps.
- **Vision analysis** — The recording is split into per-action video clips.
  A vision LLM watches each clip and produces structured annotations (what was
  clicked, what changed, whether the action succeeded).
- **Sandboxed agent** — The investigator runs Python code in an isolated IPython
  worker process. It can read the captured data, test hypotheses against the live
  site, and build the final script incrementally, with guardrails against loops
  and repetition.

## Configuration

On first run, AutomatiQ creates `~/.automatiq/config.toml` with commented
defaults. Edit it to override models, timeouts, recording settings, etc.

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

### Step-by-step usage

```bash
automatiq record https://example.com   # just record
automatiq agent                         # build automation script from last recording
```

### Install from source

AutomatiQ is managed using [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/StoneSteel27/AutomatiQ.git
cd AutomatiQ
uv sync
uv run automatiq run https://example.com
```

### Dev setup

Development dependencies (pytest, ruff, pre-commit, etc.) are installed automatically when you run `uv sync`. To set up the git hooks:

```bash
uv sync
uv run pre-commit install
```

Run tests and benchmarks:

```bash
uv run pytest
```

This ensures `ruff`, `build`, `twine`, `pytest`, and `pre-commit` hooks (lint + format on every commit) are properly configured in your isolated environment.

## Requirements

- Python 3.11+
- A supported LLM API key (Gemini, OpenAI, OpenRouter, or any
  OpenAI-compatible endpoint via `--base-url`)

## License

MIT
