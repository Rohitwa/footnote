# Foothold on-device AI (Phase 4) — build & verify

This phase adds **on-device** semantic RAG and generation to the Android app,
so "talk to your memory" can run with **no text leaving the phone**. It is
purely additive: on desktop web, or before the models are installed, the app
falls back to the server's lexical `/ask` automatically.

> ⚠️ The native plugin in this commit is **authored but not built/verified** —
> there is no Android device/emulator in the authoring environment. Everything
> below is the build + on-device test you (Rohit) run in Android Studio.

## Import contact from phone (contact editor)

The lead's Contact editor has an **"Import from phone"** button that fills
name/phone/email from the device address book. It uses
`@capacitor-community/contacts` when present, falls back to the Web Contact
Picker API where available, and hides itself otherwise (e.g. desktop). To
enable the native picker:
```
cd foothold_app
npm install            # picks up @capacitor-community/contacts (already in package.json)
npx cap sync android
```
`READ_CONTACTS` is already declared in AndroidManifest.xml. The plugin presents
the system picker; the web bridge (`pickContact()` in lead_detail.html) maps the
result into the contact form, then Save writes via POST /targets/{id}/contact.

## What was added

| Layer | File | Role |
|-------|------|------|
| Native plugin | `android/app/.../OnDeviceAiPlugin.java` | `status()` / `embed(texts)` / `generate(prompt)` via MediaPipe |
| Registration | `android/app/.../MainActivity.java` | `registerPlugin(OnDeviceAiPlugin.class)` |
| Deps | `android/app/build.gradle` | `tasks-text` (embedder) + `tasks-genai` (LLM) |
| Web bridge | served at `/static/ondevice.js` | `window.FootholdAI.askMemory()` — embed corpus + query locally, cosine rank, generate |
| Server | `GET /targets/{id}/corpus` | hands the WebView the memory chunks to embed locally |
| UI | `lead_detail.html` | ask flow prefers on-device, falls back to server |

The data flow on-device: fetch corpus (text the server already stores) →
`OnDeviceAI.embed()` the chunks + query → cosine in JS → top-k →
`OnDeviceAI.generate()` a grounded answer (or show extractive top-k if the
generative model isn't installed).

## 1. Models (not bundled — too large for the apk)

Place both in the app's files dir under `ai/`:

- **Embedder** → `<filesDir>/ai/embedder.tflite`
  MediaPipe Text Embedder model (e.g. Universal Sentence Encoder) from
  https://ai.google.dev/edge/mediapipe/solutions/text/text_embedder — rename to `embedder.tflite`. (~a few MB.)
- **Generative SLM** → `<filesDir>/ai/gemma2-2b-it-int4.task`
  Gemma-2-2B-it int4 `.task` bundle from the MediaPipe LLM Inference catalog
  (Kaggle / https://ai.google.dev/edge/mediapipe/solutions/genai/llm_inference/android). (~1.3 GB.)

### Option A — in-app first-run downloader (recommended)

Host your own copy of each model on any HTTPS URL (Supabase Storage bucket,
Cloudflare R2, S3 — **not** Fly: 512 MB RAM / ephemeral disk). The Gemma
`.task` is license-gated, so you must host your own copy. Then set these on
the Fly app:
```
fly secrets set -a foothold-yantrai \
  MODEL_EMBEDDER_URL="https://<your-bucket>/embedder.tflite" \
  MODEL_EMBEDDER_BYTES=4200000 \
  MODEL_LLM_URL="https://<your-bucket>/gemma2-2b-it-int4.task" \
  MODEL_LLM_BYTES=1340000000
```
`GET /api/ai/models` then returns the manifest (only models whose URL is set).
On the Memory tab inside the app, an **"Enable on-device AI"** banner appears
when a model is missing; tapping it streams each model to `filesDir/ai/` with a
progress bar (`OnDeviceAiPlugin.download()` → `downloadProgress` events). No
`adb` needed, survives reinstalls of the models? No — files dir is cleared on
uninstall, but a reinstall just re-shows the banner.

### Option B — manual adb push (dev shortcut)
```
adb shell run-as dev.rohit.foothold mkdir -p files/ai
adb push embedder.tflite        /data/local/tmp/ && adb shell run-as dev.rohit.foothold cp /data/local/tmp/embedder.tflite files/ai/
adb push gemma2-2b-it-int4.task /data/local/tmp/ && adb shell run-as dev.rohit.foothold cp /data/local/tmp/gemma2-2b-it-int4.task files/ai/
```

## 2. Build

```
cd foothold_app
npx cap sync android
```
In `android/app/build.gradle` confirm an arm64 filter so the genai native libs
don't bloat other ABIs:
```gradle
android { defaultConfig { ndk { abiFilters 'arm64-v8a' } } }
```
Then build/run from Android Studio (or `./gradlew :app:assembleDebug`).
Gemma-2-2B needs a device with ~4 GB+ RAM; mid-range phones will be slow.

## 3. Verify on device

1. App loads the hosted UI as before. Open any lead → Memory tab.
2. In Chrome `chrome://inspect` devtools for the WebView, run:
   ```js
   await window.FootholdAI.status()   // {embedReady:true, generateReady:true}
   ```
3. Ask a question in the capture bar (end with `?`). The answer card shows a
   green **· on-device** tag when it ran locally. Pull the network tab — there
   should be a `GET /corpus` but **no `/ask`** call (that's the cloud fallback).
4. Airplane mode: embedding + retrieval still work offline; generation works if
   the SLM is installed. With models absent, it silently falls back to `/ask`.

## Caveats / next

- True call-stream recording (Phase 5) is **not** possible on Android 10+
  (`VOICE_CALL` audio source is locked) — plan mic-capture on speakerphone.
- Embeddings here are recomputed per ask (per-company corpus is small). If a
  company's memory grows large, cache vectors in the WebView (IndexedDB) keyed
  by chunk `ref_id` + `updated_at`.
- Loading remote UI that calls a native plugin is intentional but a trust
  boundary: only `dev.rohit.foothold`'s own hosted origin should be allowed
  (it is, via `server.url`). Don't widen `server.allowNavigation`.
