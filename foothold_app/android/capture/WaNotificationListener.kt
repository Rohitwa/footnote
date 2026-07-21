package dev.rohit.foothold.capture

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/**
 * Reads incoming WhatsApp notifications and turns messages from TAGGED leads into
 * captures POSTed to /api/v1/capture/inbound (channel=whatsapp, direction=in). The
 * heavier intent extraction happens server-side; here we just forward the text.
 *
 * Honest limits (unchanged from the design in project_realestate_foothold):
 *  - Inbound only (notifications don't carry the salesman's outbound).
 *  - WhatsApp notifications carry the contact DISPLAY NAME, not a number, so we
 *    resolve name → tagged number via the synced registry. Full history needs the
 *    WhatsApp Business API.
 *  - Requires "Notification access" (special settings toggle).
 *  - Tagged leads only — every other chat is ignored on-device.
 *
 * ⚠ WRITTEN, NOT DEVICE-VERIFIED here.
 */
class WaNotificationListener : NotificationListenerService() {
    private val cfg by lazy { CaptureConfig(this) }
    private val api by lazy { CaptureApi(cfg) }
    private var lastKey: String = ""

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val pkg = sbn.packageName
        if (pkg != "com.whatsapp" && pkg != "com.whatsapp.w4b") return

        val ex = sbn.notification.extras
        val title = ex.getCharSequence(Notification.EXTRA_TITLE)?.toString()   // sender / group name
        val text = ex.getCharSequence(Notification.EXTRA_TEXT)?.toString() ?: return

        if (title.isNullOrBlank() || text.startsWith("📞")) return             // skip calls/typing
        val key = "$title|$text"
        if (key == lastKey) return; lastKey = key

        val digits = cfg.digitsForName(title) ?: return   // PRIVACY: only tagged leads
        api.capture(digits, "whatsapp", text.take(200), "in")
    }
}
