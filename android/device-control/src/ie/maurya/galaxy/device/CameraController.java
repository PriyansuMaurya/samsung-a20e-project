package ie.maurya.galaxy.device;

import android.content.Context;
import android.graphics.ImageFormat;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CameraMetadata;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.util.Size;
import android.view.Surface;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.ByteBuffer;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.LinkedBlockingQueue;

/**
 * Camera2 preview loop that doubles as the still-camera and the live-stream
 * source.
 *
 * The device has no hardware encoder path exposed to a third-party app for
 * arbitrary screen content, and MediaRecorder would take the camera away from
 * the preview. So one repeating JPEG session feeds the browser (multipart),
 * the snapshot endpoint and the photo list. Recording video from the same
 * sensor would need a second MediaRecorder session; that limitation is reported
 * by the dashboard rather than hidden.
 */
final class CameraController {
    static final String TAG = "GalaxyCamera";

    interface FrameListener {
        void onFrame(byte[] jpeg);
    }

    private final Context context;
    private final CameraManager manager;
    private final HandlerThread thread;
    private final Handler handler;
    private final Set<LinkedBlockingQueue<byte[]>> subscribers =
            Collections.newSetFromMap(new ConcurrentHashMap<LinkedBlockingQueue<byte[]>, Boolean>());
    private volatile FrameListener listener;

    private CameraDevice device;
    private CameraCaptureSession session;
    private CaptureRequest.Builder builder;
    private ImageReader reader;
    private Size openSize;
    private String openId = "";
    private volatile byte[] latest;
    private volatile long latestAt;
    private volatile int frames;
    private String pendingId = "";
    private String pendingSize = "";

    CameraController(Context context) {
        this.context = context;
        this.manager = (CameraManager) context.getSystemService(Context.CAMERA_SERVICE);
        this.thread = new HandlerThread("galaxy-camera");
        this.thread.start();
        this.handler = new Handler(thread.getLooper());
    }

    String state() {
        return openId;
    }

    String openSize() {
        Size size = openSize;
        return size == null ? "" : size.getWidth() + "x" + size.getHeight();
    }

    int frameCount() {
        return frames;
    }

    void setListener(FrameListener next) {
        listener = next;
    }

    LinkedBlockingQueue<byte[]> subscribe() {
        LinkedBlockingQueue<byte[]> queue = new LinkedBlockingQueue<>(2);
        subscribers.add(queue);
        return queue;
    }

    void unsubscribe(LinkedBlockingQueue<byte[]> queue) {
        subscribers.remove(queue);
    }

    int subscriberCount() {
        return subscribers.size();
    }

    /**
     * The torch must go through the open session while the preview holds the
     * camera: setTorchMode fails with CAMERA_IN_USE otherwise.
     */
    synchronized boolean setTorch(boolean on) {
        CaptureRequest.Builder active = builder;
        CameraCaptureSession open = session;
        if (active == null || open == null) {
            return false;
        }
        try {
            active.set(CaptureRequest.FLASH_MODE,
                    on ? CameraMetadata.FLASH_MODE_TORCH : CameraMetadata.FLASH_MODE_OFF);
            open.setRepeatingRequest(active.build(), null, handler);
            return true;
        } catch (Exception error) {
            Log.w(TAG, "torch through session failed: " + error);
            return false;
        }
    }

    /** Returns the current camera ids as "id:facing" so the UI can label them. */
    List<String> cameras() {
        List<String> found = new ArrayList<>();
        try {
            for (String id : manager.getCameraIdList()) {
                CameraCharacteristics info = manager.getCameraCharacteristics(id);
                Integer facing = info.get(CameraCharacteristics.LENS_FACING);
                found.add(id + ":" + (facing != null && facing == CameraCharacteristics.LENS_FACING_FRONT
                        ? "front" : "back"));
            }
        } catch (CameraAccessException error) {
            Log.w(TAG, "cannot list cameras: " + error.getMessage());
        }
        return found;
    }

    synchronized void start(String id, String wantSize) {
        if (device != null && id.equals(openId)) {
            return;
        }
        pendingId = id == null || id.isEmpty() ? "0" : id;
        pendingSize = wantSize == null ? "" : wantSize;
        closeQuietly();
        openDevice(pendingId);
    }

    void awaitFirstFrame(long timeoutMs) {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            if (latest != null) {
                return;
            }
            try {
                Thread.sleep(50);
            } catch (InterruptedException error) {
                return;
            }
        }
    }

    byte[] capture() throws DeviceApi.ApiError {
        byte[] frame = latest;
        if (frame == null) {
            throw new DeviceApi.ApiError(503, "camera produced no frame");
        }
        byte[] copy = new byte[frame.length];
        System.arraycopy(frame, 0, copy, 0, frame.length);
        return copy;
    }

    File saveCapture(byte[] jpeg, String id) throws Exception {
        File directory = new File(context.getFilesDir(), "photos");
        if (!directory.exists() && !directory.mkdirs()) {
            throw new DeviceApi.ApiError(500, "cannot create the photos directory");
        }
        String stamp = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date());
        File target = new File(directory, "cam" + id + "-" + stamp + ".jpg");
        try (FileOutputStream out = new FileOutputStream(target)) {
            out.write(jpeg);
        }
        return target;
    }

    synchronized void stop() {
        closeQuietly();
    }

    private void openDevice(String id) {
        try {
            Size size = chooseSize(id, pendingSize);
            openSize = size;
            reader = ImageReader.newInstance(size.getWidth(), size.getHeight(),
                    ImageFormat.JPEG, 3);
            reader.setOnImageAvailableListener(new ImageReader.OnImageAvailableListener() {
                @Override
                public void onImageAvailable(ImageReader source) {
                    Image image = null;
                    try {
                        image = source.acquireNextImage();
                        if (image == null) {
                            return;
                        }
                        ByteBuffer buffer = image.getPlanes()[0].getBuffer();
                        byte[] jpeg = new byte[buffer.remaining()];
                        buffer.get(jpeg);
                        latest = jpeg;
                        latestAt = System.currentTimeMillis();
                        frames++;
                        FrameListener current = listener;
                        if (current != null) {
                            current.onFrame(jpeg);
                        }
                        for (LinkedBlockingQueue<byte[]> queue : subscribers) {
                            queue.offer(jpeg);
                        }
                    } catch (Exception error) {
                        Log.w(TAG, "frame failed: " + error);
                    } finally {
                        if (image != null) {
                            image.close();
                        }
                    }
                }
            }, handler);

            manager.openCamera(id, new CameraDevice.StateCallback() {
                @Override
                public void onOpened(CameraDevice opened) {
                    device = opened;
                    openId = id;
                    latest = null;
                    frames = 0;
                    createSession(opened);
                }

                @Override
                public void onDisconnected(CameraDevice closed) {
                    Log.w(TAG, "camera disconnected");
                    closeQuietly();
                }

                @Override
                public void onError(CameraDevice failed, int code) {
                    Log.w(TAG, "camera error " + code);
                    closeQuietly();
                }
            }, handler);
        } catch (CameraAccessException error) {
            Log.w(TAG, "cannot open camera: " + error.getMessage());
            closeQuietly();
        } catch (SecurityException error) {
            Log.w(TAG, "camera permission missing");
            closeQuietly();
        } catch (IllegalArgumentException error) {
            Log.w(TAG, "camera unavailable: " + error.getMessage());
            closeQuietly();
        }
    }

    private void createSession(CameraDevice opened) {
        try {
            Surface surface = reader.getSurface();
            final CaptureRequest.Builder request =
                    opened.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
            request.addTarget(surface);
            request.set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_AUTO);
            request.set(CaptureRequest.JPEG_QUALITY, (byte) 80);
            builder = request;
            opened.createCaptureSession(Collections.singletonList(surface),
                    new CameraCaptureSession.StateCallback() {
                        @Override
                        public void onConfigured(CameraCaptureSession ready) {
                            session = ready;
                            try {
                                ready.setRepeatingRequest(request.build(), null, handler);
                            } catch (CameraAccessException error) {
                                Log.w(TAG, "cannot start preview: " + error.getMessage());
                            }
                        }

                        @Override
                        public void onConfigureFailed(CameraCaptureSession failed) {
                            Log.w(TAG, "camera session failed");
                            closeQuietly();
                        }
                    }, handler);
        } catch (CameraAccessException error) {
            Log.w(TAG, "cannot create session: " + error.getMessage());
            closeQuietly();
        }
    }

    private Size chooseSize(String id, String wanted) {
        Size fallback = new Size(1280, 720);
        try {
            CameraCharacteristics info = manager.getCameraCharacteristics(id);
            StreamConfigurationMap map =
                    info.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            if (map == null) {
                return fallback;
            }
            Size[] sizes = map.getOutputSizes(ImageFormat.JPEG);
            if (sizes == null || sizes.length == 0) {
                return fallback;
            }
            if (wanted != null && !wanted.isEmpty()) {
                String[] parts = wanted.toLowerCase(Locale.US).split("x");
                if (parts.length == 2) {
                    try {
                        Size exact = new Size(Integer.parseInt(parts[0]), Integer.parseInt(parts[1]));
                        for (Size size : sizes) {
                            if (size.equals(exact)) {
                                return size;
                            }
                        }
                    } catch (NumberFormatException ignored) {
                    }
                }
            }
            Size best = null;
            long bestPixels = 0;
            for (Size size : sizes) {
                long pixels = (long) size.getWidth() * size.getHeight();
                if (pixels <= 1920L * 1080L && pixels > bestPixels) {
                    best = size;
                    bestPixels = pixels;
                }
            }
            return best == null ? sizes[0] : best;
        } catch (CameraAccessException | IllegalArgumentException error) {
            return fallback;
        }
    }

    private synchronized void closeQuietly() {
        if (session != null) {
            try {
                session.stopRepeating();
            } catch (Exception ignored) {
            }
            try {
                session.close();
            } catch (Exception ignored) {
            }
            session = null;
        }
        if (device != null) {
            try {
                device.close();
            } catch (Exception ignored) {
            }
            device = null;
        }
        if (reader != null) {
            try {
                reader.close();
            } catch (Exception ignored) {
            }
            reader = null;
        }
        openId = "";
        openSize = null;
        builder = null;
        latest = null;
    }

    long lastFrameAt() {
        return latestAt;
    }
}
