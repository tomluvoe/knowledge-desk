# Artifact model

All schema contracts use JSON Schema Draft 2020-12 and semantic schema version `1.0.0`. JSON manifests and JSON examples are directly validated. Normalized Markdown uses a deliberately small YAML-compatible front matter subset: one key per line with a JSON value. This remains readable by ordinary Markdown tools and parseable without a YAML-specific runtime.

## Evidence chain

An evidence locator identifies a source, immutable content hash, normalized note, locator kind, and exact selector. Selectors support PDF pages, Markdown headings, line ranges, and blocks. A SHA-256 quote digest may pin the selected text. Validation checks source existence, digest agreement, normalized-path containment, and selector resolution.

Observations carry separate publication, expression, recording, validity, horizon, and freshness fields. Relations (`confirms`, `contradicts`, `refines`, `supersedes`) point to stable observation IDs. The generic orientation vocabulary is `supportive`, `critical`, `neutral`, `mixed`, `conditional`, or `unknown`; it makes no domain-specific claim.

Core schemas reject unknown top-level fields. Domain-specific data goes in `extensions`, keyed by a registered dotted namespace such as `org.example.ecology`. A domain-pack manifest owns that namespace and lists its schemas. Namespacing prevents a first domain from hardening into the core model.

Wiki notes are revisable interpretations and must cite observations and/or exact evidence. Memory records explicitly identify whether they are a user conclusion, decision, or open question. Templates under `system/templates/` are starting points, not evidence.
