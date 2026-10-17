const fs = require('fs');
const path = require('path');
const {installGlobals, Element} = require('./dom.js');
const {document, window} = installGlobals();
const assets = path.resolve(__dirname, '../../osu_toolbox/gui/assets');
for (const match of fs.readFileSync(path.join(assets, 'index.html'), 'utf8').matchAll(/id="([^"]+)"/g)) document.register(match[1]);
document.getElementById('confirm-dialog').hidden = true;
const bootstrap = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const pending = () => { let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject}; };
const notes = {calls: [], drains: 0};
let submission = pending(), picker = pending(), prefill = pending(), drain = pending(), timer;
window.setInterval = (action) => {timer = action; return 1;};
window.pywebview.api = {
  get_bootstrap: async () => bootstrap,
  start_tool: (tool, params, token) => {notes.calls.push({tool, params, token}); return submission.promise;},
  pick_path: () => picker.promise,
  prefill_tool: () => prefill.promise,
  drain_events: () => {notes.drains++; return drain.promise;},
};
const settle = () => new Promise(resolve => setImmediate(resolve));
const go = async id => {window.location.hash = '#tool/' + id; await window.dispatch('hashchange');};
(async () => {
  new Function('window', 'document', fs.readFileSync(path.join(assets, 'app.js'), 'utf8'))(window, document);
  await window.dispatch('pywebviewready');
  await go('a');
  const form = document.getElementById('tool-form');
  const first = form.dispatch('submit');
  await form.dispatch('submit');
  notes.preparing = {disabled: document.getElementById('start-button').disabled, label: document.getElementById('start-button').textContent, calls: notes.calls.length};
  await go('b');
  submission.resolve({status: 'confirmation_required', confirmation_token: 'old', confirmation: {message: 'overwrite'}});
  await first;
  notes.stale_confirmation_hidden = document.getElementById('confirm-dialog').hidden;
  submission = pending();
  await go('a');
  const confirm = form.dispatch('submit');
  submission.resolve({status: 'confirmation_required', confirmation_token: 'valid', confirmation: {message: 'overwrite'}});
  await confirm;
  await go('b');
  submission = pending();
  const accepted = document.getElementById('confirm-accept').dispatch('click');
  submission.resolve({status: 'started', run_id: 'run'});
  await accepted;
  notes.confirmed_tool = notes.calls[notes.calls.length - 1].tool;
  const log = document.getElementById('log-output');
  const backgroundPoll = timer();
  drain.resolve({events: [
    {run_id: 'run', type: 'log', payload: 'background a'},
    {run_id: 'run', type: 'state', payload: {state: 'running'}},
  ]});
  await backgroundPoll;
  notes.other_tool_log = log.textContent;
  notes.background_running = document.getElementById('header-status').textContent;
  await go('a');
  drain = pending();
  const previousDrains = notes.drains;
  let writes = 0;
  const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'textContent');
  Object.defineProperty(log, 'textContent', {
    get() {return descriptor.get.call(this);},
    set(value) {writes++; descriptor.set.call(this, value);},
  });
  log.scrollHeight = 2000; log.clientHeight = 100; log.scrollTop = 200;
  const poll = timer();
  await timer();
  drain.resolve({events: [
    ...Array.from({length: 200}, (_, n) => ({run_id: 'run', type: 'log', payload: 'line ' + n})),
    {run_id: 'run', type: 'state', payload: {state: 'done', message: 'partial', has_warnings: true}},
  ]});
  await poll;
  notes.log = {writes, lines: log.textContent.split('\n').length, scrollTop: log.scrollTop, drains: notes.drains - previousDrains};
  notes.partial_toast = document.getElementById('toast-region').children[document.getElementById('toast-region').children.length - 1].className;
  await go('b');
  notes.switch_reset = {text: log.textContent, scrollTop: log.scrollTop};
  await go('a');
  document.getElementById('field-path').value = 'old';
  const browse = document.getElementById('form-groups').querySelector('.button-browse').dispatch('click');
  await go('b');
  document.getElementById('field-path').value = 'keep';
  picker.resolve({ok: true, path: 'late'});
  await browse;
  notes.picker_value = document.getElementById('field-path').value;
  const pathInput = document.getElementById('field-path');
  const filling = pathInput.dispatch('change');
  document.getElementById('field-value').value = 'my edit';
  prefill.resolve({status: 'ok', params: {value: 'metadata'}, warnings: []});
  await filling;
  notes.edited_value = document.getElementById('field-value').value;
  submission = pending();
  const rejected = form.dispatch('submit');
  submission.reject(new Error('disconnected'));
  await rejected;
  notes.retry_enabled = !document.getElementById('start-button').disabled;
  notes.toasts = document.getElementById('toast-region').textContent;
  await go('a');
  submission = pending();
  const next = form.dispatch('submit');
  submission.resolve({status: 'started', run_id: 'next'});
  await next;
  const logs = async (message, state) => {
    drain = pending();
    const reading = timer();
    const events = [{run_id: 'next', type: 'log', payload: message}];
    if (state) events.push({run_id: 'next', type: 'state', payload: {state, message: 'finished'}});
    drain.resolve({events});
    await reading;
  };
  await logs('first visit');
  window.location.hash = '';
  await window.dispatch('hashchange');
  await logs('while on dashboard');
  await go('a');
  notes.same_tool_reset = {text: log.textContent, scrollTop: log.scrollTop};
  await logs('second visit');
  notes.fresh_log = log.textContent;
  drain = pending();
  const stale = timer();
  await go('b');
  await go('a');
  drain.resolve({events: [{run_id: 'next', type: 'log', payload: 'late first visit'}]});
  await stale;
  notes.stale_after_reentry = log.textContent;
  await logs('new visit');
  notes.current_after_reentry = log.textContent;
  drain = pending();
  const clearing = timer();
  await document.getElementById('clear-log-button').dispatch('click');
  drain.resolve({events: [{run_id: 'next', type: 'log', payload: 'late before clear'}]});
  await clearing;
  notes.manual_clear = {text: log.textContent, scrollTop: log.scrollTop};
  await logs('after clear');
  notes.fresh_after_clear = log.textContent;
  window.location.hash = '#settings';
  await window.dispatch('hashchange');
  await logs('finishing in background', 'done');
  notes.background_done = {text: log.textContent, disabled: document.getElementById('start-button').disabled};
  await go('b');
  notes.finished_entry = log.textContent;
  process.stdout.write(JSON.stringify(notes));
})().catch(error => {process.stderr.write(error.stack); process.exitCode = 1;});
