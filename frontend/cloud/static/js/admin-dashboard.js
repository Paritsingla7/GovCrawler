const ADMIN_DASHBOARD_POLL_MS = 3000;
let _adminDashboardTimer = null;

// ── Tab switching (same generic pattern settings.js uses) ───────────────────
function switchAdminTab(tabId) {
    document.querySelectorAll('.settings-nav li').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.settings-tab').forEach(el => el.style.display = 'none');
    document.getElementById(`nav-${tabId}`)?.classList.add('active');
    const tab = document.getElementById(`tab-${tabId}`);
    if (tab) tab.style.display = 'block';
}

function startAdminDashboardPoll() {
    loadAdminActivity();
    _adminDashboardTimer = setInterval(loadAdminActivity, ADMIN_DASHBOARD_POLL_MS);
}

async function loadAdminActivity() {
    let activity;
    try {
        activity = await apiFetch('/api/admin/activity');
    } catch (e) {
        return;
    }

    renderJobCards('ad-active-jobs', activity.crawl_jobs, jobCardHtml);
    renderJobCards('ad-active-campaigns', activity.campaigns, campaignCardHtml);
    renderJobCards('ad-recent-jobs', activity.recent_jobs, recentJobCardHtml);
    renderJobCards('ad-recent-campaigns', activity.recent_campaigns, recentCampaignCardHtml);

    document.getElementById('ad-last-updated').textContent =
        'Updated ' + new Date().toLocaleTimeString();
}

function renderJobCards(containerId, items, cardFn) {
    const el = document.getElementById(containerId);
    if (!items || !items.length) {
        el.innerHTML = '<div class="empty-state">None</div>';
        return;
    }
    el.innerHTML = items.map(cardFn).join('');
}

function jobCardHtml(job) {
    return `<div class="admin-card"><h4>${esc(job.label)}</h4></div>`;
}

function campaignCardHtml(c) {
    const stats = c.stats || {};
    return `<div class="admin-card">
        <h4>${esc(c.name)}</h4>
        <div class="admin-card-meta">Sent ${stats.sent || 0} / ${stats.total || 0} · Queued ${stats.queued || 0} · Failed ${stats.failed || 0}</div>
    </div>`;
}

function recentJobCardHtml(job) {
    return `<div class="admin-card">
        <h4>Job #${job.id}</h4>
        <div class="admin-card-meta">${esc(job.status)} · ${job.leads_found} leads · ${job.crawled_domains}/${job.total_domains} domains</div>
    </div>`;
}

function recentCampaignCardHtml(c) {
    return `<div class="admin-card">
        <h4>${esc(c.name)}</h4>
        <div class="admin-card-meta">${esc(c.status)}</div>
    </div>`;
}

// ── Users & permissions ──────────────────────────────────────────────────────

let _permissionsCatalog = null;

async function loadUsers() {
    let users;
    try {
        users = await apiFetch('/api/admin/users');
    } catch (e) {
        return;
    }
    const tbody = document.getElementById('ad-users-tbody');
    if (!tbody) return;
    tbody.innerHTML = users.map(userRowHtml).join('') || '<tr><td colspan="6">No users</td></tr>';
}

function userRowHtml(u) {
    const statusBadge = u.is_active
        ? '<span class="badge badge-green">Active</span>'
        : '<span class="badge badge-muted">Disabled</span>';
    const adminBadge = u.is_admin ? '<span class="badge badge-green">Super Admin</span>' : '—';
    return `<tr>
        <td>${esc(u.email)}</td>
        <td>${esc(u.full_name || '')}</td>
        <td>
            <select onchange="changeUserRole(${u.id}, this.value)" style="font-size:12px;">
                <option value="">— none —</option>
                ${['Admin', 'Operator', 'Viewer'].map(r =>
                    `<option value="${r}" ${u.role === r ? 'selected' : ''}>${r}</option>`).join('')}
            </select>
        </td>
        <td style="text-align:center">
            <label style="display:inline-flex; align-items:center; gap:6px; cursor:pointer;">
                <input onchange="toggleUserActive(${u.id}, this.checked)" type="checkbox" ${u.is_active ? 'checked' : ''}>
                ${statusBadge}
            </label>
        </td>
        <td style="text-align:center">${adminBadge}</td>
        <td style="white-space:nowrap;">
            <button class="btn-secondary btn-sm" onclick="openPermissionsModal(${u.id}, '${esc(u.email)}')">Permissions</button>
            <button class="btn-secondary btn-sm" onclick="resetUserPassword(${u.id})">Reset PW</button>
        </td>
    </tr>`;
}

function openNewUserModal() {
    document.getElementById('nu-email').value = '';
    document.getElementById('nu-password').value = '';
    document.getElementById('nu-full-name').value = '';
    document.getElementById('nu-role').value = '';
    document.getElementById('nu-is-admin').checked = false;
    document.getElementById('modal-new-user').style.display = 'flex';
}

function closeNewUserModal() {
    document.getElementById('modal-new-user').style.display = 'none';
}

async function submitNewUser() {
    const email = document.getElementById('nu-email').value.trim();
    const password = document.getElementById('nu-password').value;
    if (!email || !password) {
        showToast('Email and password are required.', {type: 'warning'});
        return;
    }
    try {
        await apiFetch('/api/admin/users', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                email, password,
                full_name: document.getElementById('nu-full-name').value.trim() || null,
                role: document.getElementById('nu-role').value || null,
                is_admin: document.getElementById('nu-is-admin').checked,
            }),
        });
        closeNewUserModal();
        showToast('User created.', {type: 'success'});
        loadUsers();
    } catch (e) {
        showApiError(e);
    }
}

async function toggleUserActive(userId, isActive) {
    try {
        await apiFetch(`/api/admin/users/${userId}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({is_active: isActive}),
        });
    } catch (e) {
        showApiError(e);
        loadUsers();
    }
}

async function changeUserRole(userId, role) {
    try {
        await apiFetch(`/api/admin/users/${userId}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({role: role || null}),
        });
    } catch (e) {
        showApiError(e);
        loadUsers();
    }
}

async function resetUserPassword(userId) {
    const password = prompt('New password for this user:');
    if (!password) return;
    try {
        await apiFetch(`/api/admin/users/${userId}/reset-password`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({password}),
        });
        showToast('Password reset.', {type: 'success'});
    } catch (e) {
        showApiError(e);
    }
}

async function openPermissionsModal(userId, email) {
    document.getElementById('perm-modal-email').textContent = email;
    const grid = document.getElementById('perm-modal-grid');
    grid.innerHTML = '<div class="empty-state">Loading...</div>';
    document.getElementById('modal-permissions').style.display = 'flex';

    if (!_permissionsCatalog) {
        _permissionsCatalog = await apiFetch('/api/admin/permissions');
    }
    const detail = await apiFetch(`/api/admin/users/${userId}`);
    const overrideByKey = {};
    (detail.permission_overrides || []).forEach(o => overrideByKey[o.permission_key] = o.effect);

    grid.innerHTML = Object.keys(_permissionsCatalog).sort().map(key => {
        const current = overrideByKey[key] || 'inherit';
        return `<div style="display:flex; align-items:center; justify-content:space-between; gap:12px; border-bottom:1px solid var(--border, #30363d); padding:6px 0;">
            <div>
                <div style="font-size:13px;">${esc(key)}</div>
                <div style="font-size:11px; color:var(--muted);">${esc(_permissionsCatalog[key])}</div>
            </div>
            <select onchange="setPermissionOverride(${userId}, '${key}', this.value)" style="font-size:12px;">
                <option value="inherit" ${current === 'inherit' ? 'selected' : ''}>Inherited from role</option>
                <option value="grant" ${current === 'grant' ? 'selected' : ''}>Granted</option>
                <option value="deny" ${current === 'deny' ? 'selected' : ''}>Denied</option>
            </select>
        </div>`;
    }).join('');
}

function closePermissionsModal() {
    document.getElementById('modal-permissions').style.display = 'none';
}

async function setPermissionOverride(userId, permissionKey, value) {
    const effect = value === 'inherit' ? null : value;
    try {
        await apiFetch(`/api/admin/users/${userId}/permissions/${permissionKey}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({effect}),
        });
    } catch (e) {
        showApiError(e);
    }
}

// ── Audit log ─────────────────────────────────────────────────────────────────

let _auditPage = 1;
let _auditTotalPages = 1;

function clearAuditFilters() {
    document.getElementById('audit-filter-action').value = '';
    document.getElementById('audit-filter-from').value = '';
    document.getElementById('audit-filter-to').value = '';
    loadAuditLog(1);
}

function auditPrev() {
    if (_auditPage > 1) loadAuditLog(_auditPage - 1);
}

function auditNext() {
    if (_auditPage < _auditTotalPages) loadAuditLog(_auditPage + 1);
}

async function loadAuditLog(page) {
    const params = new URLSearchParams({page: String(page), limit: '50'});
    const actionPrefix = document.getElementById('audit-filter-action').value.trim();
    const dateFrom = document.getElementById('audit-filter-from').value;
    const dateTo = document.getElementById('audit-filter-to').value;
    if (actionPrefix) params.set('action_prefix', actionPrefix);
    if (dateFrom) params.set('date_from', dateFrom);
    if (dateTo) params.set('date_to', dateTo);

    let data;
    try {
        data = await apiFetch(`/api/admin/audit?${params.toString()}`);
    } catch (e) {
        return;
    }
    _auditPage = data.page;
    _auditTotalPages = data.pages;

    const tbody = document.getElementById('audit-tbody');
    tbody.innerHTML = data.entries.map(auditRowHtml).join('') || '<tr><td colspan="6">No entries</td></tr>';
    document.getElementById('audit-page-info').textContent = `Page ${data.page} of ${data.pages} (${data.total} total)`;
    document.getElementById('btn-audit-prev').disabled = data.page <= 1;
    document.getElementById('btn-audit-next').disabled = data.page >= data.pages;
}

function auditRowHtml(e) {
    const target = e.target_type ? `${esc(e.target_type)}#${esc(e.target_id)}` : '';
    const detail = e.detail ? esc(JSON.stringify(e.detail)) : '';
    return `<tr>
        <td style="white-space:nowrap;font-size:11px;">${esc(new Date(e.created_at).toLocaleString())}</td>
        <td>${esc(e.user_email || 'system')}</td>
        <td>${esc(e.action)}</td>
        <td>${target}</td>
        <td style="font-size:11px;max-width:280px;overflow-wrap:anywhere;">${detail}</td>
        <td style="font-size:11px;">${esc(e.ip || '')}</td>
    </tr>`;
}

// ── Roles (read-only — built-in roles have no create/edit backend) ─────────

async function loadRoles() {
    const grid = document.getElementById('ad-roles-grid');
    let roles, permissions;
    try {
        [roles, permissions] = await Promise.all([
            apiFetch('/api/admin/roles'),
            apiFetch('/api/admin/permissions'),
        ]);
    } catch (e) {
        grid.innerHTML = '<div class="empty-state">Failed to load roles.</div>';
        return;
    }

    const permKeys = Object.keys(permissions).sort();
    const roleNames = roles.map(r => r.name);

    let html = `<div class="role-grid" style="grid-template-columns: 1fr repeat(${roleNames.length}, 110px);">`;
    html += `<div class="role-grid-header">Permission</div>`;
    roleNames.forEach(name => html += `<div class="role-grid-header">${esc(name)}</div>`);

    permKeys.forEach(key => {
        html += `<div class="role-grid-cell" title="${esc(permissions[key])}">${esc(key)}</div>`;
        roles.forEach(role => {
            const granted = (role.permissions || []).includes(key);
            html += `<div class="role-grid-cell">${granted ? '<span class="role-grid-check">✓</span>' : '—'}</div>`;
        });
    });
    html += '</div>';
    grid.innerHTML = html;
}

// ── System / health ──────────────────────────────────────────────────────────

async function loadSystemStatus() {
    const statusEl = document.getElementById('ad-system-status');
    const agentsBody = document.getElementById('ad-agents-tbody');
    let status;
    try {
        status = await apiFetch('/api/admin/system-status');
    } catch (e) {
        statusEl.innerHTML = '<div class="empty-state">Failed to load system status.</div>';
        return;
    }

    const dbOk = status.db_status === 'ok';
    statusEl.innerHTML = `
        <div class="health-stat">
            <div class="health-stat-label">Database</div>
            <div class="health-stat-value">
                <span class="dot ${dbOk ? 'dot-green' : 'dot-red'}"></span>
                ${dbOk ? 'Connected' : 'Unreachable'}
            </div>
        </div>
        <div class="health-stat">
            <div class="health-stat-label">Dispatch mode</div>
            <div class="health-stat-value">${esc(status.dispatch_mode || 'unknown')}</div>
        </div>
        <div class="health-stat">
            <div class="health-stat-label">Agents seen recently</div>
            <div class="health-stat-value">${(status.agents || []).length}</div>
        </div>
    `;

    agentsBody.innerHTML = (status.agents || []).map(a => `
        <tr>
            <td style="font-family:monospace; font-size:11px;">${esc(a.agent_id)}</td>
            <td>${a.job_count}</td>
            <td style="font-size:11px;">${a.last_job_at ? esc(new Date(a.last_job_at + 'Z').toLocaleString()) : '—'}</td>
        </tr>
    `).join('') || '<tr><td colspan="3" class="empty-state">No agents have run a job yet.</td></tr>';

    document.getElementById('sys-last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
}
