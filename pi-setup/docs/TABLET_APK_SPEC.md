# HanryxVault POS — Tablet (Expo APK) Spec

This is the build spec for the **customer-facing tablet** in the trade-in
flow. The tablet is the device the customer holds during a trade-in to
review the offer the operator built on the Main Pi (10.1" screen) and
Accept or Reject it before any cards are added to inventory.

This document is **everything an agent needs** to build the APK end-to-end.
It does NOT require any other context from the POS repo.

---

## 1. Architecture

```
┌─────────────────┐  Tailscale / LAN  ┌──────────────────┐
│ Tablet (Expo)   │ ◄───────────────► │ Main Pi  :8080   │
│ Android APK     │   poll 2s         │ Flask + Redis    │
└─────────────────┘                   └──────────────────┘
```

- The tablet has **no local database**. Everything is in Redis on the Pi.
- The tablet **polls** `GET /tablet/trade/current` every 2 seconds. When
  it sees a new offer (different `sent_at` than last render), it shows
  the offer screen. When the offer disappears (`{empty: true}`) or status
  becomes `accepted`/`rejected`, it returns to idle.
- Customer taps **Accept** or **Reject** → tablet posts to `/tablet/trade/<id>/accept`
  or `/reject` → operator's admin screen (which is polling
  `/admin/trade-in/<id>/offer-status` every 1.5 s) sees the result and
  can finalize the trade.

---

## 2. Network configuration

The tablet reaches the Pi over **Tailscale** (default) or **home LAN**
(fallback for at-desk dev). WireGuard is no longer used — replaced by
Tailscale for simpler key management and roaming.

| Mode                 | Base URL                       | When                                                |
|----------------------|--------------------------------|-----------------------------------------------------|
| Tailscale (default)  | `http://100.125.5.34:8080`     | Anywhere — works on iPhone hotspot at card shows    |
| Home LAN             | `http://192.168.86.36:8080`    | Tablet on home/shop wifi only (lower latency)       |

**The Tailscale URL must be the baked-in default** — a fresh install (or
"Clear app data") must show `http://100.125.5.34:8080` already populated
in the Server URL field, so the operator can take the tablet straight to
a show without typing anything.

The base URL must remain **configurable in-app** on a settings screen,
plus an "API Token" field. Persist both with `expo-secure-store` so they
survive APK reinstalls/restarts. Add a one-tap **"Use home LAN"** preset
button next to the URL field that fills `http://192.168.86.36:8080` so
home dev still works without retyping.

---

## 3. Authentication

All `/tablet/*` endpoints require the header:

```
X-API-KEY: <api_token_from_settings>
```

The token is the same value used by the kiosk SSE — the operator can
look it up at `https://<pi>:8080/admin/api-tokens` and paste it into the
tablet settings screen on first install.

If the tablet receives **HTTP 401** at any point, show a clear "Token
invalid — open Settings to fix" banner and stop polling until the user
opens the settings screen and saves a new value.

---

## 4. Endpoints (4 you actually need)

### 4.1 `GET /tablet/trade/current`

Polled every 2 seconds while the offer screen is the foreground screen.

**Empty response** (no active offer):
```json
{ "empty": true }
```

**Active offer response**:
```json
{
  "ti_id":        17,
  "reference":    "TI-2026-0042",
  "customer":     "Walk-in",
  "items": [
    {
      "id":         101,
      "name":       "Charizard ex 199/197",
      "qr_code":    "sv1-199",
      "condition":  "LP",
      "offer":      85.00,
      "market":     142.00
    },
    { "id": 102, "name": "Pikachu V 25/198", "qr_code": "swsh4-25",
      "condition": "NM", "offer": 6.50, "market": 11.00 }
  ],
  "item_count":   2,
  "total_cash":   91.50,
  "total_credit": 109.80,
  "status":       "pending",
  "sent_at":      1730000000000,
  "decided_at":   null
}
```

Important behaviors:

- If `status === "pending"` → render the offer screen with both buttons
  enabled.
- If `status === "accepted"` or `"rejected"` → render a confirmation
  splash for ~3 s, then return to idle. Stop polling for that ti_id (use
  `sent_at` as the dedupe key).
- If `empty === true` → idle screen ("Waiting for the next trade-in…").
- Always send `Cache-Control: no-store` is server-side; you don't need
  to do anything special on the client.

### 4.2 `POST /tablet/trade/<ti_id>/accept`

Body: `{}` (empty JSON object — no signature required for v1; we may add
one in v2).

Response: `{ "ok": true, "status": "accepted" }`

On success, immediately update the local UI to the accepted splash; do
NOT wait for the next poll.

### 4.3 `POST /tablet/trade/<ti_id>/reject`

Same shape as accept. Response: `{ "ok": true, "status": "rejected" }`.

### 4.4 `GET /print/status`

Polled every **5 seconds** while the print/sale screen is the foreground
screen. Pause polling when the screen is backgrounded.

**No auth header required** — this endpoint is intentionally
unauthenticated for diagnostics. Do NOT send `X-API-KEY`.

**Response (always 200):**

```json
{
  "printer_available": true,
  "printer_path": "/dev/usb/lp0",
  "bt_mac": null
}
```

| Field               | Type            | Meaning                                                                                          |
|---------------------|-----------------|--------------------------------------------------------------------------------------------------|
| `printer_available` | bool            | Server can open the printer right now. **Use as the primary readiness gate.**                     |
| `printer_path`      | string \| null  | Where the server will send bytes. Today: `/dev/usb/lp0` (USB). `"cups"` = no usable device. `null` = no config. **Future-proofing:** may also be a network host like `192.168.1.50:9100` or `tcp://printer.local:9100` once we add a network printer fallback — the readiness check below already handles this. |
| `bt_mac`            | string \| null  | Bluetooth MAC. **Currently null and will stay null** — do NOT require this to be non-null.        |

**Required readiness check** (replace any existing logic). Treats both USB
and (future) network printers as ready:

```ts
const path = res.printer_path;

const isReady =
  res.printer_available === true &&
  typeof path === 'string' &&
  path.length > 0 &&
  path !== 'cups';

// Sub-classify only for banner copy — both branches are equally "ready":
const isUsbPrinter     = isReady && /^\/dev\/usb\/lp\d+$/.test(path);
const isNetworkPrinter = isReady && !path.startsWith('/');   // host:port or tcp://… or DNS name
```

The transport sub-classification is purely cosmetic (banner copy). Both
USB and network paths must enable the print button identically — never
gate on transport type.

**Banner text on the diagnostics / sale screen:**

| Condition                                                       | Text                          |
|-----------------------------------------------------------------|-------------------------------|
| `isReady` AND `isUsbPrinter`                                    | `Printer ready (USB)`         |
| `isReady` AND `isNetworkPrinter`                                | `Printer ready (Network)`     |
| `isReady` (anything else — e.g. unknown future transport)       | `Printer ready`               |
| `printer_available === false` OR `printer_path === "cups"`      | `Printer not connected`       |
| Network error / timeout reaching the server                     | `Cannot reach POS server`     |

**Do NOT:**
- Require `bt_mac` to be non-null.
- Require `printer_path` to start with `/dev/usb/` — that breaks the
  network-printer path the moment we add it server-side.
- Check for any specific USB VID/PID (server abstracts the device).
- Cache readiness state for more than one poll cycle.
- Block the print button on `bt_mac` being null or on the transport
  sub-classification.

The diagnostics screen must include a **"Refresh printer status"** button
that triggers an immediate poll and updates the banner — guards against
stale state if the operator just plugged the printer in.

---

## 5. Screens (3 screens total)

### 5.1 Idle screen

Black background, centered:

> **HanryxVault POS**
> Waiting for the next trade-in…

Tiny "⚙" gear button in a corner that opens Settings.

### 5.2 Offer screen

Top header: **"Review Your Trade Offer"**, subtitle: `<reference> ·
<customer>`.

Scrollable list of items. Each row:
```
┌──────────────────────────────────────────────────────────┐
│ Charizard ex 199/197                            $85.00   │
│ Condition: LP  ·  Market $142.00                         │
└──────────────────────────────────────────────────────────┘
```

Bottom totals card (sticky):
```
   You'll receive:
   ╔═══════════════════════╦═══════════════════════╗
   ║   $91.50              ║   $109.80             ║
   ║   CASH                ║   STORE CREDIT        ║
   ╚═══════════════════════╩═══════════════════════╝
```

Bottom action bar (sticky, side-by-side, very large tap targets, ~80 dp):
- **REJECT** (red, secondary)
- **ACCEPT** (green, primary)

Tapping Accept shows a 1-step confirmation modal (`"Accept $91.50 cash
or $109.80 store credit for these N cards?"`) before posting. Reject can
be one-tap (less risk in over-rejecting; operator can re-send).

### 5.3 Result splash

Full-screen, ~3 second auto-dismiss back to idle.

- **Accepted:** green gradient, big "✓ Thank you!", small text "Hand
  the cards to the cashier to complete the trade."
- **Rejected:** neutral gray, "Offer declined", small text "The cashier
  will be with you shortly."

### 5.4 Settings screen (gear icon → modal)

Two fields, "Save" button, plus presets and diagnostics:

| Field      | Storage                        | Default                          |
|------------|--------------------------------|----------------------------------|
| Server URL | `secure-store: serverUrl`      | `http://100.125.5.34:8080`       |
| API Token  | `secure-store: apiToken`       | (empty — operator pastes on first install) |

**Preset buttons** (next to the URL field, fill the field on tap):
- **"Use Tailscale"** → `http://100.125.5.34:8080`
- **"Use home LAN"** → `http://192.168.86.36:8080`

**"Test Connection"** button — three sequential checks, each with a
green/red indicator:
1. `GET /health` (no auth) — server reachable
2. `GET /tablet/trade/current` (with `X-API-KEY`) — auth works
3. `GET /print/status` (no auth) — readiness payload parses and matches
   the §4.4 contract

**"Refresh printer status"** button — fires a one-shot `GET /print/status`
and updates the diagnostics banner immediately, bypassing the 5-second
poll cadence.

---

## 6. Polling rules

```ts
// Pseudocode
const POLL_MS = 2000;
let lastSentAt = 0;
let pollHandle = null;

function startPolling() {
  if (pollHandle) return;
  pollHandle = setInterval(async () => {
    try {
      const res = await fetch(`${serverUrl}/tablet/trade/current`, {
        headers: { 'X-API-KEY': apiToken, 'Cache-Control': 'no-store' },
      });
      if (res.status === 401) { showAuthBanner(); stopPolling(); return; }
      if (!res.ok) return;
      const data = await res.json();

      if (data.empty) {
        if (currentScreen !== 'idle') goIdle();
        return;
      }
      // Status transitions: pending → accepted/rejected
      if (data.status === 'accepted' || data.status === 'rejected') {
        if (data.sent_at !== lastSplashShownFor) {
          lastSplashShownFor = data.sent_at;
          showSplash(data.status);    // 3s, then goIdle()
        }
        return;
      }
      if (data.status === 'pending' && data.sent_at !== lastSentAt) {
        lastSentAt = data.sent_at;
        showOfferScreen(data);
      }
    } catch (e) {
      // network blip — silently retry next tick
    }
  }, POLL_MS);
}
```

- Pause polling while the Settings modal is open.
- Resume polling automatically on app foreground (`AppState`
  listener).
- Use `expo-keep-awake` so the screen never sleeps while in use; it's a
  counter device, plug it in.

---

## 7. Build / install

```bash
# Bootstrap
npx create-expo-app@latest hanryxvault-tablet --template blank-typescript
cd hanryxvault-tablet
npx expo install expo-secure-store expo-keep-awake expo-status-bar

# Build a local debug APK (no EAS login needed for sideload)
npx expo prebuild --platform android
cd android && ./gradlew assembleDebug
# APK lands at android/app/build/outputs/apk/debug/app-debug.apk
```

For production / signed releases use `eas build --platform android
--profile production` (EAS account required) or `./gradlew assembleRelease`
with a self-signed keystore (sideload only).

---

## 8. Acceptance criteria

The APK is "done" when:

1. Fresh install (or "Clear app data") → Settings → URL field is
   pre-populated with `http://100.125.5.34:8080` (Tailscale) — operator
   only needs to paste the API token. Test Connection green on all
   three checks (`/health`, `/tablet/trade/current`, `/print/status`).
2. Operator on the Pi clicks **📲 Send Offer to Tablet** for a 3-card
   trade-in → tablet flips from idle to the offer screen within 2 s.
3. All 3 line items render with correct name/condition/offer/market,
   plus correct cash + credit totals.
4. Tapping **Accept** → confirmation modal → confirm → splash → 3 s →
   idle.
5. The operator's admin modal pill changes to **✅ Customer accepted**
   within 1.5 s, and the **Complete & Add to Inventory** button
   unlocks.
6. Tapping **Reject** on a fresh offer → reject splash → idle, and the
   operator's pill shows **❌ Customer declined**.
7. Operator hits Cancel on the trade-in → tablet goes back to idle
   within ~2 s (because `/tablet/trade/current` returns
   `{empty: true}`).
8. Killing wifi mid-trade → tablet shows a small "Reconnecting…" badge
   but does NOT crash; restoring connectivity resumes polling
   automatically.
9. APK survives device reboot — settings persisted, polls resume on
   first app launch (no need to re-enter token).
10. **Tailscale-only test:** turn off the tablet's home wifi, connect to
    iPhone hotspot only — app still loads sale screen, polls heartbeat,
    shows `Printer ready (USB)`, and a tap on "test print" produces a
    real receipt from the Pi-attached MUNBYN printer.
11. **Printer readiness check:** with a USB printer plugged into the Pi
    and the POS container healthy, the diagnostics banner shows
    `Printer ready (USB)`. Unplug the USB cable on the Pi → within ~10 s
    the banner switches to `Printer not connected`. Plug back in → tap
    "Refresh printer status" → banner returns to `Printer ready (USB)`.
12. **Home LAN preset:** Settings → tap **"Use home LAN"** → URL field
    shows `http://192.168.86.36:8080`. Save → app reconnects on home
    wifi successfully. This confirms the override path still works.

---

## 9. Out of scope for v1

- On-screen signature capture (we'll add `signature_b64` to the accept
  body in v2).
- Per-line accept/reject (v1 is whole-offer accept-or-reject).
- Receipt PDF download on the tablet (kiosk handles receipts already).
- Push notifications / FCM (polling is enough for an in-store device).
