package dev.rohit.foothold.capture

import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * Zero-dependency HTTP client for the Foothold backend (HttpURLConnection on a
 * background executor — no OkHttp/coroutine deps).
 *
 * Endpoints (all bearer-authed):
 *   GET  /api/v1/health                 -> connectivity/token check
 *   GET  /api/v1/companies              -> leads (for the tagged-number registry)
 *   POST /api/v1/capture/inbound        -> { matched, lead_id, ... }
 *
 * ⚠ WRITTEN, NOT DEVICE-VERIFIED here.
 */
class CaptureApi(private val cfg: CaptureConfig) {
    private val io = Executors.newSingleThreadExecutor()

    private fun req(method: String, path: String, body: JSONObject?): JSONObject? {
        return try {
            val c = (URL(cfg.baseUrl + path).openConnection() as HttpURLConnection).apply {
                requestMethod = method
                connectTimeout = 8000; readTimeout = 8000
                setRequestProperty("Content-Type", "application/json")
                if (cfg.token.isNotEmpty()) setRequestProperty("Authorization", "Bearer " + cfg.token)
                if (body != null) { doOutput = true; outputStream.use { it.write(body.toString().toByteArray()) } }
            }
            if (c.responseCode in 200..299) JSONObject(c.inputStream.bufferedReader().readText())
            else null
        } catch (e: Exception) { null }
    }

    /** Validate the base URL + token. */
    fun connect(cb: (Boolean) -> Unit) = io.execute {
        val r = req("GET", "/api/v1/health", null)
        cb(r?.optBoolean("ok", false) == true)
    }

    /** Pull the tagged set (dm_phone/dm_whatsapp + names) so we can filter on-device. */
    fun refreshTagged(cb: (Boolean) -> Unit = {}) = io.execute {
        val r = req("GET", "/api/v1/companies", null) ?: return@execute cb(false)
        // /api/v1/companies returns a JSON array of lead rows.
        val arr: JSONArray = r.optJSONArray("companies") ?: r.optJSONArray("rows")
            ?: JSONArray()
        val digits = HashSet<String>(); val names = HashSet<String>()
        for (i in 0 until arr.length()) {
            val l = arr.getJSONObject(i)
            val phone = l.optString("dm_phone").ifEmpty { l.optString("dm_whatsapp") }
            CaptureConfig.digits(phone)?.let { d ->
                digits.add(d)
                val nm = l.optString("dm_name").ifEmpty { l.optString("name") }.trim()
                if (nm.isNotEmpty()) names.add("$nm||$d")
            }
        }
        if (digits.isNotEmpty()) { cfg.taggedDigits = digits; cfg.taggedNamePairs = names }
        cb(digits.isNotEmpty())
    }

    /**
     * POST a captured touch. The backend re-matches by phone and drops untagged
     * numbers, so this is safe even if the on-device filter is stale.
     *   channel: "call" | "whatsapp" | "sms" | "email" | "visit"
     *   direction: "in" | "out"
     */
    fun capture(phone: String, channel: String, text: String, direction: String) = io.execute {
        req("POST", "/api/v1/capture/inbound", JSONObject()
            .put("channel", channel).put("from_phone", phone)
            .put("text", text).put("direction", direction))
    }
}
