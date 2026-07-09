// Cloud admin's Campaigns view — read-only oversight. No dispatch/pause/
// cancel, no credential editing, no draft editing/removal, no add-leads.
// Those are agent-only actions tied to the crawl/outreach workflow the
// agent owns; this page exists so an admin without an agent running can
// still see what's happening.

let currentCampaignId = null;
let cPage = 1;
let ePage = 1;
let pollInterval = null;
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
                    <div class="campaign-title">${tBadge}${esc(c.name)}</div>
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
        document.getElementById('cd-title').innerHTML = tBadge + esc(c.name);
        document.getElementById('cd-status').textContent = c.status;
        document.getElementById('cd-status').className = `status-badge status-${c.status}`;
        document.getElementById('cd-created').textContent = new Date(c.created_at + 'Z').toLocaleString();

        updateStatsUI(c.stats);
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
                loadEmails();
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

// ── Emails Tab (read-only) ───────────────────────────────────────────────────

async function loadEmails() {
    if (!currentCampaignId) return;

    try {
        const res = await fetch(`/api/campaigns/${currentCampaignId}/emails?page=${ePage}&limit=50`);
        const data = await res.json();

        const tbody = document.getElementById('emails-tbody');
        tbody.innerHTML = '';

        if (data.emails.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="empty-state">No emails found.</td></tr>`;
            return;
        }

        data.emails.forEach(e => {
            const isDraft = e.status === 'DRAFT';
            const isSent = e.status === 'SENT';

            // Missing-fields warning badge
            let missingBadge = '';
            if (e.missing_fields) {
                const fields = e.missing_fields.split(',').join(', ');
                missingBadge = `<span class="missing-badge" title="Missing data: ${esc(fields)}">⚠ ${esc(fields)}</span>`;
            }

            const err = e.error_message || '';
            const errCell = err
                ? `<span style="color:var(--red); font-size:12px;">${esc(err)}</span>`
                : missingBadge;

            let actions = '';
            if (isSent) {
                actions = `<button class="btn-secondary btn-sm" onclick="openViewEmailModal(${e.id}, '${esc(e.recipient_email)}', '${e.sent_at || ''}')">View</button>`;
            }

            let statusHtml = `<span class="email-status-badge status-email-${e.status}">${esc(e.status)}</span>`;
            if (isDraft && !e.is_selected) {
                statusHtml += ` <span class="skipped-badge">skipped</span>`;
            }

            const tr = document.createElement('tr');
            if (isDraft && e.missing_fields) tr.classList.add('row-missing');
            tr.innerHTML = `
                <td>${esc(e.recipient_email)}</td>
                <td style="font-size:12px;">${esc(e.subject)}</td>
                <td>${statusHtml}</td>
                <td>${errCell}</td>
                <td class="action-cell">${actions}</td>
            `;
            tbody.appendChild(tr);
        });

        document.getElementById('e-page-info').textContent = `${ePage} / ${Math.ceil(data.total / 50) || 1}`;
        document.getElementById('e-btn-prev').disabled = ePage === 1;
        document.getElementById('e-btn-next').disabled = ePage >= (data.total / 50);

    } catch (e) {
        console.error(e);
    }
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
