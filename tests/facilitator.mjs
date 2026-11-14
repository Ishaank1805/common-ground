// Run with: node tests/facilitator.mjs
import assert from 'node:assert/strict';
import {Downsampler, RollingAudio, encodeWav, rootMeanSquare, LiveFacilitator, nextPlannedPoint} from '../static/facilitator.mjs';
import {ConversationRecorder} from '../static/recorder.mjs';
import {ScreenAwake} from '../static/wake.mjs';

// Downsampling keeps exact timing across chunk boundaries, including 44.1 kHz microphones.
const fromBrowser = new Downsampler(48000);
assert.equal(fromBrowser.process(new Float32Array(4096)).length + fromBrowser.process(new Float32Array(4096)).length, Math.floor(8192 / 3));
assert.deepEqual([...new Downsampler(48000).process(Float32Array.from([0.25, 0.25, 0.25, -0.5, -0.5, -0.5]))], [0.25, -0.5]);
let resampled = 0;
const fromMac = new Downsampler(44100);
for (let second = 0; second < 10; second++) resampled += fromMac.process(new Float32Array(4410)).length;
assert.ok(Math.abs(resampled - 16000) <= 1, `44.1 kHz should become 16 kHz, got ${resampled} samples`);
assert.deepEqual([...new Downsampler(16000).process(Float32Array.from([0.5, -0.5]))], [0.5, -0.5]);

// The rolling window keeps only the latest audio, in order, across wraparound.
const rolling = new RollingAudio(10, 1);
rolling.push(Float32Array.from([1, 2, 3, 4, 5, 6]));
rolling.push(Float32Array.from([7, 8, 9, 10, 11, 12]));
assert.deepEqual([...rolling.latest()], [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
assert.deepEqual([...rolling.latest(0.3)], [10, 11, 12]);
assert.equal(rolling.seconds, 1);

// WAV encoding: the exact header the server validates, with clamped samples.
const wav = encodeWav(Float32Array.from([0, 1, -1, 2, Number.NaN]), 16000);
assert.equal(wav.type, 'audio/wav');
const bytes = new DataView(await wav.arrayBuffer());
const ascii = (offset, length) => String.fromCharCode(...new Uint8Array(bytes.buffer, offset, length));
assert.deepEqual([ascii(0, 4), ascii(8, 4), ascii(12, 4), ascii(36, 4)], ['RIFF', 'WAVE', 'fmt ', 'data']);
assert.deepEqual([bytes.getUint16(20, true), bytes.getUint16(22, true), bytes.getUint32(24, true), bytes.getUint16(34, true)], [1, 1, 16000, 16]);
assert.equal(bytes.getUint32(40, true), 10);
assert.equal(bytes.byteLength, 54);
assert.deepEqual([0, 1, 2, 3, 4].map(index => bytes.getInt16(44 + index * 2, true)), [0, 32767, -32768, 32767, 0]);
assert.equal(rootMeanSquare(Float32Array.from([0.5, -0.5])), 0.5);
assert.equal(rootMeanSquare(new Float32Array()), 0);

class FakeNode { connect(target) { this.target = target; } disconnect() { this.disconnected = true; } }
class FakeAudioContext {
  static made = [];
  constructor() { this.sampleRate = 48000; this.state = 'running'; this.destination = new FakeNode(); FakeAudioContext.made.push(this); }
  resume() { this.resumed = (this.resumed || 0) + 1; return Promise.resolve(); }
  close() { this.state = 'closed'; return Promise.resolve(); }
  createMediaStreamSource(stream) { this.stream = stream; this.source = new FakeNode(); return this.source; }
  createScriptProcessor() { this.processor = new FakeNode(); return this.processor; }
  createGain() { this.gain = new FakeNode(); this.gain.gain = {value: 1}; return this.gain; }
}
function speak(context, seconds, level = 0.2) {
  const chunk = 4096;
  for (let sent = 0; sent < seconds * context.sampleRate; sent += chunk) {
    context.processor.onaudioprocess({inputBuffer: {getChannelData: () => new Float32Array(chunk).fill(level)}});
  }
}
const timers = {made: [], cleared: 0, setInterval(callback, ms) { this.made.push({callback, ms}); return this.made.length; }, clearInterval(id) { if (id) this.cleared += 1; }};

// "When I ask": nothing is sent until the person taps, and only the latest minute ever is.
const requests = [];
let reply = async () => ({prompt: {kind: 'next_point', text: 'Bring up the costs.', example_wording: '', step_id: 'step2'}, current_step_id: 'step2', raised_goal_ids: ['g1'], audio_status: 'clear', elapsed_seconds: 75});
const live = new LiveFacilitator({AudioContextClass: FakeAudioContext, timers, request: async options => { requests.push(options); return reply(options); }});
live.prepare(); // In the app this runs inside the Start recording tap.
const context = FakeAudioContext.made.at(-1);
assert.equal(context.resumed, 1);
assert.equal(live.attach({id: 'microphone'}, 'on_request', 'capture-1'), true);
assert.equal(FakeAudioContext.made.length, 1, 'attach reuses the context prepared by the tap');
assert.equal(live.active, true);
assert.equal(context.gain.gain.value, 0, 'the processing path is muted, so prompts never make a sound');
assert.equal(context.gain.target, context.destination);
assert.equal(timers.made.length, 0, 'no timer runs in "When I ask"');
assert.equal(await live.ask(true), false);
assert.match(live.message, /few seconds/);
assert.equal(requests.length, 0);
speak(context, 3);
assert.equal(await live.ask(true), true);
assert.equal(requests.length, 1);
assert.deepEqual([requests[0].requested, requests[0].mode, requests[0].audio.type], [true, 'on_request', 'audio/wav']);
assert.ok(Math.abs(requests[0].seconds - 3) < 0.1);
assert.equal((requests[0].audio.size - 44) / 2, Math.round(requests[0].seconds * 16000));
assert.equal(live.guidance.prompt.text, 'Bring up the costs.');
assert.deepEqual([live.state, live.message], ['listening', '']);
speak(context, 70);
await live.ask(true);
assert.equal(requests[1].seconds, 60, 'only the latest minute is ever sent');

// "As we go": a quiet check every few seconds that skips silent windows.
live.setMode('auto');
assert.equal(timers.made.length, 1);
assert.equal(timers.made[0].ms, 5000);
speak(context, 61, 0);
assert.equal(await timers.made[0].callback(), false, 'a silent minute is not sent');
assert.equal(requests.length, 2);
speak(context, 10);
assert.equal(await timers.made[0].callback(), true);
assert.deepEqual([requests[2].requested, requests[2].mode], [false, 'auto']);
assert.ok(requests[2].seconds <= 12.2, `automatic checks send a short clip, got ${requests[2].seconds}s`);
await live.ask(true);
assert.equal(timers.made.length, 2, 'asking restarts the quiet interval');
assert.equal(requests[3].requested, true);

// One request at a time; stopping cancels it, and a cancelled prompt is never shown.
let cancelled = null;
reply = ({signal}) => new Promise((resolve, reject) => signal.addEventListener('abort', () => { cancelled = signal; reject(Object.assign(new Error('Aborted'), {name: 'AbortError'})); }));
const waiting = live.ask(true);
assert.deepEqual([live.preparing, live.pendingRequested], [true, true]);
assert.equal(await live.ask(true), false, 'one request at a time');
const shown = live.guidance;
live.stop();
assert.equal(await waiting, false);
assert.equal(cancelled?.aborted, true);
assert.equal(live.guidance, shown, 'a request cancelled by stopping never replaces the prompt');
assert.deepEqual([live.active, live.state], [false, 'off']);
assert.equal(context.state, 'closed');
assert.ok(context.processor.disconnected && context.source.disconnected && context.gain.disconnected);
assert.equal(context.processor.onaudioprocess, null);
assert.ok(timers.cleared >= 1);

// Turning prompts back on within the same recording keeps the plan position; a new recording starts fresh.
live.prepare();
assert.equal(live.attach({id: 'microphone'}, 'on_request', 'capture-1'), true);
assert.equal(live.guidance, shown);
live.stop();
live.prepare();
live.attach({id: 'microphone'}, 'auto', 'capture-2');
assert.equal(live.guidance, null);
live.reset();
assert.deepEqual([live.active, live.capture, live.guidance, live.mode], [false, null, null, 'off']);

// Repeated failures stop prompts quietly; the recording is never touched.
const failing = new LiveFacilitator({AudioContextClass: FakeAudioContext, timers, request: async () => { throw new Error('Gemini is unavailable.'); }});
failing.prepare();
failing.attach({id: 'microphone'}, 'on_request', 'capture-3');
speak(FakeAudioContext.made.at(-1), 3);
await failing.ask(true);
assert.equal(failing.message, 'Gemini is unavailable. Your recording continues.');
assert.equal(failing.active, true);
await failing.ask(true);
await failing.ask(true);
assert.deepEqual([failing.state, failing.active], ['paused', false]);
assert.match(failing.message, /stopped after repeated problems\. Your recording continues/);

// Browsers without Web Audio, or that refuse a node, get a quiet explanation and nothing else.
const unsupported = new LiveFacilitator({AudioContextClass: null, request: async () => { throw new Error('Must not be called'); }});
unsupported.prepare();
assert.equal(unsupported.attach({id: 'microphone'}, 'auto', 'capture-4'), false);
assert.equal(unsupported.state, 'unavailable');
assert.match(unsupported.message, /aren’t available in this browser\. Your recording continues/);
assert.equal(await unsupported.ask(true), false);
class RefusingContext extends FakeAudioContext { createScriptProcessor() { throw new Error('Not allowed'); } }
const refused = new LiveFacilitator({AudioContextClass: RefusingContext, request: async () => ({})});
refused.prepare();
assert.equal(refused.attach({id: 'microphone'}, 'auto', 'capture-5'), false);
assert.equal(FakeAudioContext.made.at(-1).state, 'closed', 'a failed attach releases the audio context');
// "Get a prompt" sends the whole conversation so far, taken from the recorder without disturbing it.
const conversationRecorder = new ConversationRecorder({onChange() {}, save: async () => ({}), mediaDevices: {}, Recorder: class {}});
assert.equal(conversationRecorder.snapshot(), null);
conversationRecorder.chunks = [new Blob(['ab'], {type: 'audio/webm'}), new Blob(['cd'], {type: 'audio/webm'})];
conversationRecorder.recorder = {mimeType: 'audio/webm;codecs=opus'};
const soFar = conversationRecorder.snapshot();
assert.equal(soFar.type, 'audio/webm;codecs=opus');
assert.equal(await soFar.text(), 'abcd');
assert.equal(conversationRecorder.chunks.length, 2, 'the recording itself is untouched');
// The screen stays awake while recording: one request per need, renewed after the browser releases it.
const wakeRequests = [];
const wakeLock = {request(type) {
  wakeRequests.push(type);
  const lock = {released: false, listeners: [], release() { this.released = true; this.listeners.forEach(listener => listener()); return Promise.resolve(); }, addEventListener(event, listener) { this.listeners.push(listener); }};
  wakeLock.last = lock;
  return Promise.resolve(lock);
}};
let pageVisible = true;
const awake = new ScreenAwake({wakeLock, visible: () => pageVisible});
awake.set(true);
awake.set(true);
await awake.pending;
assert.deepEqual(wakeRequests, ['screen'], 'repeated renders request once');
awake.lock.release(); // The browser releases the lock when the page is hidden.
assert.equal(awake.lock, null);
pageVisible = false;
awake.set(true);
assert.equal(wakeRequests.length, 1, 'no request while the page is hidden');
pageVisible = true;
awake.set(true);
await awake.pending;
assert.equal(wakeRequests.length, 2, 'taken again on return');
const held = awake.lock;
awake.set(false);
assert.deepEqual([held.released, awake.lock], [true, null], 'released when recording stops');
awake.set(true);
const late = awake.pending;
awake.set(false);
await late;
assert.equal(wakeLock.last.released, true, 'a lock granted after recording stopped is released at once');
new ScreenAwake({wakeLock: null}).set(true); // Unsupported browsers: nothing happens.
const refusedLock = new ScreenAwake({wakeLock: {request: () => Promise.reject(new Error('NotAllowedError'))}, visible: () => true});
refusedLock.set(true);
await refusedLock.pending;
assert.equal(refusedLock.lock, null);
// On a tap, her next planned point shows at once: from the current step, skipping goals already raised.
const steps = [
  {id: 's1', goal_ids: ['g1'], purpose: 'Open calmly', talking_points: ['Say what you want to discuss.'], example_wording: 'Thanks for sitting with me.'},
  {id: 's2', goal_ids: ['g1'], purpose: 'Explain reasons', talking_points: ['Explain why the course matters.'], example_wording: 'This course matters to me.'},
  {id: 's3', goal_ids: ['g2'], purpose: 'Agree a next step', talking_points: [], example_wording: 'Could we look at the options this week?'}];
assert.deepEqual(nextPlannedPoint({steps}, null), {kind: 'planned', text: 'Say what you want to discuss.', example_wording: 'Thanks for sitting with me.', step_id: 's1'});
assert.equal(nextPlannedPoint({steps}, {current_step_id: 's2', raised_goal_ids: []}).step_id, 's2', 'stuck mid-step: the point of that step');
assert.equal(nextPlannedPoint({steps}, {current_step_id: 's2', raised_goal_ids: ['g1']}).step_id, 's3', 'goal already raised: move on');
assert.equal(nextPlannedPoint({steps}, {current_step_id: 's2', raised_goal_ids: ['g1']}).text, 'Agree a next step', 'no talking point: the step purpose');
assert.equal(nextPlannedPoint({steps}, {current_step_id: 's3', raised_goal_ids: ['g1', 'g2']}).step_id, 's3', 'all raised: the closing step');
assert.equal(nextPlannedPoint({steps: []}, null), null);
console.log('Facilitator checks passed: resampling, rolling window, WAV, on-request, quiet interval, silence, cancellation, failures, unsupported browsers.');
