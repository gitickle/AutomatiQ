# Changelog

All notable changes to AutomatiQ are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] — 2026-06-24

### Added
- **WebSocket recording** — full capture of WebSocket connections (text + binary frames, control frames) alongside HTTP traffic. Each connection is compiled into its own folder under `session_dump/websockets/` with a `transaction.json` and individual frame files named `{seq}_{direction}_{delta_ms}ms{opcode_suffix}.{ext}`.
- **Disk-streaming recorder** — recording data now streams directly to a temp directory during the session instead of accumulating in memory. Eliminates memory pressure on long recordings.
- **Live recording spinner** — animated spinner shows active recording status in the terminal.
- **Unified crash report system** — if the recorder crashes mid-session, a structured crash report is saved alongside the partial session dump.
- **WebSocket knowledge in agent system prompt** — the internal agent now understands the `websockets/` directory structure, frame file naming convention, timestamp reconstruction, and `websockets` library usage for replay scripts.
- **"Read the JS, not the ciphertext" principle** — agent prompt now instructs tracing JavaScript crypto logic rather than attempting manual decryption of encrypted payloads.
- `websockets` library declared as a runtime dependency so generated WebSocket replay scripts work out of the box.
- NodeMaven sponsor section in README and AGENTS.md with promo codes (`AUTOMATIQ35`, `AUTOMATIQ40`).
- `--target` CLI option documented in README.

### Changed
- Default agent model updated to `gemini/gemini-3.5-flash`.
- `max_steps` default clarified as `100` (was documented as `60` in README).
- Build system requirement bumped to `setuptools>=77` for PEP 639 SPDX license support.
- AGENTS.md trimmed to 59 lines following lean documentation guidelines.
- Recorder docstring import path corrected (`automatiq.core.recorder`, not `automatiq.recorder`).

### Fixed
- Magika content detection now runs before body file copy in `data_compressor.py` — fixes pre-existing `.bin` extension bug on detected files.
- New tabs have a reduced (~10ms) blind window for WebSocket events via a polling loop and reordered `network.enable` command.
- `active_websockets[rid]` is set before file I/O and not popped on `WebSocketClosed` — late-arriving frames still get sequence numbers.

### Removed
- `pydantic-pick` dependency (declared but never imported).
- `requirements.txt` (stale and diverged from `pyproject.toml`; `uv.lock` is the canonical lock file).

## [0.2.0] — 2026-05-28

### Added
- **UI/backend decoupling** — business logic split into `src/automatiq/core/`, presentation into `src/automatiq/cli/`. Communication via [Blinker](https://blinker.readthedocs.io/) pub/sub events.
- **Cross-platform CI** — GitHub Actions workflow runs `pytest` on Ubuntu, macOS, and Windows across Python 3.11, 3.12, and 3.13.
- Comprehensive test suite for the IPython sandbox (execution, cancellation, `rg`/`jq`/`gron` integration).
- Integration tests for the main agent loop, state machine, and Blinker event architecture.
- Pydantic core schema validation tests.
- Background sandbox preloading during the startup banner for faster first-cell execution.
- PyPI downloads badge in README.

### Changed
- Migrated development packages from `optional-dependencies` to `dependency-groups` for seamless `uv sync`.
- Terminal logs routed through a centralized Rich console — timestamps removed from log output.
- Agent output rendering inverted to an Event Router pattern.

### Fixed
- Python 3.12 thread deadlocks in the sandbox causing test hangs.
- Clean thread exit on hard aborts (Ctrl+C).
- CDP network noise silenced on EOF.
- Relative import bug in `cli/callbacks.py`.

## [0.1.3] — 2026-05-08

### Added
- **GitHub Copilot support** — OAuth-based authentication with helpful error messages for unsupported models.
- `prompt_toolkit` migration for multiline input and safe readline handling.
- Agent session history saved in timestamped subdirectories under `~/.automatiq/history/`.
- Background Esc listener migrated to `prompt_toolkit` for cross-platform robustness.
- Log file renamed to match the compiled workspace session name.

### Changed
- Recorder transitioned to the events system with AI-enhanced session naming.
- `gron` prioritized over `jq` for JSON exploration in agent prompts.
- Model names bumped to latest Gemini versions.

### Fixed
- Shadow DOM clicks, cross-origin iframe keystrokes, and missed click capture in the recorder.
- `stop_token` monitoring during agent loop — session history now saves on Ctrl+C (Linux).
- Duplicated provider prefix in model suggestions.
- Log events routing through `console.py` to restore recorder UI.

## [0.1.2] — 2026-05-05

### Added
- Sandbox preloaded in background during startup banner.
- Local models guide (Ollama, LM Studio, vLLM) in README.
- PyPI downloads badge.

### Changed
- README restructured for clarity and formatting.
- Debug statements removed.

## [0.1.1] — 2026-04-28

### Added
- Scoop shim resolver for Windows PATH detection.
- System binary copy fallback in `bin_manager.py`.
- Agent prompt updates.

## [0.1.0] — 2026-04-23

### Added
- Initial alpha release.
- CDP-based browser recorder (HTTP requests, responses, cookies, user interactions).
- Vision LLM analysis of per-action video clips.
- IPython sandboxed agent with `reading` / `testing` / `building` modes.
- Rich terminal UI with animated banner, live spinner, and markdown rendering.
- CLI flags (`--model`, `--recorder-model`, `--base-url`, `--max-steps`, `--sandbox-timeout`, `--output-dir`, `--no-banner`, `--verbose`).
- LiteLLM integration for multi-provider model support.
- `~/.automatiq/config.toml` persistent configuration.
- Blocklist filtering (StevenBlack + AdAway) for recorded network traffic.
