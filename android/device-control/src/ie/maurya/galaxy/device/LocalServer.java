package ie.maurya.galaxy.device;

import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * A deliberately small HTTP server bound to 127.0.0.1 only.
 *
 * The dashboard on the phone reaches it through loopback and puts it behind the
 * session cookie and a password re-entry, so this server sees only requests the
 * console already authorised. It never binds a public interface and refuses
 * anything without the pairing token (except /health and a timed /pair).
 */
final class LocalServer {
    static final String TAG = "GalaxyDevice";
    static final int PORT = 8790;
    private static final int MAX_BODY = 8 * 1024 * 1024;
    private static final String BOUNDARY = "galaxyframe";

    private final ControlService service;
    private final DeviceApi api;
    private final CameraController camera;
    private final AudioController audio;
    private volatile ServerSocket socket;
    private volatile boolean running;

    LocalServer(ControlService service, DeviceApi api, CameraController camera,
                AudioController audio) {
        this.service = service;
        this.api = api;
        this.camera = camera;
        this.audio = audio;
    }

    boolean running() {
        return running;
    }

    int port() {
        ServerSocket current = socket;
        return current == null ? PORT : current.getLocalPort();
    }

    void start() throws IOException {
        ServerSocket server = new ServerSocket(PORT, 16, InetAddress.getByName("127.0.0.1"));
        server.setReuseAddress(true);
        socket = server;
        running = true;
        Thread acceptor = new Thread(new Runnable() {
            @Override
            public void run() {
                while (running) {
                    try {
                        final Socket client = server.accept();
                        Thread worker = new Thread(new Runnable() {
                            @Override
                            public void run() {
                                handle(client);
                            }
                        }, "device-http");
                        worker.setDaemon(true);
                        worker.start();
                    } catch (IOException error) {
                        if (running) {
                            Log.w(TAG, "accept failed: " + error.getMessage());
                        }
                    }
                }
            }
        }, "device-accept");
        acceptor.setDaemon(true);
        acceptor.start();
        Log.i(TAG, "control server listening on 127.0.0.1:" + PORT);
    }

    void stop() {
        running = false;
        ServerSocket current = socket;
        socket = null;
        if (current != null) {
            try {
                current.close();
            } catch (IOException ignored) {
            }
        }
        camera.stop();
    }

    private void handle(Socket client) {
        try (Socket open = client) {
            open.setSoTimeout(20000);
            InputStream in = new BufferedInputStream(open.getInputStream());
            OutputStream out = new BufferedOutputStream(open.getOutputStream());
            Request request = read(in);
            if (request == null) {
                writeJson(out, 400, error("malformed request"));
                return;
            }
            try {
                route(request, out);
            } catch (DeviceApi.ApiError error) {
                writeJson(out, error.status, error(error.getMessage()));
            } catch (Exception error) {
                Log.w(TAG, "request failed: " + error);
                writeJson(out, 500, error(String.valueOf(error.getMessage())));
            }
        } catch (Exception error) {
            Log.w(TAG, "connection failed: " + error);
        }
    }

    private void route(Request request, OutputStream out) throws Exception {
        String path = request.path;
        String method = request.method;

        if ("GET".equals(method) && "/health".equals(path)) {
            writeJson(out, 200, new JSONObject()
                    .put("ok", true)
                    .put("service", "galaxy-device")
                    .put("paired", Pairing.paired(service))
                    .put("pairing_window", Pairing.windowOpen())
                    .put("uptime_s", service.uptimeSeconds())
                    .put("port", port())
                    .put("camera_open", camera.state()));
            return;
        }
        if ("POST".equals(method) && "/pair".equals(path)) {
            if (!Pairing.windowOpen()) {
                writeJson(out, 403, error("pairing window is closed; open it in the app"));
                return;
            }
            if (!Pairing.accept(service, request.json().optString("token", ""))) {
                writeJson(out, 400, error("token must be 16 to 128 characters"));
                return;
            }
            writeJson(out, 200, new JSONObject().put("ok", true).put("paired", true));
            return;
        }
        if (!Pairing.matches(service, request.headers.get("x-galaxy-token"))) {
            writeJson(out, 401, error("pairing token required"));
            return;
        }

        if ("GET".equals(method) && "/capabilities".equals(path)) {
            JSONObject payload = api.capabilities();
            payload.put("camera_stream", camera.state());
            writeJson(out, 200, payload);
            return;
        }
        if ("GET".equals(method) && "/battery".equals(path)) {
            writeJson(out, 200, api.battery());
            return;
        }
        if ("GET".equals(method) && "/storage".equals(path)) {
            writeJson(out, 200, api.storage());
            return;
        }
        if ("GET".equals(method) && "/sensors".equals(path)) {
            writeJson(out, 200, api.sensors());
            return;
        }
        if ("GET".equals(method) && "/connectivity".equals(path)) {
            writeJson(out, 200, api.connectivity());
            return;
        }
        if ("GET".equals(method) && "/volume".equals(path)) {
            writeJson(out, 200, api.volume());
            return;
        }
        if ("POST".equals(method) && "/volume".equals(path)) {
            JSONObject body = request.json();
            writeJson(out, 200, api.setVolume(
                    body.optString("stream", "music"), body.optInt("index", -1)));
            return;
        }
        if ("POST".equals(method) && "/torch".equals(path)) {
            boolean on = request.json().optBoolean("on", false);
            String open = camera.state();
            if (!open.isEmpty() && api.cameraHasFlash(open) && camera.setTorch(on)) {
                writeJson(out, 200, new JSONObject().put("ok", true).put("torch", on)
                        .put("camera", open).put("via", "session"));
                return;
            }
            writeJson(out, 200, api.torch(on));
            return;
        }
        if ("POST".equals(method) && "/vibrate".equals(path)) {
            writeJson(out, 200, api.vibrate(request.json().optLong("ms", 200)));
            return;
        }
        if ("POST".equals(method) && "/brightness".equals(path)) {
            writeJson(out, 200, api.brightness(request.json().optInt("value", -1)));
            return;
        }
        if ("GET".equals(method) && "/rotation".equals(path)) {
            writeJson(out, 200, api.rotationState());
            return;
        }
        if ("POST".equals(method) && "/rotation".equals(path)) {
            JSONObject body = request.json();
            Integer degrees = body.has("degrees") && !body.isNull("degrees")
                    ? body.optInt("degrees", 0) : null;
            writeJson(out, 200, api.rotation(degrees));
            return;
        }

        if ("GET".equals(method) && "/video/status".equals(path)) {
            writeJson(out, 200, camera.videoStatus());
            return;
        }
        if ("POST".equals(method) && "/video/start".equals(path)) {
            boolean audio = request.json().optBoolean("audio", true) && !this.audio.busy();
            writeJson(out, 200, camera.startVideo(audio));
            return;
        }
        if ("POST".equals(method) && "/video/stop".equals(path)) {
            writeJson(out, 200, camera.stopVideo());
            return;
        }
        if ("GET".equals(method) && "/videos".equals(path)) {
            writeJson(out, 200, listFolder("videos"));
            return;
        }
        if ("GET".equals(method) && "/video".equals(path)) {
            writeFile(out, folderFile("videos", request.query.getOrDefault("file", "")));
            return;
        }
        if ("POST".equals(method) && "/video/delete".equals(path)) {
            File target = folderFile("videos", request.json().optString("file", ""));
            if (!target.delete()) {
                throw new DeviceApi.ApiError(500, "could not delete the video");
            }
            writeJson(out, 200, new JSONObject().put("ok", true));
            return;
        }

        if ("POST".equals(method) && "/camera/start".equals(path)) {
            JSONObject body = request.json();
            camera.start(body.optString("camera", "0"), body.optString("size", ""));
            camera.awaitFirstFrame(2500);
            writeJson(out, 200, new JSONObject().put("ok", true)
                    .put("camera", body.optString("camera", "0"))
                    .put("size", camera.openSize())
                    .put("frames", camera.frameCount()));
            return;
        }
        if ("POST".equals(method) && "/camera/stop".equals(path)) {
            camera.stop();
            writeJson(out, 200, new JSONObject().put("ok", true).put("camera", ""));
            return;
        }
        if ("GET".equals(method) && "/camera/snapshot".equals(path)) {
            String id = request.query.getOrDefault("camera", "0");
            String size = request.query.getOrDefault("size", "");
            camera.start(id, size);
            camera.awaitFirstFrame(2500);
            byte[] frame = camera.capture();
            File saved = camera.saveCapture(frame, id);
            String opened = camera.openSize();
            if (camera.subscriberCount() == 0 && !camera.recording()) {
                // Release the sensor so the torch and other apps work again,
                // but never cut a recording short for a still.
                camera.stop();
            }
            writeJson(out, 200, new JSONObject().put("ok", true)
                    .put("file", saved.getName()).put("bytes", frame.length)
                    .put("size", opened));
            return;
        }
        if ("GET".equals(method) && "/camera/stream".equals(path)) {
            streamCamera(request, out);
            return;
        }

        if ("GET".equals(method) && "/audio/status".equals(path)) {
            writeJson(out, 200, audio.status());
            return;
        }
        if ("POST".equals(method) && "/audio/record/start".equals(path)) {
            writeJson(out, 200, audio.startRecording());
            return;
        }
        if ("POST".equals(method) && "/audio/record/stop".equals(path)) {
            writeJson(out, 200, audio.stopRecording());
            return;
        }
        if ("GET".equals(method) && "/audio/stream".equals(path)) {
            streamAudio(out);
            return;
        }

        if ("GET".equals(method) && "/media/list".equals(path)) {
            writeJson(out, 200, mediaList());
            return;
        }
        if ("POST".equals(method) && "/media/upload".equals(path)) {
            writeJson(out, 200, mediaUpload(request));
            return;
        }
        if ("POST".equals(method) && "/media/play".equals(path)) {
            String name = request.json().optString("file", "");
            writeJson(out, 200, audio.play(mediaFile(name)));
            return;
        }
        if ("POST".equals(method) && "/media/control".equals(path)) {
            writeJson(out, 200, audio.control(request.json().optString("action", "stop")));
            return;
        }
        if ("GET".equals(method) && "/media/get".equals(path)) {
            File file = mediaFile(request.query.getOrDefault("file", ""));
            writeFile(out, file);
            return;
        }
        if ("POST".equals(method) && "/media/delete".equals(path)) {
            File file = mediaFile(request.json().optString("file", ""));
            if (!file.delete()) {
                throw new DeviceApi.ApiError(500, "could not delete the file");
            }
            writeJson(out, 200, new JSONObject().put("ok", true));
            return;
        }

        if ("GET".equals(method) && "/recordings".equals(path)) {
            writeJson(out, 200, listFolder("recordings"));
            return;
        }
        if ("GET".equals(method) && "/recording".equals(path)) {
            writeFile(out, folderFile("recordings", request.query.getOrDefault("file", "")));
            return;
        }
        if ("POST".equals(method) && "/recording/play".equals(path)) {
            writeJson(out, 200, audio.play(folderFile("recordings",
                    request.json().optString("file", ""))));
            return;
        }
        if ("POST".equals(method) && "/recording/delete".equals(path)) {
            File target = folderFile("recordings", request.json().optString("file", ""));
            if (!target.delete()) {
                throw new DeviceApi.ApiError(500, "could not delete the recording");
            }
            writeJson(out, 200, new JSONObject().put("ok", true));
            return;
        }
        if ("GET".equals(method) && "/photos".equals(path)) {
            writeJson(out, 200, listFolder("photos"));
            return;
        }
        if ("POST".equals(method) && "/photo/delete".equals(path)) {
            File target = folderFile("photos", request.json().optString("file", ""));
            if (!target.delete()) {
                throw new DeviceApi.ApiError(500, "could not delete the photo");
            }
            writeJson(out, 200, new JSONObject().put("ok", true));
            return;
        }
        if ("GET".equals(method) && "/photo".equals(path)) {
            File directory = new File(service.getFilesDir(), "photos");
            File file = new File(directory, request.query.getOrDefault("file", ""));
            if (!file.getParentFile().equals(directory) || !file.isFile()) {
                throw new DeviceApi.ApiError(404, "no such photo");
            }
            writeFile(out, file);
            return;
        }
        writeJson(out, 404, error("no such endpoint"));
    }

    private void streamCamera(Request request, OutputStream out) throws Exception {
        String id = request.query.getOrDefault("camera", "0");
        String size = request.query.getOrDefault("size", "");
        int fps = 4;
        try {
            fps = Math.max(1, Math.min(Integer.parseInt(
                    request.query.getOrDefault("fps", "4")), 15));
        } catch (NumberFormatException ignored) {
        }
        camera.start(id, size);
        camera.awaitFirstFrame(2500);
        LinkedBlockingQueue<byte[]> frames = camera.subscribe();
        String head = "HTTP/1.1 200 OK\r\n"
                + "Content-Type: multipart/x-mixed-replace; boundary=" + BOUNDARY + "\r\n"
                + "Cache-Control: no-store\r\nConnection: close\r\n\r\n";
        try {
            out.write(head.getBytes(StandardCharsets.ISO_8859_1));
            out.flush();
            long deadline = System.currentTimeMillis() + 5 * 60 * 1000L;
            long interval = 1000L / fps;
            while (running && System.currentTimeMillis() < deadline) {
                byte[] frame = frames.poll(interval * 3, TimeUnit.MILLISECONDS);
                if (frame == null) {
                    continue;
                }
                String part = "--" + BOUNDARY + "\r\nContent-Type: image/jpeg\r\n"
                        + "Content-Length: " + frame.length + "\r\n\r\n";
                out.write(part.getBytes(StandardCharsets.ISO_8859_1));
                out.write(frame);
                out.write("\r\n".getBytes(StandardCharsets.ISO_8859_1));
                out.flush();
                Thread.sleep(interval);
            }
        } finally {
            camera.unsubscribe(frames);
        }
    }

    private void streamAudio(OutputStream out) throws Exception {
        LinkedBlockingQueue<byte[]> chunks = audio.subscribe();
        String head = "HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\n"
                + "Cache-Control: no-store\r\nConnection: close\r\n\r\n";
        byte[] wav = wavHeader();
        try {
            out.write(head.getBytes(StandardCharsets.ISO_8859_1));
            out.write(wav);
            out.flush();
            long deadline = System.currentTimeMillis() + 5 * 60 * 1000L;
            while (running && System.currentTimeMillis() < deadline) {
                byte[] chunk = chunks.poll(3, TimeUnit.SECONDS);
                if (chunk == null) {
                    continue;
                }
                out.write(chunk);
                out.flush();
            }
        } finally {
            audio.unsubscribe(chunks);
        }
    }

    private static byte[] wavHeader() {
        int rate = AudioController.SAMPLE_RATE;
        long data = 0xFFFFFFFFL;
        ByteArrayOutputStream buffer = new ByteArrayOutputStream(44);
        buffer.write('R');
        buffer.write('I');
        buffer.write('F');
        buffer.write('F');
        writeInt(buffer, 36 + data);
        for (char value : "WAVEfmt ".toCharArray()) {
            buffer.write(value);
        }
        writeInt(buffer, 16);
        writeShort(buffer, 1);
        writeShort(buffer, 1);
        writeInt(buffer, rate);
        writeInt(buffer, rate * 2);
        writeShort(buffer, 2);
        writeShort(buffer, 16);
        for (char value : "data".toCharArray()) {
            buffer.write(value);
        }
        writeInt(buffer, data);
        return buffer.toByteArray();
    }

    private static void writeInt(ByteArrayOutputStream out, long value) {
        out.write((int) (value & 0xff));
        out.write((int) ((value >> 8) & 0xff));
        out.write((int) ((value >> 16) & 0xff));
        out.write((int) ((value >> 24) & 0xff));
    }

    private static void writeShort(ByteArrayOutputStream out, int value) {
        out.write(value & 0xff);
        out.write((value >> 8) & 0xff);
    }

    private File mediaFile(String name) {
        if (name == null || name.isEmpty() || name.contains("/") || name.contains("..")) {
            throw new DeviceApi.ApiError(400, "invalid file name");
        }
        File directory = new File(service.getFilesDir(), "media");
        File file = new File(directory, name);
        if (!file.isFile()) {
            throw new DeviceApi.ApiError(404, "no such media file");
        }
        return file;
    }

    private JSONObject mediaUpload(Request request) throws Exception {
        String name = request.headers.getOrDefault("x-filename", "");
        if (name.isEmpty() || name.contains("/") || name.contains("..")) {
            throw new DeviceApi.ApiError(400, "X-Filename header is required");
        }
        File directory = new File(service.getFilesDir(), "media");
        if (!directory.exists() && !directory.mkdirs()) {
            throw new DeviceApi.ApiError(500, "cannot create the media directory");
        }
        File target = new File(directory, name);
        try (FileOutputStream out = new FileOutputStream(target)) {
            out.write(request.bodyBytes);
        }
        return new JSONObject().put("ok", true).put("file", name)
                .put("bytes", target.length());
    }

    private JSONObject mediaList() throws Exception {
        File directory = new File(service.getFilesDir(), "media");
        JSONArray files = new JSONArray();
        File[] listing = directory.listFiles();
        if (listing != null) {
            for (File file : listing) {
                if (file.isFile()) {
                    files.put(new JSONObject().put("file", file.getName())
                            .put("bytes", file.length())
                            .put("modified", file.lastModified()));
                }
            }
        }
        return new JSONObject().put("files", files);
    }

    private JSONObject listFolder(String name) throws Exception {
        File directory = new File(service.getFilesDir(), name);
        JSONArray files = new JSONArray();
        File[] listing = directory.listFiles();
        if (listing != null) {
            for (File file : listing) {
                if (file.isFile()) {
                    files.put(new JSONObject().put("file", file.getName())
                            .put("bytes", file.length())
                            .put("modified", file.lastModified()));
                }
            }
        }
        return new JSONObject().put("files", files);
    }

    private File folderFile(String folder, String name) {
        if (name == null || name.isEmpty() || name.contains("/") || name.contains("..")) {
            throw new DeviceApi.ApiError(400, "invalid file name");
        }
        File directory = new File(service.getFilesDir(), folder);
        File file = new File(directory, name);
        if (!file.isFile()) {
            throw new DeviceApi.ApiError(404, "no such file");
        }
        return file;
    }

    private static void writeFile(OutputStream out, File file) throws IOException {
        String head = "HTTP/1.1 200 OK\r\n"
                + "Content-Type: " + contentType(file.getName()) + "\r\n"
                + "Cache-Control: no-store\r\nConnection: close\r\n"
                + "Content-Length: " + file.length() + "\r\n\r\n";
        out.write(head.getBytes(StandardCharsets.ISO_8859_1));
        try (InputStream input = new BufferedInputStream(new java.io.FileInputStream(file))) {
            byte[] buffer = new byte[16 * 1024];
            int read;
            while ((read = input.read(buffer)) > 0) {
                out.write(buffer, 0, read);
            }
        }
        out.flush();
    }

    private static String contentType(String name) {
        String lower = name.toLowerCase(Locale.US);
        if (lower.endsWith(".webm")) {
            return "video/webm";
        }
        if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) {
            return "image/jpeg";
        }
        if (lower.endsWith(".wav")) {
            return "audio/wav";
        }
        if (lower.endsWith(".mp3")) {
            return "audio/mpeg";
        }
        if (lower.endsWith(".mp4")) {
            return "video/mp4";
        }
        return "application/octet-stream";
    }

    private static JSONObject error(String message) {
        try {
            return new JSONObject().put("error", message == null ? "failed" : message);
        } catch (Exception impossible) {
            return new JSONObject();
        }
    }

    private static Request read(InputStream in) throws IOException {
        ByteArrayOutputStream head = new ByteArrayOutputStream();
        int state = 0;
        while (state < 4) {
            int value = in.read();
            if (value < 0) {
                return null;
            }
            head.write(value);
            state = (value == '\r' && (state == 0 || state == 2)) ? state + 1
                    : (value == '\n' && (state == 1 || state == 3)) ? state + 1 : 0;
            if (head.size() > 16 * 1024) {
                return null;
            }
        }
        String[] lines = head.toString(StandardCharsets.ISO_8859_1.name()).split("\r\n");
        String[] requestLine = lines[0].split(" ");
        if (requestLine.length < 2) {
            return null;
        }
        Request request = new Request();
        request.method = requestLine[0].toUpperCase(Locale.US);
        String target = requestLine[1];
        int question = target.indexOf('?');
        request.path = question < 0 ? target : target.substring(0, question);
        if (question >= 0) {
            for (String pair : target.substring(question + 1).split("&")) {
                int equals = pair.indexOf('=');
                if (equals > 0) {
                    request.query.put(decode(pair.substring(0, equals)),
                            decode(pair.substring(equals + 1)));
                }
            }
        }
        for (int index = 1; index < lines.length; index++) {
            int colon = lines[index].indexOf(':');
            if (colon > 0) {
                request.headers.put(lines[index].substring(0, colon).trim().toLowerCase(Locale.US),
                        lines[index].substring(colon + 1).trim());
            }
        }
        int length = 0;
        try {
            length = Integer.parseInt(request.headers.getOrDefault("content-length", "0"));
        } catch (NumberFormatException ignored) {
        }
        if (length < 0 || length > MAX_BODY) {
            return null;
        }
        byte[] body = new byte[length];
        int offset = 0;
        while (offset < length) {
            int read = in.read(body, offset, length - offset);
            if (read < 0) {
                return null;
            }
            offset += read;
        }
        request.bodyBytes = body;
        request.body = new String(body, StandardCharsets.UTF_8);
        return request;
    }

    private static String decode(String value) {
        try {
            return java.net.URLDecoder.decode(value, "UTF-8");
        } catch (Exception error) {
            return value;
        }
    }

    private static void writeJson(OutputStream out, int status, JSONObject payload)
            throws IOException {
        byte[] body = payload.toString().getBytes(StandardCharsets.UTF_8);
        String head = "HTTP/1.1 " + status + " " + reason(status) + "\r\n"
                + "Content-Type: application/json; charset=utf-8\r\n"
                + "Cache-Control: no-store\r\n"
                + "Connection: close\r\n"
                + "Content-Length: " + body.length + "\r\n\r\n";
        out.write(head.getBytes(StandardCharsets.ISO_8859_1));
        out.write(body);
        out.flush();
    }

    private static String reason(int status) {
        switch (status) {
            case 200: return "OK";
            case 400: return "Bad Request";
            case 401: return "Unauthorized";
            case 403: return "Forbidden";
            case 404: return "Not Found";
            case 405: return "Method Not Allowed";
            case 409: return "Conflict";
            case 429: return "Too Many Requests";
            case 500: return "Internal Server Error";
            case 503: return "Service Unavailable";
            default: return "Response";
        }
    }

    private static final class Request {
        String method;
        String path;
        String body = "";
        byte[] bodyBytes = new byte[0];
        Map<String, String> headers = new HashMap<>();
        Map<String, String> query = new HashMap<>();

        JSONObject json() {
            if (body == null || body.trim().isEmpty()) {
                return new JSONObject();
            }
            try {
                return new JSONObject(body);
            } catch (Exception error) {
                return new JSONObject();
            }
        }
    }
}
