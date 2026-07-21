# Knowledge Desk

Knowledge Desk is a local-first tool for **collecting, sorting, and discussing a corpus of knowledge**. You ingest sources, record temporal observations, compare perspectives, explore gaps, and grow a cited wiki—with provenance underneath so claims stay grounded. Plain Markdown, immutable originals, and versioned schemas are durable truth. It is not a chatbot, vector database, finance product, or replacement for CRMs, calendars, or code hosts.

The durable chain under the desk:

```text
inbox -> original + normalized source -> temporal observation -> wiki synthesis
```

Indexes and MCP views are disposable projections. Start with the [operator guide](docs/operator-guide.md); see also [architecture](docs/architecture.md), [artifact model](docs/artifact-model.md), [workflows](docs/workflows.md), [development](docs/development.md), [implementation notes](docs/implementation-notes.md), and [AGENTS.md](AGENTS.md).

## Install and use

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). The repository pins the uv release and selects the supported Python 3.12 baseline; uv manages the disposable `.venv` and installs the locked dependencies.

```bash
uv python install 3.12
uv sync --locked
uv run knowledge-desk init   # local data dirs (gitignored)
uv run knowledge-desk ingest path/to/source.pdf
uv run knowledge-desk ingest inbox/
uv run knowledge-desk backup --out backups/desk.tar.gz

uv run knowledge-desk fetch-transcript "https://www.youtube.com/watch?v=VIDEO_ID"
uv run knowledge-desk fetch-transcript "https://www.youtube.com/watch?v=VIDEO_ID" --ingest
uv run knowledge-desk fetch-page "https://example.com/article"
uv run knowledge-desk fetch-page "https://example.com/article" --ingest --title "Article"
uv run knowledge-desk observe path/to/observation.json
uv run knowledge-desk observations list --subject entity-example
uv run knowledge-desk observations get obs-20260718-frog-calls
uv run knowledge-desk perspective at --subject entity-example --topic topic-example --as-of 2024-06-01
uv run knowledge-desk index rebuild
uv run knowledge-desk search "keyword" --layer observation
uv run knowledge-desk wiki evolve
uv run knowledge-desk wiki refine-validate
uv run knowledge-desk maintain once
uv run knowledge-desk maintain status
uv run knowledge-desk explore gaps
uv run knowledge-desk explore ask "What does the source say about …?"
uv run knowledge-desk mcp serve --transport stdio
uv run knowledge-desk validate
uv run knowledge-desk lint
uv run knowledge-desk proposal list
```

### Docker MCP and maintainer

```bash
# Read-only MCP (default service)
docker compose up --build mcp
# MCP SSE on localhost:8000; bind-mounts this repo read-only at /vault

# Unattended maintainer (read-write vault; inbox → wiki → lint → index)
docker compose --profile maintainer up --build maintainer
```

Supported bootstrap formats are PDF, Markdown (`.md`, `.markdown`), and UTF-8 plain text (`.txt`). `ingest` prints a JSON operation result suitable for scripts. Metadata can be supplied with `--title`, `--creator`, `--published`, `--url`, and `--language`. Originals are copied byte-for-byte under `sources/<source-id>/original/`; normalized Markdown and a manifest live beside them.

`observe` appends a temporal observation JSON document under `observations/`. It validates the schema, resolves every evidence locator against immutable sources, rejects self-relations and relation cycles, and never rewrites an existing `observation_id` (identical re-submit is a no-op).

**Obsidian is optional:** open the desk root as a vault to browse Markdown notes. **Git tracks product code only**; corpus data (`sources/`, `wiki/`, …) is local—use `init` after clone and `backup`/`restore` for archives. See the [operator guide](docs/operator-guide.md).

## Development

```bash
uv lock --check
uv sync --locked
uv run --offline --no-sync python -m unittest discover -s tests -v
uv run --offline --no-sync knowledge-desk validate
```

See [development](docs/development.md) before adding or upgrading a dependency. `pyproject.toml` is the human-maintained declaration; the committed `uv.lock` is the reproducible resolution and must change in the same review.

Still deferred or partial: LLM-assisted observation extraction, OCR/STT when captions are missing, and substantive domain packs.
