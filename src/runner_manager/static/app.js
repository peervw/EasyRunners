const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
const headers = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf};
const esc = value => String(value ?? '').replace(
  /[&<>'"]/g,
  character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[character]),
);
let poolConfigs = {};
let githubRepositories = [];
let repositoryBound = false;
let refreshing = false;
let dashboardCache = {};
let lastRefreshFailure = '';
let usagePeriod = '24h';
let adoptionData = {repositories: [], replacements: {}};
let githubData = {configured: false, installed: false, repositories: []};
let drawerReturnFocus = null;
let repositoryBrowserReturnFocus = null;
let repositoryBrowserFilter = 'attention';
let repositoryBrowserQuery = '';
let repositoryBrowserPage = 1;
const repositoryBrowserPageSize = 25;

function toast(message, kind = 'error') {
  const item = document.createElement('div');
  item.className = `toast ${kind}`;
  item.textContent = message;
  document.querySelector('#toasts')?.appendChild(item);
  setTimeout(() => item.remove(), 6000);
}

function duration(seconds) {
  seconds = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

function formatBytes(bytes) {
  const value = Math.max(0, Number(bytes) || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  if (value < 1024 ** 4) return `${(value / 1024 ** 3).toFixed(1)} GiB`;
  return `${(value / 1024 ** 4).toFixed(1)} TiB`;
}

function resourceItem(label, value, detail, percent) {
  const bounded = Math.max(0, Math.min(100, Number(percent) || 0));
  return `<div class="resource-item"><div><span>${esc(label)}</span><strong>${esc(value)}</strong></div><small>${esc(detail)}</small><progress value="${bounded}" max="100">${bounded}%</progress></div>`;
}

function renderHost(host) {
  const badge = document.querySelector('#host-badge');
  const target = document.querySelector('#host-resources');
  if (!host?.available) {
    badge.textContent = 'Unavailable';
    badge.className = 'badge offline';
    target.innerHTML = `<p class="empty-message">${esc(host?.error || 'Host resource information is unavailable.')}</p>`;
    return;
  }
  const cpuUsed = Math.max(0, host.cpus_total - host.cpus_available);
  const memoryUsed = Math.max(0, host.memory_total_bytes - host.memory_available_bytes);
  const diskUsedPercent = host.disk_free_percent == null ? 0 : 100 - host.disk_free_percent;
  const pressure = host.pressure || [];
  badge.textContent = pressure.length ? `Pressure: ${pressure.join(', ')}` : 'Healthy';
  badge.className = `badge ${pressure.length ? 'warning' : 'online'}`;
  target.innerHTML = [
    resourceItem('CPU', `${host.cpus_available} of ${host.cpus_total} cores free`, `${host.cpus_reserved} cores reserved`, host.cpus_total ? cpuUsed * 100 / host.cpus_total : 0),
    resourceItem('Memory', `${formatBytes(host.memory_available_bytes)} free`, `${formatBytes(memoryUsed)} of ${formatBytes(host.memory_total_bytes)} reserved`, host.memory_total_bytes ? memoryUsed * 100 / host.memory_total_bytes : 0),
    resourceItem('Disk', `${formatBytes(host.disk_free_bytes)} free`, `${host.disk_free_percent ?? '—'}% available`, diskUsedPercent),
    resourceItem('Runner capacity', `${host.available_runner_capacity} more`, `${host.active_runners} active across all pools`, host.active_runners + host.available_runner_capacity ? host.active_runners * 100 / (host.active_runners + host.available_runner_capacity) : 0),
  ].join('');
}

function adoptionMeta(repository = {}) {
  const count = Number(repository.hosted_jobs) || 0;
  const states = {
    needs_migration: ['Hosted runner', 'warning', '!', `${count || 'Recent'} hosted-runner ${count === 1 ? 'job needs' : 'jobs need'} migration`],
    mixed: ['Mixed', 'warning', '!', `${count || 'Some'} hosted-runner ${count === 1 ? 'job remains' : 'jobs remain'}`],
    using_easy_runners: ['Migrated', 'online', '✓', 'Recent jobs use EasyRunners'],
    no_recent_jobs: ['Unverified', '', '○', 'No recent workflow jobs were found'],
    error: ['Scan failed', 'error', '×', repository.error || 'The repository scan failed'],
    pending: ['Not scanned', '', '○', 'Run a scan to check recent workflow jobs'],
  };
  return states[repository.status] || states.pending;
}

function repositoryRecords() {
  const byRepository = Object.fromEntries(
    (adoptionData.repositories || []).map(repository => [repository.repository, repository]),
  );
  return [...(githubData.repositories || [])].map(name =>
    byRepository[name] || {repository: name, status: 'pending', examples: []});
}

function repositoryGroup(repository) {
  if (['needs_migration', 'mixed', 'error'].includes(repository.status)) return 'attention';
  if (repository.status === 'using_easy_runners') return 'migrated';
  return 'unverified';
}

function orderedRepositories(repositories) {
  const priority = {attention: 0, unverified: 1, migrated: 2};
  return [...repositories].sort((left, right) =>
    priority[repositoryGroup(left)] - priority[repositoryGroup(right)]
    || left.repository.localeCompare(right.repository));
}

function repositoryRow(repository) {
  const [label, className, icon, description] = adoptionMeta(repository);
  return `<button class="repository-status-row" type="button" data-repository="${esc(repository.repository)}" title="${esc(description)}. Click for details.">
    <span class="repository-status-mark ${esc(className)}" aria-hidden="true">${esc(icon)}</span>
    <span class="repository-status-copy"><strong>${esc(repository.repository)}</strong><small>${esc(description)}</small></span>
    <span class="badge ${esc(className)}">${esc(label)}</span>
    <svg class="repository-chevron" aria-hidden="true" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg>
  </button>`;
}

function repositoryCounts(records) {
  return records.reduce((counts, repository) => {
    counts[repositoryGroup(repository)] += 1;
    return counts;
  }, {attention: 0, migrated: 0, unverified: 0});
}

function repositoryScanSummary() {
  const total = Number(adoptionData.repository_count_total ?? githubData.repositories?.length) || 0;
  const scanned = Number(adoptionData.repository_count_scanned) || 0;
  const scan = adoptionData.scan || {};
  if (scan.scanning) {
    const coverage = scanned < total ? `${scanned} of ${total} checked` : `All ${total} checked`;
    return `${coverage} · scanning ${scan.completed || 0}/${scan.total || 0}`;
  }
  if (scan.error) return `${scanned} of ${total} scanned · scan paused`;
  if (!adoptionData.scanned_at) return 'Waiting for the first scan';
  return scanned < total ? `${scanned} of ${total} scanned` : `${total} repositories checked`;
}

function repositorySummaryFilters(counts) {
  return `<div class="repository-summary" aria-label="Repository migration summary">
    <button class="repository-summary-filter" type="button" data-repository-filter="attention"><strong>${counts.attention}</strong><span>Need attention</span></button>
    <button class="repository-summary-filter" type="button" data-repository-filter="migrated"><strong>${counts.migrated}</strong><span>Migrated</span></button>
    <button class="repository-summary-filter" type="button" data-repository-filter="unverified"><strong>${counts.unverified}</strong><span>Unverified</span></button>
  </div>`;
}

function renderRepositoryBrowser() {
  const browser = document.querySelector('#repository-browser');
  if (!browser) return;
  const allRecords = orderedRepositories(repositoryRecords());
  const counts = repositoryCounts(allRecords);
  const query = repositoryBrowserQuery.trim().toLowerCase();
  const filtered = allRecords.filter(repository =>
    (repositoryBrowserFilter === 'all' || repositoryGroup(repository) === repositoryBrowserFilter)
    && (!query || repository.repository.toLowerCase().includes(query)));
  const pageCount = Math.max(1, Math.ceil(filtered.length / repositoryBrowserPageSize));
  repositoryBrowserPage = Math.min(repositoryBrowserPage, pageCount);
  const start = (repositoryBrowserPage - 1) * repositoryBrowserPageSize;
  const page = filtered.slice(start, start + repositoryBrowserPageSize);
  document.querySelector('#repository-browser-count').textContent = `${allRecords.length} repositories`;
  const progress = document.querySelector('#repository-browser-progress');
  progress.textContent = repositoryScanSummary();
  progress.title = adoptionData.scan?.error || '';
  document.querySelector('#repository-browser-filter').value = repositoryBrowserFilter;
  document.querySelector('#repository-browser-list').innerHTML = page.length
    ? page.map(repositoryRow).join('')
    : '<p class="empty-message">No repositories match this filter.</p>';
  document.querySelector('#repository-page-summary').textContent = filtered.length
    ? `${start + 1}–${Math.min(start + repositoryBrowserPageSize, filtered.length)} of ${filtered.length}`
    : '0 repositories';
  document.querySelector('#repository-page-previous').disabled = repositoryBrowserPage <= 1;
  document.querySelector('#repository-page-next').disabled = repositoryBrowserPage >= pageCount;
  document.querySelector('#repository-browser-summary').innerHTML = repositorySummaryFilters(counts);
  const manage = document.querySelector('#repository-browser-manage');
  manage.hidden = !githubData.configure_url;
  if (githubData.configure_url) manage.href = githubData.configure_url;
  browser.querySelectorAll('.refresh-adoption').forEach(button => {
    button.disabled = Boolean(adoptionData.scan?.scanning);
    button.textContent = adoptionData.scan?.scanning ? 'Scanning…' : 'Scan now';
  });
  bindRepositoryControls(browser);
}

function renderRepositoryAccess(github, adoption) {
  githubData = github || githubData;
  adoptionData = adoption || {repositories: [], replacements: {}};
  const target = document.querySelector('#repository-access');
  if (!target || !githubData.connection) {
    if (target) target.innerHTML = '';
    return;
  }
  const selection = githubData.connection.repository_selection;
  const accessWarning = selection === 'all'
    ? '<p class="access-warning">This App can access all repositories. Restrict the installation on GitHub if needed.</p>'
    : '';
  const repositoryErrors = [githubData.metadata_error, githubData.repositories_error].filter(Boolean);
  const records = orderedRepositories(repositoryRecords());
  const counts = repositoryCounts(records);
  const attention = records.filter(repository => repositoryGroup(repository) === 'attention');
  const inline = (attention.length ? attention : records).slice(0, attention.length ? 6 : 3);
  const listTitle = attention.length ? 'Needs attention' : 'Repository status';
  const repositoryList = records.length
    ? `<div class="repository-inline-heading"><span>${esc(listTitle)}</span><small>${esc(repositoryScanSummary())}</small></div><div class="repository-status-list">${inline.map(repositoryRow).join('')}</div>`
    : `<p class="empty-message">${githubData.repository_bound
      ? esc(repositoryErrors.length ? repositoryErrors.join(' · ') : 'No repositories are available to this installation.')
      : 'No installation repositories were found.'}</p>`;
  const configure = githubData.configure_url
    ? `<a href="${esc(githubData.configure_url)}" target="_blank" rel="noopener">Manage access on GitHub</a>`
    : '';
  target.innerHTML = `${accessWarning}<section class="repository-access-section" aria-labelledby="repository-access-heading">
    <div class="repository-access-header">
      <div><h3 id="repository-access-heading">Repositories</h3><p>Workflow migration across this installation.</p></div>
      <button class="secondary compact refresh-adoption" type="button" ${adoptionData.scan?.scanning ? 'disabled' : ''}>${adoptionData.scan?.scanning ? 'Scanning…' : 'Scan now'}</button>
    </div>
    ${repositorySummaryFilters(counts)}
    ${repositoryList}
    <div class="repository-access-footer"><button id="view-all-repositories" class="ghost compact" type="button">View all ${records.length} repositories</button>${configure}</div>
  </section>`;
  renderRepositoryBrowser();
}

function updateMigrationReplacement() {
  const pool = document.querySelector('#migration-pool')?.value;
  const replacement = adoptionData.replacements?.[pool];
  const line = replacement?.runs_on || adoptionData.recommended_runs_on || 'No runner pool available';
  document.querySelector('#migration-runs-on').textContent = line;
  document.querySelector('#copy-migration-runs-on').disabled = !replacement && !adoptionData.recommended_runs_on;
  document.querySelector('#migration-pool-note').textContent = replacement
    ? `${replacement.docker_mode === 'socket' ? 'Docker socket access' : 'No Docker socket'} · labels: ${replacement.labels.join(', ')}`
    : '';
}

function openMigrationDrawer(repositoryName, trigger = null) {
  const drawer = document.querySelector('#migration-drawer');
  const repository = repositoryRecords().find(item => item.repository === repositoryName)
    || {repository: repositoryName, status: 'pending', examples: []};
  const [label, className, , description] = adoptionMeta(repository);
  drawerReturnFocus = trigger || document.activeElement;
  drawer.dataset.repository = repositoryName;
  document.querySelector('#migration-title').textContent = repositoryName;
  const status = document.querySelector('#migration-status');
  status.textContent = label;
  status.className = `badge ${className}`;
  document.querySelector('#migration-summary').textContent = description;
  document.querySelector('#migration-checked').textContent = repository.scanned_at
    ? `Last checked ${new Date(repository.scanned_at).toLocaleString()}`
    : 'Not checked yet';
  const examples = repository.examples || [];
  document.querySelector('#migration-examples').innerHTML = examples.length
    ? `<div class="migration-example-heading"><h3>Detected jobs</h3><span>${examples.length} shown</span></div>${examples.map(example => `<article class="migration-example">
      <div><strong>${esc(example.workflow)} · ${esc(example.job)}</strong><small>${esc(example.workflow_path || 'Workflow file')} · ${esc((example.labels || []).join(', '))}</small></div>
      ${example.url ? `<a href="${esc(example.url)}" target="_blank" rel="noopener">Open in GitHub</a>` : ''}
    </article>`).join('')}`
    : '<div class="migration-empty"><strong>No hosted-runner jobs detected</strong><p>Only a bounded sample of recent workflow jobs is checked. Run the workflow, then scan again to verify it.</p></div>';
  const pool = document.querySelector('#migration-pool');
  pool.innerHTML = Object.entries(adoptionData.replacements || {}).map(([name, replacement]) =>
    `<option value="${esc(name)}">${esc(name)} · ${esc(replacement.docker_mode === 'socket' ? 'Docker' : 'no Docker')}</option>`,
  ).join('');
  if (adoptionData.recommended_pool && adoptionData.replacements?.[adoptionData.recommended_pool]) {
    pool.value = adoptionData.recommended_pool;
  }
  updateMigrationReplacement();
  drawer.hidden = false;
  updateModalLock();
  document.querySelector('#close-migration').focus();
}

function closeMigrationDrawer() {
  const drawer = document.querySelector('#migration-drawer');
  if (drawer.hidden) return;
  const repository = drawer.dataset.repository;
  drawer.hidden = true;
  updateModalLock();
  const browser = document.querySelector('#repository-browser');
  const triggerRoot = browser.hidden ? document : browser;
  const currentTrigger = [...triggerRoot.querySelectorAll('.repository-status-row')]
    .find(button => button.dataset.repository === repository);
  const focusTarget = drawerReturnFocus?.isConnected ? drawerReturnFocus : currentTrigger;
  drawerReturnFocus = null;
  window.requestAnimationFrame(() => focusTarget?.focus?.());
}

function updateModalLock() {
  const browserOpen = !document.querySelector('#repository-browser')?.hidden;
  const drawerOpen = !document.querySelector('#migration-drawer')?.hidden;
  document.body.classList.toggle('drawer-open', browserOpen || drawerOpen);
}

function openRepositoryBrowser(filter = 'attention', trigger = null) {
  const browser = document.querySelector('#repository-browser');
  repositoryBrowserReturnFocus = trigger || document.activeElement;
  repositoryBrowserFilter = filter;
  repositoryBrowserPage = 1;
  repositoryBrowserQuery = '';
  document.querySelector('#repository-browser-search').value = '';
  renderRepositoryBrowser();
  browser.hidden = false;
  updateModalLock();
  document.querySelector('#repository-browser-search').focus();
}

function closeRepositoryBrowser() {
  const browser = document.querySelector('#repository-browser');
  if (browser.hidden) return;
  browser.hidden = true;
  updateModalLock();
  const focusTarget = repositoryBrowserReturnFocus?.isConnected
    ? repositoryBrowserReturnFocus
    : document.querySelector('#view-all-repositories');
  repositoryBrowserReturnFocus = null;
  window.requestAnimationFrame(() => focusTarget?.focus?.());
}

function bindRepositoryControls(root = document) {
  root.querySelectorAll('.repository-status-row').forEach(button => {
    button.onclick = () => openMigrationDrawer(button.dataset.repository, button);
  });
  root.querySelectorAll('.repository-summary-filter').forEach(button => {
    button.onclick = () => {
      const browser = document.querySelector('#repository-browser');
      if (browser.hidden) {
        openRepositoryBrowser(button.dataset.repositoryFilter, button);
      } else {
        const filter = button.dataset.repositoryFilter;
        repositoryBrowserFilter = filter;
        repositoryBrowserPage = 1;
        renderRepositoryBrowser();
        window.requestAnimationFrame(() => browser.querySelector(
          `.repository-summary-filter[data-repository-filter="${filter}"]`,
        )?.focus());
      }
    };
  });
  const viewAll = root.querySelector('#view-all-repositories');
  if (viewAll) viewAll.onclick = () => openRepositoryBrowser('all', viewAll);
}

function renderNotifications(notifications) {
  const configured = Boolean(notifications?.configured);
  const badge = document.querySelector('#notification-badge');
  badge.textContent = configured ? 'Configured' : 'Optional';
  badge.className = `badge ${configured ? 'online' : ''}`;
  document.querySelector('#notification-summary').textContent = configured
    ? `Alerts are ${notifications.signed ? 'HMAC-signed' : 'not signed'} and repeated events are throttled for ${duration(notifications.cooldown_seconds)}.`
    : 'No failure webhook is configured. EasyRunners continues to record failures in logs and the dashboard.';
  document.querySelector('#test-notification').disabled = !configured;
}

function renderUsage(usage) {
  const period = usage?.[usagePeriod] || {};
  document.querySelector('#usage-jobs').textContent = period.jobs ?? 0;
  document.querySelector('#usage-minutes').textContent = `${period.runner_minutes ?? 0}m`;
  document.querySelector('#usage-queue').textContent = period.average_queue_seconds == null
    ? '—'
    : duration(period.average_queue_seconds);
  document.querySelector('#usage-failure').textContent = `${period.failure_rate ?? 0}%`;
}

function renderSetupChecklist(github, readiness, history) {
  const panel = document.querySelector('#setup-checklist');
  const passwordReady = panel.dataset.passwordReady === 'true';
  const webhookEnabled = Boolean(github.connection?.webhook_enabled);
  const steps = [
    ['Administrator password', passwordReady],
    ['GitHub App connected', Boolean(github.installed)],
    ['Webhook received', Boolean(github.installed && (!webhookEnabled || readiness.checks?.webhook?.ok))],
    ['First job completed', history.length > 0],
  ];
  panel.hidden = steps.every(([, done]) => done);
  document.querySelector('#setup-checklist-items').innerHTML = steps.map(([label, done]) =>
    `<div class="checklist-item ${done ? 'done' : ''}"><span class="checklist-mark" aria-hidden="true">${done ? '✓' : ''}</span><span>${esc(label)}</span></div>`,
  ).join('');
}

function renderVersions(versions) {
  const manager = versions.manager || 'unknown';
  const runner = versions.runner || 'unknown';
  document.querySelector('#version').textContent = `EasyRunners ${manager} · Runner ${runner}`;
  document.querySelector('#manager-version').textContent = versions.manager_update_available
    ? `${manager} → ${versions.latest_manager}`
    : manager;
  document.querySelector('#runner-version').textContent = versions.runner_update_available
    ? `${runner} → ${versions.latest_runner}`
    : runner;
  const available = Boolean(versions.update_available);
  const badge = document.querySelector('#update-badge');
  badge.textContent = available ? 'Update available' : 'Up to date';
  badge.className = `badge ${available ? 'warning' : 'online'}`;
  document.querySelector('#update-summary').textContent = available
    ? `Release ${versions.latest_manager_release?.tag || versions.latest_manager || versions.latest_runner} is available.`
    : `Checked ${versions.checked_at ? new Date(versions.checked_at).toLocaleString() : 'just now'}; no newer release was found.`;
  document.querySelector('#update-command').textContent = versions.source_update_command
    || 'git pull --ff-only && docker compose up -d --build';
  document.querySelector('#update-links').innerHTML = [
    (versions.latest_manager_release?.url || versions.manager_release_url) ? `<a href="${esc(versions.latest_manager_release?.url || versions.manager_release_url)}" target="_blank" rel="noopener">EasyRunners releases</a>` : '',
    versions.runner_release_url ? `<a href="${esc(versions.runner_release_url)}" target="_blank" rel="noopener">Runner releases</a>` : '',
  ].filter(Boolean).join('<span class="muted">·</span>');
}

function poolHealth(name, pool, status, readiness) {
  const active = pool.starting + pool.idle + pool.busy;
  const imageAvailable = readiness.checks?.runner_images?.detail?.[name];
  if (!status.last_reconcile) return ['Checking', '', 'Waiting for the first reconciliation'];
  if (pool.last_error) return ['Error', 'error', pool.last_error];
  if (status.docker !== 'connected') return ['Docker offline', 'error', 'Docker Engine is unavailable'];
  if (imageAvailable === false) return ['Image missing', 'error', 'The configured runner image was not found'];
  if (pool.queued && status.github !== 'connected') return ['GitHub offline', 'error', 'GitHub is unavailable'];
  if (pool.queued && active >= pool.max) return ['At capacity', 'warning', `Maximum ${pool.max} runners`];
  if (pool.starting) return ['Starting', 'warning', 'A runner is registering'];
  if (pool.queued) return ['Scaling', 'warning', 'Capacity is being created'];
  return ['Healthy', 'healthy', 'No current issues'];
}

function orderedLabels(labels) {
  const architectureLabels = new Set(['x64', 'arm64', 'arm']);
  const values = [...new Set((labels || []).map(label => String(label).toLowerCase()))]
    .filter(label => !architectureLabels.has(label));
  const builtins = ['self-hosted', 'linux'];
  return [...builtins.filter(label => values.includes(label)), ...values.filter(label => !builtins.includes(label)).sort()];
}

const viewMeta = {
  overview: ['Dashboard', 'Runner status and capacity'],
  activity: ['Activity', 'Runners and workflow jobs'],
  settings: ['Settings', 'GitHub, pools, and access'],
};

function selectAppView(name, updateHistory = true) {
  if (!viewMeta[name]) name = 'overview';
  document.querySelectorAll('[data-view-panel]').forEach(panel => {
    panel.hidden = panel.dataset.viewPanel !== name;
  });
  document.querySelectorAll('.main-nav [data-view]').forEach(item => {
    const active = item.dataset.view === name;
    item.classList.toggle('active', active);
    if (active) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  document.querySelector('#view-title').textContent = viewMeta[name][0];
  document.querySelector('#view-description').textContent = viewMeta[name][1];
  document.title = `${viewMeta[name][0]} · EasyRunners`;
  if (updateHistory && window.location.hash !== `#${name}`) {
    window.history.pushState({view: name}, '', `#${name}`);
  }
}

function updateRunsOn() {
  const pool = document.querySelector('#quickstart-pool')?.value;
  const labels = orderedLabels(poolConfigs[pool]?.labels || []);
  document.querySelector('#runs-on-line').textContent = `runs-on: [${labels.join(', ')}]`;
}

function repositoryOptions() {
  if (!repositoryBound) return '<option value="">Shared organization runner</option>';
  if (!githubRepositories.length) return '<option value="" selected>No repositories available</option>';
  return githubRepositories.map(repository => `<option value="${esc(repository)}">${esc(repository)}</option>`).join('');
}

async function json(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 401) {
    window.location.assign(`/auth/login?next=${encodeURIComponent(window.location.pathname)}`);
    throw new Error('Your session expired. Sign in to continue.');
  }
  if (!response.ok) {
    const error = await response.json().catch(() => ({detail: response.statusText}));
    const detail = typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail);
    throw new Error(detail || 'Request failed');
  }
  return response.status === 204 ? null : response.json();
}

async function action(work, success) {
  try {
    const result = await work();
    if (success) toast(success, 'success');
    return result;
  } catch (error) {
    toast(error.message || String(error));
    return null;
  }
}

function renderReadiness(readiness) {
  const badge = document.querySelector('#readiness-badge');
  badge.textContent = readiness.ready ? 'Ready' : 'Needs attention';
  badge.className = `badge ${readiness.ready ? 'online' : 'offline'}`;
  document.querySelector('#readiness').innerHTML = Object.entries(readiness.checks).map(([name, check]) => {
    const detail = check.detail && typeof check.detail === 'object'
      ? Object.entries(check.detail).map(([pool, ok]) => `${pool}: ${ok ? 'found' : 'missing'}`).join(', ')
      : (check.detail || 'No data');
    const ok = check.ok || check.optional;
    return `<div class="check-item ${ok ? 'ok' : 'bad'}"><span class="check-dot" aria-hidden="true">${check.ok ? '✓' : (check.optional ? '○' : '✕')}</span><div><strong>${esc(name.replaceAll('_', ' '))}</strong><p class="muted">${esc(detail)}</p></div></div>`;
  }).join('');
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const requests = {
      status: '/api/status', runners: '/api/runners', jobs: '/api/jobs',
      history: '/api/history?limit=50', tokens: '/api/auth/tokens', github: '/api/github',
      readiness: '/api/readiness', versions: '/api/version', diagnostics: '/api/diagnostics',
      usage: '/api/usage', diagnosticSettings: '/api/settings/diagnostics',
      adoption: '/api/repositories/adoption', notifications: '/api/notifications',
    };
    const entries = Object.entries(requests);
    const results = await Promise.allSettled(entries.map(([, url]) => json(url)));
    const failures = [];
    results.forEach((result, index) => {
      const name = entries[index][0];
      if (result.status === 'fulfilled') dashboardCache[name] = result.value;
      else failures.push(`${name}: ${result.reason?.message || result.reason}`);
    });
    const failureMessage = failures.join(' · ');
    if (failureMessage && failureMessage !== lastRefreshFailure) toast(`Some data could not be refreshed: ${failureMessage}`);
    if (!failureMessage && lastRefreshFailure) toast('Connection restored.', 'success');
    lastRefreshFailure = failureMessage;
    const status = dashboardCache.status;
    if (!status) throw new Error('Status has not loaded yet.');
    const runners = dashboardCache.runners || [];
    const jobs = dashboardCache.jobs || [];
    const history = dashboardCache.history || [];
    const tokens = dashboardCache.tokens || [];
    const github = dashboardCache.github || {configured: false, installed: false, repositories: []};
    const readiness = dashboardCache.readiness || {ready: false, checks: {}};
    const versions = dashboardCache.versions || {manager: 'unknown', runner: 'unknown'};
    const diagnostics = dashboardCache.diagnostics || [];
    const usage = dashboardCache.usage || {};
    const adoption = dashboardCache.adoption || {repositories: [], replacements: {}};
    const notifications = dashboardCache.notifications || {configured: false};
    const diagnosticSettings = dashboardCache.diagnosticSettings || {
      capture_enabled: true, cleanup_enabled: true, retention_days: 7,
      file_count: 0, total_size: 0, oldest_at: null,
    };
    const poolEntries = Object.entries(status.pools);
    const busyCount = poolEntries.reduce((total, [, pool]) => total + pool.busy, 0);
    const queuedCount = jobs.filter(job => job.status === 'queued').length;
    document.querySelector('#metric-runners').textContent = runners.length;
    document.querySelector('#metric-busy').textContent = busyCount;
    document.querySelector('#metric-queued').textContent = queuedCount;
    document.querySelector('#metric-pools').textContent = poolEntries.length;
    const headerHealth = document.querySelector('.sidebar-footer');
    const headerHealthText = document.querySelector('#header-health');
    const healthy = readiness.ready && status.docker === 'connected';
    headerHealth.className = `sidebar-footer ${healthy ? 'healthy' : 'unhealthy'}`;
    headerHealthText.textContent = healthy ? 'Ready' : 'Needs attention';
    const badge = document.querySelector('#github-badge');
    const githubState = github.installed ? status.github : (github.configured ? 'installation pending' : 'not configured');
    badge.textContent = githubState;
    badge.className = `badge ${status.github === 'connected' ? 'online' : 'offline'}`;
    const repositoryCount = github.repositories?.length ?? github.connection?.repositories_count ?? 0;
    const mode = github.repository_bound ? 'Repository runners' : 'Organization runners';
    document.querySelector('#connection-details').innerHTML = github.connection
      ? `<div class="connection-identity"><span class="provider-mark" aria-hidden="true">GH</span><div><strong>${esc(github.connection.owner)}</strong><small>${esc(mode)} · ${esc(github.connection.app_slug || 'manual app')} · ${github.connection.webhook_enabled ? 'Webhooks enabled' : 'Polling only'} · ${repositoryCount} ${repositoryCount === 1 ? 'repository' : 'repositories'}${github.rate_limit?.remaining == null ? '' : ` · ${github.rate_limit.remaining} API requests left`}</small></div></div>`
      : '<p class="help-text">No GitHub App is connected.</p>';
    githubRepositories = github.repositories || [];
    repositoryBound = Boolean(github.repository_bound);
    document.querySelector('#updated').textContent = status.last_reconcile
      ? `Updated ${new Date(status.last_reconcile).toLocaleTimeString()}`
      : '';
    renderVersions(versions);
    renderReadiness(readiness);
    renderUsage(usage);
    renderHost(status.host);
    renderRepositoryAccess(github, adoption);
    renderNotifications(notifications);
    renderSetupChecklist(github, readiness, history);

    poolConfigs = Object.fromEntries(poolEntries.map(([name, pool]) => [name, pool.config]));
    document.querySelector('#pools').innerHTML = poolEntries.map(([name, pool]) => {
      const active = pool.starting + pool.idle + pool.busy;
      const capacity = pool.max ? Math.min(100, Math.round((active / pool.max) * 100)) : 0;
      const labels = orderedLabels(pool.labels);
      const [healthLabel, healthClass, healthDetail] = poolHealth(name, pool, status, readiness);
      return `
      <article class="pool-card">
        <div class="pool-heading">
          <div class="pool-title"><span class="pool-avatar">${esc(name.slice(0, 2))}</span><div><h3>${esc(name)}</h3><small>${pool.config.docker_mode === 'socket' ? 'Docker socket' : 'No Docker access'} · min ${pool.min}, max ${pool.max}${pool.available_capacity == null ? '' : ` · ${pool.available_capacity} host slots free`}</small></div></div>
          <div class="pool-heading-actions"><span class="badge ${healthClass}" title="${esc(healthDetail)}">${esc(healthLabel)}</span><div class="pool-card-actions"><button class="ghost compact edit-pool" data-pool="${esc(name)}" title="Edit pool">Edit</button><button class="ghost compact delete-pool" data-pool="${esc(name)}" title="Delete pool">Delete</button></div></div>
        </div>
        <div class="pool-stats">
          <div class="pool-stat"><strong>${pool.queued}</strong><span>Queued</span></div>
          <div class="pool-stat"><strong>${pool.starting}</strong><span>Starting</span></div>
          <div class="pool-stat"><strong>${pool.idle}</strong><span>Idle</span></div>
          <div class="pool-stat"><strong>${pool.busy}</strong><span>Busy</span></div>
        </div>
        <progress class="capacity-track" value="${active}" max="${pool.max || 1}" title="${active} of ${pool.max} runners active">${capacity}%</progress>
        <div class="labels">${labels.map(label => `<span class="label">${esc(label)}</span>`).join('')}</div>
        ${pool.manual_floors?.length ? `<p class="manual-floor">${pool.manual_floors.map(floor => `${esc(floor.repository || 'organization')}: ${floor.desired} pre-warmed`).join(' · ')}</p>` : ''}
        <details class="pool-controls">
          <summary>Pre-warm</summary>
          <form class="scale-form" data-pool="${esc(name)}">
            ${repositoryBound ? `<label>Repository<select name="repository" required>${repositoryOptions()}</select></label>` : ''}
            <label>Runners<input name="desired" type="number" min="0" max="${pool.max}" value="0"></label>
            <label>TTL (sec)<input name="ttl" type="number" min="30" value="600"></label>
            <button ${repositoryBound && !githubRepositories.length ? 'disabled title="No GitHub repositories are available"' : ''}>Apply</button>
          </form>
        </details>
      </article>`;
    }).join('');

    const quickstartPool = document.querySelector('#quickstart-pool');
    const selectedQuickstartPool = quickstartPool.value;
    quickstartPool.innerHTML = Object.keys(status.pools).map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
    if (status.pools[selectedQuickstartPool]) quickstartPool.value = selectedQuickstartPool;
    else if (status.pools[adoption.recommended_pool]) quickstartPool.value = adoption.recommended_pool;
    const testRepository = document.querySelector('#test-repository');
    if (testRepository) {
      const selectedTestRepository = testRepository.value;
      testRepository.innerHTML = repositoryOptions();
      testRepository.classList.toggle('hidden', !repositoryBound);
      testRepository.disabled = repositoryBound && !githubRepositories.length;
      if (githubRepositories.includes(selectedTestRepository)) testRepository.value = selectedTestRepository;
    }
    const testPool = document.querySelector('#test-pool');
    if (testPool) {
      const selectedTestPool = testPool.value;
      testPool.innerHTML = Object.keys(status.pools).map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
      if (status.pools[selectedTestPool]) testPool.value = selectedTestPool;
    }
    const testRunnerButton = document.querySelector('#test-runner-form button');
    if (testRunnerButton) {
      testRunnerButton.disabled = repositoryBound && !githubRepositories.length;
      testRunnerButton.title = testRunnerButton.disabled ? 'No GitHub repositories are available' : '';
    }
    updateRunsOn();

    document.querySelector('#runner-count').textContent = runners.length;
    document.querySelector('#runners').innerHTML = runners.length
      ? runners.map(runner => `<tr><td>${esc(runner.name)}</td><td>${esc(runner.repository || status.target || 'organization')}</td><td>${esc(runner.pool)}</td><td><span class="badge ${esc(runner.state)}">${esc(runner.state)}</span></td><td>${duration(runner.uptime_seconds)}</td><td><code>${esc(runner.container_id.slice(0, 12))}</code></td><td>${orderedLabels(runner.labels).map(label => `<span class="label">${esc(label)}</span>`).join(' ')}</td></tr>`).join('')
      : '<tr><td colspan="7" class="empty-cell">No active runners.</td></tr>';
    document.querySelector('#job-count').textContent = jobs.length;
    document.querySelector('#jobs').innerHTML = jobs.length
      ? jobs.map(job => `<tr><td>${esc(job.name || job.id)}</td><td>${esc(job.repository)}</td><td>${job.pool ? esc(job.pool) : `<span class="unmatched">No pool matches [${job.labels.map(esc).join(', ')}]</span> <button class="secondary compact copy-replacement">Copy replacement</button>`}</td><td><span class="badge ${esc(job.status)}">${esc(job.status)}</span>${job.waiting_reason ? `<small class="job-reason">${esc(job.waiting_reason)}</small>` : ''}</td><td>${esc(job.runner_name || '—')}</td><td>${job.queued_at ? new Date(job.queued_at).toLocaleString() : '—'}</td></tr>`).join('')
      : '<tr><td colspan="6" class="empty-cell">No queued or active jobs.</td></tr>';
    document.querySelector('#history-count').textContent = history.length;
    document.querySelector('#history').innerHTML = history.length
      ? history.map(job => `<tr><td>${esc(job.name || job.id)}</td><td>${esc(job.repository)}</td><td>${esc(job.pool || '—')}</td><td><span class="badge ${esc(job.conclusion || '')}">${esc(job.conclusion || '—')}</span></td><td>${job.completed_at ? new Date(job.completed_at).toLocaleString() : '—'}</td></tr>`).join('')
      : '<tr><td colspan="5" class="empty-cell">No job history.</td></tr>';
    document.querySelector('#tokens').innerHTML = tokens.length
      ? tokens.map(token => `<li><span>${esc(token.name)}<small>${esc(token.scope)} · ${token.expires_at ? `expires ${new Date(token.expires_at).toLocaleDateString()}` : 'no expiry'} · ${esc(token.id)}</small></span><button class="ghost compact revoke-token" data-id="${esc(token.id)}">Revoke</button></li>`).join('')
      : '<li class="muted">No API tokens.</li>';
    document.querySelector('#diagnostic-count').textContent = diagnostics.length;
    document.querySelector('#diagnostics').innerHTML = diagnostics.length
      ? diagnostics.map(item => `<li><a href="/api/diagnostics/${encodeURIComponent(item.name)}">${esc(item.name)}</a><small class="muted">${Math.ceil(item.size / 1024)} KiB · ${new Date(item.modified_at).toLocaleString()}</small></li>`).join('')
      : '<li class="muted">No saved diagnostics.</li>';
    document.querySelector('#diagnostic-summary').textContent = diagnosticSettings.file_count
      ? `${diagnosticSettings.file_count} files · ${formatBytes(diagnosticSettings.total_size)} · oldest ${new Date(diagnosticSettings.oldest_at).toLocaleDateString()}`
      : 'No disk space used.';
    const diagnosticForm = document.querySelector('#diagnostic-settings-form');
    if (!diagnosticForm.dataset.dirty) {
      diagnosticForm.capture_enabled.checked = diagnosticSettings.capture_enabled;
      diagnosticForm.cleanup_enabled.checked = diagnosticSettings.cleanup_enabled;
      diagnosticForm.retention_days.value = String(diagnosticSettings.retention_days);
    }
    diagnosticForm.retention_days.disabled = !diagnosticForm.cleanup_enabled.checked;
    document.querySelector('#clear-diagnostics').disabled = diagnosticSettings.file_count === 0;
    bindDynamic();
  } catch (error) {
    toast(`Dashboard refresh failed: ${error.message || error}`);
  } finally {
    refreshing = false;
  }
}

function fillPool(name = 'default', config = null) {
  selectAppView('settings');
  const form = document.querySelector('#pool-form');
  const value = config || {labels: ['self-hosted', 'linux', name], min: 0, max: 5, cpu: 4, memory: '8g', docker_mode: 'socket'};
  form.pool_name.value = name;
  form.labels.value = value.labels.join(',');
  form.min.value = value.min;
  form.max.value = value.max;
  form.cpu.value = value.cpu;
  form.memory.value = value.memory;
  form.image.value = value.image || '';
  form.docker_mode.value = value.docker_mode;
  form.scrollIntoView({behavior: 'smooth', block: 'center'});
}

function bindDynamic() {
  document.querySelectorAll('.scale-form').forEach(form => {
    form.onsubmit = async event => {
      event.preventDefault();
      const result = await action(() => json(`/api/pools/${encodeURIComponent(form.dataset.pool)}/scale`, {
        method: 'POST', headers, body: JSON.stringify({
          desired: Number(form.desired.value),
          ttl_seconds: Number(form.ttl.value),
          repository: form.repository?.value || null,
        }),
      }), 'Pre-warm request applied.');
      if (result) refresh();
    };
  });
  document.querySelectorAll('.revoke-token').forEach(button => {
    button.onclick = async () => {
      const result = await action(() => json(`/api/auth/tokens/${encodeURIComponent(button.dataset.id)}`, {method: 'DELETE', headers}), 'Token revoked.');
      if (result !== null) refresh(); else refresh();
    };
  });
  document.querySelectorAll('.edit-pool').forEach(button => {
    button.onclick = () => fillPool(button.dataset.pool, poolConfigs[button.dataset.pool]);
  });
  document.querySelectorAll('.delete-pool').forEach(button => {
    button.onclick = async () => {
      if (!window.confirm(`Delete runner pool “${button.dataset.pool}”?`)) return;
      await action(() => json(`/api/pools/${encodeURIComponent(button.dataset.pool)}`, {method: 'DELETE', headers}), 'Pool deleted.');
      refresh();
    };
  });
  document.querySelectorAll('.copy-replacement').forEach(button => {
    button.onclick = async () => {
      const content = document.querySelector('#runs-on-line').textContent;
      await action(() => navigator.clipboard.writeText(content), 'Replacement runs-on line copied.');
    };
  });
  bindRepositoryControls(document);
  document.querySelectorAll('.refresh-adoption').forEach(button => {
    button.onclick = async () => {
      button.disabled = true;
      const result = await action(() => json('/api/repositories/adoption?refresh=true'));
      if (result) {
        dashboardCache.adoption = result;
        renderRepositoryAccess(githubData, result);
        bindDynamic();
        toast(result.scan?.scanning ? 'Repository scan started.' : 'Repository scan refreshed.', 'success');
      } else {
        button.disabled = false;
      }
    };
  });
}

document.querySelector('#reconcile')?.addEventListener('click', async () => {
  await action(() => json('/api/reconcile', {method: 'POST', headers}), 'Reconciliation completed.');
  refresh();
});
document.querySelector('#test-runner-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const params = new URLSearchParams({pool: event.target.pool.value});
  if (event.target.repository?.value) params.set('repository', event.target.repository.value);
  await action(() => json(`/api/readiness/test-runner?${params}`, {method: 'POST', headers}), 'Test runner requested. Watch the runner list.');
  refresh();
});
document.querySelector('#disconnect-github')?.addEventListener('click', async () => {
  if (!window.confirm('Disconnect this GitHub App from EasyRunners? The App remains installed on GitHub.')) return;
  await action(() => json('/api/github/disconnect', {method: 'POST', headers}), 'GitHub disconnected locally.');
  window.location.reload();
});
document.querySelector('#token-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const expires = event.target.expires_in_days.value;
  const result = await action(() => json('/api/auth/tokens', {method: 'POST', headers, body: JSON.stringify({
    name: event.target.name.value,
    scope: event.target.scope.value,
    expires_in_days: expires ? Number(expires) : null,
  })}));
  if (!result) return;
  document.querySelector('#new-token').innerHTML = `Copy this token now; it will not be shown again: <code>${esc(result.token)}</code>`;
  event.target.reset();
  refresh();
});
document.querySelector('#diagnostic-settings-form')?.addEventListener('input', event => {
  event.currentTarget.dataset.dirty = 'true';
  event.currentTarget.retention_days.disabled = !event.currentTarget.cleanup_enabled.checked;
});
document.querySelector('#diagnostic-settings-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const result = await action(() => json('/api/settings/diagnostics', {
    method: 'PUT', headers, body: JSON.stringify({
      capture_enabled: form.capture_enabled.checked,
      cleanup_enabled: form.cleanup_enabled.checked,
      retention_days: Number(form.retention_days.value),
    }),
  }), 'Diagnostic settings saved.');
  if (result) {
    delete form.dataset.dirty;
    dashboardCache.diagnosticSettings = result;
    refresh();
  }
});
document.querySelector('#clear-diagnostics')?.addEventListener('click', async () => {
  if (!window.confirm('Delete all saved runner diagnostics?')) return;
  const result = await action(() => json('/api/diagnostics', {method: 'DELETE', headers}), 'Diagnostics deleted.');
  if (result) refresh();
});
document.querySelector('#github-setup')?.addEventListener('submit', async event => {
  event.preventDefault();
  const progress = document.querySelector('#setup-progress');
  progress.textContent = 'Checking GitHub target…';
  const data = new FormData(event.target);
  const result = await action(() => json('/api/github/setup/manifest', {
    method: 'POST', headers, body: JSON.stringify({
      target_url: data.get('target_url'),
      organization_wide: data.get('organization_wide') === 'on',
      webhook_enabled: data.get('webhook_enabled') === 'on',
    }),
  }));
  if (!result) { progress.textContent = ''; return; }
  progress.textContent = 'Opening GitHub…';
  const form = document.createElement('form');
  form.method = 'post'; form.action = result.action;
  const manifest = document.createElement('input');
  manifest.type = 'hidden'; manifest.name = 'manifest'; manifest.value = JSON.stringify(result.manifest);
  form.appendChild(manifest); document.body.appendChild(form); form.submit();
});
document.querySelector('#pool-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target;
  const name = form.pool_name.value;
  const current = poolConfigs[name] || {};
  const body = {...current,
    labels: form.labels.value.split(',').map(value => value.trim()).filter(Boolean),
    min: Number(form.min.value), max: Number(form.max.value), cpu: Number(form.cpu.value),
    memory: form.memory.value, image: form.image.value || null,
    docker_mode: form.docker_mode.value,
  };
  const result = await action(() => json(`/api/pools/${encodeURIComponent(name)}`, {method: 'PUT', headers, body: JSON.stringify(body)}), 'Pool saved.');
  if (result) refresh();
});
document.querySelector('#new-pool')?.addEventListener('click', () => fillPool('new-pool'));
document.querySelector('#rust-pool')?.addEventListener('click', () => fillPool('rust', {
  labels: ['self-hosted', 'linux', 'rust'], min: 0, max: 5,
  cpu: 4, memory: '8g', image: null, docker_mode: 'none',
}));
document.querySelector('#export-pools')?.addEventListener('click', async () => {
  await action(async () => {
    const response = await fetch('/api/pools/config.yaml');
    if (!response.ok) throw new Error('Could not export pools');
    document.querySelector('#pool-yaml').value = await response.text();
  }, 'Pool YAML exported below.');
});
document.querySelector('#import-pools')?.addEventListener('click', async () => {
  const source = document.querySelector('#pool-yaml').value;
  const result = await action(() => json('/api/pools/config', {method: 'PUT', headers, body: JSON.stringify({yaml: source})}), 'Pool YAML imported.');
  if (result) refresh();
});
document.querySelector('#quickstart-pool')?.addEventListener('change', updateRunsOn);
document.querySelector('#copy-runs-on')?.addEventListener('click', async () => {
  const content = document.querySelector('#runs-on-line').textContent;
  await action(() => navigator.clipboard.writeText(content), 'runs-on line copied.');
});
document.querySelector('#migration-pool')?.addEventListener('change', updateMigrationReplacement);
document.querySelector('#copy-migration-runs-on')?.addEventListener('click', async () => {
  const line = document.querySelector('#migration-runs-on').textContent;
  if (line !== 'No runner pool available') {
    await action(() => navigator.clipboard.writeText(line), 'Replacement runs-on line copied.');
  }
});
document.querySelectorAll('[data-close-migration]').forEach(button => {
  button.addEventListener('click', closeMigrationDrawer);
});
document.querySelectorAll('[data-close-repository-browser]').forEach(button => {
  button.addEventListener('click', closeRepositoryBrowser);
});
document.querySelector('#repository-browser-search')?.addEventListener('input', event => {
  repositoryBrowserQuery = event.target.value;
  repositoryBrowserPage = 1;
  renderRepositoryBrowser();
});
document.querySelector('#repository-browser-filter')?.addEventListener('change', event => {
  repositoryBrowserFilter = event.target.value;
  repositoryBrowserPage = 1;
  renderRepositoryBrowser();
});
document.querySelector('#repository-page-previous')?.addEventListener('click', () => {
  repositoryBrowserPage = Math.max(1, repositoryBrowserPage - 1);
  renderRepositoryBrowser();
});
document.querySelector('#repository-page-next')?.addEventListener('click', () => {
  repositoryBrowserPage += 1;
  renderRepositoryBrowser();
});
document.addEventListener('keydown', event => {
  const drawer = document.querySelector('#migration-drawer');
  const browser = document.querySelector('#repository-browser');
  if (drawer.hidden && browser.hidden) return;
  if (event.key === 'Escape') {
    if (!drawer.hidden) closeMigrationDrawer();
    else closeRepositoryBrowser();
    return;
  }
  if (event.key !== 'Tab') return;
  const activeModal = !drawer.hidden
    ? drawer.querySelector('[role="dialog"]')
    : browser.querySelector('[role="dialog"]');
  const focusable = [...activeModal.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled)')]
    .filter(element => element.getClientRects().length);
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
});
document.querySelector('#test-notification')?.addEventListener('click', async () => {
  await action(() => json('/api/notifications/test', {method: 'POST', headers}), 'Test notification delivered.');
});
document.querySelector('#copy-update-command')?.addEventListener('click', async () => {
  const content = document.querySelector('#update-command').textContent;
  await action(() => navigator.clipboard.writeText(content), 'Update command copied.');
});
document.querySelector('#setup-settings')?.addEventListener('click', () => selectAppView('settings'));

const themeSelect = document.querySelector('#theme-select');
if (themeSelect && window.EasyRunnersTheme) {
  themeSelect.value = window.EasyRunnersTheme.preference();
  themeSelect.addEventListener('change', () => window.EasyRunnersTheme.set(themeSelect.value));
}

document.querySelectorAll('[data-usage-period]').forEach(button => {
  button.addEventListener('click', () => {
    usagePeriod = button.dataset.usagePeriod;
    document.querySelectorAll('[data-usage-period]').forEach(item => {
      item.classList.toggle('active', item === button);
    });
    renderUsage(dashboardCache.usage || {});
  });
});

function selectActivityTab(name) {
  document.querySelectorAll('[data-activity-tab]').forEach(tab => {
    const active = tab.dataset.activityTab === name;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('[data-activity-panel]').forEach(panel => {
    panel.hidden = panel.dataset.activityPanel !== name;
  });
}

document.querySelectorAll('[data-activity-tab]').forEach(tab => {
  tab.addEventListener('click', () => selectActivityTab(tab.dataset.activityTab));
  tab.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    const tabs = [...document.querySelectorAll('[data-activity-tab]')];
    const direction = event.key === 'ArrowRight' ? 1 : -1;
    const target = tabs[(tabs.indexOf(tab) + direction + tabs.length) % tabs.length];
    target.focus();
    selectActivityTab(target.dataset.activityTab);
  });
});

document.querySelectorAll('[data-view]').forEach(item => {
  item.addEventListener('click', event => {
    event.preventDefault();
    selectAppView(item.dataset.view);
  });
});
window.addEventListener('popstate', () => selectAppView(window.location.hash.slice(1), false));

selectAppView(window.location.hash.slice(1) || 'overview', false);
refresh();
setInterval(refresh, 5000);
