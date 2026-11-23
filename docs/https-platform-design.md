# BDH secure origins, ingress and audio — design v2

Status: accepted (v2, 2026-11-23; after two rubber-duck reviews incl. gpt-6-astra). Not yet implemented. Owner: Stefan.
Repos: BlackDiamondHub (hub/broker), bdh-supervisor, bdh-sdk, bdh-display (kiosk),
bdh-app-cameras, bdh-aec, go2rtc-nack.

## 1. Goals / non-goals

Goals
- **G1 Secure context everywhere.** Phone, laptop and kiosk WebView all use standard
  `getUserMedia`/WebRTC. No native mic bridge, no browser flags.
- **G2 Simple to maintain.** One owner across many repos. Use static config, a single source of
  truth for URLs, and as few moving parts as possible.
- **G3 Secure.** Keep per-app origin isolation. The hub session credential never leaves the broker.
  Media and mic are authorized like the app UI, and revoked with it. No admin surface is
  reachable by apps.
- **G4 Distributable.** Installers need no port forwarding and no router or local-DNS changes on
  supported networks, with honest limits (§8).
- **G5 Mic is a platform capability.** Camera talk now, AI voice agent later.

Non-goals (for now): remote/internet access (must not be blocked by this design), wake word or
always-listening, and implementing voice-agent tools (only the contract is fixed, in §9).

## 2. Trust tiers

| Tier | Members | Trusted with |
|---|---|---|
| Platform | broker/hub, supervisor, Caddy, companions (go2rtc, bdh-aec), audio gateway | sessions, routing, secrets (least-privilege per component) |
| Restricted app | bridge-network app containers | its own origin, its own cookies, signed identity headers, SDK APIs |
| Elevated LAN app | apps with `network: lan` (host network) | same as restricted, **plus LAN reach**; assumed able to reach every host TCP port, so **no host loopback/TCP listener is a trust boundary** |
| In-process plugin | Django apps inside the hub | fully trusted, same as the hub |
| Browser code | each app origin, sibling subdomains | nothing beyond its own origin; postMessage is checked by exact origin and source |
| LAN clients | anything on the LAN | nothing; must authenticate |

Rule that follows from the elevated-LAN tier: trusted channels are **unix sockets** or isolated
docker networks with controlled membership, never `127.0.0.1`.

## 3. Naming, DNS records, certificates

- Each hub has a **site domain** `D` (e.g. `mi.bdhlabs.com`, `bdl.bdhlabs.com`).
- Hub origin is `https://D`. App origin is `https://<label>.D`.
- `label` is assigned at install:
  - Format: `[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?`, derived from the app id (underscores become
    hyphens).
  - Collision-checked, stored on the install row, never reused for another install.
  - Reserved: `www api auth media audio admin hub broker`.
- Media lives on the app's own origin under `https://<label>.D/_media/...`. There are no extra
  hostnames and no CORS.
- Public DNS (DNS-only) has two records, `D` and `*.D`, both pointing at the hub LAN address.
  App installs never touch DNS.
- One certificate covers `D` and `*.D` (Let's Encrypt, DNS-01). App installs never trigger
  issuance.
- Changing the domain is a re-enrollment: all hub and app sessions are invalidated and users
  sign in again. Documented, not automated.

## 4. Ingress

```
browser ─https─> Caddy :443  (hub compose, static Caddyfile, admin off)
   ├─ host D, *.D         ─unix:/run/bdh/public.sock─> daphne-public  (proxy headers trusted)
   │                                                     └─ HostDispatcher: D → hub,
   │                                                        <label>.D → app proxy, else 421
   └─ *.D path /_media/*  route{ strip X-BDH-*; forward_auth public.sock /_bdh/media-auth;
                                rewrite; reverse_proxy unix//run/bdh/apps/{labels.3}/go2rtc.sock }

app containers ──docker bridge:8099──> daphne-internal (/_broker/ only, NO proxy-header trust)
legacy http :8000 + app ports ──> daphne-legacy (separate process; migration only, then removed)
```

### Caddy
- **Static Caddyfile**, rendered once from `D` by hub bootstrap. No per-app routes and
  `admin off`.
- Built with xcaddy plus `caddy-dns/cloudflare`, published to ghcr and pinned.
- **Hub compose owns Caddy's lifecycle**; the supervisor owns apps and companions.
- ACME state is on a persistent volume.
- Media block uses an explicit `route`, so the order is guaranteed. Caddy's default directive
  order runs `forward_auth` before `request_header`, which would otherwise erase copied headers.
  The steps, in order:
  1. Delete client `X-BDH-*`.
  2. `forward_auth` (allow/deny only; no upstream is returned).
  3. `uri strip_prefix /_media`.
  4. `reverse_proxy` to a **fixed socket path derived from the host label**.
- Unregistered labels are denied by auth before the proxy runs. A label can't contain `/`.
- Validate the pinned Caddy build with real requests: forged/duplicate headers, missing auth
  response, unknown host, path rewrite.

### Daphne
Daphne's proxy-header trust is per process, not per listener, so the hub runs **separate
processes**:
- `daphne-public`: unix socket only, `--proxy-headers`. The socket dir is shared only with Caddy.
- `daphne-internal`: the existing broker API on the docker bridge, `/_broker/` only, no
  proxy-header trust (today's separation in `proxy.py:73–94`, preserved).
- `daphne-legacy`: today's `:8000` and app ports, for side-by-side migration only. It is
  removed at the end (Phase 6).

### HostDispatcher
Replaces `PortDispatcher`. The install is chosen by the `Host` label. Broker-internal paths are
never routable through public hosts.

### Companions
- go2rtc API uses `api.unix_listen` at `/run/bdh/apps/<label>/go2rtc.sock` with
  `api.listen: ""` (turns off the default TCP listener). Remove `origin: "*"`.
- Socket access matrix:

| Socket dir | Owner | Caddy | Keeper (app server) | bdh-aec | Other apps |
|---|---|---|---|---|---|
| `/run/bdh/apps/<label>/` | companion (rw) | connect only (dir r-x, socket rw, no replace) | own app only | own app only | none |

- The supervisor fixes UID/GID per app. Companions remove a stale socket on start. Keeper and
  AEC clients gain unix-socket support (AEC currently builds TCP URLs, `aecd.py:50–52,80–88`).
- If the pinned go2rtc-nack image lacks `unix_listen`, rebase the fork. Don't patch it.
- WebRTC ICE UDP/TCP stays on the host network. ICE credentials and DTLS-SRTP protect the
  media; authorization and lifetime are enforced at signaling (§6).
- One host-wide WebRTC port today (`templates.py:210–229`). A second LAN companion needs
  per-slot ports.

## 5. Identity across origins

### Cookies
- Hub: `__Host-bdh_session` (Secure, HttpOnly, `Path=/`, SameSite=Lax) and `__Host-csrftoken`,
  host-only on D.
- App: `__Host-bdh_app` (same attributes), host-only on `<label>.D`. The value is a
  server-generated opaque ID.
- The proxy forwards only the app's declared cookies (unchanged). It also rejects upstream
  `Set-Cookie` that has a `Domain` attribute or a `__Host-bdh*` / `bdh_*` name.
- Siblings can't read these cookies, and the `__Host-` prefix stops sibling `Domain=` injection.

### App entry: a browser-bound transaction, no credential in any URL
1. The shell loads the iframe at `https://<label>.D/` (no grant).
2. The proxy sees no valid app cookie. For a document navigation it returns a **tiny
   broker-owned bootstrap page**, and sets `__Host-bdh_txn` (random nonce, 2 min) with the
   validated return path stored server-side in the transaction. Fetch/XHR gets a structured 401.
3. The bootstrap sends `{type:'bdh-enter', txn_hash}` to `parent`, targeting exact origin
   `https://D`.
4. The shell checks `event.origin === app_origin(install)` and `event.source` is that iframe.
   It then calls `POST https://D/_bdh/grant {install, txn_hash}` with hub CSRF and an exact
   Origin check. The response is an opaque single-use grant bound to
   `{install, txn_hash, hub_session_ref, exp 30 s}`.
5. The shell posts the grant to the iframe (exact target origin). The bootstrap sends
   `POST /_bdh/enter {grant}`.
6. The proxy **atomically consumes** the grant and requires `sha256(txn cookie) == txn_hash`
   and install == Host label. It creates the app session
   `{id, install, hub_session_ref, auth_hash}`, sets `__Host-bdh_app`, clears the txn, and
   303s to the stored path.
7. All entry responses use `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.
   Grants and txn values are redacted from Caddy, daphne and error logs.
- The bootstrap retries a bounded number of times and then shows a visible error.
- **Apps open only inside the hub shell.** A top-level app URL redirects to
  `https://D/apps/<id>/?path=...`. Shell popups for apps open a new shell window, so platform
  mic controls are always present.
- Dashboard/guest visitors get an **explicitly saved** anonymous hub session (an untouched
  Django session isn't persisted), so one flow covers every mode.

### Revocation contract (one rule for HTTP, push, app WS, media, audio)
- An app session is valid only if the hub session exists, is unexpired, and its auth hash still
  matches (password/session-hash change). Mode/stay/permissions are re-resolved through
  `visitor.for_session`; the 5 s cache is the bounded staleness.
- Long-lived connections carry an opaque `hub_session_ref` and **re-resolve identity** on every
  authorization check. This replaces push's frozen identity snapshot (`push.py:268–278,384–400`).
- Logout or login rotation publishes a revocation event. Push, app WS, media leases and audio
  leases tied to that ref close within 5 s, with a 60 s periodic sweep as the fallback. Apps
  re-enter after login.

### Apps acting for a visitor
`X-BDH-Identity` (signed by the broker per request) remains the only visitor assertion an app
gets. An app's own token **can't** assert arbitrary visitors. Server-side calls that need visitor
scope (e.g. media/talk checks) are broker→app authorize callbacks keyed by the broker's opaque
ref, never app→broker "on behalf of" claims.

## 6. Media authorization (no browser tickets)

Media is on the app origin, so the **app session cookie is the credential**.

- `forward_auth` → `/_bdh/media-auth` (daphne-public) checks, on the original canonical request:
  - A valid `__Host-bdh_app` for this Host label.
  - Method, path and parameters against a strict allowlist. Duplicate parameters are rejected.
    `src` must be a **registered stream id**, never a source URL.
  - Policy: the broker asks the app (signed callback, like push authorize) whether this visitor
    may `view` this stream. The result is cached per (app session, stream) for 30 s.
- **Direction is enforced by construction, not by URL:**
  - Streams registered for viewing have no backchannel producer.
  - Talk uses a separate internal stream reachable only via the companion socket from bdh-aec.
  - `/api/talk`, stream admin (`/api/streams` PUT/DELETE, `/api/config`, etc.) are never on
    the allowlist.
  - Test gate: a view-path SDP offering `sendrecv` audio must not reach the camera. If go2rtc
    still accepts it, add a fork option to refuse backchannel on API consumers.
- **Lifetime:** each WS/WebRTC consumer has a media lease `{app session, stream, exp ≤ 30 min}`.
  On lease expiry or revocation, the companion keeper closes the consumer through go2rtc's
  internal API. Clients re-request transparently.
  - Open item: confirm go2rtc can drop one consumer by id. If not, it's a small fork patch.

## 7. Endpoint contract (one source of truth)

- Site config holds `domain` and `address_policy`. `broker/urls.py` is the only code that builds
  browser-facing URLs: `hub_origin()`, `app_origin(install)`, `media_base(install)`, `audio_ws()`.
  These are used by the shell, postMessage/WS origin checks (`remote.py:103–136`, `proxy.py`,
  `push.py`) and redirects.
- Apps receive endpoints in SDK bootstrap (`bdh.endpoints`) and never derive URLs from
  `location`/ports.
- bdh-sdk runtime:
  - `SECURE_PROXY_SSL_HEADER` (apps are reachable only via the broker proxy, which sets it).
  - `CSRF_TRUSTED_ORIGINS` set to the exact app origin.
  - Secure cookies.
  - The real client IP passed for push limits.
- The contract is versioned (`endpoints_version`) so the hub, SDK and apps roll independently.

## 8. DNS, credentials, distribution, offline

- **DNS agent (supervisor):** keeps the `D` and `*.D` A records equal to the installer-chosen
  address. The default is the interface holding the default route, excluding
  docker/loopback/link-local/VPN. Updates are debounced, and the last good value is kept on
  failure. AAAA is not published.
- **Editions (explicit product decision):**
  - **BYO-domain (v1, now).** The owner supplies their own zone and a token scoped to
    Zone:Read + DNS:Edit on that one zone. It is a secret file mounted only into Caddy and the
    supervisor, and never appears in hub/app env, logs or support bundles. Acceptable because
    it's the owner's own zone.
  - **Managed name (later, before any shared `bdhlabs.com` distribution).**
    - An immutable hub id gets `<hubid>.bdhlabs.com`.
    - A tiny provisioning API with per-hub credentials can update only that hub's A records.
    - `_acme-challenge` CNAME delegation (acme-dns style) means a hub can answer only its own
      challenge.
    - Credentials can be revoked and recovered.
    - Never ship a zone token.
- **Supported networks:** the client's resolver must return private IPs from public DNS. Setup
  runs a browser check from the kiosk and from one phone, and the result is shown on the hub
  status page. Routers with DNS-rebind protection (FRITZ!Box, OpenWrt, Pi-hole, some ISPs) need
  a documented exception or local DNS. Universal "works on every resolver" would need a cloud
  relay, which is out of scope.
- **Offline: no HTTP fallback.**
  - The kiosk shows a **bundled native offline screen** on load failure (WebView can't tell
    NXDOMAIN from an attack, so a downgrade can't be made safe). It retries and returns to
    `https://D` automatically.
  - Certificate errors are never bypassed.
  - Optional local DNS (UniFi local record / dnsmasq) keeps full operation during WAN outages
    and is documented for owners who want it.
- **Recovery path that doesn't need HTTPS:** supervisor CLI over SSH, plus a minimal
  unauthenticated HTTP status page (cert/DNS state only, no login, separate process).
- HSTS: none during migration. Later, a short max-age on D only, with no includeSubDomains and
  no preload.

## 9. Audio: the mic as a platform capability

```
shell (top-level https://D)
  getUserMedia {echoCancellation, noiseSuppression, autoGainControl}
  └─ AudioWorklet → 16 kHz mono s16le, 20 ms frames (640 B) + seq + capture ts
     └─ wss://D/_bdh/audio  (hub session cookie; exact Origin)
        └─ audio gateway (daphne-public, async I/O only; no DSP in the event loop)
             atomic lease per destination (DB row lock), takeover policy, max 5 min
             └─ destination adapter
                  camera-talk → app authorize callback → bdh-aec talk socket
                                (splits each frame into 2×320 B 10 ms AEC blocks)
                                → go2rtc internal talk stream → Protect
                  ai-agent (later) → voice gateway
```

### Consent
- Apps request `bdh.audio.start({destination, target})` by postMessage.
- The **shell shows its own control** (push-to-talk / confirm sheet naming the app and
  destination). Capture starts only from that trusted interaction, never silently from an
  iframe message.
- The authorization is bound to that frame and target.
- Capture stops on release, navigation, logout, permission loss or lease expiry, and when the
  page is hidden.
- Iframes never get the `microphone` permission.

### Backpressure
- Browser: watch `bufferedAmount`. Above 200 ms it stops sending and restarts the stream rather
  than delivering stale speech.
- Gateway and adapter: bounded queues, drop-oldest, and the session closes if lag stays above
  500 ms.
- Limits: message size, frame rate, duration, and one session per shell/destination.
- Metrics: queue delay, drops, lease events. No audio payloads are logged.

### Transport and echo
- WS PCM is the v1 transport (LAN push-to-talk). Switch to WebRTC (inside the same gateway
  boundary) only if measurements show loss/jitter or a full-duplex need. Don't build both.
- Two echo paths, one fix each:
  - Kiosk speaker → kiosk mic: browser AEC (validated in the Phase 0 spike).
  - Camera speaker → camera mic → kiosk speaker: bdh-aec on the camera's audio.
  - These are independent, and both may run.

### Kiosk (bdh-display)
- The HTTPS build **deletes** the `BDHDisplay` native mic bridge (`MainActivity.java:175–177,
  567–599`). It doesn't merely avoid calling it.
- `onPermissionRequest` grants only `AUDIO_CAPTURE`, only for the exact origin `https://D`.
- Mixed content is off.
- Navigation is limited to D and `*.D`. Anything else opens externally or is blocked.
- Bundled offline screen (§8).

### Voice agent contract (fixed now, built later)
- The agent calls broker tools with server-enforced capabilities scoped by mode
  (owner/guest/dashboard).
- Sensitive actions need confirmation at execution time.
- Every call is audit-logged.
- The tool service holds credentials and the model never does. Tools are allowlisted per mode,
  not "anything HA can do".

## 10. Phases and gates

0. **Spike (throwaway, MI).** Caddy plus certificate for D, and a static page. Verify on a phone
   **and the real kiosk WebView**: getUserMedia, AudioWorklet, browser AEC, and the WS PCM
   round trip.
   - Also verify: go2rtc-nack `unix_listen`, a Caddy unix upstream from `{labels.3}`, and
     go2rtc per-consumer close.
   - **Go/no-go gate.**
1. **Ingress + identity (MI, beta).**
   - Caddy service, the three daphne processes, HostDispatcher, labels, entry transaction,
     `__Host-` cookies, `urls.py`, SDK settings, revocation contract.
   - Legacy :8000 keeps working side by side.
   - **Ops gates before anyone depends on it:** renewal on staging ACME, expiry alert
     (< 20 days), cert/DNS status page, header-spoof tests on every listener, rollback =
     kiosk URL back to legacy.
2. **Media.** Companion sockets, media-auth, view-only streams, leases. Cameras app moves to
   `bdh.endpoints` (beta).
3. **Kiosk.** HTTPS build (bridge deleted, offline screen). MI kiosk switches to `https://D`.
4. **Audio gateway + camera talk** from the browser.
5. **BDL rollout.** WAN-down cold boot, changed-IP and rebind tests at that site.
6. **Retire legacy.** Remove `daphne-legacy` and the app ports. Voice-agent work starts.

## 11. Test plan

Browser E2E with Playwright on builder.mi:
- Entry flow, including txn mismatch / replayed grant / grant from another session (login swap).
- `next` normalization attacks.
- CSRF on app POST.
- Push WS.
- Logout closing push/media/audio within 5 s.
- Sibling cookie injection.
- Forged `X-BDH-*` / `X-Forwarded-*` on every listener.
- Media: unregistered `src`, duplicate params, admin paths, view SDP attempting talk.
- Audio: simultaneous acquire, slow consumer, no silent start from iframe.
- Lifecycle: uninstall/reinstall (label not reused), missing socket, partial upgrade, rollback.

Kiosk:
- Cold boot with WAN down shows the offline screen.
- WAN up returns to https.
- A cert error is never bypassed.

Cross-site: MI phone viewing BDL cameras over IPsec (ICE candidates reachable).

## 12. Open questions

Decided (2026-11-23): v1 is BYO-domain only (installer buys a domain + Cloudflare token);
managed `bdhlabs.com` names later. HTTP local mode dropped (offline screen + optional local DNS).

1. Revocation fan-out mechanism: Django signals in one process, or Postgres LISTEN/NOTIFY once
   the hub runs more than one worker?
2. go2rtc per-consumer close: does the existing API suffice, or does it need a fork patch
   (Phase 0 answers this)?

## Appendix: review decisions

- Media tickets **cut**. A ticket leaked the session key, and the app cookie suffices (astra B1).
- Grants became a **browser-bound transaction** with nothing in URLs (B2).
- **Revocation contract** added. Push no longer uses a frozen identity (B3).
- **Separate daphne processes**: proxy trust is per process (B4).
- **Direction by construction** plus media leases (B5).
- **Shell-owned consent**. Native bridge deleted, not disabled (B6).
- **Editions** made explicit, with no shared zone tokens (B7).
- Caddy: **explicit `route`**, fixed socket path from the label, no free-form upstream header (I8).
- go2rtc `unix_listen` exists. Set `api.listen ""` and add the access matrix (I9).
- Bootstrap HTML for iframe re-entry, and apps only inside the shell (I10).
- HTTP fallback **cut**, replaced by the offline screen (I11).
- WS PCM v1 with explicit 20 ms → 2×10 ms framing and backpressure (I12).
- Trust tiers added. Caddy lifecycle belongs to hub compose (I13).
- Ops/rollback gates moved into Phase 1 (I14).
