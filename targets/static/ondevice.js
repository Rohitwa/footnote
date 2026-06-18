/*
 * Foothold on-device AI bridge (Phase 4).
 *
 * When the page runs inside the Capacitor Android app AND the OnDeviceAI
 * plugin reports models are present, this upgrades "talk to your memory" from
 * the server's lexical BM25 path to fully on-device semantic RAG:
 *   1. fetch the company's memory corpus (text the server already stores)
 *   2. embed corpus + query ON-DEVICE (no text leaves the phone)
 *   3. rank by cosine, take top-k
 *   4. answer ON-DEVICE with the generative SLM (or return extractive top-k)
 *
 * Everywhere else (desktop web, or when models aren't installed) window.
 * FootholdAI.askMemory() resolves null, and the caller falls back to the
 * server /ask endpoint. So this is purely additive.
 */
(function () {
  const Cap = window.Capacitor;
  const plugin = Cap && Cap.Plugins && Cap.Plugins.OnDeviceAI;
  const isNative = !!(Cap && typeof Cap.isNativePlatform === "function" && Cap.isNativePlatform());

  let cachedStatus = null;
  async function status() {
    if (!isNative || !plugin) return { embedReady: false, generateReady: false };
    if (cachedStatus) return cachedStatus;
    try { cachedStatus = await plugin.status(); } catch (_) { cachedStatus = { embedReady: false, generateReady: false }; }
    return cachedStatus;
  }

  function cosine(a, b) {
    let dot = 0, na = 0, nb = 0;
    for (let i = 0; i < a.length; i++) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
    if (!na || !nb) return 0;
    return dot / (Math.sqrt(na) * Math.sqrt(nb));
  }

  async function askMemory(companyId, query, k) {
    k = k || 4;
    const st = await status();
    if (!st.embedReady) return null;                 // → caller uses server /ask

    let corpus;
    try {
      const r = await fetch(`/targets/${companyId}/corpus`);
      corpus = (await r.json()).chunks || [];
    } catch (_) { return null; }
    if (!corpus.length) return { answer: "", sources: [], onDevice: true };

    let vectors;
    try {
      const texts = corpus.map(c => `${c.label || ""} ${c.text || ""}`.trim());
      const res = await plugin.embed({ texts: texts.concat([query]) });
      vectors = res.vectors;
    } catch (_) { return null; }                     // embed failed → server fallback
    const qVec = vectors[vectors.length - 1];

    const scored = corpus.map((c, i) => ({ ...c, score: cosine(vectors[i], qVec) }));
    scored.sort((x, y) => y.score - x.score);
    const top = scored.slice(0, k).filter(c => c.score > 0.15);
    const sources = top.map(c => ({
      source: c.source, ref_id: c.ref_id, label: c.label || c.source,
      text: (c.text || "").slice(0, 240), ts: c.ts ? String(c.ts).slice(0, 10) : "",
    }));
    if (!sources.length) return { answer: "", sources: [], onDevice: true };

    if (!st.generateReady) return { answer: "", sources, onDevice: true };  // extractive

    const context = top.map(c => `- [${c.source}] ${(c.text || "").slice(0, 300)}`).join("\n");
    const prompt =
      "You are a sales-memory assistant. Answer using ONLY the MEMORY snippets " +
      "about this one company. Be concise (<=3 sentences). If they don't contain " +
      "the answer, say so. Never invent facts.\n\nMEMORY:\n" + context +
      "\n\nQUESTION: " + query + "\n\nANSWER:";
    try {
      const g = await plugin.generate({ prompt: prompt });
      return { answer: (g.text || "").trim(), sources, onDevice: true };
    } catch (_) {
      return { answer: "", sources, onDevice: true };  // extractive fallback
    }
  }

  /*
   * First-run model downloader. Fetches the manifest (/api/ai/models), and for
   * each model not yet on the device, asks the plugin to download it — relaying
   * progress to onProgress({filename,label,pct,downloaded,total}). Resolves
   * {downloaded:[...], skipped:[...]} or throws. No-op (returns null) off-app.
   */
  async function ensureModels(onProgress) {
    if (!isNative || !plugin) return null;
    let manifest;
    try { manifest = (await (await fetch("/api/ai/models")).json()).models || []; }
    catch (_) { return null; }
    if (!manifest.length) return { downloaded: [], skipped: [], reason: "no model URLs configured" };

    cachedStatus = null;                       // force a fresh on-disk check
    const st = await status();
    const onDisk = { "embedder.tflite": st.embedReady, "gemma2-2b-it-int4.task": st.generateReady };

    let handle = null;
    if (plugin.addListener) {
      handle = await plugin.addListener("downloadProgress", (ev) => {
        const m = manifest.find(x => x.filename === ev.filename) || {};
        if (onProgress) onProgress({ filename: ev.filename, label: m.label || ev.filename,
          pct: ev.pct, downloaded: ev.downloaded, total: ev.total || m.bytes || 0 });
      });
    }
    const downloaded = [], skipped = [];
    try {
      for (const m of manifest) {
        if (onDisk[m.filename]) { skipped.push(m.filename); continue; }
        if (onProgress) onProgress({ filename: m.filename, label: m.label, pct: 0 });
        await plugin.download({ url: m.url, filename: m.filename });
        downloaded.push(m.filename);
      }
    } finally {
      if (handle && handle.remove) handle.remove();
      cachedStatus = null;                     // refresh after downloads
    }
    return { downloaded, skipped };
  }

  window.FootholdAI = { status, askMemory, ensureModels, isNative };
})();
