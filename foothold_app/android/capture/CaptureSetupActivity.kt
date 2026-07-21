package dev.rohit.foothold.capture

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.Gravity
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat

/**
 * One-time capture setup. Enter the backend URL + FOOTHOLD_TOKEN, validate, grant
 * runtime permissions + Notification access, then start the foreground service.
 * Built programmatically to keep it to one file.
 *
 * ⚠ WRITTEN, NOT DEVICE-VERIFIED here.
 */
class CaptureSetupActivity : AppCompatActivity() {
    private lateinit var cfg: CaptureConfig
    private lateinit var api: CaptureApi
    private lateinit var status: TextView

    private val perms = buildList {
        add(Manifest.permission.READ_CALL_LOG)
        add(Manifest.permission.READ_PHONE_STATE)
        add(Manifest.permission.READ_CONTACTS)
        if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
    }.toTypedArray()

    override fun onCreate(s: Bundle?) {
        super.onCreate(s)
        cfg = CaptureConfig(this); api = CaptureApi(cfg)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; setPadding(48, 64, 48, 48)
        }
        val url = EditText(this).apply { hint = "Backend URL"; setText(cfg.baseUrl) }
        val token = EditText(this).apply { hint = "Access token (FOOTHOLD_TOKEN)"; setText(cfg.token) }
        status = TextView(this).apply { setPadding(0, 24, 0, 24); gravity = Gravity.CENTER }

        val connect = Button(this).apply {
            text = "Connect"
            setOnClickListener {
                cfg.baseUrl = url.text.toString()
                cfg.token = token.text.toString()
                api.connect { ok ->
                    runOnUiThread { status.text = if (ok) "Connected ✓" else "Connect failed — check URL/token" }
                    if (ok) api.refreshTagged()
                }
            }
        }
        val grant = Button(this).apply {
            text = "Grant call & contacts permissions"
            setOnClickListener { ActivityCompat.requestPermissions(this@CaptureSetupActivity, perms, 7) }
        }
        val notif = Button(this).apply {
            text = "Enable WhatsApp capture (Notification access)"
            setOnClickListener { startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }
        }
        val startBtn = Button(this).apply {
            text = "Start capturing"
            setOnClickListener {
                CallCaptureService.start(this@CaptureSetupActivity)
                status.text = "Capturing — tagged leads only. Raw data stays on device."
            }
        }
        listOf(url, token, connect, grant, notif, startBtn, status).forEach { root.addView(it) }
        setContentView(root)
    }

    override fun onRequestPermissionsResult(rc: Int, p: Array<out String>, g: IntArray) {
        super.onRequestPermissionsResult(rc, p, g)
        val ok = g.isNotEmpty() && g.all { it == PackageManager.PERMISSION_GRANTED }
        status.text = if (ok) "Permissions granted ✓" else "Some permissions denied"
    }
}
