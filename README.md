# Process Autopsy

Reconstructs how work **actually** moves through a company from the operational data
that already exists, then quantifies where time is lost and what is worth automating.

Nothing is installed into the customer's systems. You export what a ticketing system,
ERP or CRM already records, map the columns once, and the process is reconstructed
from the event log.

---

## What it does

1. **Ingests** a CSV/XLSX export or an API event stream into one canonical event model.
2. **Discovers** the real process: a directly-follows graph, the path variants, and
   per-case timelines — including the paths nobody documented.
3. **Measures** cycle time, waiting time, handoffs, rework loops and SLA breaches.
4. **Detects findings**: seven deterministic detectors, each producing a statement backed
   by the metric, the baseline, the affected case count and the monthly hour cost.
5. **Scores automation opportunities** as a product of named components, so every score
   can be taken apart.
6. **Explains** findings in plain language — from computed evidence only.

## The rule that shapes the architecture

> The AI layer never produces a number, and never creates a finding.

Every quantitative claim comes from `app/metrics`, `app/processes`, `app/findings` and
`app/opportunities` — plain, deterministic, unit-tested Python. The model in `app/ai`
receives already-computed evidence and returns prose. If the AI provider is absent or
fails, the product still works; you lose the phrasing, not the analysis.

This matters because the product's only real asset is trust. A process-mining tool that
invents a bottleneck is worse than no tool at all.

## Quick start

```bash
docker compose up
```

- API and interactive docs: <http://localhost:8000/docs>
- UI: <http://localhost:3000>

The demo workspace seeds itself on first start: a synthetic order-to-delivery process of
~420 cases containing a deliberately planted approval queue, an invoice-correction loop,
a manual ERP re-entry step, a rare expensive exception path, and a degradation in the
second half of the window. All five are found by the engine.

### Without Docker

```bash
cd backend && pip install -r requirements-dev.txt && uvicorn app.main:app --reload
```

```bash
cd frontend && npm install && npm run dev
```

The backend defaults to SQLite, so no database server is needed for local work.

## Tests

```bash
cd backend && python -m pytest -q
```

89 tests cover the statistics (percentiles, Tukey fences, cycle detection), trace
construction, every detector, the opportunity scorer, CSV sanitisation and mapping, the
full HTTP surface, and tenant isolation.

## Repository layout

```
backend/app/
  core/          config, database session, tenant resolution, structured logging
  ingestion/     CSV profiling, column mapping, formula-injection sanitisation
  processes/     trace building, directly-follows discovery, analysis service
  metrics/       percentiles, throughput, waiting, handoffs, rework, before/after
  findings/      the seven detectors and their impact scoring
  opportunities/ automation scoring with fully exposed components
  ai/            provider abstraction + structured narrative generation
  reports/       Markdown report builder
  demo/          seeded synthetic event log
frontend/
  app/           Next.js App Router pages (server components)
  components/    process map renderer, import wizard
```

## Design decisions worth knowing

**Waiting time is measured from the end of the previous step**, not from its start, and
is clamped at zero. Overlapping events in real exports would otherwise produce negative
queues that quietly poison every average.

**An edge's baseline is the median wait of the *other* edges.** Comparing an edge against
a baseline that includes itself lets the single dominant queue hide — with few edges it
drags the median up to its own level.

**Cycle detection uses iterative Tarjan SCC.** Real logs produce activity graphs deep
enough to blow a recursive implementation's stack.

**Findings are upserted by fingerprint.** Re-running an analysis refreshes the numbers,
keeps the status a human set, and marks disappeared findings resolved rather than
deleting the history.

**Every string from an upload is sanitised against CSV formula injection** (`=`, `+`,
`-`, `@`), because these exports get re-exported and opened in Excel.

**Confidence is capped at 0.95.** Nothing here should ever read as certain.

**Comparisons between time windows are labelled observational.** The before/after view
reports what changed; it does not claim the change caused it.

### Two deliberate deviations from the original brief

- **No graph library.** The process map is plain SVG with a longest-path layering, about
  200 lines with zero runtime dependencies, instead of React Flow or Cytoscape. It
  renders server-side, has no hydration cost, and back edges — the rework paths that are
  the whole point — are highlighted rather than routed around.
- **No client-side data-fetching library.** App Router server components fetch directly,
  which keeps the tenant API key on the server. Only the import wizard is a client
  component, and it reaches the API through a server-side proxy route.

## Multi-tenancy

Every table carries `tenant_id`, every query filters on it, and the tenant is resolved
from an `X-API-Key` header before any handler runs. Cross-tenant object access returns
404, not 403 — the existence of another tenant's objects is not disclosed. Tests assert
both.

## Configuring the AI layer

Set `AI_PROVIDER=openai_compatible` and point `AI_BASE_URL` at any OpenAI-style
`/chat/completions` endpoint (a hosted gateway, vLLM, Ollama). The default `offline`
provider is fully deterministic and needs no network, which is why the test suite runs
without credentials.

`AI_REDACT_PII=true` strips e-mail addresses, phone numbers and long digit strings from
the evidence before it leaves the process.

## What is not built

Honest scope boundaries: no background job queue (analysis runs synchronously — fine to
a few hundred thousand events, not to millions), no live connector implementations
(the ingestion API is there, the OAuth flows are not), no RBAC beyond a per-tenant key,
and no conformance checking against a designed to-be model.
