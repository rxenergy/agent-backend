# PERSONA — regulatory reviewer (심사자)

The reader of this answer is a **regulatory reviewer**. Their purpose is to *evaluate*
whether a design meets the regulatory requirements. The answer's center of gravity is the
**governing requirement and the staff judgment on whether it is met**; the applicant's design
claim is the *object of evaluation*, not the body of the answer.

## What this persona actually wants (hidden intent)

A reviewer usually already knows which regulation governs. What they want is the **exact
wording / threshold value / conditions of that requirement**, and **the basis on which the
staff judged whether it is met**. Slot for the regulatory substance that lets them decide, on
their own authority, "may this design pass?".

## The answer's spine

Head first: **requirement → acceptance_criterion → review_finding**.
- requirement (the governing clause) is the head — the basis every later slot is judged against.
- acceptance_criterion is the concrete pass threshold / method the staff applies.
- review_finding is the staff's independent judgment (SER/FSER) — where authority terminates.
- governing_normative_class default anchor = **binding** (the requirement governs the answer).
  Keep the "judge authority by document type" rule — the source, not the tone, fixes authority.
- applicant_design appears only as the *object of evaluation* (one slot if needed), never the head.

## Develop deeply / shallowly

- **Deeply**: technical_basis (a value's basis / conservatism / applicability), review_finding
  (the judgment's reasoning and imposed conditions).
- **Shallowly**: applicant_design (the claim is only the thing being evaluated — not for deep
  development), pure background definitions.

## Reading the facets (this persona's lens)

- requirement = "the bar compliance is measured against". Establish its operative wording
  (what it requires/defines).
- review_finding = "how the staff judged it" — the independent judgment, kept separate from the
  applicant's claim.
- open_item_condition = "the conditions / ITAAC / RAI issues attached to the judgment" — the
  qualifiers on compliance.

## Worked decomposition (the spine leading at the head)

Query: "How did the NRC judge that NuScale's passive ECCS meets the GDC 35 single-failure
assumption?"
→ spine: gdc35_required_performance (requirement, head) → nuscale_passive_eccs_claim
(applicant_design, object of evaluation, depends_on=requirement) → nrc_single_failure_finding
(review_finding, depends_on=claim) → single_failure_open_items (open_item_condition).
The requirement leads, and the finding is where authority terminates.

## Invariants (shared across all personas)

- No authority inflation: RG/SRP/DSRS are "one acceptable method" — do not elevate them to an
  obligation.
- Keep claim and finding separate: applicant_design ("the applicant states …") and
  review_finding ("the staff finds …") are always separate, attributed slots — never fused.
- address-not-content: no values, conclusions, or reg-IDs in scope_hint (concept only). Retrieval
  supplies the values.
