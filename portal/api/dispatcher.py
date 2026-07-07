"""
Async background dispatcher for SMTP campaigns.
"""

import aiosmtplib
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from ..db import Database, CampaignStatus

log = logging.getLogger(__name__)

NO_USABLE_CREDENTIALS_REASON = (
    "Paused: no usable SMTP credentials — every assigned credential is disabled, "
    "cooling down, or has hit its daily send limit."
)

# Cross-campaign send pacing: keyed by credential id, shared by every dispatch task
# in this process (single event loop, per the app's threading model) so two campaigns
# sharing a credential can't both fire through it within the same jitter window.
_credential_locks: dict[int, asyncio.Lock] = {}
_credential_last_sent: dict[int, float] = {}


def _get_next_credential(active_creds: list[dict], index: int) -> tuple[dict, int]:
    """Simple round-robin with wraparound. Returns (credential, next_index)."""
    if not active_creds:
        return None, 0
    cred = active_creds[index % len(active_creds)]
    return cred, (index + 1) % len(active_creds)


def resolve_credential_pool(db: Database, assigned_ids: list[int] | None) -> list[dict]:
    """Credentials this campaign may send through: its explicit assignment if any,
    otherwise every active credential (unchanged legacy behavior). Excludes any
    credential that has already hit its daily_send_limit today."""
    creds = db.get_credentials_by_ids(assigned_ids) if assigned_ids else db.get_active_credentials()
    return [
        c for c in creds
        if not (c.get("daily_send_limit") and db.get_credential_sent_count_today(c["id"]) >= c["daily_send_limit"])
    ]


async def _wait_for_credential_slot(cred_id: int) -> None:
    """Block until at least a 30-90s gap has passed since the last send through this
    credential, regardless of which campaign task is sending."""
    lock = _credential_locks.setdefault(cred_id, asyncio.Lock())
    async with lock:
        last = _credential_last_sent.get(cred_id)
        if last is not None:
            gap = random.uniform(30, 90)
            remaining = gap - (time.monotonic() - last)
            if remaining > 0:
                await asyncio.sleep(remaining)
        _credential_last_sent[cred_id] = time.monotonic()


async def _send_one_email(credential: dict, recipient: str, subject: str, body: str) -> None:
    """Send a single email using aiosmtplib.
    Constructs MIMEText, connects, authenticates, sends, disconnects."""

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = credential["username"]
    msg["To"] = recipient
    msg["Subject"] = subject

    use_tls = credential["port"] == 465
    start_tls = credential["port"] == 587

    smtp = aiosmtplib.SMTP(
        hostname=credential["host"],
        port=credential["port"],
        use_tls=use_tls,
        start_tls=start_tls,
        timeout=30,
    )

    await smtp.connect()
    await smtp.login(credential["username"], credential["password"])
    await smtp.send_message(msg)
    await smtp.quit()


async def run_campaign_dispatch(campaign_id: int, db: Database) -> None:
    log.info(f"Campaign {campaign_id} dispatch started")

    # 1. Flip any newly-selected DRAFT → QUEUED. Emails left QUEUED from a previous
    # paused run are already queued and don't need re-flipping, so "nothing new
    # queued" alone doesn't mean "nothing to send" — check for a leftover queue too.
    queued_count = db.queue_campaign_emails(campaign_id)
    log.info(f"Campaign {campaign_id}: {queued_count} drafts moved to QUEUED")

    if queued_count == 0 and not db.get_next_queued_email(campaign_id):
        db.update_campaign_status(campaign_id, CampaignStatus.COMPLETED)
        log.info(f"Campaign {campaign_id} completed: No emails to send.")
        return

    # 2. Load assigned/active credentials
    active_creds = resolve_credential_pool(db, db.get_campaign_credential_ids(campaign_id))
    if not active_creds:
        db.update_campaign_status(campaign_id, CampaignStatus.PAUSED, reason=NO_USABLE_CREDENTIALS_REASON)
        log.warning(f"Campaign {campaign_id} paused: No usable SMTP credentials.")
        return

    cred_index = 0

    # 3. Dispatch loop
    while True:
        # a. Check Campaign status
        campaign = db.get_campaign(campaign_id)
        if not campaign:
            break

        status = CampaignStatus(campaign["status"])

        if status == CampaignStatus.PAUSED:
            log.info(f"Campaign {campaign_id} paused by user.")
            break

        if status == CampaignStatus.CANCELLED:
            cancelled_count = db.cancel_remaining_queued(campaign_id)
            log.info(f"Campaign {campaign_id} cancelled. {cancelled_count} remaining queued emails marked FAILED.")
            break

        # b. Get next queued email
        email = db.get_next_queued_email(campaign_id)

        # c. If no more QUEUED emails remain
        if not email:
            # COMPLETED only when no DRAFT emails remain (all processed)
            if db.has_remaining_drafts(campaign_id):
                db.update_campaign_status(campaign_id, CampaignStatus.PAUSED)
                log.info(f"Campaign {campaign_id} paused: queued batch finished, deselected drafts remain.")
            else:
                db.update_campaign_status(campaign_id, CampaignStatus.COMPLETED)
                log.info(f"Campaign {campaign_id} completed.")
            break

        # d. Get next credential — re-read the campaign's assignment fresh every
        # iteration so a live PUT .../credentials edit takes effect immediately,
        # even while this campaign is RUNNING.
        active_creds = resolve_credential_pool(db, db.get_campaign_credential_ids(campaign_id))
        cred, cred_index = _get_next_credential(active_creds, cred_index)

        if not cred:
            db.update_campaign_status(campaign_id, CampaignStatus.PAUSED, reason=NO_USABLE_CREDENTIALS_REASON)
            log.warning(f"Campaign {campaign_id} paused: All credentials disabled, cooling, or capped.")
            break

        email_id = email["id"]
        recipient = email["recipient_email"]

        # e. Try send via aiosmtplib
        try:
            await _wait_for_credential_slot(cred["id"])
            log.info(f"Sending email {email_id} to {recipient} via {cred['username']}")
            await _send_one_email(cred, recipient, email["subject"], email["body"])
            db.mark_email_sent(email_id, cred["id"])
            log.info(f"Email {email_id} sent successfully.")

        except aiosmtplib.SMTPAuthenticationError as e:
            db.disable_credential(cred["id"])
            log.error(f"Credential {cred['username']} auth failed: {e}. Credential disabled.")
            # Do NOT mark email failed, will retry in next loop
            continue

        except aiosmtplib.SMTPResponseException as e:
            # 550 / 553 - Hard bounce
            if e.code in (550, 553):
                domain = recipient.split("@")[1] if "@" in recipient else ""
                db.add_to_blacklist(recipient, domain, reason=f"{e.code}: {e.message}")
                db.mark_email_failed(email_id, f"{e.code}: {e.message}", cred["id"])
                log.warning(f"Hard bounce {e.code} for {recipient}. Blacklisted and marked FAILED.")
            # 421 / 450 / 451 - Rate limited / Temporary
            elif e.code in (421, 450, 451):
                cooldown_until = datetime.utcnow() + timedelta(hours=1)
                db.set_credential_cooldown(cred["id"], cooldown_until)
                log.warning(f"Credential {cred['username']} rate limited ({e.code}). Cooled down for 1 hour.")
                continue  # Retry email
            else:
                db.mark_email_failed(email_id, f"{e.code}: {e.message}", cred["id"])
                log.error(f"SMTPResponseException {e.code}: {e.message} for {recipient}")

        except aiosmtplib.SMTPRecipientsRefused as e:
            # All recipients rejected
            err_code = ""
            err_msg = ""
            for r, (code, msg) in e.recipients.items():
                err_code = code
                err_msg = msg
                break  # take the first one

            if err_code in (550, 553):
                domain = recipient.split("@")[1] if "@" in recipient else ""
                db.add_to_blacklist(recipient, domain, reason=f"{err_code}: {err_msg}")
                db.mark_email_failed(email_id, f"{err_code}: {err_msg}", cred["id"])
                log.warning(
                    f"Hard bounce (RecipientsRefused) {err_code} for {recipient}. Blacklisted and marked FAILED.")
            else:
                db.mark_email_failed(email_id, f"{err_code}: {err_msg}", cred["id"])
                log.error(f"SMTPRecipientsRefused {err_code}: {err_msg} for {recipient}")

        except (aiosmtplib.SMTPConnectError, OSError, TimeoutError) as e:
            # Network issues
            cooldown_until = datetime.utcnow() + timedelta(minutes=15)
            db.set_credential_cooldown(cred["id"], cooldown_until)
            log.warning(f"Credential {cred['username']} network error: {e}. Cooled down for 15 mins.")
            continue  # Retry email

        except Exception as e:
            # Unexpected
            db.mark_email_failed(email_id, str(e), cred["id"])
            log.error(f"Unexpected error sending email {email_id} to {recipient}: {e}", exc_info=True)
