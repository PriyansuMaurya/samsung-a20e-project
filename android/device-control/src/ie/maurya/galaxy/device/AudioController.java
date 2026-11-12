package ie.maurya.galaxy.device;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaPlayer;
import android.media.MediaRecorder;
import android.util.Log;

import org.json.JSONObject;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.text.SimpleDateFormat;
import java.util.Collections;
import java.util.Date;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.LinkedBlockingQueue;

/**
 * Microphone capture and media playback.
 *
 * One capture loop serves both the live stream and the recorder, because the
 * platform allows a single AudioRecord per microphone. Playback is a separate
 * MediaPlayer so two-way audio works while the dashboard is open: the phone
 * plays what the browser uploaded, and the microphone keeps streaming.
 */
final class AudioController {
    static final String TAG = "GalaxyAudio";
    static final int SAMPLE_RATE = 16000;
    private static final int KEEPALIVE_RATE = 8000;

    private final Context context;
    private final Set<LinkedBlockingQueue<byte[]>> subscribers =
            Collections.newSetFromMap(new ConcurrentHashMap<LinkedBlockingQueue<byte[]>, Boolean>());

    private AudioRecord record;
    private Thread pump;
    private volatile boolean capturing;
    private volatile boolean recording;
    private RandomAccessFile recordingFile;
    private String recordingName = "";
    private long recordingBytes;
    private long recordingStarted;
    private MediaPlayer player;
    private MediaPlayer keepAlive;
    private String playing = "";
    private volatile boolean preparing;
    private volatile String playbackError = "";
    private final Object lock = new Object();

    AudioController(Context context) {
        this.context = context;
    }

    LinkedBlockingQueue<byte[]> subscribe() {
        LinkedBlockingQueue<byte[]> queue = new LinkedBlockingQueue<>(8);
        subscribers.add(queue);
        ensureCapture();
        return queue;
    }

    void unsubscribe(LinkedBlockingQueue<byte[]> queue) {
        subscribers.remove(queue);
        if (subscribers.isEmpty() && !recording) {
            stopCapture();
        }
    }

    /** True while the microphone is held: MediaRecorder cannot share it. */
    boolean busy() {
        return recording || !subscribers.isEmpty();
    }

    JSONObject status() throws Exception {
        JSONObject payload = new JSONObject()
                .put("recording", recording)
                .put("file", recordingName)
                .put("bytes", recordingBytes)
                .put("streaming", !subscribers.isEmpty())
                .put("sample_rate", SAMPLE_RATE)
                .put("playing", playing)
                .put("state", playbackState())
                .put("keep_alive", keepAlive != null)
                .put("error", playbackError);
        if (recording && recordingStarted > 0) {
            payload.put("seconds", (System.currentTimeMillis() - recordingStarted) / 1000);
        }
        MediaPlayer current = player;
        if (current != null) {
            try {
                payload.put("position_ms", current.getCurrentPosition());
                payload.put("duration_ms", current.getDuration());
                payload.put("paused", !current.isPlaying());
            } catch (IllegalStateException ignored) {
            }
        }
        return payload;
    }

    JSONObject startRecording() throws Exception {
        synchronized (lock) {
            if (recording) {
                return new JSONObject().put("ok", true).put("file", recordingName)
                        .put("already", true);
            }
            File directory = new File(context.getFilesDir(), "recordings");
            if (!directory.exists() && !directory.mkdirs()) {
                throw new DeviceApi.ApiError(500, "cannot create the recordings directory");
            }
            String stamp = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date());
            recordingName = "mic-" + stamp + ".wav";
            File target = new File(directory, recordingName);
            recordingFile = new RandomAccessFile(target, "rw");
            recordingFile.setLength(0);
            recordingFile.write(new byte[44]);
            recordingBytes = 0;
            recordingStarted = System.currentTimeMillis();
            recording = true;
        }
        ensureCapture();
        return new JSONObject().put("ok", true).put("file", recordingName);
    }

    JSONObject stopRecording() throws Exception {
        File target;
        long bytes;
        long seconds;
        synchronized (lock) {
            if (!recording) {
                return new JSONObject().put("ok", true).put("file", "").put("bytes", 0);
            }
            recording = false;
            bytes = recordingBytes;
            seconds = Math.max(0, (System.currentTimeMillis() - recordingStarted) / 1000);
            try {
                finalizeWav(recordingFile, bytes);
                recordingFile.close();
            } catch (Exception error) {
                Log.w(TAG, "cannot finalize recording: " + error);
            }
            recordingFile = null;
            target = new File(new File(context.getFilesDir(), "recordings"), recordingName);
            recordingName = "";
            recordingBytes = 0;
            recordingStarted = 0;
        }
        if (subscribers.isEmpty()) {
            stopCapture();
        }
        return new JSONObject().put("ok", true)
                .put("file", target.getName())
                .put("bytes", bytes)
                .put("seconds", seconds);
    }

    JSONObject play(File file) throws Exception {
        synchronized (lock) {
            releasePlayer();
            MediaPlayer next = mediaPlayer();
            next.setDataSource(file.getAbsolutePath());
            next.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
                @Override
                public void onCompletion(MediaPlayer done) {
                    synchronized (lock) {
                        playing = "";
                        preparing = false;
                    }
                }
            });
            next.prepare();
            next.start();
            player = next;
            playing = file.getName();
            preparing = false;
            playbackError = "";
        }
        return new JSONObject().put("ok", true).put("file", file.getName());
    }

    /**
     * Stream a remote track straight from its URL. Preparing happens off the
     * request thread because a CDN can take seconds to answer, and the phone
     * keeps playing after the dashboard tab closes.
     */
    JSONObject playUrl(String url, String label) throws Exception {
        synchronized (lock) {
            releasePlayer();
            final MediaPlayer next = mediaPlayer();
            next.setDataSource(url);
            next.setOnPreparedListener(new MediaPlayer.OnPreparedListener() {
                @Override
                public void onPrepared(MediaPlayer ready) {
                    synchronized (lock) {
                        if (player != ready) {
                            return;
                        }
                        preparing = false;
                        try {
                            ready.start();
                        } catch (IllegalStateException error) {
                            playbackError = "the phone could not start this track";
                        }
                    }
                }
            });
            next.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
                @Override
                public void onCompletion(MediaPlayer done) {
                    synchronized (lock) {
                        if (player == done) {
                            releasePlayer();
                        }
                        playing = "";
                        preparing = false;
                    }
                }
            });
            next.setOnErrorListener(new MediaPlayer.OnErrorListener() {
                @Override
                public boolean onError(MediaPlayer failed, int what, int extra) {
                    synchronized (lock) {
                        if (player == failed) {
                            releasePlayer();
                        }
                        playing = "";
                        preparing = false;
                        playbackError = "the phone could not stream this track";
                        Log.w(TAG, "stream failed: what=" + what + " extra=" + extra);
                    }
                    return true;
                }
            });
            player = next;
            playing = label;
            preparing = true;
            playbackError = "";
            try {
                next.prepareAsync();
            } catch (Exception error) {
                releasePlayer();
                playing = "";
                preparing = false;
                playbackError = "the phone could not open this track";
                throw new DeviceApi.ApiError(500, playbackError);
            }
        }
        return new JSONObject().put("ok", true).put("file", label);
    }

    private MediaPlayer mediaPlayer() {
        MediaPlayer next = new MediaPlayer();
        next.setAudioAttributes(new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                .build());
        return next;
    }

    private String playbackState() {
        if (!playbackError.isEmpty()) {
            return "error";
        }
        MediaPlayer current = player;
        if (current == null) {
            return "idle";
        }
        if (preparing) {
            return "loading";
        }
        try {
            return current.isPlaying() ? "playing" : "paused";
        } catch (IllegalStateException error) {
            return "loading";
        }
    }

    /**
     * Hold the phone out of the freezer with a silent loop.
     *
     * Samsung freezes an idle app once the screen goes off, even while its
     * foreground service runs, and a frozen process answers nothing: remote
     * control dies until someone opens the app. A phone that is playing audio
     * is never frozen, so an armed service loops one second of silence. It is
     * inaudible, takes no audio focus, and stops with the service.
     */
    void startKeepAlive() {
        synchronized (lock) {
            if (keepAlive != null) {
                return;
            }
            try {
                MediaPlayer loop = mediaPlayer();
                loop.setDataSource(silentFile().getAbsolutePath());
                loop.setLooping(true);
                loop.prepare();
                loop.start();
                keepAlive = loop;
            } catch (Exception error) {
                Log.w(TAG, "cannot hold the phone awake with silence: " + error);
            }
        }
    }

    private void stopKeepAlive() {
        MediaPlayer loop = keepAlive;
        keepAlive = null;
        if (loop == null) {
            return;
        }
        try {
            loop.stop();
        } catch (Exception ignored) {
        }
        try {
            loop.release();
        } catch (Exception ignored) {
        }
    }

    /** One second of 16-bit mono silence, written once next to the app. */
    private File silentFile() throws Exception {
        File directory = new File(context.getFilesDir(), "keepalive");
        File file = new File(directory, "silence.wav");
        long bytes = KEEPALIVE_RATE * 2L;
        if (file.isFile() && file.length() == 44 + bytes) {
            return file;
        }
        if (!directory.isDirectory() && !directory.mkdirs()) {
            throw new IOException("cannot create the keepalive directory");
        }
        RandomAccessFile out = new RandomAccessFile(file, "rw");
        try {
            out.setLength(0);
            out.write(new byte[(int) (44 + bytes)]);
            finalizeWav(out, bytes);
        } finally {
            out.close();
        }
        return file;
    }

    JSONObject control(String action) throws Exception {
        synchronized (lock) {
            MediaPlayer current = player;
            if (current == null) {
                return new JSONObject().put("ok", false).put("error", "nothing is playing");
            }
            if ("pause".equals(action)) {
                current.pause();
            } else if ("resume".equals(action)) {
                current.start();
            } else if ("stop".equals(action)) {
                releasePlayer();
                playing = "";
                preparing = false;
                playbackError = "";
            } else {
                throw new DeviceApi.ApiError(400, "unknown media action");
            }
            return new JSONObject().put("ok", true).put("action", action).put("playing", playing);
        }
    }

    void stopAll() {
        synchronized (lock) {
            stopKeepAlive();
            releasePlayer();
            playing = "";
            preparing = false;
            playbackError = "";
        }
        stopCapture();
    }

    private void releasePlayer() {
        MediaPlayer current = player;
        player = null;
        if (current != null) {
            try {
                current.stop();
            } catch (Exception ignored) {
            }
            try {
                current.release();
            } catch (Exception ignored) {
            }
        }
    }

    private void ensureCapture() {
        synchronized (lock) {
            if (capturing) {
                return;
            }
            int minimum = AudioRecord.getMinBufferSize(SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            int buffer = Math.max(minimum * 2, SAMPLE_RATE);
            try {
                record = new AudioRecord(MediaRecorder.AudioSource.MIC, SAMPLE_RATE,
                        AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, buffer);
            } catch (Exception error) {
                Log.w(TAG, "cannot create AudioRecord: " + error);
                return;
            }
            if (record.getState() != AudioRecord.STATE_INITIALIZED) {
                Log.w(TAG, "AudioRecord did not initialise");
                try {
                    record.release();
                } catch (Exception ignored) {
                }
                record = null;
                return;
            }
            record.startRecording();
            capturing = true;
            final AudioRecord active = record;
            pump = new Thread(new Runnable() {
                @Override
                public void run() {
                    byte[] chunk = new byte[1600];
                    while (capturing) {
                        int read;
                        try {
                            read = active.read(chunk, 0, chunk.length);
                        } catch (Exception error) {
                            break;
                        }
                        if (read <= 0) {
                            continue;
                        }
                        byte[] copy = new byte[read];
                        System.arraycopy(chunk, 0, copy, 0, read);
                        synchronized (lock) {
                            if (recording && recordingFile != null) {
                                try {
                                    recordingFile.write(copy);
                                    recordingBytes += read;
                                } catch (Exception error) {
                                    Log.w(TAG, "cannot write recording: " + error);
                                }
                            }
                        }
                        for (LinkedBlockingQueue<byte[]> queue : subscribers) {
                            queue.offer(copy);
                        }
                        if (subscribers.isEmpty() && !recording) {
                            break;
                        }
                    }
                }
            }, "galaxy-mic");
            pump.setDaemon(true);
            pump.start();
        }
    }

    private void stopCapture() {
        synchronized (lock) {
            capturing = false;
            if (record != null) {
                try {
                    record.stop();
                } catch (Exception ignored) {
                }
                try {
                    record.release();
                } catch (Exception ignored) {
                }
                record = null;
            }
            if (recording && recordingFile != null) {
                try {
                    finalizeWav(recordingFile, recordingBytes);
                    recordingFile.close();
                } catch (Exception error) {
                    Log.w(TAG, "cannot close recording: " + error);
                }
                recordingFile = null;
                recording = false;
            }
        }
    }

    private static void finalizeWav(RandomAccessFile file, long dataBytes) throws Exception {
        long size = Math.min(dataBytes, 0xFFFFFFFFL - 36);
        file.seek(0);
        file.write("RIFF".getBytes("ISO-8859-1"));
        writeInt(file, 36 + size);
        file.write("WAVEfmt ".getBytes("ISO-8859-1"));
        writeInt(file, 16);
        writeShort(file, 1);
        writeShort(file, 1);
        writeInt(file, SAMPLE_RATE);
        writeInt(file, SAMPLE_RATE * 2);
        writeShort(file, 2);
        writeShort(file, 16);
        file.write("data".getBytes("ISO-8859-1"));
        writeInt(file, size);
    }

    private static void writeInt(RandomAccessFile file, long value) throws Exception {
        file.write((int) (value & 0xff));
        file.write((int) ((value >> 8) & 0xff));
        file.write((int) ((value >> 16) & 0xff));
        file.write((int) ((value >> 24) & 0xff));
    }

    private static void writeShort(RandomAccessFile file, int value) throws Exception {
        file.write(value & 0xff);
        file.write((value >> 8) & 0xff);
    }
}
