# Email Outreach System

Source files:

- [`portal/api/campaigns.py`](../portal/api/campaigns.py) — campaign generation + staging routes (`APIRouter`)
- [`portal/services/campaign_service.py`](../portal/services/campaign_service.py) — `render_template_string()` and
  `render_draft_emails()`; the blacklist/exclude filtering + Jinja2 rendering logic shared by campaign creation and
  the "add more leads" endpoint
- [`portal/api/dispatcher.py`](../portal/api/dispatcher.py) — async SMTP dispatch worker
- [`portal/api/templates.py`](../portal/api/templates.py) — email template CRUD (`APIRouter`)
- [`portal/api/credentials.py`](../portal/api/credentials.py) — SMTP credential management (`APIRouter`)
- [`portal/api/blacklist.py`](../portal/api/blacklist.py) — email/domain blacklist (`APIRouter`)

All route modules pull the shared `Database` instance via `Depends(get_db)` from
[`portal/api/deps.py`](../portal/api/deps.py) rather than through closures.

---

## Overview

The outreach system lets you turn crawled leads into email campaigns. The workflow is:

1. Create an **Email Template** with Jinja2 variables.
2. Create a **Campaign** selecting leads + a template → draft emails are auto-rendered.
3. Review and edit individual drafts; deselect emails with missing data.
4. Add **SMTP Credentials**.
5. **Dispatch** — a background worker sends emails with rate-limit handling and automatic hard-bounce blacklisting.

A separate **Test Campaign** flow lets you validate SMTP credentials and template rendering against dummy recipients
before sending to real leads.

---

## Email Templates

Templates use [Jinja2](https://jinja.palletsprojects.com/) syntax. Subject and body are both template strings.

**Available variables at render time:**

| Variable            | Source                                                                                  |
|---------------------|-----------------------------------------------------------------------------------------|
| `{{ name }}`        | `lead.person_name` (falls back to `"Official"` in subject, `"[MISSING: name]"` in body) |
| `{{ designation }}` | `lead.designation` (falls back to `""` in subject, `"[MISSING: designation]"` in body)  |

**Example template:**

```
Subject: Important Communication — {{ designation }}, {{ name }}

Body:
Dear {{ designation }} {{ name }},

We are writing to inform you about ...

Regards,
[Your Name]
```

Templates are validated for Jinja2 syntax errors on create and update. Invalid syntax returns HTTP 400 with the line
number and error message.

---

## Campaign Creation

`POST /api/campaigns` runs the following pipeline:

1. **Load leads** from DB by `lead_ids`.
2. **Blacklist filter** — skip any lead whose email is in the `blacklist` table.
3. **Create `Campaign` row** with status `PAUSED`.
4. **Render drafts** via `campaign_service.render_draft_emails()` — for each remaining lead:
    - Detect missing variables (`name`, `designation`).
    - Render subject with clean fallbacks (`"Official"` for missing name).
    - Render body with `[MISSING: field]` markers so reviewers know what to fix.
    - Set `is_selected = False` for any email with missing fields.
5. **Bulk insert** `CampaignEmail` rows with status `DRAFT`.

The campaign starts in `PAUSED` status. No emails are sent until you explicitly dispatch.

`POST /api/campaigns/{id}/emails` (adding more leads to an existing campaign) runs the same
`render_draft_emails()` call with an additional `exclude_emails` set — recipients already staged in the
campaign are skipped and counted separately (`already_in_campaign` in the response) rather than treated as
new drafts.

---

## Draft Review

Before dispatching, you can:

| Action            | API                                                |
|-------------------|----------------------------------------------------|
| View all drafts   | `GET /api/campaigns/{id}/emails`                   |
| Edit subject/body | `PUT /api/campaigns/{id}/emails/{eid}`             |
| Select/deselect   | `PATCH /api/campaigns/{id}/emails/{eid}/selection` |
| Delete a draft    | `DELETE /api/campaigns/{id}/emails/{eid}`          |
| Add more leads    | `POST /api/campaigns/{id}/emails`                  |

Deselected emails (`is_selected = False`) are excluded from dispatch and counted as `skipped` in stats.

---

## Dispatch

`POST /api/campaigns/{id}/dispatch` starts `run_campaign_dispatch(campaign_id, db)` as an `asyncio.Task`.

```
run_campaign_dispatch(campaign_id, db):
  1. queue_campaign_emails()       DRAFT(is_selected=True) → QUEUED
  2. Load active credentials
  3. Loop:
     a. Check campaign status:
        - PAUSED   → break (user kill-switch)
        - CANCELLED → cancel_remaining_queued() → break
     b. get_next_queued_email()    → None means done
     c. get_active_credentials()  → round-robin credential selection
     d. _send_one_email(cred, recipient, subject, body)
        - Constructs MIMEText (plain/utf-8)
        - aiosmtplib.SMTP with TLS (port 465) or STARTTLS (port 587)
     e. On success  → mark_email_sent()
        On hard bounce (550/553)  → add_to_blacklist() + mark_email_failed()
        On rate limit (421/450/451) → set_credential_cooldown(+1 hour) + retry
        On auth failure → disable_credential() + retry email
        On network error → set_credential_cooldown(+15 min) + retry
     f. await asyncio.sleep(random 30–90 s)  ← rate-limit jitter
  4. Update campaign status:
     - All emails processed + no remaining drafts → COMPLETED
     - Deselected drafts remain → PAUSED
```

### Credential Rotation

Active credentials are selected in round-robin order. After each send, the credential list is reloaded from DB to catch
any state changes (new credentials added, cooldown expired).

### Credential States

| State    | Condition                                       | Effect                                                                |
|----------|-------------------------------------------------|-----------------------------------------------------------------------|
| Active   | `is_active=True`, `cooldown_until=NULL or past` | Available for round-robin                                             |
| Cooling  | `is_active=True`, `cooldown_until` in future    | Skipped until cooldown expires                                        |
| Disabled | `is_active=False`                               | Never used; requires manual re-enable via `PUT /api/credentials/{id}` |

---

## Blacklist

The blacklist prevents emails from being staged in new campaigns and from being sent in existing ones.

**Auto-blacklisting:** On SMTP hard bounce (codes 550 or 553), the recipient email and domain are added to the blacklist
and the email is marked `FAILED`.

**Manual blacklisting:** `POST /api/blacklist` with an email address.

**Effect on campaigns:** `create_campaign` loads the full blacklist set (`get_blacklisted_emails_set()`) and filters
leads before rendering any drafts. Already-staged FAILED emails are not re-sent.

---

## Test Campaigns

Test campaigns are structurally identical to production campaigns but use manually specified dummy recipients instead of
real leads. Use them to:

- Verify your SMTP credentials work.
- Preview template rendering before a real campaign.
- Confirm deliverability with your own email addresses.

**Key differences from production campaigns:**

| Feature              | Production                              | Test                                           |
|----------------------|-----------------------------------------|------------------------------------------------|
| Recipients           | From `leads` table                      | Manually provided `dummy_details`              |
| Credential selection | Round-robin over all active credentials | Optionally pin a specific `test_credential_id` |
| Blacklist check      | Yes                                     | No                                             |
| `lead_id` FK         | Required                                | Null (no real lead)                            |

**Create a test campaign:**

```json
POST /api/test-campaigns
{
  "name": "SMTP Sanity Check",
  "template_id": 1,
  "test_credential_id": 2,
  "dummy_details": [
    {
      "name": "Test User",
      "designation": "Director",
      "email": "your.own.email@example.com",
      "department": "Test Dept"
    }
  ]
}
```

---

## Campaign Status Reference

| Status      | Who sets it       | When                                                           |
|-------------|-------------------|----------------------------------------------------------------|
| `PAUSED`    | `create_campaign` | Initial state after draft generation                           |
| `RUNNING`   | `dispatch`        | When dispatch starts; or manually via PATCH                    |
| `PAUSED`    | Dispatcher        | No active credentials; or deselected drafts remain after batch |
| `CANCELLED` | User (PATCH)      | All QUEUED emails marked FAILED                                |
| `COMPLETED` | Dispatcher        | All selected emails sent, no remaining drafts                  |

---

## SMTP Port Configuration

| Port  | Protocol            | `use_tls` | `start_tls` |
|-------|---------------------|-----------|-------------|
| 465   | SMTP over TLS (SSL) | `True`    | `False`     |
| 587   | SMTP with STARTTLS  | `False`   | `True`      |
| Other | Plain SMTP          | `False`   | `False`     |

---

## Stats Endpoint

`GET /api/campaigns/{id}/stats` returns a lightweight object polled every 3 seconds by the UI:

```json
{
  "draft": 8,
  "queued": 2,
  "sent": 45,
  "failed": 3,
  "skipped": 5,
  "total": 63,
  "campaign_status": "RUNNING"
}
```

- `draft` = selected DRAFT emails (not yet dispatched)
- `skipped` = deselected DRAFT emails
- `queued` = emails moved to QUEUED but not yet sent
- `sent` + `failed` = terminal states
