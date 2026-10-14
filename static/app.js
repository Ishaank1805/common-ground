import {preparationFromFields, samePreparation, evaluationComplete, formatDuration} from './data.mjs';
import {ConversationRecorder} from './recorder.mjs';

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
async function api(path, method = 'GET', data) {
  const options = {method};
  if (data !== undefined) { options.headers = {'Content-Type': 'application/json'}; options.body = JSON.stringify(data); }
  const response = await fetch(`/api${path}`, options);
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
function setStage(name) {
  stage = name;
  $$('.stage-panel').forEach(panel => panel.hidden = panel.id !== `${name}-stage`);
  const labels = {prepare: 'Step 1 of 3 · Prepare', record: 'Step 2 of 3 · Record', reflect: 'Step 3 of 3 · Self-evaluate'};
  $('#stage-progress').textContent = labels[name];
  renderStageNavigation();
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
  return preparationFromFields($('#audience').value, $('#topic').value,
    $$('#goals .entry-row').map(row => ({id: row.dataset.goalId, description: row.querySelector('textarea').value})),
    $$('#positives textarea').map(input => input.value), $$('#barriers textarea').map(input => input.value));
}
function fillPreparation() {
  const preparation = session?.preparation;
  $('#audience').value = preparation?.audience.join(', ') || '';
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
  $('#session-list').replaceChildren();
  if (!sessions.length) $('#session-list').append(node('p', 'muted small', 'Your saved conversations will appear here.'));
  sessions.forEach(item => {
    const button = node('button', `session-item${session?.id === item.id ? ' active' : ''}`);
    button.dataset.sessionId = item.id;
    button.append(node('strong', '', item.topic || 'Untitled conversation'));
    button.append(node('small', '', new Date(item.updated_at).toLocaleDateString(undefined, {day: 'numeric', month: 'short'})));
    button.addEventListener('click', () => {
      if (session?.id === item.id || !canLeave()) return;
      task(async () => { const loaded = await api(`/sessions/${item.id}`); recorder.reset(); session = loaded; dirty.clear(); renderSession(); setStage('prepare'); });
    });
    $('#session-list').append(button);
  });
  updateLocks();
}
function renderSession() {
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
      const response = await fetch(`/api/sessions/${id}/recording`, {method: 'POST', headers: {'Content-Type': blob.type, 'X-Recording-Started-At': startedAt, 'X-Recording-Ended-At': endedAt}, body: blob});
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
function renderRecording() {
  if (recorder.state === 'saved' && recorder.savedSession?.id === session?.id) {
    // Reflection may have been saved while audio was uploading; only merge recording.
    session.recording = recorder.savedSession.recording;
    if (session.summary && !session.summary.includes_audio) {
      session.summary.outdated = true;
      $('#summary-outdated').hidden = false;
    }
  }
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
  renderSummarySources();
  $('#recording-help').textContent = saved ? 'You can listen back here. The optional summary can use this audio after your self-evaluation.' : 'Keep this page open while recording. Let everyone know before you start.';
  updateLocks();
}

$('#preparation-fields').addEventListener('input', preparationChanged);
$$('[data-add]').forEach(button => button.addEventListener('click', () => { appendEntry(button.dataset.add)?.focus(); preparationChanged(); }));
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
$$('#stage-tabs button').forEach(button => button.addEventListener('click', () => {
  if (!busy && !recorder.pending) setStage(button.dataset.stage);
}));
$('#back-prepare').addEventListener('click', () => { if (!busy && !recorder.pending) setStage('prepare'); });
$('#back-record').addEventListener('click', () => { if (!busy && !recorder.pending) setStage('record'); });
$('#new-session').addEventListener('click', () => {
  if (!canLeave()) return;
  recorder.reset(); session = null; dirty.clear(); notice(''); renderSession(); setStage('prepare'); $('#audience').focus();
});
$('#delete-session').addEventListener('click', () => {
  if (!session || !canLeave() || !window.confirm('Delete this conversation, its recording and all reflections? This cannot be undone.')) return;
  task(async () => { await api(`/sessions/${session.id}`, 'DELETE'); recorder.reset(); session = null; dirty.clear(); renderSession(); setStage('prepare'); await refreshHistory(); notice('Conversation deleted.'); });
});
$('#start-recording').addEventListener('click', () => {
  if (dirty.has('preparation') || dirty.has('plan')) { notice('Save your preparation and chosen plan before recording.', true); setStage('prepare'); return; }
  recorder.start(session.id);
});
$('#stop-recording').addEventListener('click', () => recorder.stop());
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
await task(async () => {
  config = await api('/config');
  $('#profile').replaceChildren(document.createTextNode(config.profile.name + ' '), node('span', '', config.profile.pronouns));
  $('#model-status').textContent = config.model_configured ? '' : 'AI planning needs a Gemini API key on the server.';
  await refreshHistory();
});
