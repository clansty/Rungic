// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once
#include "audio.h"
#include "core.h"
#include <QElapsedTimer>
#include <QJsonObject>
#include <QTimer>
#include <QWebSocket>
#include <functional>
struct ResponseContext {QString text,utterance;quint64 generation=0;bool progress=false;QString instruction;bool aloud=false;};
class Session : public QObject {
public:
    std::function<void(QJsonObject)> output;
    Audio audio;
    Tasks tasks;
    QWebSocket ws;
    QTimer timer;
    QString id,conversation,phase="closed",language,prompt,utterance,focusedQuestion,lastUserText;
    QStringList inputItems;
    QHash<QString,QString> transcripts,assistantText;
    QHash<QString,ResponseContext> responses;
    QList<ResponseContext> expected,deferred;
    ReplyBuffer playback;
    QString truncateItem;
    quint64 generation=0,playStart=0,playedSamples=0,truncateStart=0,truncateSamples=0;
    bool narrationSuppressed=false,inputBlocked=false,micDropped=false;
    QList<ResponseContext> acknowledgements;
    QString steerUtterance;
    QSet<QString> steeredTasks;
    QString controlUtterance,controlledTask;
    bool connected=false,configured=false,localSpeech=false,serverSpeech=false,commitPending=false,submitted=false,responseActive=false,muted=false,externalBusy=false;
    qint64 lastVoice=0,lastUser=0,lastProgress=0,lastPlaybackPush=-10000,lastFacts=-10000;
    qint64 started=0;   // the call's start, seconds since the epoch (its time on the app's call bar)
    QElapsedTimer clock;
    QString journal,leases,processStart;
    // The session's kind (docs/115): "call", the phone call with its own audio (Audio), or "press",
    // push-to-talk, whose microphone and playback are the adapter's (VoiceAgent): a press starts and
    // commits each utterance, reply audio goes out as events.
    QString mode="call";
    bool press() const {return mode=="press";}
    quint64 pressSent=0;          // press: samples of the current reply sent to the adapter
    qint64 pressHeardMs=-1;       // press: how much of the reply the user heard when they pressed
    QSet<QString> aloud;          // press: responses reading a text aloud (heard, not shown)
    QSet<QString> spoken;         // utterances whose reply said something (no second acknowledgement)
    QStringList notices;
    QHash<int,std::function<void(QJsonObject)>> callbacks;
    int requests=0;
    explicit Session(QObject *parent=nullptr);
    void receive(QJsonObject message);
    void command(QString method,QJsonObject args,std::function<void(QJsonObject)> done);
    void start(QString conversation,QString language);
    void stop(QString reason={});
    void state();
    QJsonObject fields() const;
    bool thinking() const;
    void event(QJsonObject o,bool keep=true);
    void send(QJsonObject o);
    void configure();
    QJsonArray trusted() const;
    void incoming(QJsonObject o);
    void onSpeech(bool value);
    void tick();
    // How many 20 ms chunks of the reply to play now: enough to keep it 300 ms ahead of what Android has
    // played of it (`pushed`: 24 kHz samples of the reply played so far; `played`, `start`: Android's
    // 48 kHz frames now and when the reply began), at most 8 in one tick.
    static int chunksDue(quint64 pushed,quint64 played,quint64 start);
    void replyTooLong();
    void stopSpeaking();
    QString timeNote() const;
    void pressCommand(const QString &method,const QJsonObject &args,std::function<void(QJsonObject)> done);
    void requestReply(ResponseContext context,QString instruction={});
    QJsonObject replyRequest(const ResponseContext &context,const QString &instruction) const;
    void tool(QString name,QJsonObject args,QString callId,QString responseId);
    void rpc(QString method,QJsonObject params,std::function<void(QJsonObject)> done={});
    void notification(QString method,QJsonObject params);
    void taskNotification(Task &task,QString method,QJsonObject params);
    void question(QJsonObject message);
    QJsonObject answer(QString taskId,QJsonObject answers);
    void runQueue();
    void startTask(QString taskId);
    void stopTask(QString taskId);
    void changed(QString taskId);
    void lease(Task &task,bool active);
    bool toolsActive(const Task &task);
    void save();
    void restore();
};
