package ie.maurya.galaxy.device;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

/** Keeps the control server alive while the phone is idle. */
public final class ControlService extends Service {
    static final String ACTION_START = "ie.maurya.galaxy.device.START";
    static final String ACTION_STOP = "ie.maurya.galaxy.device.STOP";
    static final String CHANNEL = "device-control";
    static final int NOTIFICATION_ID = 4201;

    private static volatile boolean running = false;
    private static volatile long startedAt = 0L;

    private LocalServer server;
    private AudioController audio;
    private PowerManager.WakeLock wakeLock;

    static boolean running() {
        return running;
    }

    static long startedAt() {
        return startedAt;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
        startedAt = System.currentTimeMillis();
        DeviceApi api = new DeviceApi(this);
        CameraController camera = new CameraController(this);
        audio = new AudioController(this);
        server = new LocalServer(this, api, camera, audio);
        try {
            server.start();
        } catch (Exception error) {
            Log.e(LocalServer.TAG, "could not start control server: " + error);
        }
        PowerManager power = (PowerManager) getSystemService(Context.POWER_SERVICE);
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "galaxy:device-control");
        wakeLock.setReferenceCounted(false);
        wakeLock.acquire();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pending = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification notification = new Notification.Builder(this, CHANNEL)
                .setContentTitle("Galaxy device control is armed")
                .setContentText("Cameras, microphone and accessories can be driven from the console")
                .setSmallIcon(android.R.drawable.ic_menu_camera)
                .setContentIntent(pending)
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
                            | ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
        running = true;
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (server != null) {
            server.stop();
        }
        if (audio != null) {
            audio.stopAll();
        }
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    long uptimeSeconds() {
        return startedAt == 0L ? 0 : (System.currentTimeMillis() - startedAt) / 1000L;
    }

    private void createChannel() {
        NotificationManager manager =
                (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        NotificationChannel channel = new NotificationChannel(
                CHANNEL, "Device control", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("Shown while the phone accepts remote accessory control");
        manager.createNotificationChannel(channel);
    }
}
