# Workflows

## Concurrency and single-writer

v0.1 serializes **canonical writes** (ingest publish, observe append, wiki evolve, proposal apply/reject) through an exclusive lock at `system/.locks/writer.lock` (gitignored). Concurrent readers (`validate`, `lint`, `search`, MCP tools) do not take the lock. If a writer cannot acquire the lock within ~30s it fails clearly rather than interleaving publishes.

## Ingest

Run `evidence-vault ingest <file-or-directory>`. Directory ingestion is **non-recursive**, processes supported files in lexical order, **skips dotfiles**, and reports unsupported entries without corrupting successful records. Empty plain-text files still ingest but carry a warning. Supply known metadata explicitly; ingestion does not infer publication dates or creators from prose.

The operation hashes first, checks duplicates, extracts in staging, preserves the original bytes, writes the manifest and normalized note, validates them, publishes atomically, and appends an ingest-log event. Exit status is nonzero if any requested input fails. JSON output makes no-op, created, revision, and failed states distinguishable.

## Fetch YouTube transcripts

```bash
uv run evidence-vault fetch-transcript "https://www.youtube.com/watch?v=VIDEO_ID"
uv run evidence-vault fetch-transcript "https://youtu.be/VIDEO_ID" --out inbox/talk.md
uv run evidence-vault fetch-transcript "VIDEO_ID" --language en --ingest
```

`fetch-transcript` is a **network-enabled** boundary. It downloads captions via the locked `youtube-transcript-api` dependency, writes a plain Markdown file (default `inbox/youtube-<video-id>.md`) with a short metadata header and lightly formatted transcript lines (`[mm:ss] text` unless `--no-timestamps`), and does **not** publish under `sources/` unless `--ingest` is passed. Prefer reviewing the inbox file, then `uv run evidence-vault ingest inbox/youtube-….md`.

Remote content is untrusted data, never instructions. Videos without usable captions fail cleanly with no partial canonical publish. Auto-generated captions are accepted with a warning. Private, blocked, or caption-less videos are operator errors, not silent empty sources. Title/channel are not scraped from the page; pass `--title` / `--creator` when known.

## Observe

Run `evidence-vault observe path/to/observation.json` after the cited sources exist. The document must match `observation.schema.json`, every evidence locator must resolve, and relation targets must already exist. Publication is append-only: a new `observation_id` is created, an identical payload is a no-op, and a conflicting payload for an existing ID is rejected. Later confirmations, refinements, contradictions, or supersessions are new observations that link via `relations`.

## Query observations

```bash
uv run evidence-vault observations list
uv run evidence-vault observations list --subject entity-example-wetland --topic amphibian
uv run evidence-vault observations list --source-id src-…
uv run evidence-vault observations get obs-20260718-frog-calls
uv run evidence-vault observations relations
```

`list` ANDs filters (`--subject`, `--topic`, `--source-id`, `--orientation`, `--epistemic-class`, `--statement-basis`, `--id-prefix`). Subject and topic match `ref_id` exactly or as a case-insensitive substring of `ref_id`/`label`. Results are sorted by `valid_at` (then `expressed_at` / `recorded_at`) and `observation_id`. `relations` returns the outgoing confirms/contradicts/refines/supersedes graph.

## Perspective at a date

```bash
uv run evidence-vault perspective at --subject entity-alpha --topic topic-outlook --as-of 2024-06-01
uv run evidence-vault perspective timeline --subject entity-alpha --topic topic-outlook --from 2024-01-01 --to 2024-12-31
```

`perspective at` returns the supported view for a subject+topic as of a date or datetime:

- An observation applies only if its effective time (`valid_at`, else `expressed_at` / `publication_date` / `recorded_at`) is on or before `as_of` and any `horizon` still covers that day.
- Superseded observations that still fall in range are dropped when a superseding observation also applies.
- No applying observation yields `status=unknown` and `reason=insufficient_evidence` — never a synthetic neutral stance.
- An observation whose orientation is `unknown` is still `supported` (explicit unknown ≠ missing evidence).
- Concurrent disagreeing active observations yield `status=conflicted` with the latest primary and `conflicting_observation_ids`.

`perspective timeline` lists introduced/confirms/refines/contradicts/supersedes events in time order.

```bash
uv run evidence-vault perspective compare \
  --subject entity-alpha --subject entity-beta \
  --topic topic-outlook --as-of 2024-06-01
```

`perspective compare` places subjects side-by-side across explicit dimensions (orientation, assertion, statement_basis, mechanisms, risks, horizon, freshness, …). Agreement is per-dimension (`agree` / `disagree` / `insufficient`). There is **no** single opaque similarity score. Subjects without applying observations appear under `insufficient` with status `unknown`, never a synthetic neutral stance.

## Query and refine

Read normalized evidence by exact locator, then interpret observations, then consult wiki synthesis. Cite the most direct layer. A new claim becomes a new observation; never rewrite an old observation to make history cleaner. Wiki pages may be revised when their citations and `updated_at` metadata are updated.

```bash
uv run evidence-vault proposal list
uv run evidence-vault proposal apply system/update-queue/explore-ask-….json
uv run evidence-vault proposal reject system/update-queue/….json --reason "not ready"
```

Proposals under `system/update-queue/` are review-only. `proposal apply` runs under the writer lock, may append observations or memory open questions when complete, and archives JSON under `system/update-queue/applied/` (or `rejected/`). Incomplete observation stubs with `entity-todo` / `topic-todo` are skipped until edited.

## Source-gap exploration

```bash
uv run evidence-vault explore gaps
uv run evidence-vault explore gaps --topic amphibian --propose
uv run evidence-vault explore ask "Where were frog calls recorded?"
uv run evidence-vault explore ask "What is the capital of Mars?" --propose
```

`explore gaps` lists sources missing observation and/or wiki coverage (anchors include `source_id`, path, linked observation/wiki ids when partial). `explore ask` answers **evidence-first** from source passages (and observations when indexed), with exact citations, or returns `insufficient_evidence` without inventing neutral/wiki consensus. `--propose` writes review-only JSON under `system/update-queue/`; it never publishes wiki, memory, or observations by itself.

## Wiki evolve and refine-validate

```bash
uv run evidence-vault wiki evolve
uv run evidence-vault wiki evolve --observation obs-20260718-frog-calls
uv run evidence-vault wiki refine-validate
```

`wiki evolve` is a **mechanical** compiler: for each matched observation it creates or updates entity/topic pages under `wiki/entities/` and `wiki/topics/`, citing `observation_ids` and evidence locators, restating assertions without inventing hypotheses. Re-running merges observation sets and refreshes `updated_at` without dropping prior linked ids. High-impact narrative rewrites still belong in review (#8); evolve does not call an LLM.

`wiki refine-validate` runs vault `validate` and adds structured findings (severity, path, code, suggested action) for unsupported synthesis, orphan pages, dangling observation ids, empty evidence, duplicate titles, and freshness notes. Loop: observe → evolve → refine-validate → review.

## Read-only MCP server

```bash
# Local stdio (typical for desktop MCP clients)
uv run evidence-vault --vault . mcp serve --transport stdio

# Network transport (SSE)
uv run evidence-vault --vault . mcp serve --transport sse --host 127.0.0.1 --port 8000

# Docker (vault mounted read-only; disposable index under /tmp)
docker compose up --build
```

The MCP server is **read-only**: it exposes search, sources, evidence locators, entities/topics, observations, perspective at/timeline/compare, synthesis pages, and explore gaps/ask. It never writes observations, wiki, memory, or update-queue proposals. Set `EVIDENCE_VAULT_ROOT` and optional `EVIDENCE_VAULT_INDEX_PATH` for container deployments. Prefer observation `statement_basis` over wiki prose; missing evidence is `unknown`/`insufficient_evidence`, never neutral fill-in.

## Search index

The SQLite FTS5 index under `system/.index/` is disposable derived state. Rebuild anytime:

```bash
uv run evidence-vault index rebuild
uv run evidence-vault search "frog calls" --layer observation
uv run evidence-vault search frog --subject entity-example-wetland
```

Hits identify `layer` (`source`, `observation`, `wiki`, `memory`) and stable vault ids/paths. Deleting the database loses no durable knowledge; run `index rebuild` after checkout or bulk imports. External MCP/private context is never indexed unless explicitly imported into the vault.

## Validate, lint, and review

```bash
uv run evidence-vault validate
uv run evidence-vault lint
```

`validate` is deterministic and CI-suitable: schemas, IDs, immutable hashes, evidence targets/selectors (including locator-kind vs media-type), dates/enums, namespace separation, dangling revision/relation/supersession targets, self-relations, and relation/supersession cycles.

`lint` adds structured review findings (`severity`, `path`, `code`, `message`, `suggested_action`, optional `evidence` ids). It includes validate errors, wiki refine-validate issues (unsupported synthesis, orphans, duplicates), unresolved contradiction pairs, stale-current horizon mismatches, and thin agent-inference rationales. Lint **never auto-fixes** content. Quality gates and the offline eval corpus are documented under `tests/corpus/README.md`.
