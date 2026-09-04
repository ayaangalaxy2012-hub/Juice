import os
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
    {"type":"function","function":{"name":"read_file","description":"Read the full contents of a file at the given path (relative to working directory).","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
    {"type":"function","function":{"name":"write_file","description":"Write (or overwrite) a file at the given path with the provided content.","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}}},
    {"type":"function","function":{"name":"edit_file","description":"Replace the FIRST occurrence of old_str with new_str in the file. old_str must match EXACTLY.","parameters":{"type":"object","properties":{"path":{"type":"string"},"old_str":{"type":"string"},"new_str":{"type":"string"}},"required":["path","old_str","new_str"]}}},
    {"type":"function","function":{"name":"list_dir","description":"List files and directories in working dir or a subdirectory.","parameters":{"type":"object","properties":{"subpath":{"type":"string","default":""}},"required":[]}}},
    {"type":"function","function":{"name":"run_command","description":"Run any shell command in the working directory.","parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}},
    {"type":"function","function":{"name":"search_code","description":"Search for a regex pattern across files in the workspace (or a subpath). Uses grep via SSH as backend — can search locally or on a remote host.","parameters":{"type":"object","properties":{"pattern":{"type":"string","description":"The regex pattern to search for"},"path":{"type":"string","description":"Optional subdirectory to search in (relative to working dir). Defaults to the whole working directory."},"file_pattern":{"type":"string","description":"Optional file glob pattern to filter (e.g. '*.py', '*.js', '*.go')."},"host":{"type":"string","description":"Optional SSH host to search on remotely (e.g. 'user@server'). If omitted, searches locally."},"remote_path":{"type":"string","description":"Base path on the remote host when using SSH. Defaults to the working directory on remote."}},"required":["pattern"]}}},
]

SYSTEM_PROMPT = """You are juice, an expert AI coding agent with full access to the user's file system and terminal.

You have 6 tools:
1. read_file(path) — read any file. Always do this before editing.
2. write_file(path, content) — create or overwrite a file.
3. edit_file(path, old_str, new_str) — replace exact string in a file. Read file first to get exact text.
4. list_dir(subpath="") — list files/dirs.
5. run_command(command) — run any shell command.
6. search_code(pattern, path="", file_pattern="", host="", remote_path="") — search for regex pattern across files using grep. Supports local and SSH remote search.

Rules:
- Call tools autonomously, no user approval needed.
- Chain tool calls until the task is fully done.
- Only write a final reply when all tool calls are complete.
- Always read files before editing them.
- If a command fails, diagnose and fix automatically.
- Write tests using run_command tool to test for errors or bugs.

Note: The user may provide additional rules in the chat. Follow these rules if provided.
"""

def execute_tool(name, args, working_dir):
    try:
        if name == "read_file":
            p = Path(working_dir) / args["path"]
            if not p.exists(): return f"ERROR: File not found: {args['path']}"
            return p.read_text(encoding="utf-8", errors="replace")

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
            pattern = args["pattern"]
            search_path = args.get("path", "")
            file_pattern = args.get("file_pattern", "")
            host = args.get("host", "").strip()
            remote_path = args.get("remote_path", "").strip()

            base_path = str(Path(working_dir) / search_path) if search_path else working_dir
            grep_cmd = f"grep -rnI --no-messages"
            if file_pattern:
                grep_cmd += f" --include={shlex.quote(file_pattern)}"
            grep_cmd += f" -e {shlex.quote(pattern)} -- {shlex.quote(base_path)} | head -200"

            if host:
                remote_dir = remote_path if remote_path else base_path
                ssh_cmd = f"grep -rnI --no-messages"
                if file_pattern:
                    ssh_cmd += f" --include={shlex.quote(file_pattern)}"
                ssh_cmd += f" -e {shlex.quote(pattern)} -- {shlex.quote(remote_dir)} | head -200"
                full_cmd = f"ssh -o StrictHostKeyChecking=no {shlex.quote(host)} {shlex.quote(ssh_cmd)}"
                r = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=60)
            else:
                r = subprocess.run(grep_cmd, shell=True, capture_output=True, text=True, timeout=60)

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