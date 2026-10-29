package com.fenshen.helper;

import android.content.Context;
import android.os.Looper;

/**
 * fenshen-helper: runs as the shell user via app_process and owns the agent's virtual display.
 *
 *   CLASSPATH=/data/local/tmp/fenshen-helper.jar app_process / com.fenshen.helper.Main <mode> [options]
 *
 * Modes:
 *   vd      create the agent display only (used by the E2 focus experiment)
 *   serve   create the agent display + UiAutomation + JSON RPC on localabstract:<socket>
 *
 * Options:
 *   --width W --height H --dpi D        agent display size (default 720x1280/320)
 *   --ime-policy hide|local|fallback    where an IME for the agent display may appear (default hide)
 *   --no-steal-top-focus                omit STEAL_TOP_FOCUS_DISABLED (control group only)
 *   --fps F                             cap the agent display's frame rate (0 = no cap; E8: rendering the
 *                                       agent display at 60 fps costs the user's app frames)
 *   --socket NAME                       RPC socket name (default "fenshen")
 *
 * Prints "DISPLAY_ID=<id>" once the display exists, then keeps running until killed. The display
 * uses DESTROY_CONTENT_ON_REMOVAL, so when this process dies the agent's apps are destroyed
 * instead of being moved onto the user's screen.
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 0 || !("vd".equals(args[0]) || "serve".equals(args[0]))) {
            System.err.println("usage: vd|serve [--width W] [--height H] [--dpi D] "
                    + "[--ime-policy hide|local|fallback] [--no-steal-top-focus] [--fps F] [--socket NAME]");
            System.exit(2);
        }
        boolean serve = "serve".equals(args[0]);

        int width = 720;
        int height = 1280;
        int dpi = 320;
        int imePolicy = AgentDisplay.IME_POLICY_HIDE;
        boolean stealTopFocusDisabled = true;
        float fps = 0;
        String socket = "fenshen";
        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "--width": width = Integer.parseInt(args[++i]); break;
                case "--height": height = Integer.parseInt(args[++i]); break;
                case "--dpi": dpi = Integer.parseInt(args[++i]); break;
                case "--ime-policy": imePolicy = parseImePolicy(args[++i]); break;
                case "--no-steal-top-focus": stealTopFocusDisabled = false; break;
                case "--fps": fps = Float.parseFloat(args[++i]); break;
                case "--socket": socket = args[++i]; break;
                default: throw new IllegalArgumentException("unknown option: " + args[i]);
            }
        }

        Looper.prepareMainLooper();
        Context context = ShellEnv.createShellContext();
        AgentDisplay display = AgentDisplay.create(context, width, height, dpi, imePolicy, stealTopFocusDisabled, fps);
        Runtime.getRuntime().addShutdownHook(new Thread(display::release));

        System.out.println("DISPLAY_ID=" + display.displayId);
        System.out.println("FLAGS=0x" + Integer.toHexString(display.flags) + " IME_POLICY=" + imePolicy
                + " STEAL_TOP_FOCUS_DISABLED=" + stealTopFocusDisabled + " FPS_CAP=" + fps);

        if (serve) {
            Automation automation = new Automation(display);
            new RpcServer(socket, new Commands(context, display, automation)).start();
            System.out.println("RPC=localabstract:" + socket);
        }
        System.out.flush();

        Looper.loop();
    }

    private static int parseImePolicy(String value) {
        switch (value) {
            case "local": return AgentDisplay.IME_POLICY_LOCAL;
            case "fallback": return AgentDisplay.IME_POLICY_FALLBACK_DISPLAY;
            case "hide": return AgentDisplay.IME_POLICY_HIDE;
            default: throw new IllegalArgumentException("unknown ime policy: " + value);
        }
    }
}
