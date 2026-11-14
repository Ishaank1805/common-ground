# Privacy and consent: a summary for Umeed for Women

This explains, in plain language, what Common Ground collects, where it goes, what each participant controls, and the decisions we need from Umeed for Women before a pilot. It describes the app as built on 13 November 2026. It is not legal advice; please have it reviewed before real participants use the app.

## What the app keeps

| What | Why | Who can see it |
|---|---|---|
| Account: username, name, pronouns, optional gender | So each person has her own private space | Only her |
| Password | Signing in | No one: stored only as a scrypt hash, which cannot be turned back into the password |
| Preparation, plan, reflection, AI summary | The core of the app | Only her |
| Recording of the conversation | So she can listen back and, if she asks, get a summary | Only her |
| Quiet prompts: the cues shown, short neutral notes about topics, goals raised | So she can review what was suggested | Only her |
| Follow-up: whether the conversation has happened, what got in the way, a date, a private note | To support her next try | Only her |
| Voice fingerprint (optional, experimental, off by default) | To tell her voice from her family's | No one: 192 numbers kept on our server, never the audio |
| Her choice to share anonymous insights (off by default) | Helping the NGO improve support | NGO staff see counts only (see below) |

Nothing is used for advertising, and nothing is shared with anyone else.

## What is sent to Google Gemini, and when

Common Ground uses Google's Gemini AI. Data goes to Google only at these moments:
- **Planning:** when she asks for a plan, her preparation text is sent.
- **Summary:** only when she presses "Generate summary", her reflection and, if there is one, the recording are sent.
- **Quiet prompts:** only if she switches them on for that conversation. "As we go" sends the latest minute of audio about every 45 seconds; "When I ask" sends the conversation so far when she taps. Silence is never sent.
- **Instant prompts (experimental):** only if she also ticks "Instant prompts" for that conversation, the whole conversation streams to Google while prompts are on, so cues arrive in about a second. Gemini's spoken replies are never played or kept.

The voice sample is never sent to Google: voice matching runs on our own server. Before a pilot, the NGO should confirm Google's data terms for the API plan used, including whether data may be used to improve Google's products.

## Consent built into the app

- **Quiet prompts are off for every new conversation.** She chooses them each time, after a short explanation of what is sent.
- **Recording:** the app reminds her to let everyone know before she starts recording. Her family members' voices are in the recording.
- **Voice sample:** optional, clearly marked experimental, and only available if the server switches it on.
- **Anonymous insights:** off unless she ticks "Share anonymous insights" in Account, and she can stop at any time.

## What she controls

- Delete any conversation, with its recording and prompts.
- Discard a recording and record again.
- Delete her voice sample.
- Stop sharing insights.
- Delete her account, which removes everything above.
- Sign out on shared phones. There is no "keep me signed in", and she is signed out after 30 minutes without activity.

## Anonymous insights for the NGO

If a participant chooses to share, the NGO sees counts across everyone who shares: where conversations stand, what got in the way, how goals went in participants' own assessment, and how quiet prompts were used, including how often the safety cue appeared.

The NGO never sees names, topics, words, plans, notes, reflections or recordings. Nothing is shown until at least five people share, and any count below three appears only as "fewer than 3", so no one can be singled out. Only staff whose usernames the NGO lists can see insights.

## Security in brief

- Accounts follow OWASP and NIST guidance: passwords of at least 15 characters, sign-in paused after repeated failures, and server-side sessions that end after 30 minutes of inactivity or 12 hours.
- A one-time recovery code replaces email, since participants may not have private email.
- Each person's data is separated; anyone else asking for it gets "not found".
- The app should be served over HTTPS so phones can record safely.

## Decisions we need from Umeed for Women

1. **Participants under 18.** India's Digital Personal Data Protection Rules, 2025 require verifiable consent from a parent or guardian before processing the personal data of anyone under 18, and bar tracking or behavioural monitoring of children; most of these obligations take effect in mid-2027. Many participants may be under 18, and the app's purpose is a conversation with those same parents. Options include limiting the pilot to participants aged 18 and over, or collecting guardian consent through the NGO's own enrolment. This needs legal advice.
2. **Family members in recordings.** What should participants say to family before recording, and should recording ever be optional for certain conversations?
3. **How long recordings are kept.** Today they stay until she deletes them. A default retention period (for example, automatic deletion after a set time) may be safer.
4. **Safety message wording.** The safety cue and helplines (112, and the Women Helpline on 181) are in English only. We need NGO-approved Hindi wording.
5. **Who sees insights,** and how that list is kept up to date.
6. **Where the app is hosted,** and who has administrator access to the server.
7. **Google's terms** for the Gemini plan used.

## Sources

- India's DPDP Rules, 2025 and parental consent for under-18s: India Briefing (india-briefing.com/news/dpdp-rules-2025-india-data-protection-law-compliance-40769.html), Observer Research Foundation on the 18-month phased roll-out (orfonline.org/expert-speak/dpdp-rules-and-the-future-of-child-data-safety), Mondaq regulatory update (mondaq.com/india/data-protection/1713500).
