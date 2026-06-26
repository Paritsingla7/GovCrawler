// campaigns.js

let currentCampaignId = null;
let cPage = 1;
let ePage = 1;
let pollInterval = null;
let currentDraftEditId = null;
let loadedCampaigns = [];
let currentCampaignIsTest = false;

async function loadCampaigns() {
    try {
        const toggleEl = document.getElementById('toggle-tests');
        const showTests = toggleEl ? toggleEl.checked : false;
        
        const res = await fetch(`/api/campaigns?page=${cPage}&limit=20&include_test=${showTests}`);
        const data = await res.json();
        
        const ul = document.getElementById('campaign-list');
        ul.innerHTML = '';
        
        if (data.campaigns.length === 0) {
            ul.innerHTML = '<li class="empty-state">No campaigns found.</li>';
            document.getElementById('c-page-info').textContent = '—';
            return;
        }
        
        loadedCampaigns = data.campaigns;
        
        data.campaigns.forEach(c => {
            const liId = c.is_test ? `test-${c.id}` : `camp-${c.id}`;
            const li = document.createElement('li');
            li.id = `camp-item-${liId}`;
            if (c.id === currentCampaignId && c.is_test === currentCampaignIsTest) li.classList.add('active');
            
            li.onclick = () => selectCampaign(c.id, c.is_test);
            
            const total = c.stats.total || 0;
            const sent = c.stats.sent || 0;
            const pct = total > 0 ? Math.round((sent / total) * 100) : 0;
            
            const sBadge = `<span class="status-badge status-${c.status}" style="font-size:10px; padding:2px 4px;">${c.status}</span>`;
            const tBadge = c.is_test ? `<span class="status-badge" style="background:#e0f2fe;color:#0369a1;font-size:10px;padding:2px 4px;margin-right:4px;">Test</span>` : '';
            
            li.innerHTML = `
                <div style="display:flex; justify-content:space-between;">
                    <div class="campaign-title">${tBadge}${c.name}</div>
                    ${sBadge}
                </div>
                <div class="campaign-stats-mini">
                    <span>${sent} / ${total} sent</span>
                    <span>${pct}%</span>
                </div>
            `;
            ul.appendChild(li);
        });
        
        document.getElementById('c-page-info').textContent = `${cPage} / ${Math.ceil(data.total/20) || 1}`;
        document.getElementById('c-btn-prev').disabled = cPage === 1;
        document.getElementById('c-btn-next').disabled = cPage >= (data.total/20);
        
    } catch (e) {
        console.error(e);
    }
}

function prevCampaignPage() { if (cPage > 1) { cPage--; loadCampaigns(); } }
function nextCampaignPage() { cPage++; loadCampaigns(); }

function toggleTestCampaigns() {
    cPage = 1;
    loadCampaigns();
}

function selectCampaign(id, isTest) {
    currentCampaignId = id;
    currentCampaignIsTest = isTest;
    ePage = 1;
    
    document.querySelectorAll('.campaign-list li').forEach(li => li.classList.remove('active'));
    const liId = isTest ? `test-${id}` : `camp-${id}`;
    const sel = document.getElementById(`camp-item-${liId}`);
    if (sel) sel.classList.add('active');
    
    document.getElementById('campaign-empty').style.display = 'none';
    document.getElementById('campaign-main').style.display = 'flex';
    
    loadCampaignDetail();
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(pollCampaignStats, 3000);
}

async function loadCampaignDetail() {
    if (!currentCampaignId) return;
    
    const apiPath = currentCampaignIsTest ? 'test-campaigns' : 'campaigns';
    
    try {
        const res = await fetch(`/api/${apiPath}/${currentCampaignId}`);
        const c = await res.json();
        
        const tBadge = currentCampaignIsTest ? `<span class="status-badge" style="background:#e0f2fe;color:#0369a1;font-size:12px;padding:2px 6px;margin-right:8px;vertical-align:middle;">Test</span>` : '';
        document.getElementById('cd-title').innerHTML = tBadge + c.name;
        document.getElementById('cd-status').textContent = c.status;
        document.getElementById('cd-status').className = `status-badge status-${c.status}`;
        document.getElementById('cd-created').textContent = new Date(c.created_at + 'Z').toLocaleString();
        
        updateStatsUI(c.stats);
        updateButtonsUI(c);
        
        loadEmails();
    } catch (e) {
        console.error(e);
    }
}

async function pollCampaignStats() {
    if (!currentCampaignId) return;
    const apiPath = currentCampaignIsTest ? 'test-campaigns' : 'campaigns';
    try {
        const res = await fetch(`/api/${apiPath}/${currentCampaignId}/stats`);
        if (res.ok) {
            const stats = await res.json();
            updateStatsUI(stats);
            if (stats.campaign_status) {
                document.getElementById('cd-status').textContent = stats.campaign_status;
                document.getElementById('cd-status').className = `status-badge status-${stats.campaign_status}`;
                updateButtonsUI({status: stats.campaign_status, stats: stats});
                
                // If the campaign is actively running, or we just need to keep the table fresh, reload emails
                // We do it only if the modal is not open to avoid any focus issues if we were to add inputs later
                if (document.getElementById('modal-edit-email').style.display === 'none') {
                    loadEmails();
                }
            }
        }
    } catch(e) {}
}

function updateStatsUI(stats) {
    document.getElementById('stat-total').textContent = stats.total || 0;
    document.getElementById('stat-draft').textContent = stats.draft || 0;
    document.getElementById('stat-queued').textContent = stats.queued || 0;
    document.getElementById('stat-sent').textContent = stats.sent || 0;
    document.getElementById('stat-failed').textContent = stats.failed || 0;
    
    // Update progress bar if running
    const wrap = document.getElementById('dispatch-progress-wrap');
    if (stats.queued > 0 || stats.sent > 0 || stats.failed > 0) {
        wrap.style.display = 'block';
        const total = stats.total || 1;
        const done = (stats.sent || 0) + (stats.failed || 0);
        const pct = Math.min(100, (done / total) * 100);
        document.getElementById('dispatch-progress-fill').style.width = `${pct}%`;
    } else {
        wrap.style.display = 'none';
    }
}

function updateButtonsUI(c) {
    const btnPause = document.getElementById('btn-pause');
    const btnDispatch = document.getElementById('btn-dispatch');
    const btnCancel = document.getElementById('btn-cancel');
    
    btnPause.style.display = 'none';
    btnDispatch.style.display = 'none';
    btnCancel.style.display = 'none';
    
    if (c.status === 'PAUSED') {
        if ((c.stats.draft || 0) > 0 || (c.stats.queued || 0) > 0) {
            btnDispatch.style.display = 'inline-block';
            btnCancel.style.display = 'inline-block';
        }
    } else if (c.status === 'RUNNING') {
        btnPause.style.display = 'inline-block';
        btnCancel.style.display = 'inline-block';
    }
}

// ── Emails Tab ───────────────────────────────────────────────────────────────

async function loadEmails() {
    if (!currentCampaignId) return;
    
    const apiPath = currentCampaignIsTest ? 'test-campaigns' : 'campaigns';
    
    try {
        const res = await fetch(`/api/${apiPath}/${currentCampaignId}/emails?page=${ePage}&limit=50`);
        const data = await res.json();
        
        const tbody = document.getElementById('emails-tbody');
        tbody.innerHTML = '';
        
        if (data.emails.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="empty-state">No emails found.</td></tr>`;
            return;
        }
        
        data.emails.forEach(e => {
            const err = e.error_message || '';
            const action = e.status === 'DRAFT' ? `<button class="btn-secondary btn-sm" onclick="openEditEmailModal(${e.id}, '${e.recipient_email}')">Edit Body</button>` : '';
            
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${e.recipient_email}</td>
                <td>${e.subject}</td>
                <td><span style="font-size:11px;font-weight:bold">${e.status}</span></td>
                <td style="color:var(--danger); font-size:12px;">${err}</td>
                <td>${action}</td>
            `;
            tbody.appendChild(tr);
        });
        
        document.getElementById('e-page-info').textContent = `${ePage} / ${Math.ceil(data.total/50) || 1}`;
        document.getElementById('e-btn-prev').disabled = ePage === 1;
        document.getElementById('e-btn-next').disabled = ePage >= (data.total/50);
        
    } catch (e) {
        console.error(e);
    }
}

function prevEmailPage() { if (ePage > 1) { ePage--; loadEmails(); } }
function nextEmailPage() { ePage++; loadEmails(); }


// ── Actions ──────────────────────────────────────────────────────────────────

function triggerDispatch() {
    const draftCount = document.getElementById('stat-draft').textContent;
    if (draftCount === '0') {
        alert("No drafts available to dispatch.");
        return;
    }
    
    document.getElementById('dispatch-draft-count').textContent = draftCount;
    document.getElementById('modal-dispatch').style.display = 'flex';
}

function closeDispatchModal() {
    document.getElementById('modal-dispatch').style.display = 'none';
}

async function confirmDispatch() {
    const btn = document.querySelector('#modal-dispatch .btn-primary');
    btn.disabled = true;
    btn.textContent = 'Dispatching...';
    
    const apiPath = currentCampaignIsTest ? 'test-campaigns' : 'campaigns';
    
    try {
        const res = await fetch(`/api/${apiPath}/${currentCampaignId}/dispatch`, { method: 'POST' });
        if (res.ok) {
            closeDispatchModal();
            loadCampaignDetail();
            loadCampaigns();
        } else {
            const err = await res.json();
            alert("Failed to start dispatch: " + err.detail);
        }
    } catch (e) {
        alert("Network error.");
    } finally {
        btn.disabled = false;
        btn.textContent = '▶ Yes, Dispatch';
    }
}

async function pauseCampaign() {
    const apiPath = currentCampaignIsTest ? 'test-campaigns' : 'campaigns';
    await fetch(`/api/${apiPath}/${currentCampaignId}`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'PAUSED'})
    });
    loadCampaignDetail();
    loadCampaigns();
}

async function cancelCampaign() {
    if (!confirm("Cancel campaign? Remaining drafts and queued emails will not be sent.")) return;
    const apiPath = currentCampaignIsTest ? 'test-campaigns' : 'campaigns';
    await fetch(`/api/${apiPath}/${currentCampaignId}`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'CANCELLED'})
    });
    loadCampaignDetail();
    loadCampaigns();
}

// ── Edit Email Modal ─────────────────────────────────────────────────────────

async function openEditEmailModal(emailId, recipient) {
    currentDraftEditId = emailId;
    document.getElementById('ee-recipient').textContent = recipient;
    document.getElementById('ee-body').value = "Loading...";
    document.getElementById('modal-edit-email').style.display = 'flex';
    
    // We don't have a GET /emails/{id} yet, but we can fetch it from the list data
    // To be safe, we'll implement a fast lookup or just rely on the API.
    // Wait, let's just fetch it by paginated list filtering or a dedicated endpoint?
    // Let's add GET /api/campaigns/{id}/emails/{eid} if it doesn't exist, or just use the current page's data.
    // Easiest is using current page data.
    
    const apiPath = currentCampaignIsTest ? 'test-campaigns' : 'campaigns';
    
    try {
        const res = await fetch(`/api/${apiPath}/${currentCampaignId}/emails?page=${ePage}&limit=50`);
        const data = await res.json();
        const draft = data.emails.find(e => e.id === emailId);
        if (draft) {
            document.getElementById('ee-subject').value = draft.subject || "";
            document.getElementById('ee-body').value = draft.body;
        } else {
            document.getElementById('ee-subject').value = "";
            document.getElementById('ee-body').value = "Could not load body text.";
        }
    } catch (e) {
        document.getElementById('ee-body').value = "Error loading body text.";
    }
}

function closeEditEmailModal() {
    document.getElementById('modal-edit-email').style.display = 'none';
}

async function saveEmailEdit() {
    const btn = document.querySelector('#modal-edit-email .btn-primary');
    btn.disabled = true;
    btn.textContent = "Saving...";
    
    const newSubject = document.getElementById('ee-subject').value;
    const newBody = document.getElementById('ee-body').value;
    const apiPath = currentCampaignIsTest ? 'test-campaigns' : 'campaigns';
    
    try {
        const res = await fetch(`/api/${apiPath}/${currentCampaignId}/emails/${currentDraftEditId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({subject: newSubject, body: newBody})
        });
        
        if (res.ok) {
            closeEditEmailModal();
            // Just refresh list
            loadEmails();
        } else {
            alert("Failed to update email body.");
        }
    } catch (e) {
        alert("Network error.");
    }
}
