let leadsPage = 1;
let leadsTotalPgs = 1;
let selectedLeadIds = new Set();
let leadsSearchTimer = null;
let leadsTotalMatching = 0;

document.addEventListener('DOMContentLoaded', () => {
    loadLeadsFilters();
    loadLeads();
});

async function loadLeadsFilters() {
    try {
        // If we're viewing a specific job, we could pass it here, but typically we view global leads on this page
        const urlParams = new URLSearchParams(window.location.search);
        const jobId = urlParams.get('job_id') || '';
        const jobIdParam = jobId ? `?job_id=${encodeURIComponent(jobId)}` : '';

        const cats = await apiFetch(`/api/leads/categories${jobIdParam}`);
        const catSel = document.getElementById('leads-cat-select');
        cats.forEach(c => {
            const o = document.createElement('option');
            o.value = c.code;
            o.textContent = `${c.title} (${c.count.toLocaleString()})`;
            catSel.appendChild(o);
        });
        await reloadLeadsStateOptions('');
    } catch (e) {
    }
}

async function reloadLeadsStateOptions(cat) {
    const stateSel = document.getElementById('leads-state-select');
    if (!stateSel) return;
    const prev = stateSel.value;
    stateSel.innerHTML = '<option value="">All States</option>';
    try {
        const urlParams = new URLSearchParams(window.location.search);
        const jobId = urlParams.get('job_id') || '';
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
    loadLeads();
}

function debounceLeadsSearch() {
    clearTimeout(leadsSearchTimer);
    leadsSearchTimer = setTimeout(() => {
        leadsPage = 1;
        loadLeads();
    }, 380);
}

async function loadLeads() {
    try {
        const cat = document.getElementById('leads-cat-select').value;
        const state = document.getElementById('leads-state-select').value;
        const search = document.getElementById('leads-search-input').value.trim();

        const params = new URLSearchParams({page: leadsPage, limit: 100});
        if (cat) params.set('category', cat);
        if (state) params.set('state', state);
        if (search) params.set('search', search);

        const url = `/api/leads?${params}`;
        const data = await apiFetch(url);
        leadsTotalPgs = data.pages;
        leadsTotalMatching = data.total;
        renderLeads(data.leads, data.total);
    } catch (e) {
        console.error('loadLeads', e);
    }
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
    leads.forEach(l => {
        const catCode = (l.category_code || 'default').toLowerCase();
        const checked = selectedLeadIds.has(l.id);
        const tr = document.createElement('tr');
        tr.innerHTML = `
      <td style="width:40px"><input type="checkbox" ${checked ? 'checked' : ''} onchange="toggleLead(${l.id}, this.checked)"></td>
      <td><a href="mailto:${esc(l.email)}" style="font-family:monospace;font-size:11px;color:var(--accent)">${esc(l.email || '—')}</a></td>
      <td>${esc(l.person_name || '—')}</td>
      <td style="font-size:12px;color:var(--muted)">${esc(l.designation || '—')}</td>
      <td style="font-size:12px;color:var(--muted)">${esc(l.department || '—')}</td>
      <td style="font-size:12px">${esc(l.domain_state || '—')}</td>
      <td style="font-size:12px">${esc(l.domain_title || '—')}</td>
      <td><span class="tag tag-${catCode}">${catCode.toUpperCase()}</span></td>
      <td style="font-size:11px;color:var(--muted);max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(l.source_title || '')}">${esc(l.source_title || '—')}</td>
      <td>${l.source_url ? `<a href="${esc(l.source_url)}" target="_blank" title="${esc(l.source_url)}">↗</a>` : '—'}</td>`;
        tbody.appendChild(tr);
    });
    updateSelectAllLeads();
    updateSelLeadsCount();
}

function toggleLead(id, checked) {
    if (checked) selectedLeadIds.add(id); else selectedLeadIds.delete(id);
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
    updateSelLeadsCount();
}

async function selectAllLeads() {
    const cat = document.getElementById('leads-cat-select').value;
    const state = document.getElementById('leads-state-select').value;
    const search = document.getElementById('leads-search-input').value.trim();
    const params = new URLSearchParams();
    if (cat) params.set('category', cat);
    if (state) params.set('state', state);
    if (search) params.set('search', search);
    try {
        const data = await apiFetch(`/api/leads/ids?${params}`);
        data.ids.forEach(id => selectedLeadIds.add(id));
        document.querySelectorAll('#leads-tbody input[type=checkbox]').forEach(cb => cb.checked = true);
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
    updateSelLeadsCount();
}

function updateSelLeadsCount() {
    const n = selectedLeadIds.size;
    document.getElementById('sel-leads-count').textContent = n.toLocaleString();
    const show = n > 0 ? 'inline-block' : 'none';
    document.getElementById('sel-leads-count-label').style.display = show;
}

function updateSelectAllLeads() {
    const all = document.querySelectorAll('#leads-tbody input[type=checkbox]');
    const n = Array.from(all).filter(cb => cb.checked).length;
    document.getElementById('leads-select-all').checked = all.length > 0 && n === all.length;
    document.getElementById('leads-select-all').indeterminate = n > 0 && n < all.length;
}

async function exportLeads() {
    const cat = document.getElementById('leads-cat-select').value || null;
    const state = document.getElementById('leads-state-select').value || null;
    const search = document.getElementById('leads-search-input').value.trim() || null;

    const body = {category: cat, state: state, search: search};
    if (selectedLeadIds.size > 0) {
        body.lead_ids = [...selectedLeadIds];
    }
    
    // Find the button to show loading state
    const btns = document.querySelectorAll('button[onclick="exportLeads()"]');
    const originalTexts = [];
    btns.forEach((btn, i) => {
        originalTexts[i] = btn.textContent;
        btn.textContent = "Exporting...";
    });

    try {
        const resp = await fetch('/api/leads/export', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        
        if (!resp.ok) {
            if (resp.status === 404) {
                alert("No leads matched your current filters to export.");
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
        if (blob.size === 0) {
            alert("No leads matched your current filters to export.");
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
    } catch (e) {
        alert("Export failed: " + e.message);
    } finally {
        btns.forEach((btn, i) => {
            btn.textContent = originalTexts[i];
        });
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