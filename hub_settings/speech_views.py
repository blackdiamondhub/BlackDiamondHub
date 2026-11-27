"""Settings → Speech: the voice the hub speaks announcements and alerts in.

The Azure key is write-only, like Home Assistant tokens: the page only says
whether one is saved. See :mod:`hub_settings.speech` for the engines, the
fallbacks and the clip cache.
"""
from django.http import HttpResponse, JsonResponse
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_POST

from . import speech, vault
from .models import AppSetting, SpeechClip
from .views import _render, saved_redirect, staff_page

SAMPLE = "Hello from the Black Diamond Hub. Dinner is ready."
TEST_SPEAKER_LIMIT = 200


def _save(key, value, user):
    if value is None:
        AppSetting.objects.filter(app_id="hub", key=key).delete()
    else:
        AppSetting.objects.update_or_create(app_id="hub", key=key, defaults={"value": value, "updated_by": user})


def _speakers():
    from bdh_sdk import speakers

    try:
        return speakers.list_speakers()
    except Exception:  # HA not set up or unreachable: no speakers to test on
        return []


def _page(request, errors=None, posted=None, status=200, message=""):
    cfg = speech.config()
    posted = posted or {}
    key_set = speech.key_saved()
    voices = speech.voices(cfg["region"]) if key_set else []
    voice = posted.get("voice") or cfg["voice"] or speech.DEFAULT_VOICE
    if voice not in {v for v, _label in voices}:
        voices = [(voice, voice)] + list(voices)
    return _render(
        request, "hub_settings/speech.html", "speech",
        engines=list(speech.ENGINES.items()), engine=posted.get("engine", cfg["engine"]),
        region=posted.get("region", cfg["region"]), voice=voice, voices=voices, voice_saved=bool(cfg["voice"]),
        key_set=key_set, vault_ok=vault.available(), speakers=_speakers(), sample=SAMPLE,
        clip_count=SpeechClip.objects.count(), errors=errors or {}, has_errors=bool(errors) or bool(message),
        message=message, just_saved=request.GET.get("saved") == "1", status=status,
    )


@staff_page
@sensitive_post_parameters()
@sensitive_variables("key")
def speech_settings(request):
    if request.method != "POST":
        return _page(request)
    post = request.POST
    if post.get("clear_key"):
        vault.delete_secret(speech.VAULT_OWNER, speech.AZURE_KEY)
        speech.clear_voices()
        return saved_redirect(request, f"{request.path}?saved=1")

    errors = {}
    engine = post.get("engine", "")
    if engine not in speech.ENGINES:
        errors["engine"] = "Pick an engine."
    region = post.get("region", "").strip().lower() or speech.DEFAULT_REGION
    if not speech.REGION_RE.match(region):
        errors["region"] = "An Azure region name, like westus3."
    voice = post.get("voice", "").strip()
    if voice and not speech.VOICE_RE.match(voice):
        errors["voice"] = "An Azure voice name, like en-US-AvaNeural."
    key = post.get("key", "").strip()
    if key:
        from bdh_sdk.secrets import check_value

        if not vault.available():
            errors["key"] = "The secret store has no key on this hub; see the README."
        else:
            try:
                check_value(key)
            except ValueError as exc:
                errors["key"] = str(exc)
    if engine == speech.AZURE and not key and not speech.key_saved():
        errors.setdefault("key", "Azure Speech needs a key.")
    posted = {"engine": engine, "region": region, "voice": voice}
    if errors:
        return _page(request, errors, posted, status=400)

    _save(speech.ENGINE_KEY, engine, request.user)
    _save(speech.REGION_KEY, None if region == speech.DEFAULT_REGION else region, request.user)
    _save(speech.VOICE_KEY, voice or None, request.user)
    if key:
        vault.set_secret(speech.VAULT_OWNER, speech.AZURE_KEY, key, user=request.user)
    speech.clear_voices()
    return saved_redirect(request, f"{request.path}?saved=1")


def _text(request):
    text = speech.normalize(request.POST.get("text", "")) or SAMPLE
    if len(text) > speech.max_text():
        raise ValueError(f"At most {speech.max_text()} characters.")
    return text


@staff_page
@require_POST
def speech_preview(request):
    """The saved settings speaking ``text``, for the page's Play button."""
    from bdh_sdk import ha, speakers

    try:
        text = _text(request)
        out = speech.speak(text)
        if out is None:
            audio, content_type = speakers.tts_audio(text)
            engine = "ha"
        else:
            audio, content_type, engine = out
    except ValueError as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)
    except (speech.SpeechError, ha.HAError):
        return JsonResponse({"ok": False, "message": "No speech engine worked; see the hub log."}, status=502)
    response = HttpResponse(audio, content_type=content_type)
    response["X-Speech-Engine"] = engine
    response["Cache-Control"] = "no-store"
    return response


@staff_page
@require_POST
def speech_test(request):
    """Speak ``text`` on one speaker with the saved settings."""
    from bdh_sdk import speakers

    speaker = request.POST.get("speaker", "")[:TEST_SPEAKER_LIMIT]
    if speaker not in {eid for eid, _label in _speakers()}:
        return JsonResponse({"ok": False, "message": "Pick a speaker."}, status=400)
    try:
        text = _text(request)
        failed = speakers.announce(text, [speaker])
    except ValueError as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)
    except speakers.AnnounceError as exc:
        return JsonResponse({"ok": False, "message": f"Couldn't play it: {exc}"}, status=502)
    if failed:
        return JsonResponse({"ok": False, "message": "That speaker didn't play it."}, status=502)
    return JsonResponse({"ok": True, "message": "Sent to the speaker."})
