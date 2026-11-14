"""Patient SMS notifications (Twilio REST, with a logged mock fallback).

The message never contains the patient's name or any clinical detail: only a hospital line and the
login-free tracker link. If Twilio is not configured, switched off, or fails, the SMS text and link are
logged and returned instead, so Odoo can show them in the chatter and the demo never depends on
cellular delivery.
"""
import logging

import httpx

from config import settings
from schemas import SmsDispatchResult

log = logging.getLogger("clearpath.sms")

TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

MESSAGES = {
    "started": (
        "ClearPath Hospital: Your doctor has completed your discharge review! Track your live Pharmacy, "
        "Billing & Insurance clearance here: {tracking_url}"
    ),
    "ready_for_payment": (
        "ClearPath Hospital: Your Pharmacy, Billing & Insurance checks are complete! Please complete your "
        "AED {copay_amount:.2f} co-pay to unlock your exit pass: {tracking_url}"
    ),
    "released": (
        "ClearPath Hospital: You are cleared to go home. Show your digital exit pass at the ward desk: "
        "{tracking_url}"
    ),
}


def tracker_url(tracking_token: str) -> str:
    return f"{settings.public_odoo_base_url.rstrip('/')}/discharge/track/{tracking_token}"


def build_message(stage_event: str, tracking_token: str, copay_amount: float = 150.0) -> str:
    template = MESSAGES.get(stage_event, MESSAGES["started"])
    return template.format(tracking_url=tracker_url(tracking_token), copay_amount=copay_amount)


def _mask(number: str) -> str:
    """+971501234567 -> +9715*****567 (logs must not carry full phone numbers)."""
    return number if len(number) < 8 else number[:5] + "*" * (len(number) - 8) + number[-3:]


def twilio_enabled() -> bool:
    return bool(
        not settings.use_mock_sms
        and settings.twilio_account_sid
        and settings.twilio_auth_token
        and settings.twilio_from_number
    )


async def send_patient_tracker_sms(
    to_number: str,
    tracking_token: str,
    stage_event: str = "started",
    copay_amount: float = 150.0,
) -> SmsDispatchResult:
    to_number = (to_number or "").strip() or settings.patient_demo_phone
    url = tracker_url(tracking_token)
    body = build_message(stage_event, tracking_token, copay_amount)

    def result(sent: bool, mode: str, sid: str | None = None, error: str = "") -> SmsDispatchResult:
        return SmsDispatchResult(
            sent=sent, mode=mode, to_number=to_number, tracking_url=url, message_preview=body,
            twilio_sid=sid, stage_event=stage_event, error=error,
        )

    if not twilio_enabled():
        log.warning("[MOCK SMS] to %s | %s", _mask(to_number), body)
        return result(False, "mock_logged")

    try:
        async with httpx.AsyncClient(timeout=settings.sms_timeout_seconds) as client:
            resp = await client.post(
                TWILIO_URL.format(sid=settings.twilio_account_sid),
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                data={"To": to_number, "From": settings.twilio_from_number, "Body": body},
            )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"Twilio HTTP {resp.status_code}: {detail}"[:200])
        sid = resp.json().get("sid")
        log.info("SMS sent to %s (%s, sid=%s)", _mask(to_number), stage_event, sid)
        return result(True, "twilio", sid)
    except Exception as exc:  # delivery must never break the clinical flow
        log.error("Twilio send failed (%s); falling back to logged SMS: %s", stage_event, exc)
        log.warning("[MOCK SMS] to %s | %s", _mask(to_number), body)
        return result(False, "twilio_failed_logged", error=str(exc)[:200])
