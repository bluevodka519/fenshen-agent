package com.fenshen.helper;

import android.net.Credentials;
import android.net.LocalServerSocket;
import android.net.LocalSocket;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/**
 * Newline-delimited JSON over an abstract local socket. Reach it from the PC with
 * {@code adb forward tcp:<port> localabstract:<name>}.
 *
 * Request:  {"id": 1, "cmd": "snapshot", "args": {...}}
 * Response: {"id": 1, "ok": true, "result": {...}}  or  {"id": 1, "ok": false, "error": "..."}
 *
 * Abstract sockets have no filesystem permissions, so any app on the device could connect. We only
 * accept peers running as shell (adbd forwards arrive as shell) or root.
 */
final class RpcServer {

    interface Handler {
        JSONObject handle(String cmd, JSONObject args) throws Exception;
    }

    private static final int ROOT_UID = 0;

    private final String name;
    private final Handler handler;

    RpcServer(String name, Handler handler) {
        this.name = name;
        this.handler = handler;
    }

    void start() throws IOException {
        LocalServerSocket server = new LocalServerSocket(name);
        Thread acceptor = new Thread(() -> {
            while (true) {
                try {
                    LocalSocket socket = server.accept();
                    Thread worker = new Thread(() -> serve(socket), "fenshen-rpc-client");
                    worker.setDaemon(true);
                    worker.start();
                } catch (IOException e) {
                    System.err.println("rpc accept failed: " + e);
                    return;
                }
            }
        }, "fenshen-rpc-accept");
        acceptor.setDaemon(true);
        acceptor.start();
    }

    private void serve(LocalSocket socket) {
        try (LocalSocket s = socket) {
            Credentials peer = s.getPeerCredentials();
            if (peer.getUid() != ShellEnv.SHELL_UID && peer.getUid() != ROOT_UID) {
                System.err.println("rpc: rejected peer uid " + peer.getUid());
                return;
            }
            BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
            OutputStream out = s.getOutputStream();
            String line;
            while ((line = in.readLine()) != null) {
                if (line.isEmpty()) {
                    continue;
                }
                JSONObject response = new JSONObject();
                try {
                    JSONObject request = new JSONObject(line);
                    response.put("id", request.opt("id"));
                    JSONObject args = request.optJSONObject("args");
                    JSONObject result = handler.handle(request.getString("cmd"), args == null ? new JSONObject() : args);
                    response.put("ok", true);
                    response.put("result", result == null ? new JSONObject() : result);
                } catch (Throwable t) {
                    Throwable cause = t.getCause() != null ? t.getCause() : t;
                    response.put("ok", false);
                    response.put("error", cause.getClass().getSimpleName() + ": " + cause.getMessage());
                }
                out.write((response.toString() + "\n").getBytes(StandardCharsets.UTF_8));
                out.flush();
            }
        } catch (Exception e) {
            System.err.println("rpc client error: " + e);
        }
    }
}
