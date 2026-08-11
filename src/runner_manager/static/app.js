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

function orderedLabels(labels) {
  const values = [...new Set((labels || []).map(label => String(label).toLowerCase()))];
  const builtins = ['self-hosted', 'linux', 'x64', 'arm64', 'arm'];
  return [...builtins.filter(label => values.includes(label)), ...values.filter(label => !builtins.includes(label)).sort()];
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
    const detail = typeof check.detail === 'object'
      ? Object.entries(check.detail).map(([pool, ok]) => `${pool}: ${ok ? 'found' : 'missing'}`).join(', ')
      : (check.detail || 'Not observed yet');
    const ok = check.ok || check.optional;
    return `<div class="check-item ${ok ? 'ok' : 'bad'}"><strong>${check.ok ? '✓' : (check.optional ? '○' : '✕')} ${esc(name.replaceAll('_', ' '))}</strong><p class="muted">${esc(detail)}</p></div>`;
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
    if (failureMessage && failureMessage !== lastRefreshFailure) toast(`Some dashboard data is stale: ${failureMessage}`);
    if (!failureMessage && lastRefreshFailure) toast('Dashboard data is live again.', 'success');
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
    const badge = document.querySelector('#github-badge');
    const githubState = github.installed ? status.github : (github.configured ? 'installation pending' : 'not configured');
    badge.textContent = githubState;
    badge.className = `badge ${status.github === 'connected' ? 'online' : 'offline'}`;
    const repositoryCount = github.repositories?.length ?? github.connection?.repositories_count ?? 0;
    const mode = github.repository_bound ? 'repository-isolated runners' : 'shared organization runners';
    document.querySelector('#connection-details').innerHTML = github.connection
      ? `<strong>${esc(github.connection.owner)} · ${esc(mode)}</strong>`
        + `<p class="muted">App: ${esc(github.connection.app_slug || 'manual')} · Installation: ${esc(github.connection.installation_id || 'pending')} · ${github.connection.webhook_enabled ? 'webhook + polling' : 'polling only'} · ${repositoryCount} ${repositoryCount === 1 ? 'repository' : 'repositories'}${github.rate_limit?.remaining == null ? '' : ` · GitHub API ${github.rate_limit.remaining} remaining`}</p>`
      : '<p class="muted">Paste your GitHub account or organization URL below. Repository access is selected on GitHub.</p>';
    const selection = github.connection?.repository_selection;
    const accessWarning = selection === 'all'
      ? '<p class="access-warning">GitHub granted this App access to all repositories. EasyRunners can serve matching jobs from any of them. Select only the repositories you trust.</p>'
      : '';
    const repositoryErrors = [github.metadata_error, github.repositories_error].filter(Boolean);
    const repositoryList = github.repositories?.length
      ? `<div class="repository-list">${github.repositories.map(repository => `<span class="label">${esc(repository)}</span>`).join('')}</div>`
      : (github.repository_bound
        ? `<p class="access-warning">No repositories could be loaded. ${repositoryErrors.length ? esc(repositoryErrors.join(' · ')) : 'Check the App repository access on GitHub.'}</p>`
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
    document.querySelector('#version').textContent = versions.update_available
      ? `Runner ${versions.runner} · ${versions.latest_runner} available`
      : `EasyRunners ${versions.manager} · Runner ${versions.runner}`;
    renderReadiness(readiness);

    poolConfigs = Object.fromEntries(Object.entries(status.pools).map(([name, pool]) => [name, pool.config]));
    document.querySelector('#pools').innerHTML = Object.entries(status.pools).map(([name, pool]) => `
      <article class="card">
        <div class="section-heading"><h2>${esc(name)}</h2><span>${pool.min}–${pool.max}</span></div>
        <div class="stats">
          <div class="stat"><b>${pool.queued}</b><span>Queued</span></div>
          <div class="stat"><b>${pool.starting}</b><span>Starting</span></div>
          <div class="stat"><b>${pool.idle}</b><span>Idle</span></div>
          <div class="stat"><b>${pool.busy}</b><span>Busy</span></div>
        </div>
        <div class="labels">${pool.labels.map(label => `<span class="label">${esc(label)}</span>`).join('')}</div>
        ${pool.manual_floors?.length ? `<p class="muted">${pool.manual_floors.map(floor => `${esc(floor.repository || 'organization')}: ${floor.desired} pre-warmed`).join(' · ')}</p>` : ''}
        <form class="scale-form inline-form" data-pool="${esc(name)}">
          ${repositoryBound ? `<select name="repository" aria-label="Pre-warm repository" required>${repositoryOptions()}</select>` : ''}
          <input name="desired" type="number" min="0" max="${pool.max}" value="0" aria-label="Desired pre-warm">
          <input name="ttl" type="number" min="30" value="600" aria-label="TTL seconds">
          <button ${repositoryBound && !githubRepositories.length ? 'disabled title="No GitHub repositories are available"' : ''}>Pre-warm</button>
        </form>
        <div class="actions"><button class="secondary compact edit-pool" data-pool="${esc(name)}">Edit</button><button class="secondary compact delete-pool" data-pool="${esc(name)}">Delete</button></div>
      </article>`).join('');

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
      ? runners.map(runner => `<tr><td>${esc(runner.name)}</td><td>${esc(runner.repository || status.target || 'organization')}</td><td>${esc(runner.pool)}</td><td><span class="badge ${esc(runner.state)}">${esc(runner.state)}</span></td><td>${duration(runner.uptime_seconds)}</td><td><code>${esc(runner.container_id.slice(0, 12))}</code></td><td>${runner.labels.map(label => `<span class="label">${esc(label)}</span>`).join(' ')}</td></tr>`).join('')
      : '<tr><td colspan="7" class="muted">No managed runners.</td></tr>';
    document.querySelector('#job-count').textContent = jobs.length;
    document.querySelector('#jobs').innerHTML = jobs.length
      ? jobs.map(job => `<tr><td>${esc(job.name || job.id)}${job.waiting_reason ? `<small class="job-reason">${esc(job.waiting_reason)}</small>` : ''}</td><td>${esc(job.repository)}</td><td>${job.pool ? esc(job.pool) : `<span class="unmatched">No pool matches [${job.labels.map(esc).join(', ')}]</span> <button class="secondary compact copy-replacement">Copy replacement</button>`}</td><td><span class="badge ${esc(job.status)}">${esc(job.status)}</span></td><td>${esc(job.runner_name || '—')}</td><td>${job.queued_at ? new Date(job.queued_at).toLocaleString() : '—'}</td></tr>`).join('')
      : '<tr><td colspan="6" class="muted">No queued or active jobs.</td></tr>';
    document.querySelector('#history').innerHTML = history.length
      ? history.map(job => `<tr><td>${esc(job.name || job.id)}</td><td>${esc(job.repository)}</td><td>${esc(job.pool || '—')}</td><td>${esc(job.conclusion || '—')}</td><td>${job.completed_at ? new Date(job.completed_at).toLocaleString() : '—'}</td></tr>`).join('')
      : '<tr><td colspan="5" class="muted">No completed jobs observed.</td></tr>';
    document.querySelector('#tokens').innerHTML = tokens.map(token => `<li><span>${esc(token.name)} <small class="muted">${esc(token.scope)} · ${token.expires_at ? `expires ${new Date(token.expires_at).toLocaleDateString()}` : 'no expiry'} · ${esc(token.id)}</small></span><button class="secondary compact revoke-token" data-id="${esc(token.id)}">Revoke</button></li>`).join('');
    document.querySelector('#diagnostics').innerHTML = diagnostics.length
      ? diagnostics.map(item => `<li><a href="/api/diagnostics/${encodeURIComponent(item.name)}">${esc(item.name)}</a><small class="muted">${Math.ceil(item.size / 1024)} KiB · ${new Date(item.modified_at).toLocaleString()}</small></li>`).join('')
      : '<li class="muted">No runner diagnostics have been archived yet.</li>';
    bindDynamic();
  } catch (error) {
    toast(`Dashboard refresh failed: ${error.message || error}`);
  } finally {
    refreshing = false;
  }
}

function fillPool(name = 'default', config = null) {
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

refresh();
setInterval(refresh, 5000);
