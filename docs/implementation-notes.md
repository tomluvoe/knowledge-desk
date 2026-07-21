# Bootstrap implementation notes

This repository began empty, so the bootstrap follows the requested Python 3.12+, `pyproject.toml`, and `src/` conventions without preserving a prior toolchain.

The normalized-note front matter is YAML-compatible but intentionally limited to one key per line with JSON-encoded values. This gives Obsidian-readable properties while avoiding a YAML parser and its implicit type coercions. PDF extraction uses `pypdf`; OCR remains an adapter boundary because guessing text would violate provenance.

The example domain-pack manifest demonstrates namespace mechanics but is not installed under `domains/`, because substantive domain packs are deferred. Ecology and history observation examples exercise namespaced extensions without making either domain part of the core.

The bootstrap is one coherent package spanning the repository contract, artifact schemas/layout, ingestion, and the shared validation foundation. Its pull request links issues #4, #14, and #16.

Observation append (`knowledge-desk observe`) validates schema and resolvable evidence before publishing under `observations/<observation_id>.json`. Query helpers (`observations list|get|relations`) and temporal `perspective at|timeline` advance #11 and #10.

YouTube transcript fetch (`fetch-transcript`) uses the uv-locked `youtube-transcript-api` package behind an injectable fetcher boundary so unit tests never require network. The downloaded Markdown is ordinary text for the existing ingest adapters; the transcript file is the immutable original (not the video stream).

Mechanical living-wiki evolve (entity/topic/source-summary/cross-source/comparison/event pages), refine-validate, rebuildable FTS search, dimensional perspective compare, source-gap explore, MCP, vault `lint`, offline eval corpus, exclusive write locking, proposal list/apply/reject, desk `init`, tar.gz `backup`/`restore` (data out of product Git), and unattended `maintain once|loop` (Compose `maintainer` profile, durable `system/jobs/`) are implemented. LLM observation extraction and OCR/STT remain open.
