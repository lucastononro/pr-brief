#!/usr/bin/env python3
"""Local HTTP server for pr-brief review UI.

Serves static files (index.html, data.json) + bridges to `gh` CLI via a
couple of POST endpoints. Stdlib-only, no pip install needed.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ARGS = None
ROOT = Path(__file__).parent.resolve()

# GitHub's secondary rate limit fires when many writes hit the same resource
# in quick succession. Their guidance: at least 1s between writes. We use 1.5s
# for headroom across concurrent saves.
_MIN_WRITE_GAP = 1.5
_write_lock = threading.Lock()
_last_write_ts = 0.0


def throttle_write():
    """Block until at least _MIN_WRITE_GAP has passed since the last write."""
    global _last_write_ts
    with _write_lock:
        now = time.monotonic()
        wait = (_last_write_ts + _MIN_WRITE_GAP) - now
        if wait > 0:
            time.sleep(wait)
        _last_write_ts = time.monotonic()


def is_secondary_rate_limit(text):
    return "secondary rate limit" in (text or "").lower()


# Inline-chat state. Only one chat turn runs at a time so the streaming endpoint
# can hand the subprocess back to /api/chat-cancel cleanly (and so two concurrent
# `claude --resume` calls don't race on the same session jsonl).
_chat_lock = threading.Lock()
_chat_proc_lock = threading.Lock()
_current_chat_proc = None  # subprocess.Popen | None


def run_gh(args, input_data=None):
    """Run a gh command and return (stdout, stderr, returncode)."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            input=input_data,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout, result.stderr, result.returncode
    except FileNotFoundError:
        return "", "gh CLI not found on PATH", 127
    except subprocess.TimeoutExpired:
        return "", "gh command timed out after 30s", 124


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[server] {fmt % args}\n")

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            data = Path(path).read_bytes()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_file(ROOT / "index.html", "text/html; charset=utf-8")
        elif self.path == "/data.json":
            self._send_file(ROOT / "data.json", "application/json")
        elif self.path == "/api/context":
            self._send_json(200, {
                "pr": ARGS.pr,
                "repo": ARGS.repo,
                "sha": ARGS.sha,
            })
        elif self.path == "/api/chat-context":
            self._send_json(200, {
                "enabled": bool(ARGS.session_id),
                "session_id": ARGS.session_id,
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"bad json: {e}"})
            return

        if self.path == "/api/auth-status":
            out, err, rc = run_gh(["auth", "status"])
            self._send_json(200, {
                "ok": rc == 0,
                "message": (out + err).strip(),
            })
            return

        if self.path == "/api/submit-review":
            comments = body.get("comments", [])
            summary = body.get("summary", "")
            if not comments:
                self._send_json(400, {"error": "no comments to submit"})
                return
            api_comments = []
            for c in comments:
                entry = {"path": c["path"], "body": c["body"]}
                if "line" in c and "side" in c:
                    # GitHub line/side format — supports multi-line via start_line/start_side
                    entry["line"] = c["line"]
                    entry["side"] = c["side"]
                    if c.get("start_line") and c.get("start_side"):
                        entry["start_line"] = c["start_line"]
                        entry["start_side"] = c["start_side"]
                elif "position" in c:
                    # Legacy position-based comments (single-line only)
                    entry["position"] = c["position"]
                else:
                    self._send_json(400, {"error": f"comment missing line/side or position: {c}"})
                    return
                api_comments.append(entry)
            payload = {
                "commit_id": ARGS.sha,
                "event": "COMMENT",
                "body": summary,
                "comments": api_comments,
            }
            throttle_write()
            out, err, rc = run_gh(
                [
                    "api",
                    f"repos/{ARGS.repo}/pulls/{ARGS.pr}/reviews",
                    "--method", "POST",
                    "--input", "-",
                ],
                input_data=json.dumps(payload),
            )
            if rc != 0:
                msg = err or out
                if is_secondary_rate_limit(msg):
                    self._send_json(429, {
                        "ok": False,
                        "error": msg,
                        "retryable": True,
                        "rate_limited": True,
                        "retry_after_seconds": 60,
                        "hint": "GitHub secondary rate limit. Wait ~1 min before retrying.",
                    })
                    return
                self._send_json(500, {"error": msg, "payload": payload})
                return
            try:
                response = json.loads(out)
            except json.JSONDecodeError:
                self._send_json(500, {"error": "gh returned non-JSON", "raw": out})
                return
            self._send_json(200, {
                "ok": True,
                "url": response.get("html_url"),
                "id": response.get("id"),
                "count": len(comments),
            })
            return

        if self.path == "/api/post-comment":
            # Single inline comment, posted immediately (not batched in a review).
            # Maps to: POST /repos/{owner}/{repo}/pulls/{num}/comments
            required = ("path", "body", "line", "side")
            for k in required:
                if k not in body:
                    self._send_json(400, {"error": f"missing field: {k}"})
                    return
            payload = {
                "commit_id": ARGS.sha,
                "path": body["path"],
                "body": body["body"],
                "line": body["line"],
                "side": body["side"],
            }
            if body.get("start_line") and body.get("start_side"):
                payload["start_line"] = body["start_line"]
                payload["start_side"] = body["start_side"]
            throttle_write()
            out, err, rc = run_gh(
                [
                    "api",
                    f"repos/{ARGS.repo}/pulls/{ARGS.pr}/comments",
                    "--method", "POST",
                    "--input", "-",
                ],
                input_data=json.dumps(payload),
            )
            if rc != 0:
                msg = err or out
                if is_secondary_rate_limit(msg):
                    self._send_json(429, {
                        "ok": False,
                        "error": msg,
                        "retryable": True,
                        "rate_limited": True,
                        "retry_after_seconds": 60,
                        "hint": "GitHub secondary rate limit. Switch to Batch mode or wait ~1 min.",
                    })
                    return
                self._send_json(500, {"error": msg, "payload": payload})
                return
            try:
                response = json.loads(out)
            except json.JSONDecodeError:
                self._send_json(500, {"error": "gh returned non-JSON", "raw": out})
                return
            self._send_json(200, {
                "ok": True,
                "url": response.get("html_url"),
                "id": response.get("id"),
            })
            return

        if self.path == "/api/post-briefs":
            briefs = body.get("briefs", [])
            if not briefs:
                self._send_json(400, {"error": "no briefs to post"})
                return
            payload = {
                "commit_id": ARGS.sha,
                "event": "COMMENT",
                "comments": [
                    {"path": b["path"], "position": 1, "body": b["body"]}
                    for b in briefs
                ],
            }
            throttle_write()
            out, err, rc = run_gh(
                [
                    "api",
                    f"repos/{ARGS.repo}/pulls/{ARGS.pr}/reviews",
                    "--method", "POST",
                    "--input", "-",
                ],
                input_data=json.dumps(payload),
            )
            if rc != 0:
                msg = err or out
                if is_secondary_rate_limit(msg):
                    self._send_json(429, {
                        "ok": False,
                        "error": msg,
                        "retryable": True,
                        "rate_limited": True,
                        "retry_after_seconds": 60,
                        "hint": "GitHub secondary rate limit. Wait ~1 min before retrying.",
                    })
                    return
                self._send_json(500, {"error": msg})
                return
            try:
                response = json.loads(out)
            except json.JSONDecodeError:
                self._send_json(500, {"error": "gh returned non-JSON", "raw": out})
                return
            self._send_json(200, {
                "ok": True,
                "url": response.get("html_url"),
                "count": len(briefs),
            })
            return

        if self.path == "/api/chat":
            self._handle_chat(body)
            return

        if self.path == "/api/chat-cancel":
            self._handle_chat_cancel()
            return

        self._send_json(404, {"error": "unknown endpoint"})

    # --- inline chat (streaming) ---------------------------------------------

    def _start_ndjson_stream(self, status=200):
        """Open a streaming response. No Content-Length, no keep-alive."""
        self.send_response(status)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _stream_event(self, obj):
        """Write one stream-json event. Returns False if the client hung up."""
        try:
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _handle_chat(self, body):
        global _current_chat_proc
        if not ARGS.session_id:
            self._send_json(400, {
                "error": "chat disabled — no session id captured",
                "hint": "Re-run the pr-brief skill to capture a session.",
            })
            return
        message = (body.get("message") or "").strip()
        snippet = body.get("snippet")
        if not message:
            self._send_json(400, {"error": "empty message"})
            return

        if snippet and isinstance(snippet, dict):
            # Build a Markdown preamble naming the snippet location, so the
            # resumed session knows what the reviewer is asking about without
            # us having to re-attach the diff.
            path = snippet.get("path", "?")
            sl = snippet.get("start_line")
            el = snippet.get("end_line")
            code = snippet.get("code", "")
            range_label = f"{sl}" if sl == el or not el else f"{sl}-{el}"
            text = (
                f"> Looking at **`{path}:{range_label}`** in this PR.\n\n"
                f"```\n{code}\n```\n\n"
                f"{message}"
            )
        else:
            text = message

        if not _chat_lock.acquire(blocking=False):
            self._send_json(409, {
                "error": "another chat turn is in flight",
                "hint": "Wait for the current stream to finish (or hit Stop).",
            })
            return

        try:
            args = [
                "claude",
                "--output-format=stream-json",
                "--verbose",
                "--dangerously-skip-permissions",
                "--resume", ARGS.session_id,
                "-p", text,
            ]
            cwd = ARGS.session_cwd or os.getcwd()
            env = {**os.environ, "FORCE_COLOR": "0", "CLICOLOR": "0"}

            self._start_ndjson_stream(200)
            self._stream_event({"type": "chat_started"})

            try:
                proc = subprocess.Popen(
                    args,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=1,
                    text=True,
                    env=env,
                )
            except FileNotFoundError:
                self._stream_event({"type": "error", "message": "`claude` CLI not found on PATH"})
                return

            with _chat_proc_lock:
                _current_chat_proc = proc

            try:
                # Read stream-json one line at a time and forward.
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    line = line.rstrip("\n")
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        # Forward as a raw text event so the UI can show *something*.
                        if not self._stream_event({"type": "raw", "text": line}):
                            break
                        continue
                    if not self._stream_event(ev):
                        # Client disconnected — kill the subprocess.
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        break
                proc.wait(timeout=2)
            finally:
                with _chat_proc_lock:
                    _current_chat_proc = None
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            stderr_tail = ""
            if proc.stderr:
                try:
                    stderr_tail = proc.stderr.read() or ""
                except Exception:
                    stderr_tail = ""
            if proc.returncode not in (0, -signal.SIGTERM, -signal.SIGINT) and proc.returncode is not None:
                self._stream_event({
                    "type": "error",
                    "message": f"claude exited {proc.returncode}: {stderr_tail.strip()[-400:]}",
                })
            self._stream_event({"type": "chat_done", "code": proc.returncode})
        finally:
            _chat_lock.release()

    def _handle_chat_cancel(self):
        with _chat_proc_lock:
            proc = _current_chat_proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            self._send_json(200, {"ok": True, "cancelled": True})
        else:
            self._send_json(200, {"ok": True, "cancelled": False, "note": "no chat in flight"})


def main():
    global ARGS
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7681)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--sha", required=True)
    parser.add_argument(
        "--session-id",
        default=None,
        help="Claude Code session id to resume for the inline chat panel. "
             "When set, /api/chat-context reports enabled and the right-click "
             "'Chat about this' menu lights up in the UI.",
    )
    parser.add_argument(
        "--session-cwd",
        default=None,
        help="Working directory to spawn `claude --resume` in. Defaults to the "
             "server's cwd, which is usually wrong — pass the original project "
             "directory the skill ran in so the resumed session can resolve "
             "file paths from its history.",
    )
    ARGS = parser.parse_args()
    # Treat empty strings (from shell ${SID:+...} expansions that fell through) as None.
    if not ARGS.session_id:
        ARGS.session_id = None
    if not ARGS.session_cwd:
        ARGS.session_cwd = None

    server = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
    print(f"[server] pr-brief review UI on http://localhost:{ARGS.port}", flush=True)
    print(f"[server] PR {ARGS.repo}#{ARGS.pr} @ {ARGS.sha[:10]}", flush=True)
    if ARGS.session_id:
        print(f"[server] chat enabled (resume session {ARGS.session_id[:8]}…)", flush=True)
    else:
        print("[server] chat disabled (no --session-id)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[server] shutting down", flush=True)


if __name__ == "__main__":
    main()
