# Cross-MCP composition

Knowledge Desk is one MCP among many. **Private or live external context stays in its originating system.** A consuming agent joins results at query time. This desk does **not** copy portfolio, CRM, calendar, ticket, or codebase state into the vault by default.

Core schemas stay domain-neutral: they do not name holdings, securities, analysts, tickets, accounts, or similar external objects.

## Policy

| Rule | Meaning |
|------|---------|
| Join at query time | Call external MCP(s) + Knowledge Desk MCP separately; compose in the agent (or via `compose join`) |
| Stamp origin | Every fact carries `origin` (MCP/system name) and `origin_kind` (`vault` \| `external_mcp` \| `agent_reasoning`) |
| Stamp epistemic | `explicit` \| `inferred` \| `unknown` — never present inferred external data as explicit fact |
| No silent import | External claims are **not** written to sources/observations/wiki unless the operator runs an explicit ingest or proposal workflow |
| Read-only desk MCP | Knowledge Desk MCP tools never write; composition helpers are also read-only |

## Claim envelope (domain-neutral)

```json
{
  "claim_id": "claim-…",
  "text": "Fact or assertion text",
  "origin": "example-external-mcp",
  "origin_kind": "external_mcp",
  "epistemic": "explicit",
  "confidence": 0.9,
  "as_of": "2026-07-21",
  "citations": [{"ref": "opaque-id-from-external-system"}],
  "subject_refs": [],
  "topic_refs": [],
  "extensions": {}
}
```

Vault claims use `"origin": "knowledge-desk"` and `"origin_kind": "vault"`, with citations that resolve to observation ids or source locators.

## Orchestration recipe

1. **External MCP** — fetch live/private context; convert each fact into a claim with `origin` + `epistemic`.
2. **Knowledge Desk MCP** — `search`, `get_perspective_at`, `explore_ask`, `get_observations`, etc. for corpus evidence.
3. **Join** — `compose_with_external` (MCP) or `knowledge-desk compose join` (CLI). Nothing is stored.
4. **Reason** — agent compares datasets; mark its own inferences as `origin_kind: agent_reasoning`.
5. **Optional durable import** — only if the user wants a fact in the desk: write a reviewable file to `inbox/`, ingest, observe, propose — never auto-mirror the whole external system.

Machine-readable contract:

```bash
uv run knowledge-desk compose contract
# MCP tool: compose_contract
```

## CLI and MCP tools

```bash
# Print contract
uv run knowledge-desk compose contract

# Join external claims file with vault perspective + ask
uv run knowledge-desk compose join "How does external context relate to wetland amphibians?" \
  --external /tmp/external-claims.json \
  --subject entity-example-wetland \
  --topic topic-amphibian-activity \
  --as-of 2026-07-18
```

MCP (read-only):

| Tool | Role |
|------|------|
| `compose_contract` | Policy + field definitions + recipe |
| `compose_with_external` | Pass `external_context_json` + optional subject/topic/as_of; returns joined bundle |

## Worked example (finance-shaped, no core schema coupling)

An agent that reasons about investments might use **two MCPs**. The external system is only illustrated here; Knowledge Desk never defines a portfolio schema.

### 1. External MCP result (caller-stamped)

Illustrative JSON the agent builds after calling a separate portfolio-style MCP:

```json
{
  "claims": [
    {
      "text": "Constraint: single-name concentration limit is 5% of NAV.",
      "origin": "example-portfolio-mcp",
      "origin_kind": "external_mcp",
      "epistemic": "explicit",
      "as_of": "2026-07-21",
      "citations": [{"ref": "policy/concentration-v3"}]
    },
    {
      "text": "Working thesis: marsh restoration names may benefit from multi-year public spending.",
      "origin": "example-portfolio-mcp",
      "origin_kind": "external_mcp",
      "epistemic": "inferred",
      "confidence": 0.55,
      "as_of": "2026-07-21",
      "citations": [{"ref": "thesis/draft-12"}]
    }
  ]
}
```

Note the second claim is **`inferred`**: the agent must not treat it as an explicit external disclosure.

### 2. Vault MCP result (corpus)

```text
get_perspective_at(subject=entity-example-wetland, topic=topic-amphibian-activity, as_of=2026-07-18)
explore_ask("What mechanisms link wetland health to amphibian activity?", subject=…, topic=…)
```

Returns observations with `statement_basis`, orientations, and resolvable source locators — not a blended “score.”

### 3. Join

```bash
uv run knowledge-desk compose join \
  "Does vault evidence support the external thesis on wetland-related mechanisms?" \
  --external-json '{"claims":[…]}' \
  --subject entity-example-wetland \
  --topic topic-amphibian-activity \
  --as-of 2026-07-18
```

The bundle lists `external_claims` and `vault_claims` separately, each with origin stamps. The agent’s reasoning layer compares:

- explicit concentration constraint (external) vs nothing in vault (no invented portfolio fact),
- inferred thesis (external) vs explicit source statements (vault),
- conflicted vault perspectives surface as their own stamped claims.

### 4. What not to do

- Do not insert external holdings dumps into `sources/` as if they were research evidence without a deliberate import.
- Do not add `portfolio`, `holding`, or `ticker` fields to core observation/wiki schemas.
- Do not let MCP `explore_ask` silently write external context into the wiki.

## Other domains (same pattern)

| External MCP (examples) | Vault supplies |
|-------------------------|----------------|
| Project / tickets | Design notes, decisions, cited research |
| CRM | Account research sources, people pages |
| Calendar | Meeting notes and transcripts already ingested |
| Codebase | ADRs and architecture sources in the desk |
| Lab / instrument | Literature and field notes as sources |

Always: **external = live/private system of record; vault = cited corpus; join at the agent.**

## Example agent prompt (orchestration)

```text
You have two MCP servers: (1) knowledge-desk (read-only corpus), (2) an external context MCP.
For the user question:
1. Query the external MCP for current constraints and live state; stamp each fact origin + epistemic.
2. Query knowledge-desk: compose_contract, then search / get_perspective_at / explore_ask as needed.
3. Call compose_with_external with the external claims JSON and vault subject/topic/as_of when known.
4. Answer using the joined bundle. Label which MCP each sentence relies on.
5. If vault evidence is insufficient, say unknown — do not fill with external inference.
6. Do not write external state into the vault unless the user explicitly requests import.
```

## Related

- [architecture.md](architecture.md) — disposable MCP projections; external context at agent join time
- [workflows.md](workflows.md) — MCP tools and read-only policy
- [AGENTS.md](../AGENTS.md) — no private external dumps as default vault content
- Issue #13 — original cross-MCP composition requirement
