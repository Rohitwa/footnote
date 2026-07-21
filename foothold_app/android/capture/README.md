# Foothold native capture (Phase 5) — WRITTEN, NOT DEVICE-VERIFIED

Native Android capture modules that feed the Foothold server capture spine
(`POST /api/v1/capture/inbound`, verified live). These Kotlin files were ported
from the earlier `salesman_app` track and adapted to Foothold's bearer-auth
`/api/v1` contract and the `dev.rohit.foothold` package.

> ⚠ These were **written but not compiled or device-tested** in the build
> environment (no Android SDK / device there). Build + verify on a Mac with adb.

## What each file does
| File | Role |
|---|---|
| `CaptureConfig.kt` | On-device prefs + the **tagged-number registry** (privacy boundary) |
| `CaptureApi.kt` | Zero-dep HTTP client → `GET /api/v1/health`, `GET /api/v1/companies`, `POST /api/v1/capture/inbound` (Bearer) |
| `CallCaptureService.kt` | Foreground service: call-log observer → posts call metadata for **tagged** numbers → "log the call" notification |
| `WaNotificationListener.kt` | NotificationListener: WhatsApp **inbound** from tagged leads → capture (channel=whatsapp) |
| `VoiceTranscriber.kt` | Offline `SpeechRecognizer` (EXTRA_PREFER_OFFLINE) for site-visit voice notes |
| `CaptureSetupActivity.kt` | One-time setup: backend URL + `FOOTHOLD_TOKEN`, permissions, notif access, start service |
| `manifest-additions.xml` | Permissions + component registrations to merge into AndroidManifest |

## The privacy contract (unchanged)
Raw media (call audio, message bodies) **never leaves the phone** as raw. Only
**derived signals** (direction, duration, short text) are POSTed, and only for
**tagged leads** — every capture hook filters to the synced tagged-number set at
source; the server re-filters by phone (`find_lead_by_phone`) and drops any
untagged number (`{matched:false}`). Recorded call audio + tone is a **separate,
server-side** path via the CPaaS bridge (`/api/v1/webhooks/call`), not this module.

## Build + device test (on your Mac)
1. Copy `*.kt` into `foothold_app/android/app/src/main/java/dev/rohit/foothold/capture/`.
2. Merge `manifest-additions.xml` into `app/src/main/AndroidManifest.xml`.
3. Ensure `appcompat` is a dependency (CaptureSetupActivity extends AppCompatActivity).
4. Build:
   ```bash
   cd foothold_app && npx cap sync android
   cd android && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" \
     ANDROID_HOME=~/Library/Android/sdk ./gradlew assembleDebug
   ```
5. Install + open capture setup:
   ```bash
   ~/Library/Android/sdk/platform-tools/adb install -r app/build/outputs/apk/debug/app-debug.apk
   adb shell am start -n dev.rohit.foothold/dev.rohit.foothold.capture.CaptureSetupActivity
   ```
6. In the setup screen: enter `https://foothold-yantrai.fly.dev` + your `FOOTHOLD_TOKEN`,
   Connect (syncs tagged leads), grant permissions + Notification access, Start capturing.
7. Verify: place a call to / receive a WhatsApp from a **tagged** lead's number →
   the touch appears on that lead's **Lead Brain** and the owner's 🔔 lights up.

## Known caveats
- WhatsApp: inbound-only + resolves by **contact display name** (needs the lead saved
  as a contact). Full history/outbound needs the WhatsApp Business API.
- Call recording audio is **not** captured on-device (Android 10+ blocks it) — that's
  the CPaaS server-side path, by design.
- OEM battery managers may kill the foreground service; whitelist the app.
