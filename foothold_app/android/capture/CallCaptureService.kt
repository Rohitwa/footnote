package dev.rohit.foothold.capture

import android.app.*
import android.content.Context
import android.content.Intent
import android.database.ContentObserver
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.CallLog

/**
 * Foreground service watching the call log. When a call to a TAGGED lead ends it
 * POSTs the call metadata (direction + duration) to /api/v1/capture/inbound, which
 * appends it to the lead's Lead Brain, rescoress, and notifies the current owner.
 * No call-audio recording (Android blocks that; recorded audio + tone comes later
 * via the server-side CPaaS bridge → /api/v1/webhooks/call).
 *
 * ⚠ WRITTEN, NOT DEVICE-VERIFIED here.
 */
class CallCaptureService : Service() {
    private lateinit var cfg: CaptureConfig
    private lateinit var api: CaptureApi
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var observer: ContentObserver

    override fun onCreate() {
        super.onCreate()
        cfg = CaptureConfig(this); api = CaptureApi(cfg)
        startForeground(NOTIF_ID, buildOngoing())
        observer = object : ContentObserver(handler) {
            override fun onChange(selfChange: Boolean) = checkLatestCall()
        }
        contentResolver.registerContentObserver(CallLog.Calls.CONTENT_URI, true, observer)
        api.refreshTagged()
    }

    private fun checkLatestCall() {
        val cols = arrayOf(CallLog.Calls.NUMBER, CallLog.Calls.TYPE, CallLog.Calls.DURATION, CallLog.Calls.DATE)
        contentResolver.query(CallLog.Calls.CONTENT_URI, cols, null, null, CallLog.Calls.DATE + " DESC LIMIT 1")
            ?.use { c ->
                if (!c.moveToFirst()) return
                val number = c.getString(0)
                val type = c.getInt(1)
                val dur = c.getLong(2)
                val ts = c.getLong(3)
                if (ts <= cfg.lastCallTs) return             // already processed
                cfg.lastCallTs = ts
                if (!cfg.isTagged(number)) return             // PRIVACY: tagged leads only
                val outgoing = type == CallLog.Calls.OUTGOING_TYPE
                val dir = if (outgoing) "Outgoing" else "Incoming"
                val mins = "%d:%02d".format(dur / 60, dur % 60)
                api.capture(number, "call", "$dir call · $mins",
                    if (outgoing) "out" else "in")
                notifyLogCall(number)
            }
    }

    /** Tap → open the app to this lead so the salesman does the 2-tap intent capture. */
    private fun notifyLogCall(number: String) {
        val deep = Intent(Intent.ACTION_VIEW).apply {
            data = android.net.Uri.parse(cfg.baseUrl + "/?capture=" +
                (CaptureConfig.digits(number) ?: ""))
            flags = Intent.FLAG_ACTIVITY_NEW_TASK
        }
        val pi = PendingIntent.getActivity(this, 1, deep,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val n = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setContentTitle("Log the call?")
            .setContentText("Tap to capture what they wanted — it updates the score.")
            .setAutoCancel(true).setContentIntent(pi).build()
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).notify(2, n)
    }

    private fun buildOngoing(): Notification {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (android.os.Build.VERSION.SDK_INT >= 26)
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Foothold capture", NotificationManager.IMPORTANCE_LOW))
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_call)
            .setContentTitle("Foothold")
            .setContentText("Capturing activity for your leads only").build()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int) = START_STICKY
    override fun onBind(intent: Intent?): IBinder? = null
    override fun onDestroy() { contentResolver.unregisterContentObserver(observer); super.onDestroy() }

    companion object {
        private const val CHANNEL = "foothold_capture"; private const val NOTIF_ID = 1
        fun start(ctx: Context) = ctx.startForegroundService(Intent(ctx, CallCaptureService::class.java))
    }
}
