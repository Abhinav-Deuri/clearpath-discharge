"""Doctor voice notes (ASR) and nurse supply-sheet photos (vision) -> structured text/data.

Live calls use OpenAI-compatible endpoints configured via ASR_* / VISION_* settings.
Anything unconfigured, or any live failure, falls back to a deterministic PT-402 demo result
so a demo never hangs. Audio/image bytes are never logged.
"""
import base64
import binascii
import io
import asyncio
import json
import logging
import re
import wave

import httpx

from config import settings
from schemas import SupplyItem

log = logging.getLogger("clearpath.multimodal")

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024

DEMO_TRANSCRIPT = (
    "Bed 402, Arabith, post-op day one after laparoscopic appendectomy. Uncomplicated course, "
    "afebrile, pain controlled on oral analgesia, tolerating a regular diet and ambulating "
    "independently. Incisions clean and dry. Plan: discharge home today. Start Amoxicillin 500mg "
    "three times daily for five days as post-operative prophylaxis. Surgical clinic follow-up in two weeks."
)

DEMO_SUPPLIES = [
    SupplyItem(item_name="Hemostatic Gauze Pad", surgical_qty_used=4, billed_qty=2),
    SupplyItem(item_name="Skin Stapler", surgical_qty_used=1, billed_qty=0),
]

_VISION_PROMPT = (
    "This is a photo of a nurse's bedside surgical supply tally sheet. Extract every supply item "
    "and the quantity used in surgery. Respond ONLY with JSON: "
    '{"items": [{"item_name": "<name>", "surgical_qty_used": <integer>}]}. '
    "Do not guess items that are not legible."
)


def decode_payload(data_b64: str, max_bytes: int, what: str) -> bytes:
    """Decode (optionally data-URI) base64 and enforce a size cap."""
    if "," in data_b64[:100] and data_b64.lstrip().startswith("data:"):
        data_b64 = data_b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{what} is not valid base64")
    if not raw:
        raise ValueError(f"{what} is empty")
    if len(raw) > max_bytes:
        raise ValueError(f"{what} is too large (max {max_bytes // (1024 * 1024)} MB)")
    return raw


def _riva_live() -> bool:
    return (not settings.use_mock_llm) and bool(settings.asr_riva_function_id and settings.asr_api_key)


def _asr_live() -> bool:
    return (not settings.use_mock_llm) and bool(
        settings.asr_base_url and settings.asr_api_key and settings.asr_model_name)


def _riva_transcribe_sync(audio: bytes) -> str:
    """Blocking NVIDIA Riva (hosted Parakeet) offline recognition. Needs PCM WAV."""
    import riva.client as rc  # lazy: only needed when Riva is configured

    auth = rc.Auth(
        uri=settings.asr_riva_server, use_ssl=True,
        metadata_args=[["function-id", settings.asr_riva_function_id],
                       ["authorization", f"Bearer {settings.asr_api_key}"]],
    )
    cfg = rc.RecognitionConfig(language_code=settings.asr_language_code, max_alternatives=1,
                               enable_automatic_punctuation=True)
    with wave.open(io.BytesIO(audio)) as wav:  # raises wave.Error for non-WAV uploads
        cfg.sample_rate_hertz = wav.getframerate()
        cfg.audio_channel_count = wav.getnchannels()
    resp = rc.ASRService(auth).offline_recognize(audio, cfg)
    return " ".join(r.alternatives[0].transcript.strip()
                    for r in resp.results if r.alternatives).strip()


def _vision_live() -> bool:
    return (not settings.use_mock_llm) and bool(
        settings.vision_base_url and settings.vision_api_key and settings.vision_model_name)


async def transcribe_doctor_voice_note(audio_base64: str, filename: str) -> str:
    """Audio -> text. Live: NVIDIA Riva gRPC (WAV) if ASR_RIVA_FUNCTION_ID set, else Whisper-style REST."""
    audio = decode_payload(audio_base64, MAX_AUDIO_BYTES, "Audio")
    if _riva_live():
        try:
            text = await asyncio.wait_for(asyncio.to_thread(_riva_transcribe_sync, audio),
                                          timeout=settings.ai_timeout_seconds * 3)
            if text:
                return text
            log.warning("Riva ASR returned empty text; using demo transcript")
        except Exception as exc:  # never hang the demo
            log.warning("Riva ASR failed (%s); using demo transcript", type(exc).__name__)
    elif _asr_live():
        try:
            async with httpx.AsyncClient(timeout=settings.ai_timeout_seconds * 3) as client:
                resp = await client.post(
                    settings.asr_base_url.rstrip("/") + "/audio/transcriptions",
                    headers={"Authorization": f"Bearer {settings.asr_api_key}"},
                    data={"model": settings.asr_model_name},
                    files={"file": (filename or "voice_note.wav", audio)},
                )
                resp.raise_for_status()
            text = (resp.json().get("text") or "").strip()
            if text:
                return text
            log.warning("ASR returned empty text; using demo transcript")
        except Exception as exc:  # never hang the demo
            log.warning("Live ASR failed (%s); using demo transcript", type(exc).__name__)
    return DEMO_TRANSCRIPT


def _mime_for(image: bytes) -> str:
    if image.startswith(b"\x89PNG"):
        return "image/png"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


async def extract_supply_checklist_from_image(image_base64: str) -> list[SupplyItem]:
    """Photo of a tally sheet -> supply counts. Live: vision chat/completions with image_url."""
    image = decode_payload(image_base64, MAX_IMAGE_BYTES, "Image")
    if _vision_live():
        try:
            data_uri = f"data:{_mime_for(image)};base64,{base64.b64encode(image).decode()}"
            body = {
                "model": settings.vision_model_name,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ]}],
                "temperature": 0,
                "max_tokens": 2000,
            }
            if "nvidia.com" in settings.vision_base_url:  # skip reasoning trace: faster, content never empty
                body["chat_template_kwargs"] = {"enable_thinking": False}
            async with httpx.AsyncClient(timeout=settings.ai_timeout_seconds * 2) as client:
                resp = await client.post(
                    settings.vision_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {settings.vision_api_key}"},
                    json=body,
                )
                resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
            parsed = json.loads(fence.group(1) if fence else content)
            items = [
                SupplyItem(item_name=str(i["item_name"]).strip(),
                           surgical_qty_used=max(int(i["surgical_qty_used"]), 0), billed_qty=0)
                for i in parsed.get("items", []) if str(i.get("item_name", "")).strip()
            ]
            if items:
                return items
            log.warning("Vision returned no items; using demo supplies")
        except Exception as exc:
            log.warning("Live vision failed (%s); using demo supplies", type(exc).__name__)
    return [s.model_copy() for s in DEMO_SUPPLIES]
