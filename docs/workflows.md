# Workflows

## Ingest

Run `evidence-vault ingest <file-or-directory>`. Directory ingestion processes supported files in lexical order and reports unsupported entries without corrupting successful records. Supply known metadata explicitly; ingestion does not infer publication dates or creators from prose.

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

`perspective timeline` lists introduced/confirms/refines/contradicts/supersedes events in time order. Multi-subject comparison scoring remains a follow-up under #10.

## Query and refine

Read normalized evidence by exact locator, then interpret observations, then consult wiki synthesis. Cite the most direct layer. A new claim becomes a new observation; never rewrite an old observation to make history cleaner. Wiki pages may be revised when their citations and `updated_at` metadata are updated. There is not yet a wiki compile, wiki refine-validate, or source-gap explorer command; those remain roadmap items (#27, #28).

Automated writers should eventually serialize canonical changes through one maintainer. Until then, place machine-proposed changes in `system/update-queue/` for review.

## Wiki evolve and refine-validate

```bash
uv run evidence-vault wiki evolve
uv run evidence-vault wiki evolve --observation obs-20260718-frog-calls
uv run evidence-vault wiki refine-validate
```

`wiki evolve` is a **mechanical** compiler: for each matched observation it creates or updates entity/topic pages under `wiki/entities/` and `wiki/topics/`, citing `observation_ids` and evidence locators, restating assertions without inventing hypotheses. Re-running merges observation sets and refreshes `updated_at` without dropping prior linked ids. High-impact narrative rewrites still belong in review (#8); evolve does not call an LLM.

`wiki refine-validate` runs vault `validate` and adds structured findings (severity, path, code, suggested action) for unsupported synthesis, orphan pages, dangling observation ids, empty evidence, duplicate titles, and freshness notes. Loop: observe → evolve → refine-validate → review.

## Search index

The SQLite FTS5 index under `system/.index/` is disposable derived state. Rebuild anytime:

```bash
uv run evidence-vault index rebuild
uv run evidence-vault search "frog calls" --layer observation
uv run evidence-vault search frog --subject entity-example-wetland
```

Hits identify `layer` (`source`, `observation`, `wiki`, `memory`) and stable vault ids/paths. Deleting the database loses no durable knowledge; run `index rebuild` after checkout or bulk imports. External MCP/private context is never indexed unless explicitly imported into the vault.

## Validate and review

Run `evidence-vault validate` and the unit tests. Validation covers schema definitions and examples, IDs, immutable hashes, evidence targets and selectors (including locator-kind vs media-type agreement), dates/enums via schemas, namespace separation, normalized extraction consistency, dangling revision/relation/supersession targets, self-relations, and directed cycles among observation relations or memory supersession links. Semantic lint (near-duplicates, unsupported synthesis prose, stale current-state claims) remains a separate follow-up. Git review is the recovery boundary for high-impact source, observation, wiki, memory, schema, and template changes.
