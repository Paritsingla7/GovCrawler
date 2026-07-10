// settings.js

let editTemplateId = null;

function switchTab(tabId) {
    document.querySelectorAll('.settings-nav li').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.settings-tab').forEach(el => el.style.display = 'none');

    document.getElementById(`nav-${tabId}`).classList.add('active');
    document.getElementById(`tab-${tabId}`).style.display = 'block';
}

// ── Credentials ──────────────────────────────────────────────────────────────

const OAUTH_PROVIDER_DEFAULTS = {
    microsoft: {host: 'smtp.office365.com', port: 587, label: 'Microsoft'},
    google: {host: 'smtp.gmail.com', port: 587, label: 'Google'},
};

async function loadCredentials() {
    try {
        const res = await fetch('/api/credentials');
        const creds = await res.json();

        const tbody = document.getElementById('credentials-tbody');
        tbody.innerHTML = '';

        if (creds.length === 0) {
            tbody.innerHTML = `<tr><td colspan="12" class="empty-state">No credentials configured.</td></tr>`;
            return;
        }

        creds.forEach(c => {
            const status = c.is_active ? '<span style="color:var(--success)">Active</span>' : '<span style="color:var(--danger)">Disabled</span>';
            let cooldown = '—';
            if (c.cooldown_until) {
                const cdDate = new Date(c.cooldown_until + 'Z');
                if (cdDate > new Date()) {
                    cooldown = `<span style="color:var(--warning)">Cooldown til ${cdDate.toLocaleTimeString()}</span>`;
                }
            }

            const isOAuth = c.provider !== 'basic';
            const providerLabel = isOAuth ? (OAUTH_PROVIDER_DEFAULTS[c.provider]?.label ?? c.provider) : 'Basic';
            const oauthBadge = isOAuth
                ? (c.oauth_connected
                    ? '<span class="badge badge-green">Connected</span>'
                    : '<span class="badge badge-muted">Not connected</span>')
                : '';
            const connectBtn = isOAuth
                ? `<button class="btn-secondary btn-sm" onclick="connectCredentialOAuth(${c.id}, '${c.provider}')">Connect</button>`
                : '';

            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><input type="checkbox" class="cred-checkbox" value="${c.id}" onchange="updateBatchActionVisibility('cred-checkbox', 'batch-action-cred')"></td>
                <td>${c.host}</td>
                <td>${c.port}</td>
                <td>${c.username}</td>
                <td>${providerLabel} ${oauthBadge}</td>
                <td>${status}</td>
                <td>${cooldown}</td>
                <td><input type="number" min="1" class="cred-limit-input" value="${c.daily_send_limit ?? ''}"
                           placeholder="Unlimited" style="width:80px"
                           onchange="updateCredentialLimit(${c.id}, this.value)"></td>
                <td>${c.sent_today}</td>
                <td>${c.sent_total}</td>
                <td>${c.failed_total}</td>
                <td>
                    ${connectBtn}
                    <button class="btn-secondary btn-sm" onclick="testCredential(${c.id}, this)">Test</button>
                    <button class="btn-secondary btn-sm" onclick="deleteCredential(${c.id})" style="color:var(--danger)">Del</button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error("Failed to load credentials", e);
    }
}

function onCredentialProviderChange() {
    const provider = document.getElementById('cred-provider').value;
    const isOAuth = provider !== 'basic';
    document.getElementById('cred-pass-group').style.display = isOAuth ? 'none' : '';
    document.getElementById('cred-oauth-group').style.display = isOAuth ? '' : 'none';
    if (isOAuth) {
        const defaults = OAUTH_PROVIDER_DEFAULTS[provider];
        document.getElementById('cred-host').value = defaults.host;
        document.getElementById('cred-port').value = defaults.port;
    }
}

function openCredentialModal() {
    document.getElementById('cred-provider').value = 'basic';
    document.getElementById('cred-host').value = '';
    document.getElementById('cred-port').value = '';
    document.getElementById('cred-user').value = '';
    document.getElementById('cred-pass').value = '';
    document.getElementById('cred-daily-limit').value = '';
    onCredentialProviderChange();
    document.getElementById('modal-credential').style.display = 'flex';
}

function closeCredentialModal() {
    document.getElementById('modal-credential').style.display = 'none';
}

async function updateCredentialLimit(id, value) {
    const limit = parseInt(value, 10);
    if (!limit || limit < 1) return;
    try {
        await fetch(`/api/credentials/${id}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({daily_send_limit: limit})
        });
    } catch (e) {
        console.error("Failed to update daily limit", e);
    }
}

async function saveCredential() {
    const provider = document.getElementById('cred-provider').value;
    const dailyLimit = parseInt(document.getElementById('cred-daily-limit').value, 10);
    const payload = {
        host: document.getElementById('cred-host').value,
        port: parseInt(document.getElementById('cred-port').value) || 0,
        username: document.getElementById('cred-user').value,
        provider: provider,
        password: provider === 'basic' ? document.getElementById('cred-pass').value : null,
        daily_send_limit: Number.isNaN(dailyLimit) ? null : dailyLimit
    };

    try {
        const res = await fetch('/api/credentials', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast('Failed to save credential: ' + (err.detail || 'unknown error'), {type: 'error'});
            return;
        }

        const data = await res.json();
        const newId = data.id;

        if (provider !== 'basic') {
            showToast('Credential saved — click "Connect" in the table to sign in.', {type: 'success'});
            closeCredentialModal();
            loadCredentials();
            return;
        }

        // Basic auth: auto-test on save.
        const saveBtn = document.querySelector('#modal-credential .btn-primary');
        let origText = "Save";
        if (saveBtn) {
            origText = saveBtn.textContent;
            saveBtn.textContent = "Testing...";
            saveBtn.disabled = true;
        }

        try {
            const testRes = await fetch(`/api/credentials/${newId}/test`, {method: 'POST'});
            const testData = await testRes.json();

            if (testData.success) {
                showToast('Credential saved and connection test successful.', {type: 'success'});
            } else {
                showToast('Credential saved, but the connection test failed: ' + testData.error, {type: 'warning'});
            }
        } catch (e) {
            showToast('Credential saved, but an error occurred while testing it.', {type: 'warning'});
        } finally {
            if (saveBtn) {
                saveBtn.textContent = origText;
                saveBtn.disabled = false;
            }
            closeCredentialModal();
            loadCredentials();
        }
    } catch (e) {
        console.error(e);
        showToast("Can't reach the server — check your connection.", {type: 'error'});
    }
}

async function connectCredentialOAuth(id, provider) {
    try {
        const res = await fetch(`/api/credentials/${id}/oauth/start`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({provider})
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.authorize_url) {
            showToast('Failed to start sign-in: ' + (data.detail || 'unknown error'), {type: 'error'});
            return;
        }
        window.open(data.authorize_url, '_blank');
        showToast('Complete sign-in in the new tab, then click Refresh below.', {type: 'info'});
    } catch (e) {
        showToast("Can't reach the server — check your connection.", {type: 'error'});
    }
}

async function deleteCredential(id) {
    if (!confirm("Delete this credential?")) return;
    await fetch(`/api/credentials/${id}`, {method: 'DELETE'});
    loadCredentials();
}

async function testCredential(id, btn) {
    const origText = btn.textContent;
    btn.textContent = "Testing...";
    btn.disabled = true;

    try {
        const res = await fetch(`/api/credentials/${id}/test`, {method: 'POST'});
        const data = await res.json();
        if (data.success) {
            showToast('Connection successful.', {type: 'success'});
        } else {
            showToast('Connection failed: ' + data.error, {type: 'error'});
        }
    } catch (e) {
        showToast("Can't reach the server — check your connection.", {type: 'error'});
    } finally {
        btn.textContent = origText;
        btn.disabled = false;
        loadCredentials();
    }
}

// ── UI Helpers ────────────────────────────────────────────────────────────────

function toggleAllCheckboxes(source, checkboxClass, actionClass) {
    const checkboxes = document.querySelectorAll(`.${checkboxClass}`);
    checkboxes.forEach(cb => cb.checked = source.checked);
    updateBatchActionVisibility(checkboxClass, actionClass);
}

function updateBatchActionVisibility(checkboxClass, actionClass) {
    const checkboxes = document.querySelectorAll(`.${checkboxClass}`);
    const checkedCount = Array.from(checkboxes).filter(cb => cb.checked).length;
    const actions = document.querySelectorAll(`.${actionClass}`);
    actions.forEach(btn => {
        btn.style.display = checkedCount > 0 ? 'inline-block' : 'none';
    });
}

function getSelectedIds(checkboxClass) {
    const checkboxes = document.querySelectorAll(`.${checkboxClass}:checked`);
    return Array.from(checkboxes).map(cb => parseInt(cb.value));
}

// ── Navigation ────────────────────────────────────────────────────────────────

async function loadTemplates() {
    try {
        const res = await fetch('/api/templates');
        const templates = await res.json();

        const tbody = document.getElementById('templates-tbody');
        tbody.innerHTML = '';

        if (templates.length === 0) {
            tbody.innerHTML = `<tr><td colspan="4" class="empty-state">No templates created.</td></tr>`;
            return;
        }

        templates.forEach(t => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><input type="checkbox" class="tpl-checkbox" value="${t.id}" onchange="updateBatchActionVisibility('tpl-checkbox', 'batch-action-tpl')"></td>
                <td><strong>${t.name}</strong></td>
                <td>${t.subject}</td>
                <td>
                    <button class="btn-secondary btn-sm" onclick="editTemplate(${t.id})">Edit</button>
                    <button class="btn-secondary btn-sm" onclick="deleteTemplate(${t.id})" style="color:var(--danger)">Del</button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error("Failed to load templates", e);
    }
}

let lastFocusedInput = null;

function insertTemplateVar(varText) {
    const input = lastFocusedInput || document.getElementById('tpl-body');
    const start = input.selectionStart || 0;
    const end = input.selectionEnd || 0;
    const text = input.value;
    input.value = text.slice(0, start) + varText + text.slice(end);
    input.focus();
    input.selectionStart = input.selectionEnd = start + varText.length;
}

function openTemplateModal() {
    editTemplateId = null;
    document.getElementById('tpl-modal-title').textContent = "Create Template";
    document.getElementById('tpl-error').style.display = 'none';
    const subj = document.getElementById('tpl-subject');
    const body = document.getElementById('tpl-body');
    document.getElementById('tpl-name').value = '';
    subj.value = '';
    body.value = '';

    subj.onfocus = () => lastFocusedInput = subj;
    body.onfocus = () => lastFocusedInput = body;
    lastFocusedInput = body;

    document.getElementById('modal-template').style.display = 'flex';
}

function closeTemplateModal() {
    document.getElementById('modal-template').style.display = 'none';
}

async function editTemplate(id) {
    try {
        const res = await fetch(`/api/templates/${id}`);
        const tpl = await res.json();
        editTemplateId = id;
        document.getElementById('tpl-modal-title').textContent = "Edit Template";
        document.getElementById('tpl-error').style.display = 'none';
        const subj = document.getElementById('tpl-subject');
        const body = document.getElementById('tpl-body');
        document.getElementById('tpl-name').value = tpl.name;
        subj.value = tpl.subject;
        body.value = tpl.raw_body;

        subj.onfocus = () => lastFocusedInput = subj;
        body.onfocus = () => lastFocusedInput = body;
        lastFocusedInput = body;

        document.getElementById('modal-template').style.display = 'flex';
    } catch (e) {
        console.error(e);
    }
}

async function saveTemplate() {
    const payload = {
        name: document.getElementById('tpl-name').value.trim(),
        subject: document.getElementById('tpl-subject').value,
        raw_body: document.getElementById('tpl-body').value
    };

    if (!payload.name) {
        const errDiv = document.getElementById('tpl-error');
        errDiv.textContent = "Template name cannot be empty.";
        errDiv.style.display = 'block';
        return;
    }

    const method = editTemplateId ? 'PUT' : 'POST';
    const url = editTemplateId ? `/api/templates/${editTemplateId}` : '/api/templates';

    try {
        const res = await fetch(url, {
            method: method,
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });

        if (res.ok) {
            closeTemplateModal();
            loadTemplates();
        } else {
            const err = await res.json();
            const errDiv = document.getElementById('tpl-error');
            errDiv.textContent = err.detail || "Validation error";
            errDiv.style.display = 'block';
        }
    } catch (e) {
        console.error(e);
    }
}

async function deleteTemplate(id) {
    if (!confirm("Delete this template?")) return;
    await fetch(`/api/templates/${id}`, {method: 'DELETE'});
    loadTemplates();
}

// ── Blacklist ────────────────────────────────────────────────────────────────

async function loadBlacklist() {
    try {
        const res = await fetch('/api/blacklist');
        const data = await res.json();

        const tbody = document.getElementById('blacklist-tbody');
        tbody.innerHTML = '';

        if (data.entries.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="empty-state">Blacklist is empty.</td></tr>`;
            return;
        }

        data.entries.forEach(b => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><input type="checkbox" class="blk-checkbox" value="${b.id}" onchange="updateBatchActionVisibility('blk-checkbox', 'batch-action-blk')"></td>
                <td>${b.email}</td>
                <td>${b.domain}</td>
                <td>${b.reason || '—'}</td>
                <td>
                    <button class="btn-secondary btn-sm" onclick="removeBlacklist(${b.id})" style="color:var(--danger)">Unblock</button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error("Failed to load blacklist", e);
    }
}

function openBlacklistModal() {
    document.getElementById('blk-error').style.display = 'none';
    document.getElementById('blk-email').value = '';
    document.getElementById('blk-reason').value = '';
    document.getElementById('modal-blacklist').style.display = 'flex';
}

function closeBlacklistModal() {
    document.getElementById('modal-blacklist').style.display = 'none';
}

async function saveBlacklist() {
    const payload = {
        email: document.getElementById('blk-email').value,
        reason: document.getElementById('blk-reason').value || null
    };

    try {
        const res = await fetch('/api/blacklist', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });

        if (res.ok) {
            closeBlacklistModal();
            loadBlacklist();
        } else {
            const err = await res.json();
            const errDiv = document.getElementById('blk-error');
            errDiv.textContent = err.detail || "Error blocking email";
            errDiv.style.display = 'block';
        }
    } catch (e) {
        console.error(e);
    }
}

async function removeBlacklist(id) {
    if (!confirm("Unblock this email?")) return;
    await fetch(`/api/blacklist/${id}`, {method: 'DELETE'});
    loadBlacklist();
}

// ── Batch Actions ────────────────────────────────────────────────────────────

async function batchTestCredentials() {
    const ids = getSelectedIds('cred-checkbox');
    if (ids.length === 0) return;

    const btns = document.querySelectorAll('.batch-action-cred');
    const btn = btns[0];
    const origText = btn.textContent;
    btn.textContent = "Testing...";
    btn.disabled = true;

    let successes = 0;
    let fails = 0;
    for (const id of ids) {
        try {
            const res = await fetch(`/api/credentials/${id}/test`, {method: 'POST'});
            const data = await res.json();
            if (data.success) successes++;
            else fails++;
        } catch (e) {
            fails++;
        }
    }

    btn.textContent = origText;
    btn.disabled = false;
    showToast(`Batch test complete — ${successes} successful, ${fails} failed.`, {type: fails > 0 ? 'warning' : 'success'});
    loadCredentials();
}

async function batchDeleteCredentials() {
    const ids = getSelectedIds('cred-checkbox');
    if (ids.length === 0) return;
    if (!confirm(`Delete ${ids.length} credentials?`)) return;

    for (const id of ids) {
        await fetch(`/api/credentials/${id}`, {method: 'DELETE'});
    }
    const selectAll = document.querySelector('input[onchange*="cred-checkbox"]');
    if (selectAll) selectAll.checked = false;
    updateBatchActionVisibility('cred-checkbox', 'batch-action-cred');
    loadCredentials();
}

async function batchDeleteTemplates() {
    const ids = getSelectedIds('tpl-checkbox');
    if (ids.length === 0) return;
    if (!confirm(`Delete ${ids.length} templates?`)) return;

    for (const id of ids) {
        await fetch(`/api/templates/${id}`, {method: 'DELETE'});
    }
    const selectAll = document.querySelector('input[onchange*="tpl-checkbox"]');
    if (selectAll) selectAll.checked = false;
    updateBatchActionVisibility('tpl-checkbox', 'batch-action-tpl');
    loadTemplates();
}

async function batchDeleteBlacklist() {
    const ids = getSelectedIds('blk-checkbox');
    if (ids.length === 0) return;
    if (!confirm(`Unblock ${ids.length} items?`)) return;

    for (const id of ids) {
        await fetch(`/api/blacklist/${id}`, {method: 'DELETE'});
    }
    const selectAll = document.querySelector('input[onchange*="blk-checkbox"]');
    if (selectAll) selectAll.checked = false;
    updateBatchActionVisibility('blk-checkbox', 'batch-action-blk');
    loadBlacklist();
}
