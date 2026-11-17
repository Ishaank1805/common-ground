// Instant prompts (experimental): streams the conversation's 16 kHz audio to this server, which relays it to a
// Gemini Live session (see live.py). Cues come back as text; nothing is ever spoken aloud.
export const LIVE_RATE = 16000;

export function toPcm16(samples) {
  const pcm = new Int16Array(samples.length);
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    pcm[index] = sample < 0 ? Math.round(sample * 0x8000) : Math.round(sample * 0x7fff);
  }
  return pcm.buffer;
}

function parse(data) {
  if (typeof data !== 'string') return null;
  try {
    const message = JSON.parse(data);
    return message && typeof message === 'object' ? message : null;
  } catch {
    return null;
  }
}

export class LiveStream {
  constructor({url, WebSocketClass = globalThis.WebSocket, onCue = () => {}, onClose = () => {},
    onHeard = () => {}, onAudioConfirmed = () => {}, onSent = () => {}, onRejected = () => {},
    openTimeoutMs = 15000, checkTimeoutMs = 25000, timers = globalThis}) {
    Object.assign(this, {url, WebSocketClass, onCue, onClose, onHeard, onAudioConfirmed, onSent, onRejected,
      openTimeoutMs, checkTimeoutMs, timers});
    this.ready = false;
    this.closed = false;
    this.socket = null;
    this.checks = new Map();
    this.nextId = 1;
    this.stopMessage = '';
  }

  // Resolves once the server's Gemini Live session is listening.
  open() {
    return new Promise((resolve, reject) => {
      let settled = false;
      const settle = (ok, error) => {
        if (settled) return;
        settled = true;
        this.timers.clearTimeout(timer);
        if (ok) resolve(); else reject(error);
      };
      const timer = this.timers.setTimeout(() => { settle(false, new Error('Instant prompts couldn’t start.')); this.close(); }, this.openTimeoutMs);
      let socket;
      try {
        socket = new this.WebSocketClass(this.url);
      } catch (error) {
        settle(false, error);
        return;
      }
      socket.binaryType = 'arraybuffer';
      this.socket = socket;
      socket.onmessage = event => {
        const message = parse(event.data);
        if (!message) return;
        if (message.type === 'ready') {
          this.ready = true;
          settle(true);
        } else if (message.type === 'guidance') {
          // A check's answer, or (with no id) a threat reported at once.
          this.settleCheck(message.id, check => check.resolve(message.guidance), () => this.onCue(message.guidance));
        } else if (message.type === 'error') {
          this.onRejected({id: message.id, reason: String(message.reason || '')});
          this.settleCheck(message.id, check => check.reject(new Error(message.message)));
        } else if (message.type === 'heard') {
          this.onHeard({text: String(message.text || ''), finished: Boolean(message.finished)});
        } else if (message.type === 'audio') {
          this.onAudioConfirmed(Number(message.seconds) || 0);
        } else if (message.type === 'sent') {
          this.onSent({id: message.id, check: String(message.check || '')});
        } else if (message.type === 'stopped') {
          this.stopMessage = message.message || '';
        }
      };
      socket.onclose = event => {
        const message = this.stopMessage || event?.reason || '';
        this.ready = false;
        this.failChecks(new Error('Instant prompts stopped. Your recording continues.'));
        settle(false, new Error(message || 'Instant prompts couldn’t start.'));
        if (!this.closed) this.onClose(message || 'Instant prompts stopped.');
        this.closed = true;
      };
    });
  }

  settleCheck(id, found, missing = () => {}) {
    const check = id === null || id === undefined ? null : this.checks.get(id);
    if (!check) { missing(); return; }
    this.checks.delete(id);
    this.timers.clearTimeout(check.timer);
    found(check);
  }

  failChecks(error) {
    for (const check of this.checks.values()) {
      this.timers.clearTimeout(check.timer);
      check.reject(error);
    }
    this.checks.clear();
  }

  sendAudio(samples, rate) {
    if (this.ready && rate === LIVE_RATE && samples?.length && this.socket?.readyState === 1) this.socket.send(toPcm16(samples));
  }

  // Ask for a judgement of this moment; resolves with the same guidance a regular prompt returns.
  check({requested, mode, elapsedSeconds}) {
    if (!this.ready) return Promise.reject(new Error('Instant prompts aren’t connected. Your recording continues.'));
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolve, reject) => {
      const timer = this.timers.setTimeout(() => {
        this.checks.delete(id);
        reject(new Error('No prompt this time. Your recording continues.'));
      }, this.checkTimeoutMs);
      this.checks.set(id, {resolve, reject, timer});
      this.socket.send(JSON.stringify({type: 'check', id, requested, mode, elapsed_seconds: elapsedSeconds}));
    });
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.ready = false;
    try {
      if (this.socket?.readyState === 1) this.socket.send(JSON.stringify({type: 'stop'}));
      this.socket?.close(1000);
    } catch { /* already closed */ }
    this.failChecks(new Error('Instant prompts stopped. Your recording continues.'));
  }
}
