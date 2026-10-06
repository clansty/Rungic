package com.rungic.plasma;

import android.Manifest;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.ImageFormat;
import android.hardware.camera2.*;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.*;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.NoiseSuppressor;
import android.net.*;
import android.os.*;
import android.util.Range;
import android.util.Size;
import org.json.*;
import java.io.*;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

/**
 * Private, demand-driven capture with the permissions the user gave the Rungic app. It runs in
 * the independent media backend (MediaDaemon, docs/117), not in the app: the app's process,
 * hidden, frozen or killed, no longer ends a call's audio. What only the app can do (ask for a
 * permission, know that the desktop is in front, Android's call and its notification) the backend
 * asks of it through Host; without the app, the desktop counts as not in front.
 */
final class CaptureBridge implements Closeable {
    /** What the app side provides (MediaDaemon: over the link to the app, MediaLink). */
    interface Host {
        /** Whether the Rungic app has the runtime permission. */
        boolean permitted(String permission);
        /** The user refused it before (asked again only from the app's menu). */
        boolean denied(String permission);
        /** Asks the user in the app (in front), waits for the answer; throws when it cannot. */
        void requestPermission(String permission) throws Exception;
        /** The capture notification's service: what is in use, a call's included. */
        void service(boolean microphone,boolean camera,boolean call);
        /** Android's call for a call with the Agent (AgentCall): -> whether Telecom has it. */
        boolean placeCall();
        void endCall();
        /** Android holds the call for another one, or hung it up. */
        boolean held();
        boolean hungUp();
        /** Something the Linux side follows changed (capture-info, HostEvents.CAPTURE). */
        void changed();
    }
    private final Host host;
    private final Context context;
    private final File path;
    private final CameraManager cameras;
    private final ExecutorService clients=Executors.newFixedThreadPool(6);
    private final Set<LocalSocket> sockets=ConcurrentHashMap.newKeySet();
    // A call with the Agent goes on with the screen locked or Plasma in the background, as a phone
    // call does (2026-10-05, the user): it is a call Android knows (AgentCall), its sockets stay open
    // when the app is hidden, the capture service stays up for the whole call (Android lets only an
    // app in front start it), and a wake lock keeps the phone awake for the Linux side meanwhile.
    // In this backend the call also outlives the app's process (docs/117). Other capture still needs
    // Plasma in front.
    private final Set<LocalSocket> communicationSockets=ConcurrentHashMap.newKeySet();
    private android.os.PowerManager.WakeLock callWakeLock;
    private final Semaphore slots=new Semaphore(6);
    private final AtomicBoolean microphoneBusy=new AtomicBoolean();
    private final AtomicBoolean cameraBusy=new AtomicBoolean();
    private final AtomicBoolean phoneOutputBusy=new AtomicBoolean();
    private final ConcurrentHashMap<String,CommunicationOutput> communicationOutputs=new ConcurrentHashMap<>();
    private final Object permissionLock=new Object();
    private volatile boolean visible, running;
    private volatile boolean micActive, cameraActive;
    private LocalSocket bound;
    private LocalServerSocket server;

    CaptureBridge(Context context,Host host,File path) {
        this.context=context;this.host=host;this.path=path;
        cameras=context.getSystemService(CameraManager.class);
    }
    /** The desktop is in front (the app says so; without the app, it is not). */
    void setVisible(boolean value) {
        if(visible!=value)host.changed();   // the media bridge follows it (capture-info)
        visible=value;
        if(!value) {
            for(LocalSocket socket:sockets)if(!communicationSockets.contains(socket))try { socket.close(); } catch(IOException ignored) {}
        }
    }
    private void ensurePermission(String permission) throws Exception {
        synchronized(permissionLock) {
            if(host.permitted(permission))return;
            if(!visible)throw new IOException("请先返回 Plasma Mobile");
            if(host.denied(permission))throw new SecurityException("请在 Plasma Mobile菜单中开启麦克风与相机权限");
            host.requestPermission(permission);
            if(!visible)throw new IOException("采集已暂停，请返回 Plasma Mobile");
            if(!host.permitted(permission))throw new SecurityException("采集权限未授予");
        }
    }
    private synchronized void captureState(boolean mic,boolean active) throws Exception {
        if(active && !visible && !(mic && inCall()))throw new IOException("请先返回 Plasma Mobile");
        if(mic)micActive=active;else cameraActive=active;
        updateCaptureService();
    }
    private boolean inCall() { return !communicationOutputs.isEmpty(); }
    /** The capture service (the notification Android shows) for what is in use; during a call the
     *  microphone stays in it even while muted, as it could not be started again from the background. */
    private boolean serviceMic, serviceCamera, serviceCall;
    private void updateCaptureService() {
        boolean call=inCall(), mic=micActive || call;
        // Only on a change: Android refuses a foreground service started from the background, and a
        // muted call, unmuted with the screen locked, asked for the same service again.
        if(mic==serviceMic && cameraActive==serviceCamera && call==serviceCall)return;
        serviceMic=mic;serviceCamera=cameraActive;serviceCall=call;
        host.service(mic,cameraActive,call);
    }
    /** The app came back: it shows the service again (its notification went with its process). */
    synchronized void showService() {
        serviceMic=serviceCamera=serviceCall=false;updateCaptureService();
    }
    /** The call started or ended: Telecom's call, the service and the wake lock follow. */
    private boolean callOpen;
    private synchronized void callChanged() {
        if(inCall()!=callOpen) {
            callOpen=inCall();
            if(!callOpen)host.endCall();       // placed by communicationOutput
        }
        updateCaptureService();
        android.os.PowerManager power=context.getSystemService(android.os.PowerManager.class);
        if(inCall() && callWakeLock==null) {
            callWakeLock=power.newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK,"rungic:agent-call");
            callWakeLock.acquire(4*60*60*1000L);     // a bound, should the call's end be missed
        } else if(!inCall() && callWakeLock!=null) {
            if(callWakeLock.isHeld())callWakeLock.release();
            callWakeLock=null;
        }
    }
    private JSONArray cameraList;
    JSONObject info() throws Exception {
        // The phone's cameras do not change: listed once, not on every request (the media bridge
        // asked every second, a camera service round trip per camera each time).
        if(cameraList==null)cameraList=listCameras();
        return new JSONObject().put("version",1).put("visible",visible).put("microphonePermission",host.permitted(Manifest.permission.RECORD_AUDIO))
            .put("microphoneActive",micActive).put("cameraActive",cameraActive)
            .put("cameraPermission",host.permitted(Manifest.permission.CAMERA))
            .put("cameraDenied",host.denied(Manifest.permission.CAMERA))
            .put("microphoneDenied",host.denied(Manifest.permission.RECORD_AUDIO))
            .put("communication",new JSONObject().put("version",1).put("active",!communicationOutputs.isEmpty()).put("aecAvailable",AcousticEchoCanceler.isAvailable()).put("playbackCursor",true)).put("cameras",cameraList);
    }
    private JSONArray listCameras() throws Exception {
        JSONArray list=new JSONArray();
        for(String id:cameras.getCameraIdList()) {
            CameraCharacteristics c=cameras.getCameraCharacteristics(id);
            Integer facing=c.get(CameraCharacteristics.LENS_FACING),orientation=c.get(CameraCharacteristics.SENSOR_ORIENTATION);
            if(facing==null || (facing!=CameraCharacteristics.LENS_FACING_FRONT && facing!=CameraCharacteristics.LENS_FACING_BACK))continue;
            // Export one logical front and one logical rear device, not each
            // physical lens of the same logical multi-camera.
            String name=facing==CameraCharacteristics.LENS_FACING_FRONT?"front":"back";
            boolean duplicate=false;for(int i=0;i<list.length();i++)if(list.getJSONObject(i).getString("facing").equals(name))duplicate=true;
            if(duplicate)continue;
            StreamConfigurationMap map=c.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            Size size=chooseSize(map==null?null:map.getOutputSizes(ImageFormat.YUV_420_888));
            if(size==null)continue;
            list.put(new JSONObject().put("id",id).put("facing",name).put("width",size.getWidth()).put("height",size.getHeight())
                .put("rotation",orientation==null?0:orientation).put("fps",30));
        }
        return list;
    }
    private static Size chooseSize(Size[] sizes) {
        if(sizes==null)return null;
        Size best=null;double score=Double.MAX_VALUE;
        for(Size size:sizes) {
            int w=size.getWidth(),h=size.getHeight();
            if(w<320 || h<240 || w>1920 || h>1080 || (w%2)!=0 || (h%2)!=0)continue;
            double value=Math.abs(Math.log((double)(w*h)/(1280*720)))+2*Math.abs((double)w/h-16.0/9);
            if(value<score) { score=value;best=size; }
        }
        return best;
    }
    synchronized void start() throws IOException {
        if(running)return;
        path.delete();bound=new LocalSocket();
        bound.bind(new LocalSocketAddress(path.getAbsolutePath(),LocalSocketAddress.Namespace.FILESYSTEM));
        server=new LocalServerSocket(bound.getFileDescriptor());
        try { android.system.Os.chmod(path.getAbsolutePath(),0666); } catch(Exception e) { throw new IOException(e); }
        running=true;
        Thread acceptor=new Thread(() -> {
            while(running)try {
                LocalSocket socket=server.accept();int uid=socket.getPeerCredentials().getUid();
                if((uid!=1000 && uid!=0) || !slots.tryAcquire()) { socket.close();continue; }
                sockets.add(socket);
                clients.execute(() -> { try(LocalSocket client=socket) { serve(client); }
                    catch(Exception ignored) {} finally { sockets.remove(socket);communicationSockets.remove(socket);slots.release(); } });
            } catch(Exception ignored) {}
        },"rungic-capture-accept");acceptor.setDaemon(true);acceptor.start();
    }
    private static void json(OutputStream out,JSONObject value) throws Exception {
        out.write((value.toString()+"\n").getBytes(StandardCharsets.UTF_8));out.flush();
    }
    private void serve(LocalSocket socket) throws Exception {
        socket.setSoTimeout(3000);
        socket.setSendBufferSize(131072);
        android.system.Os.setsockoptTimeval(socket.getFileDescriptor(),android.system.OsConstants.SOL_SOCKET,
            android.system.OsConstants.SO_SNDTIMEO,android.system.StructTimeval.fromMillis(1500));
        ByteArrayOutputStream line=new ByteArrayOutputStream();int b;
        while((b=socket.getInputStream().read())!=-1 && b!='\n') { if(line.size()>4096)throw new IOException("Request too large");line.write(b); }
        JSONObject request=new JSONObject(line.toString("UTF-8"));
        try {
            String op=request.getString("op");
            // The backend's state (the same as capture-info): also its readiness check.
            if(op.equals("info")) { json(socket.getOutputStream(),info().put("ok",true));return; }
            // Playback follows the Linux sink, like the Termux output; only capture needs the app in front,
            // except a call's microphone and control once the call is open (communicationSockets).
            boolean call=op.equals("communication-microphone")||op.equals("communication-control");
            if(!visible && !op.equals("phone-output") && !(call && inCall()))throw new IOException("请先返回 Plasma Mobile");
            if(op.startsWith("communication-"))communicationSockets.add(socket);
            switch(op) {
                case "microphone": microphone(socket);break;
                case "camera":camera(socket,request.getString("id"));break;
                case "phone-output":phoneOutput(socket);break;
                case "communication-output":communicationOutput(socket,request.getString("sessionId"));break;
                case "communication-microphone":
                    if(!communicationOutputs.containsKey(communicationSession(request.getString("sessionId"))))throw new IOException("Communication session ended");
                    microphone(socket,true);break;
                case "communication-control":communicationControl(socket,request.getString("sessionId"));break;
                default:throw new IOException("Unsupported capture operation");
            }
        } catch(Exception e) {
            // After stream headers, closing the socket signals failure/EOS;
            // never inject error text into PCM or video payloads.
            throw e;
        }
    }
    // The audio threads forward small blocks: at normal priority, a busy phone (a call, the
    // compositor in this process) starved them and the phone heard a call in pieces (docs/63).
    private static void audioPriority() {
        try { android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_URGENT_AUDIO); } catch(Exception ignored) {}
    }
    private void microphone(LocalSocket socket) throws Exception {
        microphone(socket,false);
    }
    private void microphone(LocalSocket socket,boolean communication) throws Exception {
        audioPriority();
        if(!microphoneBusy.compareAndSet(false,true))throw new IOException("麦克风正在使用中");
        AudioRecord recorder=null;AcousticEchoCanceler aec=null;NoiseSuppressor ns=null;boolean active=false,header=false;
        try {
            ensurePermission(Manifest.permission.RECORD_AUDIO);
            int min=AudioRecord.getMinBufferSize(48000,AudioFormat.CHANNEL_IN_MONO,AudioFormat.ENCODING_PCM_16BIT);
            if(min<0)throw new IOException("Unsupported microphone format");
            recorder=new AudioRecord.Builder().setAudioSource(communication?MediaRecorder.AudioSource.VOICE_COMMUNICATION:MediaRecorder.AudioSource.MIC)
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(48000).setChannelMask(AudioFormat.CHANNEL_IN_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setBufferSizeInBytes(Math.max(min,9600)).build();
            if(recorder.getState()!=AudioRecord.STATE_INITIALIZED)throw new IOException("Microphone unavailable");
            if(communication) {
                if(AcousticEchoCanceler.isAvailable()) { aec=AcousticEchoCanceler.create(recorder.getAudioSessionId());if(aec!=null)aec.setEnabled(true); }
                if(NoiseSuppressor.isAvailable()) { ns=NoiseSuppressor.create(recorder.getAudioSessionId());if(ns!=null)ns.setEnabled(true); }
            }
            captureState(true,true);active=true;recorder.startRecording();
            JSONObject spec=new JSONObject().put("ok",true).put("rate",48000).put("channels",1).put("format","s16le");
            if(communication)spec.put("aec",aec!=null&&aec.getEnabled());
            json(socket.getOutputStream(),spec);header=true;
            byte[] block=new byte[1920];
            while(running && (visible || communication && inCall())) {
                int count=recorder.read(block,0,block.length,AudioRecord.READ_BLOCKING);
                if(count<=0)throw new IOException("Microphone read failed");
                if(communication && host.held())java.util.Arrays.fill(block,0,count,(byte)0);   // held for a phone call
                socket.getOutputStream().write(block,0,count);
            }
        } catch(Exception e) { if(!header)json(socket.getOutputStream(),new JSONObject().put("error",e.getMessage()==null?"Microphone unavailable":e.getMessage())); }
        finally {
            android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_DEFAULT);   // a pool thread: others use it next
            if(recorder!=null) { try { recorder.stop(); } catch(Exception ignored) {} }
            if(aec!=null)aec.release();if(ns!=null)ns.release();
            if(recorder!=null)recorder.release();
            if(active)try { captureState(true,false); } catch(Exception ignored) {}
            microphoneBusy.set(false);
        }
    }
    /** An exclusive communication stream, with epochs fencing queued PCM after interruption. */
    private final class CommunicationOutput {
        final AudioTrack track;
        long epoch=1,written;
        boolean silent;
        CommunicationOutput(AudioTrack track) { this.track=track; }
        synchronized int write(long generation,byte[] pcm,int offset,int count) {
            if(generation!=epoch)return count;
            int n=track.write(pcm,offset,count,AudioTrack.WRITE_NON_BLOCKING);
            if(n>0)written+=n/2;
            return n;
        }
        synchronized JSONObject position() throws JSONException {
            // Held for another call, the Agent is not heard; hung up from Android's side, the Linux
            // session ends the call (AgentCall).
            boolean held=host.held();
            if(held!=silent) { silent=held;track.setVolume(held?0f:1f); }
            return new JSONObject().put("ok",true).put("epoch",epoch).put("hungUp",host.hungUp()).put("held",held)
                .put("playedFrames",Integer.toUnsignedLong(track.getPlaybackHeadPosition())).put("writtenFrames",written).put("rate",48000);
        }
        synchronized JSONObject flush(long generation) throws Exception {
            JSONObject result=position();
            if(generation<=epoch)throw new IOException("Stale playback generation");
            track.pause();track.flush();epoch=generation;written=0;track.play();
            return result.put("newEpoch",epoch);
        }
    }
    private static String communicationSession(String session) throws IOException {
        if(!session.matches("[A-Za-z0-9_-]{1,80}"))throw new IOException("Invalid communication session");
        return session;
    }
    private void communicationOutput(LocalSocket socket,String session) throws Exception {
        communicationSession(session);audioPriority();
        if(!phoneOutputBusy.compareAndSet(false,true))throw new IOException("Phone output busy");
        AudioManager audio=context.getSystemService(AudioManager.class);AudioTrack track=null;
        boolean header=false,telecom=false;int oldMode=audio.getMode();
        CommunicationOutput stream=null;
        try {
            if(!visible)throw new IOException("Open Plasma before starting communication audio");
            if(oldMode!=AudioManager.MODE_NORMAL)throw new IOException("A phone call is using communication audio");
            // A call Android knows (AgentCall, in the app): Telecom sets the mode and the route.
            // Without it, the backend does (communicationMode).
            telecom=host.placeCall();
            if(!telecom)communicationMode(audio);
            int min=AudioTrack.getMinBufferSize(48000,AudioFormat.CHANNEL_OUT_MONO,AudioFormat.ENCODING_PCM_16BIT);
            if(min<0)throw new IOException("Communication output format unavailable");
            track=new AudioTrack.Builder().setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(48000).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(Math.max(min,9600)).build();
            if(track.getState()!=AudioTrack.STATE_INITIALIZED)throw new IOException("Communication output unavailable");
            stream=new CommunicationOutput(track);communicationOutputs.put(session,stream);host.changed();callChanged();track.play();
            json(socket.getOutputStream(),new JSONObject().put("ok",true).put("rate",48000).put("channels",1).put("format","s16le").put("version",1));header=true;
            DataInputStream in=new DataInputStream(socket.getInputStream());socket.setSoTimeout(0);
            while(running) {
                long generation=in.readLong();int count=in.readInt();
                if(count<=0||count>9600||(count%2)!=0)throw new IOException("Invalid communication PCM frame");
                byte[] pcm=new byte[count];in.readFully(pcm);int offset=0;
                while(offset<count&&running) {
                    int n=stream.write(generation,pcm,offset,count-offset);
                    if(n<0)throw new IOException("Communication output write failed");
                    if(n==0)Thread.sleep(2);else offset+=n;
                }
            }
        } catch(Exception e) { if(!header)json(socket.getOutputStream(),new JSONObject().put("error",e.getMessage()==null?"Communication unavailable":e.getMessage())); }
        finally {
            if(stream!=null){communicationOutputs.remove(session,stream);host.changed();callChanged();}
            else if(telecom)host.endCall();       // placed, but the call's audio never opened
            if(track!=null) { try { track.stop(); } catch(Exception ignored) {}track.release(); }
            synchronized(this) {
                if(modeSet) { modeSet=false;audio.clearCommunicationDevice();if(audio.getMode()==AudioManager.MODE_IN_COMMUNICATION)audio.setMode(oldMode); }
            }
            phoneOutputBusy.set(false);android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_DEFAULT);
        }
    }
    /** Whether this backend set the communication mode (no Telecom call, or Telecom's call went). */
    private boolean modeSet;
    private synchronized void communicationMode(AudioManager audio) {
        if(modeSet)return;
        audio.setMode(AudioManager.MODE_IN_COMMUNICATION);modeSet=true;
        int[] order={AudioDeviceInfo.TYPE_WIRED_HEADSET,AudioDeviceInfo.TYPE_USB_HEADSET,AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
            AudioDeviceInfo.TYPE_BLE_HEADSET,AudioDeviceInfo.TYPE_BUILTIN_SPEAKER};
        for(int type:order) {
            boolean selected=false;
            for(AudioDeviceInfo device:audio.getAvailableCommunicationDevices())if(device.getType()==type) {
                selected=audio.setCommunicationDevice(device);if(selected)break;
            }
            if(selected)break;
        }
    }
    /** Android's call for it went with the app (killed): the call goes on, its audio mode now this
     *  backend's, so the voice stays on the call's route and echo cancellation. */
    void callLost() {
        if(inCall())communicationMode(context.getSystemService(AudioManager.class));
    }
    private void communicationControl(LocalSocket socket,String session) throws Exception {
        communicationSession(session);CommunicationOutput stream=communicationOutputs.get(session);
        if(stream==null)throw new IOException("Communication output not ready");
        json(socket.getOutputStream(),new JSONObject().put("ok",true).put("version",1));
        BufferedReader in=new BufferedReader(new InputStreamReader(socket.getInputStream(),StandardCharsets.UTF_8));
        String line;
        while(running&&(line=in.readLine())!=null) {
            if(line.length()>4096)throw new IOException("Control request too large");
            JSONObject req=new JSONObject(line);JSONObject result;
            try {
                if(communicationOutputs.get(session)!=stream)throw new IOException("Communication session ended");
                String op=req.getString("op");
                if(op.equals("position"))result=stream.position();
                else if(op.equals("flush"))result=stream.flush(req.getLong("epoch"));
                else throw new IOException("Unsupported communication control");
            } catch(Exception e) { result=new JSONObject().put("error",e.getMessage()); }
            result.put("id",req.optInt("id"));json(socket.getOutputStream(),result);
        }
    }
    /** The phone's own output: a wired/USB/Bluetooth headset if one is connected, else the speaker. */
    private static AudioDeviceInfo localOutput(AudioManager audio) {
        int[] order={AudioDeviceInfo.TYPE_WIRED_HEADSET,AudioDeviceInfo.TYPE_WIRED_HEADPHONES,AudioDeviceInfo.TYPE_USB_HEADSET,
            AudioDeviceInfo.TYPE_BLE_HEADSET,AudioDeviceInfo.TYPE_BLUETOOTH_A2DP,AudioDeviceInfo.TYPE_BUILTIN_SPEAKER};
        AudioDeviceInfo[] devices=audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS);
        for(int type:order)for(AudioDeviceInfo device:devices)if(device.getType()==type)return device;
        return null;
    }
    /**
     * PCM from the Linux "phone" sink, played on the phone even while Android routes media to a
     * cast display. Linux keeps its default sink on Android's routing; this is the explicit
     * alternative (docs/59). Small socket buffers keep the unreported latency low.
     */
    private void phoneOutput(LocalSocket socket) throws Exception {
        if(!phoneOutputBusy.compareAndSet(false,true))throw new IOException("Phone output busy");
        audioPriority();
        AudioManager audio=context.getSystemService(AudioManager.class);
        AudioTrack track=null;AudioDeviceCallback callback=null;boolean header=false;
        try {
            int min=AudioTrack.getMinBufferSize(48000,AudioFormat.CHANNEL_OUT_STEREO,AudioFormat.ENCODING_PCM_16BIT);
            if(min<0)throw new IOException("Unsupported output format");
            final AudioTrack out=track=new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(48000).setChannelMask(AudioFormat.CHANNEL_OUT_STEREO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                // A small buffer (~40 ms): games and apps play here too, not only speech; a blocking
                // 150 ms buffer made a game's sounds stutter, half a second late. Not the low-latency
                // mode: its track ignored the preferred device and went to the TV (PROXY) while casting.
                .setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(Math.max(min,7680)).build();
            if(out.getState()!=AudioTrack.STATE_INITIALIZED)throw new IOException("Phone output unavailable");
            out.setPreferredDevice(localOutput(audio));
            callback=new AudioDeviceCallback() {
                @Override public void onAudioDevicesAdded(AudioDeviceInfo[] added) { out.setPreferredDevice(localOutput(audio)); }
                @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] removed) { out.setPreferredDevice(localOutput(audio)); }
            };
            audio.registerAudioDeviceCallback(callback,null);
            socket.setReceiveBufferSize(8192);
            json(socket.getOutputStream(),new JSONObject().put("ok",true).put("rate",48000).put("channels",2).put("format","s16le"));header=true;
            socket.setSoTimeout(10000);
            out.play();
            InputStream in=socket.getInputStream();byte[] block=new byte[3840];int pending=0;
            while(running) {
                int count=in.read(block,pending,block.length-pending);
                if(count<0)break;
                pending+=count;
                int frames=pending-pending%4;
                if(frames==0)continue;
                // The Linux side runs on its own clock: what does not fit now is late, dropped (the
                // fill stays bounded); an empty track plays silence until more comes.
                if(out.write(block,0,frames,AudioTrack.WRITE_NON_BLOCKING)<0)throw new IOException("Phone output write failed");
                System.arraycopy(block,frames,block,0,pending-frames);pending-=frames;
            }
        } catch(Exception e) { if(!header)json(socket.getOutputStream(),new JSONObject().put("error",e.getMessage()==null?"Phone output unavailable":e.getMessage())); }
        finally {
            android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_DEFAULT);   // a pool thread: others use it next
            if(callback!=null)audio.unregisterAudioDeviceCallback(callback);
            if(track!=null) {
                android.util.Log.i("RungicAudio","phone output ended, underruns="+track.getUnderrunCount());
                try { track.stop(); } catch(Exception ignored) {}track.release();
            }
            phoneOutputBusy.set(false);
        }
    }
    private void camera(LocalSocket socket,String id) throws Exception {
        long deadline=SystemClock.elapsedRealtime()+2000;
        while(!cameraBusy.compareAndSet(false,true)) {
            if(!visible || SystemClock.elapsedRealtime()>deadline)throw new IOException("相机正在使用中");
            Thread.sleep(25);
        }
        HandlerThread thread=new HandlerThread("rungic-camera");thread.start();Handler handler=new Handler(thread.getLooper());
        Object cameraLock=new Object();AtomicBoolean accepting=new AtomicBoolean(true);
        AtomicReference<CameraDevice> device=new AtomicReference<>();
        AtomicReference<CameraCaptureSession> session=new AtomicReference<>();
        AtomicReference<Image> latest=new AtomicReference<>();Semaphore frames=new Semaphore(0);
        AtomicReference<String> failure=new AtomicReference<>();CountDownLatch configured=new CountDownLatch(1);
        ImageReader reader=null;boolean active=false,header=false;
        try {
            JSONObject metadata=null;JSONArray list=info().getJSONArray("cameras");
            for(int i=0;i<list.length();i++)if(list.getJSONObject(i).getString("id").equals(id))metadata=list.getJSONObject(i);
            if(metadata==null)throw new IOException("Unknown camera");
            ensurePermission(Manifest.permission.CAMERA);
            int width=metadata.getInt("width"),height=metadata.getInt("height"),rotation=metadata.getInt("rotation");
            reader=ImageReader.newInstance(width,height,ImageFormat.YUV_420_888,3);
            final ImageReader source=reader;
            reader.setOnImageAvailableListener(r -> {
                try {
                    Image image=r.acquireLatestImage();if(image==null)return;
                    if(!accepting.get()) { image.close();return; }
                    Image old=latest.getAndSet(image);if(old!=null)old.close();
                    if(frames.availablePermits()==0)frames.release();
                } catch(Exception ignored) {}
            },handler);
            CameraCharacteristics traits=cameras.getCameraCharacteristics(id);
            captureState(false,true);active=true;
            cameras.openCamera(id,new CameraDevice.StateCallback() {
                @Override public void onOpened(CameraDevice d) {
                    synchronized(cameraLock) {
                    if(!accepting.get()) { d.close();thread.quitSafely();return; }
                    device.set(d);
                    try {
                        d.createCaptureSession(Collections.singletonList(source.getSurface()),new CameraCaptureSession.StateCallback() {
                            @Override public void onConfigured(CameraCaptureSession s) {
                                synchronized(cameraLock) {
                                if(!accepting.get()) { s.close();return; }
                                session.set(s);
                                try {
                                    CaptureRequest.Builder request=d.createCaptureRequest(CameraDevice.TEMPLATE_RECORD);
                                    request.addTarget(source.getSurface());
                                    int[] modes=traits.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES);
                                    if(modes!=null)for(int mode:modes)if(mode==CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)request.set(CaptureRequest.CONTROL_AF_MODE,mode);
                                    Range<Integer>[] ranges=traits.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES);
                                    Range<Integer> best=null;
                                    if(ranges!=null)for(Range<Integer> range:ranges)
                                        if(range.getUpper()==30 && (best==null || range.getLower()>best.getLower()))best=range;
                                    if(best!=null)request.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE,best);
                                    s.setRepeatingRequest(request.build(),null,handler);
                                } catch(Exception e) { failure.set("Camera configuration failed"); }
                                configured.countDown();
                                }
                            }
                            @Override public void onConfigureFailed(CameraCaptureSession s) { failure.set("Camera configuration failed");configured.countDown(); }
                        },handler);
                    } catch(Exception e) { failure.set("Camera unavailable");configured.countDown(); }
                    }
                }
                @Override public void onDisconnected(CameraDevice d) { failure.set("Camera disconnected");d.close();configured.countDown();frames.release(); }
                @Override public void onError(CameraDevice d,int error) { failure.set("Camera error "+error);d.close();configured.countDown();frames.release(); }
            },handler);
            if(!configured.await(6,TimeUnit.SECONDS) || failure.get()!=null)throw new IOException(failure.get()==null?"Camera timed out":failure.get());
            json(socket.getOutputStream(),new JSONObject(metadata.toString()).put("ok",true).put("format","android-yuv420"));header=true;
            DataOutputStream out=new DataOutputStream(socket.getOutputStream());byte[][] data=new byte[3][];
            while(running && visible && failure.get()==null) {
                if(!frames.tryAcquire(3,TimeUnit.SECONDS))throw new IOException("No camera frames");
                Image image=latest.getAndSet(null);if(image==null)continue;
                try {
                    Image.Plane[] planes=image.getPlanes();int[] lengths=new int[3];
                    for(int i=0;i<3;i++) {
                        ByteBuffer buffer=planes[i].getBuffer();int size=buffer.remaining();lengths[i]=size;
                        if(size>width*height*2)throw new IOException("Invalid image plane");
                        if(data[i]==null || data[i].length<size)data[i]=new byte[size];
                        buffer.get(data[i],0,size);
                    }
                    out.writeInt(0x4d43414d);out.writeInt(1);out.writeInt(width);out.writeInt(height);out.writeInt(rotation);
                    for(Image.Plane plane:planes)out.writeInt(plane.getRowStride());
                    for(Image.Plane plane:planes)out.writeInt(plane.getPixelStride());
                    for(int length:lengths)out.writeInt(length);
                    out.writeLong(image.getTimestamp());
                    for(int i=0;i<3;i++)out.write(data[i],0,lengths[i]);
                } finally { image.close(); }
            }
        } catch(Exception e) { if(!header)json(socket.getOutputStream(),new JSONObject().put("error",e.getMessage()==null?"Camera unavailable":e.getMessage())); }
        finally {
            synchronized(cameraLock) {
                accepting.set(false);
                CameraCaptureSession s=session.get();if(s!=null)s.close();
                CameraDevice d=device.get();if(d!=null)d.close();
            }
            if(reader!=null)reader.setOnImageAvailableListener(null,null);
            // Keep the callback looper alive briefly if openCamera timed out;
            // a late onOpened must still close the newly delivered device.
            if(device.get()==null)handler.postDelayed(thread::quitSafely,10000);
            else { thread.quitSafely();thread.join(1500); }
            Image image=latest.getAndSet(null);if(image!=null)image.close();
            if(reader!=null)reader.close();
            if(active)try { captureState(false,false); } catch(Exception ignored) {}
            cameraBusy.set(false);
        }
    }
    @Override public synchronized void close() throws IOException {
        running=false;setVisible(false);
        if(server!=null)server.close();if(bound!=null)bound.close();path.delete();clients.shutdownNow();
        host.service(false,false,false);
    }
}
