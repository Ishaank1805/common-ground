# Annotation guide and manual review

Read only the prefix ending at `analysis_after_utterance_id`. Label each visible utterance in order. A later clarification may change an earlier utterance's interpretation at a later checkpoint; it cannot be used in the earlier checkpoint's labels or rationale.

## Utterance labels

- **Concerning:** the visible words establish dismissal, exclusion, gender stereotyping, belittling or pressure on personal choice. Use only those concern types and quote exact evidence from that utterance. Explain how the words act in this context.
- **Benign:** neutral inquiry, support, respectful disagreement, a legitimate boundary, apology or explicitly critical reporting of harmful language. Do not flag disagreement, a refusal, self-description or unequal speaking time by itself.
- **Ambiguous:** a missing referent or context permits materially different interpretations. State what is missing. Do not invent coercion, sarcasm, motive, vocal tone or a prior relationship.

Classify the conversation from the checkpoint's labels: any concerning label takes precedence; otherwise any ambiguous label gives ambiguous; otherwise benign. Source offensiveness and toxicity labels are preserved for audit, never substituted for these judgements.

## Facilitator decision

- **suggest:** a visible concern remains unresolved, a participant's request is displaced, or repeated pressure/exclusion continues. Offer one short, neutral invitation focused on participation, choices, evidence or clarification. Avoid diagnosing intent or commanding a personal decision.
- **no_nudge:** no concern needs facilitation, or a concerning remark has already been meaningfully challenged and repaired. A direct participant boundary can stand without facilitator correction. An apology must address the conduct; a bare “sorry you feel that way” would not necessarily resolve it.
- **needs_context:** missing information prevents a confident decision. Explain the question that would resolve it; leave `example_nudge` null, as the plan requires.

Detection does not mandate intervention. In development C001, the stereotype warrants `suggest` at U2, then `no_nudge` after acknowledgement, apology and accepted repair. The concerning utterance still counts toward its category. In C007 the reported housework stereotype is explicitly rejected; it is benign quotation in context. In C010 the capacity explanation resolves the first ambiguity, while a different suitability statement leaves the final checkpoint ambiguous. C002 repeats exclusion after a reasonable boundary, making a prompt more appropriate.

Do not require reference-nudge wording verbatim. Assess whether an alternative prompt is neutral, relevant, concise and leaves the facilitator and participant in control.

## Owner's checklist for each record

- [ ] Read the complete conversation for naturalness and coherent references; check that fictional speakers and replies are clearly recorded.
- [ ] Open its selected `provenance.csv` entry. Compare source snapshot, original labels, source URL, locator and licence; confirm each copied/adapted/added turn is accurately mapped. Inspect AMI's segment order and anonymous speaker map.
- [ ] Review each checkpoint independently using only its visible prefix. Confirm every label, exact excerpt, concern type and rationale; check ambiguous cases for over-flagging.
- [ ] Check category precedence separately from the intervention decision. Confirm any repair is adequate, repeated behaviour matters, and the suggested prompt supports agency.
- [ ] Check both output dialogue and provenance for identifying information. Automated contact-pattern checks do not detect every name or identifying detail.
- [ ] Review textual stance and any fictional delivery cue. Confirm that no acoustic observation is claimed and no scripted cue is assigned to copied real speech.
- [ ] Keep evaluation content out of demos and tuning. Resolve contamination/thread-link uncertainties with the backend team before reporting model performance.
- [ ] Only after these checks, set this record's `review.status` to `checked` and record any corrections in `review.notes`. For evaluation corrections, update the declared lock deliberately before a new run. Re-run validation and tests.

The assistant's self-audit is not your human approval. All 15 statuses remain `pending`; no acoustic listening or field validation has occurred.
