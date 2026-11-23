# UniFi Screens app: screen watchdog — design v2 (2026-11-23)

Status: approved (v2, after rubber-duck review). In progress: SDK 0.43.0 + hub `displays.status` first. The old AppDaemon watchdog and builder.mi kiosk-watch are retired (dry-run / disabled).

## 1. Goal / non-goals

Replace the AppDaemon `unifi_screen_watchdog` (appdaemon.mi; pause = SSH-edit YAML; MI only)
with an **optional BDH store app** any site with UniFi Connect screens can install. Sites
without UniFi screens never install it; hub core gains only a small vendor-neutral API.

Non-goals: page/renderer recovery (the `bdh-display` APK already does that in-process);
other screen vendors; detecting a black panel whose JS still runs (heartbeat can't see it —
keep a manual "Recover now" for that).

## 2. Shape

Repo `blackdiamondlodge/bdh-app-unifi-screens`, container app:

- `lifecycle="always"` (as timers uses), `network: lan` (LAN approval at install).
- Permissions: `displays.read` (new, §4), `alerts`, optional `ha.call:notify.*`.
- UI: a staff-protected settings page (supported custom settings-page link; every view checks
  signed broker identity **and** staff; CSRF on). **No on-screen UI at all** — no tile, card,
  status row or dashboard entry, by owner decision (not just v1). The manifest declares only a
  settings page; incidents surface via the settings page and phone notify.
- Persistent state in the app's own SQLite (not hub settings — 30 s cache TTL):
  enrollments, pause, escalation, budgets, cooldowns, action reservations, incidents, history.

## 3. Credentials and trust

- One **Network console** (URL, site default `default`, API key) + one optional, separately
  addressed **Connect console** (URL, local user, password) per hub. MI = gateway + UNVR.
- Secrets in the hub secret store (AES-GCM, app-scoped, write-only in UI, never logged).
  Boundary stated honestly: the hub platform is trusted (it stores/decrypts them for the app);
  other apps can't read them.
- TLS: self-signed consoles → **cert fingerprint pinned at setup (TOFU), checked before any
  credential is sent on every new connection**; re-pin only by explicit admin action; no
  cross-origin redirects with credentials. UI warns that the first connect is trust-on-first-use.
- UniFi Network API keys are full-admin on the console — documented in setup.

## 4. Hub change (vendor-neutral)

New broker op `displays.status`, grant `displays.read`:
`{ready_since, now, clients:[{ip, last_seen, age_seconds}]}` — no path/user agent.
`ready_since` = when the **public-serving** hub path last became ready (shared across the HTTPS
design's processes, not a module import time); lets apps tell "screen silent" from "hub
wasn't serving".

Steps: SDK accepts `displays.read` (validator + fake broker) → hub grant constant + handler
(grant check before data access) + `rpc.OPS` registration as read-only → SDK release/pin, app
`requires_sdk` → capability e2e tests (allowed / denied / no leak). The app feature-probes the
op at start; unsupported or denied = recovery disabled, **never** fall back to the open
`/display/api/heartbeats/` endpoint.

## 5. Screens: discovery + explicit enrollment

- Discovery proposes candidates: heartbeating IP → Network client (MAC, `sw_mac`, `sw_port`)
  → Connect device (by MAC/IP, if Connect is configured).
- **Enrollment is explicit** and keyed on **console id + screen MAC** (+ Connect device id,
  + approved switch MAC/port). IP is only the current address.
- Approval never transfers to another MAC. Enrolled screens stay listed when their heartbeat
  or client record disappears. Topology changes (new switch/port) → recovery for that screen
  held until re-approved (alert).
- Without Connect configured, enrollment is manual (admin picks the client), not inferred.

## 6. Signals

- **H** — hub page heartbeat fresh (`age < 300 s`) by the screen's current IP. "Unknown" if
  broker unreachable or `now - ready_since < 300 s`.
- **L** — Network: client connected + switch port up. "Unknown" if console unreachable/auth
  failure.
- **K** (optional, diagnostic) — `bdh-display` `/status` on :8787 (URL, `main_frame_error`,
  crash counters). Refines the step choice/evidence only; K unreachable never triggers action.
- Post-reboot Connect "client not found", 401/429, malformed responses = dependency problems,
  not screen failures.

## 7. Decision rules

- **Unknown never acts.** After any dependency outage (broker, console, hub not ready),
  re-enter a 300 s observation grace.
- **Site circuit breaker** (only with ≥ 2 enrolled screens): if ≥ 2 screens lose H within the
  same window while their L is up → treat as a shared hub/ingress failure: alert, no destructive
  action. With a single enrolled screen, hub-wide failures are covered by H=Unknown (broker
  unreachable, or within 300 s of hub `ready_since`) plus the caps.
  Hub additionally probes its own configured kiosk origin; failure → hold. (Limitation:
  can't prove kiosk-side DNS; stated in UI.)
- Recoveries are **serialized** across screens (one at a time).
- Grace 120 s before the first step. Escalation resets after 600 s **continuously known-healthy**.

## 8. Recovery ladder

| Step | Precondition | Action | Observe |
|---|---|---|---|
| 1 | Connect configured, device reachable in Connect | **Restore kiosk**: switch to app mode + launch the BDH APK by its asset `apkId` (resolved by package name), using actions from the device's supported list | 120 s |
| 2 | Connect configured | Connect `reboot` (then re-check app mode on return) | 240 s |
| 3 | PoE interlock passes | Network `cmd/devmgr power-cycle` (`mac` = switch, `port_idx`) | 240 s |
| — | 3 PoE cycles without recovery | `gave_up` incident; stays until 600 s healthy | — |

Caps: ≤ 1 action per screen per observe window; ≤ 6 PoE cycles per screen per 24 h.
`load_website`/`refresh_website` are never used (wrong mode for the APK).

**PoE interlock** (fresh read right before dispatch):
- Port = the **approved** switch MAC + port for this enrolled screen; port is PoE-capable and
  not an uplink.
- If the client is visible: its MAC must be on that port and be the only client there.
- If the client is absent (offline case): no *other* MAC may be seen on that port; switch
  record fresh. Otherwise refuse + incident.
- Exact `stat/sta`/`stat/device` behaviour while unplugged/booting/moved is validated with
  read-only captures before live mode.

## 9. Execution contract (durable)

- Single serialized executor. Before a destructive request: write an action **reservation**;
  then re-check pause, enrollment, budget, interlock; then dispatch.
- Transport timeout = "possibly executed": counted, then observe; no blind retry of
  destructive POSTs.
- Pause (1 h / 4 h / until resumed, auto-expires) is acknowledged only after queued actions are
  cancelled; the UI reports any already-dispatched action.
- "Recover now" goes through the same executor, interlocks and budgets.
- Dry-run counters are separate from real budgets.
- Each decision records evidence: identity, observation timestamps, topology, suppression
  reason, budget.
- UniFi clients: session reuse, bounded re-auth, CSRF header refresh, 429 backoff, timeouts on
  every call; success = HTTP OK **and** API-level OK.

## 10. Liveness of the watchdog itself

Expose last successful loop + per-dependency status on the settings page. Per-screen errors
are isolated. If the executor dies or a loop exceeds its deadline, the process exits so the
supervisor restarts it (always-on reconciliation).

## 11. Notifications

Incidents are persisted and shown on the settings page (source of truth). Delivery:
HA phone notify (configured + "send test" during setup; UI warns if no external route) and
`alerts.push(admins_only)` as a bonus (live pop-up only). Events: gave up, interlock refused,
topology needs re-approval, credential/pin failure, circuit breaker tripped, recovered after
≥ step 2. Deduplicated.

## 12. Phase 2: night sleep moves out of core

Core keeps a **generic, queryable desired display state** (sleep schedule/state per screen);
publishes `display/sleep` / `display/wake` events as hints. App subscribes
(`events.subscribe:display/#`) and **periodically reconciles** desired vs actual via Connect
(events are best-effort; a lost wake must self-heal). Watchdog knows which screens it put to
sleep. Cutover makes the app the single owner; core's `display/connect.py` and its Connect
password are removed in the same change. Verify overnight APK heartbeat behaviour first.

## 13. Testing + rollout gates

Unit (injected clock, fakes for broker / Network / Connect / kiosk):
grace, escalation, reset, unknown-holds, re-grace after outage, circuit breaker, caps, pause
racing a queued action, manual vs automatic race, restart between reservation/dispatch/response,
timeout-as-executed, counters across upgrades and clock changes, DHCP reuse, port move,
offline client missing from topology, broker OK but public HTTPS down, APK not launched /
wrong Connect mode, lost wake event (phase 2).
Hub: `displays.status` grant e2e.

Rollout:
1. Hub + SDK PR: `displays.read` / `displays.status`.
2. Read-only UniFi captures at MI (unplug / boot / port move) to pin the interlock rules.
3. App `0.1.0b1`: discovery, enrollment, settings page, **dry-run only**, alongside AppDaemon
   ≥ 2 days; compare decisions.
4. `0.1.0b2` live at MI: **disable AppDaemon first, verify inactive, then enable live** —
   never two power-cycle authorities.
5. BDL install; promote to stable.
6. Phase 2 (sleep).

## 14. Decisions on former open questions

1. Identity: real client IP (trusted proxy headers only on the public ingress per the HTTPS
   design) for observation; MAC + console identity for enrollment/actions. Kiosk device id:
   later, not a prerequisite.
2. Kiosk `:8787/status`: optional read-only diagnostic signal in v1; no control commands.
3. Consoles: one Network + one optional Connect per hub; multi-console deferred.

## 15. Cut from v1

`refresh_website`; "no hand-mapping" promise (discovery + explicit enrollment instead);
per-screen threshold knobs (keep enable / dry-run / pause / notify);
multi-console; kiosk debug-API control commands.
