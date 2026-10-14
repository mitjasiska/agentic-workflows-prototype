// Optional developer unit checks using Node built-ins and a small DOM double.
// These check display/refresh behavior, not browser rendering or layout.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const app = readFileSync(new URL('../task_start/web/app.js', import.meta.url), 'utf8');
const settle = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

function task(issue, project, state = 'running', phase = 'implementation') {
  return {
    issue, project: {linear_project: project, base_branch: 'main', state: 'verified'},
    repository: {path: '/projects/shared-repository'},
    workspace: {path: `/worktrees/${issue}`, branch: issue.toLowerCase(), workspace_id: issue, registration: 'registered'},
    state, phase, controller: 'held',
    loop: {state, passes: 3, reviews: 1, pause_requested: false, next_phase: phase},
    guidance: {message: `Canonical guidance for ${issue}.`, command: null, options: [], human_action_required: state === 'paused', reconciliation_required: false},
    review: {state: 'not_recorded', acceptance: 'absent'}, integration: null,
    publication: {state: 'not_recorded', current_acceptance_published: false, merge: 'not_observed'},
    contexts: {implementation: {context_id: `${issue}-I1`, agent: 'codex', model: 'synthetic-model', mode: 'high', state: 'active', worktree: `/worktrees/${issue}`, session_id: 'private-session-id'}, review: null, integration: null},
    uncertainties: [], history: {limit: 64, truncated: false, entries: [{source: 'loop', phase: 'implementation', state: 'completed', context_id: `${issue}-I1`, pass_id: 'synthetic-pass', timestamp: null}]},
  };
}
function snapshot(tasks = [task('DEV-7', 'Alpha'), task('DEV-8', 'Alpha', 'paused', 'review'), task('DEV-9', 'Beta', 'clean', 'review')]) {
  const projects = [...new Set(tasks.map((item) => item.project.linear_project))];
  return {revision: 1, projection: {tasks, projects: projects.map((name) => ({linear_project: name, tasks: tasks.map((item, i) => item.project.linear_project === name ? i : -1).filter((i) => i >= 0)})), unverified_project_tasks: []},
    discovery: {state: 'available', uncertainties: []}, stale: false, error: null,
    checked_at: new Date().toISOString(), changed_at: new Date().toISOString(), verified_at: new Date().toISOString(), refresh_seconds: 5};
}

function harness(initial = snapshot(), reportReader = null) {
  let document;
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this._text = ''; this.className = ''; this.open = false; }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
    set innerHTML(_) { throw new Error('Projected data must never be interpolated as HTML'); }
    append(...items) { this.children.push(...items); }
    prepend(...items) { this.children.unshift(...items); }
    replaceChildren(...items) { this._text = ''; this.children = items; }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    focus() { document.activeElement = this; }
    scrollIntoView() { this.revealed = true; }
    click() { if (!this.disabled) { this.focus(); this.listeners.click?.(); } }
    select() { this.selectionStart = 0; this.selectionEnd = this.value.length; }
    querySelectorAll(selector) {
      return this.children.flatMap((child) => [child, ...child.querySelectorAll('*')]).filter((child) => selector === '*' || selector === child.tagName || selector === '[data-focus-key]' && child.dataset.focusKey);
    }
    get classList() {
      return {add: (name) => this.className += ` ${name}`, toggle: (name, on) => { this.className = this.className.split(' ').filter((item) => item !== name).concat(on ? [name] : []).join(' '); }};
    }
  }
  const ids = Object.fromEntries(['connection', 'overview', 'projects', 'tasks-title', 'task-count', 'tasks', 'task-detail', 'notices', 'freshness', 'observation-times'].map((id) => [id, new Element('section')]));
  document = {activeElement: null, createElement: (tag) => new Element(tag), getElementById: (id) => ids[id], querySelectorAll: (selector) => Object.values(ids).flatMap((element) => element.querySelectorAll(selector))};
  const browser = {listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }};
  const calls = [], intervals = [], streams = [];
  const clipboard = [];
  const navigator = {clipboard: {writeText: async (text) => clipboard.push(text)}};
  let current = initial, failed = false;
  class EventSource {
    constructor(url) { this.url = url; this.listeners = {}; streams.push(this); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    close() { this.closed = true; }
  }
  const context = vm.createContext({document, window: browser, EventSource, AbortController, navigator,
    fetch: async (url, options) => {
      calls.push({url, options});
      if (failed) throw new Error('offline');
      if (url.startsWith('/api/report?')) {
        const query = new URL(url, 'http://localhost').searchParams;
        const index = Number(query.get('task')), revision = Number(query.get('revision'));
        const selected = current.projection.tasks[index];
        const identity = [selected.issue, selected.repository.path, selected.workspace.path, selected.workspace.branch, selected.workspace.workspace_id, selected.workspace.endpoint ?? null];
        const result = reportReader ? await reportReader({index, revision, options}) : {
          kind: 'report', message: 'Saved loop report.', text: `${current.projection.tasks[index].issue} full report\nCheck full: not_run — CI pending`,
        };
        return {ok: true, json: async () => ({revision, task: index, identity, ...result})};
      }
      return {ok: true, json: async () => structuredClone(current)};
    },
    setInterval: (fn, ms) => intervals.push({fn, ms}), setTimeout: () => 1, clearTimeout: () => {}, location: {reload() {}},
  });
  vm.runInContext(app, context);
  return {ids, document, calls, intervals, streams, browser, clipboard, navigator,
    reportReader(fn) { reportReader = fn; },
    update(value) { current = value; streams[0].listeners.invalidate(); },
    offline(value) { failed = value; },
    refresh() { intervals.find((item) => item.ms === 15000).fn(); },
    buttons(id) { return ids[id].querySelectorAll('button'); },
    details(key) { return ids['task-detail'].querySelectorAll('details').find((item) => item.dataset.sectionKey === key); },
    reportText() { return ids['task-detail'].querySelectorAll('textarea')[0]; },
    action(name) { return ids['task-detail'].querySelectorAll('button').find((item) => item.textContent === name); },
  };
}

test('project-first navigation, status hierarchy, and canonical attention guidance', async () => {
  const h = harness(); await settle();
  assert.equal(h.buttons('projects').length, 2, 'Shared repositories must not replace project grouping');
  assert.match(h.ids.overview.textContent, /1Running/);
  assert.match(h.ids.overview.textContent, /1Need attention/);
  assert.match(h.ids.overview.textContent, /1Clean/);
  assert.equal(h.buttons('tasks').length, 2);
  h.buttons('tasks')[1].click();
  assert.match(h.ids['task-detail'].textContent, /DEV-8/);
  assert.match(h.ids['task-detail'].textContent, /Current phaseReview/);
  assert.match(h.ids['task-detail'].textContent, /3 passes · 1 reviews/);
  assert.match(h.ids['task-detail'].textContent, /Needs your attentionCanonical guidance for DEV-8\./);
  h.buttons('projects')[1].click();
  assert.equal(h.buttons('tasks').length, 1);
  assert.match(h.ids['task-detail'].textContent, /DEV-9Clean/);
});

test('IDs and history start collapsed; expanded sections and focus survive live refresh', async () => {
  const h = harness(); await settle();
  for (const section of h.ids['task-detail'].querySelectorAll('details')) assert.equal(section.open, false);
  const history = h.details('history'); history.open = true; history.children[0].focus();
  h.details('workspace').open = true;
  const changed = snapshot(); changed.projection.tasks[0].loop.passes = 4;
  h.update(changed); await settle();
  assert.equal(h.details('history').open, true);
  assert.equal(h.details('workspace').open, true);
  assert.equal(h.document.activeElement, h.details('history').children[0]);
  assert.match(h.ids['task-detail'].textContent, /4 passes/);
  h.buttons('tasks')[1].click();
  assert.equal(h.details('history').open, false, 'Selection should not inherit another task’s disclosure state');
});

test('selecting a task in the stacked mobile view brings its detail into focus', async () => {
  const h = harness(); await settle();
  h.browser.matchMedia = () => ({matches: true});
  h.buttons('tasks')[1].click();
  assert.equal(h.document.activeElement, h.ids['task-detail']);
  assert.equal(h.ids['task-detail'].revealed, true);
  assert.match(h.ids['task-detail'].textContent, /DEV-8/);
});

test('phase colors retain exact canonical state and unknown guidance is never inferred', async () => {
  const value = snapshot([task('DEV-7', 'Alpha', 'running', 'review')]);
  const h = harness(value); await settle();
  let state = h.ids['task-detail'].querySelectorAll('span').find((item) => item.className === 'badge');
  assert.equal(state.dataset.state, 'running'); assert.equal(state.dataset.phase, 'review');
  const changed = structuredClone(value); changed.projection.tasks[0].state = 'uncertain';
  changed.projection.tasks[0].guidance = {message: 'Inspect incomplete evidence; no recovery action is inferred.', human_action_required: true, reconciliation_required: true, options: [], command: null};
  h.update(changed); await settle();
  state = h.ids['task-detail'].querySelectorAll('span').find((item) => item.className === 'badge');
  assert.equal(state.dataset.state, 'uncertain');
  assert.match(h.ids['task-detail'].textContent, /Inspect incomplete evidence; no recovery action is inferred\./);
  assert.doesNotMatch(h.ids['task-detail'].textContent, /recoverable/i);
});

test('stream open, invalidation, reconnect, and cached polling refetch the full snapshot', async () => {
  const h = harness(); await settle();
  assert.equal(h.calls.filter(({url}) => url === '/api/snapshot').length, 1);
  assert.equal(h.streams[0].url, '/api/events');
  h.streams[0].onopen(); await settle();
  h.update(snapshot([task('DEV-7', 'Alpha', 'clean', 'review')])); await settle();
  assert.match(h.ids['task-detail'].textContent, /DEV-7Clean/);
  h.streams[0].onerror(); assert.match(h.ids.connection.textContent, /Reconnecting/);
  h.streams[0].onopen(); await settle();
  h.refresh(); await settle();
  assert.equal(h.calls.filter(({url}) => url === '/api/snapshot').length, 5);
  assert.ok(h.calls.every(({url, options}) => (url === '/api/snapshot' || url.startsWith('/api/report?')) && options.cache === 'no-store' && !options.method));
  h.browser.listeners.pagehide(); assert.equal(h.streams[0].closed, true);
});

test('stale snapshots keep tasks visible and confirmed removal clears selected details', async () => {
  const h = harness(); await settle();
  const stale = snapshot(); stale.stale = true;
  stale.discovery = {state: 'unavailable', uncertainties: [{message: 'Herdr unavailable.'}]};
  h.update(stale); await settle();
  assert.equal(h.buttons('tasks').length, 2);
  assert.match(h.ids.notices.textContent, /Stale \/ unverified/);
  assert.match(h.ids.notices.textContent, /Herdr unavailable/);
  assert.match(h.ids.overview.textContent, /unverified/);
  h.update(snapshot([])); await settle();
  assert.equal(h.buttons('tasks').length, 0);
  assert.match(h.ids.tasks.textContent, /No registered AWL task workspaces/);
  assert.match(h.ids['task-detail'].textContent, /no longer in the registered task overview/);
  assert.doesNotMatch(h.ids['task-detail'].textContent, /private-session-id/);
});

test('unknown initial discovery and network failures do not claim a verified empty view', async () => {
  const unknown = snapshot([]); unknown.projection.tasks = null; unknown.stale = true; unknown.verified_at = null;
  unknown.discovery = {state: 'unavailable', uncertainties: []};
  const h = harness(unknown); await settle();
  assert.match(h.ids.tasks.textContent, /empty view does not confirm cleanup/);
  assert.match(h.ids.overview.textContent, /—/);
  h.offline(true); h.refresh(); await settle();
  assert.match(h.ids.notices.textContent, /Cannot reach the dashboard server/);
  assert.match(h.ids.connection.textContent, /Disconnected/);
  h.offline(false); h.update(snapshot()); await settle();
  assert.doesNotMatch(h.ids.notices.textContent, /Cannot reach/);
  assert.equal(h.buttons('projects').length, 2);
});

test('projected text stays literal and conditional guidance remains read-only', async () => {
  const value = snapshot([task('DEV-7', '<img src=x onerror=alert(1)>', 'paused')]);
  value.projection.tasks[0].guidance.command = 'task loop DEV-7 --continue';
  value.projection.tasks[0].guidance.options = [{command: 'task loop DEV-7 --abort', message: 'Only after deliberate abandonment and safety checks.'}];
  const h = harness(value); await settle();
  assert.match(h.buttons('projects')[0].textContent, /<img src=x onerror=alert\(1\)>/);
  assert.deepEqual(h.buttons('task-detail').map((b) => b.textContent), ['Copy report', 'Select text']);
  assert.match(h.ids['task-detail'].textContent, /task loop DEV-7 --continue/);
  assert.match(h.details('guidance').textContent, /Only after deliberate abandonment/);
  assert.equal(h.ids['task-detail'].querySelectorAll('img').length, 0);
});

test('multiple executions remain distinguishable and unverified projects stay separate', async () => {
  const value = snapshot([task('DEV-7', 'Alpha'), task('DEV-7', 'Alpha')]);
  value.projection.tasks[1].workspace = {path: '/different/task', workspace_id: 'another', branch: 'dev-7-second'};
  value.projection.tasks.push(task('DEV-8', null, 'uncertain'));
  value.projection.unverified_project_tasks = [2];
  const h = harness(value); await settle();
  assert.match(h.buttons('tasks')[1].textContent, /dev-7-second/);
  h.buttons('tasks')[1].click();
  assert.match(h.details('workspace').textContent, /\/different\/task/);
  h.buttons('projects')[1].click();
  assert.match(h.ids['tasks-title'].textContent, /Unverified project/);
  assert.match(h.ids['task-detail'].textContent, /DEV-8Uncertain/);
});

test('only the selected report is fetched and full literal text is copied after clipboard success', async () => {
  const full = 'DEV-7 loop: clean\n<img src=x onerror=alert(1)>\n' + 'Check complete: not_run — pending CI\n'.repeat(3000) + 'Final review: clean';
  const h = harness(snapshot(), () => ({kind: 'report', message: 'Saved report', text: full})); await settle();
  assert.equal(h.calls.filter(({url}) => url.startsWith('/api/report?')).length, 1);
  assert.match(h.calls[1].url, /task=0$/);
  assert.equal(h.reportText().value, full);
  assert.equal(h.reportText().readOnly, true);
  assert.equal(h.ids['task-detail'].querySelectorAll('img').length, 0);
  let complete;
  h.navigator.clipboard.writeText = (text) => { h.clipboard.push(text); return new Promise((resolve) => { complete = resolve; }); };
  h.action('Copy report').click();
  assert.equal(h.action('Copy report').disabled, true);
  assert.doesNotMatch(h.ids['task-detail'].textContent, /Report copied/);
  complete(); await settle();
  assert.deepEqual(h.clipboard, [full]);
  assert.match(h.ids['task-detail'].textContent, /Report copied\./);
  assert.equal(h.action('Copy report').disabled, false);
});

for (const rejected of [false, true]) {
  const outcome = rejected ? 'rejection' : 'success';
  test(`delayed clipboard ${outcome} survives unchanged refreshes`, async (t) => {
    for (const kind of ['report', 'diagnostics']) {
      for (const copyFinishesFirst of [false, true]) {
        await t.test(`${kind}: clipboard finishes ${copyFinishesFirst ? 'before' : 'after'} report refresh`, async () => {
          const saved = {kind, message: 'Saved task information.', text: 'DEV-7 recorded checks and limitations'};
          const h = harness(snapshot(), () => saved); await settle();
          const copying = deferred(), refreshing = deferred();
          h.navigator.clipboard.writeText = (text) => { h.clipboard.push(text); return copying.promise; };
          const copy = h.action(`Copy ${kind}`);
          copy.click();
          h.reportReader(() => refreshing.promise);
          h.refresh(); await settle();
          assert.equal(copy.disabled, true);
          assert.equal(h.reportText().value, saved.text);
          if (!copyFinishesFirst) {
            refreshing.resolve(saved); await settle();
            assert.equal(copy.disabled, true, 'An unchanged response must not allow a second pending clipboard write');
            copy.click();
            assert.equal(h.clipboard.length, 1);
          }
          if (rejected) copying.reject(new Error('Clipboard denied'));
          else copying.resolve();
          await settle();
          const expected = rejected ? /Clipboard access failed.*Select text.*manually/ : new RegExp(`${kind === 'report' ? 'Report' : 'Diagnostics'} copied\\.`);
          assert.match(h.ids['task-detail'].textContent, expected);
          assert.equal(copy.disabled, copyFinishesFirst, 'Copy stays disabled while the report is still loading');
          if (copyFinishesFirst) {
            refreshing.resolve(saved); await settle();
          }
          h.refresh(); await settle();
          assert.match(h.ids['task-detail'].textContent, expected, 'Completed feedback also survives subsequent unchanged polling');
          assert.deepEqual(h.clipboard, [saved.text]);
          assert.equal(copy.disabled, false);
          if (rejected) {
            assert.doesNotMatch(h.ids['task-detail'].textContent, /copied\./);
            assert.equal(h.details('report').open, true);
            h.action('Select text').click();
            assert.equal(h.reportText().selectionEnd, saved.text.length);
          }
        });
      }
    }
  });

  test(`delayed clipboard ${outcome} cannot affect a changed task or report`, async (t) => {
    for (const change of ['selection', 'text', 'kind', 'unavailable', 'revision', 'stale']) {
      await t.test(change, async () => {
        const saved = {kind: 'report', message: 'Saved report.', text: 'DEV-7 saved report'};
        const h = harness(snapshot(), () => saved); await settle();
        const obsolete = deferred(), current = deferred();
        h.navigator.clipboard.writeText = () => obsolete.promise;
        h.action('Copy report').click();
        if (change === 'selection') {
          h.buttons('tasks')[1].click(); await settle();
          h.buttons('tasks')[0].click(); await settle();
        } else if (change === 'revision' || change === 'stale') {
          const changed = snapshot();
          if (change === 'revision') changed.revision++;
          else changed.stale = true;
          h.update(changed); await settle();
          h.update(snapshot()); await settle();
        } else {
          const replacement = change === 'text' ? {...saved, text: 'Updated DEV-7 report'}
            : change === 'kind' ? {...saved, kind: 'diagnostics'}
              : {kind: 'unavailable', text: null, message: 'Report cannot be verified.'};
          h.reportReader(() => replacement); h.refresh(); await settle();
          h.reportReader(() => saved); h.refresh(); await settle();
        }
        // Returning to identical text must not revive an obsolete copy operation.
        assert.equal(h.reportText().value, saved.text);
        h.navigator.clipboard.writeText = () => current.promise;
        h.action('Copy report').click();
        if (rejected) obsolete.reject(new Error('Old clipboard denial'));
        else obsolete.resolve();
        await settle();
        assert.doesNotMatch(h.ids['task-detail'].textContent, /copied\.|Clipboard access failed/);
        assert.equal(h.details('report').open, false, 'An obsolete failure must not open the current disclosure');
        assert.equal(h.action('Copy report').disabled, true, 'An obsolete completion must not enable another pending copy');
        current.resolve(); await settle();
        assert.match(h.ids['task-detail'].textContent, /Report copied\./);
        assert.equal(h.action('Copy report').disabled, false);
        h.reportReader(() => ({...saved, text: 'Next report'})); h.refresh(); await settle();
        assert.doesNotMatch(h.ids['task-detail'].textContent, /copied\.|Clipboard access failed/, 'Changed reports clear completed feedback too');
      });
    }
  });
}

test('clipboard denial or missing API offers manual selection without claiming success', async () => {
  for (const unavailable of [false, true]) {
    const h = harness(); await settle();
    h.navigator.clipboard = unavailable ? undefined : {writeText: async () => { throw new Error('denied'); }};
    h.action('Copy report').click(); await settle();
    assert.match(h.ids['task-detail'].textContent, /Clipboard access failed.*Select text.*manually/);
    assert.doesNotMatch(h.ids['task-detail'].textContent, /Report copied\./);
    assert.equal(h.details('report').open, true);
    h.action('Select text').click();
    assert.equal(h.document.activeElement, h.reportText());
    assert.equal(h.reportText().selectionStart, 0);
    assert.equal(h.reportText().selectionEnd, h.reportText().value.length);
    const textarea = h.reportText();
    h.refresh(); await settle();
    assert.equal(h.reportText(), textarea, 'Polling must preserve manual selection on unchanged text');
    assert.equal(textarea.selectionEnd, textarea.value.length);
  }
});

test('unavailable checkpoint diagnostics are selectable and copied with an accurate label', async () => {
  const text = 'No saved loop report exists for this task.\nDEV-7\nRecorded guidance: inspect local evidence.';
  const h = harness(snapshot(), () => ({kind: 'diagnostics', message: 'No saved loop report exists for this task.', text})); await settle();
  assert.equal(h.reportText().value, text);
  assert.equal(h.reportText().attributes['aria-label'], 'Task diagnostics text');
  assert.equal(h.action('Copy report'), undefined);
  h.action('Copy diagnostics').click(); await settle();
  assert.deepEqual(h.clipboard, [text]);
  assert.match(h.ids['task-detail'].textContent, /Diagnostics copied\./);
  h.reportReader(() => ({kind: 'unavailable', message: 'Selected execution cannot be verified.', text: null}));
  h.refresh(); await settle();
  assert.equal(h.reportText().value, '');
  assert.equal(h.action('Copy report').disabled, true);
  assert.equal(h.action('Select text').disabled, true);
  assert.equal(h.details('report').hidden, true);
});

test('switching executions clears text immediately and discards late report responses', async () => {
  const requests = [];
  const h = harness(snapshot(), ({index, options}) => new Promise((resolve) => requests.push({index, options, resolve}))); await settle();
  const oldCopy = h.action('Copy report');
  h.buttons('tasks')[1].click(); await settle();
  assert.equal(requests[0].options.signal.aborted, true);
  requests[1].resolve({kind: 'report', message: 'Saved report', text: 'DEV-8 selected report'}); await settle();
  requests[0].resolve({kind: 'report', message: 'Saved report', text: 'DEV-7 WRONG report'}); await settle();
  assert.equal(h.reportText().value, 'DEV-8 selected report');
  oldCopy.click();
  h.action('Copy report').click(); await settle();
  assert.deepEqual(h.clipboard, ['DEV-8 selected report']);
  assert.equal(requests.length, 2);
});

test('refresh refetches prose without overview changes and supersedes older pending reads', async () => {
  const h = harness(); await settle();
  const pending = [];
  h.reportReader(() => new Promise((resolve) => pending.push(resolve)));
  h.refresh(); await settle();
  assert.equal(h.action('Copy report').disabled, true);
  h.action('Copy report').click(); assert.deepEqual(h.clipboard, []);
  h.refresh(); await settle();
  pending[1]({kind: 'report', message: 'Saved report', text: 'Latest full prose'}); await settle();
  pending[0]({kind: 'report', message: 'Saved report', text: 'STALE prose'}); await settle();
  assert.equal(h.reportText().value, 'Latest full prose');
  h.action('Copy report').click(); await settle();
  assert.deepEqual(h.clipboard, ['Latest full prose']);
});

test('a changed snapshot immediately removes prior success before the replacement report arrives', async () => {
  const h = harness(); await settle();
  let complete;
  h.reportReader(() => new Promise((resolve) => { complete = resolve; }));
  const changed = snapshot(); changed.revision = 2; changed.projection.tasks[0].state = 'stale_review';
  h.update(changed); await settle();
  assert.equal(h.reportText().value, '');
  assert.equal(h.action('Copy report').disabled, true);
  complete({kind: 'diagnostics', message: 'Report unavailable: stale review', text: 'DEV-7 stale review'}); await settle();
  assert.equal(h.reportText().value, 'DEV-7 stale review');
});

test('same-issue executions fetch and copy their own selected index and identity', async () => {
  const value = snapshot([task('DEV-7', 'Alpha'), task('DEV-7', 'Alpha')]);
  value.projection.tasks[1].workspace = {path: '/other/task', branch: 'dev-7-other', workspace_id: 'w2'};
  const h = harness(value, ({index}) => ({kind: 'report', message: 'Saved report', text: `Execution ${index} report`})); await settle();
  h.buttons('tasks')[1].click(); await settle();
  h.action('Copy report').click(); await settle();
  assert.deepEqual(h.clipboard, ['Execution 1 report']);
  assert.match(h.calls.at(-1).url, /task=1$/);
});

test('changed revisions, wrong response identity, network failures and removal cannot retain copyable reports', async () => {
  const h = harness(); await settle();
  for (const mismatch of [{revision: 99}, {task: 1}, {identity: ['DEV-8', '/other', '/other', 'dev-8', 'w2', '/other.sock']}]) {
    h.reportReader(() => ({kind: 'report', message: 'Incorrect identity', text: 'WRONG', ...mismatch}));
    h.refresh(); await settle();
    assert.doesNotMatch(h.reportText().value, /WRONG/);
    assert.match(h.reportText().value, /Retained diagnostics.*may be stale/);
    assert.equal(h.action('Copy report'), undefined);
    assert.equal(h.action('Copy diagnostics').disabled, false);
  }
  h.reportReader(null); h.refresh(); await settle();
  assert.match(h.reportText().value, /DEV-7/);
  h.offline(true); h.refresh(); await settle();
  assert.match(h.reportText().value, /Retained diagnostics.*may be stale/);
  assert.doesNotMatch(h.reportText().value, /DEV-7 full report/);
  h.action('Copy diagnostics').click(); await settle();
  assert.match(h.clipboard[0], /Canonical guidance for DEV-7/);
  h.offline(false);
  let complete;
  h.reportReader(() => new Promise((resolve) => { complete = resolve; }));
  h.refresh(); await settle();
  h.update(snapshot([])); await settle();
  complete({kind: 'report', message: 'Deleted', text: 'REMOVED report'}); await settle();
  assert.equal(h.reportText(), undefined);
  assert.equal(h.action('Copy report'), undefined);
});

test('stale discovery clears saved success while diagnostics are fetched and selection changes ignore clipboard completion', async () => {
  const h = harness(); await settle();
  let copied;
  h.navigator.clipboard.writeText = () => new Promise((resolve) => { copied = resolve; });
  h.action('Copy report').click();
  h.buttons('tasks')[1].click(); await settle();
  copied(); await settle();
  assert.doesNotMatch(h.ids['task-detail'].textContent, /Report copied\./);
  let diagnostics;
  h.reportReader(() => new Promise((resolve) => { diagnostics = resolve; }));
  const stale = snapshot(); stale.stale = true; stale.revision = 2;
  h.update(stale); await settle();
  assert.equal(h.reportText().value, '');
  assert.equal(h.action('Copy report').disabled, true);
  diagnostics({kind: 'diagnostics', message: 'Unverified snapshot', text: 'DEV-8 stale guidance'}); await settle();
  assert.equal(h.action('Copy diagnostics').disabled, false);
  assert.equal(h.reportText().value, 'DEV-8 stale guidance');
});
