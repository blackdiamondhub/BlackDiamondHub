"""A fake Home Assistant for capabilities_test.py, on 127.0.0.1:8123.

Serves the REST and websocket calls bdh_sdk.ha makes, and records every one
so tests can assert what reached HA (GET /_calls, POST /_reset).
"""
import json
import sys

from aiohttp import WSMsgType, web

TOKEN = "fake-ha-token"
DEVICES = [{"id": "dev-kitchen", "name": "Kitchen light", "name_by_user": None,
            "manufacturer": "e2e", "model": "bulb"},
           {"id": "dev-safe", "name": "Safe", "name_by_user": None, "manufacturer": "e2e", "model": "lock"}]
ENTITIES = [
    {"entity_id": "light.kitchen", "device_id": "dev-kitchen", "platform": "hue", "name": None,
     "original_name": "Kitchen", "translation_key": None, "disabled_by": None},
    {"entity_id": "light.porch", "device_id": None, "platform": "hue", "name": None,
     "original_name": "Porch", "translation_key": None, "disabled_by": None},
    {"entity_id": "lock.safe", "device_id": "dev-safe", "platform": "august", "name": None,
     "original_name": "Safe", "translation_key": None, "disabled_by": None},
    {"entity_id": "media_player.lounge", "device_id": None, "platform": "sonos", "name": None,
     "original_name": "Lounge", "translation_key": None, "disabled_by": None},
]
STATES = {
    "light.kitchen": {"state": "off", "attributes": {"friendly_name": "Kitchen"}},
    "light.porch": {"state": "on", "attributes": {"friendly_name": "Porch"}},
    "lock.safe": {"state": "locked", "attributes": {"friendly_name": "Safe"}},
    "media_player.lounge": {"state": "idle", "attributes": {"friendly_name": "Lounge"}},
}
AUDIO = b"ID3\x03\x00\x00\x00\x00\x00\x00fake-tts-audio"

calls = []


def state(eid):
    return {"entity_id": eid, **STATES[eid], "last_changed": "2026-01-01T00:00:00+00:00"}


@web.middleware
async def record(request, handler):
    if request.path.startswith("/_"):
        return await handler(request)
    if request.path != "/api/websocket":
        body = None
        if request.content_type == "application/json" and request.can_read_body:
            raw = await request.text()
            body = json.loads(raw) if raw.strip() else None
        calls.append({"method": request.method, "path": request.path, "body": body})
        if request.headers.get("Authorization") != f"Bearer {TOKEN}" and not request.path.startswith("/api/tts_proxy"):
            return web.json_response({"message": "unauthorized"}, status=401)
    return await handler(request)


async def states(request):
    return web.json_response([state(e) for e in STATES])


async def one_state(request):
    eid = request.match_info["eid"]
    if eid not in STATES:
        return web.json_response({"message": "Entity not found."}, status=404)
    return web.json_response(state(eid))


async def service(request):
    return web.json_response([])


async def upload(request):
    form = await request.post()
    target = form.get("media_content_id", "")
    name = form["file"].filename if "file" in form else "file"
    calls[-1]["body"] = {"media_content_id": target, "filename": name}
    return web.json_response({"media_content_id": f"{target}/{name}"})


async def tts_get_url(request):
    return web.json_response({"path": "/api/tts_proxy/e2e.mp3", "url": "http://ha.invalid/api/tts_proxy/e2e.mp3"})


async def tts_audio(request):
    return web.Response(body=AUDIO, content_type="audio/mpeg")


WS_RESULTS = {
    "config/entity_registry/list": ENTITIES,
    "config/device_registry/list": DEVICES,
    "get_config": {"unit_system": {"temperature": "°C", "length": "km"}, "time_zone": "UTC"},
}


async def websocket(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_json({"type": "auth_required", "ha_version": "2026.1.0"})
    auth = await ws.receive_json()
    if auth.get("access_token") != TOKEN:
        await ws.send_json({"type": "auth_invalid", "message": "bad token"})
        await ws.close()
        return ws
    await ws.send_json({"type": "auth_ok", "ha_version": "2026.1.0"})
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            break
        data = json.loads(msg.data)
        kind = data.get("type")
        calls.append({"method": "WS", "path": kind, "body": data})
        if kind == "ping":
            await ws.send_json({"id": data.get("id"), "type": "pong"})
        elif kind == "get_states":
            await ws.send_json({"id": data.get("id"), "type": "result", "success": True,
                                "result": [state(e) for e in STATES]})
        else:
            await ws.send_json({"id": data.get("id"), "type": "result", "success": True,
                                "result": WS_RESULTS.get(kind)})
    return ws


async def get_calls(request):
    return web.json_response(calls)


async def reset(request):
    calls.clear()
    return web.json_response({"ok": True})


def app():
    a = web.Application(middlewares=[record])
    a.add_routes([
        web.get("/api/", lambda r: web.json_response({"message": "API running."})),
        web.get("/api/states", states),
        web.get("/api/states/{eid}", one_state),
        web.post("/api/services/{domain}/{service}", service),
        web.post("/api/media_source/local_source/upload", upload),
        web.post("/api/tts_get_url", tts_get_url),
        web.get("/api/tts_proxy/{name}", tts_audio),
        web.get("/api/websocket", websocket),
        web.get("/_calls", get_calls),
        web.post("/_reset", reset),
    ])
    return a


if __name__ == "__main__":
    web.run_app(app(), host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8123, print=None)
