# Workflows

## Multi-desk clones, init, and backup

The Git repository is the **product** (code + schemas + docs). Each clone or worktree can hold a separate corpus under local data directories that are **gitignored**.

```bash
git clone git@github.com:tomluvoe/knowledge-desk.git my-desk
cd my-desk
uv sync --locked
uv run knowledge-desk init
uv run knowledge-desk ingest path/to/source.pdf
# durable data is local — commit code changes only, not sources/wiki/…

uv run knowledge-desk backup --out backups/my-desk.tar.gz
uv run knowledge-desk restore backups/my-desk.tar.gz --vault /path/to/other-desk --force
```

`init` creates empty data trees without overwriting existing files. `backup` takes the writer lock and archives a consistent snapshot of durable data only (not `.venv`, `.staging`, `.locks`; index only with `--include-index`). The output must be outside every archived root so an archive cannot recursively include itself.

`restore` preflights the archive contract and member paths, extracts into same-filesystem staging, validates the complete candidate desk, and then publishes whole durable roots with rollback on failure. It refuses non-empty data trees unless `--force`; a forced restore first writes a timestamped `knowledge-desk-pre-restore-*.tar.gz` recovery archive beside the desk and reports its path. Immutable originals are therefore replaced only as part of a validated whole-root swap, never overwritten in place. If rollback itself cannot complete, the command reports the preserved staging recovery path. Prefer encrypting archives out of band (e.g. GPG) if they contain private sources.

Migration from older checkouts that committed data dirs: keep local files, ensure `.gitignore` lists them, then `git rm -r --cached sources observations wiki memory inbox domains system/logs system/update-queue` and commit. Existing files remain on disk.

## Concurrency and single-writer

v0.1 serializes **canonical writes** through an exclusive, process-wide lock at `system/.locks/writer.lock` (gitignored). The lock is re-entrant within one writer thread so a multi-step operation can safely call ingestion or wiki compilation. Concurrent readers (`validate`, `lint`, `search`, MCP tools) do not take the lock. If a writer cannot acquire it within ~30s, or the platform cannot provide a real `flock`-style cross-process lock, the operation fails clearly rather than running without exclusion.

Mutation boundaries are explicit:

| Mutation | Classification | Publication rule |
|---|---|---|
| ingest / re-normalize | canonical evidence + append-only log | writer lock; stage and replace; immutable originals never rewritten |
| observe | canonical append-only record | writer lock; staged publication |
| wiki evolve and workspace page changes | canonical revisable Markdown | writer lock; same-directory synced atomic replacement |
| proposal apply/reject | canonical effects + review archive | one writer transaction; collision-safe archive publication |
| subscription integration | inbox fetch + canonical source/wiki + operational cursor | each video is one writer transaction; briefing and cursor cannot interleave with another writer |
| backup / restore | durable local snapshot and whole-root recovery | writer lock; verified archive; staged validation; rollback-capable root publication; forced restore creates a recovery archive |
| proposal creation, fetch-only inbox files, maintainer ledgers | review/operational data | synced publication; never treated as canonical evidence |
| index rebuild | disposable derived data | no canonical writer authority |

An interrupted subscription integration is retryable. The cursor advances only after source ingestion and atomic briefing publication succeed. If a later cursor write fails, the next poll reuses the content-addressed source as an ingest no-op and replaces the deterministic briefing path before advancing the cursor; it does not duplicate evidence.

## Ingest

Run `knowledge-desk ingest <file-or-directory>`. Directory ingestion is **non-recursive**, processes supported files in lexical order, **skips dotfiles**, and reports unsupported entries without corrupting successful records. Empty plain-text files still ingest but carry a warning. Supply known metadata explicitly; ingestion does not infer publication dates or creators from prose.

The operation hashes first, checks duplicates, extracts in staging, preserves the original bytes, writes the manifest and normalized note, validates them, publishes atomically, and appends an ingest-log event. Exit status is nonzero if any requested input fails. JSON output makes no-op, created, revision, and failed states distinguishable.

Normalized notes carry a manifest digest and adapter/version history. Ordinary re-ingestion of identical bytes remains a no-op. Run `knowledge-desk ingest <same-file> --renormalize` to explicitly append an immutable, auditable normalization revision and update the manifest's current pointer, even when the adapter output is byte-for-byte unchanged. Existing locator paths remain registered and resolvable. Older manifests without integrity history can be upgraded explicitly with the same command.

## Fetch web pages

```bash
uv run knowledge-desk fetch-page "https://example.com/article"
uv run knowledge-desk fetch-page "https://example.com/article" --out inbox/article.md
uv run knowledge-desk fetch-page "https://example.com/article" --ingest --title "Article title"
```

`fetch-page` is a **network-enabled** boundary (not used by plain `ingest`/`validate`). It accepts **http/https only**, applies timeout (default 30s), response size cap (default 5 MiB), and redirect limits, then extracts **main content** with locked `trafilatura` into reviewable Markdown under `inbox/` (header: canonical URL, final URL, fetch time, content-type). Prefer reviewing the inbox file, then `uv run knowledge-desk ingest inbox/web-….md --url "…"`. Use `--ingest` for one-shot publish.

Remote HTML is untrusted data—never executed. Scripts/styles/nav chrome are stripped by extraction heuristics; the result is cleaned text, not a full DOM archive. Empty extractions (paywall, CAPTCHA, non-article) fail clearly. Non-HTML content-types and undecodable bodies are rejected. Unit tests inject fake HTTP responses and never require live network. SPA JavaScript rendering and recursive crawls are out of scope.

## Fetch YouTube transcripts

```bash
uv run knowledge-desk fetch-transcript "https://www.youtube.com/watch?v=VIDEO_ID"
uv run knowledge-desk fetch-transcript "https://youtu.be/VIDEO_ID" --out inbox/talk.md
uv run knowledge-desk fetch-transcript "VIDEO_ID" --language en --ingest
```

`fetch-transcript` is a **network-enabled** boundary. It downloads captions via the locked `youtube-transcript-api` dependency, writes a plain Markdown file (default `inbox/youtube-<video-id>.md`) with a short metadata header and lightly formatted transcript lines (`[mm:ss] text` unless `--no-timestamps`), and does **not** publish under `sources/` unless `--ingest` is passed. Prefer reviewing the inbox file, then `uv run knowledge-desk ingest inbox/youtube-….md`.

Remote content is untrusted data, never instructions. Videos without usable captions fail cleanly with no partial canonical publish. Auto-generated captions are accepted with a warning. Private, blocked, or caption-less videos are operator errors, not silent empty sources. Title/channel are not scraped from the page; pass `--title` / `--creator` when known.

## YouTube channel / playlist subscriptions

```bash
uv run knowledge-desk subscribe add \
  --url "https://www.youtube.com/@JordiVisserLabs/videos" \
  --since 2026-01-01 \
  --label "Jordi Visser Labs" \
  --subject-ref entity-jordi-visser \
  --topic-ref topic-macro

uv run knowledge-desk subscribe add \
  --url "https://www.youtube.com/playlist?list=PL…" \
  --since 2026-01-01 \
  --label "Playlist name"

uv run knowledge-desk subscribe list
# one-shot poll (cron/systemd/timer or maintainer container)
uv run knowledge-desk subscribe poll
uv run knowledge-desk subscribe poll --id sub-… --max-videos 5
```

Subscriptions live under local `system/subscriptions/` (gitignored). Poll discovers videos via YouTube Atom feeds, keeps only items **on/after `--since`** and not already processed (long playlists do not bulk-download history), fetches transcripts, ingests them, and writes a **delta briefing** under `wiki/syntheses/` (new video + perspective timeline notes for the bound subject/topic). Scheduler is external: run `subscribe poll` on a cron, or use the maintainer worker (`knowledge-desk maintain loop` / Compose `maintainer` profile). LLM claim extraction remains a follow-up; briefings point operators to append observations with relations.

## Maintainer worker (unattended)

The maintainer is an **automated knowledge-desk agent**, not the interactive discuss/MCP surface. Manual CLI + MCP remain first-class without it.

```bash
# One maintenance cycle (local / cron)
uv run knowledge-desk maintain once
uv run knowledge-desk maintain once --steps inbox_ingest,wiki_evolve,lint --no-subscribe
uv run knowledge-desk maintain status

# Container loop (read-write vault mount)
docker compose --profile maintainer up --build maintainer
# or: KNOWLEDGE_DESK_MAINTAIN_INTERVAL=600 docker compose --profile maintainer up maintainer
```

Default steps (comma-separated via `--steps` or `KNOWLEDGE_DESK_MAINTAIN_STEPS`):

1. `inbox_ingest` — non-recursive ingest of `inbox/` (skips `README.md` / dotfiles)
2. `subscribe_poll` — YouTube poll when subscriptions exist
3. `wiki_evolve` — living wiki compile from observations
4. `lint` — vault + semantic lint (hard-fail only if vault invalid)
5. `index_rebuild` — disposable FTS index
6. `explore_gaps` — source-gap report; writes review-only proposals under `system/update-queue/`

Job state is durable under local `system/jobs/` (`ledger.jsonl`, `last-run.json`, `dead-letter.jsonl`). Cycles are idempotent: re-ingesting the same inbox bytes is a no-op; wiki evolve retains prior `observation_ids`. Secrets (future LLM providers) are runtime env only—never stored in the vault. Content-changing LLM extraction is **not** enabled by default; proposals stay reviewable until `proposal apply`.

## Living wiki compile

```bash
uv run knowledge-desk wiki evolve
uv run knowledge-desk wiki refine-validate
```

`wiki evolve` is mechanical (no invented LLM prose). From observations it updates:

| Kind | When |
|------|------|
| entity / topic | Always when subjects/topics are referenced |
| synthesis (source summary) | Per source that has observations |
| synthesis (cross-source) | Topic with evidence from ≥2 sources |
| comparison | ≥2 entities on a topic with differing orientations or `contradicts` relations |
| event | ≥2 observations sharing the same `valid_at` calendar day |

Pages keep **source-specific positions**, separate **consensus / disagreement / uncertainty**, carry an **as_of** stamp (`extensions.knowledge.desk.wiki.as_of`), and a **What changed** section. Prior `observation_ids` are retained on re-evolve so regeneration does not silently drop reviewed links. Wiki prose remains revisable synthesis, not immutable evidence.

## Observe

Run `knowledge-desk observe path/to/observation.json` after the cited sources exist. The document must match `observation.schema.json`, every evidence locator must resolve, and relation targets must already exist. Publication is append-only: a new `observation_id` is created, an identical payload is a no-op, and a conflicting payload for an existing ID is rejected. Later confirmations, refinements, contradictions, or supersessions are new observations that link via `relations`.

## Query observations

```bash
uv run knowledge-desk observations list
uv run knowledge-desk observations list --subject entity-example-wetland --topic amphibian
uv run knowledge-desk observations list --source-id src-…
uv run knowledge-desk observations get obs-20260718-frog-calls
uv run knowledge-desk observations relations
```

`list` ANDs filters (`--subject`, `--topic`, `--source-id`, `--orientation`, `--epistemic-class`, `--statement-basis`, `--id-prefix`). Subject and topic match `ref_id` exactly or as a case-insensitive substring of `ref_id`/`label`. Results are sorted by `valid_at` (then `expressed_at` / `recorded_at`) and `observation_id`. `relations` returns the outgoing confirms/contradicts/refines/supersedes graph.

## Perspective at a date

```bash
uv run knowledge-desk perspective at --subject entity-alpha --topic topic-outlook --as-of 2024-06-01
uv run knowledge-desk perspective timeline --subject entity-alpha --topic topic-outlook --from 2024-01-01 --to 2024-12-31
```

`perspective at` returns the supported view for a subject+topic as of a date or datetime:

- An observation applies only if its effective time (`valid_at`, else `expressed_at` / `publication_date` / `recorded_at`) is on or before `as_of` and any `horizon` still covers that day.
- Date-only `as_of` and timeline `--to` values include the complete day (including fractional timestamps in its final second); date-only `--from` starts at midnight. Offset timestamps are normalized to UTC before ordering.
- Superseded observations that still fall in range are dropped when a superseding observation also applies.
- No applying observation yields `status=unknown` and `reason=insufficient_evidence` — never a synthetic neutral stance.
- An observation whose orientation is `unknown` is still `supported` (explicit unknown ≠ missing evidence).
- Concurrent disagreeing active observations yield `status=conflicted` with the latest primary and `conflicting_observation_ids`.

`perspective timeline` lists introduced/confirms/refines/contradicts/supersedes events in time order. Each event's `relations` array preserves every material outgoing relation; `change` and `related_observation_id` remain a first-relation summary for compatibility.

```bash
uv run knowledge-desk perspective compare \
  --subject entity-alpha --subject entity-beta \
  --topic topic-outlook --as-of 2024-06-01
```

`perspective compare` places subjects side-by-side across explicit dimensions (orientation, assertion, statement_basis, mechanisms, risks, horizon, freshness, …). Agreement is per-dimension (`agree` / `disagree` / `insufficient`). There is **no** single opaque similarity score. Subjects without applying observations appear under `insufficient` with status `unknown`, never a synthetic neutral stance.

## Query and refine

Read normalized evidence by exact locator, then interpret observations, then consult wiki synthesis. Cite the most direct layer. A new claim becomes a new observation; never rewrite an old observation to make history cleaner. Wiki pages may be revised when their citations and `updated_at` metadata are updated.

```bash
uv run knowledge-desk proposal list
uv run knowledge-desk proposal apply system/update-queue/explore-ask-….json
uv run knowledge-desk proposal reject system/update-queue/….json --reason "not ready"
```

Proposals under `system/update-queue/` are review-only. `proposal apply` runs under the writer lock, may append observations or memory open questions when complete, and archives JSON under `system/update-queue/applied/` (or `rejected/`). Incomplete single `explore ask` observation stubs with `entity-todo` / `topic-todo` are skipped until edited; compile-from-ask batches use the stricter all-or-nothing behavior below.

## Source-gap exploration

```bash
uv run knowledge-desk explore gaps
uv run knowledge-desk explore gaps --topic amphibian --propose
uv run knowledge-desk explore ask "Where were frog calls recorded?"
uv run knowledge-desk explore ask "What is the capital of Mars?" --propose
```

`explore gaps` lists sources missing observation and/or wiki coverage (anchors include `source_id`, path, linked observation/wiki ids when partial). `explore ask` answers **evidence-first** from source passages (and observations when indexed), with exact citations, or returns `insufficient_evidence` without inventing neutral/wiki consensus. Scope answers with filters:

```bash
uv run knowledge-desk explore ask "What is the view on rates?" \
  --subject entity-jordi-visser \
  --topic topic-rates
```

Filters AND with the query. If nothing matches **inside** the filter, the reason is `no_matches_in_filter` (out-of-scope sources are never used silently). Unfiltered ask remains available. Recipe: “What does XYZ say about ABC?” → bind XYZ as `entity-*` subject on observations, then `explore ask` / `perspective at` with `--subject` and `--topic`. `--propose` writes review-only JSON under `system/update-queue/`; it never publishes wiki, memory, or observations by itself.

### Compile from ask (demand-driven wiki)

When sources answer a question but the living wiki is **missing or thin** for the subject/topic, queue a reviewable compile proposal (MCP `explore_ask` stays read-only):

```bash
uv run knowledge-desk explore compile-from-ask "What does the note say about frogs?" \
  --subject entity-example-wetland \
  --topic topic-amphibian-activity
# review system/update-queue/compile-from-ask-….json
uv run knowledge-desk proposal apply system/update-queue/compile-from-ask-….json
```

Outcomes: `proposed` (wiki missing/thin + evidence), `noop` (wiki already healthy), `insufficient_evidence` (open-question proposal). Compile proposal observation IDs include a per-proposal entropy token, so same-day proposals with similar excerpts do not collide. Apply uses all-or-nothing observation semantics under the writer lock: every stub must be an object, have resolved subjects/topics, validate, and pass ID/collision preflight before any observation is published. A staged publication failure rolls back every observation created by that batch; the proposal remains pending with a clear failure. Identical existing payloads are noops. Once the complete batch is created/noop, only those successful IDs (plus explicit proposal scope) feed `wiki evolve`.

## Wiki evolve and refine-validate

```bash
uv run knowledge-desk wiki evolve
uv run knowledge-desk wiki evolve --observation obs-20260718-frog-calls
uv run knowledge-desk wiki refine-validate
```

`wiki evolve` is a **mechanical** compiler: for each matched observation it creates or updates entity/topic pages under `wiki/entities/` and `wiki/topics/`, citing `observation_ids` and evidence locators, restating assertions without inventing hypotheses. Re-running merges observation sets and refreshes `updated_at` without dropping prior linked ids. High-impact narrative rewrites still belong in review (#8); evolve does not call an LLM.

`wiki refine-validate` runs vault `validate` and adds structured findings (severity, path, code, suggested action) for unsupported synthesis, orphan pages, dangling observation ids, empty evidence, duplicate titles, and freshness notes. Loop: observe → evolve → refine-validate → review.

## Read-only MCP server

```bash
# Local stdio (typical for desktop MCP clients)
uv run knowledge-desk --vault . mcp serve --transport stdio

# Network transport (SSE)
uv run knowledge-desk --vault . mcp serve --transport sse --host 127.0.0.1 --port 8000

# Docker (vault mounted read-only; disposable index under /tmp)
docker compose up --build
```

The MCP server is **read-only**: it exposes search, sources, evidence locators, entities/topics, observations, perspective at/timeline/compare, synthesis pages, explore gaps/ask, and cross-MCP composition helpers (`compose_contract`, `compose_with_external`). It never writes observations, wiki, memory, or update-queue proposals. Set `KNOWLEDGE_DESK_ROOT` and optional `KNOWLEDGE_DESK_INDEX_PATH` for container deployments. Prefer observation `statement_basis` over wiki prose; missing evidence is `unknown`/`insufficient_evidence`, never neutral fill-in.

Compose publishes MCP on host loopback only (`127.0.0.1:8000`) by default. Do not expose the unauthenticated MCP transport directly to a LAN or the public internet. Remote deployments must opt in explicitly and place an authenticated, TLS-terminating reverse proxy or equivalent access-control boundary in front of the service.

## Memory workspaces (user-owned thesis / frameworks)

Multi-page **user-owned** workbenches live under `memory/workspaces/` (thesis, framework, prediction sets, research programs, …). They are **not** written by `wiki evolve`, ingest, subscribe poll, or the maintainer. Refine only via explicit CLI (or `workspace_refine_proposal`).

```bash
uv run knowledge-desk workspace init --title "Macro liquidity thesis" --kind thesis \
  --id ws-thesis-macro --subject entity-… --topic topic-… \
  --statement "Working stance…"

uv run knowledge-desk workspace add-page --id ws-thesis-macro --title "Credit stress" --page-kind pillar
uv run knowledge-desk workspace refine --id ws-thesis-macro --summary "Clarify stance after new tape" --body "…"
uv run knowledge-desk workspace benchtest --id ws-thesis-macro --since 2026-07-01
uv run knowledge-desk workspace list
uv run knowledge-desk workspace get --id ws-thesis-macro
```

Benchtest classifies claims as supported / challenged / untested / conflicted / pending and may write `benchtests/*.json` + a changelog line; it **does not** auto-edit pages. Link `observation_ids` on pages; label pure priors with `--prior`. MCP exposes read-only `list_workspaces` / `get_workspace`.

## Cross-MCP composition

Join external MCP context with vault evidence **at query time** without storing private external state:

```bash
uv run knowledge-desk compose contract
uv run knowledge-desk compose join "How does external context relate to this topic?" \
  --external path/to/external-claims.json \
  --subject entity-example \
  --topic topic-example \
  --as-of 2026-07-18
```

Full recipe, claim envelope, and worked example: [cross-mcp.md](cross-mcp.md).

## Search index

The SQLite FTS5 index under `system/.index/` is disposable derived state. Rebuild anytime:

```bash
uv run knowledge-desk index rebuild
uv run knowledge-desk search "frog calls" --layer observation
uv run knowledge-desk search frog --subject entity-example-wetland
```

Hits identify `layer` (`source`, `observation`, `wiki`, `memory`) and stable vault ids/paths. Deleting the database loses no durable knowledge; run `index rebuild` after checkout or bulk imports. External MCP/private context is never indexed unless explicitly imported into the vault.

Structured filters use exact relational facets, not substring matching. Each hit exposes `subtype`, `subjects`, `topics`, `source_ids`, and `observation_ids` according to its canonical layer:

| Layer | Subtype | Structured associations |
|---|---|---|
| source | manifest `media_type` | source ID plus subjects, topics, and observations that cite the source |
| observation | `statement_basis` | direct subject/topic refs, cited sources, its own ID, and relation targets |
| wiki | wiki `kind` | entity/topic identity plus linked observations and their subject/topic/source associations |
| memory | memory `kind` or workspace `page_kind` | workspace subject/topic refs plus linked observations and evidence sources |

Rebuild constructs and integrity-checks a complete database under disposable staging, then atomically replaces the live SQLite file. Readers therefore retain the prior complete index until the new one is ready.

## Validate, lint, and review

```bash
uv run knowledge-desk validate
uv run knowledge-desk lint
```

`validate` is deterministic and CI-suitable: schemas, IDs, immutable hashes, evidence targets/selectors (including locator-kind vs media-type), dates/enums, namespace separation, dangling revision/relation/supersession targets, self-relations, and relation/supersession cycles.

`lint` adds structured review findings (`severity`, `path`, `code`, `message`, `suggested_action`, optional `evidence` ids). It includes validate errors, wiki refine-validate issues (unsupported synthesis, orphans, duplicates), unresolved contradiction pairs, stale-current horizon mismatches, and thin agent-inference rationales. Lint **never auto-fixes** content. Quality gates and the offline eval corpus are documented under `tests/corpus/README.md`.
