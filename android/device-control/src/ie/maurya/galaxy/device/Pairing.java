package ie.maurya.galaxy.device;

import android.content.Context;
import android.content.SharedPreferences;

import java.security.MessageDigest;
import java.security.SecureRandom;

/** Shared pairing secret. The dashboard generates it; the app only stores it. */
final class Pairing {
    private static final String PREFS = "galaxy-device";
    private static final String TOKEN = "token";
    private static final SecureRandom RANDOM = new SecureRandom();

    private static volatile long windowUntil = 0L;

    private Pairing() {
    }

    static void openWindow(long seconds) {
        windowUntil = System.currentTimeMillis() + seconds * 1000L;
    }

    static boolean windowOpen() {
        return System.currentTimeMillis() < windowUntil;
    }

    static String token(Context context) {
        return prefs(context).getString(TOKEN, "");
    }

    static boolean paired(Context context) {
        return !token(context).isEmpty();
    }

    /** Returns false when no pairing window is open or the value is unusable. */
    static boolean accept(Context context, String token) {
        if (!windowOpen() || token == null) {
            return false;
        }
        String trimmed = token.trim();
        if (trimmed.length() < 16 || trimmed.length() > 128) {
            return false;
        }
        prefs(context).edit().putString(TOKEN, trimmed).apply();
        windowUntil = 0L;
        return true;
    }

    static boolean matches(Context context, String provided) {
        String stored = token(context);
        if (stored.isEmpty() || provided == null || provided.isEmpty()) {
            return false;
        }
        return MessageDigest.isEqual(stored.getBytes(), provided.getBytes());
    }

    static void forget(Context context) {
        prefs(context).edit().remove(TOKEN).apply();
    }

    static String newToken() {
        byte[] raw = new byte[24];
        RANDOM.nextBytes(raw);
        StringBuilder out = new StringBuilder(raw.length * 2);
        for (byte value : raw) {
            out.append(String.format("%02x", value));
        }
        return out.toString();
    }

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }
}
