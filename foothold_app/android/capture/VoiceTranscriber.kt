package dev.rohit.foothold.capture

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

/**
 * Truly on-device transcription for site-visit voice notes ("raw audio never
 * leaves the phone"). Uses SpeechRecognizer with EXTRA_PREFER_OFFLINE so
 * recognition runs on the device's offline model (download the offline language
 * pack once in system settings). Privacy-grade counterpart to the PWA's Web
 * Speech API (which, on Chrome, may use the cloud).
 *
 * Usage: VoiceTranscriber(ctx).listen { text ->
 *          CaptureApi(cfg).capture(leadPhone, "visit", text, "out") }
 *
 * ⚠ WRITTEN, NOT DEVICE-VERIFIED here.
 */
class VoiceTranscriber(private val ctx: Context) {
    private var sr: SpeechRecognizer? = null

    fun listen(onText: (String) -> Unit, onError: (Int) -> Unit = {}) {
        if (!SpeechRecognizer.isRecognitionAvailable(ctx)) { onError(-1); return }
        sr = SpeechRecognizer.createSpeechRecognizer(ctx).apply {
            setRecognitionListener(object : android.speech.RecognitionListener {
                override fun onResults(b: Bundle) {
                    val txt = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty()
                    if (txt.isNotBlank()) onText(txt)
                    release()
                }
                override fun onError(e: Int) { onError(e); release() }
                override fun onReadyForSpeech(p: Bundle?) {}
                override fun onBeginningOfSpeech() {}
                override fun onRmsChanged(v: Float) {}
                override fun onBufferReceived(b: ByteArray?) {}
                override fun onEndOfSpeech() {}
                override fun onPartialResults(p: Bundle?) {}
                override fun onEvent(t: Int, p: Bundle?) {}
            })
        }
        val i = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-IN")
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)   // on-device
        }
        sr?.startListening(i)
    }

    fun release() { sr?.destroy(); sr = null }
}
