package com.fenshen.helper;

import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** RPC command dispatch. One command at a time: the agent is a single actor. */
final class Commands implements RpcServer.Handler {

    private static final Pattern DISPLAY_HEADER = Pattern.compile("^Display #(\\d+) ");
    private static final Pattern ACTIVITY_PKG = Pattern.compile("ActivityRecord\\{\\S+ u\\d+ ([\\w.]+)/");

    private final Context context;
    private final AgentDisplay display;
    private final Automation automation;

    Commands(Context context, AgentDisplay display, Automation automation) {
        this.context = context;
        this.display = display;
        this.automation = automation;
    }

    @Override
    public synchronized JSONObject handle(String cmd, JSONObject a) throws Exception {
        switch (cmd) {
            case "ping":
                return new JSONObject().put("pong", true);
            case "info":
                return new JSONObject()
                        .put("display_id", display.displayId)
                        .put("width", display.width)
                        .put("height", display.height)
                        .put("dpi", display.dpi)
                        .put("flags", display.flags)
                        .put("ime_policy", display.imePolicy)
                        .put("frames", display.frameCount())
                        .put("ms_since_last_frame", display.msSinceLastFrame());
            case "snapshot":
                return automation.snapshot(a.optInt("max_nodes", 1500));
            case "node_action":
                return new JSONObject().put("performed",
                        automation.nodeAction(a.getInt("node"), a.getString("action"), a.optString("text", null)));
            case "tap":
                return new JSONObject().put("injected", automation.tap(a.getInt("x"), a.getInt("y")));
            case "swipe":
                return new JSONObject().put("injected", automation.swipe(a.getInt("x1"), a.getInt("y1"),
                        a.getInt("x2"), a.getInt("y2"), a.optInt("ms", 300)));
            case "key":
                return new JSONObject().put("injected", automation.key(a.getInt("code")));
            case "wait_idle": {
                long start = System.currentTimeMillis();
                boolean idle = automation.waitIdle(a.optLong("idle_ms", 500), a.optLong("timeout_ms", 5000));
                return new JSONObject().put("idle", idle).put("waited_ms", System.currentTimeMillis() - start);
            }
            case "screenshot": {
                byte[] jpeg = display.jpeg(a.optInt("quality", 70), a.optInt("max_width", 0));
                JSONObject r = new JSONObject().put("frames", display.frameCount());
                if (jpeg != null) {
                    r.put("jpeg_b64", Base64.encodeToString(jpeg, Base64.NO_WRAP));
                }
                return r;
            }
            case "launch":
                return launch(a.getString("package"));
            case "am_start":
                return amStart(a.getJSONArray("args"));
            case "foreign_visible_packages":
                return new JSONObject().put("packages", new JSONArray(visiblePackagesOnOtherDisplays()));
            case "apps":
                return new JSONObject().put("apps", launcherApps());
            default:
                throw new IllegalArgumentException("unknown command: " + cmd);
        }
    }

    /** Launcher apps with their user-visible labels, so the brain can resolve "时钟" / "美团" to a package. */
    private JSONArray launcherApps() throws Exception {
        PackageManager pm = context.getPackageManager();
        Intent intent = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        JSONArray out = new JSONArray();
        Set<String> seen = new LinkedHashSet<>();
        for (ResolveInfo ri : pm.queryIntentActivities(intent, 0)) {
            String pkg = ri.activityInfo.packageName;
            if (seen.add(pkg)) {
                out.put(new JSONObject().put("package", pkg).put("label", String.valueOf(ri.loadLabel(pm))));
            }
        }
        return out;
    }

    private JSONObject launch(String pkg) throws Exception {
        String resolved = ShellEnv.exec("cmd", "package", "resolve-activity", "--brief",
                "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", pkg);
        String component = lastComponentLine(resolved);
        if (component == null) {
            throw new IllegalArgumentException("no launcher activity for " + pkg + ": " + resolved);
        }
        List<String> args = new ArrayList<>();
        args.add("-n");
        args.add(component);
        return startOnAgentDisplay(args, component);
    }

    private JSONObject amStart(JSONArray json) throws Exception {
        List<String> args = new ArrayList<>();
        for (int i = 0; i < json.length(); i++) {
            String arg = json.getString(i);
            if ("--display".equals(arg) || "--user".equals(arg)) {
                throw new SecurityException(arg + " is chosen by the helper, not the caller");
            }
            args.add(arg);
        }
        List<String> resolveCmd = new ArrayList<>();
        resolveCmd.add("cmd");
        resolveCmd.add("package");
        resolveCmd.add("resolve-activity");
        resolveCmd.add("--brief");
        resolveCmd.addAll(args);
        String component = lastComponentLine(ShellEnv.exec(resolveCmd.toArray(new String[0])));
        if (component == null) {
            throw new IllegalArgumentException("intent does not resolve to an activity: " + args);
        }
        return startOnAgentDisplay(args, component);
    }

    /**
     * Launching an app whose task is visible on the user's display would move that task onto the
     * agent display and yank it away from the user (README §6). Refuse instead.
     */
    private JSONObject startOnAgentDisplay(List<String> args, String component) throws Exception {
        String pkg = component.substring(0, component.indexOf('/'));
        Set<String> foreign = visiblePackagesOnOtherDisplays();
        if (foreign.contains(pkg)) {
            throw new IllegalStateException("CONFLICT: " + pkg + " is open on the user's screen; not launching it on the agent display");
        }
        List<String> cmd = new ArrayList<>();
        cmd.add("am");
        cmd.add("start");
        cmd.add("-W");
        cmd.add("--display");
        cmd.add(String.valueOf(display.displayId));
        cmd.addAll(args);
        String output = ShellEnv.exec(cmd.toArray(new String[0]));
        boolean ok = output.contains("Status: ok") || output.contains("Warning: Activity not started");
        return new JSONObject().put("component", component).put("ok", ok).put("output", output);
    }

    private static String lastComponentLine(String output) {
        String found = null;
        for (String line : output.split("\n")) {
            String s = line.trim();
            if (s.matches("[\\w.]+/[\\w.$]+")) {
                found = s;
            }
        }
        return found;
    }

    /** Packages with a visible task on any display other than the agent display. */
    Set<String> visiblePackagesOnOtherDisplays() throws Exception {
        Set<String> packages = new LinkedHashSet<>();
        int current = -1;
        boolean inVisibleTask = false;
        for (String line : ShellEnv.exec("dumpsys", "activity", "activities").split("\n")) {
            // The per-display sections end where the global summary starts.
            if (line.startsWith("  ResumedActivity:") || line.startsWith("  topDisplayFocusedRootTask")) {
                break;
            }
            Matcher header = DISPLAY_HEADER.matcher(line);
            if (header.find()) {
                current = Integer.parseInt(header.group(1));
                inVisibleTask = false;
                continue;
            }
            if (line.startsWith("  * Task{")) {
                inVisibleTask = line.contains(" visible=true");
                continue;
            }
            if (current < 0 || current == display.displayId || !inVisibleTask) {
                continue;
            }
            Matcher m = ACTIVITY_PKG.matcher(line);
            if (m.find()) {
                packages.add(m.group(1));
            }
        }
        return packages;
    }
}
