// ── Defaults (shown in config panel even before server restart) ───────────────
const CFG_DEFAULTS = {
    workers: 5, max_depth: 3, recrawl_days: 30,
    request_delay: 1.5, per_url_timeout: 100,
    httpx_first: true, playwright_fallback: true,
    playwright_timeout: 45, js_settle_time: 3.0,
    email_enabled: true, email_context_chars: 200,
    person_enabled: true, person_proximity_chars: 300,
};

// ── State ─────────────────────────────────────────────────────────────────────
let selectedIds = new Set();
let currentPage = 1;
let totalPages = 1;
let totalMatching = 0;
let activeJobId = null;
let pollTimer = null;
let impPollTimer = null;
let searchTimer = null;

// ── Init ──────────────────────────────────────────────────────────────────────


// ── Domain label (shows URL-derived name when title is empty) ─────────────────
function domainLabel(d) {
    if (d.title && d.title.trim()) return d.title.trim();
    try {
        let host = new URL(d.main_url || d.contact_url || 'https://unknown.gov.in').hostname.toLowerCase();
        host = host.replace(/\.(gov|nic|res|ac|edu)\.in$/, '');
        return host.replace(/\./g, ' · ').replace(/-/g, ' ');
    } catch {
        return d.main_url || '—';
    }
}

function domainShortUrl(d) {
    const url = d.contact_url || d.main_url || '';
    try {
        return new URL(url).hostname;
    } catch {
        return url.substring(0, 40);
    }
}

// ── Selection Persistence ─────────────────────────────────────────────────────
function saveSelection() {
    sessionStorage.setItem('dashboard_selection', JSON.stringify([...selectedIds]));
}

function restoreSelection() {
    try {
        const saved = JSON.parse(sessionStorage.getItem('dashboard_selection') || '[]');
        selectedIds = new Set(saved);
    } catch { selectedIds = new Set(); }
}

// ── Filter Persistence ────────────────────────────────────────────────────────
function getDashboardFilters() {
    try { return JSON.parse(sessionStorage.getItem('dashboard_filters') || '{}'); } catch { return {}; }
}

function saveDashboardFilters() {
    sessionStorage.setItem('dashboard_filters', JSON.stringify({
        cat:     document.getElementById('cat-select')?.value || '',
        state:   document.getElementById('state-select')?.value || '',
        orgType: document.getElementById('orgtype-select')?.value || '',
        search:  document.getElementById('search-input')?.value || '',
    }));
}

function clearDashboardFilters() {
    sessionStorage.removeItem('dashboard_filters');
    const catSel   = document.getElementById('cat-select');
    const stateSel = document.getElementById('state-select');
    const orgSel   = document.getElementById('orgtype-select');
    const searchEl = document.getElementById('search-input');
    if (catSel)   catSel.value   = '';
    if (stateSel) stateSel.value = '';
    if (orgSel)   orgSel.value   = '';
    if (searchEl) searchEl.value = '';
    currentPage = 1;
    reloadStateOptions('').then(() => reloadOrgTypeOptions('', '')).then(() => loadDomains());
}

// ── Filters ───────────────────────────────────────────────────────────────────
async function loadFilters() {
    try {
        const saved = getDashboardFilters();

        const cats = await apiFetch('/api/categories');
        const catSel = document.getElementById('cat-select');
        cats.forEach(c => {
            const o = document.createElement('option');
            o.value = c.code;
            o.textContent = `${c.title} (${c.count.toLocaleString()})`;
            catSel.appendChild(o);
        });
        if (saved.cat) catSel.value = saved.cat;
        if (saved.search) document.getElementById('search-input').value = saved.search;

        await reloadStateOptions(catSel.value);
        if (saved.state) document.getElementById('state-select').value = saved.state;

        await reloadOrgTypeOptions(catSel.value, document.getElementById('state-select').value);
        if (saved.orgType) document.getElementById('orgtype-select').value = saved.orgType;
    } catch (e) {
        console.error('loadFilters', e);
    }
}

async function reloadStateOptions(cat) {
    const stateSel = document.getElementById('state-select');
    const prev = stateSel.value;
    stateSel.innerHTML = '<option value="">All States</option>';
    try {
        const params = cat ? `?category=${encodeURIComponent(cat)}` : '';
        const states = await apiFetch(`/api/states${params}`);
        states.forEach(s => {
            const o = document.createElement('option');
            o.value = s;
            o.textContent = s;
            if (s === prev) o.selected = true;
            stateSel.appendChild(o);
        });
    } catch (e) {
    }
}

async function reloadOrgTypeOptions(cat, state) {
    const orgSel = document.getElementById('orgtype-select');
    const prev = orgSel.value;
    orgSel.innerHTML = '<option value="">All Types</option>';
    try {
        const p = new URLSearchParams();
        if (cat) p.set('category', cat);
        if (state) p.set('state', state);
        const orgTypes = await apiFetch(`/api/org-types${p.toString() ? '?' + p : ''}`);
        orgTypes.forEach(t => {
            const o = document.createElement('option');
            o.value = t.code;
            o.textContent = `${t.title} (${t.count.toLocaleString()})`;
            if (t.code === prev) o.selected = true;
            orgSel.appendChild(o);
        });
    } catch (e) {
    }
}

async function onCategoryChange() {
    currentPage = 1;
    const cat = document.getElementById('cat-select').value;
    await reloadStateOptions(cat);
    await reloadOrgTypeOptions(cat, '');
    saveDashboardFilters();
    loadDomains();
}

async function onStateChange() {
    currentPage = 1;
    const cat = document.getElementById('cat-select').value;
    const state = document.getElementById('state-select').value;
    await reloadOrgTypeOptions(cat, state);
    saveDashboardFilters();
    loadDomains();
}

function onFilterChange() {
    currentPage = 1;
    saveDashboardFilters();
    loadDomains();
}

function debounceSearch() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
        currentPage = 1;
        saveDashboardFilters();
        loadDomains();
    }, 380);
}

// ── Domains ───────────────────────────────────────────────────────────────────
async function loadDomains() {
    const cat = document.getElementById('cat-select').value;
    const state = document.getElementById('state-select').value;
    const orgType = document.getElementById('orgtype-select').value;
    const search = document.getElementById('search-input').value.trim();
    const params = new URLSearchParams({page: currentPage, limit: 50});
    if (cat) params.set('category', cat);
    if (state) params.set('state', state);
    if (orgType) params.set('org_type', orgType);
    if (search) params.set('search', search);
    try {
        const data = await apiFetch(`/api/domains?${params}`);
        totalPages = data.pages;
        totalMatching = data.total;
        renderDomains(data.domains, data.total);
    } catch (e) {
        console.error('loadDomains', e);
    }
}

function renderDomains(domains, total) {
    document.getElementById('page-info').textContent =
        total > 0 ? `Page ${currentPage} of ${totalPages}` : '—';
    document.getElementById('total-label').textContent =
        total > 0 ? `${total.toLocaleString()} domains` : '';
    document.getElementById('domain-count-label').textContent =
        total > 0 ? `${total.toLocaleString()} matching domains` : 'No domains match filters';
    document.getElementById('btn-prev').disabled = currentPage <= 1;
    document.getElementById('btn-next').disabled = currentPage >= totalPages;

    // Update "select all N" in toolbar
    const btnSelectAll = document.getElementById('btn-select-all-results');
    document.getElementById('sel-all-toolbar-n').textContent = total.toLocaleString();
    btnSelectAll.style.display = total > 0 ? '' : 'none';

    const tbody = document.getElementById('domain-tbody');
    if (!domains.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No domains found. Try changing your filters.</td></tr>';
        return;
    }

    tbody.innerHTML = '';
    domains.forEach(d => {
        const label = domainLabel(d);
        const shortUrl = domainShortUrl(d);
        const url = d.contact_url || d.main_url || '';
        const catCode = (d.category_code || 'default').toLowerCase();
        const orgLabel = d.org_type_title || d.org_type || '—';
        const checked = selectedIds.has(d.id);

        const tr = document.createElement('tr');
        tr.innerHTML = `
      <td style="width:40px"><input type="checkbox" ${checked ? 'checked' : ''} onchange="toggleDomain(${d.id}, this.checked)"></td>
      <td>
        <div class="d-name">${esc(label)}</div>
        ${d.state ? `<div class="d-state">${esc(d.state)}</div>` : ''}
      </td>
      <td><span class="tag tag-${catCode}">${(d.category_code || '').toUpperCase()}</span></td>
      <td style="font-size:12px;color:var(--muted)">${esc(orgLabel)}</td>
      <td style="max-width:180px">
        ${url ? `<a href="${esc(url)}" target="_blank" style="font-size:11px;font-family:monospace;color:var(--muted)" title="${esc(url)}">↗ ${esc(shortUrl)}</a>` : '<span style="color:var(--small)">—</span>'}
      </td>`;
        tbody.appendChild(tr);
    });

    updateSelectAll();
    updateSelCount();
}

function toggleDomain(id, checked) {
    if (checked) selectedIds.add(id); else selectedIds.delete(id);
    saveSelection();
    updateSelCount();
}

function toggleSelectAll(checked) {
    document.querySelectorAll('#domain-tbody input[type=checkbox]').forEach(cb => {
        const m = cb.getAttribute('onchange').match(/\d+/);
        if (!m) return;
        const id = parseInt(m[0]);
        if (checked) selectedIds.add(id); else selectedIds.delete(id);
        cb.checked = checked;
    });
    saveSelection();
    updateSelCount();
}

async function selectAllResults() {
    const cat = document.getElementById('cat-select').value;
    const state = document.getElementById('state-select').value;
    const orgType = document.getElementById('orgtype-select').value;
    const search = document.getElementById('search-input').value.trim();
    const params = new URLSearchParams();
    if (cat) params.set('category', cat);
    if (state) params.set('state', state);
    if (orgType) params.set('org_type', orgType);
    if (search) params.set('search', search);
    try {
        const data = await apiFetch(`/api/domains/ids?${params}`);
        data.ids.forEach(id => selectedIds.add(id));
        document.querySelectorAll('#domain-tbody input[type=checkbox]').forEach(cb => cb.checked = true);
        saveSelection();
        updateSelectAll();
        updateSelCount();
    } catch (e) {
        // Endpoint not available yet — restart server
        alert(`Select-all requires a server restart.\n\nAfter restarting, this will select all ${totalMatching.toLocaleString()} domains.`);
    }
}

function clearSelection() {
    selectedIds.clear();
    document.querySelectorAll('#domain-tbody input[type=checkbox]').forEach(cb => cb.checked = false);
    document.getElementById('select-all').checked = false;
    document.getElementById('select-all').indeterminate = false;
    saveSelection();
    updateSelCount();
}

function updateSelCount() {
    const n = selectedIds.size;
    document.getElementById('sel-count').textContent = n.toLocaleString();
    const showBtn = n > 0 ? 'inline-block' : 'none';
    document.getElementById('sel-count-label').style.display = showBtn;
    document.getElementById('crawl-btn').style.display = showBtn;
}

function updateSelectAll() {
    const all = document.querySelectorAll('#domain-tbody input[type=checkbox]');
    const n = Array.from(all).filter(cb => cb.checked).length;
    document.getElementById('select-all').checked = all.length > 0 && n === all.length;
    document.getElementById('select-all').indeterminate = n > 0 && n < all.length;
}

function prevPage() {
    if (currentPage > 1) {
        currentPage--;
        loadDomains();
    }
}

function nextPage() {
    if (currentPage < totalPages) {
        currentPage++;
        loadDomains();
    }
}

// ── Import ────────────────────────────────────────────────────────────────────
function triggerJsonImport() {
    document.getElementById('json-file-input').click();
}

document.addEventListener('DOMContentLoaded', function () {
    document.getElementById('json-file-input').addEventListener('change', async function () {
        const file = this.files[0];
        if (!file) return;
        document.getElementById('json-file-label').textContent = file.name;
        if (!confirm(`Import from "${file.name}"?\n\nThis clears all existing domains.`)) {
            this.value = '';
            return;
        }
        const form = new FormData();
        form.append('file', file);
        try {
            await apiFetch('/api/import/json', {method: 'POST', body: form});
            startImportPoll();
        } catch (e) {
            alert('Import failed to start: ' + e.message);
        }
        this.value = '';
    });
});

async function triggerImport() {
    if (!confirm('Refresh from india.gov.in API?\n\nThis makes many network requests and takes 2–5 minutes.')) return;
    try {
        await apiFetch('/api/import', {method: 'POST'});
        startImportPoll();
    } catch (e) {
        alert('Import failed: ' + e.message);
    }
}

function startImportPoll() {
    document.getElementById('import-progress').style.display = 'block';
    document.getElementById('import-json-btn').disabled = true;
    document.getElementById('import-btn').disabled = true;
    if (impPollTimer) clearInterval(impPollTimer);
    impPollTimer = setInterval(checkImportStatus, 1500);
}

async function checkImportStatus() {
    try {
        const s = await apiFetch('/api/import/status');
        const bar = document.getElementById('import-bar');
        const msg = document.getElementById('import-msg-text');
        const dot = document.getElementById('import-dot');
        const prg = document.getElementById('import-progress');

        if (s.running) {
            prg.style.display = 'block';
            dot.className = 'dot dot-yellow';
            const pct = s.total_entries > 0 ? Math.round(s.inserted / s.total_entries * 100) : 0;
            bar.style.width = pct + '%';
            const src = s.source === 'json' ? 'JSON' : 'API';
            msg.textContent = `[${src}] ${s.done_categories}/${s.total_categories} categories · ${s.inserted.toLocaleString()} domains`;
        } else if (s.inserted > 0) {
            prg.style.display = 'block';
            dot.className = 'dot dot-green';
            bar.style.width = '100%';
            msg.textContent = `Done — ${s.inserted.toLocaleString()} domains imported`;
            document.getElementById('import-json-btn').disabled = false;
            document.getElementById('import-btn').disabled = false;
            clearInterval(impPollTimer);
            document.getElementById('db-count').textContent = `${s.inserted.toLocaleString()} domains`;
            loadFilters();
            loadDomains();
        } else if (s.error) {
            prg.style.display = 'block';
            dot.className = 'dot dot-red';
            msg.textContent = 'Error: ' + s.error;
            document.getElementById('import-json-btn').disabled = false;
            document.getElementById('import-btn').disabled = false;
            clearInterval(impPollTimer);
        } else {
            prg.style.display = 'none';
            document.getElementById('import-json-btn').disabled = false;
            document.getElementById('import-btn').disabled = false;
        }
    } catch (e) { /* server starting */
    }
}

// ── Crawl ─────────────────────────────────────────────────────────────────────
async function startCrawl() {
    if (selectedIds.size === 0) return;
    const cat = document.getElementById('cat-select').value || null;
    const search = document.getElementById('search-input').value.trim() || null;
    try {
        const resp = await apiFetch('/api/jobs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({domain_ids: [...selectedIds], category_filter: cat, title_filter: search}),
        });
        activeJobId = resp.id;
        openJobPanel(resp.id);
        clearSelection();
        startJobPoll();
        const badge = document.getElementById('live-badge');
        if (badge) badge.style.display = 'flex';
    } catch (e) {
        alert('Failed to start crawl: ' + e.message);
    }
}

async function cancelJob() {
    if (!activeJobId) return;
    if (!confirm('Cancel this job?')) return;
    try {
        await apiFetch(`/api/jobs/${activeJobId}/cancel`, {method: 'POST'});
        updateJobPanel({...await apiFetch(`/api/jobs/${activeJobId}`)});
    } catch (e) {
        alert('Failed to cancel: ' + e.message);
    }
}

// ── Dock Logic ────────────────────────────────────────────────────────────────
let isDraggingDock = false;
document.getElementById('dock-resizer').addEventListener('mousedown', (e) => {
    isDraggingDock = true;
    document.body.style.cursor = 'ns-resize';
});
document.addEventListener('mousemove', (e) => {
    if (!isDraggingDock) return;
    const dock = document.getElementById('bottom-dock');
    const h = Math.max(40, Math.min(window.innerHeight - e.clientY, window.innerHeight * 0.9));
    dock.style.height = h + 'px';
    document.querySelector('.layout').style.paddingBottom = h + 'px';
});
document.addEventListener('mouseup', () => {
    isDraggingDock = false;
    document.body.style.cursor = '';
});

function openDock(tab) {
    const dock = document.getElementById('bottom-dock');
    dock.style.display = 'flex';
    if (!dock.style.height || dock.style.height === '40px') dock.style.height = '250px';
    document.querySelector('.layout').style.paddingBottom = dock.style.height;
    if (tab) switchDockTab(tab);
}

function closeDock() {
    document.getElementById('bottom-dock').style.display = 'none';
    document.querySelector('.layout').style.paddingBottom = '0';
    if (logsTimer) {
        clearInterval(logsTimer);
        logsTimer = null;
    }
}

function switchDockTab(tab) {
    document.querySelectorAll('.dock-tab').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.dock-content').forEach(el => el.style.display = 'none');
    document.getElementById('tab-' + tab).classList.add('active');
    document.getElementById('dock-' + tab).style.display = 'block';
    if (tab === 'logs') {
        if (!logsTimer) logsTimer = setInterval(fetchLogs, 2000);
        fetchLogs();
    } else {
        if (logsTimer) {
            clearInterval(logsTimer);
            logsTimer = null;
        }
    }

    if (tab === 'seeds') fetchJobSeeds();
    if (tab === 'leads') fetchJobLeads();
}

function openJobPanel(jobId) {
    openDock('job');
    document.getElementById('job-title').textContent = `Crawl Job #${jobId}`;
}

function closeJobPanel() {
    closeDock();
}

function startJobPoll() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
        if (!activeJobId) return;
        try {
            const job = await apiFetch(`/api/jobs/${activeJobId}`);
            updateJobPanel(job);
            if (job.status === 'done' || job.status === 'failed') {
                clearInterval(pollTimer);
                const badge = document.getElementById('live-badge');
                if (badge) badge.style.display = 'none';
                loadRecentJobs();
            }
        } catch (e) {
        }
    }, 2000);
}

function updateJobPanel(job) {
    const sv = document.getElementById('job-status-val');
    sv.textContent = job.status.charAt(0).toUpperCase() + job.status.slice(1);
    sv.style.color = job.status === 'done' ? 'var(--green)'
        : job.status === 'failed' ? 'var(--red)' : job.status === 'cancelled' ? 'var(--muted)' : 'var(--yellow)';
    document.getElementById('job-seed-val').textContent = job.seed_domains || job.total_domains;
    document.getElementById('job-queued-val').textContent = job.queued_urls || 0;
    document.getElementById('job-visited-val').textContent = job.visited_urls || 0;
    document.getElementById('job-skipped-val').textContent = job.skipped_urls || 0;
    document.getElementById('job-leads-val').textContent = job.leads_found.toLocaleString();

    let elapsedStr = '—';
    let diffSecs = 0;
    if (job.started_at) {
        const start = new Date(job.started_at + 'Z'); // parse UTC
        let end = new Date();
        if (job.finished_at) {
            end = new Date(job.finished_at + 'Z');
        }
        diffSecs = Math.floor((end - start) / 1000);
        if (diffSecs >= 0) {
            const m = Math.floor(diffSecs / 60);
            const s = diffSecs % 60;
            elapsedStr = m > 0 ? `${m}m ${s}s` : `${s}s`;
        }
    }
    document.getElementById('job-elapsed-val').textContent = elapsedStr;

    const totalUrls = (job.visited_urls || 0) + (job.queued_urls || 0);
    const pct = totalUrls > 0 ? Math.round((job.visited_urls || 0) / totalUrls * 100) : 0;
    document.getElementById('job-progress-bar').style.width = pct + '%';

    document.getElementById('job-depth-val').textContent = `Depth ${job.current_depth ?? '—'}`;
    document.getElementById('job-domains-val').textContent =
        `${job.crawled_domains ?? '—'} / ${job.total_domains ?? '—'} domains`;
    const rate = diffSecs > 0
        ? ((job.visited_urls || 0) / diffSecs).toFixed(1) + ' URLs/s'
        : '— URLs/s';
    document.getElementById('job-rate-val').textContent = rate;
    document.getElementById('job-workers-val').textContent =
        `${job.active_workers ?? '—'} active`;

    if (job.status === 'done' || job.status === 'failed' || job.status === 'cancelled') {
        document.getElementById('btn-cancel-job').style.display = 'none';
    } else {
        document.getElementById('btn-cancel-job').style.display = '';
    }
}

// ── Leads & Logs logic ────────────────────────────────────────────────────────
function viewLeads() {
    window.location.href = '/leads';
}

function openResults() {
    window.location.href = '/leads';
}

async function exportDashboardLeads() {
    if (!activeJobId) {
        alert("No active job selected to export leads from.");
        return;
    }

    const body = { job_id: activeJobId };
    
    // Optional: show loading feedback on the button
    const btn1 = document.getElementById('btn-export-leads');
    const btn2 = document.getElementById('btn-export-leads-tab');
    const originalText1 = btn1 ? btn1.textContent : '';
    const originalText2 = btn2 ? btn2.textContent : '';
    
    if (btn1) btn1.textContent = "Exporting...";
    if (btn2) btn2.textContent = "Exporting...";

    try {
        const resp = await fetch('/api/leads/export', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        
        if (!resp.ok) {
            if (resp.status === 404) {
                alert("No leads found for this job.");
                return;
            }
            let errText = "Unknown error";
            try {
                const errJson = await resp.json();
                errText = errJson.detail || errText;
            } catch (e) {
                errText = await resp.text();
            }
            throw new Error(errText);
        }

        const blob = await resp.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `leads_export_job#${activeJobId}_${new Date().toISOString().slice(0, 10)}.csv`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
    } catch (e) {
        console.error("Export failed:", e);
        alert("Export failed: " + e.message);
    } finally {
        if (btn1) btn1.textContent = originalText1;
        if (btn2) btn2.textContent = originalText2;
    }
}

let logsTimer = null;

function openLogs() {
    openDock('logs');
}

function closeLogs() {
    closeDock();
}

async function fetchLogs() {
    try {
        const data = await apiFetch('/api/logs');
        const pre = document.getElementById('logs-content');
        const isAtBottom = pre.scrollHeight - pre.scrollTop <= pre.clientHeight + 20;
        pre.textContent = data.logs;
        if (isAtBottom) pre.scrollTop = pre.scrollHeight;
    } catch (e) {
        document.getElementById('logs-content').textContent = 'Error fetching logs: ' + e.message;
    }
}

async function fetchJobSeeds() {
    if (!activeJobId) return;
    try {
        const seeds = await apiFetch(`/api/jobs/${activeJobId}/seeds`);
        const tbody = document.getElementById('seeds-tbody');
        if (!seeds.length) {
            tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No seeds found for this job.</td></tr>';
            return;
        }
        tbody.innerHTML = '';
        seeds.forEach(d => {
            const url = d.main_url || '';
            const catCode = (d.category || 'default').toLowerCase();
            tbody.innerHTML += `
        <tr>
          <td><div class="d-name">${esc(d.title || url)}</div></td>
          <td>${d.state ? `<div class="d-state">${esc(d.state)}</div>` : '<span style="color:var(--small)">—</span>'}</td>
          <td><span class="tag tag-${catCode}">${(d.category || '').toUpperCase()}</span></td>
          <td style="max-width:180px">
            ${url ? `<a href="${esc(url)}" target="_blank" style="font-size:11px;font-family:monospace;color:var(--muted)">↗ ${esc(url.replace(/^https?:\/\//, '').replace(/\/$/, ''))}</a>` : '—'}
          </td>
        </tr>
          `;
        });
    } catch (e) {
        document.getElementById('seeds-tbody').innerHTML = `<tr><td colspan="4" class="empty-state" style="color:var(--red)">Error loading seeds.</td></tr>`;
    }
}

async function useSameSeeds() {
    if (!activeJobId) return;
    try {
        const seeds = await apiFetch(`/api/jobs/${activeJobId}/seeds`);
        const ids = seeds.map(s => s.id);
        if (!ids.length) {
            alert("No seeds found in this job.");
            return;
        }
        if (!confirm(`Start a new job with these ${ids.length} seeds?`)) return;

        const resp = await apiFetch('/api/jobs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({domain_ids: ids}),
        });

        activeJobId = resp.id;
        openJobPanel(resp.id);
        clearSelection();
        startJobPoll();
        const badge = document.getElementById('live-badge');
        if (badge) badge.style.display = 'flex';
    } catch (e) {
        alert('Failed to start crawl: ' + e.message);
    }
}

async function fetchJobLeads() {
    if (!activeJobId) return;
    try {
        const data = await apiFetch(`/api/leads?job_id=${activeJobId}&limit=100`);
        const tbody = document.getElementById('dock-leads-tbody');
        if (!data.leads.length) {
            tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No leads found yet.</td></tr>';
            return;
        }
        tbody.innerHTML = '';
        data.leads.forEach(L => {
            const urlPart = L.source_url ? L.source_url.split('/').filter(Boolean).pop() : 'link';
            tbody.innerHTML += `
        <tr>
          <td><strong style="color:var(--text)">${esc(L.email)}</strong></td>
          <td>
            <div style="font-weight:500;color:var(--text)">${esc(L.person_name || '—')}</div>
            <div style="font-size:11px;color:var(--small)">${esc(L.designation || L.department || '')}</div>
          </td>
          <td style="font-size:11px;max-width:200px">
            ${L.source_url ? `<a href="${esc(L.source_url)}" target="_blank" style="color:var(--muted)">↗ ${esc(urlPart)}</a>` : '—'}
          </td>
          <td style="font-size:11px;color:var(--muted)">${esc(L.domain_title || '')}</td>
        </tr>
          `;
        });
    } catch (e) {
        document.getElementById('dock-leads-tbody').innerHTML = `<tr><td colspan="4" class="empty-state" style="color:var(--red)">Error loading leads.</td></tr>`;
    }
}

// ── Recent jobs sidebar ───────────────────────────────────────────────────────
async function loadRecentJobs(limit = 6) {
    try {
        const jobs = await apiFetch(`/api/jobs?limit=${limit}`);
        const el = document.getElementById('jobs-list');
        if (!el) return;
        if (!jobs.length) {
            el.innerHTML = '<span style="font-size:12px;color:var(--small)">No jobs yet.</span>';
            return;
        }
        el.innerHTML = '';
        jobs.forEach(j => {
            const color = j.status === 'done' ? 'var(--green)'
                : j.status === 'failed' ? 'var(--red)' : 'var(--yellow)';
            const div = document.createElement('div');
            div.className = 'job-item';
            div.innerHTML = `
        <div class="job-item-top">
          <strong>Job #${j.id}</strong>
          <span style="font-size:11px;color:${color};font-weight:500">${j.status}</span>
        </div>
        <div class="job-item-meta">${j.total_domains} domains · ${j.leads_found} leads</div>`;
            div.onclick = () => {
                activeJobId = j.id;
                openJobPanel(j.id);
                updateJobPanel(j);
                if (j.status === 'running' || j.status === 'queued') {
                    startJobPoll();
                }
            };
            el.appendChild(div);
        });

        const runningJob = jobs.find(j => j.status === 'running');
        const badge = document.getElementById('live-badge');
        if (runningJob) {
            if (badge) badge.style.display = 'flex';
            if (!activeJobId) {
                activeJobId = runningJob.id;
                openJobPanel(runningJob.id);
                updateJobPanel(runningJob);
                startJobPoll();
            }
        } else {
            if (badge) badge.style.display = 'none';
        }
    } catch (e) {
    }
}

function viewAllJobs() {
    const btn = document.getElementById('view-all-jobs-btn');
    if (btn) btn.style.display = 'none';
    loadRecentJobs(100);
}

// ── Config panel ──────────────────────────────────────────────────────────────
async function openConfig() {
    document.getElementById('config-panel').classList.add('open');
    await loadConfig();
}

function closeConfig() {
    document.getElementById('config-panel').classList.remove('open');
}

async function loadConfig() {
    let c = {...CFG_DEFAULTS};
    try {
        Object.assign(c, await apiFetch('/api/config'));
    } catch {
    }
    
    const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
    const setCheck = (id, val) => { const el = document.getElementById(id); if (el) el.checked = val; };
    
    setVal('cfg-workers', c.workers);
    setVal('cfg-max-depth', c.max_depth);
    setVal('cfg-recrawl-days', c.recrawl_days);
    setVal('cfg-request-delay', c.request_delay);
    setVal('cfg-per-url-timeout', c.per_url_timeout);
    setCheck('cfg-httpx-first', c.httpx_first);
    setCheck('cfg-playwright-fallback', c.playwright_fallback);
    setVal('cfg-playwright-timeout', c.playwright_timeout);
    setVal('cfg-js-settle', c.js_settle_time);
    setCheck('cfg-email-enabled', c.email_enabled);
    setVal('cfg-email-context', c.email_context_chars);
    setVal('cfg-email-valid-suffixes', c.valid_suffixes || '');
    setCheck('cfg-person-enabled', c.person_enabled);
    setVal('cfg-person-proximity', c.person_proximity_chars);
    setVal('cfg-person-title-prefixes', c.title_prefixes || '');
    setVal('cfg-person-designation-keywords', c.designation_keywords || '');

    setVal('cfg-max-links-0', c.max_links_per_page_0);
    setVal('cfg-max-links-1', c.max_links_per_page_1);
    setVal('cfg-max-links-2', c.max_links_per_page_2);
    setVal('cfg-max-links-default', c.max_links_per_page_default);

    setVal('cfg-target-suffixes', c.target_suffixes || '');
    setVal('cfg-priority-keywords', c.priority_keywords || '');
    setVal('cfg-skip-extensions', c.skip_extensions || '');

    setVal('cfg-user-agent', c.user_agent || '');
    setVal('cfg-email-regex', c.email_regex || '');
    setVal('cfg-js-indicators', c.js_indicators || '');
    setVal('cfg-email-obfuscation', c.email_obfuscation || '');

    checkConfigWarnings();
}

function checkConfigWarnings() {
    const elWorkers = document.getElementById('cfg-workers');
    const elDepth = document.getElementById('cfg-max-depth');
    const workers = elWorkers ? (parseInt(elWorkers.value) || 0) : 0;
    const depth = elDepth ? (parseInt(elDepth.value) || 0) : 0;
    let warnings = [];
    if (workers > 10) warnings.push("• Workers > 10: Advised max is 10. Higher values risk IP bans or memory exhaustion.");
    if (depth > 4) warnings.push("• Max Depth > 4: Advised max is 4. Beyond 4 rarely yields new contacts and exponentially increases crawl time.");

    const warnDiv = document.getElementById('cfg-warn-msg');
    if (warnDiv) {
        if (warnings.length > 0) {
            warnDiv.innerHTML = warnings.join("<br>");
            warnDiv.style.display = 'block';
        } else {
            warnDiv.style.display = 'none';
        }
    }
}

document.getElementById('cfg-workers')?.addEventListener('input', checkConfigWarnings);
document.getElementById('cfg-max-depth')?.addEventListener('input', checkConfigWarnings);

async function saveConfig() {
    const body = {};
    
    // Explicit manual mappings to match server payload exactly
    const addIf = (id, key, parser) => { const el = document.getElementById(id); if (el) body[key] = parser(el.value); };
    const addCheckIf = (id, key) => { const el = document.getElementById(id); if (el) body[key] = el.checked; };

    addIf('cfg-workers', 'workers', parseInt);
    addIf('cfg-max-depth', 'max_depth', parseInt);
    addIf('cfg-recrawl-days', 'recrawl_days', parseInt);
    addIf('cfg-request-delay', 'request_delay', parseFloat);
    addIf('cfg-per-url-timeout', 'per_url_timeout', parseInt);
    addCheckIf('cfg-httpx-first', 'httpx_first');
    addCheckIf('cfg-playwright-fallback', 'playwright_fallback');
    addIf('cfg-playwright-timeout', 'playwright_timeout', parseInt);
    addIf('cfg-js-settle', 'js_settle_time', parseFloat);
    addCheckIf('cfg-email-enabled', 'email_enabled');
    addIf('cfg-email-context', 'email_context_chars', parseInt);
    addIf('cfg-email-valid-suffixes', 'valid_suffixes', String);
    addCheckIf('cfg-person-enabled', 'person_enabled');
    addIf('cfg-person-proximity', 'person_proximity_chars', parseInt);
    addIf('cfg-person-title-prefixes', 'title_prefixes', String);
    addIf('cfg-person-designation-keywords', 'designation_keywords', String);

    addIf('cfg-max-links-0', 'max_links_per_page_0', parseInt);
    addIf('cfg-max-links-1', 'max_links_per_page_1', parseInt);
    addIf('cfg-max-links-2', 'max_links_per_page_2', parseInt);
    addIf('cfg-max-links-default', 'max_links_per_page_default', parseInt);

    addIf('cfg-target-suffixes', 'target_suffixes', String);
    addIf('cfg-priority-keywords', 'priority_keywords', String);
    addIf('cfg-skip-extensions', 'skip_extensions', String);

    try {
        await apiFetch('/api/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        const msg1 = document.getElementById('cfg-save-msg');
        if (msg1) {
            msg1.style.display = 'flex';
            setTimeout(() => msg1.style.display = 'none', 4000);
        }
        const msg2 = document.getElementById('cfg-save-msg-advanced');
        if (msg2) {
            msg2.style.display = 'flex';
            setTimeout(() => msg2.style.display = 'none', 4000);
        }
        if (!msg1 && !msg2) {
            alert("Settings saved!");
        }
    } catch (e) {
        console.error(e);
        alert("Failed to save settings");
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
async function apiFetch(url, opts = {}) {
    const r = await fetch(url, opts);
    if (!r.ok) {
        const b = await r.text();
        throw new Error(`${r.status}: ${b}`);
    }
    return r.json();
}

function esc(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function clearVisitedUrls() {
    if (!confirm('Are you sure you want to clear all visited URLs? This will reset the recrawl protection.')) return;
    try {
        const res = await apiFetch('/api/visited-urls', {method: 'DELETE'});
        alert(res.message);
    } catch (e) {
        alert('Error clearing visited URLs: ' + e.message);
    }
}