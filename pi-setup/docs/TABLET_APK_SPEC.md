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
┌─────────────────┐  WireGuard / LAN  ┌──────────────────┐
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

The tablet reaches the Pi over either:

| Mode      | Base URL                          | When                      |
|-----------|-----------------------------------|---------------------------|
| In-store  | `http://192.168.86.36:8080`       | Tablet is on shop wifi    |
| Remote    | `http://10.8.0.1:8080`            | Tablet is on WireGuard    |

The base URL must be **configurable in-app** on a settings screen, plus
an "API Token" field. Persist both with `expo-secure-store` so they
survive APK reinstalls/restarts.

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

## 4. Endpoints (only 3 you actually need)

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

Two fields, "Save" button:

| Field      | Storage                        |
|------------|--------------------------------|
| Server URL | `secure-store: serverUrl`      |
| API Token  | `secure-store: apiToken`       |

A "Test Connection" button that does `GET /health` (no auth) then
`GET /tablet/trade/current` (with auth) and shows pass/fail for each.

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

1. Fresh install → Settings → enter URL + token → Test Connection green
   on both checks.
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

---

## 9. Out of scope for v1

- On-screen signature capture (we'll add `signature_b64` to the accept
  body in v2).
- Per-line accept/reject (v1 is whole-offer accept-or-reject).
- Receipt PDF download on the tablet (kiosk handles receipts already).
- Push notifications / FCM (polling is enough for an in-store device).
