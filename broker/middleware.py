"""Keeps the broker on its own port, whatever server runs the hub.

``broker.proxy.PortDispatcher`` already does this under daphne; this covers
``runserver`` and tests. On the broker port only ``/_broker/rpc/<op>`` is
served, straight from here, so the hub's other middleware (sessions, CSRF,
``ALLOWED_HOSTS`` — apps call in by the bridge IP) never sees broker calls.
Everywhere else ``/_broker`` doesn't exist.
"""
import re

from django.http import HttpResponseNotFound

from . import conf

RPC_PATH = re.compile(r"^/_broker/rpc/(?P<op>[a-z_]+\.[a-z_]+)$")


class BrokerPortMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if str(request.META.get("SERVER_PORT")) == str(conf.broker_port()):
            match = RPC_PATH.match(path)
            if match is None:
                return HttpResponseNotFound("Not found")
            from .views import rpc

            return rpc(request, match["op"])
        if path == "/_broker" or path.startswith("/_broker/"):
            return HttpResponseNotFound("Not found")
        return self.get_response(request)
