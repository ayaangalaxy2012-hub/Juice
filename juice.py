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
from datetime import datetime
import shutil
from bs4 import BeautifulSoup
from pathlib import Path
import logging
import time

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

PROVIDERS = {
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "default_model": "anthropic/claude-sonnet-4-5",
        "headers": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:5000",
            "X-Title": "Juice AI Coding Agent"
        }
    },
    "google": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "default_model": "gemini-2.0-flash",
        "headers": lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
    }
}

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
    {"type":"function","function":{"name":"apply_patch","description":"Apply a precise, atomic multi-file patch in ONE call (prefer over many edit_file/write_file calls). EXAMPLE patch arg:\n*** Begin Patch\n*** Update File: src/auth.py\n@@\n def login(user):\n-    return authenticate(user)\n+    result = authenticate(user)\n+    return result\n*** Add File: tests/test_auth.py\n+def test_login():\n+    assert True\n*** Delete File: old_auth.py\n*** End Patch\nRULES: wrap in *** Begin Patch / *** End Patch; one section per file (*** Update File: / *** Add File: / *** Delete File: + workspace-relative path); every hunk starts with @@; hunk lines need a marker prefix: ' ' unchanged context, '-' removed, '+' added (blank context line = a lone ' '); 2-4 context lines per hunk so the target is unique; copy context EXACTLY (indentation included) from a fresh read_file; paths never absolute, never ../. A standard unified diff (--- a/f / +++ b/f / @@ hunks) is also accepted. All-or-nothing: any failure writes nothing and reports file+hunk+reason. dry_run=true validates without writing.","parameters":{"type":"object","properties":{"patch":{"type":"string","description":"Full patch text starting with *** Begin Patch and ending with *** End Patch (or a standard unified diff). Do NOT wrap it in markdown code fences."},"dry_run":{"type":"boolean","description":"If true, validate only and do not write files.","default":False}},"required":["patch"]}}},
]

SYSTEM_PROMPT = """You are juice, an expert AI coding agent with full access to the user's file system and terminal.

You have 7 tools:
1. read_file(path, start_line?, end_line?) — read a file or a line range (e.g. read_file("app.py", 120, 170)). Prefer ranges for large files: search_code to locate, then read the surrounding lines. Always do this before editing.
2. write_file(path, content) — create or overwrite a file.
3. edit_file(path, old_str, new_str) — replace exact string in a file. Read file first to get exact text.
4. list_dir(subpath="") — list files/dirs.
5. run_command(command) — run any shell command.
6. search_code(pattern, path="", file_pattern="") — search a regex across files (path:line:content, skips hidden/binary/node_modules). Use it to locate code, then read_file the surrounding lines.
7. apply_patch(patch, dry_run=false) — apply a precise atomic multi-file patch in ONE call. Prefer this over many edit_file/write_file calls when changing multiple files or multiple spots in one file.
   Patch format (exact text; a standard unified diff with --- a/f / +++ b/f / @@ hunks works too):
   *** Begin Patch
   *** Update File: path/to/file.py
   @@
    def login(user):
   -    return authenticate(user)
   +    result = authenticate(user)
   +    return result
   *** Add File: path/to/new.py
   +first line of new file
   +second line
   *** Delete File: path/to/old.py
   *** End Patch
   Hard rules — a patch violating any of these is REJECTED (nothing is written):
   - Wrap the whole patch in *** Begin Patch / *** End Patch (no markdown code fences around it).
   - One section per file: *** Update File: <relative path>, *** Add File: <relative path>, or *** Delete File: <relative path>. Paths are workspace-relative, never absolute, never ../.
   - Directives and @@ start at column 0 (no indentation).
   - Every hunk starts with a @@ line; every other hunk line starts with exactly one marker: ' ' (unchanged context), '-' (removed), '+' (added). A blank context line is a lone ' '.
   - Give 2-4 context lines per hunk so the match is UNIQUE; copy context EXACTLY from a fresh read_file (same indentation, no trailing-space changes).
   - Update hunks locate by context+removed lines: too little context = ambiguous = rejected; wrong whitespace = not found = rejected.

Rules:
- Call tools autonomously, no user approval needed.
- Chain tool calls until the task is fully done.
- Only write a final reply when all tool calls are complete.
- Always read files before editing them (use line ranges on large files; max 500 lines per read).
- Ideal loop: search_code → read_file(lines) → apply_patch → run_command → read_file(lines) → verify.
- Prefer apply_patch for multi-file or multi-hunk changes (atomic, all-or-nothing).
- If a command fails, diagnose and fix automatically.
- Write tests using run_command tool to test for errors or bugs.

Note: The user may provide additional rules in the chat. Follow these rules if provided.
"""

# ── apply_patch engine ──────────────────────────────────────────────
MAX_PATCH_BYTES = 256 * 1024

_HUNK_HDR_RE = re.compile(r"^@@\s*(?:-(\d+)(?:,(\d+))?\s*\+(\d+)(?:,(\d+))?\s*@@)?\s*(.*)?$")


class PatchError(Exception):
    def __init__(self, error, file=None, hunk=None, reason=None):
        super().__init__(error)
        self.error = error
        self.file = file
        self.hunk = hunk
        self.reason = reason


def _normalize_patch_rel(p):
    p = (p or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _resolve_patch_path(working_dir, rel_path):
    """Resolve a patch path safely inside the workspace. Returns (abs Path, normalized rel)."""
    rel = _normalize_patch_rel(rel_path)
    if not rel:
        raise PatchError("Invalid path", file=rel_path, reason="Empty file path")
    if "\x00" in rel:
        raise PatchError("Invalid path", file=rel, reason="Null byte in path")
    # NB: os.path.isabs is OS-dependent (on Windows '/abs' is not "absolute"),
    # so also reject POSIX-style absolute paths explicitly.
    if rel.startswith("/") or os.path.isabs(rel) or os.path.isabs(rel_path.strip() or "") or re.match(r"^[a-zA-Z]:[\\/]", rel_path.strip() or "") or rel.startswith("//"):
        raise PatchError("Path escapes workspace", file=rel, reason="Absolute paths are not allowed; use workspace-relative paths")
    base = Path(working_dir).resolve()
    # Join using OS separator then resolve (strict=False so new files work)
    target = (base / Path(*rel.split("/"))).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise PatchError("Path escapes workspace", file=rel, reason=f"Path '{rel}' resolves outside the workspace")
    # Extra guard: reject any '..' that would escape even before resolution
    return target, rel


def _detect_newline(raw: bytes):
    if b"\r\n" in raw:
        style = "\r\n"
    elif b"\r" in raw and b"\n" not in raw:
        style = "\r"
    else:
        style = "\n"
    trailing = (raw.endswith(b"\n") or raw.endswith(b"\r")) if raw else True
    return style, trailing


def _is_binary_raw(raw: bytes):
    return b"\x00" in raw


PATCH_TEMPLATE_HINT = (
    "Expected either the Juice patch format — '*** Begin Patch', then per file "
    "'*** Update File: <path>' with '@@' hunks (' ' context, '-' remove, '+' add), "
    "'*** Add File: <path>' with '+' lines, '*** Delete File: <path>', ending with "
    "'*** End Patch' — or a standard unified diff ('--- a/<path>' / '+++ b/<path>' / '@@' hunks)."
)


def _strip_code_fences(text):
    """Drop a surrounding markdown code fence (``` or ```diff) if the model wrapped the patch."""
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return lines


def _is_patch_marker(ln, word):
    # Column-anchored (must start with '*', so marker-prefixed hunk content can
    # never false-positive) but tolerant of trailing '***' and casing, e.g.
    # '*** Begin Patch ***'.
    if not ln.startswith("*"):
        return False
    return ln.strip().strip("*").strip().lower() == word


def _looks_like_unified_diff(lines):
    has_old = has_new = has_hunk = False
    for ln in lines:
        if ln.startswith("---"):
            has_old = True
        elif ln.startswith("+++"):
            has_new = True
        elif ln.startswith("@@"):
            has_hunk = True
    return has_old and has_new and has_hunk


def _strip_diff_path(p):
    p = (p or "").strip()
    if len(p) >= 2 and p[0] in "\"'" and p[-1] == p[0]:
        p = p[1:-1]
    p = p.split("\t")[0].strip()  # drop trailing timestamps
    if p.startswith(("a/", "b/")) and len(p) > 2:
        p = p[2:]
    return p


def _validate_ops(ops):
    """Shared validation for both patch formats. Raises PatchError."""
    seen = set()
    for op in ops:
        rel = _normalize_patch_rel(op["path"])
        op["path"] = rel
        if rel in seen:
            raise PatchError("Duplicate file operation", file=rel, reason=f"File '{rel}' appears more than once; combine into one section")
        seen.add(rel)
        if op["op"] in ("update", "add") and not op["hunks"]:
            raise PatchError("Missing hunks", file=rel, reason=f"*** {'Update' if op['op'] == 'update' else 'Add'} File *** has no hunks/content")
        for idx, h in enumerate(op["hunks"], start=1):
            if not h["lines"]:
                raise PatchError("Empty hunk", file=rel, hunk=idx, reason="Hunk contains no content lines")
            if op["op"] == "add":
                bad = [k for k, _ in h["lines"] if k == "-"]
                if bad:
                    raise PatchError("Invalid Add File hunk", file=rel, hunk=idx,
                                     reason="*** Add File *** hunks may only contain '+' lines (found '-' line)")
    return ops


def _parse_unified_diff(lines):
    """Parse a standard unified / git diff into the same ops structure."""
    ops = []
    current = None
    current_hunk = None
    pending_old = None
    have_old = False

    def ensure_hunk():
        nonlocal current_hunk
        if current is None:
            raise PatchError("Invalid patch", reason="'@@' hunk outside of a file diff (missing '---'/'+++' headers?)")
        if current_hunk is None:
            current_hunk = {"header": None, "hint": None, "lines": []}
            current["hunks"].append(current_hunk)
        return current_hunk

    for line_no, raw in enumerate(lines, start=1):
        if (raw.startswith(("diff --git ", "index ", "new file mode", "deleted file mode",
                            "similarity index ", "dissimilarity index "))):
            continue
        if raw.startswith(("rename from", "rename to")):
            raise PatchError("Invalid patch", reason="Renames are not supported; express as a Delete + an Add instead")
        if raw.startswith("Binary files ") or (raw.startswith("Binary ") and "differ" in raw):
            raise PatchError("Invalid patch", reason="Binary files are not supported")
        if raw.startswith("---"):
            pending_old = _strip_diff_path(raw[3:])
            have_old = True
            continue
        if raw.startswith("+++"):
            if not have_old:
                raise PatchError("Invalid patch", reason=f"Found '+++ ...' without a preceding '--- ...' line (line {line_no})")
            new = _strip_diff_path(raw[3:])
            if pending_old in ("/dev/null", "dev/null"):
                op = {"op": "add", "path": new, "hunks": []}
            elif new in ("/dev/null", "dev/null"):
                op = {"op": "delete", "path": pending_old, "hunks": []}
            else:
                if pending_old != new:
                    raise PatchError("Invalid patch", file=pending_old,
                                     reason=f"Rename ({pending_old} -> {new}) is not supported; use Delete + Add instead")
                op = {"op": "update", "path": new, "hunks": []}
            ops.append(op)
            current = op
            current_hunk = None
            have_old = False
            continue
        if raw.startswith("@@"):
            if current is None:
                raise PatchError("Invalid patch", reason=f"'@@' hunk outside of a file diff (line {line_no})")
            if current["op"] == "delete":
                current_hunk = None  # deleted-file hunks carry no needed info; path suffices
                continue
            m = _HUNK_HDR_RE.match(raw.strip())
            if not m:
                raise PatchError("Invalid hunk header", file=_normalize_patch_rel(current["path"]),
                                 hunk=len(current["hunks"]) + 1, reason=f"Malformed '@@' header: '{raw}'")
            hint = int(m.group(1)) - 1 if m.group(1) else None
            if hint is not None and hint < 0:
                hint = 0
            current_hunk = {"header": raw, "hint": hint, "lines": []}
            current["hunks"].append(current_hunk)
            continue
        # Hunk content (or ignorable filler).
        if current is None:
            if not raw.strip():
                continue
            raise PatchError("Invalid patch", reason=f"Content outside of a file diff (line {line_no}): '{raw[:60]}'")
        if current["op"] == "delete":
            if not raw.strip() or raw[0] in (" ", "-", "\\"):
                continue  # nothing to verify for deletions; the path identifies the file
            if raw[0] == "+":
                raise PatchError("Invalid patch", file=_normalize_patch_rel(current["path"]),
                                 reason="Additions ('+' lines) make no sense inside a file deletion")
            raise PatchError("Invalid hunk line", file=_normalize_patch_rel(current["path"]),
                             reason=f"Line must start with ' ', '+' or '-': '{raw[:80]}'")
        if raw == "":
            ensure_hunk()["lines"].append((" ", ""))  # blank context line in unified diffs
            continue
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        h = ensure_hunk()
        if raw[0] in (" ", "+", "-"):
            h["lines"].append((raw[0], raw[1:]))
        else:
            raise PatchError("Invalid hunk line", file=_normalize_patch_rel(current["path"]),
                             hunk=len(current["hunks"]),
                             reason=f"Line must start with ' ', '+' or '-': '{raw[:80]}'")
    if not ops:
        raise PatchError("Invalid patch", reason="No file diffs found. " + PATCH_TEMPLATE_HINT)
    return _validate_ops(ops)


def _parse_patch(patch_text):
    """Parse full patch text into ops. Raises PatchError on any syntax problem."""
    if patch_text is None or not isinstance(patch_text, str):
        raise PatchError("Invalid patch", reason="Patch must be a string")
    if len(patch_text.encode("utf-8")) > MAX_PATCH_BYTES:
        raise PatchError(f"Patch too large (max {MAX_PATCH_BYTES} bytes)", reason="Configurable limit exceeded")
    lines = _strip_code_fences(patch_text)
    # locate begin/end. Directives live at column 0 (hunk content lines always
    # carry a ' '/'+'/'-' marker prefix, so a marker-prefixed lookalike can
    # never false-positive here).
    begin = end = None
    for i, ln in enumerate(lines):
        if _is_patch_marker(ln, "begin patch"):
            begin = i
            break
    if begin is None:
        if _looks_like_unified_diff(lines):
            return _parse_unified_diff(lines)
        raise PatchError("Invalid patch", reason="Missing '*** Begin Patch'. " + PATCH_TEMPLATE_HINT)
    for i in range(begin + 1, len(lines)):
        if _is_patch_marker(lines[i], "end patch"):
            end = i
            break
    if end is None:
        raise PatchError("Invalid patch", reason="Missing '*** End Patch' (patch was cut off? resend the COMPLETE patch). " + PATCH_TEMPLATE_HINT)
    body = lines[begin + 1:end]
    ops = []
    current = None  # {op, path, hunks: [{header, hint, lines: [(kind, text)]}]}
    current_hunk = None

    def new_file_op(op, path, line_no):
        if not path:
            raise PatchError("Invalid directive", reason=f"Missing path in file directive (line {line_no})")
        return {"op": op, "path": path.strip(), "hunks": []}

    def ensure_hunk():
        nonlocal current_hunk
        if current is None:
            raise PatchError("Invalid patch", reason="Hunk found outside of a file directive")
        if current_hunk is None:
            current_hunk = {"header": None, "hint": None, "lines": []}
            current["hunks"].append(current_hunk)
        return current_hunk

    for offset, raw in enumerate(body):
        line_no = begin + 2 + offset  # 1-indexed approx
        s = raw.strip()
        # Structural markers must sit at column 0. Hunk content lines always
        # start with a ' '/'+'/'-' marker, so e.g. a context line ' @@x' or
        # ' *** bold' can never be mistaken for a header/directive.
        if raw.startswith("*** Update File:"):
            path = raw.split("*** Update File:", 1)[1]
            current = new_file_op("update", path, line_no)
            ops.append(current)
            current_hunk = None
        elif raw.startswith("*** Add File:"):
            path = raw.split("*** Add File:", 1)[1]
            current = new_file_op("add", path, line_no)
            ops.append(current)
            current_hunk = None
        elif raw.startswith("*** Delete File:"):
            path = raw.split("*** Delete File:", 1)[1]
            current = new_file_op("delete", path, line_no)
            ops.append(current)
            current_hunk = None
        elif raw.startswith("***"):
            raise PatchError("Invalid patch", reason=f"Unknown directive '{raw.strip()}' (line {line_no})")
        elif raw.startswith("@@"):
            if current is None:
                raise PatchError("Invalid patch", reason=f"'@@' outside of a file directive (line {line_no})")
            if current["op"] == "delete":
                raise PatchError("Invalid patch", file=_normalize_patch_rel(current["path"]),
                                 reason="*** Delete File *** must not contain hunks")
            m = _HUNK_HDR_RE.match(s)
            if not m:
                raise PatchError("Invalid hunk header", file=_normalize_patch_rel(current["path"]),
                                 hunk=len(current["hunks"]) + 1, reason=f"Malformed '@@' header: '{raw}'")
            hint = int(m.group(1)) - 1 if m.group(1) else None  # old start, 0-indexed
            if hint is not None and hint < 0:
                hint = 0
            current_hunk = {"header": raw, "hint": hint, "lines": []}
            current["hunks"].append(current_hunk)
        elif raw == "" :
            # Blank separator line — never file content. Use explicit ' ', '+', '-' for blank lines.
            continue
        elif raw == "\\ No newline at end of file":
            continue
        else:
            if current is None:
                if s == "":
                    continue
                raise PatchError("Invalid patch", reason=f"Content outside of a file directive (line {line_no}): '{raw[:60]}'")
            if current["op"] == "delete":
                raise PatchError("Invalid patch", file=_normalize_patch_rel(current["path"]),
                                 reason="*** Delete File *** must not contain content lines")
            h = ensure_hunk()
            if raw[0] in (" ", "+", "-"):
                h["lines"].append((raw[0], raw[1:]))
            elif current["op"] == "add" and not raw.startswith(("@", "*")):
                # Tolerated: bare lines in *** Add File *** mean additions (no
                # matching semantics there, so this is unambiguous).
                h["lines"].append(("+", raw))
            else:
                raise PatchError("Invalid hunk line", file=_normalize_patch_rel(current["path"]),
                                 hunk=len(current["hunks"]),
                                 reason=f"Line must start with ' ', '+' or '-': '{raw[:80]}'")

    if not ops:
        raise PatchError("Invalid patch", reason="Patch contains no file operations")
    return _validate_ops(ops)


def _mismatch_hint(file_lines, old_seq):
    """Diagnose WHY context didn't match (whitespace-only differences are the
    classic LLM failure). Returns '' or a ' Hint: ...' suffix. Never fuzzy-matches."""
    interesting = [l for l in old_seq if l.strip()]
    if not interesting or not file_lines:
        return ""
    exact = set(file_lines)
    missing = [l for l in interesting if l not in exact]
    if not missing:
        return ""  # all lines exist individually; only their order/adjacency differs
    rstripped = {l.rstrip() for l in file_lines}
    if all(l in rstripped for l in missing):
        return " Hint: those lines exist in the file but differ by trailing whitespace — copy them exactly."
    stripped = {l.strip() for l in file_lines}
    if all(l.strip() in stripped for l in missing):
        return " Hint: matching lines exist but indentation differs — copy the exact leading whitespace from the file (read it first)."
    first = missing[0].strip()
    if len(first) >= 10:
        for n, fl in enumerate(file_lines, start=1):
            if first in fl and fl.strip() != first:
                return f" Hint: similar text at line {n} ('{fl.strip()[:80]}') — context must match exactly, extend it."
    return ""


def _find_hunk_index(file_lines, old_seq, hint):
    """Locate old_seq in file_lines. Prefers hint, else requires exactly one global match."""
    n, m = len(file_lines), len(old_seq)
    if m == 0:
        # Pure-addition hunk with zero context: only applicable via explicit offset
        if hint is not None and 0 <= hint <= n:
            return hint
        raise PatchError("Hunk failed to apply", reason="Hunk has no context/removal lines; add context lines or a '@@ -start,count +start,count @@' offset")
    # Try hinted position first (supports normal unified-diff offsets)
    if hint is not None and 0 <= hint <= n - m:
        if file_lines[hint:hint + m] == old_seq:
            return hint
    matches = []
    for i in range(n - m + 1):
        if file_lines[i:i + m] == old_seq:
            matches.append(i)
            if len(matches) > 1 and hint is None:
                break
    if not matches:
        # Build a short preview to help the model fix context
        preview = " | ".join(old_seq[:3])[:200]
        raise PatchError("Hunk failed to apply",
                         reason=f"Expected context was not found (looking for: '{preview}...'). Read the file first and copy context exactly."
                                + _mismatch_hint(file_lines, old_seq))
    if len(matches) > 1:
        raise PatchError("Hunk failed to apply",
                         reason=f"Ambiguous match: context occurs {len(matches)} times. Add more surrounding context lines to make it unique.")
    # Single global match — if a hint was given but points elsewhere, we still use the unique match
    # unless the hint was valid and matched (handled above). This keeps bare-@@ patches working
    # while honoring explicit offsets when they match.
    return matches[0]


def _apply_hunks_to_lines(orig_lines, hunks, rel):
    """Apply hunks sequentially to orig_lines. Returns (new_lines, insertions, deletions)."""
    cur = list(orig_lines)
    ins = dels = 0
    for idx, h in enumerate(hunks, start=1):
        old_seq = [t for k, t in h["lines"] if k in (" ", "-")]
        new_seq = [t for k, t in h["lines"] if k in (" ", "+")]
        h_ins = sum(1 for k, _ in h["lines"] if k == "+")
        h_del = sum(1 for k, _ in h["lines"] if k == "-")
        try:
            at = _find_hunk_index(cur, old_seq, h.get("hint"))
        except PatchError as e:
            e.file = rel
            e.hunk = idx
            e.error = "Hunk failed to apply"
            raise
        cur[at:at + len(old_seq)] = new_seq
        ins += h_ins
        dels += h_del
    return cur, ins, dels


def execute_apply_patch(patch, dry_run, working_dir):
    """Two-phase atomic apply. Returns success/failure dict (never partial)."""
    base = Path(working_dir)
    if not base.exists() or not base.is_dir():
        return {"success": False, "error": "Invalid working directory", "reason": f"{working_dir} is not a directory", "applied": False}
    try:
        ops = _parse_patch(patch)
    except PatchError as e:
        return {"success": False, "error": e.error, **({"file": e.file} if e.file else {}),
                **({"hunk": e.hunk} if e.hunk else {}), "reason": e.reason or e.error, "applied": False}

    # Phase 1 — snapshot + validate + compute in memory
    snapshots = {}   # rel -> {target, raw, text, lines, newline, trailing, mode, size, mtime_ns}
    planned = {}     # rel -> {target, op, new_lines, newline, trailing, old_lines}
    total_ins = total_del = 0
    diffs = []
    try:
        for op in ops:
            rel = op["path"]
            try:
                target, _ = _resolve_patch_path(working_dir, rel)
            except PatchError as e:
                e.file = rel
                raise
            if op["op"] == "add":
                if os.path.lexists(target):
                    raise PatchError("Add failed", file=rel, reason="File already exists")
                # Validate parent is a dir (or will be created inside workspace)
                new_lines = []
                for h in op["hunks"]:
                    for k, t in h["lines"]:
                        if k in ("+", " "):
                            new_lines.append(t)
                total_ins += len(new_lines)
                planned[rel] = {"target": target, "op": "add", "new_lines": new_lines,
                                "newline": "\n", "trailing": True if new_lines else False, "old_lines": []}
                diff = difflib.unified_diff([], new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="")
                diffs.append("\n".join(list(diff)))
            elif op["op"] == "delete":
                if not os.path.lexists(target) or not target.is_file():
                    raise PatchError("Delete failed", file=rel, reason="File not found")
                raw = target.read_bytes()
                if _is_binary_raw(raw):
                    raise PatchError("Delete failed", file=rel, reason="Binary files are not supported")
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise PatchError("Delete failed", file=rel, reason="File is not valid UTF-8 (binary?)")
                old_lines = text.splitlines()
                st = target.stat()
                snapshots[rel] = {"target": target, "raw": raw, "size": len(raw), "mtime_ns": st.st_mtime_ns}
                total_del += len(old_lines)
                planned[rel] = {"target": target, "op": "delete", "new_lines": None,
                                "old_lines": old_lines, "newline": "\n", "trailing": False}
                diff = difflib.unified_diff(old_lines, [], fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="")
                diffs.append("\n".join(list(diff)))
            else:  # update
                if not os.path.lexists(target) or not target.is_file():
                    raise PatchError("Update failed", file=rel, reason="File not found; use *** Add File *** for new files")
                raw = target.read_bytes()
                if _is_binary_raw(raw):
                    raise PatchError("Update failed", file=rel, reason="Binary files are not supported")
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise PatchError("Update failed", file=rel, reason="File is not valid UTF-8 (binary?)")
                newline, trailing = _detect_newline(raw)
                old_lines = text.splitlines()
                try:
                    mode = stat.S_IMODE(target.stat().st_mode)
                except Exception:
                    mode = None
                st = target.stat()
                snapshots[rel] = {"target": target, "raw": raw, "size": len(raw),
                                  "mtime_ns": st.st_mtime_ns, "mode": mode}
                new_lines, h_ins, h_del = _apply_hunks_to_lines(old_lines, op["hunks"], rel)
                total_ins += h_ins
                total_del += h_del
                planned[rel] = {"target": target, "op": "update", "new_lines": new_lines,
                                "old_lines": old_lines, "newline": newline, "trailing": trailing, "mode": mode}
                diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="")
                d = "\n".join(list(diff))
                if d:
                    diffs.append(d)
    except PatchError as e:
        return {"success": False, "error": e.error, **({"file": e.file} if e.file else {}),
                **({"hunk": e.hunk} if e.hunk else {}), "reason": e.reason or e.error, "applied": False}

    full_diff = "\n".join(d for d in diffs if d)
    files_changed = sorted(planned.keys())

    if dry_run:
        return {"success": True, "dry_run": True, "files_changed": files_changed,
                "insertions": total_ins, "deletions": total_del, "diff": full_diff, "applied": False}

    # Phase 2 — concurrent-modification guard, then atomic-ish write (all validated already)
    try:
        for rel, snap in snapshots.items():
            cur_raw = snap["target"].read_bytes()
            if cur_raw != snap["raw"]:
                return {"success": False, "error": "Concurrent modification detected", "file": rel,
                        "reason": "File changed between validation and write; no files were modified. Re-read and retry.",
                        "applied": False}
            # Re-resolve to catch symlink swaps between phases
            try:
                uretarget, _ = _resolve_patch_path(working_dir, rel)
            except PatchError as e:
                return {"success": False, "error": e.error, "file": rel, "reason": e.reason, "applied": False}
            if uretarget != snap["target"]:
                return {"success": False, "error": "Concurrent modification detected", "file": rel,
                        "reason": "File path resolution changed; no files were modified.", "applied": False}
        # All clear — write everything
        for rel in files_changed:
            p = planned[rel]
            target = p["target"]
            # Final safety: re-check containment (handles TOCTOU on new parents)
            _resolve_patch_path(working_dir, rel)
            if p["op"] == "delete":
                target.unlink()
            elif p["op"] == "add":
                target.parent.mkdir(parents=True, exist_ok=True)
                text = p["newline"].join(p["new_lines"])
                if p["trailing"] and p["new_lines"]:
                    text += p["newline"]
                target.write_bytes(text.encode("utf-8"))
            else:
                text = p["newline"].join(p["new_lines"])
                if p["trailing"] and p["new_lines"]:
                    text += p["newline"]
                elif not p["new_lines"]:
                    text = ""
                target.write_bytes(text.encode("utf-8"))
                if p.get("mode") is not None:
                    try:
                        os.chmod(target, p["mode"])
                    except Exception:
                        pass
    except PatchError as e:
        return {"success": False, "error": e.error, **({"file": e.file} if e.file else {}),
                **({"hunk": e.hunk} if e.hunk else {}), "reason": e.reason or e.error, "applied": False}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}", "reason": str(e), "applied": False}

    return {"success": True, "files_changed": files_changed, "insertions": total_ins,
            "deletions": total_del, "diff": full_diff, "applied": True}

# ── end apply_patch engine ──────────────────────────────────────────

# ── read_file engine (ranged reads + hard output limits) ──────────────
READ_MAX_LINES = 500
READ_MAX_BYTES = 50 * 1024
READ_TRUNC_LINES = "[OUTPUT TRUNCATED: 500 line limit]"
READ_TRUNC_BYTES = "[OUTPUT TRUNCATED: 50 KB output limit]"


def _parse_read_lineno(value, name, path):
    """Return an int line number or an ERROR string."""
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

    # Whole-file read (no range): raw content, subject to the same hard limits.
    if not has_start and not has_end:
        if total == 0:
            return "(empty file)"
        if len(all_lines) <= READ_MAX_LINES and len(text.encode("utf-8")) <= READ_MAX_BYTES:
            return text  # byte-identical to the file; never reflow small reads
        out_lines = all_lines
        notice = None
        if len(out_lines) > READ_MAX_LINES:
            out_lines = out_lines[:READ_MAX_LINES]
            notice = READ_TRUNC_LINES
        out = "\n".join(out_lines)
        if notice is None and len(out.encode("utf-8")) > READ_MAX_BYTES:
            # Trim to whole lines that fit within the byte budget.
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

    # Ranged read: 1-indexed, inclusive, rendered as 'N | content'.
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
        end = total  # clamp; header below reports the actual range returned
    if start > end:
        return f"ERROR: start_line ({start}) is after end_line ({end}) for {path_arg}"

    notice = None
    hi = end
    if hi - start + 1 > READ_MAX_LINES:
        hi = start + READ_MAX_LINES - 1
        notice = READ_TRUNC_LINES
    rendered = [f"{n} | {all_lines[n - 1]}" for n in range(start, hi + 1)]
    if notice is None:
        # Enforce the byte budget on whole rendered lines.
        while rendered and len(("\n".join(rendered)).encode("utf-8")) > READ_MAX_BYTES:
            rendered.pop()
        if len(rendered) < hi - start + 1:
            if not rendered:  # a single huge line exceeds the budget on its own
                rendered = [f"{start} | {all_lines[start - 1]}"]
            notice = READ_TRUNC_BYTES
            hi = start + len(rendered) - 1
    out = f"{path_arg} (lines {start}-{hi} of {total}):\n" + "\n".join(rendered)
    if notice:
        out += f"\n{notice}"
    return out

# ── search_code engine (pure-Python local search; grep only over SSH) ──
SEARCH_MAX_RESULTS = 200
SEARCH_MAX_LINE_CHARS = 500
SEARCH_MAX_FILE_BYTES = 10 * 1024 * 1024
SEARCH_SKIP_DIRS = {"node_modules", "__pycache__"}


def _local_search_code(pattern, base_dir, working_dir, file_pattern):
    """Walk base_dir and regex-match line by line. Returns the result string."""
    try:
        rx = re.compile(pattern)
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
        # Prune junk dirs in place (hidden, node_modules, __pycache__).
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d not in SEARCH_SKIP_DIRS)
        for name in sorted(files):
            if file_pattern and not fnmatch.fnmatch(name, file_pattern):
                continue
            fp = Path(root) / name
            try:
                if fp.stat().st_size > SEARCH_MAX_FILE_BYTES:
                    continue
                with open(fp, "rb") as f:
                    head = f.read(8192)
                    if b"\x00" in head:
                        continue  # binary, like grep -I
                    rest = f.read()
                text = (head + rest).decode("utf-8", errors="replace")
            except (OSError, PermissionError):
                continue
            try:
                rel = Path(fp).resolve().relative_to(work_resolved).as_posix()
            except ValueError:
                rel = str(fp)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
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

# ── end search_code engine ────────────────────────────────────────────

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
                               capture_output=True, text=True, timeout=30)
            out = []
            if r.stdout: out.append(f"STDOUT:\n{r.stdout}")
            if r.stderr: out.append(f"STDERR:\n{r.stderr}")
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
                # Remote hosts are Unix-like: grep over SSH still works there.
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

            # Local search is pure Python (grep does not exist on Windows).
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
        return "ERROR: Command timed out after 30 seconds"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

def fetch_url(url):
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script","style","nav","footer","header"]): tag.decompose()
        lines = [l for l in soup.get_text("\n", strip=True).splitlines() if l.strip()]
        return "\n".join(lines[:300])
    except Exception as e:
        return f"[Could not fetch {url}: {e}]"

def sanitize_messages_for_provider(messages, provider, model):
    """Ensure Gemini/Google OpenAI compatibility receives required thought signatures."""
    if provider != "google" and "gemini" not in model.lower():
        return messages

    sanitized = []
    for msg in messages:
        msg_copy = dict(msg)
        if msg_copy.get("role") == "assistant" and "tool_calls" in msg_copy:
            new_tcs = []
            for tc in msg_copy["tool_calls"]:
                tc_item = dict(tc)
                # Check for existing thought signature
                extra = tc_item.get("extra_content", {})
                existing_sig = None
                if isinstance(extra, dict):
                    existing_sig = extra.get("google", {}).get("thought_signature") or extra.get("thought_signature")
                if not existing_sig:
                    existing_sig = tc_item.get("thought_signature")

                # Fallback to bypass sentinel if missing
                sig = existing_sig or "skip_thought_signature_validator"
                tc_item["thought_signature"] = sig
                tc_item["extra_content"] = {"google": {"thought_signature": sig}}
                new_tcs.append(tc_item)
            msg_copy["tool_calls"] = new_tcs
        sanitized.append(msg_copy)
    return sanitized
def is_quota_exceeded(response):
    """Detect if the response contains Google AI Studio's quota exceeded error."""
    try:
        # Check non-200 responses to avoid consuming stream on success
        if not response.ok:
            return "You exceeded your current quota" in response.text
    except Exception:
        pass
    return False

def call_llm_stream(messages, api_key, model, provider="openrouter", max_retries=5):
    cfg = PROVIDERS.get(provider, PROVIDERS["openrouter"])
    payload = {
        "model": model,
        "messages": sanitize_messages_for_provider(messages, provider, model),
        "tools": TOOLS,
        "tool_choice": "auto",
        "stream": True
    }

    retries = 0
    while True:
        resp = requests.post(
            cfg["url"],
            headers=cfg["headers"](api_key),
            json=payload,
            timeout=120,
            stream=True
        )

        if is_quota_exceeded(resp):
            retries += 1
            print(f"\n[Google Quota Exceeded] 'You exceeded your current quota' detected. Waiting 15s before retry ({retries}/{max_retries})...")
            if retries >= max_retries:
                resp.raise_for_status()
            time.sleep(15)
            continue

        resp.raise_for_status()
        return resp

def sse_stream(api_key, model, wdir, msgs, rules_extra="", provider="openrouter"):
    system_content = SYSTEM_PROMPT
    if rules_extra:
        system_content += f"\n\n## User-defined rules\n{rules_extra}\n"
    api_msgs = [{"role":"system","content":system_content}] + msgs
    context_limit = False

    while True:
        try:
            stream_resp = call_llm_stream(api_msgs, api_key, model, provider)
        except Exception as e:
            err_msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                try: err_msg = e.response.text
                except: pass
            yield f"event: error\ndata: {json.dumps({'error': err_msg})}\n\n"
            return

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

                # Extract chunk-level thought signature
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
                            tool_calls[idx]["id"] += tc["id"]
                        if tc.get("function"):
                            fn = tc["function"]
                            if fn.get("name"):
                                tool_calls[idx]["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                tool_calls[idx]["function"]["arguments"] += fn["arguments"]

                        # Extract per-tool-call thought signature
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

        # Announce tool info
        for idx, tc in tool_calls.items():
            try: args = json.loads(tc["function"]["arguments"])
            except: args = {}
            if not tc["id"]:
                tc["id"] = f"call_{idx}_{int(datetime.now().timestamp())}"
            yield f"event: tool_info\ndata: {json.dumps({'index': idx, 'tool': tc['function']['name'], 'args': args})}\n\n"

        if assistant_content:
            log_agent_msg(assistant_content)

        # Assemble assistant message with preserved thought signatures
        formatted_tool_calls = []
        is_google = (provider == "google" or "gemini" in model.lower())
        for idx, tc in sorted(tool_calls.items()):
            call_dict = {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"]
                }
            }
            if is_google:
                sig = tc.get("thought_signature") or last_thought_sig or "skip_thought_signature_validator"
                call_dict["thought_signature"] = sig
                call_dict["extra_content"] = {"google": {"thought_signature": sig}}
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
            result = execute_tool(fn_name, args, wdir)
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
    wdir = request.json.get("working_dir", "").strip()
    return jsonify({"sessions": list_sessions(wdir)})

@app.route("/api/sessions/save", methods=["POST"])
def api_save_session():
    data = request.json
    wdir = data.get("working_dir", "").strip()
    sid  = data.get("session_id", "").strip() or datetime.now().strftime("%Y%m%d_%H%M%S")
    p = history_path(wdir, sid)
    p.write_text(json.dumps({
        "id": sid, "name": data.get("name", sid),
        "model": data.get("model", ""),
        "provider": data.get("provider", "openrouter"),
        "messages": data.get("messages", []),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M")
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True, "session_id": sid})

@app.route("/api/sessions/load", methods=["POST"])
def api_load_session():
    wdir = request.json.get("working_dir", "").strip()
    sid  = request.json.get("session_id", "").strip()
    p = history_path(wdir, sid)
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
    wdir = data.get("working_dir", "").strip()
    if not wdir: return jsonify({"ok": False, "error": "Missing working_dir"})
    cfg = {
        "provider": data.get("provider", "openrouter"),
        "api_key": data.get("api_key", ""),
        "model": data.get("model", ""),
        "rules_extra": data.get("rules_extra", ""),
        "working_dir": wdir
    }
    (Path(wdir) / PRESET_FILENAME).write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True})

@app.route("/api/preset/load", methods=["POST"])
def api_preset_load():
    wdir = request.json.get("working_dir", "").strip()
    p = Path(wdir) / PRESET_FILENAME
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
    return (Path(__file__).parent / "UI.html").read_text(encoding="utf-8")

@app.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    data = request.json
    api_key = data.get("api_key","").strip()
    model = data.get("model","").strip()
    wdir = data.get("working_dir","").strip()
    msgs = data.get("messages",[])
    rules = data.get("rules_extra","").strip()
    provider = data.get("provider","openrouter").strip()

    if not api_key or not model or not wdir:
        return jsonify({"error":"Missing api_key, model, or working_dir"}), 400

    if msgs and msgs[-1].get("role") == "user":
        log_sep(" USER MESSAGE ", "─")
        print(f"  [{provider.upper()}] {msgs[-1].get('content','')[:200]}")
        log_sep()

    return FlaskResponse(
        sse_stream(api_key, model, wdir, msgs, rules, provider),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    )

if __name__ == "__main__":
    log_sep("JUICE — AI Coding Agent", "═")
    print("  Server running at http://localhost:5000")
    print("  Providers supported: OpenRouter, Google AI Studio")
    log_sep()
    app.run(debug=True, port=5000, use_reloader=False)