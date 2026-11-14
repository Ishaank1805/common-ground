// Optional quiet prompts during a recorded conversation.
// Prompts are passive: they change quietly on screen and never use sound,
// vibration, pop-ups or motion. Audio for prompts comes from a short rolling
// window held in memory beside the normal recording, which is never touched.

export const PROMPT_RATE = 16000;

/** Box-filter downsampling for speech, carrying the fractional position across chunks. */
export class Downsampler {
  constructor(inputRate, outputRate = PROMPT_RATE) {
    this.ratio = Math.max(1, inputRate / outputRate);
    this.position = 0;
    this.sum = 0;
    this.count = 0;
  }

  process(input) {
    const output = new Float32Array(Math.ceil(input.length / this.ratio) + 2);
    let written = 0;
    for (let index = 0; index < input.length; index++) {
      this.sum += input[index];
      this.count += 1;
      this.position += 1;
      if (this.position >= this.ratio) {
        output[written++] = this.sum / this.count;
        this.sum = 0;
        this.count = 0;
        this.position -= this.ratio;
      }
    }
    return output.subarray(0, written);
  }
}

/** Keeps only the most recent audio, so memory stays fixed however long the conversation runs. */
export class RollingAudio {
  constructor(sampleRate, seconds) {
    this.sampleRate = sampleRate;
    this.samples = new Float32Array(Math.max(1, Math.round(sampleRate * seconds)));
    this.size = 0;
    this.next = 0;
  }

  get seconds() { return this.size / this.sampleRate; }

  push(chunk) {
    const capacity = this.samples.length;
    for (let index = 0; index < chunk.length; index++) {
      this.samples[this.next] = chunk[index];
      this.next = (this.next + 1) % capacity;
    }
    this.size = Math.min(capacity, this.size + chunk.length);
  }

  latest(seconds = Infinity) {
    const capacity = this.samples.length;
    const count = Math.min(this.size, Math.floor(seconds * this.sampleRate));
    const start = (this.next - count + capacity) % capacity;
    const output = new Float32Array(count);
    for (let index = 0; index < count; index++) output[index] = this.samples[(start + index) % capacity];
    return output;
  }
}

export function rootMeanSquare(samples) {
  if (!samples.length) return 0;
  let total = 0;
  for (let index = 0; index < samples.length; index++) total += samples[index] * samples[index];
  return Math.sqrt(total / samples.length);
}

/**
 * Her next planned point, shown the moment she asks; the AI cue replaces it when it arrives.
 * Starts from the step the conversation is at and skips steps whose goals have already come up.
 */
export function nextPlannedPoint(plan, guidance) {
  const steps = plan?.steps || [];
  if (!steps.length) return null;
  const raised = new Set(guidance?.raised_goal_ids || []);
  const current = steps.findIndex(step => step.id === guidance?.current_step_id);
  const ahead = steps.slice(Math.max(current, 0));
  const step = ahead.find(candidate => candidate.goal_ids.some(goal => !raised.has(goal))) || steps[current + 1] || steps.at(-1);
  return {kind: 'planned', text: step.talking_points?.[0] || step.purpose, example_wording: step.example_wording || '', step_id: step.id};
}

/** 16-bit mono PCM WAV: supported by Gemini and simple for the server to validate. */
export function encodeWav(samples, sampleRate) {
  const view = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const ascii = (offset, text) => { for (let index = 0; index < text.length; index++) view.setUint8(offset + index, text.charCodeAt(index)); };
  ascii(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  ascii(8, 'WAVE');
  ascii(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  ascii(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  for (let index = 0; index < samples.length; index++) {
    const value = Math.max(-1, Math.min(1, samples[index] || 0));
    view.setInt16(44 + index * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  }
  return new Blob([view.buffer], {type: 'audio/wav'});
}

/**
 * Runs beside ConversationRecorder on the same microphone stream.
 * Modes: 'on_request' sends audio only when the person asks; 'auto' checks every
 * few seconds, using a short clip so a cue can arrive close to real time, and
 * skips silent windows. Any failure leaves the recording alone.
 */
export const AUTO_PROMPT_INTERVAL_MS = 5000;
export const AUTO_PROMPT_WINDOW_SECONDS = 12;
export const AUTO_PROMPT_LEAD_MS = 1500;

export class LiveFacilitator {
  constructor({request, onChange = () => {}, AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext,
    intervalMs = AUTO_PROMPT_INTERVAL_MS, windowSeconds = 60, autoWindowSeconds = AUTO_PROMPT_WINDOW_SECONDS,
    leadMs = AUTO_PROMPT_LEAD_MS, minSeconds = 2, silence = 0.002, maxFailures = 3, timers = globalThis, onAudio = null}) {
    Object.assign(this, {request, onChange, AudioContextClass, intervalMs, windowSeconds, autoWindowSeconds, leadMs, minSeconds, silence, maxFailures, timers, onAudio});
    this.mode = 'off';
    this.state = 'off'; // off | listening | preparing | unavailable | paused
    this.message = '';
    this.guidance = null;
    this.capture = null;
    this.context = null;
    this.audio = null;
    this.pending = null;
    this.pendingRequested = false;
    this.failures = 0;
    this.timer = null;
    this.lead = null;
  }

  get active() { return Boolean(this.processor); }
  get preparing() { return Boolean(this.pending); }

  update(state, message = this.message) {
    this.state = state;
    this.message = message;
    this.onChange(this);
  }

  // Call from a tap or click: phones only start audio processing after a user gesture.
  prepare() {
    if (!this.AudioContextClass) return;
    try {
      if (!this.context || this.context.state === 'closed') this.context = new this.AudioContextClass();
      this.context.resume?.()?.catch?.(() => {});
    } catch {
      this.context = null;
    }
  }

  attach(stream, mode, capture) {
    if (this.active) { this.setMode(mode); return true; }
    if (capture !== this.capture) this.guidance = null;
    this.capture = capture;
    this.failures = 0;
    try {
      this.prepare();
      const context = this.context;
      if (!stream || !context?.createMediaStreamSource || !context.createScriptProcessor) throw new Error('Unsupported');
      const rate = Math.min(PROMPT_RATE, context.sampleRate);
      const downsampler = new Downsampler(context.sampleRate, rate);
      this.audio = new RollingAudio(rate, this.windowSeconds);
      this.source = context.createMediaStreamSource(stream);
      this.processor = context.createScriptProcessor(4096, 1, 1);
      // The processor must reach the destination to run; a zero gain keeps it silent.
      this.silent = context.createGain();
      this.silent.gain.value = 0;
      this.processor.onaudioprocess = event => {
        const chunk = downsampler.process(event.inputBuffer.getChannelData(0));
        this.audio?.push(chunk);
        this.onAudio?.(chunk, rate); // Instant prompts stream the same 16 kHz audio while they are on.
      };
      this.source.connect(this.processor);
      this.processor.connect(this.silent);
      this.silent.connect(context.destination);
    } catch {
      this.release();
      this.update('unavailable', 'Prompts aren’t available in this browser. Your recording continues.');
      return false;
    }
    this.state = 'listening';
    this.message = '';
    this.setMode(mode);
    return true;
  }

  setMode(mode, {lead = true} = {}) {
    this.mode = mode;
    this.timers.clearInterval(this.timer);
    this.timers.clearTimeout?.(this.lead);
    this.timer = null;
    this.lead = null;
    if (this.active && mode === 'auto') {
      this.timer = this.timers.setInterval(() => this.ask(false), this.intervalMs);
      // The first cue should not wait out the whole interval. A manual ask restarts the interval only.
      if (lead && typeof this.timers.setTimeout === 'function') this.lead = this.timers.setTimeout(() => this.ask(false), this.leadMs);
    }
    this.onChange(this);
  }

  async ask(requested = false) {
    if (!this.active || this.pending) return false;
    if (requested) this.context?.resume?.()?.catch?.(() => {});
    const samples = this.audio.latest(requested ? this.windowSeconds : this.autoWindowSeconds);
    const seconds = samples.length / this.audio.sampleRate;
    if (seconds < this.minSeconds) {
      if (requested) this.update(this.state, 'A prompt needs a few seconds of conversation first. Try again in a moment.');
      return false;
    }
    // A silent window has nothing new to respond to; only a direct request is sent anyway.
    if (!requested && rootMeanSquare(samples) < this.silence) return false;
    if (requested && this.mode === 'auto') this.setMode('auto', {lead: false}); // The next quiet check waits a full interval.
    const pending = new AbortController();
    this.pending = pending;
    this.pendingRequested = requested;
    this.update('preparing', requested ? '' : this.message);
    try {
      const guidance = await this.request({audio: encodeWav(samples, this.audio.sampleRate), seconds, requested, mode: this.mode, signal: pending.signal});
      if (this.pending !== pending) return false; // Stopped while waiting; this prompt is never shown.
      this.pending = null;
      this.failures = 0;
      this.guidance = guidance;
      this.update('listening', '');
      return true;
    } catch (error) {
      if (this.pending !== pending) return false;
      this.pending = null;
      this.failures += 1;
      if (this.failures >= this.maxFailures) {
        this.stop();
        this.update('paused', 'Prompts stopped after repeated problems. Your recording continues.');
      } else {
        this.update('listening', `${error?.message || 'The prompt couldn’t be updated.'} Your recording continues.`);
      }
      return false;
    }
  }

  stop() {
    this.timers.clearInterval(this.timer);
    this.timers.clearTimeout?.(this.lead);
    this.timer = null;
    this.lead = null;
    const pending = this.pending;
    this.pending = null;
    pending?.abort();
    this.release();
    if (this.state !== 'off') this.update('off', '');
  }

  reset() {
    this.stop();
    this.guidance = null;
    this.capture = null;
    this.mode = 'off';
    this.message = '';
  }

  release() {
    if (this.processor) this.processor.onaudioprocess = null;
    for (const audioNode of [this.source, this.processor, this.silent]) {
      try { audioNode?.disconnect(); } catch { /* already disconnected */ }
    }
    this.source = null;
    this.processor = null;
    this.silent = null;
    this.audio = null;
    const context = this.context;
    this.context = null;
    try { context?.close?.()?.catch?.(() => {}); } catch { /* already closed */ }
  }
}
