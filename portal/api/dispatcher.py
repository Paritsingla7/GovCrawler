"""
Async background dispatcher for SMTP campaigns.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta
from email.mime.text import MIMEText

import aiosmtplib

from ..db import Database, CampaignStatus

log = logging.getLogger(__name__)


def _get_next_credential(active_creds: list[dict], index: int) -> tuple[dict, int]:
    """Simple round-robin with wraparound. Returns (credential, next_index)."""
    if not active_creds:
        return None, 0
    cred = active_creds[index % len(active_creds)]
    return cred, (index + 1) % len(active_creds)


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

    # 1. Flip DRAFT → QUEUED
    queued_count = db.queue_campaign_emails(campaign_id)
    log.info(f"Campaign {campaign_id}: {queued_count} drafts moved to QUEUED")

    if queued_count == 0:
        db.update_campaign_status(campaign_id, CampaignStatus.COMPLETED)
        log.info(f"Campaign {campaign_id} completed: No emails to send.")
        return

    # 2. Load active credentials
    active_creds = db.get_active_credentials()
    if not active_creds:
        db.update_campaign_status(campaign_id, CampaignStatus.PAUSED)
        log.warning(f"Campaign {campaign_id} paused: No active credentials.")
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

        # d. Get next credential
        active_creds = db.get_active_credentials()  # reload in case they changed
        cred, cred_index = _get_next_credential(active_creds, cred_index)

        if not cred:
            db.update_campaign_status(campaign_id, CampaignStatus.PAUSED)
            log.warning(f"Campaign {campaign_id} paused: All credentials disabled or cooling.")
            break

        email_id = email["id"]
        recipient = email["recipient_email"]

        # e. Try send via aiosmtplib
        try:
            log.info(f"Sending email {email_id} to {recipient} via {cred['username']}")
            await _send_one_email(cred, recipient, email["subject"], email["body"])
            db.mark_email_sent(email_id)
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
                db.mark_email_failed(email_id, f"{e.code}: {e.message}")
                log.warning(f"Hard bounce {e.code} for {recipient}. Blacklisted and marked FAILED.")
            # 421 / 450 / 451 - Rate limited / Temporary
            elif e.code in (421, 450, 451):
                cooldown_until = datetime.utcnow() + timedelta(hours=1)
                db.set_credential_cooldown(cred["id"], cooldown_until)
                log.warning(f"Credential {cred['username']} rate limited ({e.code}). Cooled down for 1 hour.")
                continue  # Retry email
            else:
                db.mark_email_failed(email_id, f"{e.code}: {e.message}")
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
                db.mark_email_failed(email_id, f"{err_code}: {err_msg}")
                log.warning(
                    f"Hard bounce (RecipientsRefused) {err_code} for {recipient}. Blacklisted and marked FAILED.")
            else:
                db.mark_email_failed(email_id, f"{err_code}: {err_msg}")
                log.error(f"SMTPRecipientsRefused {err_code}: {err_msg} for {recipient}")

        except (aiosmtplib.SMTPConnectError, OSError, TimeoutError) as e:
            # Network issues
            cooldown_until = datetime.utcnow() + timedelta(minutes=15)
            db.set_credential_cooldown(cred["id"], cooldown_until)
            log.warning(f"Credential {cred['username']} network error: {e}. Cooled down for 15 mins.")
            continue  # Retry email

        except Exception as e:
            # Unexpected
            db.mark_email_failed(email_id, str(e))
            log.error(f"Unexpected error sending email {email_id} to {recipient}: {e}", exc_info=True)

        # g. asyncio.sleep(random.uniform(30, 90))
        jitter = random.uniform(30, 90)
        log.debug(f"Sleeping for {jitter:.2f}s before next send...")
        await asyncio.sleep(jitter)


async def run_test_campaign_dispatch(campaign_id: int, db: Database) -> None:
    log.info(f"Test Campaign {campaign_id} dispatch started")

    queued_count = db.queue_test_campaign_emails(campaign_id)
    log.info(f"Test Campaign {campaign_id}: {queued_count} drafts moved to QUEUED")

    if queued_count == 0:
        db.update_test_campaign_status(campaign_id, CampaignStatus.COMPLETED)
        log.info(f"Test Campaign {campaign_id} completed: No emails to send.")
        return

    campaign = db.get_test_campaign(campaign_id)
    if not campaign:
        return

    test_credential_id = campaign.get("test_credential_id")

    cred_index = 0

    while True:
        campaign = db.get_test_campaign(campaign_id)
        if not campaign:
            break

        status = CampaignStatus(campaign["status"])

        if status == CampaignStatus.PAUSED:
            log.info(f"Test Campaign {campaign_id} paused by user.")
            break

        if status == CampaignStatus.CANCELLED:
            cancelled_count = db.cancel_remaining_queued_test(campaign_id)
            log.info(f"Test Campaign {campaign_id} cancelled. {cancelled_count} remaining queued emails marked FAILED.")
            break

        email = db.get_next_queued_test_email(campaign_id)
        if not email:
            db.update_test_campaign_status(campaign_id, CampaignStatus.COMPLETED)
            log.info(f"Test Campaign {campaign_id} completed.")
            break

        if test_credential_id:
            cred = db.get_credential(test_credential_id)
            if not cred or not cred["is_active"]:
                db.update_test_campaign_status(campaign_id, CampaignStatus.PAUSED)
                log.warning(f"Test Campaign {campaign_id} paused: Selected credential inactive or not found.")
                break
        else:
            active_creds = db.get_active_credentials()
            cred, cred_index = _get_next_credential(active_creds, cred_index)
            if not cred:
                db.update_test_campaign_status(campaign_id, CampaignStatus.PAUSED)
                log.warning(f"Test Campaign {campaign_id} paused: All credentials disabled or cooling.")
                break

        email_id = email["id"]
        recipient = email["recipient_email"]

        try:
            log.info(f"Sending test email {email_id} to {recipient} via {cred['username']}")
            await _send_one_email(cred, recipient, email["subject"], email["body"])
            db.mark_test_email_sent(email_id)
            log.info(f"Test Email {email_id} sent successfully.")

        except aiosmtplib.SMTPAuthenticationError as e:
            db.disable_credential(cred["id"])
            log.error(f"Credential {cred['username']} auth failed: {e}. Credential disabled.")
            continue

        except aiosmtplib.SMTPResponseException as e:
            if e.code in (550, 553):
                domain = recipient.split("@")[1] if "@" in recipient else ""
                db.add_to_blacklist(recipient, domain, reason=f"{e.code}: {e.message}")
                db.mark_test_email_failed(email_id, f"{e.code}: {e.message}")
                log.warning(f"Hard bounce {e.code} for {recipient}. Blacklisted and marked FAILED.")
            elif e.code in (421, 450, 451):
                cooldown_until = datetime.utcnow() + timedelta(hours=1)
                db.set_credential_cooldown(cred["id"], cooldown_until)
                log.warning(f"Credential {cred['username']} rate limited ({e.code}). Cooled down for 1 hour.")
                continue
            else:
                db.mark_test_email_failed(email_id, f"{e.code}: {e.message}")
                log.error(f"SMTPResponseException {e.code}: {e.message} for {recipient}")

        except aiosmtplib.SMTPRecipientsRefused as e:
            err_code = ""
            err_msg = ""
            for r, (code, msg) in e.recipients.items():
                err_code = code
                err_msg = msg
                break

            if err_code in (550, 553):
                domain = recipient.split("@")[1] if "@" in recipient else ""
                db.add_to_blacklist(recipient, domain, reason=f"{err_code}: {err_msg}")
                db.mark_test_email_failed(email_id, f"{err_code}: {err_msg}")
                log.warning(
                    f"Hard bounce (RecipientsRefused) {err_code} for {recipient}. Blacklisted and marked FAILED.")
            else:
                db.mark_test_email_failed(email_id, f"{err_code}: {err_msg}")
                log.error(f"SMTPRecipientsRefused {err_code}: {err_msg} for {recipient}")

        except (aiosmtplib.SMTPConnectError, OSError, TimeoutError) as e:
            cooldown_until = datetime.utcnow() + timedelta(minutes=15)
            db.set_credential_cooldown(cred["id"], cooldown_until)
            log.warning(f"Credential {cred['username']} network error: {e}. Cooled down for 15 mins.")
            continue

        except Exception as e:
            db.mark_test_email_failed(email_id, str(e))
            log.error(f"Unexpected error sending test email {email_id} to {recipient}: {e}", exc_info=True)

        jitter = random.uniform(30, 90)
        log.debug(f"Sleeping for {jitter:.2f}s before next send...")
        await asyncio.sleep(jitter)
