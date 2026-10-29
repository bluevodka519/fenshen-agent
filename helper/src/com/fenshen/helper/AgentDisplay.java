package com.fenshen.helper;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.hardware.display.VirtualDisplayConfig;
import android.media.Image;
import android.media.ImageReader;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.SystemClock;

import java.io.ByteArrayOutputStream;
import java.lang.reflect.Constructor;
import java.nio.ByteBuffer;

/**
 * The agent's virtual display. It is backed by an ImageReader we own, so screenshots come straight
 * from our own buffer: no screencap, no MediaProjection, and nothing from the user's display.
 */
final class AgentDisplay {

    // android.hardware.display.DisplayManager VIRTUAL_DISPLAY_FLAG_* (several are @hide)
    private static final int FLAG_PUBLIC = 1;
    private static final int FLAG_PRESENTATION = 1 << 1;
    private static final int FLAG_OWN_CONTENT_ONLY = 1 << 3;
    private static final int FLAG_SUPPORTS_TOUCH = 1 << 6;
    private static final int FLAG_ROTATES_WITH_CONTENT = 1 << 7;
    private static final int FLAG_DESTROY_CONTENT_ON_REMOVAL = 1 << 8;
    private static final int FLAG_SHOULD_SHOW_SYSTEM_DECORATIONS = 1 << 9;
    private static final int FLAG_TRUSTED = 1 << 10;
    private static final int FLAG_OWN_DISPLAY_GROUP = 1 << 11;
    private static final int FLAG_ALWAYS_UNLOCKED = 1 << 12;
    private static final int FLAG_TOUCH_FEEDBACK_DISABLED = 1 << 13;
    private static final int FLAG_OWN_FOCUS = 1 << 14;
    private static final int FLAG_DEVICE_DISPLAY_GROUP = 1 << 15;
    private static final int FLAG_STEAL_TOP_FOCUS_DISABLED = 1 << 16;

    // android.view.WindowManager DISPLAY_IME_POLICY_*
    static final int IME_POLICY_LOCAL = 0;
    static final int IME_POLICY_FALLBACK_DISPLAY = 1;
    static final int IME_POLICY_HIDE = 2;

    final int width;
    final int height;
    final int dpi;
    final int flags;
    final int imePolicy;
    final int displayId;

    private final VirtualDisplay virtualDisplay;
    private final ImageReader reader;
    private Image latest;
    private long frameCount;
    private long lastFrameUptime;

    private AgentDisplay(int width, int height, int dpi, int flags, int imePolicy,
            VirtualDisplay virtualDisplay, ImageReader reader) {
        this.width = width;
        this.height = height;
        this.dpi = dpi;
        this.flags = flags;
        this.imePolicy = imePolicy;
        this.virtualDisplay = virtualDisplay;
        this.reader = reader;
        this.displayId = virtualDisplay.getDisplay().getDisplayId();
    }

    static AgentDisplay create(Context context, int width, int height, int dpi, int imePolicy,
            boolean stealTopFocusDisabled, float fps, boolean ownDisplayGroup) throws Exception {
        int flags = FLAG_PUBLIC
                | FLAG_PRESENTATION
                | FLAG_OWN_CONTENT_ONLY
                | FLAG_SUPPORTS_TOUCH
                | FLAG_ROTATES_WITH_CONTENT
                | FLAG_DESTROY_CONTENT_ON_REMOVAL
                | FLAG_SHOULD_SHOW_SYSTEM_DECORATIONS
                | FLAG_TRUSTED
                | FLAG_TOUCH_FEEDBACK_DISABLED
                | FLAG_OWN_FOCUS;
        if (ownDisplayGroup) {
            // scrcpy's choice: an independent power group, so the display can run while the phone's
            // screen is off. On a real phone (Seeker, Android 16) that group went to state OFF while
            // the phone was awake and in use, and apps on it were never drawn (E10); a wake key aimed
            // at the display did not bring it back.
            flags |= FLAG_OWN_DISPLAY_GROUP | FLAG_ALWAYS_UNLOCKED;
        } else {
            // Default: join the phone's display group, so the agent display is on whenever the
            // user's screen is on (which is exactly when the user is using the phone).
            flags |= FLAG_DEVICE_DISPLAY_GROUP;
        }
        if (stealTopFocusDisabled) {
            flags |= FLAG_STEAL_TOP_FOCUS_DISABLED;
        }

        // 3 buffers: we hold the latest frame, the producer keeps the other two.
        ImageReader reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 3);

        Constructor<DisplayManager> ctor = DisplayManager.class.getDeclaredConstructor(Context.class);
        ctor.setAccessible(true);
        DisplayManager dm = ctor.newInstance(context);
        VirtualDisplay vd;
        if (fps > 0) {
            // Android 14+: ask the compositor for fewer frames on this display. The agent reads the screen
            // a few times per step; 60 fps of an app nobody looks at only competes with the user's display.
            VirtualDisplayConfig config = new VirtualDisplayConfig.Builder("fenshen", width, height, dpi)
                    .setSurface(reader.getSurface())
                    .setFlags(flags)
                    .setRequestedRefreshRate(fps)
                    .build();
            vd = dm.createVirtualDisplay(config);
        } else {
            vd = dm.createVirtualDisplay("fenshen", width, height, dpi, reader.getSurface(), flags);
        }

        AgentDisplay display = new AgentDisplay(width, height, dpi, flags, imePolicy, vd, reader);
        ShellEnv.setDisplayImePolicy(display.displayId, imePolicy);
        display.startFrameDrain();
        return display;
    }

    private void startFrameDrain() {
        HandlerThread thread = new HandlerThread("fenshen-frames");
        thread.start();
        reader.setOnImageAvailableListener(r -> {
            Image image;
            try {
                image = r.acquireLatestImage();
            } catch (IllegalStateException e) {
                return; // all buffers busy; the next frame will catch up
            }
            if (image == null) {
                return;
            }
            synchronized (this) {
                if (latest != null) {
                    latest.close();
                }
                latest = image;
                frameCount++;
                lastFrameUptime = SystemClock.uptimeMillis();
            }
        }, new Handler(thread.getLooper()));
    }

    synchronized long frameCount() {
        return frameCount;
    }

    synchronized long msSinceLastFrame() {
        return lastFrameUptime == 0 ? -1 : SystemClock.uptimeMillis() - lastFrameUptime;
    }

    /** JPEG of the latest frame, optionally downscaled to maxWidth; null before the first frame. */
    synchronized byte[] jpeg(int quality, int maxWidth) {
        if (latest == null) {
            return null;
        }
        Image.Plane plane = latest.getPlanes()[0];
        ByteBuffer buffer = plane.getBuffer();
        buffer.rewind();
        int paddedWidth = plane.getRowStride() / plane.getPixelStride();
        Bitmap padded = Bitmap.createBitmap(paddedWidth, height, Bitmap.Config.ARGB_8888);
        padded.copyPixelsFromBuffer(buffer);
        Bitmap bitmap = paddedWidth == width ? padded : Bitmap.createBitmap(padded, 0, 0, width, height);
        if (maxWidth > 0 && width > maxWidth) {
            bitmap = Bitmap.createScaledBitmap(bitmap, maxWidth, height * maxWidth / width, true);
        }
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        bitmap.compress(Bitmap.CompressFormat.JPEG, quality, out);
        return out.toByteArray();
    }

    void release() {
        virtualDisplay.release();
    }
}
