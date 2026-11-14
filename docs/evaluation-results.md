# Evaluation results

What we tested, how, and what we found, as of 13 November 2026. All live tests used Google's `gemini-3.5-flash-lite`. "3/3" means three repeated runs of the same scenario. Synthetic voices were used where noted; nothing has yet been tested with real participants.

## At a glance

| Area | Result |
|---|---|
| Automated tests | 63 Python tests plus two browser test suites passing at the last full run (also inside the Docker image); 4 more cover follow-up and insights |
| Safety cue on a threat | 12/12 after the server began enforcing it, across every speaker-label condition |
| False safety alarms | 0/12 on interruptions and ordinary concerns |
| Invented claims in cues | 0 after the fix (3/3 runs clean) |
| Voice matching, real voices | 100% correct, 0% wrong across 8 pairs (5 of them two women) |
| Voice matching, similar voices | 0% wrong (labelled "unclear" instead) |
| Prompt speed | about 4 seconds per tap at any conversation length (was 73 seconds after 10 minutes) |
| Sign-in speed with full password hashing | about 0.5 seconds |

## 1. Quiet prompts during the conversation

**First scenario set (12 scenarios).** Prompts stayed quiet by default. An early fee concern was picked up with a relevant cue. A threat produced the safety cue without being asked; respectful disagreement did not. Silence sent nothing to the AI. An instruction hidden in speech ("ignore your rules") was ignored. Hindi speech was understood. Support faded as designed, privacy controls worked, and an invalid API key produced a quiet message, pausing after three failures.

**Problems found, and fixed.**
- Cues sometimes put claims in her mouth that were never said (a scholarship, part-time work). After the fix: 3/3 runs clean.
- Some cues argued the family's side. After the fix: 3/3 on her side.
- "Agree on a next step" was marked as covered in 6 of 9 runs where no next step was discussed. After requiring evidence from the conversation: 0/3 wrongly marked, and 3/3 correctly marked when a step was really agreed.
- Cues for Hindi conversations came back in English (0/3). After adding her choice of prompt language: 3/3 in Devanagari, and her choice overrode the audio language 3/3.

**Still weak.** Hinglish wording often repeated English plan sentences. "Agree on a next step" remains inconsistent in the latest runs: a proposed visit was marked in 2 of 3 runs, and one run marked it when nothing was proposed.

**Speed.** Background checks took 4-7 seconds; taps 4-28 seconds, with occasional "overloaded" errors from Google that the app retries.

## 2. Safety

The most important finding of the project came from testing the safety cue together with voice matching. When any speaker timeline was attached, even a correct one, the model missed a calmly spoken threat ("If you bring this up again, you'll regret it") in 3 of 3 runs. The fix made threat detection a separate yes/no answer that the model gives first, and the server shows the safety message whenever it is yes.

| Threat, with speaker information… | Before | After |
|---|---|---|
| none | 2/3 | 3/3 |
| all "unclear" | 0/3 | 3/3 |
| correct (parent) | 0/3 | 3/3 |
| wrong (her) | 0/3 | 3/3 |

False alarms after the change: 0 of 12 runs across interruptions, an ambiguous proposal and ordinary fee concerns. Being interrupted and told to be quiet produced a "pause" cue in 3/3 runs, not the safety cue.

## 3. Voice matching (experimental, off by default)

**Accuracy.** On SpeechBrain's real test speech, the same person scored 0.66-0.79 against her sample and other people -0.04 to 0.06. On 8 pairs from Mini LibriSpeech (16 real speakers, 5 pairs of two women):

| Method | Real pairs | Similar synthetic voices | Near-identical synthetic voices |
|---|---|---|---|
| Fixed threshold (first version) | 97% right, 0% wrong | 41% wrong | 45% wrong |
| Comparing voices within the conversation (current) | 100% right, 0% wrong | 0% wrong, all "unclear" | 0% wrong, all "unclear" |

Overlapping speech came out "unclear". In real Chrome, with recorded speech as the microphone, every turn of a two-voice conversation was labelled correctly, within about 0.2 seconds of the true boundaries.

**Speed.** A one-minute check went from 7.3 to 1.5 seconds and a tap after 10 minutes from 73 to 4.1 seconds (4.3 seconds after 30 minutes), by computing voice fingerprints in parallel and labelling only the last three minutes. In Docker with no network, the model loads in 2.6 seconds instead of more than 25.

**Limit.** Not yet tested on real families, where relatives may sound more alike than strangers.

## 4. Accounts, privacy and reliability

- Sign-up, recovery code, account panel, sign-out and sign-in all worked in real Chrome at phone size, each taking about half a second with full-strength password hashing.
- Testing found that a form submitted before the page's JavaScript loaded could put a password in the web address. All sign-in and account forms now use POST; retested with no passwords in any address.
- Another person's conversation always returns "not found". Deleting an account removes every conversation, recording, prompt and voice fingerprint.
- Discarding a recording and recording again worked end to end in real Chrome.
- Docker: image of 1.82 GB, tests pass inside it, runs as a non-root user, and works offline.

## 5. What has not been tested yet

- Real participants and real families, which is the most important next step.
- iPhone Safari, and real phones over HTTPS generally.
- Real recordings of relatives' voices for voice matching.
- Hinglish cue quality with a reviewer fluent in how participants speak.
