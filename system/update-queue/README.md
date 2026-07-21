# Proposed update queue

Future non-writer agents place proposed canonical changes here for single-writer review. Queue entries are review-only and do not become truth merely by existing.

`knowledge-desk explore … --propose` may write JSON proposals here (gap reports, observation stubs, open questions). Apply or reject with:

```bash
uv run knowledge-desk proposal list
uv run knowledge-desk proposal apply <file.json>
uv run knowledge-desk proposal reject <file.json>
```

Applied/rejected proposals are archived under `applied/` and `rejected/`. They do not become truth merely by existing in the queue.
