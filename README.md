# Juice

<p>
  <img alt="License: AGPL v3" src="https://img.shields.io/badge/license-AGPL--3.0-orange">
  <img alt="Python 3.13+" src="https://img.shields.io/badge/python-3.13%2B-blue">
  <img alt="Providers: OpenRouter, Google AI Studio, LinkModel" src="https://img.shields.io/badge/providers-OpenRouter%20%C2%B7%20Google%20%C2%B7%20LinkModel-orange">
</p>

<img width="1040" height="645" alt="Juice running in the browser" src="https://github.com/user-attachments/assets/adb1d35c-0749-4af0-a509-c1bd141f45dd" />

## AI coding agent — right in your browser.

Point Juice at any project folder, tell it what to do, and it reads, writes, edits, searches, and runs code for you. Think of it as having a senior developer sitting next to you — one who can take a high-level request and run with it.

Juice is a local-first agent: the server runs on your machine, the UI is your browser, and your code never leaves your computer. It speaks the OpenAI Chat Completions protocol, so the same agent loop works with **OpenRouter**, **Google AI Studio**, and **LinkModel**.

---

## Why Juice

- **Browser-native.** No terminal-only workflow. Launch the server and work from a clean chat UI at `http://localhost:5000`.
- **Patch-first editing.** Code changes go through atomic, all-or-nothing `apply_patch` calls — multi-file and multi-hunk in a single step, with automatic rollback on failure.
- **Terminal-heavy verification.** Juice inspects the repo, runs builds, linters, type-checkers, and tests with `run_command`, then reads the output instead of guessing.
- **Test-verified changes.** The base prompt requires tests after implementation: add or update them, run them, and keep iterating until green.
- **Mode-aware.** Pick a mode — Build, Debug, Refactor, Test, Review, Plan — from the prompt bar and Juice adapts its workflow without switching tools.
- **Local sessions and presets.** Conversations auto-save to `.juice/`, and per-project presets live in `.juiceconfig`.
- **Remote search.** `search_code` can grep a remote host over SSH as well as the local workspace.

---

## What you can ask it

- *"Build me a todo app with a clean UI"*
- *"Add error handling to my API routes"*
- *"Debug why my tests are failing"*
- *"Refactor this file to use async/await"*
- *"Explain how this codebase is structured"*
- *"Set up a React + Vite project from scratch"*
- *"Search my whole project for deprecated API calls"*
- *"Review my latest diff and add the missing tests"*

---

## Getting started

### You'll need

- **Python 3.13+** (tested up to 3.14)
- An **API key** — from [OpenRouter](https://openrouter.ai/keys) ($1 free credit), [Google AI Studio](https://aistudio.google.com/apikey), **or** [LinkModel](https://linkmodel.ai/en/dashboard)

### 1. Install

```bash
git clone https://github.com/ayaangalaxy2012-hub/Juice
cd juice
pip install flask requests beautifulsoup4
```

### 2. Run

```bash
python3 juice.py
```

You'll see:

```
═══════════════════ JUICE — AI Coding Agent ═══════════════════
  Server running at http://localhost:5000
  Providers supported: OpenRouter, Google AI Studio, LinkModel
```

### 3. Open your browser

Go to **http://localhost:5000**. You'll see a setup screen with these fields:

| Field | What to put |
|-------|------------|
| **Provider** | `openrouter`, `google` or `linkmodel` — choose which LLM provider to use |
| **Working directory** | Path to your project folder (click *validate* to check it) |
| **API key** | Your OpenRouter key (from [openrouter.ai/keys](https://openrouter.ai/keys)), Google AI Studio key (from [aistudio.google.com](https://aistudio.google.com/apikey)), or LinkModel key (from [linkmodel.ai/en/dashboard](https://linkmodel.ai/en/dashboard)) |
| **Model ID** | e.g. `anthropic/claude-sonnet-4-5` (OpenRouter), `gemini-2.0-flash` (Google AI Studio) or `claude-sonnet-5` (LinkModel) — any model from your provider works |
| **Reasoning** | Only shown for providers that expose it (LinkModel). *off* keeps tool calling working; see [LinkModel](#linkmodel) |
| **Rules (optional)** | Custom rules appended to the system prompt to guide how Juice behaves |

You can also **save** or **load a preset** (`.juiceconfig` file in your working directory) to persist your provider, key, model, working directory, reasoning setting, mode, and rules.

### 4. Start a session

Click **start session**. A chat interface opens. Type what you need and hit Enter. Juice streams its responses live — you'll see text appear and tool calls execute in real time, one after another, until the task is complete.

---

## Modes

Modes change *how* Juice works without changing what it can do. Pick one from the mode selector in the chat prompt bar (also mirrored in the top bar). The choice is saved with presets and sessions, so a project can stay in the workflow that suits it.

| Mode | Behaviour | Best for |
|------|-----------|----------|
| **Default** | Balanced agent; patch-first and test-aware, but conversational when you just want an answer | Everyday edits and questions |
| **Build** | Patch all related edits in one call, then run the full pipeline: format, lint, type-check, build, tests | Shipping a feature end to end |
| **Debug** | Reproduce first, form a hypothesis, confirm it with the terminal, apply the smallest fix, add a regression test | Failing tests, crashes, regressions |
| **Refactor** | Establish a green baseline, change one thing at a time, run tests after every step | Restructuring without behavior change |
| **Test** | Read the implementation and existing suite, then write and run thorough tests | Raising coverage |
| **Review** | Audit `git diff` and surrounding code, report findings by file and line, fix only when asked | PR review and code audits |
| **Plan** | Explore and produce a numbered plan, then stop and wait for approval before editing | Large or risky changes |

All modes inherit the same operating principles:

- **Patch by default.** `apply_patch` is the primary editing tool; `edit_file` is for a one-line tweak and `write_file` for new files or full rewrites.
- **Lean on the terminal.** Inspect with `git status`, `git diff`, `ls`, and `tree`; verify with formatters, linters, builds, and tests.
- **Test after you implement.** Add or update tests for the behaviour you changed, run the relevant suite, and iterate until green.
- **Verify, don't assume.** Finish only after a command has shown the change working.

The default working loop:

1. **Orient** — `git status`, `list_dir`, `search_code` to understand the project and its tooling.
2. **Locate** — `search_code` for the relevant symbols and files.
3. **Read** — `read_file` the exact line ranges you will change.
4. **Patch** — `apply_patch` with every related edit in one atomic call.
5. **Verify** — `run_command` the formatter, linter, type-checker, build, and tests.
6. **Fix** — read the failures, patch again, and re-run until green.
7. **Report** — summarize what changed, the commands you ran, test results, and any caveats.

---

## Tools

Juice has 7 tools:

| Tool | What it does | Limits |
|------|--------------|--------|
| **read_file** | Read any file, or a line range (`start_line`/`end_line`). Output is numbered as `120 \| ...` | 500 lines / 50 KB per call |
| **write_file** | Create or overwrite a file | — |
| **edit_file** | Find-and-replace the first exact match in a file (returns a diff) | One occurrence per call |
| **list_dir** | Tree-style directory listing | — |
| **run_command** | Run any shell command in the working directory | 120 s timeout; stdout and stderr capped at 16 KB each |
| **search_code** | Regex search across files (local or SSH remote), reporting `path:line:content` | 200 matches |
| **apply_patch** | Atomic multi-file patch in one call. Either dialect works: Juice's own format (`*** Update File` + `@@` hunks, `*** Add File`, `*** Delete File`, `*** Move to`) or a standard unified diff | 256 KB per patch |

These seven tools, along with live streaming and an agentic loop, let Juice take high-level requests and autonomously work through them — reading, writing, searching, running, and iterating until the job is done.

---

## Patches

`apply_patch` is the recommended way to change code. It accepts two dialects and normalises both into the same operations.

### Juice format

```
*** Begin Patch
*** Update File: src/auth.py
@@
 def login(user):
-    return authenticate(user)
+    result = authenticate(user)
+    return result
*** Add File: tests/test_auth.py
+def test_login():
+    assert True
*** Delete File: old_auth.py
*** End Patch
```

- One section per file — `*** Update File:`, `*** Add File:` or `*** Delete File:` followed by a workspace-relative path.
- `*** Move to: <path>` after `*** Update File:` renames the file, with or without hunks.
- Every hunk starts with `@@`; every hunk line starts with ` ` (context), `-` (removed) or `+` (added).
- `@@ <text>` anchors the hunk: when the same context appears more than once, the anchor picks the right spot.

### Unified diff

A standard `--- a/file` / `+++ b/file` diff with `@@` hunks works too, including `git diff` output and renames.

### What the parser tolerates

| Situation | What happens |
|-----------|--------------|
| The `*** Begin Patch` / `*** End Patch` wrapper is missing | Accepted |
| The same file is patched in several sections | The hunks are merged and applied in order |
| The context differs in trailing whitespace or indentation | Applied when the match is unique — the result reports it |
| An added file contains `@property`, `- bullets` or blank lines | Kept verbatim |
| A hunk is truncated (fewer lines than its header declares) | Rejected with a clear message |
| A file cannot be written half way through | Everything is rolled back |

### Guarantees

- **Atomic** — the whole patch is validated before a single byte is written; a failure writes nothing and names the file, hunk and reason.
- **Sandboxed** — paths stay inside the workspace; absolute paths and `../` are refused, and binary files are rejected.
- **Previewable** — `dry_run: true` validates and returns the diff without touching the workspace.
- **Size-limited** — a single patch may be at most 256 KB.
- **Concurrency-safe** — a file that changed between validation and writing aborts the patch with no modifications.

The engine lives in `patching.py`, apart from the agent loop in `juice.py`, and has its own test suite:

```bash
python3 tests/test_apply_patch.py
```

---

## Providers

| Provider | Get a key | Endpoint | Models |
|----------|-----------|----------|--------|
| **OpenRouter** | [openrouter.ai/keys](https://openrouter.ai/keys) | `https://openrouter.ai/api/v1` | Any id from [openrouter.ai/models](https://openrouter.ai/models) |
| **Google AI Studio** | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `https://generativelanguage.googleapis.com/v1beta/openai` | Any Gemini id |
| **LinkModel** | [linkmodel.ai/en/dashboard](https://linkmodel.ai/en/dashboard) | `https://api.linkmodel.ai/v1` | Any chat id from [docs.linkmodel.ai/models](https://docs.linkmodel.ai/models) |

All three speak the OpenAI Chat Completions protocol, so the full agent loop — system prompt, tool schemas, tool calls, chained tool results — behaves identically on every provider.

### LinkModel

- **40 documented chat models** (GPT, Claude, Gemini, DeepSeek, Kimi, GLM, Grok) are offered as suggestions in the Model ID field; type to filter, or type any id.
- **Coding-capable models** are the ones to pick for agentic work — `gpt-5.6-sol`, `claude-opus-4-7` and `deepseek-v4-pro` are documented for coding agents.
- **Reasoning is a setting, not a surprise** — LinkModel's chat endpoint rejects function tools while a model is reasoning, so the **Reasoning** field defaults to *off* (`reasoning: {"effort": "none"}`). If a model still refuses, Juice reads the error, switches reasoning off for that request and says so in the chat.
- **Errors are readable** — LinkModel's `{code, msg, request_id}` responses are surfaced as a plain message ("invalid api key", "insufficient balance") instead of raw JSON.
- **Rate limits are handled** — HTTP 429 and 5xx responses are retried with exponential backoff, honouring the `Retry-After` header LinkModel forwards from the upstream provider.
- **Tool calls round-trip exactly** — tool ids, streamed argument fragments and any `thought_signature` a Gemini model returns are preserved on the follow-up request, and parallel tool calls are supported.
- **Verify a key** against the live API before wiring it into a project:

  ```bash
  export LINK_MODEL_API_KEY="<your key>"
  python3 tools/linkmodel_probe.py
  ```

  The probe replays the exact tool-calling round trip Juice performs, so a green run confirms the provider handles the full agent loop.
- **Regression tests** cover the provider contract (request shape, tool round trip, error handling) and need no API key:

  ```bash
  python3 tests/test_linkmodel.py
  ```

#### Reasoning and tool calling

LinkModel's `/v1/chat/completions` endpoint rejects function tools while a model is reasoning:

> Function tools are not supported with non-default reasoning settings on this endpoint. Please use the Responses API or set reasoning effort to none.

Juice always sends tools, so on this endpoint the only workable setting is *reasoning off*. That is what the **Reasoning** field does:

| Setting | Request | When it makes sense |
|---------|---------|---------------------|
| `off` (default) | `reasoning: {"effort": "none"}` | Tool calling — the agent loop works |
| `minimal` / `low` / `medium` / `high` | `reasoning: {"effort": "<level>"}` | Chat without tools; these models refuse tool calls while reasoning |
| `model default` | *(field omitted)* | Let the model decide — reasoning models will refuse tools again |

The setting only reaches models that expose reasoning on the chat endpoint (the GPT-5.x and GPT-6.x families); the setup screen says so when the picked model is not one of them. Two safety nets run underneath:

- **Automatic fallback** — if the provider refuses tools because reasoning is on, Juice retries the request once with reasoning off and tells you in the chat. Models released after this README was written work the same way.
- **Unknown field** — if an upstream refuses the `reasoning` field itself, Juice drops it and remembers the decision, so it costs one extra request rather than one per turn.

LinkModel also serves an OpenAI **Responses** endpoint (`/v1/responses`), where reasoning and tools can be used together. Juice speaks the Chat Completions protocol, so that path is not used today.

---

## Tips

- **Modes** — switch workflow from the prompt bar. **Build** is the most aggressive about patching and testing; **Plan** is the safest for large changes.
- **@filename** — Type `@` followed by a file path to reference a file. Autocomplete shows available files and directories in your project.
- **Paste a URL** — Paste a URL into the chat; Juice fetches the page and uses it as context.
- **Multiple providers** — Switch between OpenRouter, Google AI Studio and LinkModel from the setup screen. Google's `thought_signature` requirements and quota-limit retries are handled automatically.
- **Conversations auto-save** — after 3 seconds of inactivity or on page close. Reload any session from the sidebar; sessions persist between restarts and remember their provider, model, reasoning, and mode.
- **Presets** — save and load your setup config (provider, key, model, reasoning, mode, rules) per project in a `.juiceconfig` file.
- **Context limit warnings** — Juice detects when the model's context window fills up and warns you.
- **Diff previews** — `edit_file` results render as inline red/green diffs, so you can see exactly what changed.
- **Atomic patches** — `apply_patch` results render per-file with insertion/deletion counts and a unified diff; failures show the exact file, hunk, and reason, with nothing written.
- **SSH remote search** — `search_code` can search on a remote host via SSH, not just locally.
- **New session** — Click "new session" in the sidebar to start fresh.
- **Watch the terminal** — all tool calls and results are logged there too.
- **Enter** to send · **Shift+Enter** for a newline.

---

## How it works

Juice runs a local web server (`juice.py`) that presents a chat UI and communicates with an LLM — via **OpenRouter**, **Google AI Studio** or **LinkModel**. The LLM has access to seven tools — read, write, edit, list files, run shell commands, search code (locally or over SSH), and atomic multi-file `apply_patch` — which it uses to fulfil your requests.

Juice runs a full agentic loop: it chains tool calls, executes them on your machine, feeds results back to the model, and iterates until the job is done. The system prompt sets patch-first, terminal-heavy, test-verified behaviour in every mode; the selected mode narrows that behaviour for the task at hand.

Everything runs on your machine. Your code never leaves your computer.

### Project layout

| Path | Purpose |
|------|---------|
| `juice.py` | Flask server, agent loop, tools, providers, modes, sessions, presets |
| `patching.py` | Atomic patch parser, planner, and writer |
| `UI.html` | Single-file browser UI (setup, chat, streaming, diffs) |
| `tests/` | Patch-engine and provider regression tests |
| `tools/linkmodel_probe.py` | Live LinkModel credential and tool-round-trip probe |

### HTTP API

The browser UI is a client of the same endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Serve the UI with provider and mode catalogs baked in |
| `GET` | `/api/providers` | Provider catalog (labels, defaults, models, reasoning) |
| `GET` | `/api/modes` | Mode catalog (labels and hints) |
| `POST` | `/api/chat/stream` | Run one agent turn; streams SSE events |
| `POST` | `/api/sessions/list` · `save` · `load` · `delete` | Session persistence |
| `POST` | `/api/preset/save` · `load` | Per-project `.juiceconfig` |
| `POST` | `/api/validate-dir` | Validate a working directory |
| `POST` | `/api/list-files` | List files and directories for autocomplete |
| `POST` | `/api/fetch-url` | Fetch and extract a URL as context |

---

## Development

Run the test suites (neither needs an API key):

```bash
python3 tests/test_apply_patch.py   # patch engine
python3 tests/test_linkmodel.py     # provider contract and tool round trip
```

Juice is intentionally small and flat:

- **Add a mode** — append an entry to `MODES` in `juice.py`; the UI picks it up from `/api/modes`.
- **Add a provider** — append an entry to `PROVIDERS` in `juice.py`; the setup screen renders it from the catalog.
- **Add a tool** — add its schema to `TOOLS`, handle it in `execute_tool`, and describe it in `SYSTEM_PROMPT`.

---

## Dependencies

| Package | Why |
|---------|-----|
| `flask` | Web server for the UI and API |
| `requests` | Talks to LLM providers (OpenRouter, Google AI Studio, LinkModel) and fetches URLs |
| `beautifulsoup4` | Extracts text from URLs you paste |

---

## Status

Juice is **alpha** software and may contain bugs or unexpected behaviour. Community feedback and contributions are very welcome. Use at your own risk.

## License

GNU Affero General Public License v3.0 (AGPLv3).
