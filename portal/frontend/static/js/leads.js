let leadsPage = 1;
let leadsTotalPgs = 1;
let selectedLeadIds = new Set();
let leadsSearchTimer = null;
let leadsTotalMatching = 0;
let pendingAddToCampaignId = null;

document.addEventListener('DOMContentLoaded', async () => {
    const urlParams = new URLSearchParams(window.location.search);
    const addToCamp = urlParams.get('add_to_campaign');
    if (addToCamp) {
        pendingAddToCampaignId = parseInt(addToCamp);
        showAddToCampaignBanner();
    }

    setupLeadCellListeners();
    restoreLeadsSelection();
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
        job: document.getElementById('leads-job-select')?.value || '',
        cat: document.getElementById('leads-cat-select')?.value || '',
        state: document.getElementById('leads-state-select')?.value || '',
        search: document.getElementById('leads-search-input')?.value || '',
        completeOnly: document.getElementById('leads-complete-only')?.checked || false,
    }));
}

function clearLeadsFilters() {
    sessionStorage.removeItem('leads_filters');
    document.getElementById('leads-job-select').value = '';
    document.getElementById('leads-cat-select').value = '';
    document.getElementById('leads-state-select').value = '';
    document.getElementById('leads-search-input').value = '';
    document.getElementById('leads-complete-only').checked = false;
    leadsPage = 1;
    reloadLeadsStateOptions('').then(() => loadLeads());
}

async function loadLeadsFilters() {
    try {
        const saved = getLeadsFilters();
        const urlParams = new URLSearchParams(window.location.search);
        const urlJobId = urlParams.get('job_id') || '';

        // Populate Job dropdown
        const jobSel = document.getElementById('leads-job-select');
        try {
            const jobs = await apiFetch('/api/jobs?limit=100');
            jobs.forEach(j => {
                const o = document.createElement('option');
                o.value = j.id;
                const date = j.created_at ? j.created_at.slice(0, 10) : '';
                o.textContent = `Job #${j.id} — ${j.status}${date ? ' · ' + date : ''}`;
                if (String(j.id) === urlJobId) o.selected = true;
                jobSel.appendChild(o);
            });
        } catch (e) {
        }

        // URL param takes priority over session-saved filter
        if (!urlJobId && saved.job) jobSel.value = saved.job;
        if (saved.search) document.getElementById('leads-search-input').value = saved.search;
        if (saved.completeOnly) document.getElementById('leads-complete-only').checked = true;

        const jobId = jobSel.value;
        const jobIdParam = jobId ? `?job_id=${encodeURIComponent(jobId)}` : '';

        const cats = await apiFetch(`/api/leads/categories${jobIdParam}`);
        const catSel = document.getElementById('leads-cat-select');
        cats.forEach(c => {
            const o = document.createElement('option');
            o.value = c.code;
            o.textContent = `${c.title} (${c.count.toLocaleString()})`;
            catSel.appendChild(o);
        });
        if (saved.cat) catSel.value = saved.cat;

        await reloadLeadsStateOptions(catSel.value);
        if (saved.state) document.getElementById('leads-state-select').value = saved.state;
    } catch (e) {
    }
}

async function reloadLeadsStateOptions(cat) {
    const stateSel = document.getElementById('leads-state-select');
    if (!stateSel) return;
    const prev = stateSel.value;
    stateSel.innerHTML = '<option value="">All States</option>';
    try {
        const jobId = document.getElementById('leads-job-select').value;
        const params = new URLSearchParams();
        if (jobId) params.set('job_id', jobId);
        if (cat) params.set('category', cat);

        const qs = params.toString() ? `?${params.toString()}` : '';
        const states = await apiFetch(`/api/leads/states${qs}`);
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

async function onLeadsFilterChange() {
    leadsPage = 1;
    const cat = document.getElementById('leads-cat-select').value;
    await reloadLeadsStateOptions(cat);
    saveLeadsFilters();
    await loadLeads();
}

function debounceLeadsSearch() {
    clearTimeout(leadsSearchTimer);
    leadsSearchTimer = setTimeout(() => {
        leadsPage = 1;
        saveLeadsFilters();
        loadLeads();
    }, 380);
}

function getLeadsFilterParams() {
    const jobId = document.getElementById('leads-job-select').value;
    const cat = document.getElementById('leads-cat-select').value;
    const state = document.getElementById('leads-state-select').value;
    const search = document.getElementById('leads-search-input').value.trim();
    const completeOnly = document.getElementById('leads-complete-only').checked;
    const params = new URLSearchParams();
    if (jobId) params.set('job_id', jobId);
    if (cat) params.set('category', cat);
    if (state) params.set('state', state);
    if (search) params.set('search', search);
    if (completeOnly) params.set('complete_only', 'true');
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
        tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No leads found.</td></tr>';
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
            `<td style="text-align:center">${l.confidence_band ? `<span class="badge badge-${l.confidence_band === 'HIGH' ? 'success' : 'secondary'}">${esc(l.confidence_band)}</span>` : ''}</td>`,
            `<td>
                <a href="mailto:${esc(l.email)}" style="display:block;font-family:monospace;font-size:11px;color:var(--accent)">${esc(l.email)}<span style="font-size:9px;margin-left:2px;opacity:0.55">↗</span></a>
                ${l.phone
                ? `<a href="tel:${esc(l.phone)}" style="display:block;font-family:monospace;font-size:10px;color:var(--muted);margin-top:2px">📞 ${esc(l.phone)}</a>`
                : ''}
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
    const body = {
        job_id: params.get('job_id') ? parseInt(params.get('job_id')) : null,
        category: params.get('category') || null,
        state: params.get('state') || null,
        search: params.get('search') || null,
        complete_only: params.get('complete_only') === 'true',
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