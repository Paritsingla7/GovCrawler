let leadsPage = 1;
let leadsTotalPgs = 1;
let selectedLeadIds = new Set();
let leadsSearchTimer = null;
let leadsTotalMatching = 0;
let pendingAddToCampaignId = null;
let leadsSortBy = '';
let leadsSortDir = 'desc';
// Fallback mirrors lead_scoring.DEFAULT_WEIGHTS so the split still renders
// before/without the score-weights fetch; the API response overrides it.
let scoreWeights = {email_high: 20, email_low: 10, person_name: 40, designation: 30, phone: 10};

document.addEventListener('DOMContentLoaded', async () => {
    const urlParams = new URLSearchParams(window.location.search);
    const addToCamp = urlParams.get('add_to_campaign');
    if (addToCamp) {
        pendingAddToCampaignId = parseInt(addToCamp);
        showAddToCampaignBanner();
    }

    setupLeadCellListeners();
    restoreLeadsSelection();
    try {
        const w = await apiFetch('/api/leads/score-weights');
        if (w && typeof w === 'object') scoreWeights = w;
    } catch (e) { /* keep built-in default weights */
    }
    await loadLeadsFilters();
    loadLeads();
});

function showAddToCampaignBanner() {
    const banner = document.createElement('div');
    banner.id = 'atc-banner';
    banner.style.cssText = 'background:#f0e8d0; border-bottom:1px solid #d4be8c; padding:10px 24px; font-size:13px; display:flex; align-items:center; gap:12px; flex-shrink:0;';
    banner.innerHTML = `
        <span>Select leads below, then click <strong>+ Add to Campaign</strong> in the toolbar.</span>
        <a href="/campaigns?id=${pendingAddToCampaignId}" style="margin-left:auto; font-size:12px; color:var(--accent);">← Back to Campaign</a>
    `;
    const toolbar = document.querySelector('.table-toolbar');
    if (toolbar) toolbar.insertAdjacentElement('beforebegin', banner);
}

// ── Multi-select checkbox dropdown widget ──────────────────────────────────────
function createMsDropdown(containerId, {allLabel = 'All', onChange = null} = {}) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = `
        <button type="button" class="ms-dropdown-toggle">
            <span class="ms-dropdown-label">${allLabel}</span>
            <span class="ms-caret">▾</span>
        </button>
        <div class="ms-dropdown-panel"></div>
    `;
    container._allLabel = allLabel;
    container._selected = new Set();
    container._options = [];
    container._onChange = onChange;
    container.querySelector('.ms-dropdown-panel').addEventListener('click', (e) => {
        e.stopPropagation();
    });
    container.querySelector('.ms-dropdown-toggle').addEventListener('click', (e) => {
        e.stopPropagation();
        const isOpen = container.classList.contains('open');
        document.querySelectorAll('.ms-dropdown.open').forEach(el => el.classList.remove('open'));
        if (!isOpen) container.classList.add('open');
    });
}

document.addEventListener('click', () => {
    document.querySelectorAll('.ms-dropdown.open').forEach(el => el.classList.remove('open'));
});

function setMsDropdownOptions(containerId, options) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container._options = options;
    const panel = container.querySelector('.ms-dropdown-panel');
    panel.innerHTML = '';

    // "All" row — checked whenever nothing specific is selected; checking it
    // clears every individual selection (equivalent to "no filter").
    const allRow = document.createElement('div');
    allRow.className = 'ms-dropdown-option';
    const allCb = document.createElement('input');
    allCb.type = 'checkbox';
    allCb.dataset.msAll = 'true';
    allCb.checked = container._selected.size === 0;
    const allSpan = document.createElement('span');
    allSpan.textContent = container._allLabel;
    allRow.appendChild(allCb);
    allRow.appendChild(allSpan);
    panel.appendChild(allRow);
    allCb.addEventListener('change', () => {
        if (allCb.checked) {
            container._selected.clear();
            panel.querySelectorAll('input[type=checkbox]:not([data-ms-all])').forEach(cb => cb.checked = false);
        } else if (container._selected.size === 0) {
            allCb.checked = true; // can't uncheck "All" with nothing else selected
        }
        updateMsDropdownLabel(containerId);
        if (container._onChange) container._onChange();
    });

    options.forEach(opt => {
        const row = document.createElement('div');
        row.className = 'ms-dropdown-option';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = String(opt.value);
        cb.checked = container._selected.has(String(opt.value));
        cb.addEventListener('change', () => {
            if (cb.checked) container._selected.add(String(opt.value));
            else container._selected.delete(String(opt.value));
            allCb.checked = container._selected.size === 0;
            updateMsDropdownLabel(containerId);
            if (container._onChange) container._onChange();
        });
        const span = document.createElement('span');
        span.textContent = opt.label;
        row.appendChild(cb);
        row.appendChild(span);
        panel.appendChild(row);
    });
    updateMsDropdownLabel(containerId);
}

function updateMsDropdownLabel(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const label = container.querySelector('.ms-dropdown-label');
    const n = container._selected.size;
    if (n === 0) {
        label.textContent = container._allLabel;
    } else if (n === 1) {
        const val = [...container._selected][0];
        const opt = container._options.find(o => String(o.value) === val);
        label.textContent = opt ? opt.label : '1 selected';
    } else {
        label.textContent = `${n} selected`;
    }
}

function getMsDropdownValues(containerId) {
    const container = document.getElementById(containerId);
    return container ? [...container._selected] : [];
}

function setMsDropdownValues(containerId, values) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container._selected = new Set((values || []).map(String));
    const panel = container.querySelector('.ms-dropdown-panel');
    panel.querySelectorAll('input[type=checkbox]:not([data-ms-all])').forEach(cb => {
        cb.checked = container._selected.has(cb.value);
    });
    const allCb = panel.querySelector('input[data-ms-all]');
    if (allCb) allCb.checked = container._selected.size === 0;
    updateMsDropdownLabel(containerId);
}

// ── Selection Persistence ─────────────────────────────────────────────────────
function saveLeadsSelection() {
    sessionStorage.setItem('leads_selection', JSON.stringify([...selectedLeadIds]));
}

function restoreLeadsSelection() {
    try {
        const saved = JSON.parse(sessionStorage.getItem('leads_selection') || '[]');
        selectedLeadIds = new Set(saved);
    } catch {
        selectedLeadIds = new Set();
    }
}

// ── Filter Persistence ────────────────────────────────────────────────────────
function getLeadsFilters() {
    try {
        return JSON.parse(sessionStorage.getItem('leads_filters') || '{}');
    } catch {
        return {};
    }
}

function saveLeadsFilters() {
    sessionStorage.setItem('leads_filters', JSON.stringify({
        jobs: getMsDropdownValues('leads-job-msdropdown'),
        cats: getMsDropdownValues('leads-cat-msdropdown'),
        states: getMsDropdownValues('leads-state-msdropdown'),
        search: document.getElementById('leads-search-input')?.value || '',
        completeOnly: document.getElementById('leads-complete-only')?.checked || false,
        orgs: getMsDropdownValues('leads-org-msdropdown'),
        entryType: document.getElementById('leads-entry-type-select')?.value || 'both',
        reqName: document.getElementById('leads-require-name')?.checked || false,
        reqDesig: document.getElementById('leads-require-designation')?.checked || false,
        reqPhone: document.getElementById('leads-require-phone')?.checked || false,
        sortBy: leadsSortBy,
        sortDir: leadsSortDir,
    }));
}

function clearLeadsFilters() {
    sessionStorage.removeItem('leads_filters');
    setMsDropdownValues('leads-job-msdropdown', []);
    setMsDropdownValues('leads-cat-msdropdown', []);
    setMsDropdownValues('leads-state-msdropdown', []);
    setMsDropdownValues('leads-org-msdropdown', []);
    document.getElementById('leads-search-input').value = '';
    document.getElementById('leads-complete-only').checked = false;
    document.getElementById('leads-entry-type-select').value = 'both';
    document.getElementById('leads-require-name').checked = false;
    document.getElementById('leads-require-designation').checked = false;
    document.getElementById('leads-require-phone').checked = false;
    leadsSortBy = '';
    leadsSortDir = 'desc';
    updateLeadsSortHeaders();
    leadsPage = 1;
    Promise.all([reloadLeadsCategoryOptions(), reloadLeadsOrgOptions(), reloadLeadsStateOptions([])])
        .then(() => loadLeads());
}

async function loadLeadsFilters() {
    try {
        const saved = getLeadsFilters();
        const urlParams = new URLSearchParams(window.location.search);
        const urlJobId = urlParams.get('job_id') || '';

        // Populate Job multi-select dropdown
        createMsDropdown('leads-job-msdropdown', {allLabel: 'All Jobs', onChange: onLeadsJobFilterChange});
        try {
            const jobs = await apiFetch('/api/jobs?limit=100');
            const options = jobs.map(j => {
                const date = j.created_at ? j.created_at.slice(0, 10) : '';
                const leadCount = (j.leads_found ?? 0).toLocaleString();
                return {
                    value: j.id,
                    label: `Job #${j.id} — ${j.status}${date ? ' · ' + date : ''} · ${leadCount} lead${j.leads_found === 1 ? '' : 's'}`,
                };
            });
            setMsDropdownOptions('leads-job-msdropdown', options);
            // URL param takes priority over session-saved filter
            if (urlJobId) {
                setMsDropdownValues('leads-job-msdropdown', [urlJobId]);
            } else if (saved.jobs && saved.jobs.length) {
                setMsDropdownValues('leads-job-msdropdown', saved.jobs);
            }
        } catch (e) {
        }

        if (saved.search) document.getElementById('leads-search-input').value = saved.search;
        if (saved.completeOnly) document.getElementById('leads-complete-only').checked = true;
        document.getElementById('leads-entry-type-select').value = saved.entryType || 'both';
        if (saved.reqName) document.getElementById('leads-require-name').checked = true;
        if (saved.reqDesig) document.getElementById('leads-require-designation').checked = true;
        if (saved.reqPhone) document.getElementById('leads-require-phone').checked = true;
        leadsSortBy = saved.sortBy || '';
        leadsSortDir = saved.sortDir || 'desc';
        updateLeadsSortHeaders();

        createMsDropdown('leads-cat-msdropdown', {allLabel: 'All Categories', onChange: onLeadsFilterChange});
        await reloadLeadsCategoryOptions();
        if (saved.cats && saved.cats.length) setMsDropdownValues('leads-cat-msdropdown', saved.cats);

        createMsDropdown('leads-state-msdropdown', {allLabel: 'All States', onChange: onLeadsFilterChange});
        await reloadLeadsStateOptions(getMsDropdownValues('leads-cat-msdropdown'));
        if (saved.states && saved.states.length) setMsDropdownValues('leads-state-msdropdown', saved.states);

        createMsDropdown('leads-org-msdropdown', {allLabel: 'All Organizations', onChange: onLeadsFilterChange});
        await reloadLeadsOrgOptions();
        if (saved.orgs && saved.orgs.length) setMsDropdownValues('leads-org-msdropdown', saved.orgs);
    } catch (e) {
    }
}

// Category and org-type options are scoped by job only (see /api/leads/categories,
// /api/leads/org-types) — re-fetched whenever the job selection changes so their
// counts stay job-scoped instead of going stale after a job switch.
async function reloadLeadsCategoryOptions() {
    try {
        const prev = getMsDropdownValues('leads-cat-msdropdown');
        const jobIdParams = new URLSearchParams();
        getMsDropdownValues('leads-job-msdropdown').forEach(id => jobIdParams.append('job_id', id));
        const qs = jobIdParams.toString() ? `?${jobIdParams.toString()}` : '';
        const cats = await apiFetch(`/api/leads/categories${qs}`);
        setMsDropdownOptions('leads-cat-msdropdown', cats.map(c => (
            {value: c.code, label: `${c.title} (${c.count.toLocaleString()})`}
        )));
        const validCodes = cats.map(c => String(c.code));
        setMsDropdownValues('leads-cat-msdropdown', prev.filter(c => validCodes.includes(c)));
    } catch (e) {
    }
}

async function reloadLeadsOrgOptions() {
    try {
        const prev = getMsDropdownValues('leads-org-msdropdown');
        const jobIdParams = new URLSearchParams();
        getMsDropdownValues('leads-job-msdropdown').forEach(id => jobIdParams.append('job_id', id));
        const qs = jobIdParams.toString() ? `?${jobIdParams.toString()}` : '';
        const orgs = await apiFetch(`/api/leads/org-types${qs}`);
        setMsDropdownOptions('leads-org-msdropdown', orgs.map(o => (
            {value: o.code, label: `${o.title} (${o.count.toLocaleString()})`}
        )));
        const validCodes = orgs.map(o => String(o.code));
        setMsDropdownValues('leads-org-msdropdown', prev.filter(o => validCodes.includes(o)));
    } catch (e) {
    }
}

async function reloadLeadsStateOptions(cats) {
    try {
        const prev = getMsDropdownValues('leads-state-msdropdown');
        const params = new URLSearchParams();
        getMsDropdownValues('leads-job-msdropdown').forEach(id => params.append('job_id', id));
        (cats || []).forEach(c => params.append('category', c));

        const qs = params.toString() ? `?${params.toString()}` : '';
        const states = await apiFetch(`/api/leads/states${qs}`);
        setMsDropdownOptions('leads-state-msdropdown', states.map(s => ({value: s, label: s})));
        // Drop any previously-selected states no longer valid for the current category filter
        setMsDropdownValues('leads-state-msdropdown', prev.filter(s => states.includes(s)));
    } catch (e) {
    }
}

async function onLeadsFilterChange() {
    leadsPage = 1;
    await reloadLeadsStateOptions(getMsDropdownValues('leads-cat-msdropdown'));
    saveLeadsFilters();
    await loadLeads();
}

// Job selection changes the scope for categories/org-types too (both are
// job-scoped queries) — refresh them first so onLeadsFilterChange's state
// reload and lead count read the current job's data, not the previous one.
async function onLeadsJobFilterChange() {
    await reloadLeadsCategoryOptions();
    await reloadLeadsOrgOptions();
    await onLeadsFilterChange();
}

function debounceLeadsSearch() {
    clearTimeout(leadsSearchTimer);
    leadsSearchTimer = setTimeout(() => {
        leadsPage = 1;
        saveLeadsFilters();
        loadLeads();
    }, 380);
}

// ── Column-header sorting (Score / Contact / Name only) ────────────────────────
function setLeadsSort(key) {
    if (leadsSortBy !== key) {
        leadsSortBy = key;
        leadsSortDir = 'desc';
    } else if (leadsSortDir === 'desc') {
        leadsSortDir = 'asc';
    } else {
        leadsSortBy = '';
        leadsSortDir = 'desc';
    }
    updateLeadsSortHeaders();
    leadsPage = 1;
    saveLeadsFilters();
    loadLeads();
}

function updateLeadsSortHeaders() {
    document.querySelectorAll('.leads-sort-header').forEach(th => {
        const arrow = th.querySelector('.sort-arrow');
        if (!arrow) return;
        arrow.textContent = th.dataset.sortKey === leadsSortBy
            ? (leadsSortDir === 'desc' ? '▼' : '▲')
            : '';
    });
}

function getLeadsFilterParams() {
    const jobIds = getMsDropdownValues('leads-job-msdropdown');
    const cats = getMsDropdownValues('leads-cat-msdropdown');
    const states = getMsDropdownValues('leads-state-msdropdown');
    const search = document.getElementById('leads-search-input').value.trim();
    const completeOnly = document.getElementById('leads-complete-only').checked;
    const orgTypes = getMsDropdownValues('leads-org-msdropdown');
    const entryType = document.getElementById('leads-entry-type-select').value;
    const reqName = document.getElementById('leads-require-name').checked;
    const reqDesig = document.getElementById('leads-require-designation').checked;
    const reqPhone = document.getElementById('leads-require-phone').checked;
    const params = new URLSearchParams();
    jobIds.forEach(id => params.append('job_id', id));
    cats.forEach(c => params.append('category', c));
    states.forEach(s => params.append('state', s));
    if (search) params.set('search', search);
    if (completeOnly) params.set('complete_only', 'true');
    orgTypes.forEach(o => params.append('org_type', o));
    params.set('entry_type', entryType);
    if (reqName) params.set('require_name', 'true');
    if (reqDesig) params.set('require_designation', 'true');
    if (reqPhone) params.set('require_phone', 'true');
    if (leadsSortBy) {
        params.set('sort_by', leadsSortBy);
        params.set('sort_dir', leadsSortDir);
    }
    return params;
}

async function loadLeads() {
    try {
        const params = getLeadsFilterParams();
        params.set('page', leadsPage);
        params.set('limit', 100);

        const data = await apiFetch(`/api/leads?${params}`);
        leadsTotalPgs = data.pages;
        leadsTotalMatching = data.total;
        renderLeads(data.leads, data.total);
    } catch (e) {
        console.error('loadLeads', e);
    }
}

function editableCell(leadId, field, val, tdStyle) {
    const safeVal = esc(val || '');
    const styleStr = tdStyle
        ? Object.entries(tdStyle).map(([k, v]) => `${k}:${v}`).join(';')
        : '';
    return `<td style="${styleStr}"><input type="text"
        class="lead-cell-input"
        data-lead-id="${leadId}"
        data-field="${field}"
        data-orig="${safeVal}"
        value="${safeVal}"
        placeholder="—"
        autocomplete="off"
        spellcheck="false"
    ></td>`;
}

function scoreBreakdown(l) {
    if (l.channel_tag === 'manual') {
        return 'Manually entered — not run through scoring.';
    }
    const has = (f) => !!(l[f] && String(l[f]).trim());
    const lines = [];

    const emailMax = scoreWeights.email_high;
    const emailHigh = l.confidence_band === 'HIGH';
    const emailPts = emailHigh ? scoreWeights.email_high : scoreWeights.email_low;
    lines.push(`Email: ${emailPts}/${emailMax} (${emailHigh ? 'verified — mailto/microdata' : 'scraped from page text'})`);

    const nameMax = scoreWeights.person_name;
    lines.push(`Name: ${has('person_name') ? nameMax : 0}/${nameMax}${has('person_name') ? '' : ' (missing)'}`);

    const desigMax = scoreWeights.designation;
    lines.push(`Designation: ${has('designation') ? desigMax : 0}/${desigMax}${has('designation') ? '' : ' (missing)'}`);

    const phoneMax = scoreWeights.phone;
    lines.push(`Phone: ${has('phone') ? phoneMax : 0}/${phoneMax}${has('phone') ? '' : ' (missing)'}`);

    const totalMax = emailMax + nameMax + desigMax + phoneMax;
    lines.push(`Total: ${l.lead_score ?? 0}/${totalMax}`);
    return lines.join('\n');
}

function scoreBadge(l) {
    if (l.channel_tag === 'manual') {
        return '<span class="badge badge-secondary" title="Manually entered — not scored">MANUAL</span>';
    }
    const score = l.lead_score ?? 0;
    const tier = score >= 70 ? 'success' : score >= 40 ? 'warning' : 'secondary';
    return `<span class="badge badge-${tier}" title="${esc(scoreBreakdown(l))}">${score}</span>`;
}

function renderLeads(leads, total) {
    document.getElementById('leads-total-label').textContent = `${total.toLocaleString()} total leads`;
    document.getElementById('leads-page-info').textContent = total > 0 ? `Page ${leadsPage} of ${leadsTotalPgs}` : '—';
    document.getElementById('leads-count-label').textContent = total > 0 ? `${total.toLocaleString()} matching leads` : 'No leads match filters';

    document.getElementById('btn-leads-prev').disabled = leadsPage <= 1;
    document.getElementById('btn-leads-next').disabled = leadsPage >= leadsTotalPgs;

    const btnSelectAll = document.getElementById('btn-select-all-leads');
    document.getElementById('sel-all-leads-toolbar-n').textContent = total.toLocaleString();
    btnSelectAll.style.display = total > 0 ? '' : 'none';

    const tbody = document.getElementById('leads-tbody');
    tbody.innerHTML = '';
    if (!leads.length) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-state">No leads found.</td></tr>';
        return;
    }
    const WARN_FIELDS = ['person_name', 'designation', 'department', 'domain_state'];
    leads.forEach(l => {
        const catCode = (l.category_code || 'default').toLowerCase();
        const checked = selectedLeadIds.has(l.id);
        const missing = WARN_FIELDS.filter(f => !l[f] || !String(l[f]).trim());
        let domainUrl = null;
        try {
            if (l.source_url) domainUrl = new URL(l.source_url).origin;
        } catch (_) {
        }
        const tr = document.createElement('tr');
        if (missing.length) tr.classList.add('row-warn');
        tr.innerHTML = [
            `<td style="width:40px"><input type="checkbox" ${checked ? 'checked' : ''} onchange="toggleLead(${l.id}, this.checked)"></td>`,
            missing.length
                ? `<td class="warn-cell"><span class="warn-flag" title="Missing: ${esc(missing.map(f => f.replace(/_/g, ' ')).join(', '))}">⚠</span></td>`
                : `<td></td>`,
            `<td style="text-align:center">${scoreBadge(l)}</td>`,
            `<td>
                <a href="mailto:${esc(l.email)}" style="display:block;font-family:monospace;font-size:11px;color:var(--accent)">${esc(l.email)}<span style="font-size:9px;margin-left:2px;opacity:0.55">↗</span></a>
            </td>`,
            `<td>${l.phone
                ? `<a href="tel:${esc(l.phone)}" style="display:block;font-family:monospace;font-size:11px;color:var(--muted)">📞 ${esc(l.phone)}</a>`
                : '<span class="empty-state" style="padding:0">—</span>'}
            </td>`,
            `<td class="lead-person-cell">
                <input type="text" class="lead-cell-input lead-primary-input" data-lead-id="${l.id}" data-field="person_name" data-orig="${esc(l.person_name || '')}" value="${esc(l.person_name || '')}" placeholder="Name" autocomplete="off" spellcheck="false">
                <input type="text" class="lead-cell-input lead-sub-input" data-lead-id="${l.id}" data-field="designation" data-orig="${esc(l.designation || '')}" value="${esc(l.designation || '')}" placeholder="Designation" autocomplete="off" spellcheck="false">
            </td>`,
            editableCell(l.id, 'department', l.department, {'font-size': '12px', 'color': 'var(--muted)'}),
            `<td style="text-align:center">
                <input type="text" class="lead-cell-input lead-primary-input" style="font-size:12px;color:var(--muted);text-align:center" data-lead-id="${l.id}" data-field="domain_state" data-orig="${esc(l.domain_state || '')}" value="${esc(l.domain_state || '')}" placeholder="State" autocomplete="off" spellcheck="false">
                <div style="margin-top:3px"><span class="tag tag-${catCode}">${catCode.toUpperCase()}</span></div>
            </td>`,
            `<td>${domainUrl
                ? `<p class="d-name" style="display:block;color:var(--text);text-decoration:none">${esc(l.domain_title)}</p>`
                : `<div class="d-name">${esc(l.domain_title || '—')}</div>`
            }<div style="font-size:10px;color:var(--small);margin-top:2px">Depth: ${l.depth ?? 0}</div></td>`,
            `<td style="max-width:200px">${l.channel_tag === 'manual'
                ? `<span class="badge badge-secondary" title="Uploaded via CSV">Manual Upload</span>`
                : l.source_url
                    ? `<a href="${esc(l.source_url)}" target="_blank" style="font-size:11px;color:var(--muted)" title="${esc(l.source_url)}">${esc(l.source_title || '')}${l.source_title ? '' : '—'}<span style="font-size:9px;margin-left:2px;opacity:0.55">↗</span></a>`
                    : `<span style="color:var(--small)">${esc(l.source_title || '—')}</span>`
            }</td>`,
        ].join('');
        tbody.appendChild(tr);
    });
    updateSelectAllLeads();
    updateSelLeadsCount();
}

async function saveLead(input) {
    const newVal = input.value.trim();
    if (newVal === input.getAttribute('data-orig')) return;

    const leadId = input.getAttribute('data-lead-id');
    const field = input.getAttribute('data-field');

    input.classList.add('saving');
    input.disabled = true;

    try {
        const res = await fetch(`/api/leads/${leadId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({[field]: newVal || null})
        });
        if (!res.ok) {
            let detail = 'Save failed';
            try {
                detail = (await res.json()).detail || detail;
            } catch (_) {
            }
            throw new Error(detail);
        }
        input.setAttribute('data-orig', newVal);
        input.classList.remove('saving');
        input.classList.add('saved');
        setTimeout(() => input.classList.remove('saved'), 1500);
    } catch (err) {
        console.error('saveLead:', err);
        input.value = input.getAttribute('data-orig');
        input.classList.remove('saving');
        input.classList.add('error');
        setTimeout(() => input.classList.remove('error'), 2000);
    } finally {
        input.disabled = false;
    }
}

function setupLeadCellListeners() {
    const tbody = document.getElementById('leads-tbody');
    if (!tbody) return;

    tbody.addEventListener('blur', function (e) {
        if (e.target.classList.contains('lead-cell-input')) saveLead(e.target);
    }, true);

    tbody.addEventListener('keydown', function (e) {
        if (!e.target.classList.contains('lead-cell-input')) return;
        if (e.key === 'Enter') {
            e.preventDefault();
            e.target.blur();
        }
        if (e.key === 'Escape') {
            e.target.value = e.target.getAttribute('data-orig');
            e.target.blur();
        }
    });
}

function toggleLead(id, checked) {
    if (checked) selectedLeadIds.add(id); else selectedLeadIds.delete(id);
    saveLeadsSelection();
    updateSelLeadsCount();
}

function toggleSelectAllLeads(checked) {
    document.querySelectorAll('#leads-tbody input[type=checkbox]').forEach(cb => {
        const m = cb.getAttribute('onchange').match(/\d+/);
        if (!m) return;
        const id = parseInt(m[0]);
        if (checked) selectedLeadIds.add(id); else selectedLeadIds.delete(id);
        cb.checked = checked;
    });
    saveLeadsSelection();
    updateSelLeadsCount();
}

async function selectAllLeads() {
    const params = getLeadsFilterParams();
    try {
        const data = await apiFetch(`/api/leads/ids?${params}`);
        data.ids.forEach(id => selectedLeadIds.add(id));
        document.querySelectorAll('#leads-tbody input[type=checkbox]').forEach(cb => cb.checked = true);
        saveLeadsSelection();
        updateSelectAllLeads();
        updateSelLeadsCount();
    } catch (e) {
    }
}

function clearLeadsSelection() {
    selectedLeadIds.clear();
    document.querySelectorAll('#leads-tbody input[type=checkbox]').forEach(cb => cb.checked = false);
    document.getElementById('leads-select-all').checked = false;
    document.getElementById('leads-select-all').indeterminate = false;
    saveLeadsSelection();
    updateSelLeadsCount();
}

function updateSelLeadsCount() {
    const n = selectedLeadIds.size;
    document.getElementById('sel-leads-count').textContent = n.toLocaleString();
    const show = n > 0 ? 'inline-block' : 'none';
    document.getElementById('sel-leads-count-label').style.display = show;
    document.getElementById('btn-create-campaign').style.display = show;
    document.getElementById('btn-add-to-campaign').style.display = show;
}

function updateSelectAllLeads() {
    const all = document.querySelectorAll('#leads-tbody input[type=checkbox]');
    const n = Array.from(all).filter(cb => cb.checked).length;
    document.getElementById('leads-select-all').checked = all.length > 0 && n === all.length;
    document.getElementById('leads-select-all').indeterminate = n > 0 && n < all.length;
}

function openExportModal() {
    const scopeLabel = document.getElementById('export-scope-label');
    if (selectedLeadIds.size > 0) {
        scopeLabel.textContent = `Exporting ${selectedLeadIds.size.toLocaleString()} selected lead(s).`;
    } else {
        scopeLabel.textContent = `Exporting all ${leadsTotalMatching.toLocaleString()} lead(s) matching current filters.`;
    }
    document.getElementById('modal-export').style.display = 'flex';
}

function closeExportModal() {
    document.getElementById('modal-export').style.display = 'none';
}

async function confirmExport() {
    const checkedFields = Array.from(
        document.querySelectorAll('input[name="export-field"]:checked')
    ).map(cb => cb.value);

    const params = getLeadsFilterParams();
    const jobIds = params.getAll('job_id').map(id => parseInt(id));
    const categories = params.getAll('category');
    const states = params.getAll('state');
    const orgTypes = params.getAll('org_type');
    const body = {
        job_ids: jobIds.length ? jobIds : null,
        categories: categories.length ? categories : null,
        states: states.length ? states : null,
        search: params.get('search') || null,
        complete_only: params.get('complete_only') === 'true',
        org_types: orgTypes.length ? orgTypes : null,
        entry_type: params.get('entry_type') || 'both',
        require_name: params.get('require_name') === 'true',
        require_designation: params.get('require_designation') === 'true',
        require_phone: params.get('require_phone') === 'true',
        fields: checkedFields,
    };
    if (selectedLeadIds.size > 0) {
        body.lead_ids = [...selectedLeadIds];
    }

    const btn = document.getElementById('btn-export-confirm');
    btn.textContent = 'Exporting…';
    btn.disabled = true;

    try {
        const resp = await fetch('/api/leads/export', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });

        if (!resp.ok) {
            if (resp.status === 404) {
                alert('No leads matched your current filters to export.');
                return;
            }
            let errText = 'Unknown error';
            try {
                const errJson = await resp.json();
                errText = errJson.detail || errText;
            } catch (e) {
                errText = await resp.text();
            }
            throw new Error(errText);
        }

        const blob = await resp.blob();
        if (blob.size === 0) {
            alert('No leads matched your current filters to export.');
            return;
        }

        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `leads_export_${new Date().toISOString().slice(0, 10)}.csv`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
        closeExportModal();
    } catch (e) {
        alert('Export failed: ' + e.message);
    } finally {
        btn.textContent = '⬇ Export CSV';
        btn.disabled = false;
    }
}

// ── Import Leads from CSV ────────────────────────────────────────────────────

function openImportModal() {
    document.getElementById('import-csv-file').value = '';
    document.getElementById('import-result').style.display = 'none';
    document.getElementById('modal-import').style.display = 'flex';
}

function closeImportModal() {
    document.getElementById('modal-import').style.display = 'none';
}

async function confirmImport() {
    const fileInput = document.getElementById('import-csv-file');
    const file = fileInput.files[0];
    if (!file) {
        alert('Choose a CSV file first.');
        return;
    }

    const btn = document.getElementById('btn-import-confirm');
    btn.disabled = true;
    btn.textContent = 'Importing…';

    const resultBox = document.getElementById('import-result');
    resultBox.style.display = 'none';

    try {
        const formData = new FormData();
        formData.append('file', file);

        const res = await fetch('/api/leads/import-csv', {method: 'POST', body: formData});
        if (!res.ok) {
            let detail = 'Import failed';
            try {
                detail = (await res.json()).detail || detail;
            } catch (_) {
            }
            throw new Error(detail);
        }

        const data = await res.json();
        let html = `<strong>${data.imported}</strong> imported, <strong>${data.updated}</strong> updated`;
        if (data.skipped.length) {
            html += `, <strong>${data.skipped.length}</strong> skipped:`;
            html += '<ul style="margin:6px 0 0 18px;max-height:160px;overflow-y:auto;">';
            data.skipped.forEach(s => {
                html += `<li>Row ${s.row}${s.email ? ` (${esc(s.email)})` : ''}: ${esc(s.reason)}</li>`;
            });
            html += '</ul>';
        }
        resultBox.innerHTML = html;
        resultBox.style.display = 'block';

        if (data.imported > 0 || data.updated > 0) loadLeads();
    } catch (e) {
        resultBox.innerHTML = `Import failed: ${esc(e.message)}`;
        resultBox.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.textContent = '⬆ Import';
    }
}

// ── Campaign Creation ────────────────────────────────────────────────────────

async function openCampaignModal() {
    document.getElementById('camp-leads-count').textContent = selectedLeadIds.size;
    document.getElementById('camp-name').value = '';
    document.getElementById('camp-round-robin').checked = true;
    toggleCampaignCredentialSelect();

    // Load templates
    try {
        const res = await fetch('/api/templates');
        const templates = await res.json();

        const select = document.getElementById('camp-template');
        select.innerHTML = '<option value="">-- Choose Template --</option>';
        templates.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = t.name;
            select.appendChild(opt);
        });
    } catch (e) {
        console.error("Failed to load templates", e);
    }

    loadCampaignCredentialOptions();

    document.getElementById('modal-create-campaign').style.display = 'flex';
}

function closeCampaignModal() {
    document.getElementById('modal-create-campaign').style.display = 'none';
}

function toggleCampaignCredentialSelect() {
    const isRoundRobin = document.getElementById('camp-round-robin').checked;
    document.getElementById('camp-credential-select-group').style.display = isRoundRobin ? 'none' : 'block';
}

async function loadCampaignCredentialOptions() {
    const tbody = document.getElementById('camp-credentials-list');
    try {
        const res = await fetch('/api/credentials');
        const creds = await res.json();

        if (creds.length === 0) {
            tbody.innerHTML = '<tr><td class="empty-state">No SMTP credentials configured yet.</td></tr>';
            return;
        }

        tbody.innerHTML = '';
        creds.forEach(c => {
            const limit = c.daily_send_limit ? `${c.sent_today}/${c.daily_send_limit} today` : `${c.sent_today} sent today`;
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td style="width:24px;"><input type="checkbox" class="camp-cred-checkbox" value="${c.id}"></td>
                <td>${c.username} (${c.host})</td>
                <td style="font-size:12px; color:var(--muted);">${limit}</td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        tbody.innerHTML = '<tr><td class="empty-state">Failed to load credentials.</td></tr>';
    }
}

async function submitCampaign() {
    const name = document.getElementById('camp-name').value.trim();
    const templateId = parseInt(document.getElementById('camp-template').value);

    if (!name || !templateId) {
        alert("Please provide a name and select a template.");
        return;
    }

    if (selectedLeadIds.size === 0) {
        alert("No leads selected.");
        return;
    }

    const btn = document.getElementById('btn-camp-submit');
    btn.disabled = true;
    btn.textContent = "Creating...";

    const isRoundRobin = document.getElementById('camp-round-robin').checked;
    const credentialIds = isRoundRobin
        ? []
        : Array.from(document.querySelectorAll('.camp-cred-checkbox:checked')).map(cb => parseInt(cb.value, 10));

    if (!isRoundRobin && credentialIds.length === 0) {
        alert("Select at least one SMTP credential, or use round robin.");
        btn.disabled = false;
        btn.textContent = "Generate Drafts";
        return;
    }

    try {
        const payload = {
            name: name,
            template_id: templateId,
            lead_ids: Array.from(selectedLeadIds),
            credential_ids: credentialIds
        };

        const res = await fetch('/api/campaigns', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });

        if (res.ok) {
            const data = await res.json();
            // Redirect to campaigns page
            window.location.href = `/campaigns?id=${data.campaign_id}`;
        } else {
            const err = await res.json();
            alert("Failed to create campaign: " + err.detail);
        }
    } catch (e) {
        alert("Network error.");
    } finally {
        btn.disabled = false;
        btn.textContent = "Generate Drafts";
    }
}


async function leadsPrev() {
    if (leadsPage > 1) {
        leadsPage--;
        await loadLeads();
    }
}

async function leadsNext() {
    if (leadsPage < leadsTotalPgs) {
        leadsPage++;
        await loadLeads();
    }
}


// ── Add to Existing Campaign ─────────────────────────────────────────────────

async function openAddToCampaignModal() {
    document.getElementById('atc-leads-count').textContent = selectedLeadIds.size.toLocaleString();
    document.getElementById('atc-warning').style.display = 'none';
    document.getElementById('btn-atc-submit').disabled = false;
    document.getElementById('btn-atc-submit').textContent = 'Add Leads';

    const select = document.getElementById('atc-campaign-select');
    select.innerHTML = '<option value="">Loading…</option>';
    document.getElementById('modal-add-to-campaign').style.display = 'flex';

    try {
        const data = await apiFetch('/api/campaigns?limit=100&include_test=false');
        const active = (data.campaigns || []).filter(c => c.status !== 'CANCELLED');

        select.innerHTML = '<option value="">-- Choose Campaign --</option>';
        if (active.length === 0) {
            select.innerHTML = '<option value="" disabled>No active campaigns found</option>';
            return;
        }
        active.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = `${c.name}  [${c.status}]`;
            // Pre-select if arriving from the campaign page via ?add_to_campaign=
            if (pendingAddToCampaignId && c.id === pendingAddToCampaignId) opt.selected = true;
            select.appendChild(opt);
        });
    } catch (e) {
        select.innerHTML = '<option value="" disabled>Failed to load campaigns</option>';
    }
}

function closeAddToCampaignModal() {
    document.getElementById('modal-add-to-campaign').style.display = 'none';
}

async function submitAddToCampaign() {
    const campaignId = document.getElementById('atc-campaign-select').value;
    if (!campaignId) {
        document.getElementById('atc-warning').textContent = 'Please select a campaign.';
        document.getElementById('atc-warning').style.display = 'block';
        return;
    }

    if (selectedLeadIds.size === 0) {
        document.getElementById('atc-warning').textContent = 'No leads selected.';
        document.getElementById('atc-warning').style.display = 'block';
        return;
    }

    const btn = document.getElementById('btn-atc-submit');
    btn.disabled = true;
    btn.textContent = 'Adding…';
    document.getElementById('atc-warning').style.display = 'none';

    try {
        const res = await fetch(`/api/campaigns/${campaignId}/emails`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({lead_ids: Array.from(selectedLeadIds)})
        });

        if (res.ok) {
            const data = await res.json();
            const skipped = (data.blacklisted_count || 0) + (data.already_in_campaign || 0);
            const msg = skipped > 0 ? `?notice=${encodeURIComponent(`${data.added} added, ${skipped} skipped (duplicates/blacklisted)`)}` : '';
            window.location.href = `/campaigns?id=${campaignId}${msg}`;
        } else {
            const err = await res.json();
            document.getElementById('atc-warning').textContent = err.detail || 'Failed to add leads.';
            document.getElementById('atc-warning').style.display = 'block';
            btn.disabled = false;
            btn.textContent = 'Add Leads';
        }
    } catch (e) {
        document.getElementById('atc-warning').textContent = 'Network error.';
        document.getElementById('atc-warning').style.display = 'block';
        btn.disabled = false;
        btn.textContent = 'Add Leads';
    }
}