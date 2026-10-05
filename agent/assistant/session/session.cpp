// SPDX-License-Identifier: GPL-2.0-or-later
#include "session.h"
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QNetworkProxy>
#include <QNetworkRequest>
#include <QRegularExpression>
#include <QSaveFile>
#include <QUuid>
#include <algorithm>
#include <signal.h>

static QString uuid(){return QUuid::createUuid().toString(QUuid::Id128);}
static QByteArray startTime(qint64 pid){QFile f(QString("/proc/%1/stat").arg(pid));if(!f.open(QIODevice::ReadOnly))return {};auto d=f.readAll();return d.mid(d.lastIndexOf(')')+2).split(' ').value(19);}
static QJsonObject fn(QString name,QString description,QJsonObject properties,QStringList required){QJsonArray r;for(auto k:required)r.append(k);return {{"type","function"},{"name",name},{"description",description},{"parameters",QJsonObject{{"type","object"},{"properties",properties},{"required",r},{"additionalProperties",false}}}};}
Session::Session(QObject *parent):QObject(parent),audio(this){
    clock.start();journal=QDir::homePath()+"/.local/share/rungic-voice-agent/phone-tasks.json";
    leases=QString::fromLocal8Bit(qgetenv("XDG_RUNTIME_DIR"))+"/rungic-task-leases";
    QDir().mkpath(leases);QFile::setPermissions(leases,QFileDevice::ReadOwner|QFileDevice::WriteOwner|QFileDevice::ExeOwner);
    processStart=QString::fromLatin1(startTime(QCoreApplication::applicationPid()));restore();
    timer.setInterval(20);connect(&timer,&QTimer::timeout,this,[this]{tick();});timer.start();
    connect(&ws,&QWebSocket::connected,this,[this]{connected=true;phase="connecting";state();});
    connect(&ws,&QWebSocket::textMessageReceived,this,[this](QString text){incoming(QJsonDocument::fromJson(text.toUtf8()).object());});
    connect(&ws,&QWebSocket::disconnected,this,[this]{if(!id.isEmpty())stop("Voice connection ended; tap to resume");});
    connect(&ws,&QWebSocket::errorOccurred,this,[this](QAbstractSocket::SocketError){if(!id.isEmpty())stop("Voice connection failed; tap to resume");});
    audio.ready=[this]{
        // Opened again after an interruption: the reply goes on from what is still to be played.
        if(phase=="reconnecting"){playStart=audio.written;playedSamples=0;phase="connecting";}
        if(configured&&!inputBlocked){phase="connected";}
        state();
    };
    audio.failed=[this](QString reason){stop(reason);};
    // The call's audio broke and is being opened again (Audio::recover): the call goes on, the bar says
    // "Reconnecting". A pending interruption is reported to the model as what was pushed.
    audio.interrupted=[this](QString){
        if(id.isEmpty())return;
        if(!truncateItem.isEmpty()&&configured)
            send({{"type","conversation.item.truncate"},{"item_id",truncateItem},{"content_index",0},{"audio_end_ms",double(truncateSamples*1000/24000)}});
        truncateItem.clear();phase="reconnecting";state();
    };
    // Hung up by the platform (on Android its call notification or a headset): as the app's hang-up (docs/101).
    audio.hungUp=[this]{stop();};
    audio.microphone=[this](const QByteArray &data){
        if(!configured||muted||inputBlocked||id.isEmpty())return;
        // The network behind for a moment: this piece of live audio is dropped, the call goes on.
        if(ws.bytesToWrite()>32768){if(!micDropped)qWarning()<<"network behind: dropping microphone audio";micDropped=true;return;}
        micDropped=false;
        send({{"type","input_audio_buffer.append"},{"audio",QString::fromLatin1(data.toBase64())}});
    };
    audio.speech=[this](bool value){onSpeech(value);};
    audio.flushed=[this](quint64 oldPlayed,quint64){
        if(!truncateItem.isEmpty()&&configured){
            const quint64 frames=oldPlayed>truncateStart?oldPlayed-truncateStart:0;
            send({{"type","conversation.item.truncate"},{"item_id",truncateItem},{"content_index",0},{"audio_end_ms",double(std::min<quint64>(truncateSamples*1000/24000,frames*1000/48000))}});
        }
        truncateItem.clear();playedSamples=0;state();
    };
}
void Session::event(QJsonObject o,bool keep){if(!o.contains("conversation"))o["conversation"]=conversation;o["time"]=double(QDateTime::currentMSecsSinceEpoch())/1000;if(output)output({{"type","event"},{"event",o},{"keep",keep}});}
// Said, the reply not begun: the words are being taken in (spoken, not yet submitted) or a reply was
// asked for and has not started. The app's call bar shows it as "Thinking".
bool Session::thinking() const{
    if(id.isEmpty()||localSpeech||serverSpeech||responseActive||!playback.pending.isEmpty())return false;
    return (!submitted&&!utterance.isEmpty())||!expected.isEmpty();
}
// What the app's call bar and panel show, as the live event and as the snapshot (the app reopened).
QJsonObject Session::fields() const{
    return {{"sessionId",id},{"conversation",conversation},{"phase",phase},{"microphone",configured&&audio.opened&&!muted},{"muted",muted},
            {"speaking",!playback.pending.isEmpty()||responseActive},{"listening",localSpeech},{"thinking",thinking()},
            {"startedAt",double(started)},{"focusedTask",tasks.focused},{"tasks",tasks.snapshot()}};
}
void Session::state(){auto o=fields();o["type"]="phone-state";event(o,false);}
void Session::receive(QJsonObject o){
    const auto type=o["type"].toString();
    if(type=="rpc-result") {auto f=callbacks.take(o["id"].toInt());if(f)f(o);}
    else if(type=="request")question(o);
    else if(type=="notification")notification(o["method"].toString(),o["params"].toObject());
    else if(type=="command")command(o["method"].toString(),o["args"].toObject(),[this,o](QJsonObject r){if(output)output({{"type","reply"},{"id",o["id"]},{"result",r}});});
}
void Session::command(QString method,QJsonObject args,std::function<void(QJsonObject)> done){
    if(method=="BackendReset"){
        stop("Task connection changed; tap to resume voice");
        for(auto &t:tasks.rows)if(!t.terminal()){lease(t,false);t.status="interrupted";changed(t.id);}
        done({{"ok",true}});
    } else if(method=="StartPhoneMode"){
        QString target=args["conversationId"].toString();if(target.isEmpty()){done({{"error","A conversation is required"}});return;}
        if(!id.isEmpty()){if(target==conversation){done({{"sessionId",id}});return;}done({{"error","Another conversation owns the phone session"}});return;}
        prompt=args["instructions"].toString();
        if(prompt.isEmpty()){done({{"error","Phone mode instructions unavailable"}});return;}
        start(target,args["language"].toString());done(id.isEmpty()?QJsonObject{{"error","Voice could not start"}}:QJsonObject{{"sessionId",id}});
    } else if(method=="StopPhoneMode"){
        if(args["sessionId"].toString()!=id){done({{"error","Stale phone session"}});return;}stop();done({{"ok",true}});
    } else if(method=="SetPhoneMuted"){
        if(args["sessionId"].toString()!=id||id.isEmpty()){done({{"error","Stale phone session"}});return;}
        muted=args["muted"].toBool();if(!muted){inputBlocked=true;lastVoice=clock.elapsed();phase="connecting";}
        if(muted){if(!submitted)++generation;localSpeech=serverSpeech=false;inputItems.clear();transcripts.clear();utterance.clear();commitPending=false;submitted=true;send({{"type","input_audio_buffer.clear"}});}
        audio.mute(muted);state();done({{"ok",true}});
    } else if(method=="StopSpeaking"){
        if(args["sessionId"].toString()!=id){done({{"error","Stale phone session"}});return;}narrationSuppressed=true;stopSpeaking();done({{"ok",true}});
    } else if(method=="StopTaskById"){
        auto *t=tasks.target(args["taskId"].toString());if(!t){done({{"error","Choose a task to stop"}});return;}auto tid=t->id;stopTask(tid);done({{"ok",true},{"taskId",tid}});
    } else if(method=="AnswerTask"){done(answer(args["taskId"].toString(),args["answers"].toObject()));
    } else if(method=="FocusTask"){
        auto *t=tasks.find(args["taskId"].toString());if(!t){done({{"error","Unknown task"}});return;}tasks.focused=t->id;state();done({{"ok",true}});
    } else if(method=="PhoneSnapshot"){done(fields());}
    // A call goes on with Plasma hidden or the screen locked, as a phone call does: hanging up ends
    // it (2026-10-05; it used to end here, "Voice paused while Plasma is hidden", docs/101).
    else if(method=="Foreground") {done({{"ok",true}});}
    // The task's progress as the push-to-talk turn has it (TurnState.facts, phone_session.py):
    // part of the trusted snapshot, so "how far is it" is answered from it.
    else if(method=="TaskFacts"){
        auto *t=tasks.find(args["taskId"].toString());if(t&&!t->terminal())t->facts=args["facts"].toString().left(1200);
        // The voice's snapshot follows every 5 s at most; a task's own change sends it at once.
        if(t&&configured&&clock.elapsed()-lastFacts>5000){lastFacts=clock.elapsed();
            send({{"type","session.update"},{"session",QJsonObject{{"type","realtime"},{"instructions",prompt+"\nTrusted task snapshot:\n"+QString::fromUtf8(QJsonDocument(trusted()).toJson(QJsonDocument::Compact))}}}});}
        done({{"ok",true}});
    }
    // Progress for the voice to say, from the same progress rules as push-to-talk's
    // (VoiceAgent.progress_tick): one sentence, told as the instruction says.
    else if(method=="Narrate"){
        // Not over the voice's own words: an update waits for 12 s of quiet after it spoke (the
        // updates came every few seconds over its acknowledgements, 2026-10-05).
        const bool quiet=!responseActive&&playback.pending.isEmpty()&&clock.elapsed()-lastPlaybackPush>12000;
        if(configured&&!id.isEmpty()&&!localSpeech&&!narrationSuppressed&&quiet)requestReply({args["text"].toString(),{},generation,true},"Say one short sentence to the user, as the message asks. Do not start, steer or stop any task.");
        done({{"ok",true}});
    }
    else if(method=="ExternalBusy"){externalBusy=args["busy"].toBool();if(!externalBusy)runQueue();done({{"ok",true}});}
    else if(method=="SendPhoneText"){
        if(id.isEmpty()||args["conversationId"].toString()!=conversation){done({{"error","Text belongs to another conversation"}});return;}
        QString text=args["text"].toString().trimmed();if(text.isEmpty()){done({{"ok",true}});return;}lastUserText=text;
        stopSpeaking();++generation;utterance=uuid();submitted=true;localSpeech=serverSpeech=false;inputItems.clear();transcripts.clear();send({{"type","input_audio_buffer.clear"}});
        QJsonObject item{{"type","message"},{"role","user"},{"content",QJsonArray{QJsonObject{{"type","input_text"},{"text",text}}}}};
        send({{"type","conversation.item.create"},{"item",item}});
        event({{"type","message"},{"id",utterance},{"role","user"},{"text",text}});lastUser=clock.elapsed();requestReply({text,utterance,generation,false});done({{"ok",true}});
    } else if(method=="SubmitTask"){
        auto target=args["conversationId"].toString();auto input=args["input"].toArray();
        if(target.isEmpty()||input.isEmpty()){done({{"error","A conversation and input are required"}});return;}
        const auto tid=tasks.add(args["text"].toString(),false,target,"typed:"+uuid());
        if(tid.isEmpty()){done({{"error","The task queue is full; finish or stop a task first"}});return;}
        tasks.find(tid)->input=input;changed(tid);runQueue();done({{"taskId",tid}});
    } else if(method=="Reconcile"){
        for(const auto &t:tasks.rows)if(t.status=="interrupted"&&!t.thread.isEmpty()){
            const auto tid=t.id;rpc("thread/read",{{"threadId",t.thread},{"includeTurns",true}},[this,tid](QJsonObject o){
                auto *t=tasks.find(tid);if(!t)return;auto thread=o["result"].toObject()["thread"].toObject();auto turns=thread["turns"].toArray();
                if(turns.isEmpty()){changed(tid);return;}auto turn=turns.last().toObject();auto status=turn["status"].toString();
                if(status=="inProgress"){
                    t->turn=turn["id"].toString();t->status="running";rpc("thread/resume",{{"threadId",t->thread},{"taskId",t->id},{"readOnly",t->readOnly}});lease(*t,true);
                }else if(status=="completed")t->status="completed";else if(status=="interrupted")t->status="stopped";else t->status="failed";
                changed(tid);
            });
        }
        done({{"ok",true}});
    } else done({{"error","Unknown phone command"}});
}
void Session::start(QString target,QString lang){
    QFile keyFile(QDir::homePath()+"/.config/rungic-voice-agent/openai-api-key");
    if(!keyFile.open(QIODevice::ReadOnly)||keyFile.size()>8192){event({{"type","error"},{"conversation",target},{"text","Set the OpenAI API key in Agent settings"}});return;}
    auto key=keyFile.readAll().trimmed();if(key.isEmpty())return;
    conversation=target;language=lang;id="voice_"+uuid();phase="connecting";muted=false;configured=false;connected=false;submitted=true;localSpeech=serverSpeech=false;narrationSuppressed=false;inputBlocked=false;lastVoice=-10000;acknowledgements.clear();
    playback=ReplyBuffer();responses.clear();expected.clear();deferred.clear();assistantText.clear();truncateItem.clear();inputItems.clear();transcripts.clear();utterance.clear();++generation;
    QString proxy=QString::fromLocal8Bit(qgetenv("https_proxy"));if(proxy.isEmpty())proxy=QString::fromLocal8Bit(qgetenv("HTTPS_PROXY"));
    if(proxy.isEmpty())proxy=QString::fromLocal8Bit(qgetenv("http_proxy"));
    if(!proxy.isEmpty()){QUrl u(proxy);auto type=u.scheme().startsWith("socks")?QNetworkProxy::Socks5Proxy:QNetworkProxy::HttpProxy;ws.setProxy(QNetworkProxy(type,u.host(),u.port(type==QNetworkProxy::Socks5Proxy?1080:8080),u.userName(),u.password()));}
    else ws.setProxy(QNetworkProxy::NoProxy);
    QNetworkRequest req(QUrl("wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1-mini"));req.setRawHeader("Authorization","Bearer "+key);key.fill(0);
    started=QDateTime::currentSecsSinceEpoch();ws.open(req);audio.start(id);lastUser=clock.elapsed();state();
    const auto current=id;QTimer::singleShot(25000,this,[this,current]{if(id==current&&(!configured||!audio.opened))stop("Voice connection timed out; tap to retry");});
}
void Session::stop(QString reason){
    const auto old=id;if(old.isEmpty())return;
    // The call's summary, kept in its conversation: how long it was and what was started in it
    // (the tasks of this conversation created since it began), as the app shows it after the call.
    if(started>0){
        QJsonArray work;for(const auto &t:tasks.rows)if(t.conversation==conversation&&t.created>=started)work.append(QJsonObject{{"taskId",t.id},{"text",t.text},{"status",t.status}});
        event({{"type","phone-ended"},{"sessionId",old},{"startedAt",double(started)},{"seconds",double(std::max<qint64>(0,QDateTime::currentSecsSinceEpoch()-started))},{"tasks",work},{"reason",reason}});
    }
    started=0;id.clear();configured=connected=false;phase="closed";playback.clear();localSpeech=serverSpeech=false;submitted=true;++generation;
    ws.abort();audio.close();responses.clear();expected.clear();deferred.clear();inputItems.clear();transcripts.clear();truncateItem.clear();utterance.clear();
    if(!reason.isEmpty())event({{"type","phone-notice"},{"text",reason}});state();save();
}
void Session::send(QJsonObject o){if(!connected)return;if(!o.contains("event_id"))o["event_id"]="event_"+uuid();ws.sendTextMessage(QString::fromUtf8(QJsonDocument(o).toJson(QJsonDocument::Compact)));}
void Session::configure(){
    prompt+="\nCurrent system time (UTC): "+QDateTime::currentDateTimeUtc().toString(Qt::ISODate)+". Resolve relative dates in the user's or requested location's timezone.";
    QJsonObject words{{"original_words",QJsonObject{{"type","string"},{"description","The user's original words, with negations, constraints and corrections"}}}};
    QJsonObject target=words;target["task_id"]=QJsonObject{{"type","string"},{"description","Exact taskId from the trusted task snapshot"}};
    auto start=words;start["access"]=QJsonObject{{"type","string"},{"enum",QJsonArray{"read_only","exclusive"}},{"description","read_only for research, web queries and terminal commands that only read, wait or calculate; exclusive for edits, GUI interaction, device control, external writes or uncertain effects"}};
    QJsonArray tools{fn("start_task","Start a clear complete new request; independent tasks may run in parallel. Never execute discussions or hypotheses.",start,{"original_words","access"}),fn("steer_task","Correct or constrain the named running task. This does not stop it.",target,{"task_id","original_words"}),fn("stop_task","Explicitly cancel the named execution task. Speaking, backchannels and negated stop are not cancellation.",{{"task_id",target["task_id"]}},{"task_id"}),fn("task_status","Read the actual status of a named task.",{{"task_id",target["task_id"]}},{"task_id"}),fn("stop_speaking","Stop narration only; leave all tasks running.",{}, {}),fn("answer_task","Answer a pending task question using the user's explicit answer. Never invent an answer or approve a change implicitly.",{{"task_id",target["task_id"]},{"answers",QJsonObject{{"type","object"},{"additionalProperties",QJsonObject{{"type","array"},{"items",QJsonObject{{"type","string"}}}}}}}},{"task_id","answers"})};
    QJsonObject input{{"format",QJsonObject{{"type","audio/pcm"},{"rate",24000}}},{"transcription",QJsonObject{{"model","gpt-4o-mini-transcribe"}}},{"turn_detection",QJsonObject{{"type","semantic_vad"},{"eagerness","medium"},{"create_response",false},{"interrupt_response",true}}}};
    if(QRegularExpression("^[a-z]{2}$").match(language).hasMatch()){auto t=input["transcription"].toObject();t["language"]=language;input["transcription"]=t;}
    QJsonObject out{{"format",QJsonObject{{"type","audio/pcm"},{"rate",24000}}},{"voice","marin"}};
    QJsonObject config{{"type","realtime"},{"instructions",prompt+"\nTrusted task snapshot:\n"+QString::fromUtf8(QJsonDocument(trusted()).toJson(QJsonDocument::Compact))},{"output_modalities",QJsonArray{"audio"}},{"tools",tools},{"tool_choice","auto"},{"audio",QJsonObject{{"input",input},{"output",out}}}};
    send({{"type","session.update"},{"session",config}});
}
void Session::onSpeech(bool value){
    if(muted)return;localSpeech=value;if(value)lastVoice=clock.elapsed();if(!configured||inputBlocked)return;
    if(value){
        narrationSuppressed=false;
        lastVoice=lastUser=clock.elapsed();
        if(submitted||utterance.isEmpty()){++generation;utterance=uuid();inputItems.clear();transcripts.clear();submitted=false;commitPending=false;}
        stopSpeaking();
    }
    state();
}
void Session::incoming(QJsonObject o){
    const auto type=o["type"].toString();
    if(type=="session.created"){configure();return;}
    if(type=="session.updated"){if(!configured){inputBlocked=localSpeech||clock.elapsed()-lastVoice<700;if(inputBlocked)event({{"type","phone-notice"},{"text","The connection is ready; pause briefly and repeat the complete request"}});}configured=true;if(audio.opened&&!inputBlocked&&(muted||(audio.sourceReady&&audio.captureReady)))phase="connected";state();return;}
    if(type=="error"){
        auto e=o["error"].toObject();auto code=e["code"].toString();
        if(code.contains("cancel_not_active")||code.contains("truncate")||code.contains("commit_empty")){if(code.contains("commit_empty"))commitPending=false;return;}
        // Recovery first (2026-10-05): one refused request (a reply, an update) does not end the call;
        // the reply it was for is given up. Only a session that cannot go on (expired, the key or
        // quota refused) ends it.
        if(code.contains("session_expired")||code.contains("api_key")||code.contains("quota")||code.contains("unauthorized")){
            stop(e["message"].toString("Voice protocol error; tap to resume"));return;
        }
        qWarning().noquote()<<"voice request refused:"<<code<<e["message"].toString();
        if(!expected.isEmpty()&&!responseActive){expected.removeFirst();state();}
        return;
    }
    if(type=="input_audio_buffer.speech_started"){
        if(muted||inputBlocked)return;
        serverSpeech=true;lastUser=clock.elapsed();if(utterance.isEmpty()||submitted){++generation;utterance=uuid();inputItems.clear();transcripts.clear();submitted=false;}
        QString item=o["item_id"].toString();if(!item.isEmpty()&&!inputItems.contains(item))inputItems.append(item);stopSpeaking();state();
    } else if(type=="input_audio_buffer.speech_stopped"){serverSpeech=false;state();}
    else if(type=="input_audio_buffer.committed"){
        commitPending=false;serverSpeech=false;auto item=o["item_id"].toString();if(!submitted&&!item.isEmpty()&&!inputItems.contains(item))inputItems.append(item);
    } else if(type=="conversation.item.input_audio_transcription.completed"){
        const auto item=o["item_id"].toString();if(inputItems.contains(item))transcripts[item]=o["transcript"].toString().trimmed();
    } else if(type=="conversation.item.input_audio_transcription.failed"){
        if(inputItems.contains(o["item_id"].toString())){submitted=true;event({{"type","phone-notice"},{"text","I couldn't understand that; please repeat"}});}
    } else if(type=="response.created"){
        auto responseId=o["response"].toObject()["id"].toString();
        if(expected.isEmpty()){send({{"type","response.cancel"},{"response_id",responseId}});return;}
        auto context=expected.takeFirst();responses[responseId]=context;
        if(context.generation!=generation){send({{"type","response.cancel"},{"response_id",responseId}});return;}
        responseActive=true;state();
    } else if(type=="response.output_audio.delta"){
        auto response=o["response_id"].toString();auto context=responses.value(response);
        if(!responses.contains(response)||context.generation!=generation||localSpeech||narrationSuppressed)return;
        if(playback.response!=response){playStart=audio.written;playedSamples=0;}
        if(!playback.append(response,o["item_id"].toString(),QByteArray::fromBase64(o["delta"].toString().toLatin1())))replyTooLong();
    } else if(type=="response.output_audio_transcript.delta"){
        const auto response=o["response_id"].toString();if(responses.contains(response)&&responses[response].generation==generation){}
    } else if(type=="response.output_audio_transcript.done"){
        const auto response=o["response_id"].toString();if(responses.contains(response)&&responses[response].generation==generation)event({{"type","message"},{"id",o["item_id"]},{"role","assistant"},{"text",o["transcript"]}});
    } else if(type=="response.function_call_arguments.done"){
        tool(o["name"].toString(),QJsonDocument::fromJson(o["arguments"].toString().toUtf8()).object(),o["call_id"].toString(),o["response_id"].toString());
    } else if(type=="response.cancelled"){
        const auto response=o["response_id"].toString(o["response"].toObject()["id"].toString());if(response==playback.response&&!playback.full)stopSpeaking();
    } else if(type=="response.done"){
        const auto r=o["response"].toObject();const auto response=r["id"].toString();
        if(responses.contains(response)&&responses[response].generation==generation){responseActive=false;if(r["status"]=="cancelled"&&response==playback.response&&!playback.full)stopSpeaking();state();}
        responses.remove(response);
    }
}
void Session::replyTooLong(){
    // A reply too long to buffer is cut where its buffered audio ends: that part still plays, the
    // model stops generating and its record of the reply ends there too (what the user hears), and
    // the conversation goes on. Stopping the session here ended conversations (2026-10-05).
    const qint64 heardMs=(qint64(playedSamples)+playback.pending.size()/2)*1000/24000;
    send({{"type","conversation.item.truncate"},{"item_id",playback.item},{"content_index",0},{"audio_end_ms",heardMs}});
    if(responseActive)send({{"type","response.cancel"},{"response_id",playback.response}});
    qWarning().noquote()<<"reply audio past"<<ReplyBuffer::MaxPendingBytes/48000<<"s ahead of playback: cut at"<<heardMs<<"ms";
}
void Session::stopSpeaking(){
    if(!playback.item.isEmpty()&&(!playback.pending.isEmpty()||audio.player)){
        if(audio.flushing){
            // This newer response has not reached playback while the previous
            // generation is being flushed. Its heard duration is exactly zero.
            send({{"type","conversation.item.truncate"},{"item_id",playback.item},{"content_index",0},{"audio_end_ms",0}});
        } else {
            truncateItem=playback.item;truncateStart=playStart;truncateSamples=playedSamples;audio.stopPlayback();
        }
    }
    playback.clear();
    if(responseActive){send({{"type","response.cancel"}});responseActive=false;}
}

int Session::chunksDue(quint64 pushed,quint64 played,quint64 start){
    constexpr qint64 Ahead=48000*300/1000,Chunk=960;     // 48 kHz frames: 300 ms; a chunk is 20 ms
    const qint64 ahead=qint64(pushed)*2-qint64(played>start?played-start:0);
    return ahead>=Ahead?0:int(std::min<qint64>(8,(Ahead-ahead+Chunk-1)/Chunk));
}
void Session::tick(){
    const auto now=clock.elapsed();
    if(localSpeech)lastVoice=now;
    if(configured&&inputBlocked&&!localSpeech&&now-lastVoice>=700){inputBlocked=false;send({{"type","input_audio_buffer.clear"}});if(audio.opened&&(muted||(audio.sourceReady&&audio.captureReady)))phase="connected";state();}
    if(configured&&audio.opened&&!id.isEmpty()){
        if(localSpeech)lastVoice=now;
        if(!submitted&&!localSpeech&&!muted&&!utterance.isEmpty()){
            if(serverSpeech&&!commitPending&&now-lastVoice>=2000){commitPending=true;send({{"type","input_audio_buffer.commit"}});}
            bool complete=!inputItems.isEmpty();QStringList parts;
            for(const auto &item:inputItems){if(!transcripts.contains(item)){complete=false;break;}parts.append(transcripts[item]);}
            if(complete&&!serverSpeech&&!commitPending&&now-lastVoice>=700){
                submitted=true;QString text=parts.join(" ").trimmed();
                if(!text.isEmpty()){lastUserText=text;event({{"type","message"},{"id",utterance},{"role","user"},{"text",text}});requestReply({text,utterance,generation,false});}
            } else if(now-lastVoice>10000){
                // Not completed in time (a transcript late or lost, the end of speech not heard): what
                // was heard is acted on, not dropped (a request for Ardour music was lost so,
                // 2026-10-05). Only nothing heard at all asks to repeat.
                submitted=true;QStringList heard;for(const auto &item:inputItems)if(transcripts.contains(item)&&!transcripts[item].isEmpty())heard.append(transcripts[item]);
                const QString text=heard.join(" ").trimmed();
                if(!text.isEmpty()){lastUserText=text;event({{"type","message"},{"id",utterance},{"role","user"},{"text",text}});requestReply({text,utterance,generation,false});}
                else event({{"type","phone-notice"},{"text","That utterance was not completed; please repeat"}});
            }
        }
        // The reply is kept ahead of what Android has played of it. Not by Android's own buffer: the
        // communication service keeps that a little ahead with PulseAudio's silence whatever is played,
        // and holding back while it looked full let silence take the reply's place for good (docs/101,
        // 2026-10-04). A late tick is made up in the next: one 20 ms chunk a tick fell behind and stuttered.
        if(!audio.flushing&&!localSpeech)
            for(int n=chunksDue(playedSamples,audio.played,playStart);n>0&&!playback.pending.isEmpty();--n){
                auto chunk=playback.pending.left(960);playback.pending.remove(0,chunk.size());playedSamples+=chunk.size()/2;lastPlaybackPush=now;audio.play(chunk);
            }
        if(!narrationSuppressed&&!notices.isEmpty()&&!localSpeech&&!serverSpeech&&!responseActive&&playback.pending.isEmpty()&&now-lastPlaybackPush>300&&now-lastUser>1500&&now-lastProgress>5000){
            QString text=notices.join("\n");notices.clear();lastProgress=now;requestReply({text,{},generation,true},"Report only these verified task updates in one short sentence. Do not start or modify any task.");
        }
    }
    if(configured&&!narrationSuppressed&&!responseActive&&expected.isEmpty()&&!localSpeech&&playback.pending.isEmpty()&&now-lastPlaybackPush>300&&!acknowledgements.isEmpty()){
        const auto c=acknowledgements.takeLast();acknowledgements.clear();
        if(c.generation==generation)requestReply({c.text,c.utterance,generation,true},"Acknowledge the actual tool result accurately in one short sentence: say that you do it. Do not ask the user anything that the tool result does not ask. Do not call tools.");
    }
    if(configured&&!responseActive&&expected.isEmpty()&&!localSpeech&&!deferred.isEmpty()&&(!deferred.last().progress||(playback.pending.isEmpty()&&now-lastPlaybackPush>300))){
        auto c=deferred.takeLast();deferred.clear();if(c.generation==generation)requestReply(c,c.instruction);
    }
    bool changedAny=false;
    for(auto &t:tasks.rows)if(t.status=="stopping"&&t.backendStopped&&!toolsActive(t)){t.status="stopped";changed(t.id);changedAny=true;}
    if(changedAny)runQueue();
}
void Session::requestReply(ResponseContext context,QString instruction){
    if(!configured||context.generation!=generation)return;
    if(responseActive||!expected.isEmpty()||(context.progress&&(!playback.pending.isEmpty()||clock.elapsed()-lastPlaybackPush<300))){context.instruction=instruction;deferred.append(context);if(deferred.size()>8)deferred.removeFirst();return;}
    expected.append(context);state();
    send({{"type","response.create"},{"response",replyRequest(context,instruction)}});
}
// The language of the user's last words, named for the voice: an update outside the conversation came
// in English and in Korean with only "the user's language" to go by (2026-10-05).
static QString languageOf(const QString &text){
    int han=0,kana=0,hangul=0,latin=0;
    for(const QChar c:text){const auto u=c.unicode();
        if(u>=0x4E00&&u<=0x9FFF)++han;else if(u>=0x3040&&u<=0x30FF)++kana;else if(u>=0xAC00&&u<=0xD7AF)++hangul;else if(c.isLetter()&&u<0x250)++latin;}
    if(hangul>0&&hangul>=han)return "Korean";
    if(kana>0)return "Japanese";
    if(han>0)return "Chinese";
    if(latin>0)return "the language of these words";
    return {};
}
QJsonObject Session::replyRequest(const ResponseContext &context,const QString &instruction) const{
    QJsonObject response;
    const auto language=languageOf(lastUserText);
    if(context.progress){
        response["tool_choice"]="none";
        // A response's instructions replace the session's: the rules stay with it (one assistant, no
        // steps for the user, the user's language). Without them an acknowledgement told the user to
        // choose the TV's input, and an update came in Korean (the G100 S, 2026-10-05).
        response["instructions"]=prompt+"\nTrusted task snapshot:\n"+QString::fromUtf8(QJsonDocument(trusted()).toJson(QJsonDocument::Compact))
            +"\n\n"+instruction+(language.isEmpty()?QString():"\nSpeak "+language+", as the user does.")+"\nVerified updates:\n"+context.text;
        if(context.utterance.isEmpty()){
            // An update of its own (progress, a task's end) is spoken outside the conversation: inside
            // it, it answered a request the user had just made, with no tools ("I can't do that").
            response["conversation"]="none";
            const QString said=lastUserText.isEmpty()?QString():"\nThe user's last words (speak "+(language.isEmpty()?QString("their language"):language)+"): "+lastUserText.left(200);
            response["input"]=QJsonArray{QJsonObject{{"type","message"},{"role","user"},{"content",QJsonArray{QJsonObject{{"type","input_text"},{"text","Give this update to the user now."+said}}}}}};
        }
    }
    return response;
}
void Session::tool(QString name,QJsonObject args,QString callId,QString responseId){
    if(callId.isEmpty())return;const auto context=responses.value(responseId);
    auto result=[this,callId,context](QJsonObject o){
        if(!configured)return;send({{"type","conversation.item.create"},{"item",QJsonObject{{"type","function_call_output"},{"call_id",callId},{"output",QString::fromUtf8(QJsonDocument(o).toJson(QJsonDocument::Compact))}}}});
        // Read the actual tool result once the current model response has finished.
        if(context.generation==generation)acknowledgements.append(context);
    };
    if(!responses.contains(responseId)||context.generation!=generation||context.progress||context.text.isEmpty()){result({{"error","This utterance was interrupted or has no finalized transcript"}});return;}
    const QString raw=context.text;
    if(name=="start_task"){
        const auto tid=tasks.add(raw,args["access"]=="read_only",conversation,id+":"+context.utterance);if(tid.isEmpty()){result({{"error","The task queue is full; finish or stop a task first"}});return;}changed(tid);runQueue();result(tasks.find(tid)->json());
    } else if(name=="stop_speaking"){narrationSuppressed=true;stopSpeaking();result({{"ok",true},{"tasksContinue",true}});}
    else {
        auto *t=tasks.target(args["task_id"].toString());if(!t||t->conversation!=conversation){result({{"error","Choose a task from this conversation"}});return;}QString tid=t->id;
        if(name=="steer_task"||name=="stop_task"){
            if(controlUtterance!=context.utterance){controlUtterance=context.utterance;controlledTask.clear();}
            if(!controlledTask.isEmpty()&&controlledTask!=tid){result({{"error","This utterance already controls another task. Name one task per request."}});return;}
            controlledTask=tid;
        }
        if(name=="answer_task") {
            for(auto q:t->question["questions"].toArray())if(q.toObject()["isSecret"].toBool()){result({{"error","Type secret answers on the task card"}});return;}
            result(answer(tid,args["answers"].toObject()));
        }
        else if(name=="task_status")result(t->json());
        else if(name=="stop_task"){stopTask(tid);result(tasks.find(tid)->json());}
        else if(name=="steer_task"){
            if(steerUtterance!=context.utterance){steerUtterance=context.utterance;steeredTasks.clear();}
            if(steeredTasks.contains(tid)){result({{"correctionAlreadySubmitted",true},{"task",t->json()}});return;}
            if(t->status=="queued"){
                steeredTasks.insert(tid);t->text+='\n'+raw;
                if(!t->input.isEmpty())t->input.append(QJsonObject{{"type","text"},{"text",raw}});
                changed(tid);result(t->json());
            }
            else if(t->status=="running"&&!t->turn.isEmpty()){
                steeredTasks.insert(tid);
                const auto turn=t->turn;rpc("turn/steer",{{"threadId",t->thread},{"expectedTurnId",turn},{"input",QJsonArray{QJsonObject{{"type","text"},{"text",raw}}}}},[this,tid,result,raw](QJsonObject o){
                    if(o.contains("error")){result({{"error","The task changed before this correction was applied. Check its status and start a follow-up task."}});return;}
                    if(auto *t=tasks.find(tid)){t->text+='\n'+raw;changed(tid);result(t->json());}
                });
            } else result({{"error","The task is no longer running. Start a follow-up task with the requested correction."},{"task",t->json()}});
        } else result({{"error","Unknown task tool"}});
    }
}
void Session::rpc(QString method,QJsonObject params,std::function<void(QJsonObject)> done){
    int rid=++requests;if(done)callbacks[rid]=done;
    if(output)output({{"type","rpc"},{"id",rid},{"method",method},{"params",params}});
    if(done)QTimer::singleShot(30000,this,[this,rid]{auto f=callbacks.take(rid);if(f)f({{"error","Task protocol timed out"},{"uncertain",true}});});
}
void Session::lease(Task &t,bool active){
    const auto path=leases+"/"+t.id+".json";
    if(!active||t.readOnly){QFile::remove(path);return;}
    QSaveFile f(path);if(f.open(QIODevice::WriteOnly)){f.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);f.write(QJsonDocument(QJsonObject{{"taskId",t.id},{"exclusive",true},{"pid",double(QCoreApplication::applicationPid())},{"startTime",processStart}}).toJson(QJsonDocument::Compact));f.commit();}
}
bool Session::toolsActive(const Task &t){
    QFile f(leases+"/"+t.id+".json.tools");if(!f.open(QIODevice::ReadOnly))return false;auto o=QJsonDocument::fromJson(f.readAll()).object();auto pid=qint64(o["pid"].toDouble());
    return o["active"].toBool()&&pid>0&&o["startTime"].toString().toLatin1()==startTime(pid);
}
void Session::changed(QString tid){
    // A shared task's card is push-to-talk's turn card (the same thread); a read-only one has its own.
    auto *t=tasks.find(tid);if(!t)return;save();if(!t->shared)event({{"type","phone-task"},{"conversation",t->conversation},{"task",t->json()}});
    if(t->terminal()&&t->conversation==conversation)notices.append(t->text.left(80)+": "+t->status+". "+t->result.left(500));
    if(configured)send({{"type","session.update"},{"session",QJsonObject{{"type","realtime"},{"instructions",prompt+"\nTrusted task snapshot:\n"+QString::fromUtf8(QJsonDocument(trusted()).toJson(QJsonDocument::Compact))}}}});
    state();
}
// Push-to-talk at work no longer holds the call's tasks (ExternalBusy is kept, unused): they join
// its turn on the same thread.
void Session::runQueue(){for(auto tid:tasks.schedule())startTask(tid);}
void Session::startTask(QString tid){
    auto *t=tasks.find(tid);if(!t)return;changed(tid);
    rpc("thread/start",{{"taskId",tid},{"readOnly",t->readOnly},{"conversation",t->conversation}},[this,tid](QJsonObject o){
        auto *t=tasks.find(tid);if(!t)return;
        if(o.contains("error")){t->status=t->cancelRequested?"stopped":"failed";t->result=o["error"].toString();lease(*t,false);changed(tid);runQueue();return;}
        t->shared=o["result"].toObject()["shared"].toBool();
        // A task of its own (no executor) owns its tools by a lease; a shared one uses the
        // conversation's, as push-to-talk does.
        if(!t->shared)lease(*t,true);
        t->thread=o["result"].toObject()["thread"].toObject()["id"].toString();
        if(t->thread.isEmpty()){t->status="failed";t->result="No task thread returned";lease(*t,false);changed(tid);runQueue();return;}
        if(t->cancelRequested){t->backendStopped=true;changed(tid);return;}
        rpc("turn/start",{{"threadId",t->thread},{"taskId",tid},{"input",t->input.isEmpty()?QJsonArray{QJsonObject{{"type","text"},{"text",t->text}}}:t->input}},[this,tid](QJsonObject o){
            auto *t=tasks.find(tid);if(!t)return;
            if(o.contains("error")){
                if(o["uncertain"].toBool()){t->cancelRequested=true;t->status="stopping";t->result="Start acknowledgement lost; awaiting verified task termination";lease(*t,false);if(!t->turn.isEmpty())stopTask(tid);changed(tid);return;}
                t->status=t->cancelRequested?"stopped":"failed";t->result=o["error"].toString();lease(*t,false);changed(tid);runQueue();return;
            }
            t->turn=o["result"].toObject()["turn"].toObject()["id"].toString();
            if(t->status=="starting")t->status="running";
            if(t->cancelRequested)stopTask(tid);changed(tid);
        });
    });
}
void Session::stopTask(QString tid){
    auto *t=tasks.find(tid);if(!t||t->terminal())return;tasks.stop(tid);lease(*t,false);
    if(!t->question.isEmpty()){rpc("ServerResponse",{{"requestId",t->question["requestId"]},{"result",QJsonObject{{"answers",QJsonObject{}}}}});t->question={};}
    changed(tid);
    if(!t->turn.isEmpty())rpc("turn/interrupt",{{"threadId",t->thread},{"turnId",t->turn}},[this,tid](QJsonObject o){if(o.contains("error")){if(auto *t=tasks.find(tid)){t->result="Stop requested; awaiting task completion acknowledgement";changed(tid);}}});
    runQueue();
}
void Session::notification(QString method,QJsonObject p){
    // The conversation's own thread carries several tasks over time, and push-to-talk's turns:
    // each live task follows its own turn (shared tasks take the turn they joined).
    const auto live=tasks.onThread(p["threadId"].toString());
    if(!live.isEmpty()&&live.first()->shared){
        const auto turnId=method=="turn/completed"||method=="turn/started"?p["turn"].toObject()["id"].toString():p["turnId"].toString();
        QStringList ids;for(auto *s:live)if(!s->turn.isEmpty()&&s->turn==turnId)ids.append(s->id);
        for(const auto &id:ids)if(auto *s=tasks.find(id))taskNotification(*s,method,p);
        return;
    }
    auto *t=tasks.byThread(p["threadId"].toString());if(!t)return;
    taskNotification(*t,method,p);
}
void Session::taskNotification(Task &task,QString method,QJsonObject p){
    auto *t=&task;const auto tid=t->id;
    if(method=="turn/started"){
        t->turn=p["turn"].toObject()["id"].toString();if(t->cancelRequested)t->status="stopping";if(t->status=="starting")t->status="running";if(t->cancelRequested)stopTask(tid);else changed(tid);
    } else if(method=="turn/completed"){
        auto turn=p["turn"].toObject();if(!t->turn.isEmpty()&&turn["id"].toString()!=t->turn)return;
        t->backendStopped=true;lease(*t,false);
        auto status=turn["status"].toString();
        if(t->cancelRequested)t->status=toolsActive(*t)?"stopping":"stopped";
        else t->status=status=="completed"?"completed":status=="interrupted"?"stopped":"failed";
        if(turn["error"].isObject())t->result=turn["error"].toObject()["message"].toString();changed(tid);runQueue();
    } else if(method=="item/completed"){
        auto item=p["item"].toObject();auto type=item["type"].toString();
        if(type=="agentMessage"&&item["phase"]=="final_answer"){t->result=item["text"].toString().left(16000);changed(tid);}
        else if(!t->shared&&(type=="commandExecution"||type=="mcpToolCall"||type=="fileChange"))event({{"type","phone-task-detail"},{"conversation",t->conversation},{"taskId",tid},{"item",item}});
    } else if(method=="item/started"&&!t->shared){
        auto item=p["item"].toObject();event({{"type","phone-task-detail"},{"conversation",t->conversation},{"taskId",tid},{"item",item}},false);
    }
}
void Session::save(){
    QDir().mkpath(QFileInfo(journal).absolutePath());QSaveFile f(journal);if(f.open(QIODevice::WriteOnly)){f.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);f.write(QJsonDocument(tasks.snapshot()).toJson(QJsonDocument::Compact));f.commit();}
}
void Session::restore(){QFile f(journal);if(f.open(QIODevice::ReadOnly))tasks.restore(QJsonDocument::fromJson(f.readAll()).array());}

void Session::question(QJsonObject message){
    auto p=message["params"].toObject();Task *t=nullptr;
    // On the conversation's own thread: the live task of the asking turn.
    for(auto *s:tasks.onThread(p["threadId"].toString()))if(!s->shared||s->turn==p["turnId"].toString()){t=s;break;}
    if(!t)t=tasks.byThread(p["threadId"].toString());
    if(!t){rpc("ServerResponse",{{"requestId",message["id"]},{"result",QJsonObject{{"answers",QJsonObject{}}}}});return;}
    if(message["method"]=="item/tool/requestUserInput"){
        if(t->terminal()||t->cancelRequested||(!p["turnId"].toString().isEmpty()&&p["turnId"].toString()!=t->turn)){
            rpc("ServerResponse",{{"requestId",message["id"]},{"result",QJsonObject{{"answers",QJsonObject{}}}}});return;
        }
        t->question={{"requestId",message["id"]},{"questions",p["questions"]}};
        t->status="waiting_input";changed(t->id);
        notices.append("Task "+t->id+" needs the user's answer: "+QString::fromUtf8(QJsonDocument(p["questions"].toArray()).toJson(QJsonDocument::Compact)));
    } else rpc("ServerResponse",{{"requestId",message["id"]},{"result",QJsonObject{{"decision","decline"}}}});
}
QJsonObject Session::answer(QString tid,QJsonObject answers){
    auto *t=tasks.find(tid);if(!t||t->status!="waiting_input"||t->question.isEmpty())return {{"error","This task has no pending question"}};
    QJsonObject values;
    for(auto v:t->question["questions"].toArray()){
        auto q=v.toObject();auto key=q["id"].toString();auto a=answers[key].toArray();
        if(a.isEmpty()||a.first().toString().trimmed().isEmpty())return {{"error","Answer every pending question"}};
        values[key]=QJsonObject{{"answers",a}};
    }
    const QJsonValue request=t->question["requestId"];t->question={};t->status="running";changed(tid);
    rpc("ServerResponse",{{"requestId",request},{"result",QJsonObject{{"answers",values}}}});return {{"ok",true},{"taskId",tid}};
}

QJsonArray Session::trusted() const {
    QJsonArray result;int finished=0;
    for(auto i=tasks.rows.crbegin();i!=tasks.rows.crend();++i){
        const auto &t=*i;if(t.conversation!=conversation)continue;
        if(t.terminal()&&finished++>=8)continue;
        QJsonObject o{{"taskId",t.id},{"text",t.text.left(500)},{"status",t.status},{"readOnly",t.readOnly},{"result",t.result.left(500)},{"question",t.question}};
        if(!t.terminal()&&!t.facts.isEmpty())o["progress"]=t.facts;
        result.append(o);
    }
    return result;
}
