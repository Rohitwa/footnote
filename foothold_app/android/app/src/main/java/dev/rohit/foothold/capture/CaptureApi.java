package dev.rohit.foothold.capture;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Zero-dependency HTTP client for the Foothold backend (bearer-authed). */
public class CaptureApi {
    private final CaptureConfig cfg;
    private final ExecutorService io = Executors.newSingleThreadExecutor();

    public CaptureApi(CaptureConfig c) { cfg = c; }

    private JSONObject req(String method, String path, JSONObject body) {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(cfg.baseUrl() + path).openConnection();
            c.setRequestMethod(method);
            c.setConnectTimeout(8000);
            c.setReadTimeout(8000);
            c.setRequestProperty("Content-Type", "application/json");
            if (!cfg.token().isEmpty()) c.setRequestProperty("Authorization", "Bearer " + cfg.token());
            if (body != null) {
                c.setDoOutput(true);
                OutputStream os = c.getOutputStream();
                os.write(body.toString().getBytes("UTF-8"));
                os.close();
            }
            int code = c.getResponseCode();
            if (code >= 200 && code < 300) {
                BufferedReader br = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"));
                StringBuilder sb = new StringBuilder();
                String line;
                while ((line = br.readLine()) != null) sb.append(line);
                br.close();
                return new JSONObject(sb.toString());
            }
        } catch (Exception ignored) { }
        return null;
    }

    /** Pull leads → build the tagged name→number registry (dm_name incl. Hindi). */
    public void refreshTagged(final Runnable done) {
        io.execute(new Runnable() {
            public void run() {
                JSONObject r = req("GET", "/api/v1/companies", null);
                if (r != null) {
                    JSONArray arr = r.optJSONArray("companies");
                    if (arr != null) {
                        Set<String> names = new HashSet<String>();
                        for (int i = 0; i < arr.length(); i++) {
                            JSONObject l = arr.optJSONObject(i);
                            if (l == null) continue;
                            String phone = l.optString("dm_phone", "");
                            if (phone.isEmpty()) phone = l.optString("dm_whatsapp", "");
                            String d = CaptureConfig.digits(phone);
                            String nm = l.optString("dm_name", "");
                            if (nm.isEmpty()) nm = l.optString("name", "");
                            if (d != null && !nm.trim().isEmpty()) names.add(nm.trim() + "||" + d);
                        }
                        if (!names.isEmpty()) cfg.setTaggedNames(names);
                    }
                }
                if (done != null) done.run();
            }
        });
    }

    /** POST a captured touch → backend re-matches by phone + scores it. */
    public void capture(final String phone, final String channel, final String text, final String direction) {
        io.execute(new Runnable() {
            public void run() {
                try {
                    JSONObject b = new JSONObject();
                    b.put("channel", channel);
                    b.put("from_phone", phone);
                    b.put("text", text);
                    b.put("direction", direction);
                    req("POST", "/api/v1/capture/inbound", b);
                } catch (Exception ignored) { }
            }
        });
    }
}
