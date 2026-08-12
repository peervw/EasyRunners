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
  return `${(value / 1024 ** 2).toFixed(1)} MiB`;
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
    ? 'Review the available versions before rebuilding.'
    : 'No newer release was found.';
  document.querySelector('#update-command').textContent = versions.source_update_command
    || 'git pull --ff-only && docker compose up -d --build';
  document.querySelector('#update-links').innerHTML = [
    versions.manager_release_url ? `<a href="${esc(versions.manager_release_url)}" target="_blank" rel="noopener">EasyRunners releases</a>` : '',
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
    const selection = github.connection?.repository_selection;
    const accessWarning = selection === 'all'
      ? '<p class="access-warning">This App can access all repositories. Restrict the installation on GitHub if needed.</p>'
      : '';
    const repositoryErrors = [github.metadata_error, github.repositories_error].filter(Boolean);
    const repositoryList = github.repositories?.length
      ? `<div class="repository-list">${github.repositories.map(repository => `<span class="label">${esc(repository)}</span>`).join('')}</div>`
      : (github.repository_bound
        ? `<p class="access-warning">Repositories could not be loaded. ${repositoryErrors.length ? esc(repositoryErrors.join(' · ')) : 'Check the App installation on GitHub.'}</p>`
        : '');
    const configure = github.configure_url
      ? `<a href="${esc(github.configure_url)}" target="_blank" rel="noopener">Manage repository access on GitHub</a>`
      : '';
    document.querySelector('#repository-access').innerHTML = github.connection
      ? `${accessWarning}${repositoryList}${configure}`
      : '';
    githubRepositories = github.repositories || [];
    repositoryBound = Boolean(github.repository_bound);
    document.querySelector('#updated').textContent = status.last_reconcile
      ? `Updated ${new Date(status.last_reconcile).toLocaleTimeString()}`
      : '';
    renderVersions(versions);
    renderReadiness(readiness);
    renderUsage(usage);
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
          <div class="pool-title"><span class="pool-avatar">${esc(name.slice(0, 2))}</span><div><h3>${esc(name)}</h3><small>${pool.config.docker_mode === 'socket' ? 'Docker socket' : 'No Docker access'} · min ${pool.min}, max ${pool.max}</small></div></div>
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
