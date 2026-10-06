package com.rungic.plasma;

import android.app.*;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

/** Explicit camera/microphone FGS; never starts a capture by itself. During a call with the Agent
 *  it is the call's service, with Android's call notification and its hang-up (AgentCall). */
public final class CaptureService extends Service {
    @Override public void onCreate() {
        super.onCreate();
        getSystemService(NotificationManager.class).createNotificationChannel(
            new NotificationChannel("capture",getString(R.string.capture_channel),NotificationManager.IMPORTANCE_LOW));
    }
    @Override public int onStartCommand(Intent intent,int flags,int id) {
        if(intent!=null && "hang-up".equals(intent.getAction())) { AgentCall.hangUp();return START_NOT_STICKY; }
        boolean mic=intent!=null && intent.getBooleanExtra("microphone",false);
        boolean camera=intent!=null && intent.getBooleanExtra("camera",false);
        boolean call=intent!=null && intent.getBooleanExtra("call",false);
        if(!mic && !camera) { stopSelf();return START_NOT_STICKY; }
        String title=getString(mic && camera?R.string.capture_both:mic?R.string.capture_microphone:R.string.capture_camera);
        PendingIntent open=PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder notification=new Notification.Builder(this,"capture").setSmallIcon(android.R.drawable.ic_menu_camera)
            .setContentTitle(title).setContentText(getString(R.string.capture_stop_hint)).setContentIntent(open).setOngoing(true);
        int types=(mic?ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE:0)|(camera?ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA:0);
        if(call) {
            types|=ServiceInfo.FOREGROUND_SERVICE_TYPE_PHONE_CALL;
            notification.setSmallIcon(android.R.drawable.stat_sys_phone_call).setContentTitle(getString(R.string.agent_call))
                .setContentText(getString(R.string.agent_call_hint)).setCategory(Notification.CATEGORY_CALL);
            PendingIntent hangUp=PendingIntent.getService(this,1,new Intent(this,CaptureService.class).setAction("hang-up"),
                PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);
            if(Build.VERSION.SDK_INT>=31)
                notification.setStyle(Notification.CallStyle.forOngoingCall(new Person.Builder().setName("Agent").build(),hangUp));
            else notification.addAction(new Notification.Action.Builder(null,getString(R.string.agent_call_hang_up),hangUp).build());
        }
        // The media backend uses the microphone and cameras (docs/117); this is only Android's
        // notice of it. Android refuses a microphone or camera service started while the app is not
        // in front: the notice is then left out, never the app's process (a refusal ended it).
        try { startForeground(2,notification.build(),types); }
        catch(RuntimeException e) { android.util.Log.w("RungicMedia","capture notification refused",e);stopSelf(); }
        return START_NOT_STICKY;
    }
    @Override public IBinder onBind(Intent intent) { return null; }
}
