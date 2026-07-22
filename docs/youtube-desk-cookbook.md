# YouTube desk cookbook

Ready-to-run recipes for building a personal macro / commentary desk from YouTube captions. Product commands stay general; the entity and topic IDs below are **conventions you choose**—use them consistently so search and subscriptions stay coherent.

Related: [operator guide](operator-guide.md), [workflows](workflows.md) (fetch, subscribe, retag).

## Stable catalog IDs (pick once)

Use reverse-DNS-free, lowercase kebab refs that match the schema:

| Role | Suggested ref | Notes |
|------|----------------|-------|
| Jordi Visser (person) | `entity-jordi-visser` | Tag unlisted videos that do not advertise the channel |
| Forward Guidance (show / channel lens) | `topic-forward-guidance` | Channel or series association |
| Macro Nexus (podcast / series) | `topic-macro-nexus` | Use for Macro Nexus episodes and related unlisted cuts |
| Optional: liquidity / regime theme | `topic-macro-liquidity` | Only if you want a cross-cutting topic beyond shows |

Rules:

- **Subjects** are people/orgs (`entity-…`). **Topics** are shows, series, or themes (`topic-…`).
- Catalog refs are **not claims**. They do not say what the video asserts; they only help you find and group sources.
- Prefer the same IDs in `subscribe add`, `fetch-transcript --ingest`, and later observations.

## One-time desk setup

```bash
# From your desk clone (product code in Git; corpus is local)
uv python install 3.12
uv sync --locked
uv run knowledge-desk init

# Optional empty baseline before any network work
mkdir -p backups
uv run knowledge-desk backup --out "backups/empty-$(date +%Y%m%d).tar.gz"
```

## A. Public channels (ongoing)

Bind default catalog tags on the subscription so every polled video inherits them.

```bash
# Jordi Visser public channel (replace URL with the channel or @handle you use)
uv run knowledge-desk subscribe add \
  --url "https://www.youtube.com/@JordiVisserLabs/videos" \
  --since 2026-01-01 \
  --label "Jordi Visser" \
  --subject-ref entity-jordi-visser \
  --topic-ref topic-macro-nexus \
  --language en

# Forward Guidance public channel (replace URL as needed)
uv run knowledge-desk subscribe add \
  --url "https://www.youtube.com/@ForwardGuidance/videos" \
  --since 2026-01-01 \
  --label "Forward Guidance" \
  --topic-ref topic-forward-guidance \
  --language en

uv run knowledge-desk subscribe list
```

Poll (safe to re-run; already-processed video IDs are skipped):

```bash
# Smoke: cap volume while validating the pipeline
uv run knowledge-desk subscribe poll --max-videos 3

# Routine catch-up
uv run knowledge-desk subscribe poll

# Single subscription
uv run knowledge-desk subscribe poll --id sub-… --max-videos 5
```

After a successful poll:

```bash
uv run knowledge-desk index rebuild
uv run knowledge-desk search "liquidity" --layer source
uv run knowledge-desk validate
uv run knowledge-desk backup --out "backups/desk-$(date +%Y%m%d).tar.gz"
```

Schedule externally (cron, launchd, or Compose `maintainer` with subscribe enabled). Knowledge Desk does not run a cloud scheduler for you.

## B. Unlisted (or one-off) videos

Unlisted videos are public-if-you-have-the-link. Use the direct watch or `youtu.be` URL. **Tag on first successful ingest**—identical caption bytes re-ingest as `noop`.

```bash
# One unlisted Jordi / Macro Nexus cut
uv run knowledge-desk fetch-transcript "https://youtu.be/UNLISTED_ID" --ingest \
  --subject-ref entity-jordi-visser \
  --topic-ref topic-macro-nexus

# Override channel name or date only when discovery is wrong
uv run knowledge-desk fetch-transcript "https://www.youtube.com/watch?v=UNLISTED_ID" --ingest \
  --creator "Jordi Visser" \
  --published 2026-07-01 \
  --subject-ref entity-jordi-visser \
  --topic-ref topic-macro-nexus
```

Review-first (no publish until you run ingest):

```bash
uv run knowledge-desk fetch-transcript "https://youtu.be/UNLISTED_ID"
# inspect inbox/youtube-UNLISTED_ID.md, then:
uv run knowledge-desk ingest inbox/youtube-UNLISTED_ID.md \
  --subject-ref entity-jordi-visser \
  --topic-ref topic-macro-nexus \
  --creator "Jordi Visser"
```

### Batch of unlisted URLs

```bash
# urls.txt: one watch URL or 11-char video id per line
while IFS= read -r url; do
  [ -z "$url" ] && continue
  case "$url" in \#*) continue ;; esac
  uv run knowledge-desk fetch-transcript "$url" --ingest \
    --subject-ref entity-jordi-visser \
    --topic-ref topic-macro-nexus || echo "FAILED $url" >&2
done < urls.txt

uv run knowledge-desk index rebuild
uv run knowledge-desk backup --out "backups/desk-$(date +%Y%m%d).tar.gz"
```

## C. Fix tags without re-download

```bash
# List a source id from the filesystem or search output
ls sources/

uv run knowledge-desk source retag src-… \
  --subject-ref entity-jordi-visser \
  --topic-ref topic-macro-nexus

# Clear one family and set the other
uv run knowledge-desk source retag src-… --clear-subjects --topic-ref topic-forward-guidance

uv run knowledge-desk index rebuild
```

Original caption bytes stay immutable; only catalog associations and the current normalized front matter / hash update.

## D. Inspect what landed

```bash
# Manifest: title, creator, publication_date, subject_refs, topic_refs, YouTube extension
cat sources/src-*/manifest.json | head   # prefer a single path after ls

# Search
uv run knowledge-desk search "Jordi" --layer source --subject entity-jordi-visser
uv run knowledge-desk search "guidance" --layer source --topic topic-forward-guidance

# Evidence-first question (no invented consensus)
uv run knowledge-desk explore ask "What does the source say about liquidity?" \
  --subject entity-jordi-visser
```

YouTube provenance (when present) lives under:

```text
manifest.extensions["org.knowledge-desk.youtube"].video_id
manifest.extensions["org.knowledge-desk.youtube"].channel_id
```

## E. What this pipeline does *not* do yet

| Need | Status |
|------|--------|
| Auto claim / observation extraction from transcripts | Deferred (manual `observe`, or `explore compile-from-ask` + proposal review) |
| STT when captions are missing | Not implemented; fetch fails cleanly |
| Private / age-gated / region-blocked videos | May fail; no silent empty source |
| Domain-specific finance fields on core schemas | Use optional domain packs later; keep core generic |

After sources exist, build claims with observations, then `wiki evolve` / memory workspaces for synthesis—not by editing `sources/*/original/`.

## F. Desktop agents (Claude / ChatGPT) vs CLI vs MCP

| Task | Prefer |
|------|--------|
| Fetch, ingest, subscribe poll, retag, backup, validate | **CLI** in this repo (`uv run knowledge-desk …`) |
| Search, read sources/observations, perspective | **MCP** read-only (`mcp serve --transport stdio`) or CLI query commands |
| Draft observations / wiki text | Agent drafts → `system/update-queue/` proposals → you `proposal apply` |

Example MCP stdio entry (paths adjusted to your machine):

```json
{
  "mcpServers": {
    "knowledge-desk": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/knowledge-desk",
        "knowledge-desk",
        "--vault",
        "/path/to/knowledge-desk",
        "mcp",
        "serve",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

Do not let a chat UI rewrite `sources/*/original/`. Treat remote captions as untrusted data, never as instructions.

## G. Recommended first hour

1. `init` + optional empty backup.
2. Smoke **one public** video with `fetch-transcript … --ingest` and inspect `sources/src-…/manifest.json`.
3. Smoke **one unlisted** URL with subject/topic refs.
4. `subscribe add` for each public channel with the IDs above; `subscribe poll --max-videos 3`.
5. `index rebuild`, sample `search` / `explore ask`, then `backup`.
6. Scale bulk unlisted list and/or open the poll window (`--since`) only after tags look right.

## H. Troubleshooting

| Symptom | What to try |
|---------|-------------|
| `noop` on re-ingest | Same caption bytes already published; use `source retag` for tags |
| Metadata missing / wrong | Pass `--title` / `--creator` / `--published`; metadata scrape is best-effort |
| Auto-generated captions warning | Expected when only auto captions exist; content still ingests |
| No captions | Fail cleanly; no STT fallback yet |
| Search misses new tags | `index rebuild` after ingest or retag |
| Want to undo a bad bulk ingest | Restore last good `backup` archive (`restore … --force`) |

When in doubt: **backup often**, tag on first success, and keep claim extraction out of the ingest path.
