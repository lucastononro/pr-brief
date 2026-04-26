---
name: pr-brief
allowed-tools: Bash(gh pr view:*), Bash(gh pr diff:*), Bash(gh pr list:*), Bash(gh api:*), Bash(gh auth:*), Bash(gh repo view:*), Bash(python3:*), Bash(open:*), Bash(mkdir:*), Bash(cp:*), Bash(find:*), Bash(lsof:*), Bash(kill:*), Read, Write, Glob, Grep, Task, TodoWrite
description: Interactive PR review. Pulls a PR via gh, groups files into features (markdown descriptions), computes a suggested review sequence, and launches a local HTML UI with GitHub-style stacked diffs, inline commenting (click-and-drag multi-line), realtime posting via gh, and an AI **narrative layer** of "explain pills" — purple ✨ callouts that tell the story of the PR alongside the code, connecting each block to the broader change. Triggers on: review this pr, brief pr, pr review ui, narrate pr, annotate pr, interactive pr review.
---

# PR Brief

Launch an interactive, browser-based review workflow for a pull request.

**What the user sees:** a local webpage with an ordered review sequence in the sidebar, and — when a feature is clicked — every file in that feature stacked top-to-bottom (GitHub "Files Changed"-style) with syntax-highlighted diffs, per-line inline commenting, and a parallel **narrative layer** of AI-authored "explain pills" (purple ✨ callouts) on the right that tell the story of the change. Each saved comment is **posted to GitHub immediately** (realtime — no batch submit step).

**Pipeline:**
1. Fetch PR via `gh`
2. Opus agent groups every changed file into a feature, writes a **markdown** description, suggests a review order, and authors the **narrative explain-pill set** (1–3 markdown callouts per non-trivial file, planned together as one connected story across the sequence)
3. Build `data.json` (PR meta + features + sequence + per-file unified diff with pre-computed GitHub `position` values + resolved explain-pill positions)
4. Copy the HTML + Python server templates to a per-PR output dir
5. Launch the server; open the browser
6. The user reviews, leaves inline comments, each save POSTs immediately via `gh api`

---

## Agent assumptions (applies to every agent and subagent)

- All tools are functional and will work without error. Do not test tools or make exploratory calls.
- Only call a tool when required for the task. Every call needs a clear purpose.
- Never modify the target repo's working tree. Never commit, never push.
- All generated artifacts live under `~/.claude/pr-review/pr-<num>/` — never inside the repo being reviewed.
- **Always use opus for subagents in this skill.** Do not use haiku or sonnet; opus makes fewer mistakes on diff analysis and feature grouping, which is load-bearing.

---

## Inputs

The user passes a PR identifier. Accept any of:
- `123` — PR number, current repo (resolve via `gh repo view --json nameWithOwner -q .nameWithOwner`)
- `owner/repo#123`
- `https://github.com/owner/repo/pull/123`

If absent, use the current branch's open PR: `gh pr view --json number,headRepository`.

---

## Steps

### 1. Preflight

```bash
gh auth status || echo "NOT_AUTHED"
```

If not authenticated, stop and tell the user: "Run `gh auth login` in a terminal, then re-invoke this skill."

```bash
mkdir -p ~/.claude/pr-review/pr-<num>
```

### 2. Fetch PR

```bash
gh pr view <num> --repo <owner>/<repo> \
  --json number,title,body,author,headRefOid,headRefName,baseRefName,url,additions,deletions,changedFiles \
  > ~/.claude/pr-review/pr-<num>/pr.json

gh pr diff <num> --repo <owner>/<repo> > ~/.claude/pr-review/pr-<num>/pr.diff
gh pr diff <num> --repo <owner>/<repo> --name-only > ~/.claude/pr-review/pr-<num>/files.txt
```

Capture `headRefOid` — this is the `sha` you'll pass to the server (every comment posted needs it).

### 3. Feature discovery + sequencing (opus agent)

Launch **one opus agent** with:
- The file list (`files.txt`)
- The full PR diff (`pr.diff`) if <500 KB, else the first 1500 lines
- PR title and body (author intent)
- Output path: `~/.claude/pr-review/pr-<num>/features.json`

Instruct the agent to:

1. Group every file into a single `feature` bucket. A feature is a kebab-case change theme (1-3 words): `ownership-guards`, `asgi-middleware`, `group-conversations`, `migrations`, `documentation`, etc. Group by *change theme*, not directory. A file with multiple themes joins them with `+` (e.g. `sse-stability + inbox-ux`).

2. For each feature, produce:
   - `tldr` — one-line plain-text what + why (≤120 chars; **no markdown**, the UI renders this as plain text)
   - `full_description` — **MARKDOWN-formatted, 2-4 short paragraphs or a bulleted list**. The frontend renders this through `marked` (GitHub-flavored markdown). Use:
     - `**bold**` for key terms (file paths, function names, important nouns)
     - `- item` bullet points for lists of changes (preferred over long sentences when there are 3+ discrete points)
     - `` `inline code` `` for symbols, file paths, flags
     - Short paragraphs — no walls of text
     - Do NOT use h1/h2 headings (the description already lives under one) — h3 is OK if needed
   - `blast_radius` — `high` (schema/auth/migrations/breaking), `medium` (behavioral/endpoint changes/SSE), or `low` (docs/tests/refactors/lockfiles)
   - `why_first` — one-line plain-text rationale for sequence position

3. Order features into a `sequence` list. Heuristic:
   - Foundation first: migrations, schemas, core modules, shared utilities, types
   - Domain/services next: business logic, repositories
   - Entry points: controllers, API endpoints, middleware
   - UI: components, stores, pages
   - Last: docs, tests, build/ci
   - Within each layer: higher blast_radius first

4. Emit `edges` — pairs `["fromFeature", "toFeature"]` for "understanding A helps read B". Examples: `["migrations","ai-enabled-flag"]`, `["ownership-guards","controllers-using-ownership"]`. ~5-12 edges total.

5. **Per-file briefs (required for non-trivial files).** For every file, produce:
   - `tldr` — one-line plain-text what + why for *this file specifically* (≤120 chars; **no markdown**). Distinct from the feature-level `tldr`: that one summarizes the whole feature; this one summarizes the file's role within it. Example feature tldr: "Schema updates for group conversations". Example file tldr for `V0079_*.py`: "Adds `is_group` and `group_jid` columns to `conversation_assignments`".
   - `description` — **markdown**, 1-3 short sentences or a small bullet list. Mini-PR-description for the file: what changed + why this file in particular needs to change. Use `` `inline code` `` for symbols, `**bold**` for the key noun. No headings. The UI renders this through `marked` and shows it in a header card directly above each file's diff (so a reviewer sees the file's purpose before scanning the diff itself).

   Skip both fields (or set to empty strings) only for: lockfiles, pure renames with no content changes, deleted-file shells, or auto-generated artifacts. For these, the feature-level brief is sufficient.

   Per-file `tldr` + `description` are distinct from explain pills: the brief sits at the *top* of the file as a header; pills annotate *specific line ranges* inline. The brief answers "why am I about to look at this file?" — pills answer "why is this block written this way?".

6. **Explain pills — the narrative layer.** This is the most important output of the agent after the feature grouping. Explain pills are AI-authored callouts that **tell the story of the PR**. They render as floating purple ✨ boxes next to the diff, and a reviewer reading them in `sequence` order should leave understanding *the whole change*, not just isolated snippets.

   Treat the pill set as one connected narrative arc, not a pile of independent annotations.

   **Step 6a — Plan the narrative (do this BEFORE writing any pill).**
   Walk through the `sequence` you produced. For each feature, ask:
   - What is this feature *introducing* into the codebase? (a new column, a new dependency, a new contract, a refactor, a fix)
   - What does the *next* feature in the sequence build on top of it?
   - For each file in this feature: what role does it play? (defines / publishes / consumes / wires-up / tests / migrates)
   - Where is the *seam* between files — where does feature A's output become feature B's input?
   Hold this map in mind while writing pills. Pills should reinforce that map, not restate the diff.

   **Step 6b — Per file, produce 1–3 pills.**
   - Skip entirely (no pills): lockfiles, simple doc-only changes, single-line tweaks, pure renames, deleted-file shells.
   - Pick line ranges that map to a coherent unit: a function body, a key `if`/`try` block, a config dict, a SQL statement, a state machine. Avoid trivial 1-line spans unless that one line *is* the point.

   **Step 6c — Each pill must do at least three of these four jobs.**
   1. **What** the block does — in plain language, not a re-read of the code.
   2. **Why** this approach: name the constraint, the prior failure, the tradeoff, the security/performance concern, the upstream contract that forced it.
   3. **Where it connects** — name a *specific* call site, consumer, or producer by `path:symbol`. Examples: "Called by `inbox_service.get_conversations`", "Reads the `ai_enabled` column written in `V0077_*.py`", "Consumed by the frontend store at `src/stores/inbox.ts:streamInbox()`". This is the connective tissue that makes pills feel like a story.
   4. **How it fits the feature** — one short clause tying back to the feature's `tldr`. Examples: "This is the producer side of the group-conversations seam", "Closes the SSE retry loop the middleware enabled".

   **Step 6d — Voice and form.**
   - Markdown body. **2–4 short sentences** or a small bullet list. Never a wall of text.
   - Use `` `inline code` `` for symbols, files, flags, env vars.
   - Use `**bold**` for the key noun (the function, the column, the concept).
   - First sentence should hook a reviewer reading in sequence: assume they've read previous pills.
   - No fluff ("This code…", "Here we…"). Lead with the verb or the noun.
   - Title: 4–8 words. Concrete and specific. *Not* "Migration code" — *yes* "Idempotent V0077 backfill".

   **Step 6e — Connect across the sequence.**
   - The first pill in a feature should briefly ground the reader: this feature's job in one clause.
   - Mid-feature pills should reference earlier features by name when relevant ("uses the ownership dep added in `ownership-guards`").
   - The last pill in a feature should hand off forward when something downstream depends on it ("set up here, consumed by `inbox-controller` next").
   - Don't force these — only when the connection is real.

   **Fields:**
   - `title` — 4–8 words, plain text.
   - `body` — markdown per Step 6d.
   - `start_line` — first line number of the range, in the **new** file (post-change line numbers from the diff).
   - `end_line` — last line number of the range (== start_line for single-line).
   - `side` — `"RIGHT"` for new/added/context lines (default); `"LEFT"` only when the explanation is specifically about removed code.
   - Do NOT compute `position` here — `build_data.py` resolves it from `(start_line, end_line, side)` against the parsed diff.

   **Anti-patterns to avoid:**
   - ❌ Restating the diff: "Adds an if check that returns 403."
   - ❌ Generic description: "This function handles authorization."
   - ❌ No connection: a pill that could be in any PR — make it *this* PR's pill.
   - ❌ Long prose. If it doesn't fit in 4 sentences, it's the wrong unit.

   **Examples of good pills (showing connectivity):**

   ```json
   {
     "title": "ai_enabled column lands first",
     "body": "Adds the **`ai_enabled`** boolean to `conversation_assignments`. This is the schema seam the rest of `ai-enabled-flag` builds on — `session_management.py` reads it via Redis cache (next pill), and the controller flips it through `PATCH /inbox/.../ai`.\n\nIdempotent because Cloud Run may rerun the migration on cold start."
   }
   ```

   ```json
   {
     "title": "Cache layer in front of Postgres",
     "body": "Reads **`ai_enabled`** from Redis with a TTL fallback to the column added in the prior migration. Cuts the per-message DB hit on the hot WhatsApp path.\n\nWritten by the inbox controller's toggle endpoint; cache key is `ai:{phone_group_key}` to share state across group sessions."
   }
   ```

   ```json
   {
     "title": "Pure ASGI to unblock SSE",
     "body": "Switched from Starlette's `BaseHTTPMiddleware` to raw ASGI because the former buffers `StreamingResponse` bodies through an asyncio queue.\n\n**Why now:** `inbox_service.stream_conversations` (next file) emits SSE — the buffer would have collapsed all events into a single chunk, which is the bug `sse-stability` fixes."
   }
   ```

**Output:** Write `~/.claude/pr-review/pr-<num>/features.json` matching exactly this schema:

```json
{
  "features": {
    "<feature-name>": {
      "tldr": "Plain-text one-liner",
      "full_description": "**Markdown** with `code` and:\n- bullet one\n- bullet two\n\nA closing paragraph.",
      "blast_radius": "high|medium|low",
      "why_first": "Plain-text rationale",
      "files": [
        {
          "path": "path/to/file.ext",
          "tldr": "Plain-text one-liner about THIS file",
          "description": "**Markdown** mini-brief. 1-3 short sentences or a small bullet list — what changed in this file and why it had to.",
          "explanations": [
            {
              "title": "Short title (plain text, 4-8 words)",
              "body": "**Markdown** body. 1-3 short sentences or a small bullet list explaining *why* this code is here.",
              "start_line": 42,
              "end_line": 56,
              "side": "RIGHT"
            }
          ]
        }
      ]
    }
  },
  "sequence": ["<feature-name>", ...],
  "edges": [["<from>", "<to>"], ...]
}
```

**Hard rules:**
- Every path in `files.txt` appears in exactly one feature. No duplicates, no orphans.
- `sequence` covers every feature.
- `full_description` is markdown; `tldr` and `why_first` are plain text.
- Valid JSON (parseable by `json.load`).

### 4. Build `data.json`

This step combines `pr.json`, `features.json`, and `pr.diff` into the single file the HTML reads. Do this yourself (not via agent) — it's mechanical. Use a small Python script with stdlib only:

For each file, extract its unified diff from `pr.diff` (split on `^diff --git a/`). Parse the diff into an array of line objects with pre-computed GitHub `position` values.

**Position rules (load-bearing — GitHub's API is strict):**
- `position` is 1-indexed. The first line after the file's header (i.e. the first `@@` hunk header) is **position 1**.
- Every subsequent line (hunk headers, context, `+`, `-`) increments position by 1.
- Position resets when a new file's diff starts.
- `\ No newline at end of file` markers do NOT increment position; skip them.

**Per-line objects:**

```json
{ "position": 1, "type": "hunk", "content": "@@ -1,5 +1,10 @@" }
{ "position": 2, "type": "context", "content": " foo", "old_line": 1, "new_line": 1 }
{ "position": 3, "type": "add", "content": "+bar", "new_line": 2 }
{ "position": 4, "type": "del", "content": "-baz", "old_line": 2 }
```

Track `old_line` / `new_line` per file: context increments both; `+` increments new only; `-` increments old only. Hunk header resets both via `@@ -A,B +C,D @@`.

Binary / rename-only files: `lines: []`.

**Resolve explanations:** for each explanation in a file's `explanations` array, look up its `start_position` and `end_position` from the parsed lines:
- For `side: "RIGHT"`: find the line where `(type == "context" or type == "add") and new_line == target_line` — its `position` is what you want.
- For `side: "LEFT"`: find the line where `(type == "context" or type == "del") and old_line == target_line`.
- If a target line isn't found in the diff (e.g. agent picked an out-of-diff line), drop that explanation with a warning.
- Pass the augmented `explanations` array through to the file entry in data.json.
- Pass the file-level `tldr` and `description` (from `features.json`) straight through to the file entry in data.json — the frontend renders them as a header card above each file's diff.

**Final `data.json` shape:**

```json
{
  "pr": {
    "number": 969,
    "title": "...",
    "url": "https://github.com/.../pull/969",
    "head_sha": "0468e2ed...",
    "base": "main",
    "author": "alice",
    "additions": 7220,
    "deletions": 120
  },
  "sequence": ["migrations", "ownership-guards", ...],
  "edges": [["migrations", "ownership-guards"], ...],
  "features": {
    "migrations": {
      "tldr": "Schema updates for group conversations and ai_enabled column",
      "full_description": "Adds two new columns:\n- **`is_group`** ...\n- **`group_jid`** ...",
      "blast_radius": "high",
      "why_first": "Schema lands before code that reads the new columns.",
      "files": [
        {
          "path": "backend-python/src/migrations/versions/V0079_....py",
          "tldr": "Adds `is_group` and `group_jid` columns to `conversation_assignments`.",
          "description": "Schema migration that introduces the two columns the rest of `group-conversations` depends on. Idempotent via `migration_tracking` so a Cloud Run cold start can replay it safely.",
          "additions": 45,
          "deletions": 2,
          "lines": [ ... ],
          "explanations": [
            {
              "title": "Idempotent migration guard",
              "body": "Checks `migration_tracking` before writing — makes the upgrade safe to re-run on Cloud Run cold starts.",
              "start_line": 22,
              "end_line": 35,
              "side": "RIGHT",
              "start_position": 5,
              "end_position": 18
            }
          ]
        }
      ]
    }
  }
}
```

Write to `~/.claude/pr-review/pr-<num>/data.json`.

### 5. Copy templates + launch

The skill ships `index.html` and `server.py` under its own `templates/` directory. The skill's location depends on how it was installed — standalone (`~/.claude/skills/pr-brief/`) or as a plugin (`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/pr-brief/`). Resolve the templates directory at runtime with `find`, then copy the files into the PR output dir each run (so improvements to the templates propagate):

```bash
TEMPLATES_DIR=$(find ~/.claude -type d -path "*/skills/pr-brief/templates" 2>/dev/null | head -1)
[ -z "$TEMPLATES_DIR" ] && { echo "pr-brief templates not found under ~/.claude"; exit 1; }
cp "$TEMPLATES_DIR/index.html" ~/.claude/pr-review/pr-<num>/index.html
cp "$TEMPLATES_DIR/server.py"  ~/.claude/pr-review/pr-<num>/server.py
```

Pick a free port. Default 7681; if `lsof -i:7681` is busy, try 7682, 7683, ... up to 7690.

```bash
cd ~/.claude/pr-review/pr-<num>
python3 server.py --port <port> --pr <num> --repo <owner>/<repo> --sha <head_sha> &
```

Background the server, then open the browser:

```bash
open "http://localhost:<port>"
```

### 6. Report

Output a compact summary to the terminal:

```
🟢 Review UI ready → http://localhost:7681

Suggested order (N features, M files):
  1. migrations        (6 files, high blast) — schema lands first
  2. ownership-guards  (13 files, high blast) — dep for controller changes
  3. ...

In the UI:
  • Click a feature → all its files render stacked, scroll through them top-to-bottom
  • Click "+" in the gutter → inline editor; click-and-drag the "+" or shift-click for multi-line
  • Save → posted to GitHub immediately (realtime); the badge flips Pending → Posted ✓ with link
  • "Viewed" checkbox per file (sticky, persists per PR)
  • If a post fails (network/auth), the comment stays local; "Retry N unposted" in the sidebar resends

Stop the server: lsof -ti:<port> | xargs kill
```

---

## UI behavior baked into the templates

These behaviors are part of the bundled `index.html` and `server.py`. **Do not regress** when modifying templates:

- **Stacked files (no tabs).** When a feature is selected, every file in it is rendered top-to-bottom with its own collapsible header. Scroll through them in order.
- **Sticky sidebar buttons.** The Submit/Clear/Publish-briefs row is pinned at the bottom of the sidebar regardless of how long the feature list grows. (Flex `flex: 1; min-height: 0; overflow-y: auto` on `.feature-list`; `flex-shrink: 0` on `.pending-box`.)
- **Per-file "Viewed" checkbox.** Persisted in `localStorage` keyed by `pr-brief-viewed-<repo>-<num>`. Marking a file viewed dims and collapses it.
- **GitHub-style syntax highlighting.** `highlight.js` 11.9 with the `github-dark` stylesheet. Line prefix (` ` / `+` / `-`) is colored separately so add/del row tints stay correct.
- **Markdown rendering of `full_description`.** Frontend uses `marked` (CDN). Bullet points, bold, inline code, and short paragraphs render as expected. The agent producing `features.json` MUST emit markdown for this field.
- **Explain pills (AI narrative layer).** Each file's diff has a 400px right-side track. For every entry in `file.explanations`, a purple "✨" callout floats anchored to the **start** row of its range (`data-start-position`), measured via `getBoundingClientRect()` against the track. Pills auto-stack (sorted by start position) to avoid overlap. After layout, `track.style.minHeight` is set to `lastBottom + 16px` so pills near EOF are not clipped. Pills are collapsible (toggle button), the body is markdown-rendered through `marked` and resizable (CSS `resize: vertical`). Rows in the explanation's range get a left-border accent (`box-shadow: inset 3px 0 0 #bb80ff`). Pill content is **narrative-driven** — see step 6 of the agent task for the storytelling rules.
- **Inline comment editor (no modal).** Hover a line → blue `+` in the gutter → click expands an editor row directly below. Cmd/Ctrl+Enter saves, Esc cancels.
- **Multi-line comments — three entry points:**
  1. **Click-and-drag** from the gutter `+` across lines (GitHub-native UX). Live blue band highlights the range.
  2. **Shift-click** a second `+` after a previous click.
  3. **Dropdown** in the editor header: "Single line / From L42 / From L41 / ..." (last 50 commentable lines).
- **Posting modes — Realtime / Batch (toggle in sidebar).** Default is Realtime: each Save POSTs `/api/post-comment` immediately, server shells `gh api repos/.../pulls/<num>/comments`. Switching to Batch makes Save just queue locally; clicking the sidebar submit button POSTs `/api/submit-review` once with all queued comments (single API call, sidesteps GitHub's secondary rate limit). Mode persists in `localStorage` per PR (`pr-brief-mode-<repo>-<num>`).
- **Server-side throttle.** All write endpoints (`/api/post-comment`, `/api/submit-review`, `/api/post-briefs`) gate behind a 1.5s minimum gap (per GitHub's "≥1s between writes" guidance) via a single threading lock — even with concurrent saves, the actual `gh` calls are serialized.
- **Secondary rate-limit detection.** If `gh` returns "secondary rate limit" output, the server replies HTTP 429 with `{ok:false, rate_limited:true, retry_after_seconds:60, hint}`. The frontend detects this, shows a "Rate limited — switching to Batch" toast, and auto-flips MODE to `batch`.
- **Submit button.** Disabled when nothing is unposted. Label depends on MODE: Realtime → `Retry N unposted` / `All posted`; Batch → `Submit N as one review` / `All posted`.

---

## Server endpoints (server.py)

Stdlib-only Python `http.server`:

- `GET /` and `/index.html` → static
- `GET /data.json` → static
- `GET /api/context` → `{pr, repo, sha}` for the UI
- `POST /api/auth-status` → runs `gh auth status`, returns `{ok, message}`
- `POST /api/post-comment` (**realtime path**) → body `{path, body, line, side, start_line?, start_side?}` → throttle 1.5s → `gh api repos/<repo>/pulls/<pr>/comments` → returns `{ok, url, id}`. On secondary rate limit returns 429 with `{ok:false, rate_limited:true, retry_after_seconds, hint}`.
- `POST /api/submit-review` (**batch path**) → body `{comments: [...], summary}` → throttle 1.5s → `gh api repos/<repo>/pulls/<pr>/reviews` (event=COMMENT) → returns `{ok, url, id, count}`. Used by Batch-mode submit and "Publish briefs". Same 429 contract on rate limit.
- `POST /api/post-briefs` → posts feature briefs as `position: 1` comments per file via the reviews endpoint

All POST endpoints expect/emit JSON.

---

## Rules

- **Never commit. Never push. Never modify the target repo's working tree.** All artifacts go under `~/.claude/pr-review/pr-<num>/`.
- **Never add comment headers to source files in the repo.** The UI handles briefing — do not touch the code.
- **Use opus for the feature-discovery agent.** Diff grouping is load-bearing.
- **Every changed file must land in exactly one feature.** No orphans.
- **`full_description` must be markdown.** `tldr` and `why_first` are plain text.
- **Position counting is per-file and starts at 1.** Off-by-one = GitHub rejects the comment.
- **Lockfiles** (`uv.lock`, `package-lock.json`, `yarn.lock`, `poetry.lock`, `Pipfile.lock`) collapse into a `build` or `deps` feature; `tldr` = "Auto-generated lockfile update — no manual review needed", `lines: []`.
- **Large PRs (>200 files):** if the opus agent's response is too big, fall back to directory-based grouping (top-level dir = feature) and note this in the report.
- **Port conflict:** try 7681–7690; if all busy, tell the user.
- **Force-push mid-session:** `gh api` returns 422 with stale SHA — tell the user to re-run the skill.
- **Always overwrite the templates on launch** (resolve `TEMPLATES_DIR` via `find ~/.claude -type d -path "*/skills/pr-brief/templates"`, then `cp "$TEMPLATES_DIR"/* …`) so future template improvements propagate to existing per-PR dirs.

---

## Notes

- Output dir `~/.claude/pr-review/pr-<num>/` lets multiple PRs coexist.
- Pending comments and Viewed flags survive a browser reload (`localStorage` keyed per PR).
- `gh` CLI inherits the user's system auth — server just shells out, no token in the HTML.
- Frontend deps from CDN: `highlight.js + github-dark` (syntax), `marked` (description / explanation markdown). No build step.
