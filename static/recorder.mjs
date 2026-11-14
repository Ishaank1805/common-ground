export class ConversationRecorder {
  constructor({onChange, save, mediaDevices = globalThis.navigator?.mediaDevices, Recorder = globalThis.MediaRecorder}) {
    this.onChange = onChange;
    this.save = save;
    this.mediaDevices = mediaDevices;
    this.Recorder = Recorder;
    this.state = 'idle';
    this.blob = null;
    this.message = '';
    this.stream = null;
    this.recorder = null;
    this.timer = null;
  }

  get pending() { return ['requesting', 'recording', 'stopping', 'saving'].includes(this.state) || Boolean(this.blob); }
  update(state, message = '') {
    this.state = state;
    this.message = message;
    this.onChange(this);
  }
  elapsed() { return this.startedAt ? Math.floor(((this.endedAt ? new Date(this.endedAt).getTime() : Date.now()) - new Date(this.startedAt).getTime()) / 1000) : 0; }

  async start(sessionId) {
    if (this.pending) return;
    if (!this.mediaDevices?.getUserMedia || !this.Recorder) {
      this.update('error', 'Recording needs a supported browser and a secure HTTPS connection (or localhost on this computer).');
      return;
    }
    this.sessionId = sessionId;
    this.startedAt = null;
    this.endedAt = null;
    this.chunks = [];
    this.interrupted = false;
    this.update('requesting', 'Allow microphone access to begin.');
    try {
      this.stream = await this.mediaDevices.getUserMedia({audio: true});
      const mimeType = ['audio/mp4;codecs=mp4a.40.2', 'audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'].find(type => this.Recorder.isTypeSupported(type));
      this.recorder = mimeType ? new this.Recorder(this.stream, {mimeType}) : new this.Recorder(this.stream);
      this.recorder.addEventListener('dataavailable', event => { if (event.data.size) this.chunks.push(event.data); });
      this.recorder.addEventListener('error', () => this.interrupt());
      this.recorder.addEventListener('stop', () => this.captureStopped());
      this.stream.getTracks().forEach(track => {
        track.addEventListener('ended', () => this.interrupt());
        track.addEventListener('mute', () => {
          if (this.state !== 'recording') return;
          this.interrupted = true;
          this.update('recording', 'The microphone was interrupted. Some audio may be missing.');
        });
        track.addEventListener('unmute', () => {
          if (this.state === 'recording') this.update('recording', 'Recording resumed. Earlier audio may be missing.');
        });
      });
      this.recorder.start(1000);
      this.startedAt = new Date().toISOString();
      this.update('recording', 'Recording. Keep this page open.');
      this.timer = setInterval(() => this.onChange(this), 1000);
    } catch (error) {
      this.release();
      const message = error.name === 'NotAllowedError' ? 'Microphone access was denied. Allow it in your browser settings and try again.' : 'The microphone could not start. Check that it is available and try again.';
      this.update('error', message);
    }
  }

  // The conversation so far as one playable file: MediaRecorder chunks from the start concatenate cleanly.
  snapshot() {
    if (!this.chunks?.length) return null;
    return new Blob(this.chunks, {type: this.recorder?.mimeType || this.chunks[0]?.type || 'audio/webm'});
  }

  stop() {
    if (this.state !== 'recording') return;
    this.endedAt = new Date().toISOString();
    this.update('stopping', 'Finishing the recording…');
    if (this.recorder?.state !== 'inactive') this.recorder.stop();
  }
  interrupt() {
    if (!['recording', 'stopping'].includes(this.state)) return;
    this.interrupted = true;
    this.endedAt = new Date().toISOString();
    this.update('stopping', 'Recording was interrupted. Keeping the audio captured so far…');
    if (this.recorder?.state !== 'inactive') this.recorder.stop();
  }
  release() {
    clearInterval(this.timer);
    this.timer = null;
    this.stream?.getTracks().forEach(track => track.stop());
    this.stream = null;
  }
  async captureStopped() {
    if (!this.endedAt) {
      this.endedAt = new Date().toISOString();
      this.interrupted = true;
    }
    this.release();
    this.blob = new Blob(this.chunks, {type: this.recorder.mimeType || this.chunks[0]?.type || 'audio/webm'});
    this.chunks = [];
    if (!this.blob.size) {
      this.blob = null;
      this.update('error', 'No audio was captured. Please start a new recording.');
      return;
    }
    await this.persist();
  }
  async persist() {
    if (!this.blob || this.state === 'saving') return;
    this.update('saving', this.interrupted ? 'Recording was interrupted. Saving the captured audio…' : 'Saving your recording as MP4…');
    try {
      const session = await this.save(this.sessionId, this.blob, this.startedAt, this.endedAt);
      this.blob = null;
      this.savedSession = session;
      this.update('saved', this.interrupted ? 'Saved captured audio. The recording was interrupted and may be incomplete.' : 'Recording saved.');
    } catch (error) {
      this.update('save_failed', `${error.message} Your captured audio is still here. Retry saving or download a backup before leaving.`);
    }
  }
  discard() {
    if (this.state !== 'save_failed') return;
    this.blob = null;
    this.reset();
  }
  reset() {
    if (this.pending) throw new Error('Save your captured audio before changing conversations.');
    this.release();
    this.startedAt = null;
    this.endedAt = null;
    this.savedSession = null;
    this.state = 'idle';
    this.message = '';
  }
}
