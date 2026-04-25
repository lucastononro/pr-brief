#!/usr/bin/env python3
"""Local HTTP server for pr-brief review UI.

Serves static files (index.html, data.json) + bridges to `gh` CLI via a
couple of POST endpoints. Stdlib-only, no pip install needed.
"""
import argparse
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ARGS = None
ROOT = Path(__file__).parent.resolve()


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
                self._send_json(500, {"error": err or out, "payload": payload})
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
                self._send_json(500, {"error": err or out, "payload": payload})
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
                self._send_json(500, {"error": err or out})
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

        self._send_json(404, {"error": "unknown endpoint"})


def main():
    global ARGS
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7681)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--sha", required=True)
    ARGS = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
    print(f"[server] pr-brief review UI on http://localhost:{ARGS.port}", flush=True)
    print(f"[server] PR {ARGS.repo}#{ARGS.pr} @ {ARGS.sha[:10]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[server] shutting down", flush=True)


if __name__ == "__main__":
    main()
