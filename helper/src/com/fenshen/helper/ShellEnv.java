package com.fenshen.helper;

import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.os.IBinder;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;

/** Everything needed to look like the shell user to system services from inside app_process. */
final class ShellEnv {

    static final String SHELL_PACKAGE = "com.android.shell";
    static final int SHELL_UID = 2000; // android.os.Process.SHELL_UID

    private ShellEnv() {
    }

    /** Same trick as scrcpy's Workarounds: a bare ActivityThread is enough to get a system Context. */
    static Context createShellContext() throws Exception {
        Class<?> activityThreadClass = Class.forName("android.app.ActivityThread");
        Constructor<?> atCtor = activityThreadClass.getDeclaredConstructor();
        atCtor.setAccessible(true);
        Object activityThread = atCtor.newInstance();

        Field current = activityThreadClass.getDeclaredField("sCurrentActivityThread");
        current.setAccessible(true);
        current.set(null, activityThread);

        Field systemThread = activityThreadClass.getDeclaredField("mSystemThread");
        systemThread.setAccessible(true);
        systemThread.setBoolean(activityThread, true);

        Method getSystemContext = activityThreadClass.getDeclaredMethod("getSystemContext");
        return new ShellContext((Context) getSystemContext.invoke(activityThread));
    }

    static void setDisplayImePolicy(int displayId, int policy) throws Exception {
        Class<?> serviceManager = Class.forName("android.os.ServiceManager");
        IBinder binder = (IBinder) serviceManager.getDeclaredMethod("getService", String.class).invoke(null, "window");
        Class<?> stub = Class.forName("android.view.IWindowManager$Stub");
        Object wm = stub.getMethod("asInterface", IBinder.class).invoke(null, binder);
        wm.getClass().getMethod("setDisplayImePolicy", int.class, int.class).invoke(wm, displayId, policy);
    }

    /** Runs a command without a shell (no word splitting or injection) and returns stdout+stderr. */
    static String exec(String... command) throws IOException, InterruptedException {
        Process process = new ProcessBuilder(command).redirectErrorStream(true).start();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (InputStream in = process.getInputStream()) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) != -1) {
                out.write(buf, 0, n);
            }
        }
        process.waitFor();
        return out.toString(StandardCharsets.UTF_8.name()).trim();
    }

    /** Presents the system context as the shell package, so DisplayManagerService accepts the caller. */
    private static final class ShellContext extends ContextWrapper {
        ShellContext(Context base) {
            super(base);
        }

        @Override
        public String getPackageName() {
            return SHELL_PACKAGE;
        }

        @Override
        public String getOpPackageName() {
            return SHELL_PACKAGE;
        }

        @Override
        public AttributionSource getAttributionSource() {
            return new AttributionSource.Builder(SHELL_UID).setPackageName(SHELL_PACKAGE).build();
        }

        @Override
        public Context getApplicationContext() {
            return this;
        }
    }
}
