// SPDX-License-Identifier: MIT
package com.rungic.plasma;

import android.content.Context;
import android.content.pm.PackageManager;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Looper;
import java.io.*;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

/**
 * The independent media backend (docs/117): the phone's microphone, cameras and speaker for the
 * Linux side (CaptureBridge, its capture socket) in a root process of its own, started and
 * supervised by system/android-media as the device backend is. The app's process, hidden, frozen
 * or killed, no longer ends a call's audio or the Linux side's capture sessions.
 *
 * What only the app can do goes over the link (abstract socket LINK, the app's uid only, MediaLink
 * in the app): the user's permission answers, whether the desktop is in front, Android's call for a
 * call with the Agent (AgentCall) and the capture notification. Without the app the desktop counts
 * as not in front: other capture waits for it, a call goes on (its audio mode then this backend's).
 *
 * Messages are JSON lines. Backend to app: info (what capture-info answers), service, call-end, and
 * the requests request-permission and call-start (answered with {"reply":id,...}). App to backend:
 * ui (visible, denied), call (held, hungUp) and the replies.
 */
public final class MediaDaemon implements CaptureBridge.Host {
    static final String LINK="com.rungic.media.v1";
    private static final String PACKAGE="com.rungic.plasma";
    private final Context context;
    private final int appUid;
    private CaptureBridge bridge;
    private final Object linkLock=new Object();
    private LocalSocket link;
    private volatile boolean deniedMicrophone, deniedCamera, held, hungUp, telecom;
    private boolean serviceMicrophone, serviceCamera, serviceCall;
    private int nextId;
    private final java.util.Map<Integer,JSONObject> replies=new java.util.HashMap<>();

    private MediaDaemon(Context context,int appUid) { this.context=context;this.appUid=appUid; }

    @Override public boolean permitted(String permission) {
        return context.getPackageManager().checkPermission(permission,PACKAGE)==PackageManager.PERMISSION_GRANTED;
    }
    @Override public boolean denied(String permission) {
        return permission.equals(android.Manifest.permission.CAMERA)?deniedCamera:deniedMicrophone;
    }
    @Override public void requestPermission(String permission) throws Exception {
        if(request(new JSONObject().put("op","request-permission").put("permission",permission),45000)==null)
            throw new IOException("请先返回 Plasma Mobile");
    }
    @Override public void service(boolean microphone,boolean camera,boolean call) {
        synchronized(linkLock) { serviceMicrophone=microphone;serviceCamera=camera;serviceCall=call; }
        sendService();
    }
    @Override public boolean placeCall() {
        held=false;hungUp=false;telecom=false;
        try {
            JSONObject answer=request(new JSONObject().put("op","call-start"),3500);
            telecom=answer!=null && answer.optBoolean("telecom");
        } catch(Exception ignored) {}
        return telecom;
    }
    @Override public void endCall() {
        telecom=false;held=false;
        try { send(new JSONObject().put("op","call-end")); } catch(Exception ignored) {}
    }
    @Override public boolean held() { return held; }
    @Override public boolean hungUp() { return hungUp; }
    @Override public void changed() {
        try { send(new JSONObject().put("op","info").put("info",bridge.info())); } catch(Exception ignored) {}
    }

    private void sendService() {
        try {
            JSONObject message;
            synchronized(linkLock) {
                message=new JSONObject().put("op","service").put("microphone",serviceMicrophone).put("camera",serviceCamera).put("call",serviceCall);
            }
            send(message);
        } catch(Exception ignored) {}
    }
    private void send(JSONObject message) throws IOException {
        synchronized(linkLock) {
            if(link==null)throw new IOException("The app is not connected");
            OutputStream out=link.getOutputStream();
            out.write((message.toString()+"\n").getBytes(StandardCharsets.UTF_8));out.flush();
        }
    }
    /** A request to the app; -> its reply, null without the app or when it does not answer in time. */
    private JSONObject request(JSONObject message,long timeout) throws Exception {
        synchronized(linkLock) {
            if(link==null)return null;
            int id=++nextId;LocalSocket asked=link;
            replies.put(id,null);
            try {
                send(message.put("id",id));
                long deadline=System.currentTimeMillis()+timeout;
                while(replies.get(id)==null && link==asked) {
                    long left=deadline-System.currentTimeMillis();
                    if(left<=0)break;
                    linkLock.wait(left);
                }
                return replies.get(id);
            } finally { replies.remove(id); }
        }
    }

    /** One app at a time: a new link replaces the old (the app restarted). */
    private void serveLink(LocalSocket socket) {
        try {
            if(socket.getPeerCredentials().getUid()!=appUid) { socket.close();return; }
            synchronized(linkLock) {
                if(link!=null)try { link.close(); } catch(IOException ignored) {}
                link=socket;
            }
            sendService();changed();
            BufferedReader in=new BufferedReader(new InputStreamReader(socket.getInputStream(),StandardCharsets.UTF_8));
            String line;
            while((line=in.readLine())!=null) {
                if(line.length()>65536)break;
                JSONObject message=new JSONObject(line);
                if(message.has("reply")) {
                    synchronized(linkLock) {
                        int id=message.getInt("reply");
                        if(replies.containsKey(id)) { replies.put(id,message);linkLock.notifyAll(); }
                    }
                    continue;
                }
                switch(message.optString("op")) {
                    case "ui":
                        deniedMicrophone=message.optBoolean("microphoneDenied");deniedCamera=message.optBoolean("cameraDenied");
                        bridge.setVisible(message.optBoolean("visible"));
                        changed();
                        break;
                    case "call":
                        held=message.optBoolean("held");
                        if(message.optBoolean("hungUp"))hungUp=true;
                        break;
                    default: break;
                }
            }
        } catch(Exception ignored) {}
        finally {
            boolean current;
            synchronized(linkLock) {
                current=link==socket;
                if(current) { link=null;linkLock.notifyAll(); }
            }
            try { socket.close(); } catch(IOException ignored) {}
            if(current) {
                // The app is gone: the desktop is not in front; Android's call went with it.
                bridge.setVisible(false);
                held=false;
                if(telecom) { telecom=false;bridge.callLost(); }
            }
        }
    }

    /** CameraManager.openCamera reads a developer toggle from Settings, which a process unknown to
     *  ActivityManager may not (SecurityException); the framework caches it, so it is preset. */
    private static void presetDesktopModeToggle() {
        try {
            java.lang.reflect.Field cached=Class.forName("android.window.DesktopModeFlags").getDeclaredField("sCachedToggleOverride");
            cached.setAccessible(true);
            for(Object value:cached.getType().getEnumConstants())if(value.toString().equals("OVERRIDE_UNSET"))cached.set(null,value);
        } catch(Exception ignored) {}   // an older framework without it reads no such setting
    }

    public static void main(String[] args) {
        try {
            // As android-device starts it: the app's uid and the APK (the code's, not used here).
            if(android.os.Process.myUid()!=0 || args.length<1)throw new IllegalArgumentException();
            int appUid=Integer.parseInt(args[0]);
            if(appUid<10000)throw new IllegalArgumentException();
            Looper.prepareMainLooper();
            Class<?> at=Class.forName("android.app.ActivityThread");
            Context context=(Context)at.getMethod("getSystemContext").invoke(at.getMethod("systemMain").invoke(null));
            presetDesktopModeToggle();
            MediaDaemon daemon=new MediaDaemon(context,appUid);
            daemon.bridge=new CaptureBridge(context,daemon,new File("capture.sock").getAbsoluteFile());
            LocalServerSocket server=new LocalServerSocket(LINK);
            daemon.bridge.start();
            // Published after binding: a second instance cannot replace a live one.
            try(FileWriter pid=new FileWriter("pid")) { pid.write(Integer.toString(android.os.Process.myPid())); }
            new Thread(()->{
                for(;;)try {
                    LocalSocket socket=server.accept();
                    new Thread(()->daemon.serveLink(socket),"media-link").start();
                } catch(IOException e) { System.exit(1); }
            },"media-link-accept").start();
            Looper.loop();
        } catch(Throwable e) {
            System.err.println("media backend unavailable: "+e.getClass().getSimpleName()+": "+e.getMessage());System.exit(1);
        }
    }
}
