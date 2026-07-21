package dev.rohit.foothold.capture;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.provider.Settings;
import android.view.Gravity;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * One-time WhatsApp-capture setup: enter backend URL + FOOTHOLD_TOKEN, sync the
 * tagged leads, grant Notification access. Launch via:
 *   adb shell am start -n dev.rohit.foothold/dev.rohit.foothold.capture.CaptureSetupActivity
 */
public class CaptureSetupActivity extends Activity {
    private CaptureConfig cfg;
    private CaptureApi api;
    private TextView status;

    @Override
    protected void onCreate(Bundle s) {
        super.onCreate(s);
        cfg = new CaptureConfig(this);
        api = new CaptureApi(cfg);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(48, 96, 48, 48);

        TextView h = new TextView(this);
        h.setText("Foothold — WhatsApp capture setup");
        h.setTextSize(18f);
        h.setPadding(0, 0, 0, 24);

        final EditText url = new EditText(this);
        url.setHint("Backend URL");
        url.setText(cfg.baseUrl());
        final EditText token = new EditText(this);
        token.setHint("Access token (FOOTHOLD_TOKEN)");
        token.setText(cfg.token());

        status = new TextView(this);
        status.setPadding(0, 24, 0, 24);
        status.setGravity(Gravity.CENTER);

        Button connect = new Button(this);
        connect.setText("Connect + sync leads");
        connect.setOnClickListener(v -> {
            cfg.setBaseUrl(url.getText().toString());
            cfg.setToken(token.getText().toString());
            status.setText("Syncing…");
            api.refreshTagged(() -> runOnUiThread(
                () -> status.setText("Synced " + cfg.taggedNames().size() + " tagged leads ✓")));
        });

        Button notif = new Button(this);
        notif.setText("Grant Notification access");
        notif.setOnClickListener(v ->
            startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)));

        root.addView(h);
        root.addView(url);
        root.addView(token);
        root.addView(connect);
        root.addView(notif);
        root.addView(status);
        setContentView(root);
    }
}
