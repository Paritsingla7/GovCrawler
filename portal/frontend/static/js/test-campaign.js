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
    try {
        const res = await fetch('/api/credentials');
        const data = await res.json();
        const select = document.getElementById('test-credential');
        select.innerHTML = '<option value="">-- Select Credential --</option>';
        data.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = `${c.username} (${c.host})`;
            select.appendChild(opt);
        });
    } catch (e) {
        console.error("Failed to load credentials", e);
        document.getElementById('test-credential').innerHTML = '<option value="">Error loading credentials</option>';
    }
}

function toggleCredentialSelect() {
    const isRoundRobin = document.getElementById('test-round-robin').checked;
    const credGroup = document.getElementById('credential-select-group');
    const credSelect = document.getElementById('test-credential');

    if (isRoundRobin) {
        credGroup.style.display = 'none';
        credSelect.removeAttribute('required');
    } else {
        credGroup.style.display = 'block';
        credSelect.setAttribute('required', 'true');
    }
}

let leadCount = 1;

function addDummyLead() {
    leadCount++;
    const container = document.getElementById('dummy-leads-container');
    const newLead = document.createElement('div');
    newLead.className = 'dummy-lead-entry';
    newLead.style = 'border: 1px solid var(--border); padding: 16px; border-radius: 8px; margin-bottom: 16px; position: relative;';

    newLead.innerHTML = `
        <button type="button" onclick="this.parentElement.remove()" style="position: absolute; right: 16px; top: 16px; background: none; border: none; color: var(--danger); cursor: pointer; font-size: 16px;">✕</button>
        <h4 style="margin-top:0; margin-bottom: 12px; font-size: 14px; font-weight: 600;">Lead ${leadCount}</h4>
        <div class="form-group">
            <label>Name</label>
            <input type="text" class="test-lead-name" required placeholder="John Doe">
        </div>
        <div class="form-group">
            <label>Designation</label>
            <input type="text" class="test-lead-designation" placeholder="Director">
        </div>
        <div class="form-group">
            <label>Department</label>
            <input type="text" class="test-lead-department" placeholder="IT Department">
        </div>
        <div class="form-group" style="margin-bottom: 0;">
            <label>Email Address</label>
            <input type="email" class="test-lead-email" required placeholder="john@example.com">
        </div>
    `;
    container.appendChild(newLead);
}

async function submitTestCampaign(e) {
    e.preventDefault();
    const btn = document.getElementById('btn-run-test');
    btn.disabled = true;
    btn.textContent = "Creating...";

    const isRoundRobin = document.getElementById('test-round-robin').checked;
    const credId = isRoundRobin ? null : document.getElementById('test-credential').value;

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
        test_credential_id: credId ? parseInt(credId, 10) : null,
        dummy_details: dummyDetails
    };

    try {
        // 1. Create Test Campaign
        const res = await fetch('/api/test-campaigns', {
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
