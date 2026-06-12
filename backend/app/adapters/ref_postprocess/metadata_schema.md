# Metadata Catalog Schema

The catalog has **8,218 rows** unified from three sources. Column names are
**kept verbatim** from the source files (ADAMS uses `PascalCase`,
NRC GovInfo rows use `camelCase`, SUBDOC uses `snake_case`).

Counts breakdown by `source_type` (row layout — which columns are populated):

| `source_type`        | rows  | layout                                                          |
| -------------------- | ----- | --------------------------------------------------------------- |
| `NRC_MANUAL_ADAMS`   | 2,660 | ADAMS columns, `AccessionNumber` present                        |
| `NRC_MANUAL_CFR`     |   129 | GovInfo columns, `packageId` present                            |
| `NuScale`            | 5,377 | ADAMS columns, `AccessionNumber` present                        |
| `NRC_MANUAL_SUBDOC`  |    52 | SUBDOC-only columns, `subdoc_id` present (GDCs inside 10 CFR Part 50 Appendix A) |

Counts breakdown by `doc_category` (top-level grouping):

- **NRC_MANUAL**: `RG` (1,474), `SRP` (1,027), `10CFR` (117), `FR` (98), `DSRS` (73).
  Note: `FR` (98) = 86 ADAMS Federal Register notices + 12 GovInfo FR volumes.
  Note: the 129 `NRC_MANUAL_CFR` rows split into 117 `10CFR` and 12 `FR` by
  `doc_category`.
- **NuScale**: `nuscale_Letter` (1,537), `nuscale_RAI` (1,382),
  `nuscale_Meeting` (991), `nuscale_DCA` (270), `nuscale_Affidavit` (209),
  `nuscale_Audit` (194), `nuscale_TechReport` (192), `nuscale_SER` (189),
  `nuscale_Topical_Report` (161), `nuscale_FSAR` (157), `nuscale_etc` (78),
  `nuscale_Inspection` (17).

Use `query_metadata(filters, columns_to_return?, top_k?)` to search. Filters
are ANDed across columns.

## Row layout — which columns each row carries

There are three row layouts that share **only two columns**: `source_type` and
`doc_category`. Everything else is mutually exclusive between ADAMS and CFR.

### ⚠ Column compatibility table

| Group              | Columns                                                                                                                                                                                                                                                                                                              | Used by                                              |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| **Shared**         | `source_type`, `doc_category`                                                                                                                                                                                                                                                                                        | ALL rows                                             |
| **ADAMS-only**     | `AccessionNumber`, `DocumentTitle`, `DocumentDate`, `DocumentType`, `DocumentReportNumber`, `DocketNumber`, `LicenseNumber`, `AuthorName`, `AuthorAffiliation`, `AddresseeName`, `AddresseeAffiliation`, `ContactPerson`, `CaseReferenceNumber`, `Keyword`, `Comment`, `PackageNumber`, `Url`, `IsPackage`, `ItemType` | NRC_MANUAL_ADAMS + NuScale rows ONLY                |
| **CFR/GovInfo-only**| `packageId`, `title`, `documentType` (lowercase d), `dateIssued`, `partRange_from`, `partRange_to`, `governmentAuthor1`, `governmentAuthor2`, `publisher`, `collectionCode`, `collectionName`, `category`, `branch`, `_path`, `download_pdfLink`, `detailsLink`                                                       | NRC_MANUAL_CFR rows ONLY (`doc_category` ∈ 10CFR/FR) |
| **SUBDOC-only**     | `subdoc_id`, `subdoc_type`, `parent_source_id`, `regulation_number`, `subdoc_name`, `anchor_chunk_id`                                                                                                                                                                                                                | NRC_MANUAL_SUBDOC rows ONLY                          |

**Always-zero query patterns to AVOID:**

| ❌ This query                                                  | Why it fails                                              |
| ------------------------------------------------------------- | --------------------------------------------------------- |
| `{"doc_category": "10CFR", "DocumentTitle": "..."}`           | CFR rows have no `DocumentTitle` column → 0 results       |
| `{"doc_category": "10CFR", "DocumentReportNumber": "..."}`    | CFR rows have no `DocumentReportNumber` → 0 results       |
| `{"doc_category": "RG", "packageId": "..."}`                  | ADAMS rows have no `packageId` → 0 results                |
| `{"doc_category": "SRP", "partRange_from": "..."}`            | ADAMS rows have no `partRange_from` → 0 results           |
| `{"subdoc_type": "GDC", "DocumentTitle": "..."}`              | SUBDOC rows have no `DocumentTitle` → 0 results           |
| `{"regulation_number": "GDC 5", "packageId": "..."}`          | SUBDOC rows have no `packageId` → 0 results               |

The tool returns a `warning` field when it detects these mismatches.

### `source_type = "NRC_MANUAL_ADAMS"` (2,660 rows)

ADAMS columns. Primary id: **`AccessionNumber`** (an `ML*` string).

### `source_type = "NuScale"` (5,377 rows)

Same ADAMS column set. Primary id: **`AccessionNumber`** (an `ML*` string).

### `source_type = "NRC_MANUAL_CFR"` (129 rows)

GovInfo CFR columns. Primary id: **`packageId`** (a `CFR-YYYY-titleN-volM` string).

### Always present (derived) on every row

- `source_type` — see above.
- `doc_category` — `RG`/`SRP`/`10CFR`/`FR`/`DSRS` for NRC_MANUAL,
  `nuscale_*` for NuScale.

## ref_source_id

The value to emit in `emit_references_v2` is:
- the row's `AccessionNumber` if present (8,037 rows), or
- the row's `packageId` otherwise (129 rows).

Both pools together form the catalog's set of valid identifiers. `query_metadata`
results always include whichever of the two is populated.

## Matching behavior

| Column                                                                   | Rule                                                                         |
| ------------------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| `AccessionNumber`, `packageId`, `DocumentReportNumber`                   | Normalized substring (case + hyphens + slashes + spaces + RG zero-padding + Rev suffix ignored). |
| `DocumentDate`, `DateAdded`, `DateAddedTimestamp`, `DateDocketed`, `dateIssued`, `lastModified` | Prefix substring (works with `YYYY-MM-DD`, e.g. `"2023"` matches all 2023 docs). |
| All other columns                                                        | Case-insensitive substring. List values match if ANY element contains the query. |

**Query format for list-valued columns**: pass a plain keyword, NOT a Python
list literal.
- Correct: `{"DocumentType": "Standard Review Plan"}`
- Wrong: `{"DocumentType": "['Standard Review Plan']"}`

## ADAMS columns (used by `NRC_MANUAL_ADAMS` and `NuScale` rows)

### `AccessionNumber` (string)
NRC ADAMS accession. 10 alphanumeric chars after `ML`. Example: `ML15355A322`.
Matching is normalized.

### `DocumentTitle` (string, 100% populated)
Full ADAMS title. Often contains a standard identifier:
- RG: `"Regulatory Guide 1.85, Revision 3, Code Case Acceptability ..."`
- SRP: `"NUREG-0800, Chpt 4, Section 4.6, Rev. 1, ..."`,
  `"Standard Review Plan 13.2.2, Revision 4, ..."`
- DSRS: `"NuScale Design-Specific Review Standard Section 10.3, ..."`

**Search tip**: use a distinctive substring (`"3.2.2"`, `"Section 13.6"`), not
the full citation text.

### `DocumentDate` (string, 100%)
`YYYY-MM-DD`. Prefix-matched.

### `DocumentType` (list of strings, 100%)
ADAMS-assigned type tags. Top patterns per category:

| doc_category            | Top `DocumentType` patterns                                                       |
| ----------------------- | --------------------------------------------------------------------------------- |
| `RG`                    | `['Regulatory Guide']` (78%), `['Regulatory Guidance']` (12%)                     |
| `SRP`                   | `['NUREG', 'Standard Review Plan']` (59%), `['NUREG, Draft', 'Standard Review Plan']` (21%), `['Standard Review Plan']` (10%) |
| `DSRS`                  | `['NUREG']` (100%)                                                                |
| `10CFR` (CFR rows)      | `'CFR'` (note: stored under column `documentType` for CFR rows, not `DocumentType`) |
| `FR`                    | `['Federal Register Notice']` (85%)                                               |
| `nuscale_Letter`        | `['E-Mail']` (48%), `['Letter']` (37%)                                            |
| `nuscale_RAI`           | `['Letter', 'Response to Request for Additional Information (RAI)']` (59%)        |
| `nuscale_Meeting`       | `['Meeting Notice', 'Meeting Agenda']` (50%)                                      |
| `nuscale_DCA`           | `['License-Application for Design Certification']` (90%)                          |
| `nuscale_Affidavit`     | `['Legal-Affidavit', 'Letter']` (64%), `['Legal-Affidavit']` (28%)                |
| `nuscale_Audit`         | `['Audit Plan', 'Memoranda']` (37%), `['Audit Report']` (33%)                     |
| `nuscale_TechReport`    | `['Letter', 'Report, Technical']` (32%)                                           |
| `nuscale_SER`           | `['Final Safety Evaluation Report (FSER)']` (37%), `['Safety Evaluation Report']` (29%) |
| `nuscale_Topical_Report`| `['Legal-Affidavit', 'Letter', 'Topical Report']` (35%), `['Letter', 'Topical Report']` (31%) |
| `nuscale_FSAR`          | `['Final Safety Analysis Report (FSAR)', 'Letter']` (25%), `['Final Safety Analysis Report (FSAR)']` (18%) |

Match with plain keyword: `{"DocumentType": "Topical Report"}` finds any row
whose list contains a string with "Topical Report".

### `DocumentReportNumber` (list of strings)
Official report identifier. **Best for RG/NUREG/SRP citations**, but sparse
for DSRS/NuScale.

Populated %:

| doc_category | populated |
| ------------ | --------- |
| `RG`         | 83%       |
| `SRP`        | 94%       |
| `DSRS`       | 0%        |
| `FR`         | 0%        |
| most `nuscale_*` | 0-13% |
| `nuscale_TechReport`, `nuscale_Topical_Report` | useful TR-#### numbers |

Examples: `['RG-1.68']`, `['NUREG-0800']`, `['NUREG-0800', 'NUREG-75/087']`,
`['NUREG/CR-6909']`, `['TR-0916-51299-NP, Rev 1']`.

Normalization is automatic: `RG 1.68` / `RG-1.68` / `Regulatory Guide 1.68`
all match. NUREG zeros are preserved (`NUREG-0800` ≠ `NUREG-800`).

### `DocketNumber` (list of strings)
NRC docket numbers. Populated for NuScale (95-100%) and FR, near 0% for
NRC_MANUAL RG/SRP/DSRS. Examples: `['05200048']`, `['PROJ0769']`.

NuScale docs use `['05200048']` (Design Certification) or `['PROJ0769']`
(legacy project) almost universally — not selective on its own.

### `LicenseNumber` (list of strings)
Usually empty.

### `AuthorName` (list of strings)
20-97% populated. Example: `['Bergman T A']`.

### `AuthorAffiliation` (list of strings)
Example: `['NuScale Power, LLC']`, `['NRC/NRO/DNRL/LB1']`, `['NRC/NRR/DSS']`.

### `AddresseeName`, `AddresseeAffiliation` (lists of strings)
Letter/affidavit recipients. Empty for many non-letter doc types.

### `ContactPerson` (string)
Often empty.

### `CaseReferenceNumber` (list of strings)
ADAMS case/correspondence numbers. Dense in NuScale RAI/Meeting (73-98%).
Example: `['LO-0719-66144']`.

### `Keyword` (list of strings)
Often internal codes (`['DPCautoadd', 'ems2']`). Sometimes topical.

### `Comment` (string)
Free-text, often empty.

### `PackageNumber` (string)
ADAMS package ID this document is part of. Useful to find sibling docs.

### `PackagesFiledIn`, `DocumentsFiledInPackage` (lists of strings)
Cross-references. Sparse.

### `DateAdded`, `DateAddedTimestamp`, `DateDocketed` (strings)
Ingestion / docketing dates. Rarely useful for citation resolution.

### `IsPackage`, `IsLegacy`, `Availability`, `ItemType` (strings)
Mostly constant (`"No"`, `"Publicly Available"`, `"doc"`). Not selective.

### `Url` (string)
PDF link on nrc.gov.

## CFR columns (used only by `NRC_MANUAL_CFR` rows)

### `packageId` (string)
GovInfo identifier, e.g. `CFR-1997-title10-vol1`. Matching is normalized.

### `title` (string)
Always `"Energy"` for CFR Title 10. Not useful as a filter.

### `documentType` (string, lowercase d)
Always `"CFR"`. Note: CFR rows use lowercase `documentType`, ADAMS rows use
PascalCase `DocumentType`.

### `dateIssued` (string, YYYY-MM-DD)
Annual edition date. Prefix match (`"2013"` → all 2013 CFR volumes).

### `partRange_from`, `partRange_to` (strings)
Inclusive range of CFR Parts in this volume. Examples:
- `"0"` / `"50"` (volume containing Part 50)
- `"51"` / `"199"`
- `"500"` / `"1706"`

**To find a volume containing Part N**, query both ends:
`{"doc_category": "10CFR", "partRange_from": "0", "partRange_to": "50"}`
or just `{"partRange_from": "0"}` if you know Part 50 sits in a 0-N range.

### `governmentAuthor1`, `governmentAuthor2`, `publisher` (strings)
Issuing org. Usually `"National Archives and Records Administration"` etc.

### `collectionCode`, `collectionName`, `category`, `branch` (strings)
Always `"CFR"`, `"Code of Federal Regulations (annual edition)"`,
`"Regulatory Information"`, `"executive"`.

### `_path` (string)
JSON file path inside the parsing root. Example:
`"10CFR/cfr/1997/CFR-1997-title10-vol1.json"`.

### `download_pdfLink`, `detailsLink` (strings)
URLs on govinfo.gov.

### `note` (string)
Free-text, often empty.

## SUBDOC columns (used only by `NRC_MANUAL_SUBDOC` rows)

Sub-document rows extracted from larger PDFs. Currently covers GDC 1-64
inside 10 CFR Part 50 Appendix A (52 rows; GDC 6-9, 47-49, 58-59 are Reserved
by the regulation, GDC 62-64 are not yet extracted).

### `subdoc_id` (string, primary key for SUBDOC rows)
Stable identifier independent of CFR annual editions. Example: `CFR-10-50-A-GDC5`.
Matching is normalized (case + hyphens + spaces ignored).

### `subdoc_type` (string, enum)
Currently only `"GDC"`. Reserved for future expansion (e.g. RG Position).

### `parent_source_id` (string)
The `packageId` of the parent CFR vol from which this sub-document was
extracted. Always points to the most recent annual edition that contains
the sub-document text. Example: `CFR-2025-title10-vol1`.

### `regulation_number` (string)
Human-readable key. Example: `"GDC 5"`. Matching is normalized so `"GDC5"`,
`"GDC-5"`, `"gdc 5"` all match the same row.

### `subdoc_name` (string)
The sub-document title (first sentence). Example for GDC 5:
`"Sharing of structures, systems, and components"`.

### `anchor_chunk_id` (string)
The chunk_id of the parent chunks JSON where this sub-document was located.
Useful for debugging / auditing the extraction.

## SUBDOC search policy

**SUBDOC rows are auto-excluded** from generic ADAMS/CFR queries (to avoid
14k+ token bloat in future expansions). To get SUBDOC rows you MUST include
at least one of these keys in `filters`:

```
subdoc_id | subdoc_type | regulation_number | parent_source_id
```

If you query without any of these, the response carries `subdoc_excluded: true`
and SUBDOC rows are filtered out. Re-query with a SUBDOC key to include them.

**Never mix SUBDOC keys with ADAMS or CFR columns** in one filter — SUBDOC
rows do not have those, so it always returns 0 rows. The tool emits a
warning when it detects this mismatch:

| ❌ Query                                                              | Why it fails                                              |
| -------------------------------------------------------------------- | --------------------------------------------------------- |
| `{"subdoc_type": "GDC", "DocumentTitle": "..."}`                     | SUBDOC rows have no `DocumentTitle` → 0 results           |
| `{"regulation_number": "GDC 5", "packageId": "..."}`                 | SUBDOC rows have no `packageId` → 0 results               |
| `{"subdoc_id": "CFR-10-50-A-GDC5", "DocumentReportNumber": "..."}`   | SUBDOC rows have no `DocumentReportNumber` → 0 results    |

## Derived columns

### `source_type` (string)
One of `"NRC_MANUAL_ADAMS"`, `"NRC_MANUAL_CFR"`, `"NuScale"`, `"NRC_MANUAL_SUBDOC"`.

### `doc_category` (string)
Top-level grouping inferred from the document's folder layout (SUBDOC rows
inherit the parent CFR's category, e.g. `"10CFR"` for all GDCs).

## Search recipes

- **"RG 1.68" / "Regulatory Guide 1.68":**
  `query_metadata(filters={"DocumentReportNumber": "RG 1.68"})`.

- **"NUREG-0800" alone:**
  `filters={"DocumentReportNumber": "NUREG-0800"}` returns many SRP rows.
  Add `doc_category=SRP` or a section number in `DocumentTitle` to narrow.

- **"SRP Section 3.2.2" / "SRP 3.2.2":**
  `filters={"doc_category": "SRP", "DocumentTitle": "3.2.2"}` first. If no
  good hit, emit a NUREG-0800 row with `section_path=["Section 3.2.2"]`.

- **"DSRS Section 10.3":**
  `filters={"doc_category": "DSRS", "DocumentTitle": "10.3"}`. DSRS has no
  `DocumentReportNumber` — `doc_category` + `DocumentTitle` is the only path.

- **"10 CFR Part 50" / "10 CFR 50.55":**
  `filters={"doc_category": "10CFR", "partRange_from": "0"}` finds CFR
  volumes whose Part range starts at 0 (i.e. covers Part 50). Pick a recent
  year; put precise location in `section_path` like
  `["Part 50", "50.55"]`. The emitted `ref_source_id` is the row's `packageId`.

- **"GDC 4" / "Criterion 4" / "General Design Criterion 4":**
  GDC has its own SUBDOC row. Query
  `filters={"regulation_number": "GDC 4"}` and emit the returned `subdoc_id`
  (e.g. `CFR-10-50-A-GDC4`) directly as `ref_source_id` — no `section_path`
  needed. Normalization handles `"GDC4"`/`"GDC-4"`/`"gdc 4"` automatically.
  Fall back to the CFR vol approach if `regulation_number` returns no row
  (e.g. for GDC 62-64 which are not yet extracted).

- **List all GDCs:**
  `filters={"subdoc_type": "GDC"}` returns all 52 indexed GDCs.

- **ADAMS accession (e.g. "ML15355A513"):**
  `filters={"AccessionNumber": "ML15355A513"}` — exact lookup.

- **NuScale Topical Report (e.g. "TR-0916-51299-NP, Rev 1"):**
  `filters={"DocumentReportNumber": "TR-0916-51299"}`.

- **NuScale FSAR Chapter 14:**
  `filters={"doc_category": "nuscale_FSAR", "DocumentTitle": "Chapter 14"}`.

- **NuScale DCA Part 2 (or any DCA section):**
  `filters={"doc_category": "nuscale_DCA", "DocumentTitle": "Part 2"}`.

- **NuScale letter referencing a case number:**
  `filters={"doc_category": "nuscale_Letter", "CaseReferenceNumber": "LO-0719"}`.

## When nothing matches

If a citation in the chunk has no corresponding row (e.g. an external standard
like ASME, IEEE, ANS), **do not invent an id**. Skip it. `emit_references_v2`
validates every `ref_source_id` and silently drops fakes.
