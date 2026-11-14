// Keeps the screen on while recording: a locked phone hides prompts and some phones pause recording.
// Unsupported or refused requests change nothing; the person can still keep the screen on themselves.
export class ScreenAwake {
  constructor({wakeLock = globalThis.navigator?.wakeLock, visible = () => globalThis.document?.visibilityState === 'visible'} = {}) {
    this.wakeLock = wakeLock;
    this.visible = visible;
    this.lock = null;
    this.pending = null;
    this.wanted = false;
  }

  get supported() { return Boolean(this.wakeLock?.request); }

  // Safe to call on every render: it requests at most once and re-requests after the browser releases it.
  set(wanted) {
    this.wanted = wanted;
    if (!this.supported) return;
    if (!wanted) {
      const lock = this.lock;
      this.lock = null;
      lock?.release().catch(() => {});
      return;
    }
    if (this.lock || this.pending || !this.visible()) return;
    this.pending = this.wakeLock.request('screen').then(lock => {
      if (!this.wanted) { lock.release().catch(() => {}); return; }
      this.lock = lock;
      lock.addEventListener?.('release', () => { if (this.lock === lock) this.lock = null; });
    }).catch(() => {}).finally(() => { this.pending = null; });
  }
}
