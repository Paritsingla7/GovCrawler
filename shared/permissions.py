"""Single source of truth for the RBAC capability catalog and built-in role
defaults. Used both to seed the database (Database.seed_rbac()) and by
portal.api.deps.require(...) to validate permission keys referenced in code.
"""

PERMISSIONS: dict[str, str] = {
    "users.manage": "Create, edit, and revoke user accounts",
    "roles.manage": "Create and edit roles and their permission bundles",
    "audit.view": "View the audit log",
    "settings.manage": "Edit global crawl policy / extraction / score weights",
    "domains.view": "Browse the domains catalog",
    "domains.import": "Trigger a domains catalog import/refresh",
    "crawl.run": "Create and start crawl jobs; cancel own jobs",
    "crawl.cancel_all": "Cancel any user's crawl job",
    "jobs.view_all": "See all users' crawl jobs (else own only)",
    "leads.view": "Browse the shared leads pool",
    "leads.edit": "Edit lead fields",
    "leads.export": "Export leads to CSV",
    "leads.import": "Bulk-import leads from CSV",
    "campaigns.manage": "Create and edit campaigns, templates usage",
    "campaigns.dispatch": "Start/pause/cancel campaign dispatch",
    "campaigns.view_all": "See all users' campaigns (else own only)",
    "templates.manage": "Create, edit, and delete email templates",
    "credentials.manage": "Create, edit, and delete SMTP credentials",
    "blacklist.manage": "Add/remove blacklist entries",
}

# Role -> set of permission keys. Admin is implied via User.is_admin
# (short-circuits every check) and does NOT need to be listed here.
ROLE_DEFAULTS: dict[str, set[str]] = {
    "Admin": set(PERMISSIONS.keys()),
    "Operator": {
        "domains.view", "domains.import",
        "crawl.run",
        "leads.view", "leads.edit", "leads.export", "leads.import",
        "campaigns.manage", "campaigns.dispatch",
        "templates.manage", "credentials.manage", "blacklist.manage",
    },
    "Viewer": {
        "domains.view",
        "leads.view",
    },
}

BUILTIN_ROLES = tuple(ROLE_DEFAULTS.keys())
