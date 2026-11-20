import threading
from unittest import mock

from django.test import SimpleTestCase

from hub import mdns


class MdnsTests(SimpleTestCase):
    def test_service_info_is_what_the_kiosk_browses_for(self):
        info = mdns.service_info("192.168.1.179", 8000,
                                 {"name": "Slivinski Hub", "subtitle": "Mercer Island"},
                                 "mercerisland", hostname="homehub.mi")
        self.assertEqual(info.type, "_bdh-hub._tcp.local.")
        self.assertEqual(info.name, "Black Diamond Hub._bdh-hub._tcp.local.")
        self.assertEqual(info.server, "homehub.local.")
        self.assertEqual(info.port, 8000)
        self.assertEqual(info.parsed_addresses(), ["192.168.1.179"])
        self.assertEqual(info.decoded_properties, {
            "path": "/", "port": "8000", "site": "mercerisland",
            "name": "Slivinski Hub", "location": "Mercer Island"})

    def test_run_registers_on_the_lan_address_and_unregisters_on_stop(self):
        stop = threading.Event()
        stop.set()
        with mock.patch.dict("os.environ", {"BDH_MDNS_ADDRESS": "10.0.0.5"}), \
                mock.patch("zeroconf.Zeroconf") as zc_cls:
            mdns.run(8000, {}, "sunpeaks", stop)
        zc_cls.assert_called_once()
        self.assertEqual(zc_cls.call_args.kwargs["interfaces"], ["10.0.0.5"])
        zc = zc_cls.return_value
        info = zc.register_service.call_args.args[0]
        self.assertEqual(info.parsed_addresses(), ["10.0.0.5"])
        self.assertTrue(zc.register_service.call_args.kwargs["allow_name_change"])
        zc.unregister_service.assert_called_once_with(info)
        zc.close.assert_called_once()
