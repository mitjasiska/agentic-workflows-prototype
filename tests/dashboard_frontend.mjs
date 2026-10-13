// Optional developer unit checks using Node built-ins and a small DOM double.
// These check display/refresh behavior, not browser rendering or layout.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const app = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const settle = () => new Promise((resolve) => setImmediate(resolve));

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
  return {projection: {tasks, projects: projects.map((name) => ({linear_project: name, tasks: tasks.map((item, i) => item.project.linear_project === name ? i : -1).filter((i) => i >= 0)})), unverified_project_tasks: []},
    discovery: {state: 'available', uncertainties: []}, stale: false, error: null,
    checked_at: new Date().toISOString(), changed_at: new Date().toISOString(), verified_at: new Date().toISOString(), refresh_seconds: 5};
}

function harness(initial = snapshot()) {
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
    click() { this.focus(); this.listeners.click?.(); }
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
  let current = initial, failed = false;
  class EventSource {
    constructor(url) { this.url = url; this.listeners = {}; streams.push(this); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    close() { this.closed = true; }
  }
  const context = vm.createContext({document, window: browser, EventSource, AbortController,
    fetch: async (url, options) => { calls.push({url, options}); if (failed) throw new Error('offline'); return {ok: true, json: async () => structuredClone(current)}; },
    setInterval: (fn, ms) => intervals.push({fn, ms}), setTimeout: () => 1, clearTimeout: () => {}, location: {reload() {}},
  });
  vm.runInContext(app, context);
  return {ids, document, calls, intervals, streams, browser,
    update(value) { current = value; streams[0].listeners.invalidate(); },
    offline(value) { failed = value; },
    refresh() { intervals.find((item) => item.ms === 15000).fn(); },
    buttons(id) { return ids[id].querySelectorAll('button'); },
    details(key) { return ids['task-detail'].querySelectorAll('details').find((item) => item.dataset.sectionKey === key); },
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
  assert.equal(h.calls.length, 1);
  assert.equal(h.streams[0].url, '/api/events');
  h.streams[0].onopen(); await settle();
  h.update(snapshot([task('DEV-7', 'Alpha', 'clean', 'review')])); await settle();
  assert.match(h.ids['task-detail'].textContent, /DEV-7Clean/);
  h.streams[0].onerror(); assert.match(h.ids.connection.textContent, /Reconnecting/);
  h.streams[0].onopen(); await settle();
  h.refresh(); await settle();
  assert.equal(h.calls.length, 5);
  assert.ok(h.calls.every(({url, options}) => url === '/api/snapshot' && options.cache === 'no-store' && !options.method));
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
  assert.equal(h.ids['task-detail'].querySelectorAll('button').length, 0);
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
