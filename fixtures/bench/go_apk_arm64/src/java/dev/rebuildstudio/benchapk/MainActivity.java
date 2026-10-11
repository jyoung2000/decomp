package dev.rebuildstudio.benchapk;

import android.app.Activity;
import android.os.Bundle;
import android.widget.TextView;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;

/** Runs the bundled native helper (lib/arm64-v8a/libbenchgo.so, our Go benchgo built for android/arm64) and shows its output. */
public class MainActivity extends Activity {
    static String runHelper(File libDir, String... args) throws Exception {
        String[] cmd = new String[args.length + 1];
        cmd[0] = new File(libDir, "libbenchgo.so").getAbsolutePath();
        System.arraycopy(args, 0, cmd, 1, args.length);
        Process p = new ProcessBuilder(cmd).redirectErrorStream(true).start();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (InputStream in = p.getInputStream()) {
            byte[] buf = new byte[4096];
            for (int n; (n = in.read(buf)) > 0; ) {
                out.write(buf, 0, n);
            }
        }
        p.waitFor();
        return out.toString("UTF-8");
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        TextView view = new TextView(this);
        try {
            view.setText(runHelper(new File(getApplicationInfo().nativeLibraryDir), "apple=3@1.25", "pear=2@0.5"));
        } catch (Exception e) {
            view.setText("benchapk: helper failed: " + e);
        }
        setContentView(view);
    }
}
