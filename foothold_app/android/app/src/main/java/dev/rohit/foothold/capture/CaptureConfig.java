package dev.rohit.foothold.capture;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.HashSet;
import java.util.Set;

/**
 * On-device config + the tagged name→number registry (the privacy boundary).
 * Synced from GET /api/v1/companies (dm_name → last-10 digits), so a WhatsApp
 * notification whose title is a tagged lead's name (incl. Hindi) resolves to
 * that lead's number. Untagged chats never leave the device.
 */
public class CaptureConfig {
    private final SharedPreferences p;

    public CaptureConfig(Context ctx) {
        p = ctx.getSharedPreferences("foothold_capture", Context.MODE_PRIVATE);
    }

    public String baseUrl() { return p.getString("baseUrl", "https://foothold-yantrai.fly.dev"); }
    public void setBaseUrl(String v) { p.edit().putString("baseUrl", v.replaceAll("/+$", "")).apply(); }

    public String token() { return p.getString("token", ""); }
    public void setToken(String v) { p.edit().putString("token", v.trim()).apply(); }

    public Set<String> taggedNames() { return p.getStringSet("taggedNames", new HashSet<String>()); }
    public void setTaggedNames(Set<String> s) { p.edit().putStringSet("taggedNames", s).apply(); }

    public String lastKey() { return p.getString("lastKey", ""); }
    public void setLastKey(String v) { p.edit().putString("lastKey", v).apply(); }

    /** last-10 digits of a phone, or null. */
    public static String digits(String phone) {
        if (phone == null) return null;
        String d = phone.replaceAll("[^0-9]", "");
        return d.length() >= 10 ? d.substring(d.length() - 10) : null;
    }

    /** Resolve a WhatsApp notification title (contact display name, incl. Hindi)
     *  to a tagged lead number. Case-insensitive exact match on the name. */
    public String digitsForName(String title) {
        if (title == null) return null;
        String t = title.trim().toLowerCase();
        for (String pair : taggedNames()) {
            int i = pair.indexOf("||");
            if (i > 0 && pair.substring(0, i).trim().toLowerCase().equals(t)) {
                return pair.substring(i + 2);
            }
        }
        return null;
    }
}
