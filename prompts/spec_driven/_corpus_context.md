# CORPUS CONTEXT (how the corpus is structured — shared across all nodes)

This is the single source of truth for the shared "CORPUS CONTEXT" block. The
prompt registry has no include mechanism (each prompt is one sha256-verified
file), so the block below is **inlined verbatim** into every spec_driven_v1 node
prompt: triage_v1.md (N0), answer_spec_v1.md (N1), query_formulation_v1.md (N2),
generation_v1.md (N4). When you change the block, update all four files and bump
their registry sha256.

Design: docs/plans/spec_driven_search_scope_metadata.design.v1.md §0/§7.0.

---8<--- BLOCK START (copy the lines between the markers into each node prompt) ---8<---

## CORPUS CONTEXT — how the corpus is organized (read this to scope correctly)

The corpus splits along two axes that mirror the NRC document lifecycle. Knowing
why lets you both scope retrieval correctly and *explain* that scoping.

- **Regulatory documents — organized by currency (status), NOT by reactor design.**
  Federal regulation (`10CFR`), the Federal Register (`FR`), Regulatory Guides
  (`RG`), Standard Review Plans (`SRP`, NUREG-0800), and NuScale's Design-Specific
  Review Standard (`DSRS`) are *common norms* that apply to every applicant. A norm
  is amended over time, so a `current` edition coexists with `history` / `draft` /
  `withdrawn` editions (e.g. RG 1.206 Rev 0/1/…). What matters is *which edition is
  in force*, not which plant. → Use **status** to scope these. They have no design.
- **NuScale applicant/review documents — organized by design, NOT by currency.**
  NuScale submitted **two distinct designs** to the NRC, and each has its own full
  set of `nuscale_*` documents (FSAR, DCA, RAI, SER, …):
  - **US600** — the original NuScale Power Module (~50 MWe/module), submitted as a
    **Design Certification Application (DCA)**, Docket 05200048 (design certified 2020).
  - **US460** — the later NuScale Power Module-20 (uprated ~77 MWe/module), submitted
    as a **Standard Design Approval Application (SDAA)**, Docket 05200050. A *separate*
    design built on US600 with power/design changes.
  - **PreApp** — pre-application-stage documents that predate the DCA.
  Mixing the designs' figures (different power/thermal-hydraulic conditions) is an
  error. → Use **design** (`US600` / `US460` / `PreApp`) to scope these. Applicant
  submissions are not norms, so they carry no regulatory `current/history` status.

**The two axes are mutually exclusive:** status only exists on RG/SRP/DSRS;
design only exists on NuScale documents. A status filter on a NuScale document, or a
design filter on a regulatory document, matches an empty field and returns nothing.

**`10CFR` is stored as govinfo annual-edition volumes that bundle many Parts** (vol1 =
Parts 1–50, vol2 = Parts 51–199; vol3+ are DOE, not nuclear). A citation like "10 CFR
50.46" is **Part 50** inside vol1; retrieval narrows to that Part's page span within the
~1000-page volume rather than the whole bundle. Explain this when scope shapes the answer
(e.g. "scoped to 10 CFR Part 50 within the Title 10 vol1 annual edition").

**Defaults (apply unless the query says otherwise):** for a regulatory document the
current edition (`status=current`); for a NuScale document the certified baseline
design (`design=US600`, the DCA) — it is the established reference design, so absent
any stated design it is the reasonable basis. State this basis when it shapes the
answer (e.g. "design unspecified, so US600 (DCA) was used; US460 (SDAA) is a separate
later design"; "current-edition RG").

---8<--- BLOCK END ---8<---
