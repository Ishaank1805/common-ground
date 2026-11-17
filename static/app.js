import {showDeckIn} from './deck.mjs';
import {renderSlides} from './conversation-deck.mjs';
import {LiveStream} from './livestream.mjs';
import {showInsights} from './insights.mjs';
import {preparationFromFields, peopleBoxes, samePreparation, evaluationComplete, formatDuration} from './data.mjs';
import {ConversationRecorder} from './recorder.mjs';
import {LiveFacilitator, nextPlannedPoint} from './facilitator.mjs';
import {ScreenAwake} from './wake.mjs';
import {Account} from './account.mjs';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
let session = null;
let busy = false;
let stage = 'prepare';
let dirty = new Set();
let nextGoal = 1;
let recordingUrl = null;
let lastBlob = null;
let config = null;
let account = null;
// Quiet prompts are chosen per conversation and always start off.
let promptMode = 'off';
let instantPrompts = false; // Gemini Live, chosen for each conversation.
let liveStream = null;
let instantCue = null; // Her next planned point, shown at once on a tap until the AI cue arrives.
let liveCue = null; // Last concrete cue. A null or filler result must not replace it with status chatter.
const PROMPT_LANGUAGES = {en: 'en', hi: 'hi', hinglish: 'hi-Latn'}; // choice → lang attribute for the cue
// The prompt language is a standing preference remembered on this device; consent (the mode) is not.
let promptLanguage = (() => { try { const saved = localStorage.getItem('cg-prompt-language'); return PROMPT_LANGUAGES[saved] ? saved : 'en'; } catch { return 'en'; } })();
let promptHistory = [];
let promptHistoryFor = null;
let renderedHistory = null;
let sessionSummaries = [];
const EARLY_CONVERSATIONS = 3;
const PROMPT_LABELS = {next_point: 'You could bring up', respond_to_concern: 'If you want to respond', return_to_goal: 'To come back to your point', pause: 'If you need a moment', close: 'When you’re ready to close', step_away: 'Take a break'};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function notice(message, error = false) {
  const element = $('#notice');
  element.textContent = message;
  element.className = `notice${error ? ' error' : ''}`;
  element.hidden = !message;
}
// A 401 means her sign-in ended (idle or expired): ask her to sign in again without losing this page.
async function authedFetch(url, options) {
  const response = await fetch(url, options);
  if (response.status === 401) account?.expired();
  return response;
}
async function api(path, method = 'GET', data) {
  const options = {method};
  if (data !== undefined) { options.headers = {'Content-Type': 'application/json'}; options.body = JSON.stringify(data); }
  const response = await authedFetch(`/api${path}`, options);
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || `Could not complete this action (${response.status}).`);
  return result;
}
async function task(action) {
  if (busy) return;
  busy = true;
  document.body.classList.add('busy');
  notice('');
  updateLocks();
  try { await action(); } catch (error) { notice(error.message || 'Something went wrong. Your input is still here.', true); }
  finally { busy = false; document.body.classList.remove('busy'); updateLocks(); }
}
function updateLocks() {
  const blocked = busy || recorder.pending;
  $('#new-session').disabled = blocked;
  $('#delete-session').disabled = blocked;
  $$('.session-item').forEach(button => button.disabled = blocked);
  ['#go-record', '#go-reflect', '#back-prepare', '#back-record'].forEach(selector => $(selector).disabled = blocked);
  const locked = Boolean(session?.recording || session?.evaluation?.revision > 0);
  $('#preparation-fields').disabled = busy || locked || recorder.pending;
  $('#preparation-locked').hidden = !locked;
  ['#plan-button', '#save-preparation', '#save-plan', '#reject-plan', '#edit-plan'].forEach(selector => $(selector).disabled = blocked || locked);
  $$('#chosen-plan-form input, #chosen-plan-form textarea').forEach(input => input.disabled = busy || locked || recorder.pending);
  $$('#clarifications input, #clarifications button').forEach(input => input.disabled = busy || locked);
  $$('#evaluation-form button, #evaluation-form input, #evaluation-form textarea, #evaluation-form select').forEach(input => input.disabled = busy);
  $('#generate-summary').disabled = blocked || !session;
  $('#save-summary').disabled = busy;
  $('#summary-text').disabled = busy;
  $('#start-recording').disabled = busy || !session?.chosen_plan || Boolean(session?.recording) || recorder.pending;
  $('#stop-recording').disabled = recorder.state !== 'recording';
  $('#retry-recording').disabled = recorder.state === 'saving';
  renderStageNavigation();
  renderFacilitator();
}
function renderStageNavigation() {
  const completed = session?.evaluation.state === 'completed';
  $('#stage-tabs').hidden = !completed;
  $('#stage-progress').hidden = completed;
  $$('.stage-navigation').forEach(element => element.hidden = completed);
  $$('#stage-tabs button').forEach(button => {
    button.disabled = busy || recorder.pending;
    if (button.dataset.stage === stage) button.setAttribute('aria-current', 'step');
    else button.removeAttribute('aria-current');
  });
}
// The NGO slides tab: slides written by Gemini from this conversation (POST /slides), checked by the server and
// kept with it, then drawn here (conversation-deck.mjs). She makes them, and can make them again after changes.
const SLIDES_INTRO = 'Gemini makes a short presentation about this conversation for Umeed for Women: your goals, plan, the cues you saw, your self-evaluation and next steps. It may only use what you wrote and what the app counted, and any quote is checked word for word against your own writing. Your preparation, self-evaluation and cue history are sent to Google Gemini; the recording is not.';
function showConversationSlides() {
  const made = session?.slides;
  $('#deck-inline').hidden = !made;
  $('#make-slides').textContent = made ? 'Make them again' : 'Create slides with Gemini';
  if (!made) { $('#slides-status').textContent = SLIDES_INTRO; return; }
  const outdated = made.source_revision !== session.evaluation.revision;
  $('#slides-status').textContent = `Made by Gemini from this conversation on ${new Date(made.created_at).toLocaleString()}.`
    + (outdated ? ' Your self-evaluation has changed since: make them again to include it.' : '');
  showDeckIn($('#deck-inline'), {key: `conversation:${session.id}:${made.created_at}`,
    title: `${session.preparation?.topic || 'Her conversation'}: slides for Umeed`, slides: renderSlides(made.slides)});
}
function setStage(name) {
  stage = name;
  $$('.stage-panel').forEach(panel => panel.hidden = panel.id !== `${name}-stage`);
  const labels = {prepare: 'Step 1 of 3 · Prepare', record: 'Step 2 of 3 · Record', reflect: 'Step 3 of 3 · Self-evaluate', slides: 'NGO slides'};
  $('#stage-progress').textContent = labels[name];
  renderStageNavigation();
  if (name === 'slides') showConversationSlides();
  $(session?.evaluation.state === 'completed' ? '#stage-tabs' : '#stage-progress').scrollIntoView({block: 'start'});
}
function markDirty(kind) {
  dirty.add(kind);
  if (kind === 'evaluation' && session?.summary) $('#summary-outdated').hidden = false;
}
function canLeave() {
  if (recorder.pending) { notice('Finish saving your recording before changing conversations.', true); return false; }
  return !dirty.size || window.confirm('You have unsaved changes. Leave this conversation without saving them?');
}
function appendPerson(value = '', {after = null} = {}) {
  if ($('#audience').children.length >= 30) { notice('You can add up to 30 people.', true); return null; }
  const row = node('div', 'entry-row person-row');
  const input = node('input');
  input.type = 'text';
  input.value = value;
  input.maxLength = 200;
  input.placeholder = 'One person';
  input.autocomplete = 'off';
  input.setAttribute('aria-label', 'Person you’re speaking to');
  const remove = node('button', 'remove-button', '×');
  remove.type = 'button';
  remove.setAttribute('aria-label', 'Remove person');
  remove.addEventListener('click', () => {
    row.remove();
    if (!$('#audience').children.length) appendPerson();
    preparationChanged();
  });
  row.append(input, remove);
  if (after?.parentElement === $('#audience')) after.after(row);
  else $('#audience').append(row);
  return input;
}
function separatePerson(input, commit = false) {
  const boxes = peopleBoxes(input.value, commit);
  if (boxes.length === 1 && boxes[0] === input.value) return;
  const row = input.closest('.entry-row');
  const room = 30 - $('#audience').children.length;
  input.value = boxes[0];
  let previous = row;
  boxes.slice(1, 1 + Math.max(room, 0)).forEach(name => {
    const created = appendPerson(name, {after: previous});
    if (created) previous = created.closest('.entry-row');
  });
  if (boxes.length > room + 1) notice('You can add up to 30 people.', true);
  if (boxes.length > 1) {
    const focus = previous.querySelector('input');
    focus.focus();
    focus.setSelectionRange?.(focus.value.length, focus.value.length);
  }
}
function appendEntry(listId, value = '', goalId) {
  if ($(`#${listId}`).children.length >= 30) { notice('You can add up to 30 entries in each list.', true); return null; }
  const row = node('div', 'entry-row');
  if (listId === 'goals') row.dataset.goalId = goalId || `g${nextGoal++}`;
  const input = node('textarea');
  input.rows = 1;
  input.value = value;
  input.maxLength = 2000;
  input.setAttribute('aria-label', listId === 'goals' ? 'Goal' : listId === 'positives' ? 'What might go well' : 'Possible barrier');
  input.placeholder = listId === 'goals' ? 'What would make this conversation worthwhile?' : listId === 'positives' ? 'Something you think may help…' : 'Something that might make it difficult…';
  const remove = node('button', 'remove-button', '×');
  remove.type = 'button';
  remove.setAttribute('aria-label', `Remove ${listId === 'goals' ? 'goal' : 'expectation'}`);
  remove.addEventListener('click', () => {
    row.remove();
    if (!$(`#${listId}`).children.length) appendEntry(listId);
    preparationChanged();
  });
  row.append(input, remove);
  $(`#${listId}`).append(row);
  return input;
}
function preparationChanged() {
  markDirty('preparation');
  $('#clarifications').hidden = true;
  $('#clarifications').replaceChildren();
  $('#plan-panel').hidden = true;
}
function getPreparation() {
  return preparationFromFields($$('#audience input').map(input => input.value), $('#topic').value,
    $$('#goals .entry-row').map(row => ({id: row.dataset.goalId, description: row.querySelector('textarea').value})),
    $$('#positives textarea').map(input => input.value), $$('#barriers textarea').map(input => input.value));
}
function fillPreparation() {
  const preparation = session?.preparation;
  $('#audience').replaceChildren();
  (preparation?.audience?.length ? preparation.audience : ['']).forEach(name => appendPerson(name));
  $('#topic').value = preparation?.topic || '';
  ['goals', 'positives', 'barriers'].forEach(id => $(`#${id}`).replaceChildren());
  nextGoal = Math.max(0, ...(preparation?.goals || []).map(goal => Number(goal.id.replace(/^g/, '')) || 0)) + 1;
  if (preparation?.goals.length) preparation.goals.forEach(goal => appendEntry('goals', goal.description, goal.id)); else appendEntry('goals');
  const positives = preparation?.expectations.what_might_go_well || [];
  const barriers = preparation?.expectations.possible_barriers || [];
  (positives.length ? positives : ['']).forEach(text => appendEntry('positives', text));
  (barriers.length ? barriers : ['']).forEach(text => appendEntry('barriers', text));
}
async function refreshHistory() {
  const sessions = await api('/sessions');
  sessionSummaries = sessions;
  $('#session-list').replaceChildren();
  if (!sessions.length) $('#session-list').append(node('p', 'muted small', 'Your saved conversations will appear here.'));
  sessions.forEach(item => {
    const button = node('button', `session-item${session?.id === item.id ? ' active' : ''}`);
    button.dataset.sessionId = item.id;
    button.append(node('strong', '', item.topic || 'Untitled conversation'));
    const day = new Date(item.updated_at).toLocaleDateString(undefined, {day: 'numeric', month: 'short'});
    button.append(node('small', '', [day, statusLabel(item)].filter(Boolean).join(' · ')));
    button.addEventListener('click', () => {
      if (session?.id === item.id || !canLeave()) return;
      openSession(item.id);
    });
    $('#session-list').append(button);
  });
  updateLocks();
}
const STATUS_LABELS = {planned: 'Planned', not_yet: 'Not yet', happened: 'Happened', recorded: 'Recorded', reflected: 'Reflected'};
function formatDay(isoDate) {
  return new Date(`${isoDate}T12:00:00`).toLocaleDateString(undefined, {weekday: 'short', day: 'numeric', month: 'short'});
}
function statusLabel(item) {
  const label = STATUS_LABELS[item.status] || '';
  return item.status === 'not_yet' && item.next_try ? `${label} · try ${formatDay(item.next_try)}` : label;
}
function openSession(id, stageName = 'prepare', then) {
  task(async () => {
    const loaded = await api(`/sessions/${id}`);
    recorder.reset(); facilitator.reset(); session = loaded; dirty.clear(); renderSession(); setStage(stageName);
    then?.();
  });
}

// Fixed, reviewable advice for each reason a conversation hasn't happened yet. No AI is involved.
const FOLLOW_UP_ADVICE = {
  no_right_moment: 'Choose a time when everyone is calm and not rushed, such as after a meal.',
  felt_nervous: 'Feeling nervous is normal. Try saying your opening line out loud a few times. You can also turn on quiet prompts with “When I ask”, so help is there if you need it.',
  family_busy: 'It can help to ask for the time in advance, for example: “Could we sit together one evening this week? There’s something I’d like to talk about.”',
  felt_unsafe: 'Your safety comes first. You don’t have to have this conversation. If you try again, consider having someone you trust with you, or talking to the Umeed for Women team first. If you feel unsafe, go somewhere safe and call 112, or the Women Helpline on 181.',
  changed_mind: 'That’s okay. Your preparation is saved, and you can change your goals or come back to it whenever you like.',
  other: 'Your preparation is saved. When you feel ready, look over your plan and try again.',
};
function renderFollowUp() {
  const panel = $('#follow-up-panel');
  const show = Boolean(session?.chosen_plan) && !session?.recording && !recorder.pending && recorder.state !== 'saved';
  panel.hidden = !show;
  $('#follow-up-date').min = new Date().toISOString().slice(0, 10);
  const follow = session?.follow_up;
  const advice = $('#follow-up-advice');
  if (!show || follow?.status !== 'not_yet') {
    advice.hidden = true;
    $('#follow-up-form').reset();
    if (!show) panel.open = false;
    return;
  }
  const opening = session.chosen_plan.steps?.[0]?.example_wording;
  const lines = [FOLLOW_UP_ADVICE[follow.reason] || FOLLOW_UP_ADVICE.other];
  if (opening && ['no_right_moment', 'felt_nervous'].includes(follow.reason)) lines.push(`Your opening line: “${opening}”`);
  if (follow.next_try) lines.push(`We’ll check in with you on ${formatDay(follow.next_try)}.`);
  advice.replaceChildren(...lines.map(text => node('p', '', text)));
  advice.hidden = false;
  const reason = $('#follow-up-form').querySelector(`input[name="reason"][value="${follow.reason}"]`);
  if (reason) reason.checked = true;
  $('#follow-up-date').value = follow.next_try || '';
  $('#follow-up-note').value = follow.note || '';
}
function openFollowUp() {
  const panel = $('#follow-up-panel');
  if (panel.hidden) return;
  panel.open = true;
  panel.scrollIntoView({block: 'start'});
}
// A gentle check-in when she comes back: did a planned conversation happen? Once per visit, never a pop-up.
function showCheckIn() {
  const today = new Date().toISOString().slice(0, 10);
  const stale = Date.now() - 3 * 24 * 3600 * 1000;
  const due = sessionSummaries.find(item => (item.status === 'not_yet' && item.next_try && item.next_try <= today)
    || (item.status === 'planned' && new Date(item.updated_at).getTime() < stale));
  const box = $('#check-in');
  box.hidden = !due;
  if (!due) return;
  const buttons = [['button secondary', 'Yes, it happened'], ['button secondary', 'Not yet'], ['text-button', 'Not now']]
    .map(([kind, label]) => { const button = node('button', kind, label); button.type = 'button'; return button; });
  const actions = node('div', 'actions', '');
  actions.append(...buttons);
  box.replaceChildren(node('p', '', `Did your conversation about “${due.topic}” happen?`), actions);
  const [yes, notYet, later] = buttons;
  later.addEventListener('click', () => { box.hidden = true; });
  yes.addEventListener('click', () => {
    if (!canLeave()) return;
    box.hidden = true;
    task(async () => {
      session = await api(`/sessions/${due.id}/follow-up`, 'PUT', {status: 'happened'});
      recorder.reset(); facilitator.reset(); dirty.clear(); renderSession(); setStage('reflect');
      notice('Glad it happened. Take a moment to reflect on how it went.');
      await refreshHistory();
    });
  });
  notYet.addEventListener('click', () => {
    if (!canLeave()) return;
    box.hidden = true;
    openSession(due.id, 'record', openFollowUp);
  });
}

function renderSession() {
  promptMode = 'off'; // Consent to prompts is given for each conversation, never carried over.
  instantPrompts = false;
  $('#delete-session').hidden = !session;
  $('#page-title').textContent = session ? session.preparation.topic : 'Start with what matters.';
  $('#page-description').textContent = session ? `A conversation with ${session.preparation.audience.join(', ')}.` : 'Prepare for a conversation, capture it, and reflect in your own words.';
  fillPreparation();
  renderClarifications();
  renderPlan();
  renderEvaluation();
  renderSummary();
  renderRecording();
  updateLocks();
  $$('.session-item').forEach(button => button.classList.toggle('active', button.dataset.sessionId === session?.id));
  // History refresh is separate so an unavailable list never replaces entered data.
}
async function savePreparation() {
  const preparation = getPreparation();
  if (session && samePreparation(preparation, session.preparation)) { dirty.delete('preparation'); return false; }
  if (dirty.has('evaluation') && !window.confirm('Changing preparation will clear your unsaved reflection. Continue?')) throw new Error('Preparation was not changed. Save your reflection first, or keep editing.');
  session = session ? await api(`/sessions/${session.id}/preparation`, 'PUT', {preparation}) : await api('/sessions', 'POST', {preparation});
  dirty.delete('preparation');
  dirty.delete('plan');
  dirty.delete('evaluation');
  $('#delete-session').hidden = false;
  $('#page-title').textContent = session.preparation.topic;
  $('#page-description').textContent = `A conversation with ${session.preparation.audience.join(', ')}.`;
  renderEvaluation();
  renderSummary();
  renderRecording();
  await refreshHistory().catch(() => {});
  return true;
}
function renderClarifications() {
  const container = $('#clarifications');
  container.replaceChildren();
  const response = session?.model_response;
  const questions = response?.status === 'clarification_required' ? response.questions : [];
  container.hidden = !questions.length;
  if (!questions.length) return;
  container.append(node('h3', '', 'A little more context'));
  container.append(node('p', 'help', 'Answer what you can, then select Plan. “Not sure” is a valid answer.'));
  questions.forEach(question => {
    const label = node('label', '', question.question);
    label.htmlFor = `question-${question.id}`;
    const row = node('div', 'question-row');
    const input = node('input');
    input.id = label.htmlFor;
    input.dataset.questionId = question.id;
    input.maxLength = 4000;
    input.value = session.clarification_answers.find(answer => answer.question_id === question.id)?.answer || '';
    input.addEventListener('input', () => markDirty('clarifications'));
    const unsure = node('button', 'button secondary', 'Not sure');
    unsure.type = 'button';
    unsure.addEventListener('click', () => { input.value = 'Not sure'; markDirty('clarifications'); });
    row.append(input, unsure);
    container.append(label, row);
  });
}
function labeledTextarea(container, labelText, value, key, rows = 3) {
  const label = node('label', '', labelText);
  const input = node('textarea');
  input.value = value;
  input.rows = rows;
  input.maxLength = 4000;
  input.dataset.key = key;
  input.id = `editor-${key}-${Math.random().toString(36).slice(2)}`;
  label.htmlFor = input.id;
  container.append(label, input);
  return input;
}
function renderPlan() {
  const plan = session?.chosen_plan || session?.model_response?.plan;
  $('#plan-panel').hidden = !plan;
  $('#plan-editor').replaceChildren();
  $('#plan-preview').replaceChildren();
  setPlanEditing(false);
  $('#plan-save-status').textContent = session?.chosen_plan ? 'Saved plan' : '';
  if (!plan) return;
  labeledTextarea($('#plan-editor'), 'What you want to achieve', plan.understanding, 'understanding', 2);
  plan.steps.forEach((step, index) => {
    const section = node('div', 'plan-step');
    section.dataset.stepId = step.id;
    section.dataset.goalIds = JSON.stringify(step.goal_ids);
    section.append(node('span', 'step-number', `Step ${String(index + 1).padStart(2, '0')}`));
    labeledTextarea(section, 'Purpose', step.purpose, 'purpose', 1);
    labeledTextarea(section, 'Talking points · one per line', step.talking_points.join('\n'), 'talking_points');
    labeledTextarea(section, 'Words you could use', step.example_wording, 'example_wording', 2);
    $('#plan-editor').append(section);
  });
  labeledTextarea($('#plan-editor'), 'Assumptions · one per line', plan.assumptions.join('\n'), 'assumptions', 2);
  renderPlanPreview(plan);
}
function setPlanEditing(editing) {
  $('#plan-editor').hidden = !editing;
  $('#plan-preview').hidden = editing;
  $('#edit-plan').textContent = editing ? 'Preview plan' : 'Edit wording';
  $('#edit-plan').setAttribute('aria-expanded', String(editing));
}
function renderPlanPreview(plan) {
  const preview = $('#plan-preview');
  preview.replaceChildren();
  const overview = node('div', 'plan-overview');
  overview.append(node('p', 'eyebrow', 'Your direction'), node('p', 'plan-intent', plan.understanding));
  const heading = node('div', 'plan-route-heading');
  heading.append(node('h3', '', `Your conversation in ${plan.steps.length} ${plan.steps.length === 1 ? 'step' : 'steps'}`));
  const expand = node('button', 'text-button', 'Expand all');
  expand.type = 'button';
  heading.append(expand);
  const route = node('ol', 'plan-route');
  const disclosures = [];
  function updateExpandLabel() {
    expand.textContent = disclosures.every(detail => detail.open) ? 'Collapse all' : 'Expand all';
  }
  plan.steps.forEach((step, index) => {
    const item = node('li', 'route-item');
    const detail = node('details', 'route-step');
    detail.open = index === 0;
    const summary = node('summary', 'route-step-heading');
    const number = node('span', 'route-number', String(index + 1).padStart(2, '0'));
    const title = node('span', 'route-title', step.purpose);
    summary.append(number, title);
    const content = node('div', 'route-step-content');
    const points = node('div', 'route-points');
    points.append(node('h4', '', 'Bring up'));
    const list = node('ul');
    step.talking_points.forEach(point => list.append(node('li', '', point)));
    points.append(list);
    const wording = node('div', 'route-wording');
    wording.append(node('h4', '', 'You could say'), node('blockquote', '', step.example_wording));
    content.append(points, wording);
    detail.append(summary, content);
    detail.addEventListener('toggle', updateExpandLabel);
    disclosures.push(detail);
    item.append(detail);
    route.append(item);
  });
  expand.addEventListener('click', () => {
    const open = !disclosures.every(detail => detail.open);
    disclosures.forEach(detail => detail.open = open);
    updateExpandLabel();
  });
  updateExpandLabel();
  preview.append(overview, heading, route);
  if (plan.assumptions.length) {
    const assumptions = node('details', 'plan-assumptions');
    assumptions.append(node('summary', '', `Assumptions to check (${plan.assumptions.length})`));
    const list = node('ul');
    plan.assumptions.forEach(value => list.append(node('li', '', value)));
    assumptions.append(list);
    preview.append(assumptions);
  }
}
function getChosenPlan() {
  const get = (parent, key) => parent.querySelector(`[data-key="${key}"]`).value.trim();
  const lines = value => value.split('\n').map(line => line.trim()).filter(Boolean);
  const editor = $('#plan-editor');
  const plan = {understanding: get(editor, 'understanding'), steps: $$('#plan-editor .plan-step').map(section => ({
    id: section.dataset.stepId, goal_ids: JSON.parse(section.dataset.goalIds), purpose: get(section, 'purpose'), talking_points: lines(get(section, 'talking_points')), example_wording: get(section, 'example_wording'),
  })), assumptions: lines(get(editor, 'assumptions'))};
  if (!plan.understanding || !plan.steps.length || plan.steps.some(step => !step.purpose || !step.talking_points.length || !step.example_wording)) throw new Error('Keep an understanding, a purpose, talking points and example wording for every step.');
  return plan;
}
function renderEvaluation() {
  $('#evaluation-goals').replaceChildren();
  $('#expectations-context').replaceChildren();
  $('#reflection').value = session?.evaluation.reflection || '';
  $('#evaluation-status').textContent = session?.evaluation.revision ? (session.evaluation.state === 'completed' ? 'Reflection completed' : 'Draft saved') : '';
  if (!session) return;
  const context = $('#expectations-context');
  context.append(node('h3', '', 'Before the conversation, you expected…'));
  [['What might go well', 'what_might_go_well'], ['Possible barriers', 'possible_barriers']].forEach(([label, key]) => {
    context.append(node('p', '', label));
    const values = session.preparation.expectations[key];
    if (!values.length) context.append(node('p', 'muted', 'None added.'));
    else { const list = node('ul'); values.forEach(value => list.append(node('li', '', value))); context.append(list); }
  });
  session.preparation.goals.forEach((goal, index) => {
    const evaluation = session.evaluation.goals.find(item => item.goal_id === goal.id) || {};
    const section = node('div', 'evaluation-goal');
    section.dataset.goalId = goal.id;
    section.append(node('span', 'step-number', `Goal ${String(index + 1).padStart(2, '0')}`), node('h3', '', goal.description));
    labeledTextarea(section, 'What were you able to achieve?', evaluation.achievement || '', 'achievement');
    const label = node('label', '', 'Your assessment');
    label.htmlFor = `assessment-${goal.id}`;
    const select = node('select');
    select.id = label.htmlFor;
    select.dataset.key = 'assessment';
    [['', 'Choose when you’re ready'], ['achieved', 'Achieved'], ['partly_achieved', 'Partly achieved'], ['not_achieved', 'Not achieved']].forEach(([value, text]) => {
      const option = node('option', '', text); option.value = value; select.append(option);
    });
    select.value = evaluation.assessment || '';
    section.append(label, select);
    const details = node('details');
    details.open = Boolean(evaluation.explanation || evaluation.next_step);
    details.append(node('summary', '', 'Add an explanation or next step (optional)'));
    labeledTextarea(details, 'Explanation', evaluation.explanation || '', 'explanation', 2);
    labeledTextarea(details, 'Next step', evaluation.next_step || '', 'next_step', 2);
    section.append(details);
    $('#evaluation-goals').append(section);
  });
}
function getEvaluation(state = 'draft') {
  return {goals: $$('#evaluation-goals .evaluation-goal').map(section => {
    const value = key => section.querySelector(`[data-key="${key}"]`).value.trim();
    return {goal_id: section.dataset.goalId, achievement: value('achievement'), assessment: value('assessment'), explanation: value('explanation'), next_step: value('next_step')};
  }), reflection: $('#reflection').value.trim(), state};
}
async function saveEvaluation(state) {
  const evaluation = getEvaluation(state);
  if (state === 'completed' && !evaluationComplete(evaluation)) throw new Error('To complete your reflection, add what you achieved and an assessment for each goal. You can save a draft at any time.');
  session = await api(`/sessions/${session.id}/evaluation`, 'PUT', evaluation);
  dirty.delete('evaluation');
  $('#evaluation-status').textContent = state === 'completed' ? 'Reflection completed' : 'Draft saved';
  if (session.summary) $('#summary-outdated').hidden = !session.summary.outdated;
  await refreshHistory().catch(() => {});
}
function renderSummary() {
  $('#summary-editor').hidden = !session?.summary;
  $('#summary-text').value = session?.summary?.text || '';
  $('#summary-outdated').hidden = !session?.summary?.outdated;
  renderSummarySources();
}
function renderSummarySources() {
  $('#summary-used-sources').textContent = session?.summary?.includes_audio ? 'Based on the saved audio and your written evaluation.' : 'Based on your written evaluation only; no audio was included.';
  $('#summary-sources').textContent = session?.recording ? 'Sends the saved audio and your written evaluation to Google Gemini when you generate a summary. Your ratings stay yours.' : 'No recording is saved. Generates a summary from your written evaluation using Google Gemini.';
}
const recorder = new ConversationRecorder({
  onChange: renderRecording,
  save: async (id, blob, startedAt, endedAt) => {
    try {
      const response = await authedFetch(`/api/sessions/${id}/recording`, {method: 'POST', headers: {'Content-Type': blob.type, 'X-Recording-Started-At': startedAt, 'X-Recording-Ended-At': endedAt}, body: blob});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || 'The recording could not be saved.');
      return data;
    } catch (error) {
      // A disconnected response may follow a successful save. Recover only this capture.
      const recovered = await api(`/sessions/${id}`).catch(() => null);
      if (recovered?.recording?.started_at === startedAt && recovered.recording.ended_at === endedAt) return recovered;
      throw error;
    }
  },
});
const screenAwake = new ScreenAwake();
const facilitator = new LiveFacilitator({
  onChange: renderFacilitator,
  onAudio: (samples, rate) => {
    liveStream?.sendAudio(samples, rate);
    if (liveStream) monitorLevel(samples);
  },
  request: async ({audio, requested, mode, signal}) => {
    // Instant prompts: Gemini Live has already heard everything, so only the check is sent.
    if (liveStream?.ready) return liveStream.check({requested, mode, elapsedSeconds: recorder.elapsed()});
    const form = new FormData();
    form.append('context', JSON.stringify({mode, requested, capture_id: recorder.startedAt, elapsed_seconds: recorder.elapsed(), language: promptLanguage,
      inspect: showMonitor}));
    form.append('audio', audio, 'latest.wav');
    // When the person asks, the model hears the whole conversation so far, not just the last minute.
    const conversation = requested ? recorder.snapshot() : null;
    if (conversation) form.append('conversation', conversation, 'conversation');
    const response = await authedFetch(`/api/sessions/${recorder.sessionId}/facilitator`, {method: 'POST', body: form, signal});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `The prompt couldn’t be updated (${response.status}).`);
    if (data.inspect) monitorExchange(data.inspect, data.prompt);
    return data;
  },
});
// The optional live view of what Gemini hears: only on this screen, never saved.
let showMonitor = (() => { try { return localStorage.getItem('cg-live-monitor') === 'on'; } catch { return false; } })();
let monitorText = '';
let monitorLines = [];
let speakerNames = {};
function resetMonitor() {
  monitorText = '';
  monitorLines = [];
  speakerNames = {};
  $('#live-monitor-transcript').textContent = '';
  $('#live-monitor-voices').textContent = 'Telling voices apart once someone speaks.';
  $('#live-monitor-status').textContent = 'Connecting to Gemini Live…';
  $('#live-monitor-level').style.width = '0%';
  $('#live-monitor-sent').hidden = true;
  for (const id of ['#io-instruction', '#io-output', '#io-shown']) $(id).textContent = '';
}
function monitorLevel(samples) {
  if (!showMonitor || !samples.length) return;
  let sum = 0;
  for (const sample of samples) sum += sample * sample;
  $('#live-monitor-level').style.width = `${Math.round(Math.min(1, Math.sqrt(sum / samples.length) * 8) * 100)}%`;
}
function monitorAudio(seconds) {
  $('#live-monitor-status').textContent = `Audio reaching Gemini: ${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
}
function speakerName(id) {
  return (id && speakerNames[id]) || 'Unclear';
}
function transcriptText() {
  return monitorLines.map(line => `${speakerName(line.speaker)}: ${line.text.trim()}`).join('\n');
}
function renderTranscript() {
  const box = $('#live-monitor-transcript');
  box.replaceChildren(...monitorLines.map(line => {
    const name = speakerName(line.speaker);
    const row = node('p', `said ${name === 'You' ? 'said-you' : name === 'Unclear' ? 'said-unclear' : 'said-other'}`, '');
    row.append(node('b', '', `${name}: `), document.createTextNode(line.text.trim()));
    return row;
  }));
  box.scrollTop = box.scrollHeight;
}
// Each piece Gemini heard joins the line of the same voice; names come from the server and can change.
function monitorHeard({text, speaker}) {
  monitorText = `${monitorText}${text}`.slice(-1500);
  const last = monitorLines.at(-1);
  if (last && last.speaker === (speaker ?? null)) last.text += text;
  else monitorLines.push({speaker: speaker ?? null, text});
  monitorLines = monitorLines.slice(-40);
  renderTranscript();
}
function monitorSpeakers(names, progress = {}) {
  speakerNames = names || {};
  renderTranscript();
  const who = [...new Set(Object.values(speakerNames))].join(', ');
  const hint = progress.hasSample ? '' : ' Register your voice in Account to be marked “You”.';
  $('#live-monitor-voices').textContent = progress.speechSeconds
    ? `Telling voices apart: ${progress.speechSeconds} s of speech, ${progress.voices} voice${progress.voices === 1 ? '' : 's'}${who ? ` (${who})` : ''}.${hint}`
    : `Telling voices apart once someone speaks.${hint}`;
}
function monitorSent({check}) {
  let shown = check;
  try { shown = JSON.stringify(JSON.parse(check.replace(/^CHECK /, '').replace(/ Call assess_moment now\.$/, '')), null, 2); } catch { /* show as sent */ }
  const heard = transcriptText() ? transcriptText().slice(-1500)
    : '(nothing heard yet: if this stays empty while the audio counter rises, Gemini is not hearing you)';
  $('#live-monitor-check').textContent = `What was said, as Gemini heard it (sent as live audio, not text):\n${heard}\n\nThe check sent with it:\n${shown}`;
  $('#live-monitor-sent').hidden = false;
}
function monitorRejected({reason}) {
  if (!reason) return;
  $('#io-shown').textContent = `Gemini's answer was not used: ${reason}`;
  $('#live-monitor-sent').hidden = false;
}
function monitorSetup(instruction) {
  $('#io-instruction').textContent = instruction;
}
function describeShown(prompt) {
  return prompt ? `Shown to you: ${prompt.kind.replaceAll('_', ' ')}: “${prompt.text}”` : 'Shown to you: nothing, so the app stayed quiet.';
}
function monitorAnswered({raw, shown, reason}) {
  $('#io-output').textContent = JSON.stringify(raw ?? {}, null, 2);
  $('#io-shown').textContent = reason ? `Gemini's answer was not used: ${reason}` : describeShown(shown);
  $('#live-monitor-sent').hidden = false;
}
function monitorExchange(trace, prompt) {
  const clips = (trace.input?.audio || []).map(clip => `${clip.type}, ${clip.seconds} s`).join(' + ');
  $('#io-instruction').textContent = trace.input?.instruction || '';
  $('#live-monitor-check').textContent = `${JSON.stringify(trace.input?.data ?? {}, null, 2)}${clips ? `\n\nAudio attached: ${clips}` : ''}`;
  monitorAnswered({raw: trace.output, shown: prompt, reason: ''});
}
const LIVE_CHECK_MS = 15000; // Gemini Live has heard everything, so "As we go" can check more often.
function useInterval(milliseconds) {
  facilitator.intervalMs = milliseconds;
  if (facilitator.active && promptMode === 'auto') facilitator.setMode('auto');
}
// Instant prompts (experimental): stream this conversation to Gemini Live while prompts are on. Chosen per conversation.
function openLive() {
  if (liveStream || !instantPrompts || !config?.live_prompts || promptMode === 'off' || !facilitator.active || !recorder.sessionId) return;
  const query = new URLSearchParams({capture_id: recorder.startedAt, language: promptLanguage});
  const stream = new LiveStream({
    url: `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/sessions/${recorder.sessionId}/live?${query}`,
    onCue: guidance => {
      if (liveStream !== stream || !guidance?.prompt) return; // Only a cue (a threat) interrupts what is on screen.
      facilitator.guidance = guidance;
      facilitator.update('listening', '');
    },
    onHeard: heard => { if (liveStream === stream) monitorHeard(heard); },
    onSpeakers: (names, progress) => { if (liveStream === stream) monitorSpeakers(names, progress); },
    onAudioConfirmed: seconds => { if (liveStream === stream) monitorAudio(seconds); },
    onSent: sent => { if (liveStream === stream) monitorSent(sent); },
    onRejected: rejected => { if (liveStream === stream) monitorRejected(rejected); },
    onSetup: instruction => { if (liveStream === stream) monitorSetup(instruction); },
    onAnswered: answered => { if (liveStream === stream) monitorAnswered(answered); },
    onClose: message => {
      if (liveStream !== stream) return;
      liveStream = null;
      useInterval(45000);
      facilitator.update(facilitator.state, `${message} Prompts continue as usual.`);
    },
  });
  liveStream = stream;
  resetMonitor();
  stream.open().then(() => { if (liveStream === stream) { useInterval(LIVE_CHECK_MS); renderFacilitator(); } }).catch(() => {});
}
function closeLive() {
  const stream = liveStream;
  liveStream = null;
  if (stream) {
    useInterval(45000);
    stream.close();
  }
}
function syncFacilitator() {
  if (recorder.state !== 'recording') { instantCue = null; liveCue = null; }
  if (recorder.state !== 'recording' || promptMode === 'off' || !instantPrompts) closeLive();
  if (recorder.state === 'recording' && recorder.stream && recorder.sessionId === session?.id) {
    // Attach once per capture. After the person turns prompts off, only their own choice turns them back on.
    if (promptMode !== 'off' && !facilitator.active && facilitator.capture !== recorder.startedAt) facilitator.attach(recorder.stream, promptMode, recorder.startedAt);
    if (promptMode !== 'off' && facilitator.active) openLive();
  } else if (recorder.state !== 'requesting' && (facilitator.active || facilitator.context)) {
    facilitator.stop(); // Prompts end the moment the recording does, before it is saved.
  }
}
function loadPromptHistory() {
  const key = session?.recording ? `${session.id}:${session.recording.started_at}` : null;
  if (key === promptHistoryFor) return;
  promptHistoryFor = key;
  promptHistory = [];
  if (!key) return;
  api(`/sessions/${session.id}/facilitator`).then(result => {
    if (promptHistoryFor !== key) return;
    promptHistory = Array.isArray(result.prompts) ? result.prompts : [];
    renderFacilitator();
  }).catch(() => {}); // A convenience for looking back; nothing else depends on it.
}
function renderFacilitator() {
  const panel = $('#prompts-panel');
  const own = recorder.sessionId === session?.id;
  const recording = own && recorder.state === 'recording';
  const ending = own && ['stopping', 'saving', 'save_failed'].includes(recorder.state);
  const saved = Boolean(session?.recording);
  const available = Boolean(config?.model_configured);
  const live = recording && promptMode !== 'off';
  $('#recording-note').textContent = saved || promptMode === 'off' ? 'Audio is saved with this conversation. There are no live alerts or analysis.' : 'Audio is saved with this conversation. Prompts appear below only as you chose. Nothing beeps, vibrates or pops up.';
  $('#record-stage').classList.toggle('prompts-live', live);
  panel.classList.toggle('live', recording);
  panel.hidden = !session?.chosen_plan || ending || (saved && !promptHistory.length);
  if (panel.hidden) return;
  $('#prompts-heading').textContent = saved ? 'Prompts from this conversation' : 'Quiet prompts';
  $('#prompts-pill').hidden = saved;
  $('#prompts-intro').hidden = recording;
  $('#prompts-intro').textContent = saved ? 'Prepared while you talked, in order. Your reflection is still yours to write.' : 'Cues from your plan that wait for you to look. Nothing beeps, vibrates or pops up.';
  $('#prompt-modes').hidden = saved;
  $$('#prompt-modes input').forEach(input => {
    input.checked = input.value === promptMode;
    input.disabled = busy || !available || !['idle', 'error', 'recording'].includes(recorder.state);
  });
  // Support fades: early conversations suggest prompts as you go; later ones suggest asking.
  const earlier = sessionSummaries.filter(item => item.recorded && item.id !== session.id).length;
  const suggested = earlier < EARLY_CONVERSATIONS ? 'auto' : 'on_request';
  $$('#prompt-modes .prompt-suggested').forEach(tag => tag.hidden = !available || tag.dataset.mode !== suggested);
  $('#prompt-suggestion').hidden = recording || saved;
  $('#prompt-suggestion').textContent = !available ? 'Prompts need a Gemini API key on the server.' : suggested === 'auto' ? 'In your first few conversations, prompts as you go can help you find your footing. You can change this at any time.' : 'You’ve recorded a few conversations here. Asking only when you need a prompt leaves more of the conversation to you.';
  $('#prompts-privacy').hidden = recording || saved || !available;
  $('#prompt-voice-tip').hidden = recording || saved || !available || !config?.voice_matching || Boolean(account?.user?.voice_sample);
  $('#prompt-language-field').hidden = recording || saved || !available;
  $('#prompt-language').value = promptLanguage;
  $('#prompt-instant-field').hidden = recording || saved || !available || !config?.live_prompts || promptMode === 'off';
  $('#prompt-instant').checked = instantPrompts;
  $('#prompt-monitor-field').hidden = recording || saved || !available || promptMode === 'off';
  $('#prompt-monitor').checked = showMonitor;
  $('#live-monitor').hidden = !(showMonitor && recording && promptMode !== 'off');
  $('#live-monitor-live').hidden = !liveStream;
  $('#prompts-live').hidden = !live;
  $('#prompt-history').hidden = !saved;
  if (live) renderLivePrompt();
  if (saved) renderPromptHistory();
}
function renderLivePrompt() {
  const guidance = facilitator.guidance;
  const halted = ['unavailable', 'paused'].includes(facilitator.state);
  const card = $('#prompt-card');
  // Rebuild only when the prompt itself changes, so nothing on screen shifts between updates.
  // Shown until a newer AI result arrives, so it stays if the AI request fails.
  const instant = instantCue && instantCue.after === guidance ? instantCue.cue : null;
  if (!halted && instant) liveCue = instant;
  if (!halted && guidance?.prompt) liveCue = guidance.prompt;
  // Keep the last real cue on screen. "Nothing to add" and check timestamps are not shown.
  const prompt = halted ? null : (instant || guidance?.prompt || liveCue);
  const key = JSON.stringify([promptMode, halted && facilitator.message, prompt]);
  if (card.dataset.key !== key) {
    card.dataset.key = key;
    card.classList.toggle('empty', !prompt);
    card.dataset.kind = prompt?.kind || '';
    if (prompt) {
      if (prompt.example_wording && !['planned', 'step_away'].includes(prompt.kind)) {
        // What she could say next is the cue; the hint says what that line does.
        card.replaceChildren(node('p', 'prompt-kind', PROMPT_LABELS[prompt.kind] || 'From your plan'), node('p', 'prompt-hint', prompt.text),
          node('blockquote', 'prompt-line', prompt.example_wording));
      } else {
        card.replaceChildren(node('p', 'prompt-kind', PROMPT_LABELS[prompt.kind] || 'From your plan'), node('p', 'prompt-text', prompt.text));
        if (prompt.example_wording) card.append(node('blockquote', '', prompt.example_wording));
      }
      if (prompt.help) card.append(helpBehindTap(prompt.help));
      // AI cues are in her chosen language; plan points and the fixed safety wording are not.
      if (!['planned', 'step_away'].includes(prompt.kind)) card.querySelectorAll('.prompt-text, .prompt-hint, blockquote').forEach(element => { element.lang = PROMPT_LANGUAGES[promptLanguage]; });
    } else {
      card.replaceChildren(node('p', 'prompt-text', halted ? facilitator.message : promptMode === 'auto' ? 'A cue will show here when it would help.' : 'Tap “Get a prompt” whenever you’d like a cue from your plan.'));
    }
  }
  const requesting = facilitator.preparing && facilitator.pendingRequested;
  $('#prompt-status').textContent = halted ? '' : facilitator.message || (requesting ? 'Preparing a cue…' : '');
  $('#ask-prompt').disabled = !facilitator.active || facilitator.preparing;
  $('#ask-prompt').textContent = requesting ? 'Preparing…' : 'Get a prompt';
  renderPromptOutline(guidance);
}
// Helplines wait behind a tap: family members may be able to see the screen.
function helpBehindTap(help) {
  const details = node('details', 'prompt-help');
  details.append(node('summary', '', 'More help'), node('p', '', help));
  return details;
}
function renderPromptOutline(guidance) {
  const outline = $('#prompt-outline');
  const plan = session.chosen_plan;
  const key = JSON.stringify([session.id, plan.steps.map(step => [step.id, step.purpose]), guidance?.current_step_id, guidance?.raised_goal_ids]);
  if (outline.dataset.key === key) return;
  outline.dataset.key = key;
  const route = node('div');
  const steps = node('ol', 'prompt-steps');
  plan.steps.forEach((step, index) => {
    const item = node('li');
    item.append(node('span', 'prompt-step-number', String(index + 1).padStart(2, '0')), node('span', 'prompt-step-title', step.purpose));
    if (step.id === guidance?.current_step_id) { item.setAttribute('aria-current', 'step'); item.append(node('span', 'prompt-now', 'Now')); }
    steps.append(item);
  });
  route.append(node('h3', '', 'Your plan'), steps);
  const raised = new Set(guidance?.raised_goal_ids || []);
  const progress = node('div');
  const goals = node('ul', 'prompt-goals');
  session.preparation.goals.forEach(goal => {
    const item = node('li', raised.has(goal.id) ? 'raised' : '');
    item.append(node('span', 'visually-hidden', raised.has(goal.id) ? 'Raised: ' : 'Not raised yet: '), document.createTextNode(goal.description));
    goals.append(item);
  });
  progress.append(node('h3', '', `Goals raised so far: ${raised.size} of ${session.preparation.goals.length}`), goals);
  outline.replaceChildren(route, progress);
}
function renderPromptHistory() {
  if (renderedHistory === promptHistory) return;
  renderedHistory = promptHistory;
  $('#prompt-history').replaceChildren(...promptHistory.map(prompt => {
    const item = node('li');
    item.dataset.kind = prompt.kind;
    const detail = node('div');
    detail.append(node('strong', '', PROMPT_LABELS[prompt.kind] || 'Prompt'), node('p', '', prompt.text));
    if (prompt.example_wording) detail.append(node('blockquote', '', prompt.example_wording));
    if (prompt.help) detail.append(node('p', 'prompt-help-text', prompt.help)); // In full afterwards, in private.
    item.append(node('span', 'prompt-time', formatDuration(prompt.elapsed_seconds)), detail);
    return item;
  }));
}
function renderRecording() {
  if (recorder.state === 'saved' && recorder.savedSession?.id === session?.id) {
    // Reflection may have been saved while audio was uploading; only merge recording.
    session.recording = recorder.savedSession.recording;
    if (session.summary && !session.summary.includes_audio) {
      session.summary.outdated = true;
      $('#summary-outdated').hidden = false;
    }
  }
  syncFacilitator();
  screenAwake.set(recorder.state === 'recording');
  loadPromptHistory();
  const saved = session?.recording;
  $('#recording-status').textContent = recorder.message || (saved ? 'Recording saved.' : session?.chosen_plan ? 'Ready when you are.' : 'Save your conversation plan before starting a recording.');
  $('#recording-timer').textContent = formatDuration(saved?.duration_seconds || recorder.elapsed());
  $('#recording-orb').classList.toggle('live', recorder.state === 'recording');
  $('#start-recording').hidden = Boolean(saved) || ['requesting', 'recording', 'stopping', 'saving', 'save_failed'].includes(recorder.state);
  $('#stop-recording').hidden = !['recording', 'stopping'].includes(recorder.state);
  $('#retry-recording').hidden = recorder.state !== 'save_failed';
  $('#download-recording').hidden = !recorder.blob || recorder.state !== 'save_failed';
  $('#discard-recording').hidden = recorder.state !== 'save_failed';
  if (lastBlob !== recorder.blob) {
    if (recordingUrl) URL.revokeObjectURL(recordingUrl);
    recordingUrl = recorder.blob ? URL.createObjectURL(recorder.blob) : null;
    lastBlob = recorder.blob;
    if (recordingUrl) {
      $('#download-recording').href = recordingUrl;
      $('#download-recording').download = `conversation-${recorder.sessionId}.${recorder.blob.type.includes('mp4') ? 'mp4' : recorder.blob.type.includes('ogg') ? 'ogg' : 'webm'}`;
    }
  }
  const audio = $('#recording-playback');
  audio.hidden = !saved;
  if (saved && audio.getAttribute('src') !== saved.url) audio.src = saved.url;
  if (!saved) audio.removeAttribute('src');
  $('#recording-saved').hidden = !saved;
  $('#rerecord').hidden = !saved;
  renderFollowUp();
  renderSummarySources();
  $('#recording-help').textContent = saved ? 'You can listen back here. The optional summary can use this audio after your self-evaluation.' : 'Keep this page open while recording. Let everyone know before you start.';
  updateLocks();
}

$('#preparation-fields').addEventListener('input', preparationChanged);
$$('[data-add]').forEach(button => button.addEventListener('click', () => {
  const field = button.dataset.add === 'audience' ? appendPerson() : appendEntry(button.dataset.add);
  field?.focus();
  preparationChanged();
}));
$('#audience').addEventListener('input', event => {
  if (event.target.matches('input')) separatePerson(event.target);
});
$('#audience').addEventListener('keydown', event => {
  if (!event.target.matches('input')) return;
  if (event.key === 'Enter') {
    event.preventDefault();
    separatePerson(event.target, true);
  } else if (event.key === 'Backspace' && event.target.value === '' && $('#audience').children.length > 1) {
    event.preventDefault();
    const row = event.target.closest('.entry-row');
    const previous = row.previousElementSibling?.querySelector('input');
    row.remove();
    previous?.focus();
    preparationChanged();
  }
});
$('#save-preparation').addEventListener('click', () => task(async () => {
  const changed = await savePreparation();
  if (changed) { renderClarifications(); renderPlan(); }
  else if ($('#plan-editor').children.length) $('#plan-panel').hidden = false;
  notice('Preparation saved. You can return later or move to reflection.');
}));
$('#preparation-form').addEventListener('submit', event => {
  event.preventDefault();
  task(async () => {
    const clarification_answers = $$('#clarifications input').map(input => ({question_id: input.dataset.questionId, answer: input.value.trim() || 'Not sure'}));
    await savePreparation();
    $('#plan-button').textContent = 'Planning…';
    try { session = await api(`/sessions/${session.id}/plan`, 'POST', {clarification_answers}); }
    finally { $('#plan-button').textContent = 'Plan ↗'; }
    dirty.delete('clarifications');
    dirty.delete('plan');
    renderClarifications();
    renderPlan();
    if (session.model_response.status === 'plan_ready') { $('#plan-panel').scrollIntoView({behavior: 'smooth', block: 'start'}); notice('Your plan is ready to review. Edit it, then save your chosen version.'); }
    else { $('#clarifications').scrollIntoView({behavior: 'smooth', block: 'center'}); notice('A little more context will help shape your plan.'); }
  });
});
$('#edit-plan').addEventListener('click', () => {
  try {
    const editing = $('#plan-editor').hidden;
    if (!editing) renderPlanPreview(getChosenPlan());
    setPlanEditing(editing);
    if (editing) $('#plan-editor textarea')?.focus();
  } catch (error) { notice(error.message, true); }
});
$('#plan-editor').addEventListener('input', () => { markDirty('plan'); $('#plan-save-status').textContent = 'Unsaved changes'; });
$('#chosen-plan-form').addEventListener('submit', event => { event.preventDefault(); task(async () => {
  session = await api(`/sessions/${session.id}/chosen-plan`, 'PUT', {plan: getChosenPlan()});
  dirty.delete('plan');
  $('#plan-save-status').textContent = 'Saved plan';
  renderPlanPreview(session.chosen_plan);
  setPlanEditing(false);
  renderRecording();
  notice('Your plan is saved. Select Next: Record when you’re ready for the conversation.');
}); });
$('#reject-plan').addEventListener('click', () => {
  $('#plan-panel').hidden = true;
  dirty.delete('plan');
  notice(session?.chosen_plan ? 'Suggestions dismissed. Your previously saved plan is kept. Edit the preparation and select Plan for another draft.' : 'Suggestions dismissed. Edit your preparation and select Plan to try again.');
});
$('#go-record').addEventListener('click', () => task(async () => {
  const changed = await savePreparation();
  if (changed) { renderClarifications(); renderPlan(); }
  if (!$('#plan-editor').children.length && !session.chosen_plan) {
    notice('You can continue to self-evaluation without a recording. Generate and save a plan to enable recording.');
  } else if (dirty.has('plan') || !session.chosen_plan) {
    session = await api(`/sessions/${session.id}/chosen-plan`, 'PUT', {plan: getChosenPlan()});
    dirty.delete('plan');
    $('#plan-save-status').textContent = 'Saved plan';
  }
  renderRecording();
  setStage('record');
}));
$('#go-reflect').addEventListener('click', () => {
  if (busy || recorder.pending) return;
  renderSummarySources();
  setStage('reflect');
});
$('#make-slides').addEventListener('click', async () => {
  const shown = session;
  if (!shown || busy) return;
  const button = $('#make-slides');
  button.disabled = true;
  $('#slides-status').textContent = 'Gemini is making the slides. This can take up to a minute…';
  try {
    const updated = await api(`/sessions/${shown.id}/slides`, 'POST', {});
    if (session?.id === shown.id) { session.slides = updated.slides; showConversationSlides(); }
  } catch (error) {
    $('#slides-status').textContent = `${error.message} Your conversation is unchanged.`;
  } finally {
    button.disabled = false;
  }
});
$$('#stage-tabs button').forEach(button => button.addEventListener('click', () => {
  if (!busy && !recorder.pending) setStage(button.dataset.stage);
}));
$('#back-prepare').addEventListener('click', () => { if (!busy && !recorder.pending) setStage('prepare'); });
$('#back-record').addEventListener('click', () => { if (!busy && !recorder.pending) setStage('record'); });
$('#new-session').addEventListener('click', () => {
  if (!canLeave()) return;
  recorder.reset(); facilitator.reset(); session = null; dirty.clear(); notice(''); renderSession(); setStage('prepare'); $('#audience input')?.focus();
});
$('#delete-session').addEventListener('click', () => {
  if (!session || !canLeave() || !window.confirm('Delete this conversation, its recording and all reflections? This cannot be undone.')) return;
  task(async () => { await api(`/sessions/${session.id}`, 'DELETE'); recorder.reset(); facilitator.reset(); session = null; dirty.clear(); renderSession(); setStage('prepare'); await refreshHistory(); notice('Conversation deleted.'); });
});
$('#start-recording').addEventListener('click', () => {
  if (dirty.has('preparation') || dirty.has('plan')) { notice('Save your preparation and chosen plan before recording.', true); setStage('prepare'); return; }
  if (promptMode !== 'off') facilitator.prepare(); // Phones only allow audio processing to start from a tap.
  recorder.start(session.id);
});
$('#stop-recording').addEventListener('click', () => recorder.stop());
$('#prompt-modes').addEventListener('change', event => {
  promptMode = event.target.value;
  if (recorder.state === 'recording' && recorder.sessionId === session?.id) {
    if (promptMode === 'off') facilitator.stop();
    else if (facilitator.active) facilitator.setMode(promptMode);
    else { facilitator.prepare(); facilitator.attach(recorder.stream, promptMode, recorder.startedAt); }
    if (promptMode === 'off') closeLive();
    else if (facilitator.active) openLive();
  }
  renderFacilitator();
});
$('#rerecord').addEventListener('click', () => {
  if (!session?.recording || recorder.pending) return;
  if (!window.confirm('Discard this recording and record again? The audio and any prompts from it will be deleted. Your reflection is kept. This can’t be undone.')) return;
  task(async () => {
    session = await api(`/sessions/${session.id}/recording`, 'DELETE');
    recorder.reset(); // Otherwise the finished recorder would merge the old recording back in.
    facilitator.reset();
    renderRecording();
    renderSummary();
    notice('Recording discarded. You can record the conversation again.');
  });
});
$('#ask-prompt').addEventListener('click', () => {
  // Her next planned point appears at once; the AI cue replaces it when it arrives.
  if (facilitator.active && !facilitator.preparing) instantCue = {cue: nextPlannedPoint(session?.chosen_plan, facilitator.guidance), after: facilitator.guidance};
  facilitator.ask(true);
  renderFacilitator();
});
$('#prompt-monitor').addEventListener('change', event => {
  showMonitor = event.target.checked;
  try { localStorage.setItem('cg-live-monitor', showMonitor ? 'on' : 'off'); } catch { /* private browsing */ }
  renderFacilitator();
});
$('#prompt-instant').addEventListener('change', event => {
  instantPrompts = event.target.checked;
  renderFacilitator();
});
$('#prompt-language').addEventListener('change', event => {
  promptLanguage = PROMPT_LANGUAGES[event.target.value] ? event.target.value : 'en';
  try { localStorage.setItem('cg-prompt-language', promptLanguage); } catch { /* not remembered, but used for this conversation */ }
});
// Browsers drop the screen lock when the page is hidden; take it again on return.
document.addEventListener('visibilitychange', () => screenAwake.set(recorder.state === 'recording'));
$('#retry-recording').addEventListener('click', () => recorder.persist());
$('#discard-recording').addEventListener('click', () => {
  if (window.confirm('Discard this unsaved audio? Download a backup first if you want to keep it.')) { recorder.discard(); renderRecording(); }
});
$('#evaluation-form').addEventListener('input', () => { markDirty('evaluation'); $('#evaluation-status').textContent = 'Unsaved changes'; });
$('#evaluation-form').addEventListener('submit', event => {
  event.preventDefault();
  task(async () => { await saveEvaluation(event.submitter?.value || 'draft'); notice('Your reflection has been saved.'); });
});
$('#generate-summary').addEventListener('click', () => task(async () => {
  if (dirty.has('summary') && !window.confirm('Replace your unsaved summary edits with a new summary?')) return;
  if (dirty.has('evaluation')) await saveEvaluation(session.evaluation.state);
  $('#generate-summary').textContent = 'Summarising…';
  try { session = await api(`/sessions/${session.id}/summary`, 'POST', {}); }
  finally { $('#generate-summary').textContent = 'Generate summary'; }
  dirty.delete('summary'); renderSummary(); notice(session.summary.includes_audio ? 'Summary created from the audio and your evaluation. You can edit it below.' : 'Summary created from your written evaluation. No audio was included.');
}));
$('#summary-text').addEventListener('input', () => markDirty('summary'));
$('#save-summary').addEventListener('click', () => task(async () => {
  session = await api(`/sessions/${session.id}/summary`, 'PUT', {text: $('#summary-text').value}); dirty.delete('summary'); notice('Summary edits saved.');
}));
window.addEventListener('beforeunload', event => { if (dirty.size || recorder.pending) { event.preventDefault(); event.returnValue = ''; } });
window.addEventListener('pagehide', () => { if (recorder.state === 'recording') recorder.interrupt(); });

renderSession();
$('#follow-up-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!session) return;
  const reason = event.currentTarget.querySelector('input[name="reason"]:checked')?.value;
  if (!reason) { notice('Choose what got in the way.', true); return; }
  task(async () => {
    session = await api(`/sessions/${session.id}/follow-up`, 'PUT',
      {status: 'not_yet', reason, next_try: $('#follow-up-date').value || null, note: $('#follow-up-note').value});
    renderFollowUp();
    notice('Saved. Your preparation is kept for when you’re ready.');
    await refreshHistory();
  });
});
$('#open-insights').addEventListener('click', showInsights);
account = new Account({
  onSignedIn: () => task(async () => {
    config = await api('/config');
    $('#model-status').textContent = config.model_configured ? '' : 'AI planning needs a Gemini API key on the server.';
    await refreshHistory();
    $('#open-insights').hidden = !account.user?.insight_viewer;
    showCheckIn();
  }),
  // Signing out or deleting the account reloads the page: never mid-recording, and unsaved changes need her OK.
  canSignOut: () => {
    if (recorder.pending) return {ok: false, message: 'Stop and save the recording before signing out.'};
    if (!canLeave()) return {ok: false};
    dirty.clear();
    return {ok: true};
  },
});
await account.start();
// Recording counts as activity: keep her signed in while a conversation is being recorded.
setInterval(() => { if (recorder.state === 'recording') authedFetch('/api/auth/me').catch(() => {}); }, 5 * 60 * 1000);
