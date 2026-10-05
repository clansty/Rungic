// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once
#include <QDateTime>
#include <QHash>
#include <QJsonArray>
#include <QJsonObject>
#include <QList>
#include <QSet>
#include <QString>

struct Task {
    QJsonObject question;
    QJsonArray input;
    QString id, conversation, thread, turn, text, status="queued", result, requestKey;
    bool readOnly=false,cancelRequested=false,backendStopped=false;
    qint64 created=QDateTime::currentSecsSinceEpoch();
    QJsonObject json() const;
    bool terminal() const;
    bool active() const;
};
class Tasks {
public:
    QList<Task> rows;
    QString focused;
    QString add(QString text,bool readOnly,QString conversation,QString requestKey);
    Task *find(const QString &id);
    Task *byThread(const QString &thread);
    Task *target(const QString &id);
    QList<QString> schedule();
    bool stop(const QString &id);
    QJsonArray snapshot(const QString &conversation={}) const;
    void restore(QJsonArray saved);
};
// Reply audio belongs to one response and generation. Cancelled deltas must never be replayed.
class ReplyBuffer {
public:
    // 24 kHz 16-bit mono: ten minutes of reply audio not yet played.
    static constexpr qsizetype MaxPendingBytes=qsizetype(24000)*2*600;
    QString item,response;
    QByteArray pending;
    quint64 generation=0;
    bool cancelled=false;
    // The reply ran past MaxPendingBytes: what was buffered still plays, the rest is dropped (the
    // session cuts the reply there, Session::replyTooLong); the conversation goes on.
    bool full=false;
    QSet<QString> retired;
    bool append(QString responseId,QString itemId,const QByteArray &data);
    void clear();
};
