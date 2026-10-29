package com.fenshen.helper;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.graphics.Rect;
import android.os.Bundle;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.SystemClock;
import android.util.SparseArray;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.util.ArrayDeque;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Observation and actions, scoped to the agent display only.
 *
 * Every entry point here is bound to {@link AgentDisplay#displayId}: there is no parameter to
 * pick a display, snapshots only walk windows of the agent display, node actions re-check the
 * node's display, and injected events are stamped with the agent display id. The user's display
 * is never read or touched.
 */
final class Automation {

    private static final int UIAUTOMATION_FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES = 1;

    private final AgentDisplay display;
    private final UiAutomation uiAutomation;
    private final Method setDisplayIdMethod;

    private final Map<Integer, AccessibilityNodeInfo> nodes = new HashMap<>();
    private int snapshotSeq;
    private volatile long lastAgentEventUptime = SystemClock.uptimeMillis();

    Automation(AgentDisplay display) throws Exception {
        this.display = display;
        this.setDisplayIdMethod = InputEvent.class.getMethod("setDisplayId", int.class);

        HandlerThread thread = new HandlerThread("fenshen-uiautomation");
        thread.start();
        Class<?> connectionInterface = Class.forName("android.app.IUiAutomationConnection");
        Object connection = Class.forName("android.app.UiAutomationConnection").getDeclaredConstructor().newInstance();
        Constructor<UiAutomation> ctor = UiAutomation.class.getDeclaredConstructor(Looper.class, connectionInterface);
        ctor.setAccessible(true);
        uiAutomation = ctor.newInstance(thread.getLooper(), connection);

        // Keep the user's own accessibility services (e.g. TalkBack) running while we are connected.
        Method connect = UiAutomation.class.getDeclaredMethod("connect", int.class);
        connect.setAccessible(true);
        connect.invoke(uiAutomation, UIAUTOMATION_FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);

        AccessibilityServiceInfo info = uiAutomation.getServiceInfo();
        info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
        uiAutomation.setServiceInfo(info);

        // Only events from the agent display count towards "UI is still changing". Events from the
        // user's display are dropped here without being inspected or stored.
        uiAutomation.setOnAccessibilityEventListener(event -> {
            if (eventDisplayId(event) == display.displayId) {
                lastAgentEventUptime = SystemClock.uptimeMillis();
            }
        });
    }

    private static int eventDisplayId(AccessibilityEvent event) {
        try {
            return event.getDisplayId();
        } catch (Throwable t) {
            return -1; // API < 33: unknown; never counts as agent activity
        }
    }

    // ------------------------------------------------------------------ observation

    synchronized JSONObject snapshot(int maxNodes) throws JSONException {
        nodes.clear();
        snapshotSeq++;
        // The accessibility cache can lag behind fast state changes (seen: AM and PM radios both
        // "checked" right after a toggle). The agent acts on what it reads, so always read fresh.
        try {
            uiAutomation.clearCache();
        } catch (Throwable ignored) {
            // API < 34: no clearCache(); fall back to the cache
        }

        SparseArray<List<AccessibilityWindowInfo>> all = uiAutomation.getWindowsOnAllDisplays();
        List<AccessibilityWindowInfo> windows = all.get(display.displayId, Collections.emptyList());

        JSONArray windowsJson = new JSONArray();
        JSONArray nodesJson = new JSONArray();
        int nextId = 0;
        boolean truncated = false;

        for (AccessibilityWindowInfo window : windows) {
            AccessibilityNodeInfo root = window.getRoot();
            JSONObject w = new JSONObject();
            w.put("id", window.getId());
            w.put("type", windowType(window.getType()));
            w.put("layer", window.getLayer());
            putIfNotEmpty(w, "title", window.getTitle());
            w.put("active", window.isActive());
            w.put("focused", window.isFocused());
            if (root != null) {
                putIfNotEmpty(w, "pkg", root.getPackageName());
            }
            windowsJson.put(w);
            if (root == null) {
                continue;
            }

            ArrayDeque<Object[]> stack = new ArrayDeque<>();
            stack.push(new Object[] {root, -1, 0});
            while (!stack.isEmpty()) {
                if (nextId >= maxNodes) {
                    truncated = true;
                    break;
                }
                Object[] item = stack.pop();
                AccessibilityNodeInfo node = (AccessibilityNodeInfo) item[0];
                int parent = (Integer) item[1];
                int depth = (Integer) item[2];
                int id = nextId++;
                nodes.put(id, node);
                nodesJson.put(nodeJson(node, id, parent, depth, window.getId()));
                // push children in reverse so they pop in document order
                for (int i = node.getChildCount() - 1; i >= 0; i--) {
                    AccessibilityNodeInfo child = node.getChild(i);
                    if (child != null) {
                        stack.push(new Object[] {child, id, depth + 1});
                    }
                }
            }
        }

        JSONObject result = new JSONObject();
        result.put("seq", snapshotSeq);
        result.put("display_id", display.displayId);
        result.put("width", display.width);
        result.put("height", display.height);
        result.put("windows", windowsJson);
        result.put("nodes", nodesJson);
        result.put("truncated", truncated);
        return result;
    }

    private static JSONObject nodeJson(AccessibilityNodeInfo n, int id, int parent, int depth, int windowId)
            throws JSONException {
        JSONObject o = new JSONObject();
        o.put("i", id);
        o.put("p", parent);
        o.put("dp", depth);
        o.put("w", windowId);
        CharSequence cls = n.getClassName();
        if (cls != null) {
            String s = cls.toString();
            o.put("c", s.substring(s.lastIndexOf('.') + 1));
        }
        putIfNotEmpty(o, "t", n.getText());
        putIfNotEmpty(o, "d", n.getContentDescription());
        putIfNotEmpty(o, "h", n.getHintText());
        putIfNotEmpty(o, "r", n.getViewIdResourceName());
        Rect r = new Rect();
        n.getBoundsInScreen(r);
        o.put("b", new JSONArray().put(r.left).put(r.top).put(r.right).put(r.bottom));
        AccessibilityNodeInfo.CollectionItemInfo item = n.getCollectionItemInfo();
        if (item != null) {
            o.put("row", item.getRowIndex()); // adapter position: stable across scrolling
        }
        putIfTrue(o, "clk", n.isClickable());
        putIfTrue(o, "lclk", n.isLongClickable());
        putIfTrue(o, "edt", n.isEditable());
        putIfTrue(o, "scr", n.isScrollable());
        putIfTrue(o, "chkb", n.isCheckable());
        putIfTrue(o, "chk", n.isChecked());
        putIfTrue(o, "foc", n.isFocused());
        putIfTrue(o, "sel", n.isSelected());
        putIfTrue(o, "pwd", n.isPassword());
        if (!n.isEnabled()) {
            o.put("dis", true);
        }
        if (!n.isVisibleToUser()) {
            o.put("inv", true);
        }
        return o;
    }

    private static void putIfNotEmpty(JSONObject o, String key, CharSequence value) throws JSONException {
        if (value != null && value.length() > 0) {
            o.put(key, value.toString());
        }
    }

    private static void putIfTrue(JSONObject o, String key, boolean value) throws JSONException {
        if (value) {
            o.put(key, true);
        }
    }

    private static String windowType(int type) {
        switch (type) {
            case AccessibilityWindowInfo.TYPE_APPLICATION: return "app";
            case AccessibilityWindowInfo.TYPE_INPUT_METHOD: return "ime";
            case AccessibilityWindowInfo.TYPE_SYSTEM: return "system";
            case AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY: return "a11y_overlay";
            case AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER: return "divider";
            case AccessibilityWindowInfo.TYPE_MAGNIFICATION_OVERLAY: return "magnification";
            default: return "type" + type;
        }
    }

    /** Waits until the agent display has produced no accessibility events for idleMs. */
    boolean waitIdle(long idleMs, long timeoutMs) throws InterruptedException {
        long deadline = SystemClock.uptimeMillis() + timeoutMs;
        while (true) {
            long now = SystemClock.uptimeMillis();
            if (now - lastAgentEventUptime >= idleMs) {
                return true;
            }
            if (now >= deadline) {
                return false;
            }
            Thread.sleep(Math.min(50, deadline - now));
        }
    }

    // ------------------------------------------------------------------ node actions

    synchronized boolean nodeAction(int nodeId, String action, String text) {
        AccessibilityNodeInfo node = nodes.get(nodeId);
        if (node == null) {
            throw new IllegalArgumentException("unknown node " + nodeId + " (take a new snapshot)");
        }
        if (!node.refresh()) {
            throw new IllegalStateException("node " + nodeId + " is gone (take a new snapshot)");
        }
        AccessibilityWindowInfo window = node.getWindow();
        if (window == null || window.getDisplayId() != display.displayId) {
            throw new SecurityException("refusing to act on a node outside the agent display");
        }
        switch (action) {
            case "click":
                return node.performAction(AccessibilityNodeInfo.ACTION_CLICK);
            case "long_click":
                return node.performAction(AccessibilityNodeInfo.ACTION_LONG_CLICK);
            case "focus":
                return node.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
            case "scroll_forward":
                return node.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD);
            case "scroll_backward":
                return node.performAction(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD);
            case "ime_enter":
                return node.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId());
            case "set_text": {
                Bundle args = new Bundle();
                args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text == null ? "" : text);
                return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
            }
            default:
                throw new IllegalArgumentException("unknown node action: " + action);
        }
    }

    // ------------------------------------------------------------------ injected input (agent display only)

    boolean tap(int x, int y) throws Exception {
        checkInside(x, y);
        long down = SystemClock.uptimeMillis();
        boolean ok = injectMotion(down, down, MotionEvent.ACTION_DOWN, x, y);
        ok &= injectMotion(down, down + 60, MotionEvent.ACTION_UP, x, y);
        return ok;
    }

    boolean swipe(int x1, int y1, int x2, int y2, int durationMs) throws Exception {
        checkInside(x1, y1);
        checkInside(x2, y2);
        int steps = Math.max(5, durationMs / 16);
        long down = SystemClock.uptimeMillis();
        boolean ok = injectMotion(down, down, MotionEvent.ACTION_DOWN, x1, y1);
        for (int i = 1; i <= steps; i++) {
            float f = (float) i / steps;
            long t = down + (long) durationMs * i / steps;
            ok &= injectMotion(down, t, MotionEvent.ACTION_MOVE, x1 + (x2 - x1) * f, y1 + (y2 - y1) * f);
            SystemClock.sleep(Math.max(1, durationMs / steps));
        }
        ok &= injectMotion(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, x2, y2);
        return ok;
    }

    boolean key(int keyCode) throws Exception {
        long now = SystemClock.uptimeMillis();
        boolean ok = injectKey(new KeyEvent(now, now, KeyEvent.ACTION_DOWN, keyCode, 0));
        ok &= injectKey(new KeyEvent(now, now + 20, KeyEvent.ACTION_UP, keyCode, 0));
        return ok;
    }

    private void checkInside(int x, int y) {
        if (x < 0 || y < 0 || x >= display.width || y >= display.height) {
            throw new IllegalArgumentException("point (" + x + "," + y + ") is outside the agent display "
                    + display.width + "x" + display.height);
        }
    }

    private boolean injectMotion(long downTime, long eventTime, int action, float x, float y) throws Exception {
        MotionEvent event = MotionEvent.obtain(downTime, eventTime, action, x, y, 0);
        event.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        setDisplayIdMethod.invoke(event, display.displayId);
        try {
            return uiAutomation.injectInputEvent(event, true);
        } finally {
            event.recycle();
        }
    }

    private boolean injectKey(KeyEvent event) throws Exception {
        event.setSource(InputDevice.SOURCE_KEYBOARD);
        setDisplayIdMethod.invoke(event, display.displayId);
        return uiAutomation.injectInputEvent(event, true);
    }
}
