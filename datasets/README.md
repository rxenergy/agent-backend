# datasets/

Source corpora consumed by infra/scripts at bring-up time.
Not application code, not evaluation datasets.

## Layout

| Path | Purpose | Consumer |
|------|---------|----------|
| `seed_docs/smr_seed.jsonl` | OpenSearch seed corpus for the `smr-docs` index | `scripts/seed_opensearch.py` (W1) |

### `smr_seed.jsonl` coverage

Built around four NuScale licensing scenarios; ~60 chunks. **All citations are synthetic/illustrative for development** — accession numbers, RAI IDs, and CUF/DR values approximate the structure of public NRC ADAMS records but are not verbatim. Replace with verified source text before any licensing use.

| `scenario_object` | Scenario | `doc_type` mix |
|-------------------|----------|----------------|
| `O1` | (S1) Issue category landscape | `category_index`, `vendor` |
| `O2` | (S2) Technical detail (PCS / natural circulation / DWO / TF-1·TF-2 / SG structural) | `fsar`, `topical_report` |
| `O3` | (S3) RAI + Response + SER + audit | `rai`, `rai_response`, `ser`, `audit` |
| `O4` | (S4) Regulatory mapping (RG / SRP / 10 CFR / GDC) | `regulation`, `mapping` |

DWO (⭐️⭐️⭐️) is intentionally over-indexed: FSAR §4.4.4, TR-0915-17564 (methodology / TF-1 / TF-2 / limitations), TR-0316-22048 (structural integrity), RAI 8916 + 9013 with responses, SER §4.4.4, audit 2018-04, and four mapping documents.

## Not in scope

- **Evaluation datasets** (Phase 6): will live under `eval/datasets/` or a
  dedicated Phoenix dataset store — keep out of this directory to avoid
  conflating seed corpora with eval ground truth.
- **Prompt fragments**: see `prompts/`.
- **Tool I/O schemas**: see `tools/schemas/`.
