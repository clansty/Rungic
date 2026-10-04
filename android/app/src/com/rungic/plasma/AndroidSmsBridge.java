package com.rungic.plasma;

import android.Manifest;
import android.app.Activity;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.provider.Telephony;
import android.telephony.SmsManager;
import android.telephony.SubscriptionManager;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.ArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/** Text messages through Android's SIM for the Linux side (op sms): send and list. Android keeps
 * the messages (the system SMS provider; a message sent here is saved to its sent box). SEND_SMS
 * and READ_SMS come from root (pm grant), like the telephony bridge's READ_PHONE_STATE. Answered on
 * its own thread: a send waits for the radio's result. */
final class AndroidSmsBridge {
    static final int MAX_TEXT=1000;
    private static final long SENT_WAIT_MS=45000, DELIVERED_WAIT_MS=15000;
    private static final AtomicInteger NEXT=new AtomicInteger();
    private final Activity activity;
    private final AndroidNetworkBridge root;
    AndroidSmsBridge(Activity activity,AndroidNetworkBridge root) { this.activity=activity;this.root=root; }

    private void grant(String permission) throws Exception {
        if(activity.checkSelfPermission(permission)!=PackageManager.PERMISSION_GRANTED)
            root.rootShell("/system/bin/pm grant "+activity.getPackageName()+" "+permission,10000);
        if(activity.checkSelfPermission(permission)!=PackageManager.PERMISSION_GRANTED)
            throw new SecurityException("Android did not grant "+permission);
    }

    JSONObject handle(JSONObject request) throws Exception {
        String action=request.optString("action");
        if(action.equals("send"))return send(request);
        if(action.equals("list"))return list(request);
        throw new IllegalArgumentException("Unknown sms action");
    }

    /** A phone number: digits with an optional leading +, as dialled (spaces and dashes removed). */
    static String number(String raw) {
        String value=raw==null?"":raw.replaceAll("[\\s-]","");
        if(!value.matches("\\+?[0-9]{3,20}"))throw new IllegalArgumentException("Invalid phone number");
        return value;
    }

    private static String result(int code) {
        switch(code) {
            case Activity.RESULT_OK: return "sent";
            case SmsManager.RESULT_ERROR_NO_SERVICE: return "no-service";
            case SmsManager.RESULT_ERROR_RADIO_OFF: return "radio-off";
            case SmsManager.RESULT_ERROR_NULL_PDU: return "null-pdu";
            case SmsManager.RESULT_ERROR_LIMIT_EXCEEDED: return "limit-exceeded";
            case SmsManager.RESULT_ERROR_SHORT_CODE_NOT_ALLOWED: return "short-code-not-allowed";
            case SmsManager.RESULT_ERROR_SHORT_CODE_NEVER_ALLOWED: return "short-code-never-allowed";
            default: return "error-"+code;
        }
    }

    private JSONObject send(JSONObject request) throws Exception {
        String to=number(request.getString("to"));
        String text=request.getString("text");
        if(text.isEmpty() || text.length()>MAX_TEXT)throw new IllegalArgumentException("Text must be 1 to "+MAX_TEXT+" characters");
        grant(Manifest.permission.SEND_SMS);
        int subscription=SubscriptionManager.getDefaultSmsSubscriptionId();
        if(subscription==SubscriptionManager.INVALID_SUBSCRIPTION_ID)throw new IllegalStateException("No SIM for text messages");
        SmsManager sms=activity.getSystemService(SmsManager.class).createForSubscriptionId(subscription);
        ArrayList<String> parts=sms.divideMessage(text);
        int id=NEXT.incrementAndGet();
        String sentAction=activity.getPackageName()+".SMS_SENT."+id, deliveredAction=activity.getPackageName()+".SMS_DELIVERED."+id;
        CountDownLatch sent=new CountDownLatch(parts.size()), delivered=new CountDownLatch(parts.size());
        int[] firstError={Activity.RESULT_OK};
        AtomicInteger deliveredOk=new AtomicInteger();
        BroadcastReceiver receiver=new BroadcastReceiver() {
            @Override public void onReceive(Context context,Intent intent) {
                if(sentAction.equals(intent.getAction())) {
                    synchronized(firstError) { if(getResultCode()!=Activity.RESULT_OK && firstError[0]==Activity.RESULT_OK)firstError[0]=getResultCode(); }
                    sent.countDown();
                } else if(deliveredAction.equals(intent.getAction())) {
                    deliveredOk.incrementAndGet();
                    delivered.countDown();
                }
            }
        };
        IntentFilter filter=new IntentFilter();filter.addAction(sentAction);filter.addAction(deliveredAction);
        activity.registerReceiver(receiver,filter,Context.RECEIVER_NOT_EXPORTED);
        try {
            ArrayList<PendingIntent> sentIntents=new ArrayList<>(), deliveredIntents=new ArrayList<>();
            for(int i=0;i<parts.size();i++) {
                int flags=PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_ONE_SHOT;
                sentIntents.add(PendingIntent.getBroadcast(activity,id*64+i,new Intent(sentAction).setPackage(activity.getPackageName()),flags));
                deliveredIntents.add(PendingIntent.getBroadcast(activity,id*64+32+i,new Intent(deliveredAction).setPackage(activity.getPackageName()),flags));
            }
            long started=System.currentTimeMillis();
            if(parts.size()==1)sms.sendTextMessage(to,null,text,sentIntents.get(0),deliveredIntents.get(0));
            else sms.sendMultipartTextMessage(to,null,parts,sentIntents,deliveredIntents);
            boolean radioAnswered=sent.await(SENT_WAIT_MS,TimeUnit.MILLISECONDS);
            JSONObject reply=new JSONObject().put("parts",parts.size()).put("sentAt",started);
            int error;
            synchronized(firstError) { error=firstError[0]; }
            if(!radioAnswered)return reply.put("status","pending").put("error","The radio did not report a result in time");
            if(error!=Activity.RESULT_OK)return reply.put("status","failed").put("error",result(error));
            // Delivery reports are optional (the network may not send them): wait briefly only.
            delivered.await(DELIVERED_WAIT_MS,TimeUnit.MILLISECONDS);
            return reply.put("status","sent").put("delivered",deliveredOk.get()>=parts.size());
        } finally {
            try { activity.unregisterReceiver(receiver); } catch(IllegalArgumentException ignored) {}
        }
    }

    /** Messages of the SMS provider, newest first: {"box":"inbox"|"sent"|"all","from":"<number>","since":<ms>,"limit":<n>}. */
    private JSONObject list(JSONObject request) throws Exception {
        grant(Manifest.permission.READ_SMS);
        String box=request.optString("box","inbox");
        Uri uri;
        switch(box) {
            case "inbox": uri=Telephony.Sms.Inbox.CONTENT_URI;break;
            case "sent": uri=Telephony.Sms.Sent.CONTENT_URI;break;
            case "all": uri=Telephony.Sms.CONTENT_URI;break;
            default: throw new IllegalArgumentException("Unknown box");
        }
        int limit=Math.max(1,Math.min(100,request.optInt("limit",20)));
        StringBuilder where=new StringBuilder();
        ArrayList<String> args=new ArrayList<>();
        if(request.has("since")) { where.append(Telephony.Sms.DATE+">=?");args.add(Long.toString(request.getLong("since"))); }
        String from=request.optString("from","");
        // Android stores the number as the network gave it (with or without +86): compare the
        // digits' end here rather than with SQL functions the provider may not accept.
        String digits=from.isEmpty()?"":number(from).replace("+","");
        JSONArray messages=new JSONArray();
        int scanned=0;
        try(Cursor c=activity.getContentResolver().query(uri,
                new String[]{Telephony.Sms._ID,Telephony.Sms.ADDRESS,Telephony.Sms.BODY,Telephony.Sms.DATE,Telephony.Sms.TYPE,Telephony.Sms.READ},
                where.length()==0?null:where.toString(),args.toArray(new String[0]),Telephony.Sms.DATE+" DESC")) {
            while(c!=null && c.moveToNext() && messages.length()<limit && scanned++<2000) {
                String address=c.getString(1)==null?"":c.getString(1).replaceAll("[^0-9]","");
                if(!digits.isEmpty() && !address.endsWith(digits))continue;
                int type=c.getInt(4);
                messages.put(new JSONObject().put("id",c.getLong(0)).put("address",c.getString(1)).put("text",c.getString(2))
                        .put("date",c.getLong(3)).put("box",type==Telephony.Sms.MESSAGE_TYPE_SENT?"sent":type==Telephony.Sms.MESSAGE_TYPE_INBOX?"inbox":"other")
                        .put("read",c.getInt(5)!=0));
            }
        }
        return new JSONObject().put("messages",messages);
    }
}
