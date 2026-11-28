import asyncio
import struct
import time
from unittest import mock

from bdh_sdk import ha
from bdh_sdk import speakers as sdk
from django.test import SimpleTestCase, TestCase, override_settings

from . import speakers as hub
from .models import AppSetting, Speaker, SpeechClip
from .speakers import audio, clips, inventory, sonos

# One MPEG-2 layer III frame, 24 kHz mono 64 kbps: 192 bytes, 576 samples (24 ms).
FRAME_24K = b"\xff\xf3\x84\xc0" + bytes(188)
# One MPEG-1 layer III frame, 44.1 kHz stereo 128 kbps: 417 bytes.
FRAME_44K = b"\xff\xfb\x90\x00" + bytes(413)
UID = "RINCON_000E58A0000001400"


def speech(frames=50):
    return b"ID3\x04\x00\x00\x00\x00\x00\x00" + FRAME_24K * frames


def wav(seconds, byte_rate=100):
    data = bytes(int(seconds * byte_rate))
    fmt = struct.pack("<HHIIHH", 1, 1, byte_rate, byte_rate, 1, 8)
    return (b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data)


class AudioTests(SimpleTestCase):
    def test_duration_strips_tags_and_info_frame(self):
        info = b"\xff\xf3\x84\xc0" + bytes(9) + b"Info" + bytes(175)
        mp3 = audio.parse_mp3(speech(0) + info + FRAME_24K * 50 + b"TAG" + bytes(125))
        self.assertEqual((mp3.rate, mp3.channels), (24000, 1))
        self.assertAlmostEqual(mp3.duration, 1.2)
        self.assertEqual(mp3.frames, FRAME_24K * 50)

    def test_join_and_mismatch(self):
        joined = audio.join(speech(25), speech(50))
        self.assertAlmostEqual(joined.duration, 1.8)
        self.assertEqual(joined.frames, FRAME_24K * 75)
        self.assertIsNone(audio.join(speech(10), FRAME_44K * 10))

    def test_chimes_join_speech(self):
        for chime in ("ding-dong", "westminster", "single-ding"):
            mp3 = audio.parse_mp3(hub.chime_bytes(chime))
            self.assertEqual((mp3.rate, mp3.channels), (24000, 1), chime)
            self.assertTrue(0.3 < mp3.duration < 6, chime)
            self.assertIsNotNone(audio.join(mp3, speech()), chime)

    def test_sniff_and_wav(self):
        self.assertEqual(audio.sniff(speech()), audio.MP3)
        self.assertEqual(audio.sniff(FRAME_44K), audio.MP3)
        self.assertEqual(audio.sniff(wav(1)), audio.WAV)
        self.assertIsNone(audio.sniff(b"OggS" + bytes(20)))
        self.assertAlmostEqual(audio.duration(wav(2.5)), 2.5)
        with self.assertRaises(audio.AudioError):
            audio.parse_mp3(b"ID3\x03" + bytes(6) + b"junk")


class ClipTests(TestCase):
    def setUp(self):
        from . import speech as hub_speech

        hub_speech.clear_memory()
        self.addCleanup(hub_speech.clear_memory)
        self.key = "a" * 64
        hub._store(self.key, SpeechClip.ANNOUNCE, b"0123456789", "mp3", "hi")

    def test_signatures(self):
        now = 1_000_000
        url = clips.path(self.key, "mp3", ttl=60, now=now)
        exp, sig = url.split("exp=")[1].split("&sig=")
        self.assertTrue(clips.check(self.key, "mp3", exp, sig, now=now))
        self.assertFalse(clips.check(self.key, "mp3", exp, sig, now=now + 61))  # expired
        self.assertFalse(clips.check("b" * 64, "mp3", exp, sig, now=now))  # other clip
        self.assertFalse(clips.check(self.key, "wav", exp, sig, now=now))
        self.assertFalse(clips.check(self.key, "mp3", int(exp) + 1, sig, now=now))  # later expiry
        self.assertFalse(clips.check(self.key, "mp3", exp, sig[:-1] + "0", now=now))
        with override_settings(SECRET_KEY="another-hub"):
            self.assertFalse(clips.check(self.key, "mp3", exp, sig, now=now))

    @override_settings(ALLOWED_HOSTS=["hub.example"])
    def test_served_to_any_host_without_a_session(self):
        url = clips.path(self.key, "mp3")
        resp = self.client.get(url, HTTP_HOST="192.168.1.58:8000")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"0123456789")
        self.assertEqual((resp["Content-Type"], resp["X-Content-Type-Options"]), ("audio/mpeg", "nosniff"))
        self.assertEqual(resp["Content-Length"], "10")
        self.assertFalse(resp.cookies)
        self.assertEqual(self.client.get(url.replace("sig=", "sig=0"), HTTP_HOST="x").status_code, 403)
        self.assertEqual(self.client.get(clips.path("c" * 64, "mp3"), HTTP_HOST="x").status_code, 404)
        self.assertEqual(self.client.get(clips.PREFIX + "x.mp3", HTTP_HOST="x").status_code, 404)
        self.assertEqual(self.client.post(url, HTTP_HOST="x").status_code, 405)
        # Other paths still get the host check.
        self.assertEqual(self.client.get("/", HTTP_HOST="192.168.1.58:8000").status_code, 400)

    def test_ranges_and_head(self):
        url = clips.path(self.key, "mp3")
        resp = self.client.get(url, HTTP_RANGE="bytes=2-5")
        self.assertEqual((resp.status_code, resp.content, resp["Content-Range"]), (206, b"2345", "bytes 2-5/10"))
        resp = self.client.get(url, HTTP_RANGE="bytes=-3")
        self.assertEqual((resp.status_code, resp.content), (206, b"789"))
        resp = self.client.get(url, HTTP_RANGE="bytes=8-")
        self.assertEqual(resp.content, b"89")
        resp = self.client.get(url, HTTP_RANGE="bytes=10-")
        self.assertEqual((resp.status_code, resp["Content-Range"]), (416, "bytes */10"))
        resp = self.client.head(url)
        self.assertEqual((resp.status_code, resp.content, resp["Content-Length"]), (200, b"", "10"))


class InventoryCase(TestCase):
    def setUp(self):
        inventory.reset()
        self.addCleanup(inventory.reset)
        self.rooms = [{"uid": UID, "name": "Kitchen", "address": "10.0.0.58"}]
        self.entries = [
            {"entity_id": "media_player.kitchen", "platform": "sonos", "unique_id": UID},
            {"entity_id": "media_player.echo", "platform": "alexa_media", "unique_id": "e"},
            {"entity_id": "media_player.tv", "platform": "cast", "unique_id": "t"},
            {"entity_id": "media_player.radio", "platform": "dlna", "unique_id": "r"},
        ]
        self.states = [
            {"entity_id": "media_player.kitchen", "state": "idle", "attributes": {"friendly_name": "Kitchen"}},
            {"entity_id": "media_player.echo", "state": "idle", "attributes": {"friendly_name": "Echo"}},
            {"entity_id": "media_player.tv", "state": "off",
             "attributes": {"friendly_name": "TV", "supported_features": 512 | 1048576}},
            {"entity_id": "media_player.radio", "state": "off",
             "attributes": {"friendly_name": "Radio", "supported_features": 512}},
        ]
        self.ha_up = True
        self.topology = mock.patch.object(sonos, "topology", side_effect=lambda seeds: self.rooms)
        self.topology.start()
        self.addCleanup(self.topology.stop)
        for target, fn in (("capabilities", mock.AsyncMock(return_value={UID: ["PLAYBACK", "AUDIO_CLIP"]})),
                           ("lan_address", mock.Mock(return_value="10.0.0.2"))):
            patcher = mock.patch.object(sonos, target, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        patchers = [
            mock.patch("hub_settings.ha.instances", return_value=[{"id": 1, "name": "HA", "site": "", "default": True}]),
            mock.patch("bdh_sdk.ha.ws_commands", side_effect=self._ws),
            mock.patch("bdh_sdk.ha.get_all_states", side_effect=self._states),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _ws(self, *messages, instance=None):
        if not self.ha_up:
            raise ha.HAError("down")
        return [self.entries]

    def _states(self, instance=None):
        if not self.ha_up:
            raise ha.HAError("down")
        return self.states


class InventoryTests(InventoryCase):
    def test_refresh_merges_sonos_and_ha(self):
        inventory.refresh(force=True)
        rows = {r.native_id: r for r in Speaker.objects.all()}
        self.assertEqual(set(rows), {f"sonos:{UID}", "alexa:1:media_player.echo", "ha:1:media_player.tv"})
        kitchen = rows[f"sonos:{UID}"]
        self.assertEqual((kitchen.legacy_id, kitchen.address, kitchen.ha_entity),
                         ("media_player.kitchen", "10.0.0.58", "media_player.kitchen"))
        self.assertTrue(kitchen.caps["audio_clip"])
        # list_speakers keeps the ids apps saved; devices() gives native ids.
        self.assertIn(("media_player.kitchen", "Kitchen (Sonos)"), inventory.pairs())
        devices = {d["id"]: d for d in inventory.devices()}
        self.assertEqual(devices[f"sonos:{UID}"]["legacy_id"], "media_player.kitchen")
        self.assertEqual(devices[f"sonos:{UID}"]["caps"], {"audio": True, "duck": True, "text": False, "volume": True})
        self.assertEqual(devices["alexa:1:media_player.echo"]["caps"]["text"], True)

    def test_ids_resolve_both_ways_and_survive_ha_and_lan_going_away(self):
        inventory.refresh(force=True)
        self.ha_up, self.rooms = False, None
        inventory.refresh(force=True)
        got = inventory.resolve(["media_player.kitchen", f"sonos:{UID}", "alexa:1:media_player.echo", "nope"])
        self.assertEqual(got["media_player.kitchen"].native_id, f"sonos:{UID}")
        self.assertEqual(got[f"sonos:{UID}"].legacy_id, "media_player.kitchen")
        self.assertEqual(got["alexa:1:media_player.echo"].legacy_id, "media_player.echo")
        self.assertIsNone(got["nope"])
        self.assertTrue(Speaker.objects.get(native_id=f"sonos:{UID}").online)

    def test_room_gone_from_the_lan_goes_offline(self):
        inventory.refresh(force=True)
        self.rooms = [{"uid": "RINCON_OTHER", "name": "Deck", "address": "10.0.0.87"}]
        self.entries = self.entries[1:]
        inventory.refresh(force=True)
        self.assertFalse(Speaker.objects.get(native_id=f"sonos:{UID}").online)


def row(kind, online=True, **kw):
    native = {"sonos": f"sonos:{UID}", "alexa": "alexa:1:media_player.echo", "ha": "ha:1:media_player.tv"}[kind]
    return Speaker(native_id=native, kind=kind, name=kind, online=online, **kw)


class PlanTests(SimpleTestCase):
    def test_routes(self):
        clip = {"audio_clip": True}
        resolved = {
            "a": row("sonos", caps=clip, address="10.0.0.58"),
            "b": row("sonos", caps={}, address="10.0.0.58", ha_instance="1", ha_entity="media_player.k"),
            "c": row("sonos", caps={}, address="10.0.0.58"),
            "d": row("sonos", online=False, caps={}),
            "e": row("alexa", ha_instance="1", ha_entity="media_player.echo"),
            "f": row("alexa", online=False),
            "g": row("ha", ha_instance="1", ha_entity="media_player.tv"),
            "h": row("ha", online=False),
            "i": None,
        }
        jobs, statuses = hub.plan(resolved, "hello")
        self.assertEqual({j.id: j.route for j in jobs}, {"a": "sonos", "b": "ha_media", "e": "alexa", "g": "ha_media"})
        self.assertEqual(statuses, {"c": sdk.UNSUPPORTED, "d": sdk.OFFLINE, "f": sdk.OFFLINE, "h": sdk.OFFLINE,
                                    "i": sdk.FAILED})
        # Alexa can't play audio.
        jobs, statuses = hub.plan({"e": resolved["e"]}, None)
        self.assertEqual((jobs, statuses), ([], {"e": sdk.UNSUPPORTED}))


class SonosFallbackTests(SimpleTestCase):
    def job(self, ha=True):
        r = row("sonos", caps={"audio_clip": True}, address="10.0.0.58",
                **({"ha_instance": "1", "ha_entity": "media_player.kitchen"} if ha else {}))
        job = hub.Job("x", r, "sonos")
        job.urls = [("http://hub/clip", 1.0)]
        return job

    def run_job(self, job, play_clip):
        call = mock.Mock(return_value=(True, None))
        with mock.patch.object(sonos, "play_clip", play_clip), mock.patch.object(hub, "_call_service", call):
            return asyncio.run(hub._sonos_play(job, 30)), call

    def test_unreachable_falls_back_to_ha(self):
        job = self.job()
        status, call = self.run_job(job, mock.AsyncMock(side_effect=sonos.NotSent("refused")))
        self.assertEqual((status, job.path), (sdk.PLAYED, "sonos>ha"))
        self.assertEqual(call.call_args.args[:3], ("media_player", "play_media", ["media_player.kitchen"]))
        self.assertEqual(call.call_args.args[3]["media_content_id"], "http://hub/clip")
        self.assertEqual(call.call_args.args[3]["extra"], {"volume": 30})

    def test_sent_but_unconfirmed_is_not_retried(self):
        status, call = self.run_job(self.job(), mock.AsyncMock(side_effect=TimeoutError()))
        self.assertEqual(status, sdk.FAILED)
        call.assert_not_called()

    def test_unreachable_without_ha_is_offline(self):
        status, call = self.run_job(self.job(ha=False), mock.AsyncMock(side_effect=sonos.NotSent("x")))
        self.assertEqual(status, sdk.OFFLINE)
        call.assert_not_called()


class GateTests(SimpleTestCase):
    def test_holds_for_the_clip_and_bounds_the_queue(self):
        async def main():
            gate = hub.Gate()
            await gate.acquire(1)
            start = time.monotonic()
            gate.hold(0.3)
            waiters = [asyncio.create_task(gate.acquire(5)) for _ in range(hub.MAX_WAITING)]
            await asyncio.sleep(0)
            with self.assertRaises(hub.Busy):
                await gate.acquire(5)
            await waiters[0]
            waited = time.monotonic() - start
            for w in waiters[1:]:
                w.cancel()
            return waited

        self.assertGreaterEqual(asyncio.run(main()), 0.28)

    def test_wait_budget(self):
        async def main():
            gate = hub.Gate()
            await gate.acquire(1)
            with self.assertRaises(hub.Busy):
                await gate.acquire(0.05)

        asyncio.run(main())


class BackendTests(InventoryCase):
    def setUp(self):
        super().setUp()
        from . import speech as hub_speech

        hub_speech.clear_memory()
        self.addCleanup(hub_speech.clear_memory)
        for name, value in (("gate", hub.Gate()), ("night_now", mock.Mock(return_value=False))):
            patcher = mock.patch.object(hub, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.play_clip = mock.AsyncMock(return_value={})
        self.call = mock.Mock(return_value=(True, None))
        for patcher in (mock.patch.object(sonos, "play_clip", self.play_clip),
                        mock.patch.object(hub, "_call_service", self.call),
                        mock.patch.object(hub, "HOLD_MARGIN", -60),
                        mock.patch("bdh_sdk.speakers.ha_tts_audio", return_value=(speech(), "audio/mpeg"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        inventory.refresh(force=True)

    def fetch(self, url):
        resp = self.client.get(url[url.index(clips.PREFIX):], HTTP_HOST="10.0.0.2:8000")
        self.assertEqual(resp.status_code, 200)
        return resp.content

    def test_installed(self):
        self.assertIsInstance(sdk._backend, hub.HubSpeakers)

    def test_announce_detail(self):
        result = sdk.announce("Dinner is ready", ["media_player.kitchen", "alexa:1:media_player.echo",
                                                  "media_player.tv", "nope"], volume=40, chime="ding-dong",
                              detail=True)
        self.assertEqual(result, {"media_player.kitchen": sdk.PLAYED, "alexa:1:media_player.echo": sdk.PLAYED,
                                  "media_player.tv": sdk.PLAYED, "nope": sdk.FAILED})
        uid, address, url, volume = self.play_clip.call_args.args
        self.assertEqual((uid, address, volume), (UID, "10.0.0.58", 40))
        self.assertTrue(url.startswith("http://10.0.0.2:8000/speech/clip/"))
        # One clip: the chime and the speech joined.
        joined = audio.parse_mp3(self.fetch(url))
        chime = audio.parse_mp3(hub.chime_bytes("ding-dong"))
        self.assertAlmostEqual(joined.duration, chime.duration + 1.2, places=3)
        services = {c.args[:2]: c for c in self.call.call_args_list}
        self.assertEqual(services[("notify", "alexa_media")].args[3]["message"], "Dinner is ready")
        tv = services[("media_player", "play_media")]
        self.assertEqual(tv.args[2], ["media_player.tv"])
        self.assertEqual(tv.args[3]["media_content_id"], url)

    def test_announce_legacy_list_result(self):
        self.play_clip.side_effect = RuntimeError("rejected")
        self.assertEqual(sdk.announce("Hi", ["media_player.kitchen", "media_player.tv"]), ["media_player.kitchen"])
        with self.assertRaises(sdk.AnnounceError):
            sdk.announce("Hi", ["media_player.kitchen"])

    def test_lan_base_override(self):
        AppSetting.objects.create(app_id="hub", key=hub.LAN_BASE_KEY, value="http://hub.lan:9000")
        sdk.announce("Hi", ["media_player.kitchen"])
        self.assertTrue(self.play_clip.call_args.args[2].startswith("http://hub.lan:9000/speech/clip/"))

    def test_play_checks(self):
        with self.assertRaisesRegex(ValueError, "MP3 or WAV"):
            sdk.play(b"OggS" + bytes(100), ["media_player.kitchen"])
        with self.assertRaisesRegex(ValueError, "2 MB"):
            hub.HubSpeakers().play(speech() + bytes(sdk.MAX_PLAY_BYTES), ["media_player.kitchen"])
        with self.assertRaisesRegex(ValueError, "60 seconds"):
            sdk.play(wav(61), ["media_player.kitchen"])
        self.play_clip.assert_not_called()
        hub.night_now.return_value = True
        with self.assertRaisesRegex(sdk.AnnounceError, "night"):
            sdk.play(wav(1), ["media_player.kitchen"])
        self.play_clip.assert_not_called()
        self.assertEqual(sdk.play(wav(1), ["media_player.kitchen"], volume=95, night=True, detail=True),
                         {"media_player.kitchen": sdk.PLAYED})
        self.assertEqual(self.play_clip.call_args.args[3], hub.DEFAULT_MAX_VOLUME)
        url = self.play_clip.call_args.args[2]
        self.assertIn(".wav?", url)
        self.assertEqual(self.fetch(url), wav(1))
        self.assertEqual(SpeechClip.objects.get(kind=SpeechClip.CUSTOM).content_type, "audio/wav")

    def test_play_skips_alexa(self):
        result = sdk.play(wav(1), ["alexa:1:media_player.echo"], detail=True)
        self.assertEqual(result, {"alexa:1:media_player.echo": sdk.UNSUPPORTED})
