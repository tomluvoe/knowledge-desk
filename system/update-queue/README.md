# Proposed update queue

Future non-writer agents place proposed canonical changes here for single-writer review. Queue entries are review-only and do not become truth merely by existing.

`evidence-vault explore … --propose` may write JSON proposals here (gap reports, observation stubs, open questions). Apply or reject with:

```bash
uv run evidence-vault proposal list
uv run evidence-vault proposal apply <file.json>
uv run evidence-vault proposal reject <file.json>
```

Applied/rejected proposals are archived under `applied/` and `rejected/`. They do not become truth merely by existing in the queue.
