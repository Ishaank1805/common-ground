// Anonymous insights for NGO staff named in INSIGHT_VIEWERS: counts only, rendered as text.
const LABELS = {
  status: {preparing: 'Being prepared', planned: 'Planned', not_yet: 'Not happened yet', happened: 'Happened, not recorded', recorded: 'Recorded', reflected: 'Reflected on'},
  reasons: {no_right_moment: 'Didn’t find the right moment', felt_nervous: 'Felt too nervous', family_busy: 'Family wasn’t available', felt_unsafe: 'Didn’t feel safe', changed_mind: 'Changed her mind', other: 'Something else'},
  assessments: {achieved: 'Achieved', partly_achieved: 'Partly achieved', not_achieved: 'Not achieved'},
  modes: {auto: 'As we go', on_request: 'When I ask'},
  kinds: {next_point: 'Next point', respond_to_concern: 'Respond to a concern', return_to_goal: 'Come back to the goal', pause: 'Pause', close: 'Close', step_away: 'Safety: take a break'},
};

function paragraph(text) {
  const element = document.createElement('p');
  element.textContent = text;
  return element;
}

function section(title, counts, labels) {
  const wrap = document.createElement('section');
  const heading = document.createElement('h3');
  heading.textContent = title;
  const list = document.createElement('ul');
  const entries = Object.entries(counts || {});
  for (const [key, value] of entries.length ? entries : [[null, null]]) {
    const item = document.createElement('li');
    item.textContent = key ? `${labels[key] || key}: ${value}` : 'None yet';
    list.append(item);
  }
  wrap.append(heading, list);
  return wrap;
}

export async function showInsights() {
  const target = document.querySelector('#insights-body');
  target.replaceChildren(paragraph('Loading…'));
  document.querySelector('#insights-dialog').showModal();
  try {
    const response = await fetch('/api/insights');
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    if (!data.enough) {
      target.replaceChildren(paragraph(`Not enough participants have chosen to share yet. Insights appear once at least ${data.minimum_people} people share.`));
      return;
    }
    target.replaceChildren(
      paragraph(`${data.people} participants are sharing, with ${data.conversations} conversations between them.`),
      section('Where conversations stand', data.status, LABELS.status),
      section('What got in the way', data.not_yet_reasons, LABELS.reasons),
      section('How goals went, in their own assessment', data.goal_assessments, LABELS.assessments),
      section('Quiet prompts: conversations by help mode', data.prompts.modes, LABELS.modes),
      section('Quiet prompts: cues shown', data.prompts.kinds, LABELS.kinds),
      paragraph(`Conversations with quiet prompts: ${data.prompts.conversations_with_prompts}. Conversations where the safety cue appeared: ${data.prompts.conversations_with_safety_cue}.`),
    );
  } catch {
    target.replaceChildren(paragraph('Insights could not be loaded. Please try again.'));
  }
}
