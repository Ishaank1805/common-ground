# Hackathon 1 conversation dataset

This dataset supports facilitators discussing gender roles, education, work, family expectations and personal decisions. It tests whether a text-based system can identify a concern and decide whether a constructive prompt would help. A suggested prompt goes to the facilitator, who retains judgement; it is not an automatic intervention.

There are exactly **15 English conversations**, the contribution assigned to one member in [dataset-plan.md](../../dataset-plan.md). There are 75 utterances and 18 annotation checkpoints. All records await the owner's manual review. This is a small behavioural test set, not evidence of field performance.

| Final category | Development | Evaluation | Total |
|---|---:|---:|---:|
| Concerning | 4 | 2 | 6 |
| Benign | 3 | 2 | 5 |
| Ambiguous | 3 | 1 | 4 |
| Total | 10 | 5 | 15 |

| Final decision | Development | Evaluation | Total |
|---|---:|---:|---:|
| suggest | 3 | 1 | 4 |
| no_nudge | 4 | 3 | 7 |
| needs_context | 3 | 1 | 4 |

Final utterance labels: **59 benign, 10 concerning, 6 ambiguous**. Earlier checkpoints add six benign, one concerning and one ambiguous label occurrence; they are prefix reannotations, not additional conversations. Two concerning conversations end with `no_nudge` because the concern has been addressed. Concern types overlap: gender stereotyping 8, belittling 4, exclusion 2, pressure on choice 4, dismissal 1.

## Sources and construction

Sources were accessed on **14 October 2026**. The provenance shortlist contains **30 candidate records or dialogue segments**: 12 Social Bias Frames posts, 12 Civil Comments records and 6 AMI exchanges. Compact source archives were scanned to identify candidates; this was not a random sample or a claim that only 30 raw rows were downloaded. Rejected candidates remain in the manifest with no selected conversation ID.

| Source | Final conversations | Development / evaluation | Retrieved release |
|---|---:|---:|---|
| Social Bias Frames | 5 | 4 / 1 | Official SBIC.v2 archive; original validation/test splits |
| Civil Comments | 6 | 3 / 3 | `google/civil_comments` mirror, revision `f2970eb3a55777454c94069077cc8d9b5866312d` |
| AMI Meeting Corpus | 3 | 2 / 1 | Official manual annotations 1.6.2 |
| Wholly synthetic | 1 | 1 / 0 | C010: staged clarification and a separate unresolved reference |

The source plan was assigned before adaptation: SBIC validation and Civil validation candidates went to development; their test candidates went to evaluation. AMI session ES2002 is development and ES2003 is evaluation. The manifest records these assignments for rejected candidates too. The synthetic case fills a specific coverage gap explained in `review.notes`.

Social Bias Frames and Civil Comments are posts/comments, **not observed conversations**. Their speakers and replies are constructed. Each example derives from one source record; unrelated posts are never joined into an apparent conversation. Rewording, contextual changes, authored pressures, quoted criticism, added replies and repair are explicitly recorded in each source's `changes` object. Most output is adaptation: 13 adapted conversations, one source excerpt and one synthetic conversation. Source labels remain reference material and do not determine the final annotation.

AMI uses contiguous lexical segments extracted from per-speaker XML, sorted by transcriber onset and speaker for ties. Nonlexical-only events are omitted and punctuation spacing is normalized. Segment IDs, word-ID endpoints, speaker letters and source intervals remain in the manifest. Anonymous P IDs preserve the actual source speaker relationships. Overlap remains possible: onset order is a serialization, not a claim that audio turns never overlap. Two exchanges change subject matter to course access while retaining speaker relationships and segment order; the short missing-context excerpt retains the original text. No audio was downloaded or listened to. The [official scenario-only partition](https://groups.inf.ed.ac.uk/ami/corpus/datasets.shtml) places ES2002 in training and ES2003 in development; these partition-qualified values are preserved in `source_split`. The alternative Full-corpus-ASR partition moves ES2003 into training, which is an explicit contamination risk if the tested model used it.

The Civil mirror omits native comment IDs, parent IDs and thread links. Its `record_id` values such as `validation:row:13` are **explicit row locators within the pinned mirror**, not native platform IDs. No parent context or observed speaker identity is claimed. Original toxicity scores are annotator fractions, not probabilities that an intervention is needed. The named addressee in one source was excluded from both its stored audit excerpt and adapted text. Other selected candidates contain no personal names, handles or contact details.

## Files and format

- `development.jsonl`: 10 complete conversations for development and demonstrations.
- `evaluation.jsonl`: 5 held-out gold conversations; use only in the evaluation harness.
- `evaluation.sha256`: byte-level lock for evaluation data, created before any model tuning.
- `tone_annotations.jsonl`: 75 companion entries, one per utterance; never required by text inference.
- `provenance.csv`: 30 privacy-safe source snapshots, original labels, stable locators, licences, source-to-utterance mappings, adaptation descriptions, split assignments and selection notes. Structured cells contain JSON; multiline CSV cells are intentional.
- `annotation_guide.md`: decision rules, edge cases and manual review checklist.
- `validate.py`: offline validator and optional download-based source audit.
- `export_inputs.py`: allowlist-only export, including optional prefix truncation.
- `test_validation.py`: automated checks with malformed development-record copies.
- `role_play/scripts.md`: four clearly fictional delivery exercises, outside model input.
- `.gitignore`: excludes local Python caches, source-cache folders and common audio files.

Conversation fields match the plan exactly: `transcript_id`, `split`, `origin`, `speaker_mapping`, `language`, `sources`, `utterances`, `annotations`, `review`. Utterances have `utterance_id`, `sequence`, `speaker_id`, `text`. Source objects have the plan's dataset/version/record/split/URL/licence fields, mapped utterance IDs, `changes` and `source_labels`. Use `null` for unavailable version or split metadata in future records; the releases and partitions used here are identified.

`changes` describes copied, adapted and added utterance IDs separately, with a plain description and a source speaker map only for AMI. This elaborates an existing field; it adds no required conversation fields. `source_labels` stores original SBIC annotation rows without annotator identities, the seven Civil scores, or `{}` for AMI, which supplies no concern labels for these excerpts.

Every checkpoint identifies the last visible utterance, labels exactly the visible prefix, and provides category, decision, rationale and nudge. A concerning utterance requires an exact evidence substring and an allowed concern type. Category precedence is concerning, then ambiguous, then benign. `suggest` requires a short neutral prompt; both other decisions require `null`. Review remains `pending` until you check the record.

For example, in development C001, “for a woman you've got a backbone” is concerning. Immediately afterward, a prompt to describe skills without comparing genders is warranted. Later the participant challenges the comparison, the speaker apologises and the participant chooses to move on. The conversation still contains a concerning utterance, but its final decision is `no_nudge`.

## Tone and later audio work

`textual_stance` uses supportive, neutral, confrontational or unclear, with a word-based explanation. It does not label a firm boundary abusive or identify emotions from punctuation. Two newly authored turns have optional `scripted_delivery` cues with `basis: scripted_or_simulated`. These are performance instructions for fictional scenarios, never reports about real speakers.

Every entry has `observed_acoustic_delivery: not_observed` and `audio_evidence: null`. Nothing here establishes loudness, aggression, pitch, speaking rate, calmness or sarcasm acoustically. The role-play exercises vary intended delivery and include a sarcasm performance note, but supply no observed labels or recordings.

Future audio work should store reviewed audio URL or file reference, aligned start/end seconds, channel, annotation method and reviewer alongside observed delivery labels in a separately versioned companion schema. Cue names alone are not acoustic measurements. Keep the existing text-only gold and its evaluation fixed, and assess an audio-assisted condition separately. The current validator intentionally rejects observed acoustic claims: extend the companion validator only when real evidence exists. No acoustic classifier is included.

## Attribution and licences

- **Social Bias Frames / SBIC**, Sap, Gabriel, Qin, Jurafsky, Smith and Choi (2020), [dataset and archive](https://homes.cs.washington.edu/~msap/social-bias-frames/), [Hugging Face dataset card](https://huggingface.co/datasets/allenai/social_bias_frames): [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- **Civil Comments archive and Jigsaw annotations**, [official TFDS description](https://www.tensorflow.org/datasets/catalog/civil_comments), [Google mirror](https://huggingface.co/datasets/google/civil_comments): [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).
- **AMI Consortium**, Carletta (2006), [overview](https://groups.inf.ed.ac.uk/ami/corpus/overview.shtml), [official download release](https://groups.inf.ed.ac.uk/ami/download/), [current corpus licence](https://groups.inf.ed.ac.uk/ami/corpus/license.shtml): [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Version 1.6.2 carries the 2017 licence change; older noncommercial notices do not describe this retrieved release.

Retain this attribution, manifest, licence links and change descriptions when redistributing sourced portions. The licences permit reuse and adaptation under their terms and provide no warranties; they do not imply endorsement or settle unrelated privacy rights. No blanket change to the repository's licence is made for newly authored content or code. The team should establish terms for those contributions before wider redistribution.

## Validation and safe consumption

Python 3.10+ and the standard library are sufficient. From the repository root:

```powershell
python data/hackathon1/validate.py
python -m unittest discover -s data/hackathon1 -p test_validation.py -v
# Optional: public network access, about 31 MiB of source archives plus small JSON pages.
python data/hackathon1/validate.py --verify-sources
# Temporary development input only; keep gold and provenance out of model prompts.
python data/hackathon1/export_inputs.py data/hackathon1/development.jsonl "$env:TEMP/umeed-development-inputs.jsonl"
```

The pilot used C001, C005 and C008 and passed before the remaining records were written. `--pilot` is for that initial three-record stage, not the completed ten-record file. Offline validation checks counts, schemas, enums, sequence, speaker relationships, checkpoints, evidence, nudge nullability, provenance agreement and hashes, split grouping, duplicates, tone and the evaluation lock. Near-duplicate checks use conversation sequence similarity ≥0.82, token-set Jaccard ≥0.75 and substantive-turn similarity ≥0.90 for turns of at least eight words. These heuristics do not prove absence of semantic duplicates.

`--verify-sources` re-fetches the official SBIC/AMI archives in memory and the two 100-row Civil viewer pages. It verifies all 30 shortlisted source texts, labels, IDs/locators and AMI ordered segments. It checks the Civil mirror revision before and after retrieval; if it has changed, verification stops rather than silently treating new row indices as old records. Retrieve the recorded commit's parquet for a future audit in that case. Nothing downloaded by this command is written to Git.

At handoff, all 30 candidates passed source verification using the original successful downloads kept outside Git, with live checks of the Civil revision and AMI partition. A redundant full-download refresh stalled and was stopped. To reuse those downloads, add `--source-cache "$env:TEMP/umeed_sources"`; it expects files `sbf`, `ami`, `civil.json` and `civil_test.json`. That temporary folder is not shipped and may be deleted by the operating system. Without it, use the normal network audit. Offline validation and all 14 test groups also passed, and the input-only development export was exercised.

No backend code or inference interface existed in the inspected checkout. The export therefore follows the plan's identifiers-and-utterances contract: only `transcript_id` and an array of `utterance_id`, `sequence`, `speaker_id`, `text`. It excludes categories, source labels, provenance, review, tone and expected answers. Use `input_only(record, after='U2')` to export a prefix; never include later turns at an earlier checkpoint. The backend team must confirm its eventual request shape, checkpoint support and handling of `needs_context`; no application interface was changed.

## Evaluation integrity and limitations

The evaluation file is locked; no model prompting, classification, prompt tuning or demo use was performed. Automated validation may read gold for integrity checks, which is distinct from sending it to a model. Structural tests use development records for malformed fixtures. Do not use evaluation examples for prompts or rehearsals. If manual review corrects evaluation content or changes its review status, explicitly version that correction and update the lock before a fresh evaluation; never quietly rewrite gold to match predictions.

SBIC and Civil examples use original validation/test records. AMI development examples use original scenario-training material; its evaluation adaptation uses original scenario-development material, also classed as training in Full-corpus-ASR. The tested model and its training data are unknown, so public-source pretraining contamination cannot be ruled out, even for original test splits. If the tested model trained on these records, exclude affected evaluation examples from a performance claim and prepare a separately versioned replacement before tuning. Native Civil thread links are absent, so separation of unknown linked threads cannot be certified; exact and near-text matches and known source groups are checked. Obtain model-training declarations and raw thread metadata before claiming contamination-free results.

All conversations were constructed and self-audited with assistant help; there is no independent human annotation agreement yet. Adapted online English and AMI design meetings are limited proxies for sensitive discussions in Umeed's setting. Real-world validation needs consented contextual conversations, facilitator review, local language and cultural coverage, privacy controls, disagreement analysis and observations of whether nudges help. This dataset establishes neither deployment safety nor measured accuracy.
