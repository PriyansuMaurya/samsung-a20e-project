package ie.maurya.galaxy.device;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** The only screen: arm, disarm, pair, and see what is granted. */
public final class MainActivity extends Activity {
    private static final int PERMISSIONS = 11;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView status;
    private TextView detail;

    private final Runnable refresh = new Runnable() {
        @Override
        public void run() {
            render();
            handler.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(buildUi());
        if (savedInstanceState == null && getIntent() != null) {
            if (getIntent().getBooleanExtra("pair", false)) {
                Pairing.openWindow(60);
            }
            if (getIntent().getBooleanExtra("arm", false)) {
                startForegroundService(new Intent(this, ControlService.class)
                        .setAction(ControlService.ACTION_START));
            }
        }
        requestPermissionsIfNeeded();
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(refresh);
    }

    @Override
    protected void onPause() {
        handler.removeCallbacks(refresh);
        super.onPause();
    }

    private LinearLayout buildUi() {
        int padding = Math.round(20 * getResources().getDisplayMetrics().density);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(padding, padding, padding, padding);
        root.setGravity(Gravity.START);

        TextView title = new TextView(this);
        title.setText("Galaxy device control");
        title.setTextSize(22);
        root.addView(title);

        status = new TextView(this);
        status.setTextSize(16);
        status.setPadding(0, padding / 2, 0, 0);
        root.addView(status);

        detail = new TextView(this);
        detail.setTextSize(13);
        detail.setPadding(0, padding / 2, 0, padding / 2);
        root.addView(detail);

        Button arm = new Button(this);
        arm.setText("Arm control service");
        arm.setOnClickListener(new android.view.View.OnClickListener() {
            @Override
            public void onClick(android.view.View view) {
                Intent intent = new Intent(MainActivity.this, ControlService.class);
                intent.setAction(ControlService.ACTION_START);
                startForegroundService(intent);
                render();
            }
        });
        root.addView(arm);

        Button disarm = new Button(this);
        disarm.setText("Disarm");
        disarm.setOnClickListener(new android.view.View.OnClickListener() {
            @Override
            public void onClick(android.view.View view) {
                stopService(new Intent(MainActivity.this, ControlService.class));
                render();
            }
        });
        root.addView(disarm);

        Button pair = new Button(this);
        pair.setText("Open pairing window (60 s)");
        pair.setOnClickListener(new android.view.View.OnClickListener() {
            @Override
            public void onClick(android.view.View view) {
                Pairing.openWindow(60);
                render();
            }
        });
        root.addView(pair);

        Button forget = new Button(this);
        forget.setText("Forget paired dashboard");
        forget.setOnClickListener(new android.view.View.OnClickListener() {
            @Override
            public void onClick(android.view.View view) {
                Pairing.forget(MainActivity.this);
                render();
            }
        });
        root.addView(forget);

        root.setLayoutParams(new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        return root;
    }

    private void requestPermissionsIfNeeded() {
        boolean camera = checkSelfPermission(Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED;
        boolean microphone = checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED;
        if (!camera || !microphone) {
            requestPermissions(new String[]{Manifest.permission.CAMERA,
                    Manifest.permission.RECORD_AUDIO}, PERMISSIONS);
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(code, permissions, results);
        render();
    }

    private void render() {
        boolean armed = ControlService.running();
        boolean paired = Pairing.paired(this);
        status.setText(armed ? "Armed, control server on 127.0.0.1:"
                        + LocalServer.PORT : "Disarmed");
        boolean camera = checkSelfPermission(Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED;
        boolean microphone = checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED;
        StringBuilder text = new StringBuilder();
        text.append(paired ? "Paired with the dashboard" : "Not paired");
        if (Pairing.windowOpen()) {
            text.append("\nPairing window open");
        }
        text.append("\nCamera permission: ").append(camera ? "granted" : "missing");
        text.append("\nMicrophone permission: ").append(microphone ? "granted" : "missing");
        text.append("\nAndroid ").append(android.os.Build.VERSION.RELEASE)
                .append(" (SDK ").append(android.os.Build.VERSION.SDK_INT).append(")");
        detail.setText(text.toString());
    }
}
