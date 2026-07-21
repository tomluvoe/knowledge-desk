# Architecture

Knowledge Desk separates durable records from rebuildable access layers.

1. `inbox/` receives untrusted candidate files.
2. `sources/` stores a byte-identical original, a manifest, and readable normalized Markdown under a content-derived stable ID.
3. `observations/` stores atomic, append-only, temporally explicit assertions whose evidence locators resolve back to sources.
4. `wiki/` stores revisable entity, topic, event, comparison, and synthesis notes.
5. `memory/` distinguishes user conclusions, decisions, and open questions from source evidence and agent hypotheses.
6. Indexes, graphs, embeddings, and read-oriented MCP endpoints are disposable projections. The optional SQLite FTS index lives at `system/.index/vault.sqlite` and is fully rebuildable from canonical paths.

External private context belongs in its originating system. A consuming agent may join results from multiple MCP servers at query time; Knowledge Desk does not copy portfolio, CRM, calendar, or codebase state by default. See [cross-mcp.md](cross-mcp.md) for the composition contract, claim envelope, CLI/MCP join tools, and worked examples (`compose contract`, `compose join`, MCP `compose_with_external`).

## Identity and immutability

A source ID is `src-` plus the first 24 hexadecimal characters of its SHA-256 content digest. Duplicate bytes therefore map to one source record regardless of filename. A different digest is a different source; ingestion may record it as a revision of an earlier same-named artifact, but never overwrites the earlier record.

Canonical publication is directory-atomic: extraction occurs in a same-filesystem staging directory, all files validate, and the completed directory is renamed into `sources/`. The append-only JSON Lines ingest log is written only after publication. A log-write failure is reported honestly without rolling back valid canonical evidence.

## Adapter boundary

Ingestion adapters return normalized Markdown, extraction status, warnings, and locator metadata. The registry selects adapters by extension. Future HTML, office, audio/video, and transcript adapters can use the same content-derived source identity, manifest, normalized note, and evidence-locator model.

YouTube caption retrieval is a separate **fetch** boundary (`fetch-transcript`), not an ingest adapter: it may use the network, writes a reviewable plain Markdown/text file (typically under `inbox/`), and only becomes canonical evidence after ordinary ingest. Unit tests inject a fake fetcher so offline CI never contacts YouTube.

PDF extraction is deterministic text extraction, not OCR. Pages remain distinct. Low-text or image-only documents are marked `needs_ocr`; no missing text is guessed.
