# Bootstrap implementation notes

This repository began empty, so the bootstrap follows the requested Python 3.12+, `pyproject.toml`, and `src/` conventions without preserving a prior toolchain.

The normalized-note front matter is YAML-compatible but intentionally limited to one key per line with JSON-encoded values. This gives Obsidian-readable properties while avoiding a YAML parser and its implicit type coercions. PDF extraction uses `pypdf`; OCR remains an adapter boundary because guessing text would violate provenance.

The example domain-pack manifest demonstrates namespace mechanics but is not installed under `domains/`, because substantive domain packs are deferred. Ecology and history observation examples exercise namespaced extensions without making either domain part of the core.

The bootstrap is one coherent package spanning the repository contract, artifact schemas/layout, ingestion, and the shared validation foundation. Its pull request links issues #4, #14, and #16. It does not claim the broader validation lifecycle in #3 or temporal-observation automation in #11 is complete.
