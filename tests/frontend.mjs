// Run with: node tests/frontend.mjs
import assert from 'node:assert/strict';
import {preparationFromFields, peopleBoxes, splitPeople, samePreparation, evaluationComplete, formatDuration} from '../static/data.mjs';
import {ConversationRecorder} from '../static/recorder.mjs';

const prep = preparationFromFields(' mother, , father ', ' A course ', [{id: 'g2', description: ' Explain why '}, {id: 'g4', description: ' '}], [' ', ' They will listen '], []);
assert.deepEqual(prep, {audience: ['mother', 'father'], topic: 'A course', goals: [{id: 'g2', description: 'Explain why'}], expectations: {what_might_go_well: ['They will listen'], possible_barriers: []}});
assert.deepEqual(splitPeople('mother father'), ['mother', 'father']);
assert.deepEqual(peopleBoxes('mother '), ['mother', '']);
assert.deepEqual(peopleBoxes('mother father'), ['mother', 'father']);
// Flask sorts object keys; saving unchanged preparation must preserve plan edits.
const reordered = {expectations: {possible_barriers: prep.expectations.possible_barriers, what_might_go_well: prep.expectations.what_might_go_well}, goals: prep.goals.map(goal => ({description: goal.description, id: goal.id})), topic: prep.topic, audience: prep.audience};
assert.equal(samePreparation(prep, reordered), true);
assert.equal(samePreparation(prep, {...reordered, topic: 'Changed topic'}), false);
assert.equal(samePreparation(prep, {...reordered, goals: [{id: 'g9', description: 'Explain why'}]}), false);
assert.throws(() => preparationFromFields(',', 'topic', [{id: 'g1', description: 'goal'}], [], []), /person/);
assert.throws(() => preparationFromFields('parent', 'topic', [], [], []), /goal/);
assert.throws(() => preparationFromFields('parent', 'x'.repeat(2001), [{id: 'g1', description: 'goal'}], [], []), /2,000/);
assert.throws(() => preparationFromFields('parent', 'topic', [{id: 'g1', description: 'goal'}], Array(31).fill('hope'), []), /30 entries/);
assert.equal(evaluationComplete({goals: [{achievement: 'We talked', assessment: 'partly_achieved'}]}), true);
assert.equal(evaluationComplete({goals: [{achievement: ' ', assessment: 'achieved'}]}), false);
assert.equal(evaluationComplete({goals: []}), false);
assert.equal(formatDuration(125), '02:05');

class Track extends EventTarget { stop() { this.stopped = true; } }
class FakeRecorder extends EventTarget {
  static isTypeSupported(type) { return type === 'audio/mp4'; }
  constructor(stream, options) { super(); this.stream = stream; this.mimeType = options.mimeType; this.state = 'inactive'; }
  start() { this.state = 'recording'; }
  stop() {
    this.state = 'inactive';
    const event = new Event('dataavailable');
    event.data = new Blob(['captured-audio'], {type: this.mimeType});
    this.dispatchEvent(event);
    this.dispatchEvent(new Event('stop'));
  }
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const track = new Track();
const updates = [];
let failSave = true;
let saves = 0;
const recording = new ConversationRecorder({
  onChange: instance => updates.push(instance.state),
  mediaDevices: {getUserMedia: async () => ({getTracks: () => [track]})},
  Recorder: FakeRecorder,
  save: async (id, blob, start, end) => {
    saves++;
    assert.equal(id, 'session-one');
    assert.equal(blob.type, 'audio/mp4');
    assert.ok(start && end);
    if (failSave) throw new Error('Offline.');
    return {id, recording: {url: '/saved.mp4'}};
  },
});
await recording.start('session-one');
assert.equal(recording.state, 'recording');
assert.equal(recording.pending, true);
assert.throws(() => recording.reset(), /captured audio/);
recording.stop();
await tick();
assert.equal(recording.state, 'save_failed');
assert.equal(track.stopped, true);
assert.ok(recording.blob.size);
assert.ok(recording.message.includes('Offline'));
const captured = recording.blob;
await recording.start('other-session');
assert.equal(recording.sessionId, 'session-one');
assert.equal(recording.blob, captured);
failSave = false;
await recording.persist();
assert.equal(recording.state, 'saved');
assert.equal(recording.blob, null);
assert.equal(recording.pending, false);
assert.equal(saves, 2);
recording.reset();
assert.equal(recording.state, 'idle');

const denied = new ConversationRecorder({onChange() {}, save() { throw new Error('Must not save'); }, Recorder: FakeRecorder, mediaDevices: {getUserMedia: async () => { const error = new Error(); error.name = 'NotAllowedError'; throw error; }}});
await denied.start('blocked');
assert.equal(denied.state, 'error');
assert.equal(denied.pending, false);
assert.match(denied.message, /denied/);

const interruptedTrack = new Track();
const interrupted = new ConversationRecorder({onChange() {}, Recorder: FakeRecorder, mediaDevices: {getUserMedia: async () => ({getTracks: () => [interruptedTrack]})}, save: async () => ({id: 'interrupted'})});
await interrupted.start('interrupted');
interruptedTrack.dispatchEvent(new Event('mute'));
assert.equal(interrupted.interrupted, true);
assert.match(interrupted.message, /missing/);
interruptedTrack.dispatchEvent(new Event('unmute'));
assert.match(interrupted.message, /resumed/);
interruptedTrack.dispatchEvent(new Event('ended'));
await tick();
assert.equal(interrupted.state, 'saved');
assert.match(interrupted.message, /incomplete/);
assert.equal(interruptedTrack.stopped, true);

const discard = new ConversationRecorder({onChange() {}, Recorder: FakeRecorder, mediaDevices: {getUserMedia: async () => ({getTracks: () => [new Track()]})}, save: async () => { throw new Error('Offline'); }});
await discard.start('discard');
discard.stop();
await tick();
assert.equal(discard.state, 'save_failed');
discard.discard();
assert.equal(discard.pending, false);
assert.equal(discard.blob, null);
assert.equal(discard.state, 'idle');
console.log('Frontend checks passed: preparation, reflection, recording save/retry, permission denial, interruption, discard.');
