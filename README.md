# Evidence Vault

Evidence Vault is a local-first, model-independent repository for trustworthy evidence and knowledge. Plain Markdown, immutable source artifacts, and version-controlled schemas are the durable truth. It is not a chatbot, vector database, finance product, or replacement for external systems such as CRMs, calendars, portfolios, and code hosts.

The durable evidence chain is:

```text
inbox -> original + normalized source -> temporal observation -> wiki synthesis
```

Generated indexes and future MCP views sit downstream and must always be rebuildable. See [the architecture](docs/architecture.md), [artifact model](docs/artifact-model.md), [development workflow](docs/development.md), [implementation notes](docs/implementation-notes.md), and the authoritative [agent contract](AGENTS.md).

## Install and use

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). The repository pins the uv release and selects the supported Python 3.12 baseline; uv manages the disposable `.venv` and installs the locked dependencies.

```bash
uv python install 3.12
uv sync --locked
uv run evidence-vault ingest path/to/source.pdf
uv run evidence-vault ingest inbox/
uv run evidence-vault fetch-transcript "https://www.youtube.com/watch?v=VIDEO_ID"
uv run evidence-vault fetch-transcript "https://www.youtube.com/watch?v=VIDEO_ID" --ingest
uv run evidence-vault observe path/to/observation.json
uv run evidence-vault observations list --subject entity-example
uv run evidence-vault observations get obs-20260718-frog-calls
uv run evidence-vault perspective at --subject entity-example --topic topic-example --as-of 2024-06-01
uv run evidence-vault index rebuild
uv run evidence-vault search "keyword" --layer observation
uv run evidence-vault wiki evolve
uv run evidence-vault wiki refine-validate
uv run evidence-vault explore gaps
uv run evidence-vault explore ask "What does the source say about …?"
uv run evidence-vault mcp serve --transport stdio
uv run evidence-vault validate
```

### Docker MCP (read-only vault mount)

```bash
docker compose up --build
# MCP SSE on localhost:8000; bind-mounts this repo read-only at /vault
```

Supported bootstrap formats are PDF, Markdown (`.md`, `.markdown`), and UTF-8 plain text (`.txt`). `ingest` prints a JSON operation result suitable for scripts. Metadata can be supplied with `--title`, `--creator`, `--published`, `--url`, and `--language`. Originals are copied byte-for-byte under `sources/<source-id>/original/`; normalized Markdown and a manifest live beside them.

`observe` appends a temporal observation JSON document under `observations/`. It validates the schema, resolves every evidence locator against immutable sources, rejects self-relations and relation cycles, and never rewrites an existing `observation_id` (identical re-submit is a no-op).

To use Obsidian, open the repository root as a vault. All canonical notes are ordinary Markdown; `system/` contains supporting schemas, templates, logs, and queues.

## Development

```bash
uv lock --check
uv sync --locked
uv run --offline --no-sync python -m unittest discover -s tests -v
uv run --offline --no-sync evidence-vault validate
```

See [development](docs/development.md) before adding or upgrading a dependency. `pyproject.toml` is the human-maintained declaration; the committed `uv.lock` is the reproducible resolution and must change in the same review.

This bootstrap intentionally defers LLM observation extraction, wiki compilation, derived search indexes, an MCP server, a containerized maintainer, substantive domain packs, OCR execution, and controlled conversational writes. Their interfaces and safety boundaries are documented, but they are not claimed as implemented.
