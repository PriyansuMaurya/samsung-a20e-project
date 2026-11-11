package ie.maurya.galaxy.device;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

/**
 * Brings the control service back after a reboot.
 *
 * The pairing token lives in the app's own preferences, so re-arming does not
 * ask anyone to pair again: the dashboard keeps the token it stored and simply
 * finds the port open once boot finishes. Android only delivers this broadcast
 * after the app has been launched at least once since installation.
 */
public final class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || !Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) {
            return;
        }
        Intent service = new Intent(context, ControlService.class)
                .setAction(ControlService.ACTION_START);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(service);
            } else {
                context.startService(service);
            }
            Log.i(LocalServer.TAG, "control service re-armed after boot");
        } catch (RuntimeException error) {
            Log.w(LocalServer.TAG, "could not re-arm after boot: " + error);
        }
    }
}
