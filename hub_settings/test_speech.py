from unittest import mock

import requests
from bdh_sdk import speakers
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import speech, vault
from .models import AppSetting, SpeechClip

KEY = b"\x07" * 32
AZURE_SECRET = "azure-speech-key-0123456789abcdef"
GOOGLE_AUDIO = (b"ID3-google", "audio/mpeg")


def _resp(status=200, content=b"ID3-azure", json=None):
    r = mock.Mock(status_code=status, content=content)
    r.json.return_value = json
    return r


class SpeechCase(TestCase):
    def setUp(self):
        vault.set_provider(vault.StaticKeyProvider(("k1", KEY)))
        self.addCleanup(vault.set_provider, None)
        speech.clear_memory()
        speech.clear_voices()
        self.addCleanup(speech.clear_memory)
        # Nothing here may reach the network.
        for target in ("requests.post", "requests.get"):
            patcher = mock.patch(f"hub_settings.speech.{target}", side_effect=AssertionError("network"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.google = mock.Mock(return_value=GOOGLE_AUDIO)
        patcher = mock.patch.dict(speech._ENGINE_FUNCS, {speech.GOOGLE: self.google})
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_azure(self, voice=None):
        AppSetting.objects.create(app_id="hub", key=speech.ENGINE_KEY, value=speech.AZURE)
        if voice:
            AppSetting.objects.create(app_id="hub", key=speech.VOICE_KEY, value=voice)
        vault.set_secret(speech.VAULT_OWNER, speech.AZURE_KEY, AZURE_SECRET)


class SynthTests(SpeechCase):
    def test_installed_as_sdk_synth(self):
        self.assertIs(speakers._synth, speech.synth)

    def test_google_by_default_then_cached(self):
        self.assertEqual(speech.synth(" Dinner   is ready "), GOOGLE_AUDIO)
        self.google.assert_called_once()
        self.assertEqual(self.google.call_args.args[0], "Dinner is ready")
        clip = SpeechClip.objects.get()
        self.assertEqual((clip.engine, clip.voice, bytes(clip.audio)), ("google", "", GOOGLE_AUDIO[0]))
        speech.clear_memory()  # from the database this time
        self.assertEqual(speech.synth("Dinner is ready", "AvaNeural"), GOOGLE_AUDIO)
        self.google.assert_called_once()

    def test_azure_request(self):
        self.use_azure()
        with mock.patch("hub_settings.speech.requests.post", return_value=_resp()) as post:
            self.assertEqual(speech.synth("Fish & chips <now>", "AvaNeural"), (b"ID3-azure", "audio/mpeg"))
        url = post.call_args.args[0]
        self.assertEqual(url, "https://westus3.tts.speech.microsoft.com/cognitiveservices/v1")
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["Ocp-Apim-Subscription-Key"], AZURE_SECRET)
        self.assertEqual(headers["X-Microsoft-OutputFormat"], "audio-24khz-96kbitrate-mono-mp3")
        self.assertEqual(headers["Content-Type"], "application/ssml+xml")
        body = post.call_args.kwargs["data"].decode()
        self.assertIn("xml:lang=\"en-US\"", body)
        self.assertIn("<voice name=\"en-US-AvaNeural\">Fish &amp; chips &lt;now&gt;</voice>", body)
        self.google.assert_not_called()
        self.assertEqual(SpeechClip.objects.get().voice, "en-US-AvaNeural")

    def test_house_voice_beats_app_voice(self):
        self.use_azure(voice="en-GB-SoniaNeural")
        with mock.patch("hub_settings.speech.requests.post", return_value=_resp()) as post:
            speech.synth("hi", "AvaNeural")
        self.assertIn("en-GB-SoniaNeural", post.call_args.kwargs["data"].decode())

    def test_app_voice_names(self):
        self.assertEqual(speech.azure_voice("AvaNeural", None), "en-US-AvaNeural")
        self.assertEqual(speech.azure_voice("en-AU-NatashaNeural", None), "en-AU-NatashaNeural")
        self.assertEqual(speech.azure_voice("bogus", None), speech.DEFAULT_VOICE)
        self.assertEqual(speech.azure_voice(None, None), speech.DEFAULT_VOICE)

    def test_cache_hit_skips_engine(self):
        self.use_azure()
        with mock.patch("hub_settings.speech.requests.post", return_value=_resp()) as post:
            speech.synth("Dinner")
            speech.clear_memory()
            speech.synth("Dinner")
        post.assert_called_once()

    def test_azure_failure_falls_back_to_google(self):
        self.use_azure()
        with mock.patch("hub_settings.speech.requests.post", return_value=_resp(status=401)), \
                self.assertLogs("hub_settings.speech", "WARNING") as logs:
            self.assertEqual(speech.synth("hi"), GOOGLE_AUDIO)
        self.assertIn("falling back to google", "\n".join(logs.output))
        self.assertNotIn(AZURE_SECRET, "\n".join(logs.output))
        with mock.patch("hub_settings.speech.requests.post", side_effect=requests.ConnectionError(AZURE_SECRET)):
            speech.clear_memory()
            SpeechClip.objects.all().delete()
            self.assertEqual(speech.synth("hi"), GOOGLE_AUDIO)

    def test_google_failure_falls_back_to_home_assistant(self):
        self.google.side_effect = RuntimeError("blocked")
        with self.assertRaises(speech.SpeechError):
            speech.synth("hi")
        with mock.patch("bdh_sdk.ha.get_tts", return_value=(b"HA", "audio/mpeg")) as get_tts, \
                mock.patch("bdh_sdk.speakers._engine", return_value="tts.google_en_com"):
            self.assertEqual(speakers.tts_audio("hi"), (b"HA", "audio/mpeg"))
        get_tts.assert_called_once()
        self.assertFalse(SpeechClip.objects.exists())

    def test_ha_engine_returns_none(self):
        AppSetting.objects.create(app_id="hub", key=speech.ENGINE_KEY, value=speech.HA)
        self.assertIsNone(speech.synth("hi"))
        self.google.assert_not_called()

    def test_text_limits(self):
        for bad in ("", "  ", "x" * 301):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                speech.synth(bad)

    def test_prune_drops_old_clips(self):
        speech.synth("old")
        speech.synth("new")
        SpeechClip.objects.filter(text="old").update(last_used=timezone.now() - timezone.timedelta(days=91))
        call_command("speech_prune", stdout=mock.Mock())
        self.assertEqual(list(SpeechClip.objects.values_list("text", flat=True)), ["new"])

    def test_voice_list_filtered_and_cached(self):
        vault.set_secret(speech.VAULT_OWNER, speech.AZURE_KEY, AZURE_SECRET)
        rows = [
            {"ShortName": "en-US-AvaNeural", "DisplayName": "Ava", "Locale": "en-US", "Gender": "Female",
             "VoiceType": "Neural"},
            {"ShortName": "fr-FR-DeniseNeural", "Locale": "fr-FR", "VoiceType": "Neural"},
            {"ShortName": "en-US-Old", "Locale": "en-US", "VoiceType": "Standard"},
        ]
        with mock.patch("hub_settings.speech.requests.get", return_value=_resp(json=rows)) as get:
            self.assertEqual(speech.voices("westus3"), [("en-US-AvaNeural", "Ava (en-US, Female)")])
            speech.voices("westus3")
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0],
                         "https://westus3.tts.speech.microsoft.com/cognitiveservices/voices/list")


class SpeechPageTests(SpeechCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.staff = User.objects.create_user("staff", password="pw", is_staff=True)
        self.guest = User.objects.create_user("guest", password="pw")
        self.url = reverse("hub_settings_speech")
        self.client.force_login(self.staff)
        patcher = mock.patch("bdh_sdk.speakers.list_speakers", return_value=[("media_player.kitchen", "Kitchen (Sonos)")])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_non_staff_refused(self):
        self.client.force_login(self.guest)
        for name in ("hub_settings_speech", "hub_settings_speech_preview", "hub_settings_speech_test"):
            with self.subTest(name=name):
                resp = self.client.post(reverse(name), {"engine": "azure", "key": AZURE_SECRET})
                self.assertEqual(resp.status_code, 302)
        self.assertFalse(vault.names(speech.VAULT_OWNER))

    def test_save_azure_key_never_rendered(self):
        resp = self.client.post(self.url, {"engine": "azure", "region": "westus3", "voice": "en-US-AvaNeural",
                                           "key": AZURE_SECRET})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(vault.get_secret(speech.VAULT_OWNER, speech.AZURE_KEY), AZURE_SECRET)
        self.assertEqual(speech.config(), {"engine": "azure", "region": "westus3", "voice": "en-US-AvaNeural"})
        with mock.patch("hub_settings.speech.requests.get", return_value=_resp(status=500)):
            page = self.client.get(self.url).content.decode()
        self.assertNotIn(AZURE_SECRET, page)
        self.assertIn("saved", page)
        self.assertIn("Kitchen (Sonos)", page)

    def test_azure_needs_key_and_bad_values_refused(self):
        resp = self.client.post(self.url, {"engine": "azure", "region": "West US!", "voice": "nope"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(set(resp.context["errors"]), {"key", "region", "voice"})
        self.assertFalse(AppSetting.objects.filter(app_id="hub").exists())

    def test_clear_key(self):
        vault.set_secret(speech.VAULT_OWNER, speech.AZURE_KEY, AZURE_SECRET)
        self.client.post(self.url, {"clear_key": "1"})
        self.assertFalse(speech.key_saved())

    def test_preview_plays_saved_engine(self):
        resp = self.client.post(reverse("hub_settings_speech_preview"), {"text": "Testing"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, GOOGLE_AUDIO[0])
        self.assertEqual(resp["X-Speech-Engine"], "google")

    def test_speaker_test(self):
        with mock.patch("bdh_sdk.speakers.announce", return_value=[]) as announce:
            resp = self.client.post(reverse("hub_settings_speech_test"),
                                    {"text": "Testing", "speaker": "media_player.kitchen"})
            self.assertTrue(resp.json()["ok"])
            announce.assert_called_once_with("Testing", ["media_player.kitchen"])
            resp = self.client.post(reverse("hub_settings_speech_test"), {"speaker": "media_player.elsewhere"})
            self.assertEqual(resp.status_code, 400)
            announce.assert_called_once()
