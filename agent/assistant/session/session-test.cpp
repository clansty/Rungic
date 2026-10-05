// SPDX-License-Identifier: GPL-2.0-or-later
// covers: agent.phone-mode/E3 agent.phone-mode/E4 agent.phone-mode/E5 agent.phone-mode/E6 agent.phone-mode/E7
#include "session.h"
#include <algorithm>
#include <QCoreApplication>
#include <QDateTime>
#include <QElapsedTimer>
#include <QJsonDocument>
#include <QLocalServer>
#include <QLocalSocket>
#include <QTemporaryDir>
#include <cstdio>
#include <cstdlib>
static void check(bool value,const char *message){if(!value){std::fprintf(stderr,"FAIL %s\n",message);std::exit(1);}}
int main(int argc,char **argv){
    QTemporaryDir dir;qputenv("HOME",dir.path().toUtf8());qputenv("XDG_RUNTIME_DIR",dir.path().toUtf8());
    QCoreApplication app(argc,argv);gst_init(&argc,&argv);Session s;
    QList<QJsonObject> output;s.output=[&](QJsonObject o){output.append(o);};
    s.conversation="origin";s.id="voice-test";s.configured=true;s.generation=1;
    s.responses["reply"]={"请调查这件事，不要改文件", "utterance1", 1, false};
    s.tool("start_task",{{"original_words","删除文件"},{"access","read_only"}},"call1","reply");
    check(s.tasks.rows.size()==1,"one task admitted");auto tid=s.tasks.rows[0].id;
    check(s.tasks.rows[0].text=="请调查这件事，不要改文件","final ASR overrides model's rewritten words");
    s.tool("start_task",{{"access","read_only"}},"call2","reply");check(s.tasks.rows.size()==1,"one utterance cannot duplicate task execution");
    s.generation=2;s.tool("start_task",{},"late-call","reply");check(s.tasks.rows.size()==1,"interrupted tool call cannot start work");
    s.receive({{"type","rpc-result"},{"id",1},{"result",QJsonObject{{"thread",QJsonObject{{"id","backend"}}}}}});
    s.receive({{"type","rpc-result"},{"id",2},{"result",QJsonObject{{"turn",QJsonObject{{"id","turn1"}}}}}});
    check(s.tasks.find(tid)->status=="running","turn starts after its own thread");
    QJsonArray questions{QJsonObject{{"id","scope"},{"question","Choose the scope"}}};
    s.question({{"id",42},{"method","item/tool/requestUserInput"},{"params",QJsonObject{{"threadId","backend"},{"questions",questions}}}});
    check(s.tasks.find(tid)->status=="waiting_input","question holds its task until answered");
    check(s.answer(tid,{}).contains("error")&&s.tasks.find(tid)->status=="waiting_input","empty answer never chooses a default");
    check(s.answer("other",{{"scope",QJsonArray{"today"}}}).contains("error"),"answers cannot target another task");
    check(s.answer(tid,{{"scope",QJsonArray{"today"}}})["ok"].toBool()&&s.tasks.find(tid)->status=="running","complete answer resumes the owning task");
    bool boundAnswer=false;for(auto o:output)if(o["type"]=="rpc"&&o["method"]=="ServerResponse"){
        auto p=o["params"].toObject();boundAnswer=p["requestId"].toInt()==42&&p["result"].toObject()["answers"].toObject()["scope"].toObject()["answers"].toArray()==QJsonArray{"today"};
    }
    check(boundAnswer,"answer retains backend request identity and protocol shape");
    s.stop();check(s.tasks.find(tid)->status=="running","hangup leaves execution running");
    s.stopTask(tid);check(s.tasks.find(tid)->status=="stopping","interrupt request is not completion");
    s.notification("turn/completed",{{"threadId","other"},{"turn",QJsonObject{{"id","turn1"},{"status","interrupted"}}}});
    check(s.tasks.find(tid)->status=="stopping","another chat cannot complete this task");
    s.notification("turn/completed",{{"threadId","backend"},{"turn",QJsonObject{{"id","stale-turn"},{"status","interrupted"}}}});
    check(s.tasks.find(tid)->status=="stopping","stale turn completion ignored");
    s.notification("turn/completed",{{"threadId","backend"},{"turn",QJsonObject{{"id","turn1"},{"status","interrupted"}}}});
    check(s.tasks.find(tid)->status=="stopped","actual backend termination completes cancellation");
    s.question({{"id",43},{"method","item/tool/requestUserInput"},{"params",QJsonObject{{"threadId","backend"},{"turnId","turn1"},{"questions",questions}}}});
    check(s.tasks.find(tid)->status=="stopped"&&s.tasks.find(tid)->question.isEmpty(),"late question cannot revive a stopped task");
    s.id="queued-test";s.configured=true;s.externalBusy=true;
    auto queued=s.tasks.add("inspect attachment",true,"origin","queued-attachment");
    s.tasks.find(queued)->input=QJsonArray{QJsonObject{{"type","text"},{"text","inspect attachment"}}};
    s.responses["correction"]={"only inspect the last page","correction-utterance",s.generation,false};
    s.tool("steer_task",{{"task_id",queued}},"steer1","correction");
    s.tool("steer_task",{{"task_id",queued}},"steer2","correction");
    check(s.tasks.find(queued)->input.size()==2&&s.tasks.find(queued)->input.last().toObject()["text"]=="only inspect the last page","queued attachment retains exactly one correction in execution input");
    s.tasks.find(queued)->status="running";s.tasks.find(queued)->thread="steered-thread";s.tasks.find(queued)->turn="steered-turn";
    s.responses["live-correction"]={"keep both constraints","another-utterance",s.generation,false};
    s.tool("steer_task",{{"task_id",queued}},"steer3","live-correction");
    s.tool("steer_task",{{"task_id",queued}},"steer4","live-correction");
    auto unrelated=s.tasks.add("weather",true,"origin","weather");
    s.tasks.find(unrelated)->status="running";s.tasks.find(unrelated)->thread="weather-thread";s.tasks.find(unrelated)->turn="weather-turn";
    s.tool("steer_task",{{"task_id",unrelated}},"steer-weather","live-correction");
    int corrections=0;for(auto o:output)if(o["type"]=="rpc"&&o["method"]=="turn/steer")++corrections;
    check(corrections==1&&s.tasks.find(unrelated)->text=="weather","duplicate correction calls cannot steer a second task");
    for(const auto &o:output)if(o["type"]=="event"&&o["event"].toObject()["type"]=="phone-task")check(o["event"].toObject()["conversation"]=="origin","history stays with origin after hangup");
    s.id="reconnected";s.phase="connecting";s.configured=false;s.audio.opened=s.audio.sourceReady=s.audio.captureReady=true;
    s.onSpeech(true);s.incoming({{"type","session.updated"}});
    check(s.inputBlocked&&s.phase=="connecting","speech spanning connection readiness requires a complete repeat");
    s.incoming({{"type","input_audio_buffer.speech_started"},{"item_id","partial"}});
    check(s.inputItems.isEmpty(),"connection cannot execute the remaining tail of a request");
    s.onSpeech(false);s.lastVoice=s.clock.elapsed()-1000;s.tick();
    check(!s.inputBlocked&&s.phase=="connected","quiet interval restores listening explicitly");
    s.incoming({{"type","error"},{"error",QJsonObject{{"code","invalid_response"},{"message","test protocol failure"}}}});
    check(!s.id.isEmpty(),"one refused request does not end the call (recovery first, 2026-10-05)");
    s.incoming({{"type","error"},{"error",QJsonObject{{"code","session_expired"},{"message","test session expired"}}}});
    check(s.id.isEmpty()&&s.phase=="closed","a session that cannot go on releases voice resources");
    {
        // covers: agent.phone-mode/E16
        // How much of a reply to play: by what Android has played of it, not by its buffer (which the
        // communication service keeps full of PulseAudio's silence), and late ticks are made up.
        check(Session::chunksDue(0,0,0)==8&&Session::chunksDue(4800,0,0)==5,"a reply starts 300 ms ahead, 8 chunks a tick at most");
        check(Session::chunksDue(7200,0,0)==0,"300 ms ahead of what was played: nothing more now");
        check(Session::chunksDue(7200,48000+960,48000)==1,"20 ms played: 20 ms more");
        check(Session::chunksDue(7200,48000+2880,48000)==3,"a tick late by 40 ms: three chunks, not one");
        check(Session::chunksDue(7200,40000,48000)==0,"Android's frames before the reply began are not the reply's");
        check(Session::chunksDue(24000,48000+33600,48000)==0&&Session::chunksDue(24000,48000+48000,48000)==8,
              "however full Android's buffer is with silence, the reply goes on as it is played");
    }
    {
        // covers: agent.phone-mode/E8
        // Mute and hang-up against a stand-in of the Android communication audio backend (its socket,
        // $XDG_RUNTIME_DIR/rungic-communication.sock): muting stops the microphone capture and tells
        // Android to close the physical microphone, while the reply keeps playing; hanging up releases
        // the capture, the playback and the backend's call audio. No sound server here: a capture that
        // cannot start is tried again while the call is open (Audio::capture).
        qputenv("PULSE_SERVER","unix:/nonexistent");
        QLocalServer backend;check(backend.listen(dir.path()+"/rungic-communication.sock"),"stand-in backend listens");
        QLocalSocket *peer=nullptr;QList<QJsonObject> requests;bool released=false;
        QObject::connect(&backend,&QLocalServer::newConnection,[&]{peer=backend.nextPendingConnection();
            QObject::connect(peer,&QLocalSocket::readyRead,[&]{while(peer->canReadLine())requests.append(QJsonDocument::fromJson(peer->readLine()).object());});
            QObject::connect(peer,&QLocalSocket::disconnected,[&]{released=true;});});
        auto until=[&](std::function<bool()> done){QElapsedTimer t;t.start();while(!done()&&t.elapsed()<5000)QCoreApplication::processEvents(QEventLoop::AllEvents,20);return done();};
        auto last=[&](QString op){for(int i=requests.size()-1;i>=0;--i)if(requests[i]["op"]==op)return requests[i];return QJsonObject{};};
        Session m;QList<QJsonObject> states;QStringList failures;
        m.output=[&](QJsonObject o){auto e=o["event"].toObject();if(e["type"]=="phone-state")states.append(e);};
        m.audio.failed=[&](QString reason){failures.append(reason);};
        m.id="mute-test";m.conversation="origin";m.configured=true;m.phase="connected";
        m.audio.start(m.id);
        check(until([&]{return !last("open").isEmpty();}),"the call audio is opened with the backend");
        peer->write(QJsonDocument(QJsonObject{{"type","ready"},{"microphone",true}}).toJson(QJsonDocument::Compact)+'\n');
        check(until([&]{return m.audio.opened;}),"backend ready");
        if(!m.audio.recorder)m.audio.recorder=gst_parse_launch("fakesrc ! fakesink",nullptr);   // without webrtcdsp
        m.audio.player=gst_parse_launch("appsrc name=audio ! fakesink",nullptr);m.audio.src=gst_bin_get_by_name(GST_BIN(m.audio.player),"audio");
        check(m.audio.recorder&&m.audio.player,"capturing and playing");
        QJsonObject reply;m.command("SetPhoneMuted",{{"sessionId","mute-test"},{"muted",true}},[&](QJsonObject r){reply=r;});
        check(reply["ok"].toBool(),"mute accepted");
        check(!m.audio.recorder&&m.audio.captureToken.isEmpty(),"muted: the microphone capture is stopped");
        check(until([&]{return last("mute")["muted"].toBool();}),"muted: Android is told to close the physical microphone");
        check(m.audio.player!=nullptr&&m.audio.opened,"muted: the reply keeps playing");
        check(!states.last()["microphone"].toBool()&&states.last()["muted"].toBool(),"muted: the card shows the microphone off");
        m.command("SetPhoneMuted",{{"sessionId","mute-test"},{"muted",false}},[&](QJsonObject r){reply=r;});
        check(until([&]{auto o=last("mute");return o.contains("muted")&&!o["muted"].toBool();}),"unmuted: Android opens the microphone again");
        check(m.audio.recorder!=nullptr||m.audio.captureRetrying,"unmuted: capture starts again, or is tried again");
        // covers: agent.phone-mode/E4
        // The screen locked or Plasma hidden: the call goes on, as a phone call does (2026-10-05).
        reply={};m.command("Foreground",{{"visible",false}},[&](QJsonObject r){reply=r;});
        check(reply["ok"].toBool()&&m.id=="mute-test"&&m.audio.opened&&m.audio.player!=nullptr,"hidden: the call stays open and keeps playing");
        // Hung up from Android's call notification (AgentCall): its position says so, and the call ends.
        const auto failedBefore=failures.size();
        peer->write(QJsonDocument(QJsonObject{{"type","position"},{"epoch",1},{"playedFrames",0},{"writtenFrames",0},{"hungUp",true}}).toJson(QJsonDocument::Compact)+'\n');
        check(until([&]{return m.id.isEmpty();}),"hung up on Android: the session ends the call");
        check(failures.size()==failedBefore,"hung up on Android: an ordinary hang-up, not a failure");
        check(!m.audio.recorder&&!m.audio.player&&!m.audio.opened,"hung up: capture and playback released");
        check(until([&]{return released;}),"hung up: the backend's call audio is released");
        check(m.phase=="closed"&&m.id.isEmpty(),"hung up: the session is closed");
    }
    {
        // covers: agent.phone-mode/E4
        // The call's audio recovers instead of ending the call (2026-10-05): the backend going away or
        // reporting an error opens the audio again while the call lasts; the bar says "reconnecting".
        QLocalServer::removeServer(dir.path()+"/rungic-communication.sock");
        QLocalServer backend;check(backend.listen(dir.path()+"/rungic-communication.sock"),"stand-in backend listens again");
        QLocalSocket *peer=nullptr;int opens=0;
        QObject::connect(&backend,&QLocalServer::newConnection,[&]{peer=backend.nextPendingConnection();
            QObject::connect(peer,&QLocalSocket::readyRead,[&,peer]{while(peer->canReadLine())if(QJsonDocument::fromJson(peer->readLine()).object()["op"]=="open")++opens;});});
        auto until=[&](std::function<bool()> done){QElapsedTimer t;t.start();while(!done()&&t.elapsed()<8000)QCoreApplication::processEvents(QEventLoop::AllEvents,20);return done();};
        auto ready=[&]{peer->write(QJsonDocument(QJsonObject{{"type","ready"},{"microphone",false}}).toJson(QJsonDocument::Compact)+'\n');};
        Session r;QStringList failures;QList<QJsonObject> states;
        r.output=[&](QJsonObject o){auto e=o["event"].toObject();if(e["type"]=="phone-state")states.append(e);};
        r.audio.failed=[&](QString reason){failures.append(reason);};
        r.id="recover-test";r.conversation="origin";r.configured=true;r.phase="connected";r.muted=true;
        r.audio.start(r.id);r.audio.muted=true;     // muted: no capture is needed without a sound server
        check(until([&]{return opens==1;}),"recovery: the call audio is opened");
        ready();check(until([&]{return r.audio.opened;}),"recovery: ready");
        peer->disconnectFromServer();
        check(until([&]{return r.phase=="reconnecting";}),"backend gone: the call is reconnecting, not ended");
        check(r.id=="recover-test"&&failures.isEmpty(),"backend gone: the session goes on");
        check(!states.isEmpty()&&states.last()["phase"]=="reconnecting","backend gone: the bar is told");
        check(until([&]{return opens==2;}),"backend gone: the audio is opened again");
        ready();check(until([&]{return r.audio.opened&&r.phase=="connected";}),"opened again: the call is connected");
        peer->write(QJsonDocument(QJsonObject{{"type","error"},{"error","Communication audio disconnected"}}).toJson(QJsonDocument::Compact)+'\n');
        check(until([&]{return opens==3;}),"the backend's error: the audio is opened again");
        ready();check(until([&]{return r.audio.opened&&r.id=="recover-test";}),"after the backend's error the call goes on");
        r.stop();
        check(r.id.isEmpty()&&!r.audio.reopening,"hung up: no more reopening");

        // covers: agent.phone-mode/E17
        // A task's progress, as the push-to-talk turn has it (TaskFacts from the adapter), is in the
        // voice's trusted snapshot while it runs, and not after.
        Session f;f.conversation="facts";auto id=f.tasks.add("make a game",false,"facts","k1");f.tasks.find(id)->status="running";
        QJsonObject reply;f.command("TaskFacts",{{"taskId",id},{"facts","Done: Write the brief\nNow: Run Krita (12 s)"}},[&](QJsonObject r){reply=r;});
        check(reply["ok"].toBool(),"facts accepted");
        auto snap=f.trusted();check(snap.size()==1&&snap[0].toObject()["progress"].toString().contains("Run Krita"),"the voice knows what the task is doing");
        f.tasks.find(id)->status="completed";
        check(!f.trusted()[0].toObject().contains("progress"),"a finished task has its result, not progress");
    }
    {
        // covers: agent.phone-mode/E7
        // A call's work that acts goes to the conversation's own thread (phone_session.py), where it
        // starts a turn or joins the running one: a second request does not wait (2026-10-05: a cast
        // waited behind a Krita drawing). The thread's notifications reach each task of its turn,
        // and a task of an earlier turn is left as it ended.
        Session w;QList<QJsonObject> rpcs,events;w.conversation="c1";w.leases=dir.path();
        w.output=[&](QJsonObject o){if(o["type"]=="rpc")rpcs.append(o);else if(o["type"]=="event")events.append(o["event"].toObject());};
        auto reply=[&](int at,QJsonObject result){w.receive({{"type","rpc-result"},{"id",rpcs[at]["id"]},{"result",result}});};
        auto old=w.tasks.add("draw earlier",false,"c1","k0");w.tasks.find(old)->status="completed";w.tasks.find(old)->thread="main";w.tasks.find(old)->shared=true;w.tasks.find(old)->turn="turn-0";
        auto a=w.tasks.add("draw a tea garden",false,"c1","k1");w.runQueue();
        check(rpcs.size()==1&&rpcs[0]["method"]=="thread/start","the first request asks for its thread");
        reply(0,{{"thread",QJsonObject{{"id","main"}}},{"shared",true}});
        check(w.tasks.find(a)->shared&&!QFile::exists(dir.path()+"/"+a+".json"),"the conversation's thread: no lease of its own");
        reply(1,{{"turn",QJsonObject{{"id","turn-1"}}}});
        check(w.tasks.find(a)->status=="running","the drawing runs");
        auto b=w.tasks.add("cast it to the TV",false,"c1","k2");w.runQueue();
        check(w.tasks.find(b)->status=="starting","the cast does not wait behind the drawing");
        reply(2,{{"thread",QJsonObject{{"id","main"}}},{"shared",true}});reply(3,{{"turn",QJsonObject{{"id","turn-1"}}},{"joined",true}});
        check(w.tasks.find(b)->status=="running"&&w.tasks.find(b)->turn=="turn-1","it joined the drawing's turn");
        check(std::none_of(events.begin(),events.end(),[](const QJsonObject &e){return e["type"]=="phone-task";}),"its card is the turn's (push-to-talk's)");
        w.receive({{"type","notification"},{"method","item/completed"},{"params",QJsonObject{{"threadId","main"},{"turnId","turn-1"},{"item",QJsonObject{{"type","agentMessage"},{"phase","final_answer"},{"text","Drawn and cast"}}}}}});
        w.receive({{"type","notification"},{"method","turn/completed"},{"params",QJsonObject{{"threadId","main"},{"turn",QJsonObject{{"id","turn-1"},{"status","completed"}}}}}});
        check(w.tasks.find(a)->status=="completed"&&w.tasks.find(b)->status=="completed","both end with their turn");
        check(w.tasks.find(a)->result=="Drawn and cast"&&w.tasks.find(old)->result.isEmpty(),"the turn's answer is theirs, not an earlier task's");
        // Progress for the voice from push-to-talk's rules (Narrate) is accepted with no call open.
        QJsonObject said;w.command("Narrate",{{"text","Progress: now drawing"}},[&](QJsonObject r){said=r;});
        check(said["ok"].toBool(),"narration accepted");
    }
    {
        // covers: agent.phone-mode/E9
        // The voice's own reports keep the session's rules (a response's instructions replace them):
        // an acknowledgement told the user to choose the TV's input, an update came in Korean, and an
        // update inside the conversation answered the user's new request without tools ("I can't").
        Session r;r.prompt="RULES: one assistant; never give the user steps.";r.lastUserText="给这个画配一段背景音乐";
        const auto ack=r.replyRequest({"cast started","utt-1",1,true},"Acknowledge the tool result.");
        check(ack["tool_choice"]=="none"&&ack["instructions"].toString().startsWith("RULES:")&&ack["instructions"].toString().contains("Acknowledge the tool result."),"an acknowledgement keeps the rules");
        check(!ack.contains("conversation"),"an acknowledgement stays with its utterance");
        const auto update=r.replyRequest({"Now: drawing the hills",{},1,true},"Report only these updates.");
        check(update["instructions"].toString().startsWith("RULES:")&&update["conversation"]=="none","an update keeps the rules and stays outside the conversation");
        check(update["input"].toArray()[0].toObject()["content"].toArray()[0].toObject()["text"].toString().contains("给这个画配一段背景音乐"),"in the user's language");
        check(r.replyRequest({"draw it","utt-2",1,false},"").isEmpty(),"an answer to the user is the session's own");
        check(ack["instructions"].toString().contains("Speak Chinese"),"the user's language named (an update came in English and in Korean)");
    }
    // covers: agent.phone-mode/E14 agent.phone-mode/E15
    // The app's call bar and its summary (docs/101): the state says when the call began and when the
    // Agent is taking in what was said; hanging up leaves the call's summary in its conversation.
    {
        Session c;QList<QJsonObject> out;c.output=[&](QJsonObject o){out.append(o);};
        c.conversation="call-conv";c.id="call-summary";c.configured=true;c.generation=1;
        c.started=QDateTime::currentSecsSinceEpoch()-75;
        check(c.fields()["startedAt"].toDouble()==double(c.started),"the state says when the call began");
        c.submitted=false;c.utterance="u1";c.localSpeech=false;
        check(c.thinking()&&c.fields()["thinking"].toBool(),"said and not yet answered: thinking");
        c.localSpeech=true;check(!c.thinking(),"still speaking: not thinking");
        c.localSpeech=false;c.responseActive=true;check(!c.thinking(),"answering: not thinking");
        c.responseActive=false;c.submitted=true;c.utterance.clear();check(!c.thinking(),"nothing said: not thinking");
        auto before=c.tasks.add("old work",true,"call-conv","before");c.tasks.find(before)->created=c.started-10;
        auto during=c.tasks.add("sort the screenshots",true,"call-conv","during");
        c.tasks.add("elsewhere",true,"other-conv","other");
        c.stop("Voice paused while Plasma is hidden");
        QJsonObject ended;bool kept=false;
        for(const auto &o:out)if(o["type"]=="event"&&o["event"].toObject()["type"]=="phone-ended"){ended=o["event"].toObject();kept=o["keep"].toBool();}
        check(!ended.isEmpty()&&kept,"hanging up keeps the call's summary in its conversation");
        check(ended["conversation"]=="call-conv"&&ended["seconds"].toDouble()>=75,"the summary says how long the call was");
        auto work=ended["tasks"].toArray();
        check(work.size()==1&&work[0].toObject()["taskId"]==during,"the summary lists only what was started in this call, in this conversation");
        check(ended["reason"]=="Voice paused while Plasma is hidden","the summary says why the call ended when it was not hung up");
        check(c.started==0&&c.fields()["startedAt"].toDouble()==0,"after the call no start time is left");
    }
    {
        // A reply too long to buffer is cut, never the conversation (2026-10-05: a 30 s bound that
        // stopped the session ended Kevin's game).
        Session v;QList<QJsonObject> events;v.output=[&](QJsonObject o){events.append(o);};
        v.conversation="long";v.id="voice-long";v.configured=true;v.generation=1;v.responseActive=true;
        v.responses["long-reply"]={"tell a story","u1",1,false};
        auto delta=[&](qsizetype bytes){v.incoming({{"type","response.output_audio.delta"},{"response_id","long-reply"},{"item_id","item-long"},
                                                    {"delta",QString::fromLatin1(QByteArray(bytes,'\x01').toBase64())}});};
        for(int i=0;i<18;++i)delta(ReplyBuffer::MaxPendingBytes/18);   // half an hour, in parts
        delta(960);delta(960);
        check(v.id=="voice-long","an overlong reply does not end the session");
        check(v.playback.full&&v.playback.pending.size()==ReplyBuffer::MaxPendingBytes,"what was buffered still plays, the rest is dropped");
        v.incoming({{"type","response.cancelled"},{"response_id","long-reply"}});
        check(v.playback.pending.size()==ReplyBuffer::MaxPendingBytes,"cutting the reply keeps its buffered audio");
        for(const auto &o:events)if(o["type"]=="event")check(o["event"].toObject()["type"]!="phone-notice","no notice ends or interrupts the conversation");
    }
    std::puts("session state, transcript fidelity, late events, cancellation and hangup checks passed");
}
