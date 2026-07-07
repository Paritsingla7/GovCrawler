const ADMIN_DASHBOARD_POLL_MS = 3000;
let _adminDashboardTimer = null;

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
