/* Android camera -> PipeWire Video/Source.
 * PipeWire stream setup derived from its MIT-licensed video-src.c example,
 * Copyright 2018 Wim Taymans. This adapter is licensed under MIT.
 * Pixel conversion/rotation uses libyuv (BSD-3-Clause), not hand-written YUV math.
 */
#include <pipewire/pipewire.h>
#include <spa/param/video/format-utils.h>
#include <json-glib/json-glib.h>
#include <libyuv.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <signal.h>
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <thread>
#include <vector>
#include <string>
#include <stdexcept>
#include <cstdlib>
#include <cstring>
#include <clocale>
#include <libintl.h>

// The node's name in the session's language (po/zh_CN/rungic-shared.po, rungic-plasma-bridges).
static const char *tr(const char *text) { return dgettext("rungic-shared",text); }

struct Source {
    pw_main_loop *loop=nullptr;
    pw_context *context=nullptr;
    pw_core *core=nullptr;
    pw_stream *stream=nullptr;
    spa_source *timer=nullptr;
    spa_hook listener{};
    std::string id, facing, error;
    int width=0,height=0,rotation=0,out_width=0,out_height=0;
    std::atomic<bool> wanted{false},exiting{false};
    std::atomic<int> fd{-1};
    std::atomic<unsigned> generation{0};
    std::mutex mutex;
    std::condition_variable wake;
    std::vector<uint8_t> frame;
    uint64_t received=0,sent=0;
    int64_t pts=0;
    std::thread worker;
};

static void read_exact(int fd,void *buffer,size_t size) {
    auto *p=static_cast<uint8_t*>(buffer);
    while(size) {
        ssize_t n=recv(fd,p,size,0);
        if(n<0 && errno==EINTR)continue;
        if(n<=0)throw std::runtime_error("Android camera stream closed");
        p+=n;size-=n;
    }
}
static void write_exact(int fd,const std::string &text) {
    size_t off=0;
    while(off<text.size()) {
        ssize_t n=send(fd,text.data()+off,text.size()-off,MSG_NOSIGNAL);
        if(n<0 && errno==EINTR)continue;
        if(n<=0)throw std::runtime_error("Cannot request Android camera");
        off+=n;
    }
}
static int on_failure(spa_loop *,bool,uint32_t,const void *,size_t,void *userdata) {
    auto *s=static_cast<Source*>(userdata);
    if(!s->exiting) {
        pw_stream_set_error(s->stream,-EIO,"Android camera unavailable; return to Linux and check camera permission");
        pw_main_loop_quit(s->loop);
    }
    return 0;
}
static void capture(Source *s) {
    while(!s->exiting) {
        {
            std::unique_lock<std::mutex> lock(s->mutex);
            s->wake.wait(lock,[&]{return s->wanted || s->exiting;});
        }
        if(s->exiting)break;
        unsigned generation=s->generation;
        int fd=-1;
        try {
            fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);
            if(fd<0)throw std::runtime_error("Cannot create camera socket");
            s->fd=fd;
            sockaddr_un address{};address.sun_family=AF_UNIX;
            // The media backend's capture socket (docs/117), else the app's own (an app from before
            // it); another one only for a stand-in (tools/system/tests).
            const char *path=getenv("RUNGIC_CAPTURE_SOCKET");
            if(!path || !*path)path=access("/var/lib/rungic-host/media/capture.sock",F_OK)==0
                ?"/var/lib/rungic-host/media/capture.sock":"/mnt/android-wayland/capture.sock";
            if(strlen(path)>=sizeof(address.sun_path))throw std::runtime_error("Camera socket path too long");
            strcpy(address.sun_path,path);
            timeval timeout{50,0};setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
            if(connect(fd,reinterpret_cast<sockaddr*>(&address),sizeof(address)))throw std::runtime_error("Android camera backend unavailable");
            JsonBuilder *builder=json_builder_new();json_builder_begin_object(builder);
            json_builder_set_member_name(builder,"op");json_builder_add_string_value(builder,"camera");
            json_builder_set_member_name(builder,"id");json_builder_add_string_value(builder,s->id.c_str());json_builder_end_object(builder);
            JsonNode *node=json_builder_get_root(builder);JsonGenerator *generator=json_generator_new();json_generator_set_root(generator,node);
            char *text=json_generator_to_data(generator,nullptr);std::string request(text);request+='\n';
            g_free(text);json_node_free(node);g_object_unref(generator);g_object_unref(builder);
            write_exact(fd,request);
            std::string header;char c;
            while(header.size()<4096) { read_exact(fd,&c,1);if(c=='\n')break;header+=c; }
            JsonParser *parser=json_parser_new();GError *err=nullptr;
            bool ok=json_parser_load_from_data(parser,header.c_str(),header.size(),&err);
            JsonNode *root=ok?json_parser_get_root(parser):nullptr;
            JsonObject *object=root && JSON_NODE_HOLDS_OBJECT(root)?json_node_get_object(root):nullptr;
            ok=object && json_object_has_member(object,"ok") && json_object_get_boolean_member(object,"ok");
            g_clear_error(&err);g_object_unref(parser);
            if(!ok)throw std::runtime_error("Android camera permission or availability rejected");
            timeout={4,0};setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
            size_t pixels=static_cast<size_t>(s->width)*s->height;
            std::vector<uint8_t> planar(pixels*3/2),rotated(pixels*3/2),rgb(pixels*4);
            std::vector<uint8_t> planes[3];
            while(s->wanted && !s->exiting && generation==s->generation) {
                uint32_t h[16];read_exact(fd,h,sizeof(h));
                for(int i=0;i<14;i++)h[i]=ntohl(h[i]);
                if(h[0]!=0x4d43414d || h[1]!=1 || h[2]!=static_cast<unsigned>(s->width) || h[3]!=static_cast<unsigned>(s->height) || h[4]!=static_cast<unsigned>(s->rotation))throw std::runtime_error("Camera frame format changed");
                if(h[8]!=1 || (h[9]!=1 && h[9]!=2) || h[9]!=h[10])throw std::runtime_error("Unsupported camera plane layout");
                for(int i=0;i<3;i++) {
                    size_t w=i?s->width/2:s->width,height=i?s->height/2:s->height;
                    size_t row=h[5+i],step=h[8+i],size=h[11+i];
                    if(row<(w-1)*step+1 || row>pixels*2 || size>pixels*2 || size<row*(height-1)+(w-1)*step+1)throw std::runtime_error("Invalid camera plane bounds");
                    planes[i].resize(size);read_exact(fd,planes[i].data(),size);
                }
                uint8_t *y=planar.data(),*u=y+pixels,*v=u+pixels/4;
                if(libyuv::Android420ToI420(planes[0].data(),h[5],planes[1].data(),h[6],planes[2].data(),h[7],h[9],
                                          y,s->width,u,s->width/2,v,s->width/2,s->width,s->height))throw std::runtime_error("Camera conversion failed");
                uint8_t *ry=rotated.data(),*ru=ry+pixels,*rv=ru+pixels/4;
                if(libyuv::I420Rotate(y,s->width,u,s->width/2,v,s->width/2,ry,s->out_width,ru,s->out_width/2,rv,s->out_width/2,
                                    s->width,s->height,static_cast<libyuv::RotationMode>(s->rotation)))throw std::runtime_error("Camera rotation failed");
                // Camera2 YUV's documented default is JPEG/full-range Rec.601.
                if(libyuv::J420ToARGB(ry,s->out_width,ru,s->out_width/2,rv,s->out_width/2,rgb.data(),s->out_width*4,
                                     s->out_width,s->out_height))throw std::runtime_error("Camera RGB conversion failed");
                {
                    std::lock_guard<std::mutex> lock(s->mutex);
                    timespec timestamp{};clock_gettime(CLOCK_MONOTONIC,&timestamp);
                    s->pts=static_cast<int64_t>(timestamp.tv_sec)*1000000000+timestamp.tv_nsec;
                    s->frame.swap(rgb);s->received++;
                    rgb.resize(pixels*4);
                }
            }
        } catch(const std::exception &e) {
            if(s->wanted && !s->exiting && generation==s->generation) {
                fprintf(stderr,"camera %s: %s\n",s->id.c_str(),e.what());
                pw_loop_invoke(pw_main_loop_get_loop(s->loop),on_failure,0,nullptr,0,false,s);
                s->wanted=false;
            }
        }
        s->fd=-1;if(fd>=0)close(fd);
    }
}
static void on_process(void *userdata) {
    auto *s=static_cast<Source*>(userdata);
    pw_buffer *b=pw_stream_dequeue_buffer(s->stream);if(!b)return;
    spa_buffer *buffer=b->buffer;
    if(!buffer->n_datas || !buffer->datas[0].data) { pw_stream_queue_buffer(s->stream,b);return; }
    spa_data &data=buffer->datas[0];
    int64_t pts=0;
    data.chunk->offset=0;data.chunk->stride=s->out_width*4;data.chunk->size=0;data.chunk->flags=SPA_CHUNK_FLAG_EMPTY;
    {
        std::lock_guard<std::mutex> lock(s->mutex);
        if(s->received>s->sent && s->frame.size()<=data.maxsize) {
            memcpy(data.data,s->frame.data(),s->frame.size());data.chunk->size=s->frame.size();data.chunk->flags=0;s->sent=s->received;pts=s->pts;
        }
    }
    auto *header=static_cast<spa_meta_header*>(spa_buffer_find_meta_data(buffer,SPA_META_Header,sizeof(spa_meta_header)));
    if(header) { header->pts=pts;header->flags=0;header->seq=s->sent;header->dts_offset=0; }
    pw_stream_queue_buffer(s->stream,b);
}
static void tick(void *userdata,uint64_t) {
    auto *s=static_cast<Source*>(userdata);
    bool have_frame;
    { std::lock_guard<std::mutex> lock(s->mutex);have_frame=s->received>s->sent; }
    if(have_frame && s->wanted)pw_stream_trigger_process(s->stream);
}
static void state_changed(void *userdata,pw_stream_state old,pw_stream_state state,const char *error) {
    auto *s=static_cast<Source*>(userdata);
    if(state==PW_STREAM_STATE_STREAMING) {
        s->generation++;s->wanted=true;s->wake.notify_one();
        timespec now{0,1},period{0,33333333};
        pw_loop_update_timer(pw_main_loop_get_loop(s->loop),s->timer,&now,&period,false);
        fprintf(stderr,"camera %s: consumer started\n",s->id.c_str());
    } else if(state==PW_STREAM_STATE_PAUSED || state==PW_STREAM_STATE_UNCONNECTED || state==PW_STREAM_STATE_ERROR) {
        s->wanted=false;s->generation++;
        int fd=s->fd;if(fd>=0)shutdown(fd,SHUT_RDWR);
        pw_loop_update_timer(pw_main_loop_get_loop(s->loop),s->timer,nullptr,nullptr,false);
        if(old==PW_STREAM_STATE_STREAMING)fprintf(stderr,"camera %s: consumer stopped\n",s->id.c_str());
        if(state==PW_STREAM_STATE_ERROR)pw_main_loop_quit(s->loop);
    }
}
static void param_changed(void *userdata,uint32_t id,const spa_pod *param) {
    auto *s=static_cast<Source*>(userdata);
    if(!param || id!=SPA_PARAM_Format)return;
    spa_video_info_raw format{};
    if(spa_format_video_raw_parse(param,&format)<0 || format.format!=SPA_VIDEO_FORMAT_BGRA ||
       format.size.width!=static_cast<unsigned>(s->out_width) || format.size.height!=static_cast<unsigned>(s->out_height)) {
        pw_stream_set_error(s->stream,-EINVAL,"Unsupported Android camera format");return;
    }
    uint8_t bytes[1024];spa_pod_builder builder=SPA_POD_BUILDER_INIT(bytes,sizeof(bytes));
    const spa_pod *params[2];
    params[0]=static_cast<const spa_pod*>(spa_pod_builder_add_object(&builder,SPA_TYPE_OBJECT_ParamBuffers,SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers,SPA_POD_CHOICE_RANGE_Int(4,2,8),SPA_PARAM_BUFFERS_blocks,SPA_POD_Int(1),
        SPA_PARAM_BUFFERS_size,SPA_POD_Int(s->out_width*s->out_height*4),SPA_PARAM_BUFFERS_stride,SPA_POD_Int(s->out_width*4)));
    params[1]=static_cast<const spa_pod*>(spa_pod_builder_add_object(&builder,SPA_TYPE_OBJECT_ParamMeta,SPA_PARAM_Meta,
        SPA_PARAM_META_type,SPA_POD_Id(SPA_META_Header),SPA_PARAM_META_size,SPA_POD_Int(sizeof(spa_meta_header))));
    pw_stream_update_params(s->stream,params,2);
}
static void stop(void *userdata,int) { pw_main_loop_quit(static_cast<Source*>(userdata)->loop); }
int main(int argc,char **argv) {
    if(argc!=6)return 2;
    Source s;s.id=argv[1];s.width=atoi(argv[2]);s.height=atoi(argv[3]);s.rotation=atoi(argv[4]);s.facing=argv[5];
    if(s.id.size()>64 || s.width<320 || s.width>1920 || s.height<240 || s.height>1080 || s.width%2 || s.height%2 ||
       (s.rotation!=0 && s.rotation!=90 && s.rotation!=180 && s.rotation!=270))return 2;
    s.out_width=s.rotation%180?s.height:s.width;s.out_height=s.rotation%180?s.width:s.height;
    signal(SIGPIPE,SIG_IGN);pw_init(&argc,&argv);
    // Only the message language: numbers keep the C locale. UTF-8 whatever LC_CTYPE says.
    setlocale(LC_MESSAGES,"");bindtextdomain("rungic-shared","/usr/share/locale");bind_textdomain_codeset("rungic-shared","UTF-8");
    s.loop=pw_main_loop_new(nullptr);s.context=pw_context_new(pw_main_loop_get_loop(s.loop),nullptr,0);
    s.core=pw_context_connect(s.context,nullptr,0);if(!s.core)return 1;
    s.timer=pw_loop_add_timer(pw_main_loop_get_loop(s.loop),tick,&s);
    pw_loop_add_signal(pw_main_loop_get_loop(s.loop),SIGINT,stop,&s);pw_loop_add_signal(pw_main_loop_get_loop(s.loop),SIGTERM,stop,&s);
    std::string name="rungic.camera."+s.id;
    s.stream=pw_stream_new(s.core,name.c_str(),pw_properties_new(PW_KEY_MEDIA_CLASS,"Video/Source",
        PW_KEY_MEDIA_ROLE,"Camera",PW_KEY_NODE_NAME,name.c_str(),PW_KEY_NODE_DESCRIPTION,s.facing=="front"?tr("Android Front Camera"):tr("Android Rear Camera"),
        PW_KEY_NODE_SUPPORTS_REQUEST,"1",PW_KEY_NODE_VIRTUAL,"true", "device.api","rungic-android", "api.libcamera.location",s.facing.c_str(),NULL));
    static const pw_stream_events events={.version=PW_VERSION_STREAM_EVENTS,.state_changed=state_changed,.param_changed=param_changed,.process=on_process};
    pw_stream_add_listener(s.stream,&s.listener,&events,&s);
    uint8_t bytes[1024];spa_pod_builder builder=SPA_POD_BUILDER_INIT(bytes,sizeof(bytes));
    spa_rectangle size=SPA_RECTANGLE(static_cast<uint32_t>(s.out_width),static_cast<uint32_t>(s.out_height));spa_fraction fps=SPA_FRACTION(30,1);
    const spa_pod *format=static_cast<const spa_pod*>(spa_pod_builder_add_object(&builder,SPA_TYPE_OBJECT_Format,SPA_PARAM_EnumFormat,
        SPA_FORMAT_mediaType,SPA_POD_Id(SPA_MEDIA_TYPE_video),SPA_FORMAT_mediaSubtype,SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format,SPA_POD_Id(SPA_VIDEO_FORMAT_BGRA),SPA_FORMAT_VIDEO_size,SPA_POD_Rectangle(&size),
        SPA_FORMAT_VIDEO_framerate,SPA_POD_Fraction(&fps)));
    s.worker=std::thread(capture,&s);
    int result=pw_stream_connect(s.stream,PW_DIRECTION_OUTPUT,PW_ID_ANY,static_cast<pw_stream_flags>(PW_STREAM_FLAG_DRIVER|PW_STREAM_FLAG_MAP_BUFFERS),&format,1);
    if(result>=0)pw_main_loop_run(s.loop);
    s.exiting=true;s.wanted=false;s.wake.notify_all();int fd=s.fd;if(fd>=0)shutdown(fd,SHUT_RDWR);s.worker.join();
    pw_stream_destroy(s.stream);pw_context_destroy(s.context);pw_main_loop_destroy(s.loop);pw_deinit();return result<0?1:0;
}
