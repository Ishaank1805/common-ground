// Accounts in the browser: sign in, create an account, recover with a recovery code, and the
// account settings dialog. The server enforces every rule; the page only guides. Forms are plain
// HTML forms with autocomplete hints, so password managers and pasting work.
const $ = selector => document.querySelector(selector);
const VIEWS = {signin: 'signin-form', register: 'register-form', recover: 'recover-form', code: 'recovery-code-view', voice: 'voice-register-view'};

async function call(path, method = 'GET', data) {
  const options = {method};
  if (data !== undefined) { options.headers = {'Content-Type': 'application/json'}; options.body = JSON.stringify(data); }
  const response = await fetch(`/api/auth/${path}`, options);
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(result.error || 'Something went wrong. Please try again.'), {status: response.status});
  return result;
}

export class Account {
  constructor({onSignedIn, canSignOut = () => ({ok: true})}) {
    Object.assign(this, {onSignedIn, canSignOut});
    this.user = null;
    this.pending = null; // Signed in, but still looking at a new recovery code.
    this.bind();
  }

  async start() {
    try { this.enter((await call('me')).user); } catch { this.show('signin'); }
  }

  // A 401 elsewhere means the sign-in ended. Hide her work, since someone else may be holding the
  // phone, but keep it in memory so signing in again carries on where she was.
  expired() {
    if (!this.user || !$('#auth-view').hidden) return;
    this.show('signin', 'You were signed out after a while without activity. Sign in again to carry on; your work on this page is kept.');
    $('#signin-username').value = this.user.username;
    $('#signin-password').focus();
  }

  show(view, message = '', error = false) {
    for (const [name, id] of Object.entries(VIEWS)) $(`#${id}`).hidden = name !== view;
    this.say('#auth-message', message, error);
    $('#auth-view').hidden = false;
    $('#workspace').hidden = true;
    $('#account-area').hidden = true;
    if ($('#account-dialog').open) $('#account-dialog').close();
    if (view !== 'code') $(`#${VIEWS[view]}`).querySelector('input')?.focus();
  }

  enter(user) {
    if (this.user && this.user.username !== user.username) { location.reload(); return; } // Someone else: start from a clean page.
    const first = !this.user;
    this.user = user;
    this.clearForms('#auth-view form');
    this.say('#auth-message', '');
    $('#auth-view').hidden = true;
    $('#workspace').hidden = false;
    $('#account-area').hidden = false;
    this.render();
    if (first) this.onSignedIn(user);
  }

  // Passwords never linger in the page, and revealed fields go back to hidden.
  clearForms(selector) {
    for (const form of document.querySelectorAll(selector)) form.reset();
    for (const counter of document.querySelectorAll('.password-count')) counter.textContent = '';
    for (const button of document.querySelectorAll('.reveal')) {
      document.getElementById(button.getAttribute('aria-controls')).type = 'password';
      button.textContent = 'Show';
      button.setAttribute('aria-pressed', 'false');
    }
  }

  render() {
    const pronouns = document.createElement('span');
    pronouns.textContent = this.user.pronouns || '';
    $('#profile').replaceChildren(document.createTextNode(`${this.user.name} `), pronouns);
    $('#account-username').textContent = this.user.username;
  }

  say(selector, text, error = false) {
    const element = $(selector);
    element.textContent = text;
    element.className = `notice${error ? ' error' : ''}`;
    element.hidden = !text;
  }

  showCode(code, message) {
    this.show('code', message);
    $('#recovery-code').textContent = code;
    $('#recovery-saved').checked = false;
    $('#recovery-continue').disabled = true;
    $('#copy-recovery-code').focus();
  }

  // Each form: no double submits; on failure, show the server's message.
  form(id, handler, messages = '#auth-message') {
    const form = $(`#${id}`);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const submit = form.querySelector('[type=submit]');
      submit.disabled = true;
      this.say(messages, '');
      try { await handler(Object.fromEntries(new FormData(form)), form); }
      catch (error) { this.say(messages, error.message, true); }
      finally { submit.disabled = false; }
    });
  }

  openSettings() {
    const {name, pronouns, gender, username} = this.user;
    this.clearForms('#account-dialog form');
    const profile = $('#profile-form').elements;
    profile.name.value = name;
    profile.pronouns.value = pronouns || '';
    profile.gender.value = gender || '';
    $('#password-form').elements.username.value = username; // Lets password managers save the new password.
    $('#new-recovery-code').hidden = true;
    $('#new-recovery-code').textContent = '';
    this.say('#account-message', '');
    this.refreshVoice();
    this.refreshTeam();
    $('#share-insights').checked = Boolean(this.user?.share_insights);
    $('#insights-status').textContent = '';
    $('#account-dialog').showModal();
  }

  async refreshVoice() {
    try {
      const {available, opted_in: optedIn, enrolled} = await (await fetch('/api/voice')).json();
      this.voiceOptIn = Boolean(optedIn);
      this.renderVoice(Boolean(available), Boolean(enrolled));
    } catch { $('#voice-section').hidden = true; }
  }

  renderVoice(available, enrolled, status) {
    $('#voice-section').hidden = !available;
    $('#voice-opt-in').checked = Boolean(this.voiceOptIn);
    for (const element of [document.querySelector('.voice-sentence'), $('#voice-status'), $('#voice-record').parentElement]) {
      element.hidden = !this.voiceOptIn;
    }
    $('#voice-delete').hidden = !enrolled;
    $('#voice-record').textContent = enrolled ? 'Record a new sample' : 'Record voice sample';
    $('#voice-status').textContent = status ?? (enrolled ? 'A voice sample is saved.' : 'No voice sample yet.');
    if (this.user) this.user.voice_sample = enrolled;
  }

  // About ten seconds of her reading the sentence. The server keeps only a voice fingerprint.
  async recordVoice() {
    if (this.voiceRecorder) { this.voiceRecorder.stop(); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({audio: true}); }
    catch { $('#voice-status').textContent = 'Allow microphone access to record a voice sample.'; return; }
    const type = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus'].find(kind => globalThis.MediaRecorder?.isTypeSupported?.(kind)) || '';
    const recorder = new MediaRecorder(stream, type ? {mimeType: type} : undefined);
    const chunks = [];
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
    const stopped = new Promise(resolve => { recorder.onstop = resolve; });
    this.voiceRecorder = recorder;
    this.voiceCancelled = false;
    recorder.start();
    $('#voice-record').textContent = 'Stop';
    $('#voice-status').textContent = 'Recording. Read the sentence aloud, then press Stop.';
    const limit = setTimeout(() => { if (recorder.state === 'recording') recorder.stop(); }, 20000);
    await stopped;
    clearTimeout(limit);
    stream.getTracks().forEach(track => track.stop());
    this.voiceRecorder = null;
    if (this.voiceCancelled) { this.renderVoice(true, Boolean(this.user?.voice_sample)); return; }
    const blob = new Blob(chunks, {type: recorder.mimeType || type || 'audio/webm'});
    $('#voice-record').disabled = true;
    $('#voice-status').textContent = 'Saving…';
    try {
      const response = await fetch('/api/voice', {method: 'POST', headers: {'Content-Type': blob.type}, body: blob});
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || 'The voice sample could not be saved. Please try again.');
      this.renderVoice(true, true, `Saved, from ${result.speech_seconds} seconds of speech. Prompts can now tell your voice from others.`);
    } catch (error) {
      this.renderVoice(true, Boolean(this.user?.voice_sample), error.message);
    } finally { $('#voice-record').disabled = false; }
  }

  // At sign-up: read the sentence aloud; recording it is her choice to use voice matching. Only a fingerprint is kept.
  async registerVoice() {
    const button = $('#voice-register-record');
    const status = $('#voice-register-status');
    if (this.voiceRecorder) { this.voiceRecorder.stop(); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({audio: true}); }
    catch { status.textContent = 'Allow microphone access to record, or skip for now.'; return; }
    const type = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus'].find(kind => globalThis.MediaRecorder?.isTypeSupported?.(kind)) || '';
    const recorder = new MediaRecorder(stream, type ? {mimeType: type} : undefined);
    const chunks = [];
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
    const stopped = new Promise(resolve => { recorder.onstop = resolve; });
    this.voiceRecorder = recorder;
    this.voiceCancelled = false;
    recorder.start();
    button.textContent = 'Stop';
    status.textContent = 'Recording. Read the sentence aloud, then press Stop.';
    const limit = setTimeout(() => { if (recorder.state === 'recording') recorder.stop(); }, 20000);
    await stopped;
    clearTimeout(limit);
    stream.getTracks().forEach(track => track.stop());
    this.voiceRecorder = null;
    button.textContent = 'Record my voice';
    if (this.voiceCancelled) return;
    const blob = new Blob(chunks, {type: recorder.mimeType || type || 'audio/webm'});
    button.disabled = true;
    status.textContent = 'Saving…';
    try {
      const optIn = await fetch('/api/voice/opt-in', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({on: true})});
      if (!optIn.ok) throw new Error('Voice registration isn’t available right now. You can skip and add it later in Account.');
      const response = await fetch('/api/voice', {method: 'POST', headers: {'Content-Type': blob.type}, body: blob});
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || 'Your voice could not be saved. Please try again.');
      status.textContent = `Saved, from ${result.speech_seconds} seconds of speech. The app will mark your voice as “You”.`;
      const user = {...this.pendingUser, voice_sample: true, voice_matching: true};
      this.pendingUser = null;
      setTimeout(() => this.enter(user), 1200);
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  async deleteVoice() {
    if (!window.confirm('Delete your voice sample? Prompts will stop telling your voice from others.')) return;
    const response = await fetch('/api/voice', {method: 'DELETE'}).catch(() => null);
    if (response?.ok) this.renderVoice(true, false, 'Voice sample deleted.');
    else $('#voice-status').textContent = 'The voice sample could not be deleted. Please try again.';
  }

  async refreshTeam() {
    $('#team-section').hidden = !this.user?.admin;
    $('#team-status').textContent = '';
    if (!this.user?.admin) return;
    const response = await fetch('/api/admin/team').catch(() => null);
    if (response?.ok) this.renderTeam((await response.json()).team);
  }

  renderTeam(team) {
    $('#team-list').replaceChildren(...team.map(member => {
      const item = document.createElement('li');
      const roles = [member.admin && 'Admin', member.insight_viewer && 'Sees insights'].filter(Boolean).join(' · ');
      item.append(document.createTextNode(`${member.username}: ${roles} `));
      for (const [role, on] of [['admin', member.admin], ['insight_viewer', member.insight_viewer]]) {
        if (!on) continue;
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'text-button danger';
        remove.textContent = role === 'admin' ? 'Remove admin' : 'Remove insights';
        remove.addEventListener('click', () => this.setRole(member.username, role, false));
        item.append(remove);
      }
      return item;
    }));
  }

  async setRole(username, role, on) {
    const response = await fetch('/api/admin/team', {method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, role, on})}).catch(() => null);
    const result = response ? await response.json().catch(() => ({})) : {};
    if (!response?.ok) {
      $('#team-status').textContent = result.error || 'That couldn’t be saved. Please try again.';
      return false;
    }
    this.renderTeam(result.team);
    $('#team-status').textContent = on ? `${username} now has this access.` : `Access removed for ${username}.`;
    if (username.toLowerCase() === this.user.username.toLowerCase() && role === 'admin' && !on) {
      this.user.admin = false;
      $('#team-section').hidden = true;
    }
    return true;
  }

  leave() {
    const check = this.canSignOut();
    if (!check.ok && check.message) this.say('#account-message', check.message, true);
    return check.ok;
  }

  bind() {
    for (const button of document.querySelectorAll('[data-auth]')) button.addEventListener('click', () => this.show(button.dataset.auth));
    for (const button of document.querySelectorAll('.reveal')) button.addEventListener('click', () => {
      const input = document.getElementById(button.getAttribute('aria-controls'));
      const reveal = input.type === 'password';
      input.type = reveal ? 'text' : 'password';
      button.textContent = reveal ? 'Hide' : 'Show';
      button.setAttribute('aria-pressed', String(reveal));
    });
    for (const counter of document.querySelectorAll('.password-count')) {
      const input = document.getElementById(counter.dataset.for);
      input.addEventListener('input', () => {
        const length = [...input.value].length;
        counter.textContent = !length ? '' : length < 15 ? `${length} of 15 characters.` : 'Long enough.';
      });
    }

    this.form('signin-form', async values => this.enter((await call('login', 'POST', values)).user));
    this.form('register-form', async values => {
      const result = await call('register', 'POST', values);
      this.pending = result.user;
      this.showCode(result.recovery_code, 'Your account is ready. One last step before you start.');
    });
    this.form('recover-form', async values => {
      const result = await call('recover', 'POST', values);
      this.pending = result.user;
      this.showCode(result.recovery_code, 'Your password is changed and you’re signed out everywhere else. Your old recovery code no longer works; this is your new one.');
    });
    $('#recovery-saved').addEventListener('change', event => { $('#recovery-continue').disabled = !event.target.checked; });
    $('#copy-recovery-code').addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText($('#recovery-code').textContent);
        this.say('#auth-message', 'Copied. Paste it somewhere private, not in a shared chat.');
      } catch { this.say('#auth-message', 'Copying isn’t available here. Write the code down instead.', true); }
    });
    $('#recovery-continue').addEventListener('click', async () => {
      const user = this.pending;
      this.pending = null;
      $('#recovery-code').textContent = '';
      // Offer to register her voice now, if this server can tell voices apart.
      const voice = await fetch('/api/voice').then(response => (response.ok ? response.json() : null)).catch(() => null);
      if (voice?.available) {
        this.pendingUser = user;
        $('#voice-register-status').textContent = '';
        this.show('voice');
        return;
      }
      this.enter(user);
    });
    $('#voice-register-record').addEventListener('click', () => this.registerVoice());
    $('#voice-register-skip').addEventListener('click', () => {
      if (this.voiceRecorder) { this.voiceCancelled = true; this.voiceRecorder.stop(); }
      const user = this.pendingUser;
      this.pendingUser = null;
      if (user) this.enter(user);
    });

    $('#open-account').addEventListener('click', () => this.openSettings());
    $('#close-account').addEventListener('click', () => $('#account-dialog').close());
    this.form('profile-form', async values => {
      this.user = (await call('profile', 'PUT', {name: values.name, pronouns: values.pronouns, gender: values.gender})).user;
      this.render();
      this.say('#account-message', 'Profile saved. New plans and summaries will use it.');
    }, '#account-message');
    this.form('password-form', async (values, form) => {
      await call('password', 'PUT', {current_password: values.current_password, new_password: values.new_password});
      form.reset();
      form.elements.username.value = this.user.username;
      this.say('#account-message', 'Password changed. You’ve been signed out on your other devices.');
    }, '#account-message');
    this.form('recovery-form', async (values, form) => {
      const {recovery_code: code} = await call('recovery-code', 'POST', {password: values.password});
      form.reset();
      $('#new-recovery-code').textContent = code;
      $('#new-recovery-code').hidden = false;
      this.say('#account-message', 'Here is your new recovery code. Save it somewhere private; the old one no longer works.');
    }, '#account-message');
    this.form('delete-account-form', async values => {
      if (!this.leave()) return;
      if (!window.confirm('Delete your account and everything in it? This can’t be undone.')) return;
      await call('account', 'DELETE', {password: values.password});
      location.reload();
    }, '#account-message');
    $('#share-insights').addEventListener('change', async event => {
      const box = event.currentTarget;
      const share = box.checked;
      const response = await fetch('/api/auth/insights', {method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({share})}).catch(() => null);
      if (response?.ok) {
        this.user = (await response.json()).user;
        $('#insights-status').textContent = share ? 'Thank you. Only anonymous counts are shared.' : 'You’ve stopped sharing.';
      } else {
        box.checked = !share;
        $('#insights-status').textContent = 'That couldn’t be saved. Please try again.';
      }
    });
    $('#voice-opt-in').addEventListener('change', async event => {
      const box = event.currentTarget;
      const on = box.checked;
      if (!on && this.user?.voice_sample && !window.confirm('Turn off voice matching? Your voice sample will be deleted.')) {
        box.checked = true;
        return;
      }
      const response = await fetch('/api/voice/opt-in', {method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({on})}).catch(() => null);
      if (!response?.ok) {
        box.checked = !on;
        return;
      }
      const state = await response.json();
      this.voiceOptIn = state.opted_in;
      if (this.user) this.user.voice_matching = state.opted_in;
      this.renderVoice(state.available, state.enrolled, on ? 'Voice matching is on. Record a sample to use it.' : 'Voice matching is off.');
    });
    $('#team-form').addEventListener('submit', async event => {
      event.preventDefault();
      const username = $('#team-username').value.trim();
      if (username && await this.setRole(username, $('#team-role').value, true)) $('#team-username').value = '';
    });
    $('#voice-record').addEventListener('click', () => this.recordVoice());
    $('#voice-delete').addEventListener('click', () => this.deleteVoice());
    $('#account-dialog').addEventListener('close', () => {
      if (this.voiceRecorder) { this.voiceCancelled = true; this.voiceRecorder.stop(); } // Closing cancels; nothing is uploaded.
    });
    $('#sign-out').addEventListener('click', async () => {
      if (!this.leave()) return;
      await call('logout', 'POST').catch(() => {});
      location.reload(); // A clean page: nothing of hers stays in memory or on screen.
    });
  }
}
