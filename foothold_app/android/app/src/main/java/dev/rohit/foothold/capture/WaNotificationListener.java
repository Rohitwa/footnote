package dev.rohit.foothold.capture;

import android.app.Notification;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

/**
 * Reads incoming WhatsApp notifications; for a TAGGED lead (name resolved via
 * the synced registry, incl. Hindi names) it POSTs the message to
 * /api/v1/capture/inbound, which scores it. Inbound-only; requires the user to
 * grant "Notification access".
 */
public class WaNotificationListener extends NotificationListenerService {
    private CaptureConfig cfg;
    private CaptureApi api;

    @Override
    public void onCreate() {
        super.onCreate();
        cfg = new CaptureConfig(this);
        api = new CaptureApi(cfg);
    }

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        String pkg = sbn.getPackageName();
        if (!"com.whatsapp".equals(pkg) && !"com.whatsapp.w4b".equals(pkg)) return;

        Bundle ex = sbn.getNotification().extras;
        if (ex == null) return;
        CharSequence titleCs = ex.getCharSequence(Notification.EXTRA_TITLE);
        CharSequence textCs = ex.getCharSequence(Notification.EXTRA_TEXT);
        if (titleCs == null || textCs == null) return;

        String title = titleCs.toString();
        String text = textCs.toString();
        if (text.startsWith("📞")) return;          // skip call notifications (📞)

        String key = title + "|" + text;
        if (key.equals(cfg.lastKey())) return;                 // dedupe repeats
        cfg.setLastKey(key);

        String digits = cfg.digitsForName(title);
        if (digits == null) return;                            // PRIVACY: tagged leads only
        api.capture(digits, "whatsapp", text.length() > 200 ? text.substring(0, 200) : text, "in");
    }
}
