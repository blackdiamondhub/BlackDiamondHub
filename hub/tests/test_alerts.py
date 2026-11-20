import asyncio
import importlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from asgiref.sync import async_to_sync
from bdh_sdk import ha
from bdh_sdk.alerts import Alert, dismiss, push
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, tag
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from broker.tests.base import make_install, reset_broker
from hub import alerts
from tests.selenium_helpers import SeleniumTestCase, wait_for_paint

HEADERS = [(b"origin", b"http://testserver"), (b"host", b"testserver")]


def doorbell(**kw):
    return Alert(**{"app": "cameras", "key": "doorbell:front", "title": "Front door",
                    "embed_url": "/cameras/popup/front/", "ttl": 45, **kw})


class AlertSocketTests(SimpleTestCase):
    def setUp(self):
        import BlackDiamondHub.asgi as asgi

        self.application = importlib.reload(asgi).application
        self.addCleanup(setattr, alerts, "_loop", None)

    def test_push_from_another_thread_reaches_connected_screens(self):
        async def run():
            screens = [WebsocketCommunicator(self.application, "/ws/hub/alerts/", headers=HEADERS)
                       for _ in range(2)]
            for comm in screens:
                connected, _ = await comm.connect()
                self.assertTrue(connected)
            # Apps push from their own worker threads, not the server loop.
            await asyncio.to_thread(push, doorbell(body="Someone is at the door"))
            received = [await comm.receive_json_from(timeout=2) for comm in screens]
            for comm in screens:
                await comm.disconnect()
            return received

        for msg in async_to_sync(run)():
            self.assertEqual(msg, {
                "type": "alert", "app": "cameras", "key": "doorbell:front", "title": "Front door",
                "body": "Someone is at the door", "embed_url": "/cameras/popup/front/",
                "ttl": 45, "wake": True, "sound": None, "speak": "", "resound": False,
            })

    def test_dismiss_reaches_connected_screens(self):
        async def run():
            comm = WebsocketCommunicator(self.application, "/ws/hub/alerts/", headers=HEADERS)
            await comm.connect()
            await asyncio.to_thread(dismiss, "timers", "timer:3")
            msg = await comm.receive_json_from(timeout=2)
            await comm.disconnect()
            return msg

        self.assertEqual(async_to_sync(run)(), {"type": "dismiss", "app": "timers", "key": "timer:3"})

    def test_push_with_no_screens_connected_is_dropped(self):
        alerts._loop = None
        with mock.patch("hub.alerts.asyncio.run_coroutine_threadsafe") as send:
            push(doorbell())
        send.assert_not_called()


class AlertTestHookTests(TestCase):
    def test_requires_staff(self):
        self.client.force_login(User.objects.create_user("guest"))
        with mock.patch("bdh_sdk.alerts.push") as sent:
            response = self.client.get("/hub/api/alerts/test/")
        self.assertEqual(response.status_code, 302)
        sent.assert_not_called()

    def test_staff_can_push_a_sample(self):
        self.client.force_login(User.objects.create_user("owner", is_staff=True))
        with mock.patch("bdh_sdk.alerts.push") as sent:
            response = self.client.get("/hub/api/alerts/test/?ttl=10")
        self.assertEqual(response.json(), {"ok": True})
        alert = sent.call_args.args[0]
        self.assertEqual((alert.key, alert.ttl, alert.embed_url),
                         ("hub:test", 10, "/hub/api/alerts/sample/"))
        self.assertEqual(self.client.get(alert.embed_url).status_code, 200)

    def test_rejects_an_off_site_embed(self):
        self.client.force_login(User.objects.create_user("owner", is_staff=True))
        response = self.client.get("/hub/api/alerts/test/?embed=//evil.example/")
        self.assertEqual(response.status_code, 400)


class TtsTests(TestCase):
    @mock.patch("bdh_sdk.speakers.tts_audio", return_value=(b"ID3audio", "audio/mpeg"))
    def test_serves_spoken_audio(self, tts_audio):
        response = self.client.get("/hub/api/tts/", {"text": "Egg timer has expired"})
        self.assertEqual((response.status_code, response["Content-Type"], response.content),
                         (200, "audio/mpeg", b"ID3audio"))
        self.assertIn("max-age", response["Cache-Control"])
        tts_audio.assert_called_once_with("Egg timer has expired", voice=None)

    def test_bad_text_or_ha_down(self):
        self.assertEqual(self.client.get("/hub/api/tts/", {"text": "x" * 301}).status_code, 400)
        with mock.patch("bdh_sdk.speakers.tts_audio", side_effect=ha.HAError("down")):
            self.assertEqual(self.client.get("/hub/api/tts/", {"text": "hi"}).status_code, 502)


@tag("selenium")
class AlertKeepOpenTest(SeleniumTestCase):
    SHOWN = (By.CSS_SELECTOR, ".hub-alert.on")

    def show(self, ttl):
        self.browser.execute_script(
            "BDHAlerts.show({key: 'k', title: 'Door', embed_url: '/hub/api/alerts/sample/',"
            " ttl: arguments[0]})", ttl)

    def test_keep_open_stops_the_countdown_until_closed(self):
        browser = self.browser
        browser.get(f"{self.live_server_url}/accounts/login/")
        wait_for_paint(browser)
        self.show(1)
        keep = WebDriverWait(browser, 5).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".hub-alert-keep")))
        keep.click()
        self.assertEqual(keep.get_attribute("aria-pressed"), "true")
        self.show(1)  # a repeat (another ring) must not restart the countdown
        time.sleep(2)
        self.assertTrue(browser.find_elements(*self.SHOWN))

        browser.find_element(By.CSS_SELECTOR, ".hub-alert-close").click()
        WebDriverWait(browser, 5).until(EC.invisibility_of_element_located(self.SHOWN))
        self.show(1)  # a fresh alert counts down again
        self.assertEqual(keep.get_attribute("aria-pressed"), "false")
        WebDriverWait(browser, 5).until(EC.invisibility_of_element_located(self.SHOWN))

    def bar_scale(self):
        return self.browser.execute_script(
            "return new DOMMatrix(getComputedStyle("
            "document.querySelector('.hub-alert-bar span')).transform).a")

    def test_repeat_with_the_same_end_keeps_the_countdown(self):
        browser = self.browser
        browser.get(f"{self.live_server_url}/accounts/login/")
        wait_for_paint(browser)
        self.show(4)
        time.sleep(2)
        self.show(2)  # a timer's second chime: ends when the first countdown does
        self.assertLess(self.bar_scale(), 0.75)
        self.show(10)  # a later end (another doorbell ring) refills the bar
        time.sleep(0.2)
        self.assertGreater(self.bar_scale(), 0.9)


@tag("selenium")
class AlertHoldTest(SeleniumTestCase):
    """The alert's own iframe can hold it open (doorbell mic on), release it, or hang up."""
    SHOWN = (By.CSS_SELECTOR, ".hub-alert.on")

    def post_from_frame(self, cmd):
        browser = self.browser
        browser.switch_to.frame(browser.find_element(By.CSS_SELECTOR, ".hub-alert-frame iframe"))
        browser.execute_script("parent.postMessage({bdhAlert: arguments[0]}, '*')", cmd)
        browser.switch_to.default_content()

    def open_alert(self, ttl):
        browser = self.browser
        browser.get(f"{self.live_server_url}/accounts/login/")
        wait_for_paint(browser)
        browser.execute_script(
            "BDHAlerts.show({key: 'k', title: 'Door', embed_url: '/hub/api/alerts/sample/',"
            " ttl: arguments[0]})", ttl)
        WebDriverWait(browser, 5).until(EC.presence_of_element_located(self.SHOWN))

    def test_hold_keeps_it_open_and_release_resumes_the_countdown(self):
        self.open_alert(2)
        self.post_from_frame("hold")
        self.browser.execute_script(
            "BDHAlerts.show({key: 'k', title: 'Door', embed_url: '/hub/api/alerts/sample/', ttl: 1})")
        time.sleep(3)  # past both the ttl and a repeat's ttl
        self.assertTrue(self.browser.find_elements(*self.SHOWN))
        self.post_from_frame("release")
        time.sleep(1)
        self.assertTrue(self.browser.find_elements(*self.SHOWN))  # grace period, not instant close
        self.assertGreater(self.browser.execute_script(
            "return new DOMMatrix(getComputedStyle("
            "document.querySelector('.hub-alert-bar span')).transform).a"), 0.9)

    def test_hangup_closes_and_only_the_alert_frame_is_obeyed(self):
        self.open_alert(30)
        self.browser.execute_script("window.postMessage({bdhAlert: 'hangup'}, '*')")
        time.sleep(0.5)
        self.assertTrue(self.browser.find_elements(*self.SHOWN))
        self.post_from_frame("hangup")
        WebDriverWait(self.browser, 5).until(EC.invisibility_of_element_located(self.SHOWN))


class _PopupPage(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<!doctype html><title>Pop-up</title><p id=pop>pop-up</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@tag("selenium")
class AlertHoldThroughAppFrameTest(SeleniumTestCase):
    """The real path: an app's pop-up runs on the app's own origin inside the hub's
    /_bare/ page inside the alert overlay, so its hold has to cross both frames."""
    SHOWN = (By.CSS_SELECTOR, ".hub-alert.on")

    def setUp(self):
        super().setUp()
        self.app = ThreadingHTTPServer(("127.0.0.1", 0), _PopupPage)
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.addCleanup(self.app.server_close)
        self.addCleanup(self.app.shutdown)
        port = self.app.server_address[1]
        make_install(port=port, upstream=f"http://127.0.0.1:{port}")
        reset_broker()
        self.addCleanup(reset_broker)

    def show(self, ttl):
        self.browser.execute_script(
            "BDHAlerts.show({key: 'k', title: 'Door', embed_url: '/apps/cams/_bare/cameras/popup/1/',"
            " ttl: arguments[0]})", ttl)

    def post_from_app(self, cmd):
        browser = self.browser
        wait = WebDriverWait(browser, 10)
        wait.until(EC.frame_to_be_available_and_switch_to_it((By.CSS_SELECTOR, ".hub-alert-frame iframe")))
        wait.until(EC.frame_to_be_available_and_switch_to_it((By.ID, "bdh-app-frame")))
        wait.until(EC.presence_of_element_located((By.ID, "pop")))
        browser.execute_script("parent.postMessage({bdh: 1, type: 'alert', cmd: arguments[0]}, '*')", cmd)
        browser.switch_to.default_content()

    def test_app_popup_holds_and_hangs_up_the_alert(self):
        browser = self.browser
        browser.get(f"{self.live_server_url}/accounts/login/")
        wait_for_paint(browser)
        self.show(3)
        self.post_from_app("hold")
        time.sleep(4)  # past the ttl
        self.assertTrue(browser.find_elements(*self.SHOWN))
        self.post_from_app("hangup")
        WebDriverWait(browser, 5).until(EC.invisibility_of_element_located(self.SHOWN))