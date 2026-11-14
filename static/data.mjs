/** One box per person. Space, comma or semicolon separates names, including "mother father". */
export function splitPeople(value) {
  const source = Array.isArray(value) ? value : [value ?? ''];
  return source.flatMap(item => String(item).split(/[,;\s]+/)).map(part => part.trim()).filter(Boolean);
}

/**
 * Boxes to show while a person field is being typed.
 * A trailing space or comma opens the next empty box: "mother " → ["mother", ""].
 */
export function peopleBoxes(raw, commit = false) {
  const text = String(raw ?? '');
  const names = splitPeople(text);
  const opensNext = commit || /[\s,;]$/.test(text);
  if (!names.length) return [''];
  return opensNext ? [...names, ''] : names;
}

export function preparationFromFields(audience, topic, goals, positives, barriers) {
  const preparation = {
    audience: splitPeople(audience),
    topic: topic.trim(),
    goals: goals.map(goal => ({id: goal.id, description: goal.description.trim()})).filter(goal => goal.description),
    expectations: {
      what_might_go_well: positives.map(value => value.trim()).filter(Boolean),
      possible_barriers: barriers.map(value => value.trim()).filter(Boolean),
    },
  };
  if (!preparation.audience.length) throw new Error('Add at least one person you’re speaking to.');
  if (!preparation.topic) throw new Error('Add a topic for this conversation.');
  if (!preparation.goals.length) throw new Error('Add at least one goal.');
  if (preparation.audience.length > 30 || preparation.audience.some(value => value.length > 200)) throw new Error('Use up to 30 audience entries, each at most 200 characters.');
  if (preparation.topic.length > 2000 || preparation.goals.some(goal => goal.description.length > 2000)) throw new Error('Keep the topic and each goal within 2,000 characters.');
  if ([preparation.goals, ...Object.values(preparation.expectations)].some(list => list.length > 30)) throw new Error('Use up to 30 entries in each list.');
  if (Object.values(preparation.expectations).flat().some(value => value.length > 2000)) throw new Error('Keep each expectation within 2,000 characters.');
  return preparation;
}

export function evaluationComplete(evaluation) {
  return evaluation.goals.length > 0 && evaluation.goals.every(goal => goal.achievement.trim() && ['achieved', 'partly_achieved', 'not_achieved'].includes(goal.assessment));
}

export function formatDuration(seconds) {
  return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`;
}

export function samePreparation(left, right) {
  const fields = value => [value.audience, value.topic, value.goals.map(goal => [goal.id, goal.description]), value.expectations.what_might_go_well, value.expectations.possible_barriers];
  return JSON.stringify(fields(left)) === JSON.stringify(fields(right));
}
