# Workflows

## Ingest

Run `evidence-vault ingest <file-or-directory>`. Directory ingestion processes supported files in lexical order and reports unsupported entries without corrupting successful records. Supply known metadata explicitly; ingestion does not infer publication dates or creators from prose.

The operation hashes first, checks duplicates, extracts in staging, preserves the original bytes, writes the manifest and normalized note, validates them, publishes atomically, and appends an ingest-log event. Exit status is nonzero if any requested input fails. JSON output makes no-op, created, revision, and failed states distinguishable.

## Query and refine

Read normalized evidence by exact locator, then interpret observations, then consult wiki synthesis. Cite the most direct layer. A new claim becomes a new observation; never rewrite an old observation to make history cleaner. Wiki pages may be revised when their citations and `updated_at` metadata are updated.

Automated writers should eventually serialize canonical changes through one maintainer. Until then, place machine-proposed changes in `system/update-queue/` for review.

## Validate and review

Run `evidence-vault validate` and the unit tests. Validation covers schema definitions and examples, IDs, immutable hashes, evidence targets and selectors (including locator-kind vs media-type agreement), dates/enums via schemas, namespace separation, normalized extraction consistency, dangling revision/relation/supersession targets, self-relations, and directed cycles among observation relations or memory supersession links. Semantic lint (near-duplicates, unsupported synthesis prose, stale current-state claims) remains a separate follow-up. Git review is the recovery boundary for high-impact source, observation, wiki, memory, schema, and template changes.
