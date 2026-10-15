'use strict';

// Presentation of canonical fields only. Colors and counts do not establish
// liveness, recovery, review validity, or publication eligibility.
let snapshot = null;
let selectedProject;
let selectedTask = null;
let rendered = null;
let pending = false;
let fetching = false;
let fetchError = false;
let live = false;
let reportView = null;
let selectedIteration = null;
let reportRequest = 0;
let reportController = null;
const byId = (id) => document.getElementById(id);
const label = (value) => {
  if (value == null) return 'Unknown';
  const text = String(value).replaceAll('_', ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
};
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
};
const taskKey = (task) => JSON.stringify([task.issue, task.repository.path, task.workspace.path, task.workspace.branch, task.workspace.workspace_id, task.workspace.endpoint]);
const stamp = (value) => value ? new Date(value).toLocaleString() : 'Unknown';
function badge(state, phase) {
  const element = node('span', label(state), 'badge');
  element.dataset.state = state || 'unknown';
  element.dataset.phase = phase || 'unknown';
  return element;
}
function fields(items) {
  const list = node('dl', null, 'fields');
  for (const [name, value] of items) {
    list.append(node('dt', name), node('dd', value == null ? 'Unknown' : String(value)));
  }
  return list;
}
function section(parent, title) {
  const area = node('section', null, 'detail-section');
  area.append(node('h3', title));
  parent.append(area);
  return area;
}
function disclosure(parent, title, key) {
  const area = node('details', null, 'disclosure');
  area.dataset.sectionKey = key;
  const summary = node('summary', title);
  summary.dataset.focusKey = `detail:${selectedTask}:${key}`;
  area.append(summary);
  parent.append(area);
  return area;
}
function choice(active, key, action) {
  const button = node('button', null, 'choice');
  button.type = 'button';
  button.dataset.focusKey = key;
  button.setAttribute('aria-pressed', String(active));
  button.addEventListener('click', action);
  return button;
}
function groups(projection) {
  const result = projection ? projection.projects.map((group) => ({key: group.linear_project, title: group.linear_project, tasks: group.tasks})) : [];
  if (projection?.unverified_project_tasks.length) {
    result.push({key: null, title: 'Unverified project', tasks: projection.unverified_project_tasks});
  }
  return result;
}
function renderOverview(projection) {
  const tasks = projection?.tasks || [];
  const overview = byId('overview');
  overview.replaceChildren();
  const values = [
    ['Registered executions', tasks.length],
    ['Running', tasks.filter((task) => task.state === 'running').length],
    ['Need attention', tasks.filter((task) => task.guidance.human_action_required).length],
    ['Clean', tasks.filter((task) => task.state === 'clean').length],
  ];
  for (const [title, count] of values) {
    const metric = node('div', null, `metric${title === 'Need attention' && count > 0 ? ' attention' : ''}`);
    metric.append(node('strong', projection?.tasks == null ? '—' : count), node('span', `${title}${snapshot.stale ? ' · unverified' : ''}`));
    overview.append(metric);
  }
}
function statusCard(parent, title, state, note) {
  const card = node('section', null, 'status-card');
  card.append(node('h3', title), node('strong', label(state)), node('p', note));
  parent.append(card);
}
function clearReport() {
  reportController?.abort();
  reportRequest++;
  reportView = null;
}
function reportArea(task) {
  const key = taskKey(task);
  if (reportView?.key === key && reportView.runId === selectedIteration) return reportView.area;
  clearReport();
  const area = node('section', null, 'detail-section task-report');
  const message = node('p', 'Loading saved report…', 'muted');
  const actions = node('div', null, 'report-actions');
  const copy = node('button', 'Copy report');
  const select = node('button', 'Select text');
  for (const [button, name] of [[copy, 'copy'], [select, 'select']]) {
    button.type = 'button';
    button.disabled = true;
    button.dataset.focusKey = `report:${key}:${name}`;
  }
  const feedback = node('p', '', 'muted');
  feedback.setAttribute('role', 'status');
  const details = disclosure(area, 'Inspect report text', 'report');
  details.hidden = true;
  const text = node('textarea');
  text.readOnly = true;
  text.rows = 10;
  text.setAttribute('aria-label', 'Task report text');
  text.dataset.focusKey = `report:${key}:text`;
  details.append(text);
  actions.append(copy, select);
  const prior = task.earlier_iterations?.entries.find((entry) => entry.run_id === selectedIteration);
  area.prepend(node('h3', selectedIteration ? `Earlier iteration report · ${prior?.reviewer_context || selectedIteration}` : 'Current iteration report'), message, actions, feedback);
  if (selectedIteration) {
    const current = node('button', 'Show current report');
    current.type = 'button';
    current.addEventListener('click', () => {
      selectedIteration = null;
      renderDetail(snapshot?.projection?.tasks?.find((item) => taskKey(item) === key));
      refreshReport();
    });
    actions.append(current);
  }
  // The disclosure and textarea survive same-task renders, preserving selection.
  const view = {key, runId: selectedIteration, area, message, copy, select, feedback, details, text, data: null, loading: true, copyOperation: null};
  reportView = view;
  select.addEventListener('click', () => {
    if (reportView !== view || view.loading || !view.data?.text) return;
    details.open = true;
    text.focus();
    text.select();
  });
  copy.addEventListener('click', async () => {
    if (reportView !== view || selectedTask !== key || view.loading || view.copyOperation || !view.data?.text) return;
    const operation = {};
    view.copyOperation = operation;
    copy.disabled = true;
    feedback.textContent = '';
    try {
      await navigator.clipboard.writeText(view.data.text);
      if (reportView === view && view.copyOperation === operation) feedback.textContent = view.data.kind === 'report' ? 'Report copied.' : 'Diagnostics copied.';
    } catch (_) {
      if (reportView === view && view.copyOperation === operation) {
        feedback.textContent = 'Clipboard access failed. Use Select text, then copy manually.';
        details.open = true;
      }
    } finally {
      if (reportView === view && view.copyOperation === operation) {
        view.copyOperation = null;
        copy.disabled = view.loading || !view.data?.text;
      }
    }
  });
  return area;
}
function showReport(view, data) {
  // Clipboard operations belong to the displayed content, not a polling request.
  if (view.data?.kind !== data.kind || view.data?.text !== data.text) {
    view.copyOperation = null;
    view.feedback.textContent = '';
  }
  view.data = data;
  view.loading = false;
  view.message.textContent = data.message;
  view.copy.textContent = data.kind === 'diagnostics' ? 'Copy diagnostics' : 'Copy report';
  view.copy.disabled = !data.text || view.copyOperation !== null;
  view.select.disabled = !data.text;
  view.text.setAttribute('aria-label', data.kind === 'diagnostics' ? 'Task diagnostics text' : 'Task report text');
  // Assigning value on every poll would reset a user's manual selection.
  if (view.text.value !== (data.text || '')) view.text.value = data.text || '';
  view.details.hidden = !data.text;
}
function reportDiagnostics(message, task) {
  if (!task) return {kind: 'unavailable', message, text: null};
  const lines = [message, 'Retained diagnostics; task evidence may be stale.',
    `${task.issue} — ${task.workspace.path}`, `Last observed state: ${task.state}; phase: ${task.phase || 'unknown'}`,
    `Recorded guidance: ${task.guidance.message}`];
  if (task.guidance.command) lines.push(task.guidance.command);
  for (const option of task.guidance.options) lines.push(`Conditional guidance: ${option.message}`, option.command);
  for (const error of task.uncertainties) lines.push(error.message);
  for (const error of snapshot?.discovery?.uncertainties || []) lines.push(error.message);
  return {kind: 'diagnostics', message, text: lines.join('\n')};
}
async function refreshReport() {
  reportController?.abort();
  const request = ++reportRequest;
  const view = reportView;
  if (!view) return;
  const index = snapshot?.projection?.tasks?.findIndex((task) => taskKey(task) === view.key);
  const task = snapshot?.projection?.tasks?.[index];
  if (fetchError || index == null || index < 0) {
    showReport(view, reportDiagnostics('Task report unavailable: the current task selection could not be refreshed. Retrying automatically.', task));
    return;
  }
  const revision = snapshot.revision;
  const iteration = view.runId ? task.earlier_iterations?.entries.findIndex((entry) => entry.run_id === view.runId && entry.report_available) : null;
  if (view.runId && (iteration == null || iteration < 0)) {
    showReport(view, {kind: 'unavailable', text: null, message: 'Earlier iteration report unavailable. Its retained evidence is missing or unverified.'});
    return;
  }
  if (snapshot.stale || view.revision !== revision) showReport(view, {kind: 'unavailable', text: null, message: 'Checking available report…'});
  view.revision = revision;
  view.loading = true;
  view.copy.disabled = view.select.disabled = true;
  view.message.textContent = 'Checking saved report…';
  const controller = new AbortController();
  reportController = controller;
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(`/api/report?revision=${revision}&task=${index}${view.runId ? `&iteration=${iteration}` : ''}`, {cache: 'no-store', signal: controller.signal});
    if (!response.ok) throw new Error('Report unavailable');
    const data = await response.json();
    if (data.revision !== revision || data.task !== index || JSON.stringify(data.identity) !== view.key
        || (data.prior_run_id || null) !== view.runId
        || !['report', 'diagnostics', 'unavailable'].includes(data.kind)
        || typeof data.message !== 'string' || (data.text !== null && typeof data.text !== 'string')) throw new Error('Unverified report');
    if (reportView === view && request === reportRequest) showReport(view, data);
  } catch (_) {
    if (reportView === view && request === reportRequest) showReport(view,
      reportDiagnostics('Task report unavailable: the selected report could not be verified or fetched. Retrying automatically.', task));
  } finally {
    clearTimeout(timeout);
  }
}
function renderDetail(task) {
  const detail = byId('task-detail');
  const sameTask = task && detail.dataset.taskKey === taskKey(task);
  if (!sameTask) selectedIteration = null;
  const openSections = new Set(sameTask ? [...detail.querySelectorAll('details')].filter((item) => item.open).map((item) => item.dataset.sectionKey) : []);
  detail.replaceChildren();
  detail.dataset.taskKey = task ? taskKey(task) : '';
  detail.append(node('p', '3 · State, contexts & history', 'eyebrow'));
  if (!task) {
    clearReport();
    detail.append(node('h2', 'No task selected'), node('p', 'Choose a task to inspect its current state, agents, and saved history.', 'muted'));
    return;
  }
  detail.append(node('p', `${task.project.linear_project || 'Unverified project'} / ${task.issue}`, 'muted breadcrumb'));
  const heading = node('div', null, 'detail-heading');
  heading.append(node('h2', task.issue), badge(task.state, task.phase));
  detail.append(heading);
  detail.append(node('h3', 'Current iteration'));
  const activity = node('section', null, 'activity');
  const phase = node('div');
  phase.append(node('p', 'Current phase', 'eyebrow'), node('h3', label(task.phase)));
  activity.append(phase);
  const progress = node('div', null, 'progress');
  if (task.loop) {
    progress.append(node('p', `${task.loop.passes} passes · ${task.loop.reviews} reviews`));
    if (task.loop.pause_requested) progress.append(node('p', 'Pause requested'));
  } else progress.append(node('p', 'No loop progress recorded'));
  activity.append(progress);
  detail.append(activity);
  const hint = node('div', null, `hint${task.guidance.human_action_required ? ' attention' : ''}`);
  hint.append(node('h3', task.guidance.human_action_required ? 'Needs your attention' : 'Current guidance'), node('p', task.guidance.message));
  if (task.guidance.command) hint.append(node('code', task.guidance.command));
  if (task.guidance.options.length) {
    const options = disclosure(hint, 'Conditional guidance', 'guidance');
    for (const option of task.guidance.options) options.append(node('p', option.message), node('code', option.command));
  }
  detail.append(hint);
  if (task.uncertainties.length) {
    const warnings = section(detail, 'Uncertain evidence');
    warnings.classList.add('warning');
    for (const error of task.uncertainties) warnings.append(node('p', error.message));
  }
  detail.append(reportArea(task));
  const earlier = section(detail, 'Earlier iterations');
  earlier.append(node('p', task.earlier_iterations?.message || 'Earlier iterations unavailable. Discarded runs cannot be reconstructed.', 'muted'));
  for (const entry of task.earlier_iterations?.entries || []) {
    const item = node('div', null, 'iteration');
    item.append(node('h4', `${entry.reviewer_context || 'Reviewer unavailable'} · ${label(entry.state)} (historical)`),
      node('p', entry.passes == null ? 'Counts unavailable' : `${entry.passes} passes · ${entry.reviews} reviews`),
      node('p', `${entry.implementation_context || 'Implementation unavailable'} / ${entry.reviewer_context || 'Reviewer unavailable'}`),
      node('p', `Evidence: ${label(entry.evidence)}`, 'muted'),
      node('p', entry.reason || 'Stop reason/report unavailable from retained evidence.', 'muted'));
    const ids = disclosure(item, 'Iteration identity', `iteration:${entry.run_id || entry.order}`);
    ids.append(node('p', `Run: ${entry.run_id || 'unavailable'} · order: ${entry.order} · evidence: ${entry.evidence}`));
    const inspect = node('button', `Inspect report · ${entry.reviewer_context || entry.order}`);
    inspect.type = 'button';
    inspect.disabled = !entry.report_available;
    inspect.addEventListener('click', () => {
      selectedIteration = entry.run_id;
      renderDetail(task);
      reportView.area.scrollIntoView({block: 'nearest'});
      refreshReport();
    });
    item.append(inspect);
    earlier.append(item);
  }
  const statuses = node('div', null, 'status-grid');
  statusCard(statuses, 'Review', task.review.state, `Acceptance: ${label(task.review.acceptance)}`);
  statusCard(statuses, 'Integration', task.integration?.state || 'not_recorded', task.integration?.context_id ? 'Agent context recorded' : 'No selected integration context');
  statusCard(statuses, 'Publication', task.publication.state, `Current acceptance delivery: ${task.publication.current_acceptance_published ? 'confirmed' : 'not confirmed'}`);
  detail.append(statuses);
  const contexts = section(detail, 'Agent contexts');
  contexts.append(node('p', 'Selected conversations. Saved context states are not proof that an agent is running.', 'muted'));
  const contextGrid = node('div', null, 'context-grid');
  contexts.append(contextGrid);
  for (const [role, context] of Object.entries(task.contexts)) {
    if (!context) continue;
    const card = node('section', null, 'context');
    const head = node('div', null, 'context-header');
    head.append(node('h4', label(role)), node('span', `Saved state: ${label(context.state)}`, 'context-state'));
    card.append(head, node('p', `${label(context.agent)} · ${context.model || 'Model unknown'} · ${label(context.mode)}`, 'muted'));
    disclosure(card, 'Context identity & location', `context:${context.context_id}`).append(fields([
      ['Context', context.context_id], ['Checkout', context.worktree], ['Workspace ID', context.workspace_id],
      ['Pane / terminal', [context.pane_id, context.terminal_id].map(label).join(' / ')],
      ['Session kind', context.session_kind], ['Session ID', context.session_id],
      ['Retired at', context.retired_at ? stamp(context.retired_at) : 'Not recorded'],
    ]));
    contextGrid.append(card);
  }
  if (!Object.values(task.contexts).some(Boolean)) contexts.append(node('p', 'No selected agent contexts recorded.', 'muted'));
  disclosure(detail, 'Workspace & branch', 'workspace').append(fields([
    ['Repository', task.repository.path], ['Checkout', task.workspace.path], ['Branch', task.workspace.branch],
    ['Base branch', task.project.base_branch], ['Herdr registration', label(task.workspace.registration)],
    ['Workspace ID', task.workspace.workspace_id], ['Endpoint', task.workspace.endpoint],
  ]));
  disclosure(detail, 'Detailed workflow evidence', 'evidence').append(fields([
    ['State / phase', `${label(task.state)} / ${label(task.phase)}`], ['Controller ownership', label(task.controller)],
    ['Loop', task.loop ? label(task.loop.state) : 'Not recorded'], ['Run ID', task.loop?.run_id],
    ['Next saved phase', label(task.loop?.next_phase)],
    ['Human action required', String(task.guidance.human_action_required)], ['Reconciliation required', String(task.guidance.reconciliation_required)],
    ['Review context', task.review.context_id], ['Review pass', task.review.pass_id],
    ['Integration checkout', task.integration?.checkout],
    ['Pull request', task.publication.pull_number ? `#${task.publication.pull_number}` : 'Not recorded'],
    ['Merge evidence', label(task.publication.merge)],
  ]));
  const history = disclosure(detail, `Execution history · ${task.history.entries.length} saved entries${task.history.truncated ? ' (truncated)' : ''}`, 'history');
  history.append(node('p', `Up to ${task.history.limit} entries; ordered within each source, not a combined timeline. Missing timestamps remain unknown.${task.history.truncated ? ' Earlier entries were omitted.' : ''}`, 'muted'));
  if (!task.history.entries.length) history.append(node('p', 'No saved history.'));
  const entries = node('ol', null, 'history');
  for (const event of task.history.entries) {
    const row = node('li');
    row.append(node('strong', `${label(event.phase)} · ${label(event.state)}`));
    row.append(node('p', [event.source, event.context_id, event.pass_id].filter(Boolean).join(' · '), 'muted'));
    row.append(node('p', event.timestamp ? stamp(event.timestamp) : 'Timestamp unknown', 'muted'));
    const counts = ['findings_count', 'resolutions_count', 'assessment_state'].filter((key) => event[key] != null).map((key) => `${label(key)}: ${label(event[key])}`);
    if (counts.length) row.append(node('p', counts.join(' · ')));
    entries.append(row);
  }
  history.append(entries);
  for (const area of detail.querySelectorAll('details')) area.open = openSections.has(area.dataset.sectionKey);
}
function render() {
  const focused = document.activeElement?.dataset.focusKey;
  const projection = snapshot?.projection;
  renderOverview(projection);
  const projectGroups = groups(projection);
  let project = projectGroups.find((item) => item.key === selectedProject);
  if (!project) project = projectGroups[0];
  selectedProject = project?.key;
  const projectNav = byId('projects');
  projectNav.replaceChildren();
  for (const group of projectGroups) {
    const button = choice(group === project, `project:${JSON.stringify(group.key)}`, () => {
      selectedProject = group.key;
      selectedTask = null;
      render();
    });
    button.append(node('strong', group.title), node('span', `${group.tasks.length} execution${group.tasks.length === 1 ? '' : 's'}`, 'muted'));
    const attention = group.tasks.filter((index) => projection.tasks[index].guidance.human_action_required).length;
    if (attention) button.append(node('span', `${attention} need attention`, 'attention-label'));
    projectNav.append(button);
  }
  if (!projectGroups.length) projectNav.append(node('p', 'No projects to display.', 'muted'));
  byId('tasks-title').textContent = project?.title || 'Tasks';
  const tasks = (project?.tasks || []).map((index) => projection.tasks[index]);
  const taskList = byId('tasks');
  taskList.replaceChildren();
  let task = tasks.find((item) => taskKey(item) === selectedTask);
  const disappeared = selectedTask !== null && !task;
  if (selectedTask === null) task = tasks[0];
  selectedTask = task ? taskKey(task) : selectedTask;
  byId('task-count').textContent = `${tasks.length} Herdr-registered execution${tasks.length === 1 ? '' : 's'}`;
  for (const item of tasks) {
    const key = taskKey(item);
    const button = choice(item === task, `task:${key}`, () => {
      selectedTask = key;
      render();
      if (window.matchMedia?.('(max-width: 780px)')?.matches) {
        byId('task-detail').focus({preventScroll: true});
        byId('task-detail').scrollIntoView({behavior: 'smooth', block: 'start'});
      }
    });
    const heading = node('div', null, 'choice-heading');
    heading.append(node('strong', item.issue), badge(item.state, item.phase));
    button.append(heading, node('span', `Phase: ${label(item.phase)}`, 'muted'));
    if (item.guidance.human_action_required) button.append(node('span', 'Needs attention', 'attention-label'));
    button.append(node('span', item.repository.path.split('/').filter(Boolean).pop(), 'repository'));
    // Branch disambiguates multiple registered executions of the same issue.
    if (tasks.filter((other) => other.issue === item.issue).length > 1) button.append(node('span', item.workspace.branch || 'Branch unknown', 'muted'));
    taskList.append(button);
  }
  if (!tasks.length) {
    const knownEmpty = snapshot?.discovery?.state === 'available';
    taskList.append(node('p', knownEmpty ? 'No registered AWP task workspaces.' : 'Task discovery is incomplete. An empty view does not confirm cleanup.', 'empty'));
  }
  renderDetail(task);
  if (disappeared) byId('task-detail').prepend(node('p', snapshot.stale ? 'The selected execution is not in this unverified view.' : 'The selected execution is no longer in the registered task overview.', 'hint'));
  if (focused) {
    const replacement = [...document.querySelectorAll('[data-focus-key]')].find((item) => item.dataset.focusKey === focused);
    replacement?.focus({preventScroll: true});
  }
  renderNotices();
  refreshReport();
}

function renderNotices() {
  const notices = byId('notices');
  const messages = [];
  if (fetchError) messages.push('Cannot reach the dashboard server. Displayed data may be stale; reconnecting automatically.');
  if (snapshot?.stale) {
    messages.push(snapshot.verified_at ? 'Stale / unverified: showing the last complete projection. Missing tasks are not treated as cleaned up.' : 'Discovery is incomplete or still starting. Shown evidence is unverified; omitted tasks may still be registered.');
  }
  if (snapshot?.error) messages.push(snapshot.error);
  for (const error of snapshot?.discovery?.uncertainties || []) messages.push(error.message);
  const text = messages.join('\n');
  if (notices.textContent !== text) notices.textContent = text;
  notices.className = messages.length ? 'notice' : '';
}
function updateFreshness() {
  const connection = fetchError ? 'Disconnected · retrying' : live ? 'Live updates connected' : 'Reconnecting · snapshot fallback';
  if (byId('connection').textContent !== connection) byId('connection').textContent = connection;
  if (!snapshot) return;
  const age = snapshot.checked_at ? Math.max(0, Math.floor((Date.now() - Date.parse(snapshot.checked_at)) / 1000)) : null;
  const delayed = age !== null && age > Math.max(30, snapshot.refresh_seconds * 3);
  byId('freshness').textContent = age === null ? 'Waiting for the first local observation…' : `Last checked ${age}s ago${delayed ? ' · Collection delayed — state may be stale' : ''}`;
  byId('freshness').classList.toggle('delayed', delayed);
  byId('observation-times').textContent = `Last checked: ${stamp(snapshot.checked_at)} · Projection changed: ${stamp(snapshot.changed_at)} · Last complete discovery: ${stamp(snapshot.verified_at)}`;
}
async function refresh() {
  pending = true;
  if (fetching) return;
  fetching = true;
  try {
    while (pending) {
      pending = false;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch('/api/snapshot', {cache: 'no-store', signal: controller.signal});
        if (!response.ok) throw new Error('Snapshot unavailable');
        snapshot = await response.json();
        fetchError = false;
        const signature = JSON.stringify([snapshot.projection, snapshot.discovery, snapshot.stale, snapshot.error]);
        if (signature !== rendered) {
          rendered = signature;
          render();
        } else refreshReport();
      } catch (_) {
        fetchError = true;
        refreshReport();
      } finally {
        clearTimeout(timeout);
      }
      renderNotices();
      updateFreshness();
    }
  } finally {
    fetching = false;
  }
}
const events = new EventSource('/api/events');
events.onopen = () => { live = true; refresh(); };
events.addEventListener('invalidate', refresh);
events.onerror = () => { live = false; updateFreshness(); };
refresh();
// Cached HTTP reads keep freshness visible, and provide a bounded fallback when
// a tunnel/browser cannot sustain SSE. They never trigger a Herdr collection.
setInterval(refresh, 15000);
setInterval(updateFreshness, 1000);
window.addEventListener('pagehide', () => events.close());
window.addEventListener('pageshow', (event) => { if (event.persisted) location.reload(); });
