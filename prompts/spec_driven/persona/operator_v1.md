# PERSONA — plant operator / operations practitioner (운영자)

The reader of this answer is an **operations practitioner running an already-licensed plant**.
They are not reviewing a design or constructing a new one; they want to know **what must be done
to operate safely while staying in compliance with the licensing conditions**.

## What this persona actually wants (hidden intent)

"What do I have to do to stay in compliance?" — the *obligations that must be satisfied before
and during operation* (limiting conditions for operation (LCO), surveillance requirements,
maintenance, ITAAC / license conditions) and their regulatory basis. The answer is
**action-oriented** (what must be done, under which condition), and the requirement is the basis
for *why* the obligation exists.

## The answer's spine

Head first: **applicability → open_item_condition → requirement**.
- applicability (under which operating condition / plant state / licensing stage / edition in
  force the obligation binds) is the head — what the operator needs first is "*when* does this
  apply".
- open_item_condition is the items that must be *satisfied* — ITAAC / license conditions /
  limiting conditions for operation (Tech Specs = FSAR Ch16, license condition = SER). Preserve
  verbatim.
- requirement is the regulatory basis of that obligation (the basis, not the head).
- governing_normative_class default anchor = **mixed** (applicability + imposed obligation + basis).

## Develop deeply / shallowly

- **Deeply**: applicability (operating condition / scope of application), open_item_condition
  (ITAAC / conditions / surveillance requirements verbatim — the items to satisfy before and
  during operation).
- **Shallowly**: design demonstration detail (background to the operator) — applicant_design /
  demonstration_method are not for deep development.

## Reading the facets (this persona's lens)

- applicability = "under which operating mode / condition / stage this obligation is in force" —
  the head of the answer.
- open_item_condition = ITAAC / COL action item / license condition / LCO = **"the obligations to
  satisfy before and during operation"** (read as operational-compliance items, not as unresolved
  design issues). Preserve verbatim.
- requirement = "the regulatory basis of that operational obligation" (not the head).

## Worked decomposition (the operator's head leading)

Query: "What limiting conditions for operation and surveillance requirements must be met to
operate the NuScale ECCS?"
→ spine: eccs_applicability (applicability, head — in which mode it binds) →
eccs_lco_surveillance (open_item_condition, Tech Specs / conditions verbatim,
depends_on=applicability) → eccs_requirement_basis (requirement, basis, background). Applicability
leads, the obligations to satisfy are the body.

## Corpus note (operator-specific)

Tech Specs live in `nuscale_FSAR` Part 2 Ch 16, and license conditions live in `nuscale_SER`.
Those documents must be present in the index for this persona to be effective (absent them, it
falls back to evidence-gap).

## Invariants (shared across all personas)

- No omission of safety-critical content: even with a tech-spec / condition-centric spine, do not
  drop the governing requirement — the spine is a head ordering, not an exclusion list. The
  operator still sees the binding basis.
- No authority inflation / keep claim and finding separate / address-not-content (concept only,
  retrieval supplies the values) — maintained.
