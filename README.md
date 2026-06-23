<p align="center">
  <img src="https://raw.githubusercontent.com/StoneSteel27/AutomatiQ/main/assets/automatiq_banner.svg" alt="AutomatiQ" width="600">
</p>

<p align="center">
  <em>Your <span style="color:#00FFC8;font-weight:bold">activity</span>, into <span style="color:#FF009E;font-weight:bold">automation</span>.</em>
</p>

<p align="center">
  <a href="https://discord.gg/8j7dFWMMDA"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord"></a>
  <img src="https://img.shields.io/badge/Python-3.11+-blue?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/License-MIT-violet?style=flat-square" alt="License">
</p>
<p align="center">
  <a href="https://github.com/StoneSteel27/AutomatiQ/actions/workflows/test.yaml"><img src="https://img.shields.io/github/actions/workflow/status/StoneSteel27/AutomatiQ/test.yaml?branch=main&label=Tests&style=flat-square&logo=github" alt="Test Status"></a>
  <a href="https://github.com/StoneSteel27/AutomatiQ/actions/workflows/lint.yaml"><img src="https://img.shields.io/github/actions/workflow/status/StoneSteel27/AutomatiQ/lint.yaml?branch=main&label=Lint&style=flat-square&logo=python&logoColor=white" alt="Lint Status"></a>
  <img src="https://img.shields.io/pypi/dm/automatiq?style=flat-square&color=violet&cacheSeconds=0" alt="PyPI Downloads">
</p>

# AutomatiQ

> [!Note]
> **Alpha** ⟶ Things will break and change. Read [VISION.md](https://github.com/StoneSteel27/AutomatiQ/blob/main/VISION.md) to understand why Automatiq exists and where it's headed.

AutomatiQ watches you browse, then an AI agent reverse-engineers your session into a standalone Python automation/extraction script; no manual inspection needed.

## How it works

<p align="center">
  <img src="https://raw.githubusercontent.com/StoneSteel27/AutomatiQ/main/assets/process.svg" alt="AutomatiQ" width="800">
</p>

1. **Record (Browser Capture)** ⟶ Chrome is launched with CDP instrumentation. Every network request, response body, cookie, and user interaction (clicks, typing, navigation) is recorded with timestamps. Press `Ctrl+C` when you're done.
2. **Compile (Vision Analysis)** ⟶ The recording is split into per-action video clips. A vision LLM watches each clip and produces structured annotations (what was clicked, what changed, whether the action succeeded). Network requests are decoded, deduplicated, and structured into a workspace dump.
3. **Agent (Sandbox Execution)** ⟶ An LLM investigator reads the workspace dump, experiments in an isolated Python/IPython environment, and iteratively produces a working script. It can test hypotheses against the live site with guardrails against loops and repetition.

## Getting Started

**Requirements:** Python 3.11+

```bash
pip install automatiq
```

Set your API key (AutomatiQ uses Gemini 3 Flash by default, but any [litellm-supported provider](https://docs.litellm.ai/docs/providers) works):

```bash
# On Linux/macOS
export GEMINI_API_KEY=your-key-here

# On Windows (PowerShell)
$env:GEMINI_API_KEY="your-key-here"
```

Run the magic command:

```bash
automatiq run https://example.com
```

That's it. Browse the site, press `Ctrl+C`, and the agent takes over.

## Sponsor

To run web automation and scraping scripts reliably, without hitting rate limits, IP bans, or CAPTCHA blocks due to high fraud scores.

You need a high-quality Proxy Provider like [NodeMaven](https://go.nodemaven.com/automatiq)

<p align="center">
  <a href="https://go.nodemaven.com/automatiq">
    <img src="https://raw.githubusercontent.com/StoneSteel27/AutomatiQ/main/assets/nodemaven_banner.png" alt="NodeMaven - High Quality Proxies" width="600">
  </a>
</p>

[NodeMaven](https://go.nodemaven.com/automatiq) is the most reliable proxy provider, offering the highest quality IPs on the market. It is the ideal solution for automation, web scraping, SEO research, and social media management.

**Why NodeMaven?**
- **99.9% uptime**
- **Sticky sessions** up to 7 days
- **IP filtering:** all proxies have a fraud score <97%
- **No KYC required**
- **Cashback on traffic** - burn GB and earn up to 10% back

🎁 **Special codes for our users, to your save money:**
- `AUTOMATIQ35` - **35% off** Mobile and Residential Proxies
- `AUTOMATIQ40` - **40% off** ISP (Static) Proxies

Maintaining this open-source project sustainably, is made possible thanks to our sponsor, **NodeMaven**.

## Usage Modes

AutomatiQ offers two main ways to operate depending on your workflow:

### 1. All-in-one execution
The `run` command records a session and immediately launches the agent to write the script.
```bash
automatiq run https://example.com
```

### 2. Step-by-step execution
If you want to record multiple sessions, or run the agent later, you can split the process:
```bash
automatiq record https://example.com   # Opens the browser and records your session
automatiq agent                        # Builds an automation script from the last recording
automatiq agent --target path/to/sess  # Builds an automation script from a specific recording
```

## Models & Custom Endpoints

AutomatiQ relies on [LiteLLM](https://github.com/BerriAI/litellm) under the hood, meaning you can easily swap the default Gemini models for OpenAI, Anthropic, GitHub Copilot, or **Local LLMs** (like Ollama, LM Studio, or vLLM).

To change the default models on the fly, use the `--model` (for the Agent) and `--recorder-model` (for Vision compilation) flags.

### Using Local Models (Ollama, LM Studio, vLLM)
If you are running a local inference server with an OpenAI-compatible endpoint, use the `--base-url` flag. You must prefix your model name with `openai/` so LiteLLM knows to route it through the OpenAI protocol.

**Example using Ollama (running locally on port 11434):**
```bash
automatiq run https://example.com \
  --model openai/llama3.3 \
  --recorder-model openai/llava \
  --base-url http://localhost:11434/v1
```

*Note: For a permanent configuration so you don't have to pass flags every time, see the **Configuration** section below.*

## Reference

### Keyboard Shortcuts

| Phase | Key | Action |
|:-----:|:---:|:------:|
| Recording | `Ctrl+C` | Stop recording and save session |
| Compilation | `Esc` | Skip AI analysis for remaining segments |
| Compilation | `y` / `n` | Confirm or deny the skip prompt |
| Agent | `q` | Quit the agent session |
| Agent | `Esc` | Cancel current LLM call or code execution |

*Note: `Ctrl+C` force-quits the application at any phase.*

### CLI Options

| Flag | Description |
|------|-------------|
| `--target PATH` | Path to a specific session folder to run the agent on |
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

### Configuration

On first run, AutomatiQ creates `~/.automatiq/config.toml` with commented defaults. Edit this file to permanently override models, custom endpoints, timeouts, and recording settings.

```toml
[models]
agent    = "gemini/gemini-3-flash-preview"
recorder = "gemini/gemini-3.1-flash-lite-preview"
# base_url = "http://localhost:11434/v1"   # Uncomment for Ollama / LM Studio / vLLM

[agent]
max_steps       = 60
sandbox_timeout = 60

[recording]
fps                   = 3
segment_pad           = 2
merge_gap_threshold   = 1.5
max_frames_per_prompt = 8
```

*Priority order: **CLI flag** > `~/.automatiq/config.toml` > built-in defaults.*

## Development

AutomatiQ is managed using [uv](https://docs.astral.sh/uv/).

```bash
# Clone and setup environment
git clone https://github.com/StoneSteel27/AutomatiQ.git
cd AutomatiQ
uv sync

# Run the project from source
uv run automatiq run https://example.com
```

### Dev Setup
Development dependencies (pytest, ruff, pre-commit, etc.) are installed automatically via `uv sync`. To set up the git hooks:

```bash
uv run pre-commit install
```

Run tests:
```bash
uv run pytest
```
This ensures `ruff`, `build`, `twine`, `pytest`, and `pre-commit` hooks (lint + format on every commit) are properly configured in your isolated environment.

## License

MIT
