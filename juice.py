import os
import re
import fnmatch
import difflib
import stat
import subprocess
import json
import shlex
import requests
from flask import Flask, request, jsonify, Response as FlaskResponse
from datetime import datetime, timezone
import shutil
from bs4 import BeautifulSoup
from pathlib import Path
import logging
import time
from email.utils import parsedate_to_datetime

from patching import PatchError, execute_apply_patch

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

LINKMODEL_CHAT_MODELS = (

    "gpt-6.1-sol", "gpt-6-luna", "gpt-6-sol", "gpt-6-astra",
    "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra",
    "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-pro", "gpt-5.2", "gpt-5",

    "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview", "gemini-3-flash-preview", "gemini-2.5-flash",

    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-sonnet-5-5", "claude-sonnet-5",
    "claude-sonnet-4-6", "claude-haiku-5-5", "claude-fable-5-1", "claude-fable-5",

    "minimax-m2.7", "deepseek-v4-pro", "deepseek-v4-flash",
    "kimi-k3", "kimi-k2.6", "glm-5.3", "glm-5.2", "glm-5.1",
    "grok-4.6", "grok-4.5",
)

REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "default")

REASONING_UNSUPPORTED = set()

DEFAULT_PROVIDER = "openrouter"

PROVIDERS = {
    "openrouter": {
        "label": "OpenRouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "default_model": "anthropic/claude-sonnet-4-5",
        "key_label": "OpenRouter API key",
        "key_placeholder": "sk-or-v1-...",
        "model_placeholder": "anthropic/claude-sonnet-4-5",
        "models_url": "https://openrouter.ai/models",
        "link_hint": "Browse at",
        "models": [],
        "headers": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:5000",
            "X-Title": "Juice AI Coding Agent"
        }
    },
    "google": {
        "label": "Google AI Studio",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "default_model": "gemini-2.0-flash",
        "key_label": "Google AI Studio API key",
        "key_placeholder": "AIzaSy...",
        "model_placeholder": "gemini-2.0-flash",
        "models_url": "https://aistudio.google.com/apikey",
        "link_hint": "Get a key at",
        "models": [],
        "headers": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
    },
    "linkmodel": {
        "label": "LinkModel",
        "url": "https://api.linkmodel.ai/v1/chat/completions",

        "default_model": "",
        "key_label": "LinkModel API key",
        "key_placeholder": "Your LinkModel API key",
        "model_placeholder": "e.g. claude-sonnet-5",
        "models_url": "https://docs.linkmodel.ai/models",
        "link_hint": "Browse at",
        "models": list(LINKMODEL_CHAT_MODELS),

        "supports_reasoning": True,
        "default_reasoning_effort": "none",
        "reasoning_efforts": list(REASONING_EFFORTS),
        "reasoning_hint": "Off keeps tool calling working — LinkModel rejects function tools while a model is reasoning.",

        "reasoning_models": [
            "gpt-6.1-sol", "gpt-6-luna", "gpt-6-sol", "gpt-6-astra",
            "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra",
            "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-pro", "gpt-5.2", "gpt-5",
        ],
        "headers": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
    }
}

def provider_catalog():
    """JSON-serialisable view of PROVIDERS for the frontend.

    The `headers` callable is server-side only and is left out. `default_provider`
    is explicit rather than implied by dictionary order.
    """
    providers = {}
    for pid, config in PROVIDERS.items():
        providers[pid] = {
            "id": pid,
            "label": config["label"],
            "default_model": config["default_model"],
            "key_label": config["key_label"],
            "key_placeholder": config["key_placeholder"],
            "model_placeholder": config["model_placeholder"],
            "models_url": config["models_url"],
            "link_hint": config["link_hint"],
            "models": list(config["models"]),
            "supports_reasoning": config.get("supports_reasoning", False),
            "default_reasoning_effort": config.get("default_reasoning_effort", "default"),
            "reasoning_efforts": list(config.get("reasoning_efforts") or []),
            "reasoning_hint": config.get("reasoning_hint", ""),
            "reasoning_models": list(config.get("reasoning_models") or []),
        }
    return {"default_provider": DEFAULT_PROVIDER, "providers": providers}

def term_width():
    try: return shutil.get_terminal_size((80, 20)).columns
    except: return 80

def log_sep(title="", char="━"):
    w = term_width()
    if title:
        title = f" {title} "
        half = (w - len(title)) // 2
        print(f"\n{char * half}{title}{char * (w - len(title) - half)}")
    else:
        print(char * w)

def log_agent_msg(text):
    log_sep(" AGENT OUTPUT ", "─")
    print(f"  {text}")
    log_sep()

def log_tool_call(tool_name, args, result):
    log_sep(" TOOL CALL ", "─")
    print(f"  Tool   : {tool_name}")
    for k, v in args.items():
        v_str = str(v)
        if len(v_str) > 200:
            v_str = v_str[:200] + "..."
        print(f"     {k:<8}: {v_str}")
    print(f"  ── Result ──")
    result_str = str(result)
    if len(result_str) > 500:
        result_str = result_str[:500] + "..."
    for line in result_str.splitlines():
        print(f"  {line}")
    log_sep()

def log_context_limit():
    log_sep(" CONTEXT LIMIT REACHED ", "═")

TOOLS = [
    {"type":"function","function":{"name":"read_file","description":"Read a specific portion of a file. Use line ranges when possible instead of reading the entire file. Omit start_line/end_line to read the whole file (only if reasonably small). Output lines are numbered as '<line> | <content>'. Max 500 lines and 50 KB per call; truncated output ends with an explicit [OUTPUT TRUNCATED] notice.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Path to the file (relative to working directory)."},"start_line":{"type":"integer","minimum":1,"description":"First line to read (1-indexed, inclusive). Defaults to 1."},"end_line":{"type":"integer","minimum":1,"description":"Last line to read (1-indexed, inclusive). Defaults to end of file."}},"required":["path"]}}},
    {"type":"function","function":{"name":"write_file","description":"Write (or overwrite) a file at the given path with the provided content.","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}}},
    {"type":"function","function":{"name":"edit_file","description":"Replace the FIRST occurrence of old_str with new_str in the file. old_str must match EXACTLY.","parameters":{"type":"object","properties":{"path":{"type":"string"},"old_str":{"type":"string"},"new_str":{"type":"string"}},"required":["path","old_str","new_str"]}}},
    {"type":"function","function":{"name":"list_dir","description":"List files and directories in working dir or a subdirectory.","parameters":{"type":"object","properties":{"subpath":{"type":"string","default":""}},"required":[]}}},
    {"type":"function","function":{"name":"run_command","description":"Run any shell command in the working directory.","parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}},
    {"type":"function","function":{"name":"search_code","description":"Search for a regex pattern across files (reports path:line:content, up to 200 matches). Skips hidden dirs, node_modules, __pycache__, and binary files. Narrow with path (subdirectory) and file_pattern (glob like '*.py'). Can also search a remote host over SSH via host/remote_path.","parameters":{"type":"object","properties":{"pattern":{"type":"string","description":"The regex pattern to search for"},"path":{"type":"string","description":"Optional subdirectory to search in (relative to working dir). Defaults to the whole working directory."},"file_pattern":{"type":"string","description":"Optional file glob pattern to filter (e.g. '*.py', '*.js', '*.go')."},"host":{"type":"string","description":"Optional SSH host to search on remotely (e.g. 'user@server'). If omitted, searches locally."},"remote_path":{"type":"string","description":"Base path on the remote host when using SSH. Defaults to the working directory on remote."}},"required":["pattern"]}}},
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Apply an atomic multi-file patch in ONE call (prefer it over many "
                "edit_file/write_file calls). The patch uses Juice's own format:\n"
                "*** Begin Patch\n"
                "*** Update File: src/auth.py\n"
                "@@\n"
                " def login(user):\n"
                "-    return authenticate(user)\n"
                "+    result = authenticate(user)\n"
                "+    return result\n"
                "*** End Patch\n"
                "One section per file (*** Update File: / *** Add File: / *** Delete File: "
                "/ *** Move to:) followed by the workspace-relative path. Every hunk starts "
                "with @@ and every hunk line starts with ' ' (context), '-' (removed) or "
                "'+' (added). A standard unified diff (--- a/f / +++ b/f / @@ hunks) is "
                "accepted as well. The patch is all-or-nothing: if anything fails nothing "
                "is written and the error names the file and hunk. The system prompt has "
                "the full format."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "The full patch text. Do not wrap it in markdown code fences.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Validate the patch without writing anything.",
                        "default": False,
                    },
                },
                "required": ["patch"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are juice, an expert AI coding agent with full access to the user's file system and terminal.

You have 7 tools:
1. read_file(path, start_line?, end_line?) — read a file or a line range (e.g. read_file("app.py", 120, 170)). Prefer ranges for large files: search_code to locate, then read the surrounding lines. Always do this before editing.
2. write_file(path, content) — create or overwrite a file. Use it only for brand-new files or full rewrites.
3. edit_file(path, old_str, new_str) — replace the first exact string in a file. Use it only for a tiny one-spot tweak; prefer apply_patch otherwise.
4. list_dir(subpath="") — list files/dirs.
5. run_command(command) — run any shell command in the working directory.
6. search_code(pattern, path="", file_pattern="") — search a regex across files (path:line:content, skips hidden/binary/node_modules). Use it to locate code, then read_file the surrounding lines.
7. apply_patch(patch, dry_run=false) — apply an atomic multi-file patch in ONE call. This is your DEFAULT way to change code: prefer it over many edit_file/write_file calls, and use it for all related edits in one pass.
   Patch format (Juice format; a standard unified diff with '--- a/f / +++ b/f / @@' hunks is accepted too):
   *** Begin Patch
   *** Update File: path/to/file.py
   @@
    def login(user):
   -    return authenticate(user)
   +    result = authenticate(user)
   +    return result
   *** Add File: path/to/new.py
   +first line of new file
   *** Delete File: path/to/old.py
   *** End Patch
   Rejected (nothing written) when a path is not workspace-relative, when a hunk's context is missing or ambiguous, or when a file gets conflicting operations.
   Accepted — the *** Begin Patch / *** End Patch wrapper is optional; '*** Move to: <path>' after '*** Update File:' renames a file; '@@ <text>' anchors a hunk when the same context repeats; a file may be patched in several sections (the hunks are applied in order); in '*** Add File' sections lines are taken verbatim, so '@property', '- item' and blank lines survive; blank lines inside a hunk are context lines; a unique match is accepted even when trailing whitespace or indentation differs (the result reports when that happened).
   Give 2-4 context lines per hunk so the match is unique, and copy context exactly from a fresh read_file.

Operating principles:
- Be autonomous. Call tools without asking for approval. Chain tool calls until the task is fully done; only then write a final reply.
- Lean on the terminal. Use run_command for real work: inspect the repo (git status, git diff, ls, tree), check tool versions, install is usually unnecessary but confirming is fine, format, lint, type-check, build, run the test suite, and run the app. Never guess what a command will print — run it and read the output.
- Patch by default. Use apply_patch for essentially every code change. Put all related edits (multiple files, multiple hunks) into one atomic patch. Reach for edit_file only for a one-line tweak, and write_file only for a new file or a wholesale rewrite.
- Read before you write. Use search_code to locate code, read_file the exact ranges, then patch using context copied from that read.
- Test after you implement. Every non-trivial change must end with tests: add or update tests for the behavior you changed, run the relevant suite, and read the result. If tests fail, fix the cause and re-run until green. If the project has no tests, add a focused one using its own framework (pytest, jest, vitest, go test, cargo test, ...) and run it.
- Verify, don't assume. After a patch, run the formatter, linter, type-checker, build, and tests that cover the change. Never claim something works without a command that shows it working.
- Fix real failures. If a command fails, read the actual error, correct the cause, and re-run. Do not paper over failures or skip tests to make output look green.

Default working loop:
1. Orient — run_command("git status") plus list_dir/search_code to understand the project and its tooling.
2. Locate — search_code for the relevant symbols and files.
3. Read — read_file the exact ranges you will change.
4. Patch — apply_patch with every related edit in one atomic call.
5. Verify — run_command the formatter, linter, type-checker, build, and tests.
6. Fix — read the failures, patch again, and re-run until green.
7. Report — summarize what changed, the exact commands you ran, test results, and any caveats.

Rules:
- Keep changes small enough to verify; never leave the workspace broken between steps.
- For noisy commands, filter output (head/tail, focused flags) so results stay useful.
- The user may provide additional rules in the chat. Follow these rules if provided.
"""

# Investigation modes. Each entry appends a focused block to SYSTEM_PROMPT, so
# the base guarantees (patch by default, terminal-heavy verification, tests
# after changes) hold in every mode while the block narrows the behaviour.
MODES = {
    "default": {
        "label": "Default",
        "hint": "Balanced agent — build, fix, refactor, explain.",
        "prompt": "",
    },
    "build": {
        "label": "Build",
        "hint": "Ship working code: patch often, run the terminal, test everything.",
        "prompt": """## Mode: Build
You are shipping working code, not sketches.
- Orient first: git status, list_dir, and read the files you will touch.
- Make all related edits in one apply_patch call rather than many small ones.
- After patching, run the project's real pipeline: formatter, linter, type-checker, build, and tests.
- Add or update tests for every behavior you add or change, then run them until they pass.
- Finish only when the build and relevant tests pass. Report the exact commands and their results.""",
    },
    "debug": {
        "label": "Debug",
        "hint": "Reproduce with the terminal, fix the root cause, add a regression test.",
        "prompt": """## Mode: Debug
You are a debugging specialist.
- Reproduce first: run the failing command/test, or write a minimal failing test. Never fix by inspection alone.
- State a hypothesis, then use the terminal, logging, or a focused test to confirm or reject it.
- Make the smallest correct fix with apply_patch.
- Add a regression test that fails without the fix and passes with it.
- Re-run the relevant suite and keep iterating until green.""",
    },
    "refactor": {
        "label": "Refactor",
        "hint": "Small verified steps: keep tests green after every change.",
        "prompt": """## Mode: Refactor
You make safe, incremental refactors.
- Run the tests first to establish a green baseline.
- Apply one small transformation at a time with apply_patch (rename, extract, inline, move).
- Run the tests after every change. If anything goes red, fix or revert before continuing.
- Preserve behavior and public interfaces unless the user explicitly asked to change them.
- Finish with a full test run and a summary of the transformations.""",
    },
    "test": {
        "label": "Test",
        "hint": "Write and run thorough tests for existing code.",
        "prompt": """## Mode: Test
You write thorough, runnable tests.
- Read the implementation and the existing tests first so you match the stack and style.
- Cover happy paths, edge cases, error conditions, and boundaries.
- Use the project's own test framework and helpers; do not add dependencies unless required.
- Run the suite and iterate until the new tests pass.
- Report what is now covered and any gaps or bugs you found.""",
    },
    "review": {
        "label": "Review",
        "hint": "Audit the diff and report concrete issues; fix only when asked.",
        "prompt": """## Mode: Review
You review code carefully and report concrete findings.
- Read git diff and the surrounding code before judging a change.
- Report issues by file and line: correctness, security, performance, error handling, and missing tests.
- Use run_command to run tests and linters so findings are backed by evidence.
- Do not modify files unless the user asks you to fix something; then use apply_patch and re-run the tests.""",
    },
    "plan": {
        "label": "Plan",
        "hint": "Explore and produce a plan, then wait for approval before editing.",
        "prompt": """## Mode: Plan
You plan before acting.
- Explore with read_file/search_code/run_command, but do not modify files yet.
- Produce a numbered plan naming the files you will touch and the commands/tests you will run.
- Stop and wait for the user to approve the plan before implementing it.""",
    },
}

def mode_catalog():
    """JSON-serialisable view of MODES for the frontend."""
    return {
        "default_mode": "default",
        "modes": {
            key: {"id": key, "label": mode["label"], "hint": mode["hint"]}
            for key, mode in MODES.items()
        },
    }

READ_MAX_LINES = 500
READ_MAX_BYTES = 50 * 1024
READ_TRUNC_LINES = "[OUTPUT TRUNCATED: 500 line limit]"
READ_TRUNC_BYTES = "[OUTPUT TRUNCATED: 50 KB output limit]"

# Tests and builds routinely take longer than a chat turn, so the terminal gets
# a wider budget than a quick command. Output is capped so a noisy command
# cannot swallow the model's context.
RUN_TIMEOUT = 120
RUN_MAX_OUTPUT = 16 * 1024

def _parse_read_lineno(value, name, path):
    if isinstance(value, bool):
        return f"ERROR: {name} must be an integer (got {value!r}) for {path}"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return f"ERROR: {name} must be an integer (got {value!r}) for {path}"
    if n < 1:
        return f"ERROR: {name} must be >= 1 (got {n}) for {path}"
    return n

def execute_read_file(path_arg, args, working_dir):
    p = Path(working_dir) / path_arg
    if not p.exists():
        return f"ERROR: File not found: {path_arg}"
    if p.is_dir():
        return f"ERROR: Path is a directory, not a file: {path_arg}"
    text = p.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    total = len(all_lines)

    has_start = args.get("start_line") is not None
    has_end = args.get("end_line") is not None

    if not has_start and not has_end:
        if total == 0:
            return "(empty file)"
        if len(all_lines) <= READ_MAX_LINES and len(text.encode("utf-8")) <= READ_MAX_BYTES:
            return text
        out_lines = all_lines
        notice = None
        if len(out_lines) > READ_MAX_LINES:
            out_lines = out_lines[:READ_MAX_LINES]
            notice = READ_TRUNC_LINES
        out = "\n".join(out_lines)
        if notice is None and len(out.encode("utf-8")) > READ_MAX_BYTES:
            kept = []
            used = 0
            for ln in out_lines:
                cost = len((ln + "\n").encode("utf-8"))
                if used + cost > READ_MAX_BYTES and kept:
                    break
                kept.append(ln)
                used += cost
            out_lines = kept
            out = "\n".join(out_lines)
            notice = READ_TRUNC_BYTES
        if notice:
            out += f"\n{notice} ({total} lines total in {path_arg}; re-read with start_line/end_line to page through it)"
        return out

    start = 1
    end = total
    if has_start:
        start = _parse_read_lineno(args.get("start_line"), "start_line", path_arg)
        if isinstance(start, str):
            return start
    if has_end:
        end = _parse_read_lineno(args.get("end_line"), "end_line", path_arg)
        if isinstance(end, str):
            return end
    if total == 0:
        return "(empty file)"
    if start > total:
        return f"ERROR: start_line ({start}) is beyond end of file ({total} lines) for {path_arg}"
    if end > total:
        end = total
    if start > end:
        return f"ERROR: start_line ({start}) is after end_line ({end}) for {path_arg}"

    notice = None
    hi = end
    if hi - start + 1 > READ_MAX_LINES:
        hi = start + READ_MAX_LINES - 1
        notice = READ_TRUNC_LINES
    rendered = [f"{n} | {all_lines[n - 1]}" for n in range(start, hi + 1)]
    if notice is None:
        while rendered and len(("\n".join(rendered)).encode("utf-8")) > READ_MAX_BYTES:
            rendered.pop()
        if len(rendered) < hi - start + 1:
            if not rendered:
                rendered = [f"{start} | {all_lines[start - 1]}"]
            notice = READ_TRUNC_BYTES
            hi = start + len(rendered) - 1
    out = f"{path_arg} (lines {start}-{hi} of {total}):\n" + "\n".join(rendered)
    if notice:
        out += f"\n{notice}"
    return out

SEARCH_MAX_RESULTS = 200
SEARCH_MAX_LINE_CHARS = 500
SEARCH_MAX_FILE_BYTES = 10 * 1024 * 1024
SEARCH_SKIP_DIRS = {"node_modules", "__pycache__"}

def _local_search_code(pattern, base_dir, working_dir, file_pattern):
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"ERROR: Invalid regex pattern {pattern!r}: {e}"
    try:
        base_resolved = Path(base_dir).resolve()
        work_resolved = Path(working_dir).resolve()
    except Exception as e:
        return f"ERROR: Cannot resolve search path: {e}"
    if not base_resolved.exists():
        return f"ERROR: Path not found: {base_dir}"
    if not base_resolved.is_dir():
        return f"ERROR: Path is not a directory: {base_dir}"

    matches = []
    truncated = False
    for root, dirs, files in os.walk(base_resolved):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d not in SEARCH_SKIP_DIRS)
        for name in sorted(files):
            if file_pattern and not fnmatch.fnmatch(name, file_pattern):
                continue
            file_path = Path(root) / name
            try:
                if file_path.stat().st_size > SEARCH_MAX_FILE_BYTES:
                    continue
                with open(file_path, "rb") as f:
                    head = f.read(8192)
                    if b"\x00" in head:
                        continue
                    rest = f.read()
                text = (head + rest).decode("utf-8", errors="replace")
            except (OSError, PermissionError):
                continue
            try:
                rel = Path(file_path).resolve().relative_to(work_resolved).as_posix()
            except ValueError:
                rel = str(file_path)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    shown = line.strip()
                    if len(shown) > SEARCH_MAX_LINE_CHARS:
                        shown = shown[:SEARCH_MAX_LINE_CHARS] + "…[long line truncated]"
                    matches.append(f"{rel}:{lineno}:{shown}")
                    if len(matches) >= SEARCH_MAX_RESULTS:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    if not matches:
        return f"No matches found for pattern: {pattern}"
    out = [f"Found {len(matches)} match(es){' (showing first 200)' if truncated else ''}:", "───────"]
    out.extend(f"  {m}" for m in matches)
    return "\n".join(out)

def execute_tool(name, args, working_dir):
    try:
        if name == "read_file":
            return execute_read_file(args.get("path", ""), args, working_dir)

        elif name == "write_file":
            p = Path(working_dir) / args["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"OK: wrote {len(args['content'])} chars to {args['path']}"

        elif name == "edit_file":
            p = Path(working_dir) / args["path"]
            if not p.exists(): return f"ERROR: File not found: {args['path']}"
            text = p.read_text(encoding="utf-8", errors="replace")
            if args["old_str"] not in text:
                return f"ERROR: old_str not found in {args['path']}. Make sure it matches exactly."
            p.write_text(text.replace(args["old_str"], args["new_str"], 1), encoding="utf-8")
            old_lines = args["old_str"].rstrip("\n").split("\n")
            new_lines = args["new_str"].rstrip("\n").split("\n")
            diff_parts = []
            max_lines = max(len(old_lines), len(new_lines))
            for i in range(max_lines):
                old_line = old_lines[i] if i < len(old_lines) else ""
                new_line = new_lines[i] if i < len(new_lines) else ""
                if i < len(old_lines) and i < len(new_lines) and old_line == new_line:
                    diff_parts.append(f" {old_line}")
                elif i < len(old_lines):
                    diff_parts.append(f"-{old_line}")
                if i >= len(old_lines):
                    diff_parts.append(f"+{new_line}")
                elif i < len(new_lines) and old_line != new_line:
                    diff_parts.append(f"+{new_line}")
            return f"OK: edited {args['path']}\n─── diff ───\n" + "\n".join(diff_parts) + "\n───────────"

        elif name == "list_dir":
            subpath = args.get("subpath", "") or ""
            base = Path(working_dir) / subpath
            if not base.exists(): return f"ERROR: Path not found: {subpath}"
            lines = [str(base)]
            def walk(path, prefix=""):
                try: entries = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name))
                except PermissionError: return
                for i, entry in enumerate(entries):
                    conn = "└── " if i == len(entries)-1 else "├── "
                    lines.append(f"{prefix}{conn}{entry.name}{'/' if entry.is_dir() else ''}")
                    if entry.is_dir() and not entry.name.startswith('.'):
                        walk(entry, prefix + ("    " if i == len(entries)-1 else "│   "))
            walk(base)
            return "\n".join(lines)

        elif name == "run_command":
            r = subprocess.run(args["command"], shell=True, cwd=working_dir,
                               capture_output=True, text=True, timeout=RUN_TIMEOUT)
            def _cap(text):
                if len(text) > RUN_MAX_OUTPUT:
                    return text[:RUN_MAX_OUTPUT] + (
                        f"\n[OUTPUT TRUNCATED: showing first {RUN_MAX_OUTPUT} chars "
                        f"of {len(text)}]")
                return text
            out = []
            if r.stdout: out.append(f"STDOUT:\n{_cap(r.stdout)}")
            if r.stderr: out.append(f"STDERR:\n{_cap(r.stderr)}")
            out.append(f"EXIT CODE: {r.returncode}")
            return "\n".join(out) or "(no output)"

        elif name == "search_code":
            pattern = args.get("pattern", "")
            search_path = args.get("path", "") or ""
            file_pattern = args.get("file_pattern", "") or ""
            host = args.get("host", "").strip() if isinstance(args.get("host", ""), str) else ""
            remote_path = args.get("remote_path", "").strip() if isinstance(args.get("remote_path", ""), str) else ""
            if not isinstance(pattern, str) or not pattern:
                return "ERROR: Missing required 'pattern' (non-empty regex string)"

            if host:
                base_path = str(Path(working_dir) / search_path) if search_path else working_dir
                remote_dir = remote_path if remote_path else base_path
                ssh_cmd = "grep -rnI --no-messages"
                if file_pattern:
                    ssh_cmd += f" --include={shlex.quote(file_pattern)}"
                ssh_cmd += f" -e {shlex.quote(pattern)} -- {shlex.quote(remote_dir)} | head -200"
                full_cmd = f"ssh -o StrictHostKeyChecking=no {shlex.quote(host)} {shlex.quote(ssh_cmd)}"
                r = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=60)
                out_parts = []
                if r.stdout:
                    lines = r.stdout.rstrip("\n").split("\n")
                    out_parts.append(f"Found {len(lines)} match(es):\n───────")
                    for line in lines:
                        out_parts.append(f"  {line}")
                if r.stderr:
                    clean_err = r.stderr.strip()
                    if clean_err and "Permission denied" not in clean_err:
                        out_parts.append(f"  [stderr: {clean_err}]")
                if not r.stdout and not r.stderr:
                    out_parts.append(f"No matches found for pattern: {pattern}")
                if r.returncode not in (0, 1):
                    out_parts.append(f"  [exit code: {r.returncode}]")
                return "\n".join(out_parts)

            base_path = str(Path(working_dir) / search_path) if search_path else working_dir
            return _local_search_code(pattern, base_path, working_dir, file_pattern)

        elif name == "apply_patch":
            patch = args.get("patch", "")
            dry_run = args.get("dry_run", False)
            if isinstance(dry_run, str):
                dry_run = dry_run.strip().lower() in ("true", "1", "yes")
            if not isinstance(patch, str) or not patch.strip():
                return json.dumps({"success": False, "error": "Invalid patch",
                                   "reason": "Missing required 'patch' string", "applied": False}, indent=2)
            result = execute_apply_patch(patch, dry_run, working_dir)
            return json.dumps(result, indent=2, ensure_ascii=False)

        return f"ERROR: Unknown tool: {name}"
    except subprocess.TimeoutExpired:
        return f"ERROR: Command timed out after {RUN_TIMEOUT} seconds"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

def fetch_url(url):
    try:
        response = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script","style","nav","footer","header"]): tag.decompose()
        lines = [l for l in soup.get_text("\n", strip=True).splitlines() if l.strip()]
        return "\n".join(lines[:300])
    except Exception as e:
        return f"[Could not fetch {url}: {e}]"

def uses_google_thought_signatures(provider, model):
    """Whether a request needs Google's `thought_signature` fields.

    Google's OpenAI-compatible endpoint requires a signature on every tool
    call. OpenRouter proxies Gemini models and needs the same treatment, but
    other providers reject the extra fields, so they are only ever sent to
    Google-compatible endpoints.
    """
    if provider == "google":
        return True
    if provider == "openrouter":
        return "gemini" in model.lower()
    return False

def reasoning_field(provider, model, effort=None):
    """The `reasoning` object to send with a request, or None to omit it.

    Only providers that declare reasoning support accept the field, and only
    for the models that actually expose reasoning — other upstreams reject
    it. "default" means "send nothing and let the model decide".
    """
    config = PROVIDERS.get(provider) or {}
    if not config.get("supports_reasoning"):
        return None
    if (provider, model) in REASONING_UNSUPPORTED:
        return None

    value = effort if effort is not None else config.get("default_reasoning_effort")
    value = str(value or "default").strip().lower()
    if value == "default" or value not in REASONING_EFFORTS:
        return None
    if model not in (config.get("reasoning_models") or ()):
        return None
    return {"effort": value}

def response_text(response):
    """The body of a response as text, or "" when it cannot be read."""
    try:
        return response.text or ""
    except Exception:
        return ""

def is_reasoning_tools_conflict(response):
    """True when the provider refused tools because reasoning was enabled.

    LinkModel's wording names the fix: "Function tools are not supported with
    non-default reasoning settings on this endpoint. Please use the Responses
    API or set reasoning effort to none."
    """
    text = response_text(response).lower()
    return "non-default reasoning" in text or "reasoning effort to none" in text

def is_reasoning_field_rejected(response):
    """True when the provider does not accept a `reasoning` field at all."""
    text = response_text(response).lower()
    if "reasoning" not in text or is_reasoning_tools_conflict(response):
        return False
    return any(w in text for w in (
        "unrecognized", "unknown", "invalid", "unexpected", "not support",
        "unsupported", "not permitted", "does not accept",
    ))

def sanitize_messages_for_provider(messages, provider, model):
    if not uses_google_thought_signatures(provider, model):
        return messages

    sanitized = []
    for msg in messages:
        msg_copy = dict(msg)
        if msg_copy.get("role") == "assistant" and "tool_calls" in msg_copy:
            new_tcs = []
            for tc in msg_copy["tool_calls"]:
                tc_item = dict(tc)
                extra = tc_item.get("extra_content", {})
                existing_sig = None
                if isinstance(extra, dict):
                    existing_sig = extra.get("google", {}).get("thought_signature") or extra.get("thought_signature")
                if not existing_sig:
                    existing_sig = tc_item.get("thought_signature")

                sig = existing_sig or "skip_thought_signature_validator"
                tc_item["thought_signature"] = sig
                tc_item["extra_content"] = {"google": {"thought_signature": sig}}
                new_tcs.append(tc_item)
            msg_copy["tool_calls"] = new_tcs
        sanitized.append(msg_copy)
    return sanitized

def is_quota_exceeded(response):
    try:
        if not response.ok:
            return "You exceeded your current quota" in response.text
    except Exception:
        pass
    return False

RETRYABLE_STATUS = (429, 500, 502, 503, 504)
MAX_RETRY_DELAY = 120

def retry_delay_seconds(response, attempt):
    """Seconds to wait before retrying, honouring a `Retry-After` header.

    `Retry-After` may be delta-seconds or an HTTP-date. LinkModel forwards the
    upstream value, so it is respected whenever the provider sends one.
    """
    header = ""
    try:
        header = (response.headers.get("Retry-After") or "").strip()
    except Exception:
        header = ""

    if header:
        try:
            return max(0.0, float(int(header)))
        except ValueError:
            pass
        try:
            target = parsedate_to_datetime(header)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            delta = (target - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, min(delta, MAX_RETRY_DELAY))
        except Exception:
            pass

    return min(2 ** attempt, 30)

def describe_api_error(response):
    """Render a provider error response as a single readable line.

    Providers report failures with a structured body — LinkModel uses
    {"code": 401, "msg": "invalid api key", "request_id": "..."} — so the raw
    JSON is turned into a message a user can act on.
    """
    try:
        body = response.json()
    except Exception:
        body = None

    if isinstance(body, dict):
        message = body.get("msg") or body.get("message") or body.get("detail")
        error = body.get("error")
        if not message and isinstance(error, dict):
            message = error.get("message") or error.get("msg")
        elif not message and isinstance(error, str):
            message = error
        if message:
            details = []
            if body.get("code"):
                details.append("code %s" % body["code"])
            if body.get("request_id"):
                details.append("request_id %s" % body["request_id"])
            return "%s%s" % (message, " (%s)" % ", ".join(details) if details else "")

    return (response.text or "").strip()[:400] or "HTTP %s" % response.status_code

def close_response(response):
    """Release a response that is not going to be read."""
    try:
        response.close()
    except Exception:
        pass

def notify(callback, message):
    """Hand a human-readable note to the caller, if it wants one."""
    if callback:
        try:
            callback(message)
        except Exception:
            pass

def call_llm_stream(messages, api_key, model, provider="openrouter", max_retries=5, reasoning_effort=None, on_notice=None):
    config = PROVIDERS.get(provider)
    if not config:
        raise ValueError("Unknown provider: %s" % provider)
    base_payload = {
        "model": model,
        "messages": sanitize_messages_for_provider(messages, provider, model),
        "tools": TOOLS,
        "tool_choice": "auto",
        "stream": True
    }

    reasoning = reasoning_field(provider, model, reasoning_effort)

    retries = 0
    downgraded_reasoning = False
    dropped_reasoning = False
    while True:
        payload = dict(base_payload)
        if reasoning:
            payload["reasoning"] = dict(reasoning)

        response = requests.post(
            config["url"],
            headers=config["headers"](api_key),
            json=payload,
            timeout=120,
            stream=True
        )

        if is_quota_exceeded(response):
            retries += 1
            print(f"\n[Google Quota Exceeded] 'You exceeded your current quota' detected. Waiting 15s before retry ({retries}/{max_retries})...")
            if retries >= max_retries:
                response.raise_for_status()
            time.sleep(15)
            continue

        if response.status_code == 400 and not downgraded_reasoning and is_reasoning_tools_conflict(response):
            downgraded_reasoning = True
            reasoning = {"effort": "none"}
            close_response(response)
            notify(on_notice, "Reasoning was switched off for this request — %s rejects tool calls while a model is reasoning." % config["label"])
            continue

        if response.status_code == 400 and not dropped_reasoning and reasoning and is_reasoning_field_rejected(response):
            dropped_reasoning = True
            reasoning = None
            REASONING_UNSUPPORTED.add((provider, model))
            close_response(response)
            notify(on_notice, "%s rejected the reasoning setting for this model — continuing without it." % config["label"])
            continue

        if response.status_code in RETRYABLE_STATUS and retries < max_retries:
            retries += 1
            delay = retry_delay_seconds(response, retries)
            print(f"\n[{provider.upper()} {response.status_code}] {describe_api_error(response)} — retrying in {delay:.0f}s ({retries}/{max_retries})...")
            close_response(response)
            time.sleep(delay)
            continue

        response.raise_for_status()
        return response

def sse_stream(api_key, model, working_directory, messages, rules_extra="", provider="openrouter", reasoning_effort=None, mode_prompt=""):
    system_content = SYSTEM_PROMPT
    if mode_prompt:
        system_content += f"\n\n{mode_prompt}"
    if rules_extra:
        system_content += f"\n\n## User-defined rules\n{rules_extra}\n"
    api_msgs = [{"role":"system","content":system_content}] + messages
    context_limit = False

    while True:
        notices = []
        try:
            stream_resp = call_llm_stream(
                api_msgs, api_key, model, provider,
                reasoning_effort=reasoning_effort,
                on_notice=notices.append,
            )
        except Exception as e:
            err_msg = str(e)
            response = getattr(e, "response", None)
            if response is not None:
                try: err_msg = describe_api_error(response)
                except Exception: pass
            yield f"event: error\ndata: {json.dumps({'error': err_msg})}\n\n"
            return

        for message in notices:
            yield f"event: notice\ndata: {json.dumps({'message': message})}\n\n"

        assistant_content = ""
        tool_calls = {}
        last_thought_sig = None

        for line in stream_resp.iter_lines(decode_unicode=True):
            if not line or line.startswith(":") or line == "data: [DONE]":
                continue
            if line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                choices = data.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})

                extra = delta.get("extra_content", {})
                if isinstance(extra, dict):
                    sig = extra.get("google", {}).get("thought_signature") or extra.get("thought_signature")
                    if sig: last_thought_sig = sig
                if delta.get("thought_signature"):
                    last_thought_sig = delta.get("thought_signature")

                content = delta.get("content", "")
                if content:
                    assistant_content += content
                    yield f"event: text_delta\ndata: {json.dumps({'text': content})}\n\n"

                tc_delta = delta.get("tool_calls", [])
                if tc_delta:
                    for tc in tc_delta:
                        idx = tc.get("index", 0)
                        if idx not in tool_calls:
                            tool_calls[idx] = {
                                "id": "",
                                "function": {"name": "", "arguments": ""},
                                "thought_signature": None
                            }
                            yield f"event: tool_start\ndata: {json.dumps({'index': idx})}\n\n"
                        if tc.get("id"):

                            if not tool_calls[idx]["id"]:
                                tool_calls[idx]["id"] = tc["id"]
                        if tc.get("function"):
                            fn = tc["function"]
                            if fn.get("name"):

                                current = tool_calls[idx]["function"]["name"]
                                if not current:
                                    tool_calls[idx]["function"]["name"] = fn["name"]
                                elif not fn["name"].startswith(current):
                                    tool_calls[idx]["function"]["name"] = current + fn["name"]
                            if fn.get("arguments"):
                                tool_calls[idx]["function"]["arguments"] += fn["arguments"]

                        tc_extra = tc.get("extra_content", {})
                        if isinstance(tc_extra, dict):
                            sig = tc_extra.get("google", {}).get("thought_signature") or tc_extra.get("thought_signature")
                            if sig: tool_calls[idx]["thought_signature"] = sig
                        if tc.get("thought_signature"):
                            tool_calls[idx]["thought_signature"] = tc.get("thought_signature")

                finish_reason = choices[0].get("finish_reason")
                if finish_reason == "length":
                    context_limit = True

        if not tool_calls:
            yield f"event: done\ndata: {json.dumps({'context_limit': context_limit, 'content': assistant_content})}\n\n"
            return

        for idx, tc in tool_calls.items():
            try: args = json.loads(tc["function"]["arguments"])
            except: args = {}
            if not tc["id"]:
                tc["id"] = f"call_{idx}_{int(datetime.now().timestamp())}"
            yield f"event: tool_info\ndata: {json.dumps({'index': idx, 'tool': tc['function']['name'], 'args': args})}\n\n"

        if assistant_content:
            log_agent_msg(assistant_content)

        formatted_tool_calls = []
        needs_google_signature = uses_google_thought_signatures(provider, model)
        for idx, tc in sorted(tool_calls.items()):
            call_dict = {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"]
                }
            }

            signature = tc.get("thought_signature") or last_thought_sig
            if signature:
                call_dict["thought_signature"] = signature
                call_dict["extra_content"] = {"google": {"thought_signature": signature}}
            elif needs_google_signature:
                call_dict["thought_signature"] = "skip_thought_signature_validator"
                call_dict["extra_content"] = {"google": {"thought_signature": "skip_thought_signature_validator"}}
            formatted_tool_calls.append(call_dict)

        assistant_msg = {
            "role": "assistant",
            "content": assistant_content if assistant_content else None,
            "tool_calls": formatted_tool_calls
        }
        api_msgs.append(assistant_msg)

        tool_results = []
        for idx in sorted(tool_calls.keys()):
            fn_name = tool_calls[idx]["function"]["name"]
            try: args = json.loads(tool_calls[idx]["function"]["arguments"])
            except: args = {}
            result = execute_tool(fn_name, args, working_directory)
            log_tool_call(fn_name, args, result)
            tool_results.append({"role": "tool", "tool_call_id": tool_calls[idx]["id"], "content": result})
            yield f"event: tool_result\ndata: {json.dumps({'index': idx, 'tool': fn_name, 'result': result})}\n\n"

        api_msgs.extend(tool_results)
        if context_limit:
            yield f"event: done\ndata: {json.dumps({'context_limit': True, 'content': assistant_content})}\n\n"
            return

        tool_calls = {}
        assistant_content = ""
        context_limit = False

HISTORY_DIRNAME = ".juice"
PRESET_FILENAME = ".juiceconfig"

def history_path(working_dir, session_id=None):
    d = Path(working_dir) / HISTORY_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    if session_id: return d / f"{session_id}.json"
    return d

def list_sessions(working_dir):
    d = history_path(working_dir)
    sessions = []
    if d.exists():
        for f in sorted(d.iterdir()):
            if f.suffix == ".json" and f.stem != "_current":
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    msg_count = len(data.get("messages", []))
                    summary = ""
                    for m in data.get("messages", []):
                        if m.get("role") == "user":
                            t = str(m.get("content", ""))
                            summary = t[:60] + "..." if len(t) > 60 else t
                            break
                    sessions.append({
                        "id": f.stem,
                        "name": data.get("name", f.stem),
                        "messages": msg_count,
                        "summary": summary,
                        "model": data.get("model", ""),
                        "provider": data.get("provider", "openrouter"),
                        "updated": data.get("updated", "")
                    })
                except: pass
    return sessions

@app.route("/api/sessions/list", methods=["POST"])
def api_list_sessions():
    working_directory = request.json.get("working_dir", "").strip()
    return jsonify({"sessions": list_sessions(working_directory)})

@app.route("/api/sessions/save", methods=["POST"])
def api_save_session():
    data = request.json
    working_directory = data.get("working_dir", "").strip()
    sid  = data.get("session_id", "").strip() or datetime.now().strftime("%Y%m%d_%H%M%S")
    p = history_path(working_directory, sid)
    p.write_text(json.dumps({
        "id": sid, "name": data.get("name", sid),
        "model": data.get("model", ""),
        "provider": data.get("provider", "openrouter"),
        "reasoning_effort": data.get("reasoning_effort", ""),
        "mode": data.get("mode", "default"),
        "messages": data.get("messages", []),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M")
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True, "session_id": sid})

@app.route("/api/sessions/load", methods=["POST"])
def api_load_session():
    working_directory = request.json.get("working_dir", "").strip()
    sid  = request.json.get("session_id", "").strip()
    p = history_path(working_directory, sid)
    if not p.exists(): return jsonify({"ok": False, "error": "Session not found"})
    try:
        return jsonify({"ok": True, "session": json.loads(p.read_text(encoding="utf-8"))})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/sessions/delete", methods=["POST"])
def api_delete_session():
    p = history_path(request.json.get("working_dir", "").strip(), request.json.get("session_id", "").strip())
    if p.exists(): p.unlink()
    return jsonify({"ok": True})

@app.route("/api/preset/save", methods=["POST"])
def api_preset_save():
    data = request.json
    working_directory = data.get("working_dir", "").strip()
    if not working_directory: return jsonify({"ok": False, "error": "Missing working_dir"})
    config = {
        "provider": data.get("provider", "openrouter"),
        "api_key": data.get("api_key", ""),
        "model": data.get("model", ""),
        "reasoning_effort": data.get("reasoning_effort", ""),
        "mode": data.get("mode", "default"),
        "rules_extra": data.get("rules_extra", ""),
        "working_dir": working_directory
    }
    (Path(working_directory) / PRESET_FILENAME).write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True})

@app.route("/api/preset/load", methods=["POST"])
def api_preset_load():
    working_directory = request.json.get("working_dir", "").strip()
    p = Path(working_directory) / PRESET_FILENAME
    if not p.exists(): return jsonify({"ok": False, "error": "No preset found"})
    try:
        return jsonify({"ok": True, "preset": json.loads(p.read_text(encoding="utf-8"))})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/validate-dir", methods=["POST"])
def validate_dir():
    path = request.json.get("path","").strip()
    p = Path(path).expanduser()
    if not p.exists() or not p.is_dir(): return jsonify({"ok":False,"error":f"Invalid directory: {path}"})
    return jsonify({"ok":True,"resolved":str(p.resolve())})

@app.route("/api/list-files", methods=["POST"])
def api_list_files():
    base = Path(request.json.get("path","").strip()).expanduser()
    if not base.exists() or not base.is_dir(): return jsonify({"files":[],"dirs":[]})
    files, dirs = [], []
    try:
        for entry in sorted(base.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith("."): continue
            (dirs if entry.is_dir() else files).append(entry.name)
    except PermissionError: pass
    return jsonify({"files":files,"dirs":dirs})

@app.route("/api/fetch-url", methods=["POST"])
def api_fetch_url():
    return jsonify({"content": fetch_url(request.json.get("url",""))})

@app.route("/logo.png")
def serve_logo():
    logo_path = Path(__file__).parent / "logo.png"
    if logo_path.exists():
        from flask import send_file
        return send_file(str(logo_path), mimetype="image/png")
    return "", 404

@app.route("/")
def index():
    """Serve the setup/chat UI with the provider catalog baked in.

    The frontend renders its provider dropdown, labels and model suggestions
    from this catalog, so there is a single place to add a provider.
    """
    html = (Path(__file__).parent / "UI.html").read_text(encoding="utf-8")
    catalog = json.dumps(provider_catalog(), indent=2).replace("</", "<\\/")
    modes = json.dumps(mode_catalog(), indent=2).replace("</", "<\\/")
    return (html
            .replace("/*__JUICE_PROVIDERS__*/null", catalog)
            .replace("/*__JUICE_MODES__*/null", modes))

@app.route("/api/providers", methods=["GET"])
def api_providers():
    return jsonify(provider_catalog())

@app.route("/api/modes", methods=["GET"])
def api_modes():
    return jsonify(mode_catalog())

@app.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    data = request.json
    api_key = data.get("api_key","").strip()
    model = data.get("model","").strip()
    working_directory = data.get("working_dir","").strip()
    messages = data.get("messages",[])
    rules = data.get("rules_extra","").strip()
    provider = data.get("provider","openrouter").strip()
    reasoning_effort = data.get("reasoning_effort")
    mode = (data.get("mode") or "default").strip()

    if not api_key or not model or not working_directory:
        return jsonify({"error":"Missing api_key, model, or working_dir"}), 400

    if provider not in PROVIDERS:
        return jsonify({"error":"Unknown provider: %s" % provider}), 400

    if mode not in MODES:
        mode = "default"
    mode_prompt = MODES[mode]["prompt"]

    if reasoning_effort is not None:
        reasoning_effort = str(reasoning_effort).strip().lower()
        if reasoning_effort and reasoning_effort not in REASONING_EFFORTS:
            return jsonify({"error":"Invalid reasoning_effort: %s (expected one of %s)" % (
                reasoning_effort, ", ".join(REASONING_EFFORTS))}), 400

    if messages and messages[-1].get("role") == "user":
        log_sep(" USER MESSAGE ", "─")
        print(f"  [{provider.upper()}] {messages[-1].get('content','')[:200]}")
        log_sep()

    return FlaskResponse(
        sse_stream(api_key, model, working_directory, messages, rules, provider, reasoning_effort, mode_prompt),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    )

if __name__ == "__main__":
    log_sep("JUICE — AI Coding Agent", "═")
    print("  Server running at http://localhost:5000")
    print("  Providers supported: %s" % ", ".join(config["label"] for config in PROVIDERS.values()))
    log_sep()
    app.run(debug=True, port=5000, use_reloader=False)
