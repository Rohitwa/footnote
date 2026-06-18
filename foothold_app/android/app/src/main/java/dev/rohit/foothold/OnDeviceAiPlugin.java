package dev.rohit.foothold;

import android.util.Log;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import com.google.mediapipe.tasks.components.containers.Embedding;
import com.google.mediapipe.tasks.text.textembedder.TextEmbedder;
import com.google.mediapipe.tasks.text.textembedder.TextEmbedderResult;
import com.google.mediapipe.tasks.genai.llminference.LlmInference;
import com.google.mediapipe.tasks.genai.llminference.LlmInference.LlmInferenceOptions;

import org.json.JSONArray;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * On-device AI for Foothold — the hybrid-SLM keystone.
 *
 *  - embed(texts)      → MediaPipe TextEmbedder (all-MiniLM / USE). Powers the
 *                        semantic upgrade of the lexical RAG: the WebView
 *                        embeds the company corpus + the query locally and
 *                        ranks by cosine, so NO text leaves the device.
 *  - generate(prompt)  → MediaPipe LLM Inference (Gemma-2-2B int4) for
 *                        capture-parse, PII redaction, and grounded answers.
 *
 * Models are NOT bundled in the apk (too large); they are read from the app
 * files dir and downloaded/placed once on first run — see ONDEVICE_AI_SETUP.md.
 * Both engines init lazily so app start is never blocked, and every method
 * fails soft (resolve with ready:false / reject) so the web layer can fall
 * back to the server path.
 */
@CapacitorPlugin(name = "OnDeviceAI")
public class OnDeviceAiPlugin extends Plugin {

    private static final String TAG = "OnDeviceAI";
    private static final String EMBEDDER_FILE = "embedder.tflite";       // ~90MB MiniLM/USE
    private static final String LLM_FILE = "gemma2-2b-it-int4.task";      // ~1.3GB Gemma-2-2B

    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private TextEmbedder embedder;
    private LlmInference llm;

    private File modelFile(String name) {
        return new File(getContext().getFilesDir(), "ai/" + name);
    }

    private synchronized TextEmbedder embedder() throws Exception {
        if (embedder == null) {
            File f = modelFile(EMBEDDER_FILE);
            if (!f.exists()) throw new IllegalStateException("embedder model missing: " + f);
            TextEmbedder.TextEmbedderOptions opts = TextEmbedder.TextEmbedderOptions.builder()
                    .setBaseOptions(com.google.mediapipe.tasks.core.BaseOptions.builder()
                            .setModelAssetPath(f.getAbsolutePath()).build())
                    .setL2Normalize(true)
                    .build();
            embedder = TextEmbedder.createFromOptions(getContext(), opts);
        }
        return embedder;
    }

    private synchronized LlmInference llm() throws Exception {
        if (llm == null) {
            File f = modelFile(LLM_FILE);
            if (!f.exists()) throw new IllegalStateException("llm model missing: " + f);
            LlmInferenceOptions opts = LlmInferenceOptions.builder()
                    .setModelPath(f.getAbsolutePath())
                    .setMaxTokens(1024)
                    .build();
            llm = LlmInference.createFromOptions(getContext(), opts);
        }
        return llm;
    }

    @PluginMethod
    public void status(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("embedReady", modelFile(EMBEDDER_FILE).exists());
        ret.put("generateReady", modelFile(LLM_FILE).exists());
        ret.put("platform", "android");
        call.resolve(ret);
    }

    @PluginMethod
    public void download(final PluginCall call) {
        final String url = call.getString("url", "");
        final String filename = call.getString("filename", "");
        if (url == null || url.isEmpty() || filename == null || filename.isEmpty()) {
            call.reject("url and filename required"); return;
        }
        // Reject path traversal — filename must be a bare name.
        if (filename.contains("/") || filename.contains("..")) {
            call.reject("invalid filename"); return;
        }
        io.execute(() -> {
            HttpURLConnection conn = null;
            try {
                File dir = modelFile(filename).getParentFile();
                if (dir != null && !dir.exists()) dir.mkdirs();
                File dest = modelFile(filename);
                File part = new File(dest.getAbsolutePath() + ".part");

                conn = (HttpURLConnection) new URL(url).openConnection();
                conn.setConnectTimeout(30000);
                conn.setReadTimeout(60000);
                conn.connect();
                int code = conn.getResponseCode();
                if (code < 200 || code >= 300) { call.reject("HTTP " + code + " for " + filename); return; }
                long total = conn.getContentLengthLong();

                // Bail early if there clearly isn't room (need ~total + 10% slack).
                long usable = dir != null ? dir.getUsableSpace() : Long.MAX_VALUE;
                if (total > 0 && usable < total + total / 10) {
                    call.reject("not enough free space for " + filename
                            + " (need " + total + ", have " + usable + ")"); return;
                }

                try (InputStream in = conn.getInputStream();
                     FileOutputStream out = new FileOutputStream(part)) {
                    byte[] buf = new byte[1 << 16];
                    long done = 0; int n; int lastPct = -1; long lastEmit = 0;
                    while ((n = in.read(buf)) != -1) {
                        out.write(buf, 0, n);
                        done += n;
                        int pct = total > 0 ? (int) (done * 100 / total) : -1;
                        // Throttle events: on each whole-percent or every 2 MB.
                        if (pct != lastPct || done - lastEmit > (2L << 20)) {
                            lastPct = pct; lastEmit = done;
                            JSObject ev = new JSObject();
                            ev.put("filename", filename);
                            ev.put("downloaded", done);
                            ev.put("total", total);
                            ev.put("pct", pct);
                            notifyListeners("downloadProgress", ev);
                        }
                    }
                }
                if (!part.renameTo(dest)) {
                    part.delete();
                    call.reject("could not finalize " + filename); return;
                }
                // New model on disk — drop any cached engine so it re-inits.
                synchronized (this) {
                    if (filename.equals(EMBEDDER_FILE)) embedder = null;
                    if (filename.equals(LLM_FILE)) llm = null;
                }
                JSObject ret = new JSObject();
                ret.put("path", dest.getAbsolutePath());
                ret.put("filename", filename);
                call.resolve(ret);
            } catch (Exception ex) {
                Log.e(TAG, "download failed", ex);
                call.reject("download failed: " + ex.getMessage());
            } finally {
                if (conn != null) conn.disconnect();
            }
        });
    }

    @PluginMethod
    public void embed(final PluginCall call) {
        final JSArray texts = call.getArray("texts");
        if (texts == null) { call.reject("texts[] required"); return; }
        io.execute(() -> {
            try {
                TextEmbedder e = embedder();
                JSONArray out = new JSONArray();
                for (int i = 0; i < texts.length(); i++) {
                    String t = texts.optString(i, "");
                    TextEmbedderResult r = e.embed(t);
                    Embedding emb = r.embeddingResult().embeddings().get(0);
                    float[] vec = emb.floatEmbedding();
                    JSONArray jv = new JSONArray();
                    for (float v : vec) jv.put((double) v);
                    out.put(jv);
                }
                JSObject ret = new JSObject();
                ret.put("vectors", out);
                call.resolve(ret);
            } catch (Exception ex) {
                Log.e(TAG, "embed failed", ex);
                call.reject("embed failed: " + ex.getMessage());
            }
        });
    }

    @PluginMethod
    public void generate(final PluginCall call) {
        final String prompt = call.getString("prompt", "");
        if (prompt == null || prompt.isEmpty()) { call.reject("prompt required"); return; }
        io.execute(() -> {
            try {
                String text = llm().generateResponse(prompt);
                JSObject ret = new JSObject();
                ret.put("text", text == null ? "" : text);
                call.resolve(ret);
            } catch (Exception ex) {
                Log.e(TAG, "generate failed", ex);
                call.reject("generate failed: " + ex.getMessage());
            }
        });
    }
}
