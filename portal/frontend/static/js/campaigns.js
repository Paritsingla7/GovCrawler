// campaigns.js

let currentCampaignId = null;
let cPage = 1;
let ePage = 1;
let pollInterval = null;
let currentDraftEditId = null;
let loadedCampaigns = [];
let currentCampaignKind = 'production';
let currentCampaignStatus = null;

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
            const isTest = c.kind === 'test';
            const liId = isTest ? `test-${c.id}` : `camp-${c.id}`;
            const li = document.createElement('li');
            li.id = `camp-item-${liId}`;
            if (c.id === currentCampaignId && c.kind === currentCampaignKind) li.classList.add('active');

            li.onclick = () => selectCampaign(c.id, c.kind);

            const total = c.stats.total || 0;
            const sent = c.stats.sent || 0;
            const pct = total > 0 ? Math.round((sent / total) * 100) : 0;

            const sBadge = `<span class="status-badge status-${c.status}" style="font-size:10px; padding:2px 4px;">${c.status}</span>`;
            const tBadge = isTest ? `<span class="status-badge" style="background:#e0f2fe;color:#0369a1;font-size:10px;padding:2px 4px;margin-right:4px;">Test</span>` : '';

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

        document.getElementById('c-page-info').textContent = `${cPage} / ${Math.ceil(data.total / 20) || 1}`;
        document.getElementById('c-btn-prev').disabled = cPage === 1;
        document.getElementById('c-btn-next').disabled = cPage >= (data.total / 20);

    } catch (e) {
        console.error(e);
    }
}

function prevCampaignPage() {
    if (cPage > 1) {
        cPage--;
        loadCampaigns();
    }
}

function nextCampaignPage() {
    cPage++;
    loadCampaigns();
}

function toggleTestCampaigns() {
    cPage = 1;
    loadCampaigns();
}

function selectCampaign(id, kind) {
    currentCampaignId = id;
    currentCampaignKind = kind;
    ePage = 1;

    document.querySelectorAll('.campaign-list li').forEach(li => li.classList.remove('active'));
    const liId = kind === 'test' ? `test-${id}` : `camp-${id}`;
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

    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}`);
        const c = await res.json();

        currentCampaignStatus = c.status;
        currentCampaignKind = c.kind;

        const isTest = c.kind === 'test';
        const tBadge = isTest ? `<span class="status-badge" style="background:#e0f2fe;color:#0369a1;font-size:12px;padding:2px 6px;margin-right:8px;vertical-align:middle;">Test</span>` : '';
        document.getElementById('cd-title').innerHTML = tBadge + c.name;
        document.getElementById('cd-status').textContent = c.status;
        document.getElementById('cd-status').className = `status-badge status-${c.status}`;
        document.getElementById('cd-created').textContent = new Date(c.created_at + 'Z').toLocaleString();

        updateStatsUI(c.stats);
        updateButtonsUI(c);
        renderPauseReason(c.pause_reason);

        if (!isTest) {
            await renderCredentialsSummary(c.credential_ids || []);
        } else {
            document.getElementById('cd-credentials-row').style.display = 'none';
        }

        loadEmails();
    } catch (e) {
        console.error(e);
    }
}

function renderPauseReason(reason) {
    const row = document.getElementById('cd-pause-reason');
    if (reason) {
        document.getElementById('cd-pause-reason-text').textContent = reason;
        row.style.display = 'block';
    } else {
        row.style.display = 'none';
    }
}

async function renderCredentialsSummary(credentialIds) {
    const row = document.getElementById('cd-credentials-row');
    row.style.display = 'block';
    const summaryEl = document.getElementById('cd-credentials-summary');

    if (credentialIds.length === 0) {
        summaryEl.textContent = 'All active credentials (round robin)';
        return;
    }

    try {
        const res = await fetch('/api/credentials');
        const creds = await res.json();
        const names = credentialIds
            .map(id => creds.find(c => c.id === id))
            .filter(Boolean)
            .map(c => `${c.username} (${c.host})`);
        summaryEl.textContent = names.length ? names.join(', ') : `${credentialIds.length} credential(s)`;
    } catch (e) {
        summaryEl.textContent = `${credentialIds.length} credential(s)`;
    }
}

async function pollCampaignStats() {
    if (!currentCampaignId) return;
    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}/stats`);
        if (res.ok) {
            const stats = await res.json();
            updateStatsUI(stats);
            renderPauseReason(stats.pause_reason);
            if (stats.campaign_status) {
                currentCampaignStatus = stats.campaign_status;
                document.getElementById('cd-status').textContent = stats.campaign_status;
                document.getElementById('cd-status').className = `status-badge status-${stats.campaign_status}`;
                updateButtonsUI({status: stats.campaign_status, stats: stats});

                if (document.getElementById('modal-edit-email').style.display === 'none' &&
                    document.getElementById('modal-view-email').style.display === 'none') {
                    loadEmails();
                }
            }
        }
    } catch (e) {
    }
}

function updateStatsUI(stats) {
    document.getElementById('stat-total').textContent = stats.total || 0;
    document.getElementById('stat-draft').textContent = stats.draft || 0;
    document.getElementById('stat-skipped').textContent = stats.skipped || 0;
    document.getElementById('stat-queued').textContent = stats.queued || 0;
    document.getElementById('stat-sent').textContent = stats.sent || 0;
    document.getElementById('stat-failed').textContent = stats.failed || 0;

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
    const btnAddLeads = document.getElementById('btn-add-leads');
    const btnEditCredentials = document.getElementById('btn-edit-credentials');
    const toolbar = document.getElementById('email-toolbar');

    btnPause.style.display = 'none';
    btnDispatch.style.display = 'none';
    btnCancel.style.display = 'none';
    btnAddLeads.style.display = 'none';
    btnEditCredentials.style.display = 'none';
    toolbar.style.display = 'none';

    if (c.status === 'PAUSED') {
        const selectedDrafts = c.stats.draft || 0;
        const hasQueued = (c.stats.queued || 0) > 0;
        if (selectedDrafts > 0 || hasQueued) {
            btnDispatch.style.display = 'inline-block';
            btnCancel.style.display = 'inline-block';
        }
        toolbar.style.display = 'flex';
        if (currentCampaignKind !== 'test') btnEditCredentials.style.display = 'inline-block';
    } else if (c.status === 'RUNNING') {
        btnPause.style.display = 'inline-block';
        btnCancel.style.display = 'inline-block';
        if (currentCampaignKind !== 'test') btnEditCredentials.style.display = 'inline-block';
    }

    // "Add Leads" available whenever the campaign is not RUNNING or CANCELLED (prod only)
    if (currentCampaignKind !== 'test' && c.status !== 'RUNNING' && c.status !== 'CANCELLED') {
        btnAddLeads.style.display = 'inline-block';
    }
}

// ── Emails Tab ───────────────────────────────────────────────────────────────

async function loadEmails() {
    if (!currentCampaignId) return;


    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}/emails?page=${ePage}&limit=50`);
        const data = await res.json();

        const tbody = document.getElementById('emails-tbody');
        tbody.innerHTML = '';

        if (data.emails.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No emails found.</td></tr>`;
            updateToolbarInfo(0, 0);
            return;
        }

        let selectedCount = 0;
        let totalDraft = 0;

        data.emails.forEach(e => {
            const isDraft = e.status === 'DRAFT';
            const isQueued = e.status === 'QUEUED';
            const isPending = isDraft || isQueued;
            const isSent = e.status === 'SENT';
            const isCancelled = currentCampaignStatus === 'CANCELLED';

            if (isPending) totalDraft++;
            if (isPending && e.is_selected) selectedCount++;

            // Checkbox cell — DRAFT and QUEUED emails are both still deselectable.
            // Unchecking a QUEUED email pulls it back to DRAFT (excluded from dispatch).
            let checkboxCell = '<td></td>';
            if (isPending) {
                const checked = e.is_selected ? 'checked' : '';
                checkboxCell = `<td><input type="checkbox" class="email-select-cb" ${checked} onchange="toggleEmailSelection(${e.id}, this.checked)" title="Include in next dispatch"></td>`;
            }

            // Missing-fields warning badge
            let missingBadge = '';
            if (e.missing_fields) {
                const fields = e.missing_fields.split(',').join(', ');
                missingBadge = `<span class="missing-badge" title="Missing data: ${fields}">⚠ ${fields}</span>`;
            }

            // Error / note cell
            const err = e.error_message || '';
            const errCell = err
                ? `<span style="color:var(--danger); font-size:12px;">${err}</span>`
                : missingBadge;

            // Actions
            let actions = '';
            if (isDraft && !isCancelled) {
                actions = `<button class="btn-secondary btn-sm" onclick="openEditEmailModal(${e.id}, '${e.recipient_email}')">Edit</button>
                           <button class="btn-remove" onclick="confirmDeleteEmail(${e.id})" title="Remove from campaign">✕</button>`;
            } else if (isSent) {
                actions = `<button class="btn-secondary btn-sm" onclick="openViewEmailModal(${e.id}, '${e.recipient_email}', '${e.sent_at || ''}')">View</button>`;
            }

            // Status display — add deselected indicator
            let statusHtml = `<span class="email-status-badge status-email-${e.status}">${e.status}</span>`;
            if (isDraft && !e.is_selected) {
                statusHtml += ` <span class="skipped-badge">skipped</span>`;
            }

            const tr = document.createElement('tr');
            if (isDraft && e.missing_fields) tr.classList.add('row-missing');
            tr.innerHTML = `
                ${checkboxCell}
                <td>${e.recipient_email}</td>
                <td style="font-size:12px;">${e.subject}</td>
                <td>${statusHtml}</td>
                <td>${errCell}</td>
                <td class="action-cell">${actions}</td>
            `;
            tbody.appendChild(tr);
        });

        document.getElementById('e-page-info').textContent = `${ePage} / ${Math.ceil(data.total / 50) || 1}`;
        document.getElementById('e-btn-prev').disabled = ePage === 1;
        document.getElementById('e-btn-next').disabled = ePage >= (data.total / 50);
        updateToolbarInfo(selectedCount, totalDraft);

    } catch (e) {
        console.error(e);
    }
}

function updateToolbarInfo(selectedCount, totalDraft) {
    const el = document.getElementById('toolbar-selection-info');
    if (el) el.textContent = totalDraft > 0 ? `${selectedCount} of ${totalDraft} pending emails selected` : '';
}

function prevEmailPage() {
    if (ePage > 1) {
        ePage--;
        loadEmails();
    }
}

function nextEmailPage() {
    ePage++;
    loadEmails();
}


// ── Actions ──────────────────────────────────────────────────────────────────

function triggerDispatch() {
    const draftCount = parseInt(document.getElementById('stat-draft').textContent) || 0;
    const queuedCount = parseInt(document.getElementById('stat-queued').textContent) || 0;
    if (draftCount === 0 && queuedCount === 0) {
        alert("No selected draft or queued emails to dispatch. Select at least one email using the checkboxes.");
        return;
    }

    document.getElementById('dispatch-draft-count').textContent = draftCount + queuedCount;
    document.getElementById('modal-dispatch').style.display = 'flex';
}

function closeDispatchModal() {
    document.getElementById('modal-dispatch').style.display = 'none';
}

async function confirmDispatch() {
    const btn = document.querySelector('#modal-dispatch .btn-primary');
    btn.disabled = true;
    btn.textContent = 'Dispatching...';


    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}/dispatch`, {method: 'POST'});
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
    await fetch(`/api/campaigns/${currentCampaignId}`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'PAUSED'})
    });
    loadCampaignDetail();
    loadCampaigns();
}

async function cancelCampaign() {
    if (!confirm("Cancel campaign? Remaining queued emails will be marked failed. Drafts will be locked.")) return;
    await fetch(`/api/campaigns/${currentCampaignId}`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'CANCELLED'})
    });
    loadCampaignDetail();
    loadCampaigns();
}

// ── Checkbox / Selection ─────────────────────────────────────────────────────

async function toggleEmailSelection(emailId, isSelected) {
    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}/emails/${emailId}/selection`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({is_selected: isSelected})
        });
        if (!res.ok) {
            const err = await res.json();
            alert("Could not update selection: " + err.detail);
            loadEmails(); // revert UI
        } else {
            // Refresh stats without full reload
            pollCampaignStats();
        }
    } catch (e) {
        alert("Network error.");
        loadEmails();
    }
}

async function selectAllEmails(selected) {
    // Applies to every DRAFT email in the campaign, not just the current page
    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}/emails/selection-all`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({is_selected: selected})
        });
        if (res.ok) {
            loadEmails();
            pollCampaignStats();
        } else {
            const err = await res.json();
            alert("Could not update selection: " + err.detail);
        }
    } catch (e) {
        alert("Network error.");
    }
}

// ── Delete Email ─────────────────────────────────────────────────────────────

async function confirmDeleteEmail(emailId) {
    if (!confirm("Remove this email from the campaign? This cannot be undone.")) return;
    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}/emails/${emailId}`, {
            method: 'DELETE'
        });
        if (res.ok) {
            loadEmails();
            pollCampaignStats();
            loadCampaigns();
        } else {
            const err = await res.json();
            alert("Could not remove email: " + err.detail);
        }
    } catch (e) {
        alert("Network error.");
    }
}

// ── Edit Email Modal ─────────────────────────────────────────────────────────

async function openEditEmailModal(emailId, recipient) {
    currentDraftEditId = emailId;
    document.getElementById('ee-recipient').textContent = recipient;
    document.getElementById('ee-body').value = "Loading...";
    document.getElementById('modal-edit-email').style.display = 'flex';


    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}/emails?page=${ePage}&limit=50`);
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

    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}/emails/${currentDraftEditId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({subject: newSubject, body: newBody})
        });

        if (res.ok) {
            closeEditEmailModal();
            loadEmails();
        } else {
            const err = await res.json();
            alert("Failed to update email: " + (err.detail || "Unknown error"));
        }
    } catch (e) {
        alert("Network error.");
    } finally {
        btn.disabled = false;
        btn.textContent = "Save Override";
    }
}

// ── View Sent Email Modal ─────────────────────────────────────────────────────

async function openViewEmailModal(emailId, recipient, sentAt) {
    document.getElementById('ve-recipient').textContent = recipient;
    document.getElementById('ve-sent-at').textContent = sentAt ? `· sent ${new Date(sentAt + 'Z').toLocaleString()}` : '';
    document.getElementById('ve-subject').value = "Loading...";
    document.getElementById('ve-body').value = "Loading...";
    document.getElementById('modal-view-email').style.display = 'flex';


    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}/emails?page=${ePage}&limit=50`);
        const data = await res.json();
        const email = data.emails.find(e => e.id === emailId);
        if (email) {
            document.getElementById('ve-subject').value = email.subject || "";
            document.getElementById('ve-body').value = email.body || "";
        } else {
            document.getElementById('ve-subject').value = "";
            document.getElementById('ve-body').value = "Could not load body text.";
        }
    } catch (e) {
        document.getElementById('ve-body').value = "Error loading body text.";
    }
}

function closeViewEmailModal() {
    document.getElementById('modal-view-email').style.display = 'none';
}

// ── Add Leads — redirect to leads page ───────────────────────────────────────

function openAddLeadsModal() {
    if (!currentCampaignId) return;
    window.location.href = `/leads?add_to_campaign=${currentCampaignId}`;
}

function closeAddLeadsModal() {
}

async function confirmAddLeads() {
}

// ── Edit Campaign Credentials Modal ──────────────────────────────────────────

function toggleEditCredentialSelect() {
    const isRoundRobin = document.getElementById('ecc-round-robin').checked;
    document.getElementById('ecc-credential-select-group').style.display = isRoundRobin ? 'none' : 'block';
}

async function openEditCampaignCredentialsModal() {
    if (!currentCampaignId) return;

    let assignedIds = [];
    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}`);
        const c = await res.json();
        assignedIds = c.credential_ids || [];
    } catch (e) {
        alert("Failed to load current credential assignment.");
        return;
    }

    document.getElementById('ecc-round-robin').checked = assignedIds.length === 0;
    toggleEditCredentialSelect();

    const tbody = document.getElementById('ecc-credentials-list');
    try {
        const res = await fetch('/api/credentials');
        const creds = await res.json();

        if (creds.length === 0) {
            tbody.innerHTML = '<tr><td class="empty-state">No SMTP credentials configured yet.</td></tr>';
        } else {
            tbody.innerHTML = '';
            creds.forEach(c => {
                const checked = assignedIds.includes(c.id) ? 'checked' : '';
                const limit = c.daily_send_limit ? `${c.sent_today}/${c.daily_send_limit} today` : `${c.sent_today} sent today`;
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td style="width:24px;"><input type="checkbox" class="ecc-cred-checkbox" value="${c.id}" ${checked}></td>
                    <td>${c.username} (${c.host})</td>
                    <td style="font-size:12px; color:var(--muted);">${limit}</td>
                `;
                tbody.appendChild(tr);
            });
        }
    } catch (e) {
        tbody.innerHTML = '<tr><td class="empty-state">Failed to load credentials.</td></tr>';
    }

    document.getElementById('modal-campaign-credentials').style.display = 'flex';
}

function closeEditCampaignCredentialsModal() {
    document.getElementById('modal-campaign-credentials').style.display = 'none';
}

async function saveCampaignCredentials() {
    const isRoundRobin = document.getElementById('ecc-round-robin').checked;
    const credentialIds = isRoundRobin
        ? []
        : Array.from(document.querySelectorAll('.ecc-cred-checkbox:checked')).map(cb => parseInt(cb.value, 10));

    if (!isRoundRobin && credentialIds.length === 0) {
        alert("Select at least one SMTP credential, or use round robin.");
        return;
    }

    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}/credentials`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({credential_ids: credentialIds})
        });
        if (res.ok) {
            closeEditCampaignCredentialsModal();
            loadCampaignDetail();
        } else {
            const err = await res.json();
            alert("Failed to update credentials: " + err.detail);
        }
    } catch (e) {
        alert("Network error.");
    }
}
