document.addEventListener("DOMContentLoaded", () => {
    loadTemplates();
    loadCredentials();
});

async function loadTemplates() {
    try {
        const res = await fetch('/api/templates');
        const data = await res.json();
        const select = document.getElementById('test-template');
        select.innerHTML = '<option value="">-- Select Template --</option>';
        data.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = t.name;
            select.appendChild(opt);
        });
    } catch (e) {
        console.error("Failed to load templates", e);
        document.getElementById('test-template').innerHTML = '<option value="">Error loading templates</option>';
    }
}

async function loadCredentials() {
    const tbody = document.getElementById('test-credentials-list');
    try {
        const res = await fetch('/api/credentials');
        const data = await res.json();

        if (data.length === 0) {
            tbody.innerHTML = '<tr><td class="empty-state">No SMTP credentials configured yet.</td></tr>';
            return;
        }

        tbody.innerHTML = '';
        data.forEach((c, i) => {
            const limit = c.daily_send_limit ? `${c.sent_today}/${c.daily_send_limit} today` : `${c.sent_today} sent today`;
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td style="width:24px;"><input type="radio" name="test-credential-radio" class="test-cred-radio" value="${c.id}" ${i === 0 ? 'checked' : ''}></td>
                <td>${c.username} (${c.host})</td>
                <td style="font-size:12px; color:var(--muted);">${limit}</td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error("Failed to load credentials", e);
        tbody.innerHTML = '<tr><td class="empty-state">Failed to load credentials.</td></tr>';
    }
}

function toggleCredentialSelect() {
    const isRoundRobin = document.getElementById('test-round-robin').checked;
    document.getElementById('credential-select-group').style.display = isRoundRobin ? 'none' : 'block';
}

let leadCount = 1;

function addDummyLead(prefill) {
    leadCount++;
    const container = document.getElementById('dummy-leads-container');
    const newLead = document.createElement('div');
    newLead.className = 'dummy-lead-entry';
    newLead.style = 'border: 1px solid var(--border); padding: 16px; border-radius: 8px; margin-bottom: 16px; position: relative;';

    const v = prefill || {};
    const esc = (s) => (s || '').toString().replace(/"/g, '&quot;');

    newLead.innerHTML = `
        <button type="button" onclick="this.parentElement.remove()" style="position: absolute; right: 16px; top: 16px; background: none; border: none; color: var(--danger); cursor: pointer; font-size: 16px;">✕</button>
        <h4 style="margin-top:0; margin-bottom: 12px; font-size: 14px; font-weight: 600;">Lead ${leadCount}</h4>
        <div class="form-group">
            <label>Name</label>
            <input type="text" class="test-lead-name" placeholder="John Doe" value="${esc(v.name)}">
        </div>
        <div class="form-group">
            <label>Designation</label>
            <input type="text" class="test-lead-designation" placeholder="Director" value="${esc(v.designation)}">
        </div>
        <div class="form-group">
            <label>Department</label>
            <input type="text" class="test-lead-department" placeholder="IT Department" value="${esc(v.department)}">
        </div>
        <div class="form-group" style="margin-bottom: 0;">
            <label>Email Address</label>
            <input type="email" class="test-lead-email" required placeholder="john@example.com" value="${esc(v.email)}">
        </div>
    `;
    container.appendChild(newLead);
}

async function uploadDummyLeadsCsv() {
    const fileInput = document.getElementById('test-csv-file');
    const file = fileInput.files[0];
    const resultBox = document.getElementById('test-csv-result');
    if (!file) {
        alert('Choose a CSV file first.');
        return;
    }

    try {
        const formData = new FormData();
        formData.append('file', file);

        const res = await fetch('/api/campaigns/parse-csv', {method: 'POST', body: formData});
        if (!res.ok) {
            let detail = 'Failed to parse CSV';
            try {
                detail = (await res.json()).detail || detail;
            } catch (_) {
            }
            throw new Error(detail);
        }

        const data = await res.json();

        const container = document.getElementById('dummy-leads-container');
        container.innerHTML = '';
        leadCount = 0;
        data.dummy_details.forEach(d => addDummyLead(d));

        let html = `Loaded <strong>${data.dummy_details.length}</strong> lead(s) from CSV.`;
        if (data.skipped.length) {
            html += ` <strong>${data.skipped.length}</strong> row(s) skipped:`;
            html += '<ul style="margin:6px 0 0 18px;max-height:140px;overflow-y:auto;">';
            data.skipped.forEach(s => {
                html += `<li>Row ${s.row}${s.email ? ` (${s.email})` : ''}: ${s.reason}</li>`;
            });
            html += '</ul>';
        }
        resultBox.innerHTML = html;
        resultBox.style.display = 'block';
    } catch (e) {
        resultBox.innerHTML = `CSV upload failed: ${e.message}`;
        resultBox.style.display = 'block';
    }
}

async function submitTestCampaign(e) {
    e.preventDefault();
    const btn = document.getElementById('btn-run-test');
    btn.disabled = true;
    btn.textContent = "Creating...";

    const isRoundRobin = document.getElementById('test-round-robin').checked;
    const checkedRadio = document.querySelector('.test-cred-radio:checked');
    const credId = isRoundRobin ? null : (checkedRadio ? checkedRadio.value : null);

    if (!isRoundRobin && !credId) {
        alert("Select an SMTP credential, or use round robin.");
        btn.disabled = false;
        btn.textContent = "Create & Dispatch Test";
        return;
    }

    const dummyDetails = [];
    document.querySelectorAll('.dummy-lead-entry').forEach(entry => {
        dummyDetails.push({
            name: entry.querySelector('.test-lead-name').value,
            designation: entry.querySelector('.test-lead-designation').value,
            department: entry.querySelector('.test-lead-department').value,
            email: entry.querySelector('.test-lead-email').value,
        });
    });

    const payload = {
        name: document.getElementById('test-name').value,
        template_id: parseInt(document.getElementById('test-template').value, 10),
        kind: 'test',
        test_credential_id: credId ? parseInt(credId, 10) : null,
        dummy_details: dummyDetails
    };

    try {
        // 1. Create Test Campaign
        const res = await fetch('/api/campaigns', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Failed to create test campaign");
        }

        const data = await res.json();
        const campaignId = data.campaign_id;

        // 2. Redirect to Campaigns list
        window.location.href = '/campaigns';

    } catch (err) {
        alert("Error: " + err.message);
        btn.disabled = false;
        btn.textContent = "Create & Dispatch Test";
    }
}
