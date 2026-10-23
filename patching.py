"""Patch engine behind Juice's ``apply_patch`` tool.

The engine accepts two dialects and normalises both into a single list of
operations before anything touches the disk:

* the **Juice format** — ``*** Update File:`` / ``*** Add File:`` /
  ``*** Delete File:`` / ``*** Move to:`` directives with ``@@`` hunks,
  optionally wrapped in ``*** Begin Patch`` / ``*** End Patch``;
* a **standard unified diff** — ``--- a/file`` / ``+++ b/file`` with ``@@`` hunks.

Both dialects end up as the same :class:`FileOp` objects, so planning,
validation, diffing and writing happen exactly once, in one place.

Everything is planned first: the patch is validated against the workspace and
the resulting writes are only performed once every operation succeeded.  A
rejected patch therefore leaves every file untouched, and a write that fails
half way is rolled back.
"""

import difflib
import os
import re
import stat
from pathlib import Path

MAX_PATCH_BYTES = 256 * 1024

PATCH_TEMPLATE_HINT = (
    "Expected either the Juice patch format — '*** Update File: <path>' with '@@' hunks "
    "(' ' context, '-' remove, '+' add), '*** Add File: <path>' with '+' lines, "
    "'*** Delete File: <path>', ending with '*** End Patch' — or a standard unified diff "
    "('--- a/<path>' / '+++ b/<path>' / '@@' hunks)."
)

_HUNK_HEADER_RE = re.compile(
    r"^@@\s*(?:-(?P<old>\d+)(?:,(?P<old_count>\d+))?\s*\+"
    r"(?P<new>\d+)(?:,(?P<new_count>\d+))?\s*@@)?\s*(?P<anchor>.*)$"
)

_DIRECTIVE_RE = re.compile(
    r"^\*\*\*\s*(update file|add file|delete file|move to)\s*:\s*(.*)$", re.IGNORECASE
)

_EOF_MARKER_RE = re.compile(r"^\*\*\*\s*end of file\s*$", re.IGNORECASE)

_GIT_METADATA = (
    "diff --git ",
    "index ",
    "new file mode",
    "deleted file mode",
    "old mode",
    "new mode",
    "similarity index ",
    "dissimilarity index ",
)

                                                                            
                                                                            
                                                                           
_MATCH_LEVELS = (
    ("exact", lambda a, b: a == b),
    ("trailing whitespace", lambda a, b: a.rstrip() == b.rstrip()),
    ("indentation", lambda a, b: a.strip() == b.strip()),
)


class PatchError(Exception):
    """A patch was rejected — nothing has been written."""

    def __init__(self, error, file=None, hunk=None, reason=None):
        super().__init__(error)
        self.error = error
        self.file = file
        self.hunk = hunk
        self.reason = reason

    def as_result(self):
        """Render the error as the JSON payload the tool returns to the model."""
        result = {
            "success": False,
            "error": self.error,
            "reason": self.reason or self.error,
            "applied": False,
        }
        if self.file:
            result["file"] = self.file
        if self.hunk:
            result["hunk"] = self.hunk
        return result


class Hunk:
    """One ``@@`` block: the text it looks for and the text it writes."""

    def __init__(self, header=None, old_start=None, old_count=None, new_count=None,
                 anchor="", lines=None, at_eof=False):
        self.header = header
        self.old_start = old_start                                         
        self.old_count = old_count                                                 
        self.new_count = new_count
        self.anchor = anchor                                                        
        self.lines = lines or []                                                       
        self.at_eof = at_eof                                      

    @property
    def old_text(self):
        """The lines the hunk expects to find in the file."""
        return [text for kind, text in self.lines if kind in (" ", "-")]

    @property
    def insertions(self):
        return sum(1 for kind, _ in self.lines if kind == "+")

    @property
    def deletions(self):
        return sum(1 for kind, _ in self.lines if kind == "-")

    @property
    def explicit_counts(self):
        """True when the header pinned down the size of the hunk."""
        return self.old_count is not None or self.new_count is not None


class FileOp:
    """A single file operation parsed out of a patch."""

    def __init__(self, kind, path, hunks=None, move_to=None):
        self.kind = kind                                       
        self.path = path
        self.hunks = hunks or []
        self.move_to = move_to                                               


def _normalize_patch_rel(path):
    path = (path or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _resolve_patch_path(working_dir, rel_path):
    """Resolve a workspace-relative path, refusing anything that escapes it."""
    rel = _normalize_patch_rel(rel_path)
    if not rel:
        raise PatchError("Invalid path", file=rel_path, reason="Empty file path")
    if "\x00" in rel:
        raise PatchError("Invalid path", file=rel, reason="Null byte in path")
    raw = (rel_path or "").strip()
    if (rel.startswith("/") or os.path.isabs(rel) or os.path.isabs(raw)
            or re.match(r"^[a-zA-Z]:[\\/]", raw) or rel.startswith("//")):
        raise PatchError("Path escapes workspace", file=rel,
                         reason="Absolute paths are not allowed; use workspace-relative paths")
    base = Path(working_dir).resolve()
    target = (base / Path(*rel.split("/"))).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise PatchError("Path escapes workspace", file=rel,
                         reason=f"Path '{rel}' resolves outside the workspace")
    return target, rel


def _detect_newline(raw):
    """Return the line ending style of a file and whether it ends with one."""
    if b"\r\n" in raw:
        style = "\r\n"
    elif b"\r" in raw and b"\n" not in raw:
        style = "\r"
    else:
        style = "\n"
    trailing = (raw.endswith(b"\n") or raw.endswith(b"\r")) if raw else True
    return style, trailing


def _is_binary_raw(raw):
    return b"\x00" in raw


def _strip_code_fences(text):
    """Drop a surrounding markdown code fence and blank padding."""
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    return lines


def _is_marker(text, word):
    """True for a wrapper marker such as '*** Begin Patch'."""
    stripped = text.strip()
    if not stripped.startswith("***"):
        return False
    return stripped.strip("*").strip().lower() == word


def _strip_diff_path(path):
    """Turn 'a/src/app.py' into 'src/app.py'."""
    path = (path or "").strip()
    if len(path) >= 2 and path[0] in "\"'" and path[-1] == path[0]:
        path = path[1:-1]
    path = path.split("\t")[0].strip()
    if path.startswith(("a/", "b/")) and len(path) > 2:
        path = path[2:]
    return path


class _Parser:
    """Single pass parser that understands both patch dialects."""

    def __init__(self, lines):
        self.lines = lines                                        
        self.ops = []
        self.current = None                                   
        self.hunk = None                                    
        self.format = None                                 
        self.pending_old = None                                          

                                                                          

    def _fail(self, error, reason, file=None, hunk=None):
        raise PatchError(error, file=file, hunk=hunk, reason=reason)

    def _current_file(self):
        return _normalize_patch_rel(self.current.path) if self.current else None

    def _starts_section(self, text):
        """True when a line begins a new file section or hunk."""
        return (text.startswith("@@") or text.startswith("***")
                or text.startswith("---") or text.startswith("+++"))

    def _next_is_section(self, index):
        """True when the next non-empty line starts a new section."""
        for _, text in self.lines[index + 1:]:
            if not text.strip():
                continue
            return self._starts_section(text)
        return True

    def _next_is_hunk_line(self, index):
        """True when the next line looks like the body of a hunk."""
        if index + 1 >= len(self.lines):
            return False
        text = self.lines[index + 1][1]
        return text == "" or text[0] in (" ", "+", "-", "\\")

                                                                          

    def parse(self):
        index = 0
        while index < len(self.lines):
            index = self._handle_line(index)
        self._check_complete()
        if not self.ops:
            self._fail("Invalid patch", "No file operations found. " + PATCH_TEMPLATE_HINT)
        return self.ops

    def _check_complete(self):
        """A hunk that declared line counts must have received all of them."""
        hunk = self.hunk
        if hunk is None or not hunk.explicit_counts or self._hunk_complete(hunk):
            return
        old_seen = sum(1 for kind, _ in hunk.lines if kind in (" ", "-"))
        new_seen = sum(1 for kind, _ in hunk.lines if kind in (" ", "+"))
        old_need = hunk.old_count if hunk.old_count is not None else old_seen
        new_need = hunk.new_count if hunk.new_count is not None else new_seen
        self._fail("Invalid patch", file=self._current_file(),
                   hunk=len(self.current.hunks),
                   reason=f"Hunk is truncated: '{hunk.header}' expected {old_need} old and "
                          f"{new_need} new lines, got {old_seen} and {new_seen}")

    def _handle_line(self, index):
        lineno, text = self.lines[index]

        if _is_marker(text, "begin patch") or _is_marker(text, "end patch"):
            return index + 1

        directive = _DIRECTIVE_RE.match(text)
        if directive:
            self._start_directive(directive.group(1).lower(), directive.group(2).strip(), lineno)
            return index + 1

        if self.current is None:
            return self._handle_orphan_line(index)
        if self.format == "unified":
            return self._handle_unified_line(index)
        return self._handle_juice_line(index)

                                                                          

    def _handle_git_line(self, index):
        """Handle the git metadata that may precede a diff section."""
        lineno, text = self.lines[index]
        if text.startswith(_GIT_METADATA):
            return True
        if text.startswith("rename from"):
            self._start_rename(_strip_diff_path(text[len("rename from"):]), lineno)
            return True
        if text.startswith("rename to"):
            self._finish_rename(_strip_diff_path(text[len("rename to"):]), lineno)
            return True
        return False

    def _handle_orphan_line(self, index):
        """A line that arrives before any file section was opened."""
        lineno, text = self.lines[index]
        if self._handle_git_line(index):
            return index + 1
        if text.startswith("---"):
            self.pending_old = _strip_diff_path(text[3:])
            return index + 1
        if text.startswith("+++"):
            self._start_unified_op(index)
            return index + 1
        if text.startswith("@@"):
            self._fail("Invalid patch", f"'@@' hunk outside of a file section (line {lineno})")
        if not text.strip():
            return index + 1
        self._fail("Invalid patch",
                   f"Content outside of a file section (line {lineno}): '{text[:60]}'")

    def _handle_unified_line(self, index):
        """A line inside a unified diff."""
        lineno, text = self.lines[index]
        if self._handle_git_line(index):
            return index + 1
        if text.startswith("Binary files ") or (text.startswith("Binary ") and "differ" in text):
            self._fail("Invalid patch", "Binary files are not supported")

        if self.hunk is None:
            if text.startswith("---"):
                self.pending_old = _strip_diff_path(text[3:])
                self.current = None
                return index + 1
            if text.startswith("+++"):
                self._start_unified_op(index)
                return index + 1
            if text.startswith("@@"):
                self._start_hunk(text, lineno)
                return index + 1
            if not text.strip():
                return index + 1
            self._fail("Invalid patch",
                       f"Content outside of a file diff (line {lineno}): '{text[:60]}'")

                                                                             
                                                                           
        if (not self.hunk.explicit_counts and text.startswith("---")
                and self._looks_like_unified_header(index)):
            self.hunk = None
            self.pending_old = _strip_diff_path(text[3:])
            self.current = None
            return index + 1

        return self._consume_hunk_line(index)

    def _handle_juice_line(self, index):
        """A line inside a Juice format section."""
        lineno, text = self.lines[index]
        if text.startswith("@@"):
            if self.current.kind == "add" and not self._next_is_hunk_line(index):
                return self._consume_add_line(index)
            self._start_hunk(text, lineno)
            return index + 1
        if _EOF_MARKER_RE.match(text):
            if self.hunk is not None:
                self.hunk.at_eof = True
            return index + 1
        if self.current.kind == "add":
            return self._consume_add_line(index)
        if self.hunk is None:
                                                                           
                                                                               
            if text.startswith("---") and self._looks_like_unified_header(index):
                self.format = "unified"
                self.pending_old = _strip_diff_path(text[3:])
                self.current = None
                return index + 1
            if not text.strip():
                return index + 1
            if text[0] not in (" ", "+", "-"):
                self._fail("Invalid hunk line", file=self._current_file(),
                           hunk=1,
                           reason=f"Line must start with ' ', '+' or '-': '{text[:80]}'")
            self._start_hunk("@@", lineno)
            return self._consume_hunk_line(index)
        if text == "" and self._next_is_section(index):
                                                                         
            return index + 1
        return self._consume_hunk_line(index)

    def _looks_like_unified_header(self, index):
        """True when the next line is the '+++' half of a diff header pair."""
        if index + 1 >= len(self.lines):
            return False
        return self.lines[index + 1][1].startswith("+++")

                                                                          

    def _start_directive(self, kind, path, lineno):
        if kind == "move to":
            if self.current is None:
                self._fail("Invalid directive",
                           f"'*** Move to:' without a preceding file directive (line {lineno})")
            if self.current.kind != "update":
                self._fail("Invalid directive", file=self._current_file(),
                           reason="'*** Move to:' is only supported after '*** Update File:'")
            if self.current.move_to:
                self._fail("Invalid directive", file=self._current_file(),
                           reason="A file can only be moved once")
            self.current.move_to = path
            return
        if not path:
            self._fail("Invalid directive", f"Missing path in file directive (line {lineno})")
        op = FileOp(kind=kind.split()[0], path=path)
        self.ops.append(op)
        self.current = op
        self.hunk = None
        self.format = "juice"

    def _start_unified_op(self, index):
        """Open the file section described by a '--- ' / '+++ ' header pair."""
        lineno, text = self.lines[index]
        if self.pending_old is None:
            self._fail("Invalid patch",
                       f"Found '+++ ...' without a preceding '--- ...' line (line {lineno})")
        old_path = self.pending_old
        new_path = _strip_diff_path(text[3:])
        self.pending_old = None
        if old_path in ("/dev/null", "dev/null"):
            op = FileOp("add", new_path)
        elif new_path in ("/dev/null", "dev/null"):
            op = FileOp("delete", old_path)
        elif old_path != new_path:
            op = FileOp("update", old_path, move_to=new_path)
        else:
            op = FileOp("update", new_path)
        self.ops.append(op)
        self.current = op
        self.hunk = None
        self.format = "unified"

    def _start_rename(self, old_path, lineno):
        self.pending_old = old_path

    def _finish_rename(self, new_path, lineno):
        if not self.pending_old:
            self._fail("Invalid patch", f"Found 'rename to' without 'rename from' (line {lineno})")
        op = FileOp(kind="update", path=self.pending_old, move_to=new_path)
        self.ops.append(op)
        self.current = op
        self.hunk = None
        self.format = "unified"
        self.pending_old = None

                                                                          

    def _start_hunk(self, header, lineno):
        if self.current is None:
            self._fail("Invalid patch", f"'@@' hunk outside of a file section (line {lineno})")
        if self.current.kind == "delete" and self.format == "juice":
            self._fail("Invalid patch", file=self._current_file(),
                       reason="*** Delete File *** must not contain hunks")
        match = _HUNK_HEADER_RE.match(header.strip())
        if not match:
            self._fail("Invalid hunk header", file=self._current_file(),
                       hunk=len(self.current.hunks) + 1,
                       reason=f"Malformed '@@' header: '{header}'")
        old_start = match.group("old")
        old_count = match.group("old_count")
        new_count = match.group("new_count")
        hunk = Hunk(
            header=header,
            old_start=int(old_start) - 1 if old_start else None,
            old_count=int(old_count) if old_count is not None else None,
            new_count=int(new_count) if new_count is not None else None,
            anchor=(match.group("anchor") or "").strip(),
        )
        if hunk.old_start is not None and hunk.old_start < 0:
            hunk.old_start = 0
        self.current.hunks.append(hunk)
        self.hunk = hunk

    def _consume_hunk_line(self, index):
        """Append one line of hunk body."""
        lineno, text = self.lines[index]
        hunk = self.hunk
        if text.startswith("\\"):
                                                               
            return index + 1
        if text == "":
            kind, body = " ", ""
        else:
            kind = text[0]
            body = text[1:]
            if kind not in (" ", "+", "-"):
                self._fail("Invalid hunk line", file=self._current_file(),
                           hunk=len(self.current.hunks),
                           reason=f"Line must start with ' ', '+' or '-': '{text[:80]}'")
        hunk.lines.append((kind, body))
        if hunk.explicit_counts and self._hunk_complete(hunk):
            self.hunk = None
        return index + 1

    def _hunk_complete(self, hunk):
        """True when an explicitly counted hunk has received all its lines."""
        old_seen = sum(1 for kind, _ in hunk.lines if kind in (" ", "-"))
        new_seen = sum(1 for kind, _ in hunk.lines if kind in (" ", "+"))
        old_need = hunk.old_count
        new_need = hunk.new_count
        if old_need is None and new_need is None:
            return False
        if old_need is not None and old_seen < old_need:
            return False
        if new_need is not None and new_seen < new_need:
            return False
        return True

    def _consume_add_line(self, index):
        """Append one line to an '*** Add File' section."""
        lineno, text = self.lines[index]
        if not self.hunk:
            self._start_hunk("@@", lineno)
        if text == "":
            if self._next_is_section(index):
                return index + 1
            body = ""
        elif text.startswith("+"):
            body = text[1:]
        elif text == " ":
            body = ""
        else:
            body = text
        self.hunk.lines.append(("+", body))
        return index + 1


def _extract_body(lines):
    """Return the patch body as (line number, text) pairs."""
    begin = end = None
    for index, text in enumerate(lines):
        if begin is None and _is_marker(text, "begin patch"):
            begin = index
        elif begin is not None and _is_marker(text, "end patch"):
            end = index
            break
    if begin is None:
        return [(index + 1, text) for index, text in enumerate(lines)]
    if end is None:
        raise PatchError("Invalid patch",
                         reason="Missing '*** End Patch' (patch was cut off? resend the "
                                "COMPLETE patch). " + PATCH_TEMPLATE_HINT)
    return [(index + 1, lines[index]) for index in range(begin + 1, end)]


def parse_patch(patch_text):
    """Parse a patch into a list of :class:`FileOp`.

    Raises :class:`PatchError` when the patch cannot be understood.
    """
    if patch_text is None or not isinstance(patch_text, str):
        raise PatchError("Invalid patch", reason="Patch must be a string")
    if len(patch_text.encode("utf-8")) > MAX_PATCH_BYTES:
        raise PatchError(f"Patch too large (max {MAX_PATCH_BYTES} bytes)",
                         reason="Configurable limit exceeded")
    lines = _strip_code_fences(patch_text)
    body = _extract_body(lines)
    ops = _Parser(body).parse()
    return _validate_ops(ops)


def _validate_ops(ops):
    """Merge repeated sections and reject patches that cannot be applied."""
    merged = []
    by_path = {}
    for op in ops:
        rel = _normalize_patch_rel(op.path)
        op.path = rel
        existing = by_path.get(rel)
        if existing is None:
            by_path[rel] = op
            merged.append(op)
            continue
                                                                             
                                      
        if existing.kind != "update" or op.kind != "update":
            raise PatchError("Duplicate file operation", file=rel,
                             reason=f"File '{rel}' appears more than once; combine it into one section")
        if op.move_to and existing.move_to and op.move_to != existing.move_to:
            raise PatchError("Invalid move", file=rel,
                             reason=f"File '{rel}' is moved to two different paths")
        existing.hunks.extend(op.hunks)
        if op.move_to:
            existing.move_to = op.move_to

    for op in merged:
        if op.move_to:
            dest = _normalize_patch_rel(op.move_to)
            op.move_to = dest
            if dest == op.path:
                raise PatchError("Invalid move", file=op.path,
                                 reason="'*** Move to:' points at the same path")
            if dest in by_path:
                raise PatchError("Duplicate file operation", file=dest,
                                 reason=f"File '{dest}' appears more than once; combine it into one section")
        if op.kind in ("update", "add") and not op.hunks and not op.move_to:
            raise PatchError("Missing hunks", file=op.path,
                             reason=f"*** {'Update' if op.kind == 'update' else 'Add'} File *** "
                                    "has no hunks/content")
        for index, hunk in enumerate(op.hunks, start=1):
            if not hunk.lines:
                raise PatchError("Empty hunk", file=op.path, hunk=index,
                                 reason="Hunk contains no content lines")
            if op.kind == "add":
                bad = [kind for kind, _ in hunk.lines if kind == "-"]
                if bad:
                    raise PatchError("Invalid Add File hunk", file=op.path, hunk=index,
                                     reason="*** Add File *** may only contain added lines")
    return merged


def _mismatch_hint(file_lines, old_seq):
    """Explain why a hunk did not match, in terms the model can act on."""
    interesting = [line for line in old_seq if line.strip()]
    if not interesting or not file_lines:
        return ""
    exact = set(file_lines)
    missing = [line for line in interesting if line not in exact]
    if not missing:
        return ""
    rstripped = {line.rstrip() for line in file_lines}
    if all(line in rstripped for line in missing):
        return (" Hint: those lines exist in the file but differ by trailing whitespace — "
                "copy them exactly.")
    stripped = {line.strip() for line in file_lines}
    if all(line.strip() in stripped for line in missing):
        return (" Hint: matching lines exist but indentation differs — copy the exact leading "
                "whitespace from the file (read it first).")
    first = missing[0].strip()
    if len(first) >= 10:
        for number, file_line in enumerate(file_lines, start=1):
            if first in file_line and file_line.strip() != first:
                return (f" Hint: similar text at line {number} ('{file_line.strip()[:80]}') — "
                        "context must match exactly, extend it.")
    return ""


def _narrow_by_anchor(file_lines, hits, anchor, size):
    """Keep only the candidate positions that sit near the anchor text."""
    needle = (anchor or "").strip()
    if not needle:
        return hits
    narrowed = []
    for hit in hits:
        for number in range(max(0, hit - 40), hit):
            if needle in file_lines[number].strip():
                narrowed.append(hit)
                break
        else:
            if any(needle in line.strip() for line in file_lines[hit:hit + size]):
                narrowed.append(hit)
    return narrowed


def _find_hunk_index(file_lines, old_seq, hint=None, anchor=""):
    """Locate where a hunk applies.

    Returns ``(index, level)`` where ``level`` is the entry of
    :data:`_MATCH_LEVELS` that matched.  Raises :class:`PatchError` when the
    hunk is missing or ambiguous.
    """
    total, size = len(file_lines), len(old_seq)
    if size == 0:
        if hint is not None and 0 <= hint <= total:
            return hint, 0
        raise PatchError("Hunk failed to apply",
                         reason="Hunk has no context/removal lines; add context lines or a "
                                "'@@ -start,count +start,count @@' offset")
    for level, (_, matcher) in enumerate(_MATCH_LEVELS):
        hits = [index for index in range(total - size + 1)
                if all(matcher(file_lines[index + offset], old_seq[offset])
                       for offset in range(size))]
        if not hits:
            continue
        if len(hits) == 1:
            return hits[0], level
        narrowed = _narrow_by_anchor(file_lines, hits, anchor, size)
        if len(narrowed) == 1:
            return narrowed[0], level
        if len(narrowed) > 1:
            raise PatchError("Hunk failed to apply",
                             reason=f"Ambiguous match: context occurs {len(narrowed)} times. "
                                    "Add more surrounding context lines to make it unique.")
        break
    preview = " | ".join(old_seq[:3])[:200]
    raise PatchError("Hunk failed to apply",
                     reason=f"Expected context was not found (looking for: '{preview}...'). "
                            "Read the file first and copy context exactly."
                     + _mismatch_hint(file_lines, old_seq))


def _leading_ws(text):
    """The leading whitespace of a line."""
    return text[:len(text) - len(text.lstrip())]


def _indent_shift(file_lines, at, hunk):
    """How far the patch's indentation is off compared to the file.

    Returns the shift in characters that has to be applied to the lines the
    hunk adds, so a patch that forgot the indentation still produces properly
    indented code.
    """
    removed = []
    context = []
    offset = 0
    for kind, text in hunk.lines:
        if kind not in (" ", "-"):
            continue
        index = at + offset
        offset += 1
        if index >= len(file_lines):
            break
        pair = (_leading_ws(text), _leading_ws(file_lines[index]))
        (removed if kind == "-" else context).append(pair)
    anchor = (removed or context or [("", "")])[0]
    patch_ws, file_ws = anchor
    if patch_ws == file_ws:
        return 0
    if "\t" in patch_ws or "\t" in file_ws:
        return 0                              
    return len(file_ws) - len(patch_ws)


def _reindent(text, shift):
    """Shift the indentation of a line by ``shift`` characters."""
    if not shift:
        return text
    stripped = text.lstrip()
    if not stripped:
        return text
    target = (len(text) - len(stripped)) + shift
    return " " * max(target, 0) + stripped


def _replacement(file_lines, at, hunk):
    """The text a hunk writes, keeping the file's own context lines."""
    out = []
    offset = 0
    shift = _indent_shift(file_lines, at, hunk)
    for kind, text in hunk.lines:
        if kind == " ":
            out.append(file_lines[at + offset])
            offset += 1
        elif kind == "-":
            offset += 1
        else:
            out.append(_reindent(text, shift))
    return out


def _apply_hunks_to_lines(orig_lines, hunks, rel):
    """Apply every hunk to a list of lines, in order."""
    current = list(orig_lines)
    insertions = deletions = 0
    relaxed = 0
    for index, hunk in enumerate(hunks, start=1):
        old_seq = hunk.old_text
        try:
            at, level = _find_hunk_index(current, old_seq, hunk.old_start, hunk.anchor)
        except PatchError as exc:
            exc.file = rel
            exc.hunk = index
            exc.error = "Hunk failed to apply"
            raise
        if hunk.at_eof and at != len(current) - len(old_seq):
            raise PatchError("Hunk failed to apply", file=rel, hunk=index,
                             reason="'*** End of File' marker: the hunk does not end at the "
                                    "end of the file")
        current[at:at + len(old_seq)] = _replacement(current, at, hunk)
        insertions += hunk.insertions
        deletions += hunk.deletions
        relaxed = max(relaxed, level)
    return current, insertions, deletions, relaxed


class PatchPlan:
    """A validated patch, ready to be written."""

    def __init__(self, working_dir):
        self.working_dir = working_dir
        self.entries = []                                            
        self.notes = []                                                 
        self.insertions = 0
        self.deletions = 0

                                                                          

    def build(self, ops):
        for op in ops:
            self._plan_op(op)
        return self

    def _plan_op(self, op):
        source = op.path
        target, _ = _resolve_patch_path(self.working_dir, source)
        destination = target
        if op.move_to:
            destination, _ = _resolve_patch_path(self.working_dir, op.move_to)

        if op.kind == "delete":
            self._plan_delete(source, target)
            return
        if op.kind == "add":
            self._plan_add(source, destination, op)
            return
        self._plan_update(source, target, destination, op)

    def _read(self, target, rel, action):
        if not os.path.lexists(target) or not target.is_file():
            raise PatchError(f"{action} failed", file=rel,
                             reason="File not found; use '*** Add File ***' for new files")
        raw = target.read_bytes()
        if _is_binary_raw(raw):
            raise PatchError(f"{action} failed", file=rel, reason="Binary files are not supported")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise PatchError(f"{action} failed", file=rel,
                             reason="File is not valid UTF-8 (binary?)")
        return raw, text

    def _plan_add(self, rel, target, op):
        if os.path.lexists(target):
            raise PatchError("Add failed", file=rel,
                             reason="File already exists — use '*** Update File:' to change it")
        new_lines = []
        for hunk in op.hunks:
            for kind, text in hunk.lines:
                if kind in ("+", " "):
                    new_lines.append(text)
        entry = {
            "rel": rel,
            "dest_rel": rel,
            "target": target,
            "action": "add",
            "new_lines": new_lines,
            "old_lines": [],
            "newline": "\n",
            "trailing": bool(new_lines),
            "mode": None,
            "source": None,
        }
        self.entries.append(entry)
        self.insertions += len(new_lines)

    def _plan_delete(self, rel, target):
        raw, text = self._read(target, rel, "Delete")
        old_lines = text.splitlines()
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            mode = None
        entry = {
            "rel": rel,
            "dest_rel": rel,
            "target": target,
            "action": "delete",
            "new_lines": None,
            "old_lines": old_lines,
            "newline": "\n",
            "trailing": False,
            "mode": mode,
            "source": None,
            "snapshot": raw,
        }
        self.entries.append(entry)
        self.deletions += len(old_lines)

    def _plan_update(self, rel, target, destination, op):
        raw, text = self._read(target, rel, "Update")
        newline, trailing = _detect_newline(raw)
        old_lines = text.splitlines()
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            mode = None
        new_lines, insertions, deletions, relaxed = _apply_hunks_to_lines(old_lines, op.hunks, rel)
        if relaxed:
            level = _MATCH_LEVELS[relaxed][0]
            self.notes.append(f"{rel}: context matched ignoring {level}")
        entry = {
            "rel": rel,
            "dest_rel": op.move_to or rel,
            "target": destination,
            "action": "update",
            "new_lines": new_lines,
            "old_lines": old_lines,
            "newline": newline,
            "trailing": trailing,
            "mode": mode,
            "source": target if destination != target else None,
            "snapshot": raw,
        }
        self.entries.append(entry)
        self.insertions += insertions
        self.deletions += deletions
        if op.move_to:
            self.notes.append(f"{rel}: renamed to {op.move_to}")

                                                                          

    def _check_unchanged(self):
        """Fail if a file changed between validation and writing."""
        for entry in self.entries:
            snapshot = entry.get("snapshot")
            if snapshot is None:
                continue
            target = entry["source"] or entry["target"]
            rel = entry["rel"]
            try:
                current = target.read_bytes()
            except OSError as exc:
                raise PatchError("Concurrent modification detected", file=rel,
                                 reason=f"File could not be read before writing: {exc}")
            if current != snapshot:
                raise PatchError("Concurrent modification detected", file=rel,
                                 reason="File changed between validation and write; no files were "
                                        "modified. Re-read the file and retry.")
            resolved, _ = _resolve_patch_path(self.working_dir, rel)
            if resolved != target:
                raise PatchError("Concurrent modification detected", file=rel,
                                 reason="File path resolution changed; no files were modified.")

    def commit(self):
        """Write every planned change, rolling back if one of them fails."""
        self._check_unchanged()
        undo = []
        try:
            for entry in self.entries:
                undo.append(self._write(entry))
        except Exception:
            for action in reversed(undo):
                try:
                    action()
                except OSError:
                    pass
            raise

    def _write(self, entry):
        """Perform one write and return the action that undoes it."""
        rel = entry["rel"]
        target = entry["target"]
        action = entry["action"]
        if action == "delete":
            target.unlink()
            return lambda: self._restore(entry["target"], entry)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = entry["newline"].join(entry["new_lines"])
        if entry["trailing"] and entry["new_lines"]:
            text += entry["newline"]
        target.write_bytes(text.encode("utf-8"))
        if entry.get("mode") is not None:
            try:
                os.chmod(target, entry["mode"])
            except OSError:
                pass
        source = entry.get("source")
        if source is not None:
            source.unlink()
            return lambda: self._restore(source, entry, remove=target)
        if action == "add":
            return lambda: self._remove(target)
        return lambda: self._restore(target, entry)

    def _restore(self, path, entry, remove=None):
        """Undo a write: put the original bytes back and drop what was added."""
        snapshot = entry.get("snapshot")
        if snapshot is not None:
            path.write_bytes(snapshot)
            if entry.get("mode") is not None:
                try:
                    os.chmod(path, entry["mode"])
                except OSError:
                    pass
        if remove is not None:
            self._remove(remove)

    @staticmethod
    def _remove(path):
        if os.path.lexists(path):
            path.unlink()

                                                                          

    @property
    def files_changed(self):
        return sorted(entry["dest_rel"] for entry in self.entries)

    def diff(self):
        """The unified diff of everything the patch does."""
        chunks = []
        for entry in self.entries:
            source = f"a/{entry['rel']}"
            destination = f"b/{entry['dest_rel']}"
            if entry["action"] == "delete":
                source, destination = source, "/dev/null"
            elif entry["action"] == "add":
                source, destination = "/dev/null", destination
            chunk = difflib.unified_diff(entry["old_lines"], entry["new_lines"] or [],
                                         fromfile=source, tofile=destination, lineterm="")
            text = "\n".join(list(chunk))
            if text:
                chunks.append(text)
        return "\n".join(chunks)

    def result(self, dry_run=False, applied=True):
        payload = {
            "success": True,
            "files_changed": self.files_changed,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "diff": self.diff(),
            "applied": applied,
        }
        if dry_run:
            payload["dry_run"] = True
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


def execute_apply_patch(patch, dry_run=False, working_dir="."):
    """Apply a patch to the workspace.

    Returns a dict describing what happened: ``success``, ``files_changed``,
    ``insertions``, ``deletions``, ``diff`` and ``applied``.  On failure the
    dict carries ``error`` and ``reason`` and nothing was written.
    """
    base = Path(working_dir)
    if not base.exists() or not base.is_dir():
        return {"success": False, "error": "Invalid working directory",
                "reason": f"{working_dir} is not a directory", "applied": False}
    try:
        ops = parse_patch(patch)
    except PatchError as exc:
        return exc.as_result()

    plan = PatchPlan(str(base))
    try:
        plan.build(ops)
    except PatchError as exc:
        return exc.as_result()

    if dry_run:
        return plan.result(dry_run=True, applied=False)

    try:
        plan.commit()
    except PatchError as exc:
        return exc.as_result()
    except OSError as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}",
                "reason": str(exc), "applied": False}
    return plan.result()
