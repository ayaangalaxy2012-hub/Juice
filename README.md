# Juice


https://github.com/user-attachments/assets/2d0f4dad-a80d-421e-ae87-dbc90fd0433e


## AI coding agent — right in your browser.

---

Point Juice at any project folder, tell it what to do, and it reads, writes, edits, searches, and runs code for you. Think of it as having a senior developer sitting next to you — one who can take a high-level request and run with it.

---

## What you can ask it

- *"Build me a todo app with a clean UI"*
- *"Add error handling to my API routes"*
- *"Debug why my tests are failing"*
- *"Refactor this file to use async/await"*
- *"Explain how this codebase is structured"*
- *"Set up a React + Vite project from scratch"*
- *"Search my whole project for deprecated API calls"*

We have 6 tools:
1. **read_file** — read any file
2. **write_file** — create or overwrite a file
3. **edit_file** — find-and-replace in a file (shows diff)
4. **list_dir** — tree-style directory listing
5. **run_command** — run any shell command
6. **search_code** — regex search across files (local or SSH remote)

These six tools, along with live streaming and an agentic loop, let Juice take high-level requests and autonomously work through them — reading, writing, searching, running, and iterating until the job is done.

---

## Getting started

### You'll need

- **Python 3.13+** (tested up to 3.14)
- An **API key** — from [OpenRouter](https://openrouter.ai/keys) ($1 free credit) **or** [Google AI Studio](https://aistudio.google.com/apikey)

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
  Providers supported: OpenRouter, Google AI Studio
```

### 3. Open your browser

Go to **http://localhost:5000**. You'll see a setup screen with these fields:

| Field | What to put |
|-------|------------|
| **Provider** | `openrouter` or `google` — choose which LLM provider to use |
| **Working directory** | Path to your project folder (click *validate* to check it) |
| **API key** | Your OpenRouter key (from [openrouter.ai/keys](https://openrouter.ai/keys)) or Google AI Studio key (from [aistudio.google.com](https://aistudio.google.com/apikey)) |
| **Model ID** | e.g. `anthropic/claude-sonnet-4-5` (OpenRouter) or `gemini-2.0-flash` (Google AI Studio) — any model from your provider works |
| **Rules (optional)** | Custom rules appended to the system prompt to guide how Juice behaves |

You can also **save** or **load a preset** (`.juiceconfig` file in your working directory) to persist your provider, key, model, working directory, and rules.

### 4. Start a session

Click **start session**. A chat interface opens. Type what you need and hit Enter. Juice streams its responses live — you'll see text appear and tool calls execute in real time, one after another, until the task is complete.

---

## Tips

- **@filename** — Type `@` followed by a file path to reference a file. Autocomplete shows available files and directories in your project.
- **Paste a URL** — Paste a URL into the chat; Juice fetches the page and uses it as context.
- **Multiple providers** — Switch between OpenRouter and Google AI Studio from the setup screen. Google's `thought_signature` requirements and quota-limit retries are handled automatically.
- **Conversations auto-save** — after 3 seconds of inactivity or on page close. Reload any session from the sidebar; sessions persist between restarts.
- **Presets** — save and load your setup config (provider, key, model, rules) per project in a `.juiceconfig` file.
- **Context limit warnings** — Juice detects when the model's context window fills up and warns you.
- **Diff previews** — `edit_file` results render as inline red/green diffs, so you can see exactly what changed.
- **SSH remote search** — `search_code` can search on a remote host via SSH, not just locally.
- **New session** — Click "new session" in the sidebar to start fresh.
- **Watch the terminal** — all tool calls and results are logged there too.
- **Enter** to send · **Shift+Enter** for a newline.

---

## How it works (briefly)

Juice runs a local web server (`juice.py`) that presents a chat UI and communicates with an LLM — via **OpenRouter** or **Google AI Studio**. The LLM has access to six tools — read, write, edit, list files, run shell commands, and search code (locally or over SSH) — which it uses to fulfill your requests. Juice runs a full agentic loop: it chains tool calls, executes them on your machine, feeds results back to the model, and iterates until the job is done. Everything runs on your machine. Your code never leaves your computer.

---

## Dependencies

| Package | Why |
|---------|-----|
| `flask` | Web server for the UI and API |
| `requests` | Talks to LLM providers (OpenRouter, Google AI Studio) and fetches URLs |
| `beautifulsoup4` | Extracts text from URLs you paste |

---

## NOTE
This project is still in alpha and may contain bugs or unexpected behavior.

We highly encourage community feedback and contributions to improve the project.

Use at your own risk.
## License

GNU Affero General Public License v3.0 (AGPLv3)

