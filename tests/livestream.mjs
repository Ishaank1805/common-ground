// Instant prompts in the browser. Run with: node tests/livestream.mjs
import assert from 'node:assert/strict';
import {LiveStream, toPcm16} from '../static/livestream.mjs';

class FakeSocket {
  constructor(url) { this.url = url; this.sent = []; this.readyState = 1; FakeSocket.last = this; }
  send(data) { this.sent.push(data); }
  close() { this.readyState = 3; this.onclose?.({reason: ''}); }
  serverSays(message) { this.onmessage({data: JSON.stringify(message)}); }
  serverCloses(reason = '') { this.readyState = 3; this.onclose({reason}); }
}

function manualTimers() {
  const pending = new Set();
  return {pending, setTimeout: (fn) => { const timer = {fn}; pending.add(timer); return timer; },
    clearTimeout: (timer) => pending.delete(timer), fire() { for (const timer of [...pending]) { pending.delete(timer); timer.fn(); } }};
}

function stream(options = {}) {
  const cues = [], closes = [];
  const live = new LiveStream({url: 'ws://host/api/sessions/s1/live', WebSocketClass: FakeSocket, timers: manualTimers(),
    onCue: cue => cues.push(cue), onClose: message => closes.push(message), ...options});
  return {live, cues, closes};
}

const tests = {
  async 'audio streams only once the session listens, as 16-bit PCM at 16 kHz'() {
    const {live} = stream();
    const opening = live.open();
    live.sendAudio(new Float32Array([0.5]), 16000);
    assert.equal(FakeSocket.last.sent.length, 0);
    FakeSocket.last.serverSays({type: 'ready'});
    await opening;
    live.sendAudio(new Float32Array([0.5, -0.5]), 16000);
    live.sendAudio(new Float32Array([0.5]), 8000);
    assert.equal(FakeSocket.last.sent.length, 1);
    assert.equal(FakeSocket.last.sent[0].byteLength, 4);
    assert.deepEqual([...new Int16Array(toPcm16(new Float32Array([1, -1, 2, 0])))], [32767, -32768, 32767, 0]);
  },
  async 'a check gets its own answer, and a threat without a check is shown at once'() {
    const {live, cues} = stream();
    const opening = live.open();
    FakeSocket.last.serverSays({type: 'ready'});
    await opening;
    const answer = live.check({requested: true, mode: 'on_request', elapsedSeconds: 42});
    const sent = JSON.parse(FakeSocket.last.sent.at(-1));
    assert.deepEqual(sent, {type: 'check', id: 1, requested: true, mode: 'on_request', elapsed_seconds: 42});
    FakeSocket.last.serverSays({type: 'guidance', id: null, guidance: {prompt: {kind: 'step_away'}}});
    FakeSocket.last.serverSays({type: 'guidance', id: 1, guidance: {prompt: {kind: 'next_point'}}});
    assert.deepEqual(await answer, {prompt: {kind: 'next_point'}});
    assert.deepEqual(cues, [{prompt: {kind: 'step_away'}}]);
  },
  async 'errors, slow answers and a dropped connection end quietly'() {
    const timers = manualTimers();
    const {live, closes} = stream({timers});
    const opening = live.open();
    FakeSocket.last.serverSays({type: 'ready'});
    await opening;
    const rejected = live.check({requested: true, mode: 'auto', elapsedSeconds: 1});
    FakeSocket.last.serverSays({type: 'error', id: 1, message: 'No prompt this time. Your recording continues.'});
    await assert.rejects(rejected, /No prompt this time/);
    const slow = live.check({requested: false, mode: 'auto', elapsedSeconds: 2});
    timers.fire();
    await assert.rejects(slow, /No prompt this time/);
    const cut = live.check({requested: true, mode: 'auto', elapsedSeconds: 3});
    FakeSocket.last.serverSays({type: 'stopped', message: 'Instant prompts stopped after repeated connection problems. Your recording continues.'});
    FakeSocket.last.serverCloses();
    await assert.rejects(cut, /Instant prompts stopped/);
    assert.deepEqual(closes, ['Instant prompts stopped after repeated connection problems. Your recording continues.']);
    await assert.rejects(live.check({requested: true, mode: 'auto', elapsedSeconds: 4}), /aren’t connected/);
  },
  async 'closing it ourselves sends stop and stays quiet'() {
    const {live, closes} = stream();
    const opening = live.open();
    FakeSocket.last.serverSays({type: 'ready'});
    await opening;
    const socket = FakeSocket.last;
    live.close();
    assert.deepEqual(JSON.parse(socket.sent.at(-1)), {type: 'stop'});
    assert.deepEqual(closes, []);
  },
  async 'what Gemini hears, audio arriving and what was asked reach the live view'() {
    const seen = [];
    const {live} = stream({onHeard: heard => seen.push(['heard', heard]), onAudioConfirmed: seconds => seen.push(['audio', seconds]),
      onSent: sent => seen.push(['sent', sent]), onRejected: rejected => seen.push(['rejected', rejected])});
    const opening = live.open();
    FakeSocket.last.serverSays({type: 'ready'});
    await opening;
    FakeSocket.last.serverSays({type: 'audio', seconds: 2});
    FakeSocket.last.serverSays({type: 'heard', text: 'The fees', finished: false});
    const answer = live.check({requested: true, mode: 'on_request', elapsedSeconds: 5});
    FakeSocket.last.serverSays({type: 'sent', id: 1, check: 'CHECK {} Call assess_moment now.'});
    FakeSocket.last.serverSays({type: 'error', id: 1, message: 'No prompt this time.', reason: 'Cue must be text.'});
    await assert.rejects(answer, /No prompt/);
    assert.deepEqual(seen, [['audio', 2], ['heard', {text: 'The fees', finished: false}],
      ['sent', {id: 1, check: 'CHECK {} Call assess_moment now.'}], ['rejected', {id: 1, reason: 'Cue must be text.'}]]);
  },
  async 'the instructions and every raw answer reach the live view'() {
    const seen = [];
    const {live} = stream({onSetup: text => seen.push(['setup', text]), onAnswered: answered => seen.push(['answered', answered])});
    const opening = live.open();
    FakeSocket.last.serverSays({type: 'ready'});
    FakeSocket.last.serverSays({type: 'setup', instruction: 'LIVE INSTRUCTIONS'});
    await opening;
    FakeSocket.last.serverSays({type: 'answered', id: 1, raw: {situation: 'stuck'}, shown: null, reason: null});
    assert.deepEqual(seen, [['setup', 'LIVE INSTRUCTIONS'], ['answered', {id: 1, raw: {situation: 'stuck'}, shown: null, reason: ''}]]);
  },
  async 'a refused connection rejects opening'() {
    const {live} = stream();
    const opening = live.open();
    FakeSocket.last.serverCloses('Instant prompts accept connections only from this site.');
    await assert.rejects(opening, /only from this site/);
  },
};

let failed = 0;
for (const [name, test] of Object.entries(tests)) {
  try { await test(); } catch (error) { failed += 1; console.error(`FAIL ${name}\n`, error); }
}
if (failed) { console.error(`${failed} livestream test(s) failed`); process.exit(1); }
console.log(`livestream: ${Object.keys(tests).length} tests passed`);
