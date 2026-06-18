# PERSONA — reactor designer / licensing practitioner (설계자)

The reader of this answer is a **reactor designer / licensing practitioner**. They are not
trying to *review* a regulation; they want to understand **how a prior applicant (NuScale)
constructed its design and got it licensed through which demonstrations and review dialogue**,
so they can apply it to their own design and licensing strategy.

## What this persona actually wants (hidden intent)

"How did NuScale do this (in their FSAR)?", "What did NuScale and the NRC exchange (RAI/SER)?",
"By what method did they get the design licensed?" — the answer's center of gravity is the
**applicant's design claim + the review dialogue**, and the regulatory requirement is the
*background / evaluative frame* for understanding those design choices.

## The answer's spine

Head first: **applicant_design → demonstration_method → review_finding → open_item_condition**.
- applicant_design (what the applicant designed and how) is the head — keep design vocabulary
  verbatim (RVV/RRV/DHRS/CNV/natural circulation; do not rewrite to active-LWR terms).
- demonstration_method is how that design was shown to comply (analysis method/code/assumptions/
  conservatism).
- review_finding is how the staff judged that design and demonstration (the basis it passed on).
- open_item_condition is the RAI back-and-forth and imposed conditions the applicant and NRC
  exchanged.
- governing_normative_class default anchor = **mixed** (the design claim + review record are the
  body of the answer).
- requirement is a *background context slot*, placed later and only when needed — never the head.

## Develop deeply / shallowly

- **Deeply**: applicant_design (design parameters / passive vocabulary), demonstration_method
  (the showing), open_item_condition (RAI exchange and SER conditions, verbatim).
- **Shallowly**: pure requirement (only background for reading the design — not for deep
  development).

## Reading the facets (this persona's lens)

- applicant_design = "the design the prior applicant chose and its basis" — the body of the answer.
- open_item_condition = "the applicant↔NRC dialogue" — the *two-way* exchange of what the staff
  asked and how the applicant resolved it (not staff-only). Preserve RAI / conditions / ITAAC
  verbatim.
- review_finding = "the basis that design passed on" — a cue for licensing strategy.
- requirement = "the background frame for evaluating the design choice" (not the head).

## Worked decomposition (the designer's head leading)

Query: "How did NuScale design and demonstrate ECCS core cooling without active systems?"
→ spine: nuscale_passive_eccs_design (applicant_design, head) →
single_failure_demonstration (demonstration_method, depends_on=design) →
nrc_eccs_finding (review_finding, depends_on=design·method) →
eccs_open_items (open_item_condition, RAI exchange). The GDC 35 requirement is a background
context slot placed later.

## Invariants (shared across all personas)

- No authority inflation: even with the design claim at the head, keep its wording as "the
  applicant states …" (applicant_claim authority). A designer spine ≠ elevating the applicant's
  claim to fact.
- Keep claim and finding separate: applicant_design and review_finding are always separate,
  attributed slots — never fused.
- address-not-content: no values, conclusions, or reg-IDs in scope_hint (concept only). Retrieval
  supplies the values.
