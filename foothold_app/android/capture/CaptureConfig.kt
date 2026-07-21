package dev.rohit.foothold.capture

import android.content.Context

/**
 * On-device config + the tagged-number registry for Foothold capture.
 *
 * Auth to the Foothold backend is a BEARER TOKEN (the same FOOTHOLD_TOKEN the
 * server uses for /api/v1). The captured touch is matched to a lead server-side
 * by phone (last-10 digits) — see targets/db.py::find_lead_by_phone.
 *
 * The tagged set is the privacy boundary: capture services only ever process
 * numbers that appear here (people saved as leads). Everyone else is dropped on
 * the device before anything leaves it ("tagged-contacts-only").
 *
 * ⚠ WRITTEN, NOT DEVICE-VERIFIED in this environment — build + test on a Mac
 *   with adb (see capture/README.md).
 */
class CaptureConfig(ctx: Context) {
    private val p = ctx.getSharedPreferences("foothold_capture", Context.MODE_PRIVATE)

    var baseUrl: String
        get() = p.getString("baseUrl", "https://foothold-yantrai.fly.dev")
            ?: "https://foothold-yantrai.fly.dev"
        set(v) = p.edit().putString("baseUrl", v.trimEnd('/')).apply()

    /** FOOTHOLD_TOKEN — sent as `Authorization: Bearer <token>`. */
    var token: String
        get() = p.getString("token", "") ?: ""
        set(v) = p.edit().putString("token", v.trim()).apply()

    /** last call-log row timestamp we already processed (dedupe) */
    var lastCallTs: Long
        get() = p.getLong("lastCallTs", 0L)
        set(v) = p.edit().putLong("lastCallTs", v).apply()

    /** tagged numbers, stored as last-10-digits */
    var taggedDigits: Set<String>
        get() = p.getStringSet("taggedDigits", emptySet()) ?: emptySet()
        set(v) = p.edit().putStringSet("taggedDigits", v).apply()

    /** "name||digits" pairs so the WhatsApp listener can resolve a contact name → number */
    var taggedNamePairs: Set<String>
        get() = p.getStringSet("taggedNames", emptySet()) ?: emptySet()
        set(v) = p.edit().putStringSet("taggedNames", v).apply()

    fun isTagged(phone: String?): Boolean = digits(phone)?.let { taggedDigits.contains(it) } ?: false

    /** resolve a WhatsApp notification title (a contact/display name) to a tagged number */
    fun digitsForName(title: String?): String? {
        if (title.isNullOrBlank()) return null
        val t = title.trim().lowercase()
        return taggedNamePairs.firstOrNull { it.substringBefore("||").lowercase() == t }
            ?.substringAfter("||")
    }

    companion object {
        fun digits(phone: String?): String? {
            val d = phone?.filter { it.isDigit() } ?: return null
            return if (d.length >= 10) d.takeLast(10) else null
        }
    }
}
