package ie.maurya.galaxy.device;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.bluetooth.BluetoothAdapter;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.AudioManager;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.StatFs;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.provider.Settings;
import android.util.Size;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/** Everything the HTTP layer is allowed to ask the phone to do. */
final class DeviceApi {
    private final Context context;

    DeviceApi(Context context) {
        this.context = context.getApplicationContext();
    }

    boolean cameraGranted() {
        return context.checkSelfPermission(Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED;
    }

    boolean microphoneGranted() {
        return context.checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED;
    }

    /** The torch lives on a camera that has a flash unit. */
    private String flashCameraId() throws Exception {
        CameraManager manager = (CameraManager) context.getSystemService(Context.CAMERA_SERVICE);
        for (String id : manager.getCameraIdList()) {
            CameraCharacteristics characteristics = manager.getCameraCharacteristics(id);
            Boolean flash = characteristics.get(CameraCharacteristics.FLASH_INFO_AVAILABLE);
            Integer facing = characteristics.get(CameraCharacteristics.LENS_FACING);
            if (Boolean.TRUE.equals(flash)
                    && (facing == null || facing == CameraCharacteristics.LENS_FACING_BACK)) {
                return id;
            }
        }
        return null;
    }

    boolean cameraHasFlash(String id) {
        try {
            CameraManager manager =
                    (CameraManager) context.getSystemService(Context.CAMERA_SERVICE);
            CameraCharacteristics characteristics = manager.getCameraCharacteristics(id);
            return Boolean.TRUE.equals(characteristics.get(
                    CameraCharacteristics.FLASH_INFO_AVAILABLE));
        } catch (Exception error) {
            return false;
        }
    }

    JSONObject torch(boolean on) throws Exception {
        if (!cameraGranted()) {
            throw new ApiError(403, "camera permission is not granted to Galaxy Device");
        }
        String id = flashCameraId();
        if (id == null) {
            throw new ApiError(404, "no camera with a flash unit");
        }
        CameraManager manager = (CameraManager) context.getSystemService(Context.CAMERA_SERVICE);
        manager.setTorchMode(id, on);
        return new JSONObject().put("ok", true).put("torch", on).put("camera", id);
    }

    JSONObject vibrate(long milliseconds) throws Exception {
        Vibrator vibrator = (Vibrator) context.getSystemService(Context.VIBRATOR_SERVICE);
        if (vibrator == null || !vibrator.hasVibrator()) {
            throw new ApiError(404, "no vibrator");
        }
        long length = Math.max(1, Math.min(milliseconds, 5000));
        vibrator.vibrate(VibrationEffect.createOneShot(length, VibrationEffect.DEFAULT_AMPLITUDE));
        return new JSONObject().put("ok", true).put("ms", length);
    }

    JSONObject battery() throws Exception {
        Intent status = context.registerReceiver(
                null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if (status == null) {
            throw new ApiError(503, "battery state unavailable");
        }
        int level = status.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
        int scale = status.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
        int plugged = status.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0);
        JSONObject out = new JSONObject();
        out.put("percentage", scale > 0 ? Math.round(level * 100f / scale) : -1);
        out.put("plugged", plugged != 0);
        out.put("status", status.getIntExtra(BatteryManager.EXTRA_STATUS, -1));
        out.put("health", status.getIntExtra(BatteryManager.EXTRA_HEALTH, -1));
        out.put("temperature_c", status.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) / 10.0);
        out.put("voltage_mv", status.getIntExtra(BatteryManager.EXTRA_VOLTAGE, 0));
        out.put("technology", status.getStringExtra(BatteryManager.EXTRA_TECHNOLOGY));
        return out;
    }

    JSONObject volume() {
        AudioManager audio = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
        JSONObject streams = new JSONObject();
        int[] known = {AudioManager.STREAM_MUSIC, AudioManager.STREAM_RING,
                AudioManager.STREAM_ALARM, AudioManager.STREAM_NOTIFICATION,
                AudioManager.STREAM_VOICE_CALL};
        for (int stream : known) {
            try {
                JSONObject entry = new JSONObject();
                entry.put("index", audio.getStreamVolume(stream));
                entry.put("max", audio.getStreamMaxVolume(stream));
                streams.put(streamName(stream), entry);
            } catch (Exception ignored) {
                // A stream the device does not expose is simply absent.
            }
        }
        try {
            JSONObject out = new JSONObject();
            out.put("streams", streams);
            out.put("ringer_mode", audio.getRingerMode());
            out.put("music_active", audio.isMusicActive());
            return out;
        } catch (Exception error) {
            throw new ApiError(500, "volume state unavailable");
        }
    }

    JSONObject setVolume(String name, int index) throws Exception {
        AudioManager audio = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
        int stream = streamFromName(name);
        int max = audio.getStreamMaxVolume(stream);
        int clamped = Math.max(0, Math.min(index, max));
        audio.setStreamVolume(stream, clamped, 0);
        return volume().put("stream", name).put("index", clamped);
    }

    private static String streamName(int stream) {
        switch (stream) {
            case AudioManager.STREAM_MUSIC: return "music";
            case AudioManager.STREAM_RING: return "ring";
            case AudioManager.STREAM_ALARM: return "alarm";
            case AudioManager.STREAM_NOTIFICATION: return "notification";
            case AudioManager.STREAM_VOICE_CALL: return "call";
            default: return "stream" + stream;
        }
    }

    private static int streamFromName(String name) {
        switch (name == null ? "music" : name) {
            case "ring": return AudioManager.STREAM_RING;
            case "alarm": return AudioManager.STREAM_ALARM;
            case "notification": return AudioManager.STREAM_NOTIFICATION;
            case "call": return AudioManager.STREAM_VOICE_CALL;
            default: return AudioManager.STREAM_MUSIC;
        }
    }

    JSONObject capabilities() throws Exception {
        JSONObject out = new JSONObject();
        out.put("model", Build.MODEL);
        out.put("device", Build.DEVICE);
        out.put("android", Build.VERSION.RELEASE);
        out.put("sdk", Build.VERSION.SDK_INT);

        JSONObject permissions = new JSONObject();
        permissions.put("camera", cameraGranted());
        permissions.put("microphone", microphoneGranted());
        out.put("permissions", permissions);

        CameraManager manager = (CameraManager) context.getSystemService(Context.CAMERA_SERVICE);
        JSONArray cameras = new JSONArray();
        for (String id : manager.getCameraIdList()) {
            CameraCharacteristics characteristics = manager.getCameraCharacteristics(id);
            JSONObject entry = new JSONObject();
            entry.put("id", id);
            Integer facing = characteristics.get(CameraCharacteristics.LENS_FACING);
            entry.put("facing", facing == null ? "unknown"
                    : facing == CameraCharacteristics.LENS_FACING_BACK ? "back"
                    : facing == CameraCharacteristics.LENS_FACING_FRONT ? "front" : "external");
            entry.put("flash", Boolean.TRUE.equals(
                    characteristics.get(CameraCharacteristics.FLASH_INFO_AVAILABLE)));
            StreamConfigurationMap map = characteristics.get(
                    CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            if (map != null) {
                JSONArray sizes = new JSONArray();
                Size[] jpeg = map.getOutputSizes(android.graphics.ImageFormat.JPEG);
                int limit = jpeg == null ? 0 : Math.min(jpeg.length, 24);
                for (int index = 0; index < limit; index++) {
                    sizes.put(jpeg[index].getWidth() + "x" + jpeg[index].getHeight());
                }
                entry.put("jpeg_sizes", sizes);
            }
            cameras.put(entry);
        }
        out.put("cameras", cameras);

        SensorManager sensors = (SensorManager) context.getSystemService(Context.SENSOR_SERVICE);
        JSONArray names = new JSONArray();
        for (Sensor sensor : sensors.getSensorList(Sensor.TYPE_ALL)) {
            names.put(sensor.getName());
        }
        out.put("sensors", names);
        out.put("volume", volume());
        out.put("storage", storage());
        out.put("brightness", brightnessValue());
        out.put("brightness_writable", Settings.System.canWrite(context));
        out.put("recording_supported", true);
        return out;
    }

    /** Read-only: this app never turns a radio on or off. */
    JSONObject connectivity() throws Exception {
        JSONObject out = new JSONObject();
        ConnectivityManager manager =
                (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
        Network active = manager.getActiveNetwork();
        if (active != null) {
            NetworkCapabilities cap = manager.getNetworkCapabilities(active);
            if (cap != null) {
                out.put("wifi", cap.hasTransport(NetworkCapabilities.TRANSPORT_WIFI));
                out.put("cellular", cap.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR));
                out.put("metered", !cap.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED));
                out.put("validated", cap.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED));
                out.put("down_mbps", cap.getLinkDownstreamBandwidthKbps() / 1000);
                out.put("up_mbps", cap.getLinkUpstreamBandwidthKbps() / 1000);
            }
            LinkProperties props = manager.getLinkProperties(active);
            if (props != null) {
                out.put("interface", props.getInterfaceName());
                JSONArray addresses = new JSONArray();
                for (LinkAddress address : props.getLinkAddresses()) {
                    addresses.put(address.getAddress().getHostAddress());
                }
                out.put("addresses", addresses);
            }
        }
        WifiManager wifi = (WifiManager) context.getApplicationContext()
                .getSystemService(Context.WIFI_SERVICE);
        if (wifi != null) {
            out.put("wifi_enabled", wifi.isWifiEnabled());
            try {
                WifiInfo info = wifi.getConnectionInfo();
                if (info != null) {
                    String ssid = info.getSSID();
                    if (ssid != null) {
                        ssid = ssid.replace("\"", "");
                    }
                    // Android 10+ withholds the SSID unless the app also holds
                    // a location grant, which this one does not request.
                    out.put("ssid", ssid == null || ssid.isEmpty()
                            || ssid.contains("<unknown") ? "hidden by Android" : ssid);
                    out.put("rssi_dbm", info.getRssi());
                    out.put("link_mbps", info.getLinkSpeed());
                }
            } catch (SecurityException error) {
                // Android 10+ hides the SSID without a location grant.
                out.put("ssid", "hidden by Android");
            }
        }
        BluetoothAdapter bluetooth = BluetoothAdapter.getDefaultAdapter();
        out.put("bluetooth_enabled", bluetooth != null && bluetooth.isEnabled());
        out.put("airplane_mode", Settings.Global.getInt(context.getContentResolver(),
                Settings.Global.AIRPLANE_MODE_ON, 0) != 0);
        return out;
    }

    JSONObject storage() throws Exception {
        JSONObject out = new JSONObject();
        StatFs data = new StatFs(context.getFilesDir().getAbsolutePath());
        long total = data.getTotalBytes();
        long free = data.getAvailableBytes();
        out.put("total_bytes", total);
        out.put("free_bytes", free);
        out.put("used_bytes", total - free);
        JSONObject folders = new JSONObject();
        for (String name : new String[]{"photos", "recordings", "media"}) {
            File directory = new File(context.getFilesDir(), name);
            JSONObject entry = new JSONObject();
            entry.put("bytes", folderSize(directory));
            File[] files = directory.listFiles();
            entry.put("count", files == null ? 0 : files.length);
            folders.put(name, entry);
        }
        out.put("folders", folders);
        return out;
    }

    private static long folderSize(File directory) {
        long total = 0;
        File[] files = directory.listFiles();
        if (files != null) {
            for (File file : files) {
                if (file.isFile()) {
                    total += file.length();
                }
            }
        }
        return total;
    }

    private int brightnessValue() {
        try {
            return Settings.System.getInt(context.getContentResolver(),
                    Settings.System.SCREEN_BRIGHTNESS);
        } catch (Exception error) {
            return -1;
        }
    }

    JSONObject brightness(int value) throws Exception {
        if (!Settings.System.canWrite(context)) {
            throw new ApiError(403,
                    "the write-settings app-op is not granted; run: adb shell appops set "
                            + context.getPackageName() + " android:write_settings allow");
        }
        int max = 255;
        try {
            max = Settings.System.getInt(context.getContentResolver(),
                    "screen_brightness_max_value");
        } catch (Exception ignored) {
        }
        int target = value < 0 ? brightnessValue() : Math.max(1, Math.min(value, max));
        boolean ok = value >= 0 && Settings.System.putInt(context.getContentResolver(),
                Settings.System.SCREEN_BRIGHTNESS, target);
        if (value >= 0) {
            // Automatic brightness overrides a manual value, so switches to manual.
            Settings.System.putInt(context.getContentResolver(),
                    Settings.System.SCREEN_BRIGHTNESS_MODE,
                    Settings.System.SCREEN_BRIGHTNESS_MODE_MANUAL);
        }
        return new JSONObject().put("ok", ok || value < 0)
                .put("value", brightnessValue()).put("max", max)
                .put("automatic", Settings.System.getInt(context.getContentResolver(),
                        Settings.System.SCREEN_BRIGHTNESS_MODE, 0)
                        != Settings.System.SCREEN_BRIGHTNESS_MODE_MANUAL);
    }

    JSONObject sensors() throws Exception {
        SensorManager manager = (SensorManager) context.getSystemService(Context.SENSOR_SERVICE);
        JSONObject out = new JSONObject();
        Handler handler = new Handler(Looper.getMainLooper());
        int[] wanted = {Sensor.TYPE_ACCELEROMETER, Sensor.TYPE_LIGHT, Sensor.TYPE_PROXIMITY,
                Sensor.TYPE_GYROSCOPE, Sensor.TYPE_MAGNETIC_FIELD};
        String[] names = {"accelerometer", "light", "proximity", "gyroscope", "magnetic"};
        for (int index = 0; index < wanted.length; index++) {
            Sensor sensor = manager.getDefaultSensor(wanted[index]);
            if (sensor == null) {
                continue;
            }
            final JSONObject reading = new JSONObject();
            final CountDownLatch latch = new CountDownLatch(1);
            SensorEventListener listener = new SensorEventListener() {
                @Override
                public void onSensorChanged(SensorEvent event) {
                    try {
                        JSONArray values = new JSONArray();
                        for (float value : event.values) {
                            values.put(Math.round(value * 1000.0) / 1000.0);
                        }
                        reading.put("values", values);
                        reading.put("at", event.timestamp / 1000000L);
                    } catch (Exception ignored) {
                    }
                    latch.countDown();
                }

                @Override
                public void onAccuracyChanged(Sensor changed, int accuracy) {
                }
            };
            manager.registerListener(listener, sensor, 100000, handler);
            try {
                latch.await(800, TimeUnit.MILLISECONDS);
            } finally {
                manager.unregisterListener(listener);
            }
            if (reading.has("values")) {
                reading.put("name", sensor.getName());
                out.put(names[index], reading);
            }
        }
        return out;
    }

    static final class ApiError extends RuntimeException {
        final int status;

        ApiError(int status, String message) {
            super(message);
            this.status = status;
        }
    }
}
