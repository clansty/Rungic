// SPDX-License-Identifier: MIT
package com.rungic.plasma;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import org.json.JSONArray;
import org.json.JSONObject;

/**
 * The app's side of the media backend (MediaDaemon, docs/117): the microphone, cameras and speaker
 * are the backend's; the app tells it what only the app knows (the desktop in front, the user's
 * permission answers) and does what only an app can (ask for a permission, Android's call for a
 * call with the Agent, the capture notification). capture-info is the backend's last state.
 */
final class MediaLink implements Closeable {
    static final int PERMISSION_REQUEST=9041;
    private final Activity activity;
    private final Object lock=new Object();
    private LocalSocket socket;
    private volatile boolean visible, running;
    private JSONObject info;
    private int permissionRequest;
    private boolean serviceMicrophone, serviceCamera, serviceCall;
    private volatile boolean callWanted;
    private final Object callLock=new Object();
    private Thread thread;

    MediaLink(Activity activity) { this.activity=activity; }

    void setVisible(boolean value) {
        visible=value;
        sendUi();
    }
    /** The user answered (onRequestPermissionsResult): a refusal is kept, the backend answered. */
    void permissionResult(String[] permissions,int[] results) {
        for(int i=0;i<permissions.length && i<results.length;i++)if(results[i]!=PackageManager.PERMISSION_GRANTED)
            preferences().edit().putBoolean(deniedKey(permissions[i]),true).apply();
        HostEvents.bump(HostEvents.CAPTURE);
        sendUi();
        int id;
        synchronized(lock) { id=permissionRequest;permissionRequest=0; }
        if(id!=0)send(reply(id));
    }
    /** The menu's "permissions": asks again for what is not granted, refusals forgotten. */
    void requestPermissionsFromUser() {
        preferences().edit().remove("denied-camera").remove("denied-microphone").apply();
        HostEvents.bump(HostEvents.CAPTURE);
        sendUi();
        ArrayList<String> required=new ArrayList<>();
        for(String permission:new String[]{Manifest.permission.CAMERA,Manifest.permission.RECORD_AUDIO})
            if(activity.checkSelfPermission(permission)!=PackageManager.PERMISSION_GRANTED)required.add(permission);
        if(!required.isEmpty())activity.requestPermissions(required.toArray(new String[0]),PERMISSION_REQUEST);
        else android.widget.Toast.makeText(activity,R.string.capture_permissions_on,android.widget.Toast.LENGTH_SHORT).show();
    }
    /** What capture-info answers: the backend's state, or, without the backend, nothing in use. */
    JSONObject info() throws Exception {
        synchronized(lock) { if(info!=null)return new JSONObject(info.toString()).put("visible",visible); }
        return new JSONObject().put("version",1).put("visible",visible).put("backend",false)
            .put("microphonePermission",activity.checkSelfPermission(Manifest.permission.RECORD_AUDIO)==PackageManager.PERMISSION_GRANTED)
            .put("cameraPermission",activity.checkSelfPermission(Manifest.permission.CAMERA)==PackageManager.PERMISSION_GRANTED)
            .put("microphoneActive",false).put("cameraActive",false)
            .put("microphoneDenied",preferences().getBoolean("denied-microphone",false))
            .put("cameraDenied",preferences().getBoolean("denied-camera",false))
            .put("communication",new JSONObject().put("version",1).put("active",false).put("aecAvailable",false).put("playbackCursor",true))
            .put("cameras",new JSONArray());
    }

    private android.content.SharedPreferences preferences() { return activity.getPreferences(Activity.MODE_PRIVATE); }
    private static String deniedKey(String permission) {
        return permission.equals(Manifest.permission.CAMERA)?"denied-camera":"denied-microphone";
    }
    private static JSONObject reply(int id) {
        try { return new JSONObject().put("reply",id); } catch(Exception e) { throw new RuntimeException(e); }
    }
    private void sendUi() {
        try {
            send(new JSONObject().put("op","ui").put("visible",visible)
                .put("microphoneDenied",preferences().getBoolean("denied-microphone",false))
                .put("cameraDenied",preferences().getBoolean("denied-camera",false)));
        } catch(Exception ignored) {}
    }
    private void send(JSONObject message) {
        synchronized(lock) {
            if(socket==null)return;
            try {
                OutputStream out=socket.getOutputStream();
                out.write((message.toString()+"\n").getBytes(StandardCharsets.UTF_8));out.flush();
            } catch(IOException e) { try { socket.close(); } catch(IOException ignored) {} }
        }
    }

    synchronized void start() {
        if(running)return;
        running=true;
        thread=new Thread(this::run,"rungic-media-link");thread.setDaemon(true);thread.start();
        Thread call=new Thread(this::followCall,"rungic-media-call");call.setDaemon(true);call.start();
    }
    /** Connected while the app runs; the backend may start later or restart. */
    private void run() {
        while(running) {
            LocalSocket s=new LocalSocket();
            try {
                s.connect(new LocalSocketAddress(MediaDaemon.LINK,LocalSocketAddress.Namespace.ABSTRACT));
                if(s.getPeerCredentials().getUid()!=0)throw new IOException("Unexpected media backend");
                synchronized(lock) { socket=s; }
                sendUi();
                BufferedReader in=new BufferedReader(new InputStreamReader(s.getInputStream(),StandardCharsets.UTF_8));
                String line;
                while(running && (line=in.readLine())!=null)handle(new JSONObject(line));
            } catch(Exception ignored) {}
            finally {
                synchronized(lock) { if(socket==s) { socket=null;info=null; } }
                try { s.close(); } catch(IOException ignored) {}
                HostEvents.bump(HostEvents.CAPTURE);
            }
            if(running)try { Thread.sleep(1000); } catch(InterruptedException e) { return; }
        }
    }
    private void handle(JSONObject message) throws Exception {
        switch(message.optString("op")) {
            case "info":
                synchronized(lock) { info=message.getJSONObject("info"); }
                HostEvents.bump(HostEvents.CAPTURE);    // the media bridge follows it
                break;
            case "service":
                service(message.optBoolean("microphone"),message.optBoolean("camera"),message.optBoolean("call"));
                break;
            case "request-permission": {
                int id=message.getInt("id");String permission=message.getString("permission");
                if(!visible) { send(reply(id));break; }
                synchronized(lock) { permissionRequest=id; }
                activity.runOnUiThread(() -> activity.requestPermissions(new String[]{permission},PERMISSION_REQUEST));
                break;
            }
            case "call-start": {
                int id=message.getInt("id");
                synchronized(callLock) { callWanted=true;callLock.notifyAll(); }
                new Thread(() -> {
                    boolean telecom=AgentCall.start(activity,2000);
                    try { send(reply(id).put("telecom",telecom)); } catch(Exception ignored) {}
                },"rungic-agent-call").start();
                break;
            }
            case "call-end":
                callWanted=false;AgentCall.end();
                break;
            default: break;
        }
    }
    /** Android's side of the call (held for another call, hung up from the notification), followed
     *  only while there is a call: no wake-ups otherwise. */
    private void followCall() {
        boolean held=false,hungUp=false;
        while(running) {
            try {
                synchronized(callLock) { while(running && !callWanted)callLock.wait(); }
                Thread.sleep(200);
            } catch(InterruptedException e) { return; }
            boolean h=AgentCall.held,u=AgentCall.hungUp;
            if(callWanted && (h!=held || u!=hungUp)) {
                held=h;hungUp=u;
                try { send(new JSONObject().put("op","call").put("held",h).put("hungUp",u)); } catch(Exception ignored) {}
            }
            if(!callWanted) { held=false;hungUp=false; }
        }
    }
    /** The capture notification for what the backend uses. Android lets only an app in front start
     *  it: started when it can, so only on a change. */
    private void service(boolean microphone,boolean camera,boolean call) {
        if(microphone==serviceMicrophone && camera==serviceCamera && call==serviceCall)return;
        // Started from behind (the app came back while the backend was recording), Android refuses
        // it; a call's is started while the desktop is in front, when the call starts.
        if((microphone || camera) && !visible && !call && !serviceMicrophone && !serviceCamera)return;
        serviceMicrophone=microphone;serviceCamera=camera;serviceCall=call;
        activity.runOnUiThread(() -> {
            try {
                if(microphone || camera)activity.startForegroundService(new Intent(activity,CaptureService.class)
                    .putExtra("microphone",microphone).putExtra("camera",camera).putExtra("call",call));
                else activity.stopService(new Intent(activity,CaptureService.class));
            } catch(RuntimeException e) { android.util.Log.w("RungicMedia","capture notification not shown",e); }
        });
    }

    @Override public void close() {
        running=false;
        synchronized(lock) { if(socket!=null)try { socket.close(); } catch(IOException ignored) {} }
        if(thread!=null)thread.interrupt();
        activity.stopService(new Intent(activity,CaptureService.class));
    }
}
