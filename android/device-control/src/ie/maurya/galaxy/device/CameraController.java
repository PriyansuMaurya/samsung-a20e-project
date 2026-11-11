package ie.maurya.galaxy.device;

import android.content.Context;
import android.graphics.ImageFormat;
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
import android.media.MediaRecorder;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.util.Size;
import android.view.Surface;

import org.json.JSONObject;

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
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * Camera2 preview loop that doubles as the still-camera, the live-stream source
 * and the video recorder.
 *
 * One repeating session feeds the browser (multipart), snapshots and the photo
 * list. Recording adds MediaRecorder's surface as a second target of the same
 * session, which Camera2 allows for one JPEG and one PRIV stream, so the video
 * is recorded with sound while the preview keeps running. MediaRecorder's own
 * camera binding is deliberately not used: that is what ends the preview.
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
    private MediaRecorder recorder;
    private File videoFile;
    private long videoStarted;
    private boolean videoAudio;
    private volatile boolean recording;
    private CountDownLatch sessionReady;
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
        if (recording) {
            stopVideoQuietly();
        }
        closeQuietly();
    }

    boolean recording() {
        return recording;
    }

    JSONObject videoStatus() throws Exception {
        JSONObject out = new JSONObject().put("recording", recording);
        File current = videoFile;
        if (current != null) {
            out.put("file", current.getName());
            out.put("bytes", current.length());
            out.put("seconds", Math.max(0, (System.currentTimeMillis() - videoStarted) / 1000));
        }
        out.put("audio", videoAudio);
        return out;
    }

    /**
     * Records the same session the preview is using. Audio is optional because
     * MediaRecorder cannot share the microphone with an AudioRecord capture.
     */
    synchronized JSONObject startVideo(boolean wantAudio) throws Exception {
        if (recording) {
            throw new DeviceApi.ApiError(409, "a video is already recording");
        }
        if (device == null || session == null) {
            // The sensor drops out after a recording on this device (camera
            // error 4), so pressing Record again must reopen it rather than
            // ask the user to fiddle with the preview first.
            if (!reopenForUse()) {
                throw new DeviceApi.ApiError(503,
                        "the camera did not open; check the preview and camera permission");
            }
        }
        CameraDevice open = device;
        CameraCaptureSession current = session;
        if (open == null || current == null) {
            throw new DeviceApi.ApiError(503, "the camera is not available");
        }
        File directory = new File(context.getFilesDir(), "videos");
        if (!directory.exists() && !directory.mkdirs()) {
            throw new DeviceApi.ApiError(500, "cannot create the videos directory");
        }
        String stamp = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date());
        File target = new File(directory, "vid-" + stamp + ".mp4");
        boolean audio = wantAudio && prepareRecorder(target, true);
        if (!audio && !prepareRecorder(target, false)) {
            throw new DeviceApi.ApiError(503, "the encoder refused the video settings");
        }
        videoFile = target;
        videoAudio = audio;
        try {
            current.stopRepeating();
        } catch (Exception ignored) {
        }
        try {
            current.close();
        } catch (Exception ignored) {
        }
        session = null;
        sessionReady = new CountDownLatch(1);
        createSession(open, true);
        boolean ready = sessionReady.await(8, TimeUnit.SECONDS) && recording;
        if (!ready) {
            stopVideoQuietly();
            throw new DeviceApi.ApiError(503, "the camera refused to start recording");
        }
        videoStarted = System.currentTimeMillis();
        return videoStatus();
    }

    synchronized JSONObject stopVideo() throws Exception {
        File finished = videoFile;
        if (finished == null) {
            // Nothing was recording (or the sensor dropped and cleared it).
            stopVideoQuietly();
            return new JSONObject().put("ok", true).put("file", "")
                    .put("bytes", 0).put("seconds", 0).put("audio", false);
        }
        long seconds = Math.max(0, (System.currentTimeMillis() - videoStarted) / 1000);
        boolean audio = videoAudio;
        stopVideoQuietly();
        return new JSONObject().put("ok", true)
                .put("file", finished.getName()).put("bytes", finished.length())
                .put("seconds", seconds).put("audio", audio);
    }

    /**
     * Opens the sensor again after the camera service has torn it down, and
     * waits until a repeating session is actually configured.
     */
    private boolean reopenForUse() {
        String id = pendingId == null || pendingId.isEmpty() ? "0" : pendingId;
        closeQuietly();
        openDevice(id);
        awaitFirstFrame(3000);
        if (device != null && session != null) {
            return true;
        }
        closeQuietly();
        return false;
    }

    private boolean prepareRecorder(File target, boolean audio) {
        MediaRecorder next = new MediaRecorder();
        try {
            next.setVideoSource(MediaRecorder.VideoSource.SURFACE);
            if (audio) {
                next.setAudioSource(MediaRecorder.AudioSource.MIC);
            }
            next.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);
            next.setVideoEncoder(MediaRecorder.VideoEncoder.H264);
            if (audio) {
                next.setAudioEncoder(MediaRecorder.AudioEncoder.AAC);
                next.setAudioChannels(1);
                next.setAudioSamplingRate(44100);
                next.setAudioEncodingBitRate(96000);
            }
            next.setVideoSize(1280, 720);
            next.setVideoFrameRate(30);
            next.setVideoEncodingBitRate(6_000_000);
            next.setOutputFile(target.getAbsolutePath());
            next.prepare();
        } catch (Exception error) {
            Log.w(TAG, "recorder refused audio=" + audio + ": " + error);
            try {
                next.release();
            } catch (Exception ignored) {
            }
            return false;
        }
        MediaRecorder previous = recorder;
        recorder = next;
        if (previous != null && previous != next) {
            try {
                previous.release();
            } catch (Exception ignored) {
            }
        }
        return true;
    }

    private void stopVideoQuietly() {
        recording = false;
        CameraCaptureSession current = session;
        if (current != null) {
            try {
                current.stopRepeating();
            } catch (Exception ignored) {
            }
            try {
                current.close();
            } catch (Exception ignored) {
            }
            session = null;
        }
        MediaRecorder active = recorder;
        recorder = null;
        if (active != null) {
            try {
                active.stop();
            } catch (RuntimeException error) {
                // Less than a frame was captured; the file is still written.
                Log.w(TAG, "recorder stopped early: " + error);
            }
            try {
                active.release();
            } catch (Exception ignored) {
            }
        }
        videoFile = null;
        videoAudio = false;
        videoStarted = 0L;
        CameraDevice open = device;
        if (open != null) {
            sessionReady = new CountDownLatch(1);
            createSession(open, false);
            try {
                sessionReady.await(3, TimeUnit.SECONDS);
            } catch (InterruptedException ignored) {
            }
            if (session == null && subscriberCount() > 0) {
                // The device often reports camera error 4 after a recording;
                // the preview stream is still attached, so bring it back.
                reopenForUse();
            }
        }
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
                    sessionReady = new CountDownLatch(1);
                    createSession(opened, false);
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

    private void createSession(CameraDevice opened, boolean withRecorder) {
        CountDownLatch ready = sessionReady;
        try {
            final List<Surface> targets = new ArrayList<>();
            targets.add(reader.getSurface());
            final MediaRecorder active = withRecorder ? recorder : null;
            if (active != null) {
                targets.add(active.getSurface());
            }
            final CaptureRequest.Builder request = opened.createCaptureRequest(
                    withRecorder ? CameraDevice.TEMPLATE_RECORD : CameraDevice.TEMPLATE_PREVIEW);
            for (Surface target : targets) {
                request.addTarget(target);
            }
            request.set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_AUTO);
            request.set(CaptureRequest.JPEG_QUALITY, (byte) 80);
            builder = request;
            opened.createCaptureSession(targets,
                    new CameraCaptureSession.StateCallback() {
                        @Override
                        public void onConfigured(CameraCaptureSession configured) {
                            session = configured;
                            try {
                                if (active != null) {
                                    active.start();
                                    recording = true;
                                }
                                configured.setRepeatingRequest(request.build(), null, handler);
                            } catch (Exception error) {
                                Log.w(TAG, "cannot start session: " + error.getMessage());
                            } finally {
                                finishSetup(ready);
                            }
                        }

                        @Override
                        public void onConfigureFailed(CameraCaptureSession failed) {
                            Log.w(TAG, "camera session failed");
                            finishSetup(ready);
                            if (withRecorder) {
                                stopVideoQuietly();
                            } else {
                                closeQuietly();
                            }
                        }
                    }, handler);
        } catch (Exception error) {
            Log.w(TAG, "cannot create session: " + error.getMessage());
            finishSetup(ready);
            if (!withRecorder) {
                closeQuietly();
            }
        }
    }

    private static void finishSetup(CountDownLatch ready) {
        if (ready != null) {
            ready.countDown();
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
        if (recorder != null) {
            try {
                recorder.stop();
            } catch (Exception ignored) {
            }
            try {
                recorder.release();
            } catch (Exception ignored) {
            }
            recorder = null;
        }
        recording = false;
        recorder = null;
        openId = "";
        openSize = null;
        builder = null;
        latest = null;
        videoFile = null;
        videoAudio = false;
        videoStarted = 0L;
    }

    long lastFrameAt() {
        return latestAt;
    }
}
