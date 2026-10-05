#include "agentscreen.h"

#include <QCoreApplication>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusPendingCallWatcher>
#include <QDBusPendingReply>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QGuiApplication>
#include <QJsonArray>
#include <QUrl>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLocalSocket>
#include <QProcess>
#include <QThread>
#include <unistd.h>

namespace
{
const QString kSocket = qEnvironmentVariable("RUNGIC_PLATFORM_SOCKET", QStringLiteral("/mnt/android-wayland/platform.sock"));
// rungic_cua.activity (docs/88): a "working" report older than this was left by a writer that went away;
// an ending is news only for a moment (the window shows it a few seconds).
constexpr double kActivityStaleS = 120;
constexpr double kEndingStaleS = 10;
// rungic_cua.hold: a hold nobody refreshed for this long has ended (the window holding it went away).
constexpr double kHoldStaleS = 600;

// One request on the platform bridge; `timeoutMs` covers a TV connection (up to a minute).
QJsonObject bridge(const QJsonObject &request, int timeoutMs = 3000)
{
    QLocalSocket socket;
    socket.connectToServer(kSocket);
    if (!socket.waitForConnected(1000))
        return {{QStringLiteral("error"), QStringLiteral("platform bridge unavailable")}};
    socket.write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n');
    socket.flush();
    QByteArray reply;
    while (!reply.contains('\n') && socket.waitForReadyRead(timeoutMs))
        reply += socket.readAll();
    return QJsonDocument::fromJson(reply.trimmed()).object();
}
}

AgentScreen::AgentScreen(int workspace, QObject *parent)
    : QObject(parent)
    , m_workspace(workspace)
{
    connect(&m_poll, &QTimer::timeout, this, &AgentScreen::poll);
    m_poll.start(1500);
    // The caption: rungic-cua replaces the file by renaming, which changes the directory.
    const QString runtime = qEnvironmentVariable("XDG_RUNTIME_DIR", QStringLiteral("/run/user/%1").arg(getuid()));
    const QString dir = runtime + QStringLiteral("/rungic-agent-screen");
    QDir().mkpath(dir);
    // Each screen its own (rungic_cua.activity): workspace N's agent, or the agent on the user's desktop.
    m_activityPath = dir + (m_workspace > 0 ? QStringLiteral("/activity-ws%1.json").arg(m_workspace) : QStringLiteral("/activity.json"));
    m_holdPath = dir + QStringLiteral("/hold-ws%1.json").arg(m_workspace);
    m_activityWatcher.addPath(dir);
    connect(&m_activityWatcher, &QFileSystemWatcher::directoryChanged, this, &AgentScreen::readActivity);
    connect(&m_activityWatcher, &QFileSystemWatcher::directoryChanged, this, &AgentScreen::readHold);
    // While this window holds the screen it says so again now and then (a hold nobody refreshes ends).
    m_holdRefresh.setInterval(60000);
    connect(&m_holdRefresh, &QTimer::timeout, this, [this] {
        callAgent(QStringLiteral("HoldScreen"), {m_workspace, true, QString()}, [](bool, const QJsonObject &) {});
    });
    readActivity();
    readHold();
    poll();
}

void AgentScreen::readHold()
{
    QFile file(m_holdPath);
    bool held = false;
    if (file.open(QIODevice::ReadOnly)) {
        const QJsonObject record = QJsonDocument::fromJson(file.readAll()).object();
        const double refreshed = record.value(QStringLiteral("refreshed")).toDouble(record.value(QStringLiteral("since")).toDouble());
        held = QDateTime::currentMSecsSinceEpoch() / 1000.0 - refreshed < kHoldStaleS;
    }
    // Asked and not answered yet: the file comes with the answer.
    const bool here = m_heldHere && (held || m_holdAsked);
    if (held == m_held && here == m_heldHere)
        return;
    m_held = held;
    m_heldHere = here;
    if (!here)
        m_holdRefresh.stop();
    Q_EMIT workChanged();
}

void AgentScreen::callAgent(const QString &method, const QVariantList &args, std::function<void(bool, const QJsonObject &)> done)
{
    QDBusMessage message = QDBusMessage::createMethodCall(QStringLiteral("com.rungic.VoiceAgent"), QStringLiteral("/com/rungic/VoiceAgent"),
                                                          QStringLiteral("com.rungic.VoiceAgent"), method);
    message.setArguments(args);
    auto *watcher = new QDBusPendingCallWatcher(QDBusConnection::sessionBus().asyncCall(message, 40000), this);
    connect(watcher, &QDBusPendingCallWatcher::finished, this, [watcher, done] {
        QDBusPendingReply<QString> reply = *watcher;
        watcher->deleteLater();
        if (reply.isError()) {
            done(false, {{QStringLiteral("error"), reply.error().message()}});
            return;
        }
        done(true, QJsonDocument::fromJson(reply.value().toUtf8()).object());
    });
}

void AgentScreen::queryWork()
{
    if (m_querying)
        return;
    m_querying = true;
    callAgent(QStringLiteral("ScreenWork"), {m_workspace}, [this](bool ok, const QJsonObject &work) {
        m_querying = false;
        const QString kind = ok ? work.value(QStringLiteral("kind")).toString() : QString();
        const bool busy = ok && work.value(QStringLiteral("busy")).toBool();
        if (kind == m_workKind && busy == m_workBusy)
            return;
        m_workKind = kind;
        m_workBusy = busy;
        Q_EMIT workChanged();
    });
}

void AgentScreen::tell(const QString &text)
{
    if (text.trimmed().isEmpty())
        return;
    callAgent(QStringLiteral("SteerScreen"), {m_workspace, text}, [this](bool ok, const QJsonObject &done) {
        Q_EMIT controlDone(QStringLiteral("tell"), ok, ok ? done.value(QStringLiteral("outcome")).toString()
                                                          : done.value(QStringLiteral("error")).toString());
    });
}

void AgentScreen::stopWork()
{
    callAgent(QStringLiteral("StopScreen"), {m_workspace}, [this](bool ok, const QJsonObject &done) {
        const bool stopped = ok && done.value(QStringLiteral("stopped")).toBool();
        Q_EMIT controlDone(QStringLiteral("stop"), stopped || done.value(QStringLiteral("asked")).toBool(),
                           done.value(QStringLiteral("asked")).toBool() ? QStringLiteral("asked") : done.value(QStringLiteral("error")).toString());
        queryWork();
    });
}

void AgentScreen::takeOver()
{
    if (m_heldHere)
        return;
    m_heldHere = m_holdAsked = true;      // at once: the next touch must not ask again
    m_holdRefresh.start();
    callAgent(QStringLiteral("HoldScreen"), {m_workspace, true, QString()}, [this](bool ok, const QJsonObject &done) {
        m_holdAsked = false;
        if (!ok) {
            m_heldHere = false;
            m_holdRefresh.stop();
        }
        readHold();
        Q_EMIT controlDone(QStringLiteral("hold"), ok, done.value(QStringLiteral("error")).toString());
    });
}

void AgentScreen::giveBack(const QString &note)
{
    if (!m_held)
        return;
    m_holdRefresh.stop();
    callAgent(QStringLiteral("HoldScreen"), {m_workspace, false, note}, [this](bool ok, const QJsonObject &done) {
        m_heldHere = false;
        readHold();
        Q_EMIT workChanged();
        Q_EMIT controlDone(QStringLiteral("back"), ok, done.value(QStringLiteral("error")).toString());
    });
}

void AgentScreen::readActivity()
{
    QFile file(m_activityPath);
    QJsonObject report;
    if (file.open(QIODevice::ReadOnly))
        report = QJsonDocument::fromJson(file.readAll()).object();
    QString state = report.value(QStringLiteral("state")).toString();
    const QString text = report.value(QStringLiteral("text")).toString();
    const double time = report.value(QStringLiteral("time")).toDouble();
    const double now = QDateTime::currentMSecsSinceEpoch() / 1000.0;
    if (now - time > (state == QLatin1String("working") ? kActivityStaleS : kEndingStaleS))
        state.clear();
    // In a team (rungic_cua.team): the member's role and state, and its own words when newer.
    QString role, kind, said = text;
    if (m_workspace > 0) {
        QFile teamFile(m_activityPath.section(QLatin1Char('/'), 0, -2) + QStringLiteral("/team-ws%1.json").arg(m_workspace));
        if (teamFile.open(QIODevice::ReadOnly)) {
            const QJsonObject post = QJsonDocument::fromJson(teamFile.readAll()).object();
            role = post.value(QStringLiteral("role")).toString();
            kind = post.value(QStringLiteral("kind")).toString();
            const QString words = post.value(QStringLiteral("text")).toString();
            if (!words.isEmpty() && post.value(QStringLiteral("time")).toDouble() > time) {
                said = words;
                const bool ended = kind == QLatin1String("done") || kind == QLatin1String("failed") || kind == QLatin1String("ended");
                state = ended ? kind : QStringLiteral("working");
            }
        }
    }
    if (state == m_activityState && said == m_activityText && time == m_activityTime && role == m_teamRole && kind == m_teamKind)
        return;
    m_activityState = state;
    m_activityText = said;
    m_activityTime = time;
    m_teamRole = role;
    m_teamKind = kind;
    Q_EMIT activityChanged();
}

AgentScreen::~AgentScreen()
{
    setFullscreen(false);
    if (m_heldHere) {
        // The window goes: the screen goes back to the agent (a hold must not outlive its holder).
        QDBusMessage message = QDBusMessage::createMethodCall(QStringLiteral("com.rungic.VoiceAgent"), QStringLiteral("/com/rungic/VoiceAgent"),
                                                              QStringLiteral("com.rungic.VoiceAgent"), QStringLiteral("HoldScreen"));
        message.setArguments({m_workspace, false, QString()});
        QDBusConnection::sessionBus().call(message, QDBus::Block, 3000);
    }
}

QString AgentScreen::fullscreenMark() const
{
    const QString runtime = qEnvironmentVariable("XDG_RUNTIME_DIR", QStringLiteral("/run/user/%1").arg(getuid()));
    return runtime + QStringLiteral("/rungic-agent-screen/desktop-fullscreen");
}

void AgentScreen::setFullscreen(bool fullscreen)
{
    if (m_workspace > 0)
        return;
    if (fullscreen) {
        QFile mark(fullscreenMark());
        if (mark.open(QIODevice::WriteOnly))
            mark.close();
    } else {
        QFile::remove(fullscreenMark());
    }
}

QString AgentScreen::op() const
{
    return m_workspace > 0 ? QStringLiteral("agent-screen") : QStringLiteral("desktop-mode");
}

void AgentScreen::setStatus(const QString &status)
{
    if (m_status == status)
        return;
    m_status = status;
    qInfo() << "agent screen:" << status;
    Q_EMIT statusChanged();
}

void AgentScreen::poll()
{
    if (m_activityState == QLatin1String("working"))
        readActivity();  // goes stale when nothing reports any more
    readHold();
    if (m_status != QLatin1String("off"))
        queryWork();
    if (m_workspace == 0) {
        // Desktop mode is on while the independent desktop runs (rungic-desktop-mode, docs/research/97
        // §19); the bridge only says whether a TV shows it (computer mode), and may not answer.
        const QString runtime = qEnvironmentVariable("XDG_RUNTIME_DIR", QStringLiteral("/run/user/%1").arg(getuid()));
        if (!QFile::exists(runtime + QStringLiteral("/wayland-ws-0"))) {
            setStatus(QStringLiteral("off"));
            QTimer::singleShot(0, qApp, [] { QCoreApplication::exit(0); });
            return;
        }
        m_onTv = bridge({{QStringLiteral("op"), op()}}).value(QStringLiteral("tv")).toBool();
        update();
        return;
    }
    const QJsonObject state = bridge({{QStringLiteral("op"), op()}});
    if (state.contains(QStringLiteral("error"))) {
        setStatus(QStringLiteral("error: ") + state.value(QStringLiteral("error")).toString());
        return;
    }
    m_enabled = state.value(QStringLiteral("enabled")).toBool();
    // On the TV when the TV shows it (alone or in the director, docs/58); the assistant's screen's
    // "tv" is about the one workspace the screen was last turned on for.
    m_onTv = state.contains(QStringLiteral("tvShown"))
        ? state.value(QStringLiteral("tvShown")).toArray().contains(m_workspace)
        : state.value(QStringLiteral("tv")).toBool();
    if (!m_enabled) {  // turned off elsewhere (quick setting, rungic-agent-screen off)
        // poll() also runs in the constructor, before app.exec(): a direct quit is lost.
        // Mark it off before QML can map its black placeholder, then quit on the event loop.
        setStatus(QStringLiteral("off"));
        QTimer::singleShot(0, qApp, [] { QCoreApplication::exit(0); });
        return;
    }
    update();
}

void AgentScreen::update()
{
    // Its own KWin, recorded by a helper connected to it; nothing while a TV presents an
    // assistant's screen. Desktop mode on a TV is this window's own view on the TV's output
    // (docs/research/97 §19.5): the picture goes on.
    const bool shownHere = m_workspace == 0 && m_onTv;
    if (m_onTv && !shownHere) {
        stopWorkspaceStream();
        setStatus(QStringLiteral("tv"));
        return;
    }
    if (!m_workspaceStream || m_streamedWorkspace != m_workspace)
        startWorkspaceStream();
    if (m_nodeId)
        setStatus(shownHere ? QStringLiteral("tv") : QStringLiteral("running"));
}

void AgentScreen::startWorkspaceStream()
{
    stopWorkspaceStream();
    m_streamedWorkspace = m_workspace;
    m_workspaceStream = new QProcess(this);
    m_workspaceStream->setProgram(QStringLiteral("rungic-workspace-env"));
    QStringList arguments{QString::number(m_workspace), QStringLiteral("/usr/libexec/rungic-workspace-stream")};
    // Desktop mode's picture without the pointer: the touches are the pointer there.
    if (m_workspace == 0)
        arguments << QStringLiteral("--pointer-hidden");
    m_workspaceStream->setArguments(arguments);
    m_workspaceStream->setProcessChannelMode(QProcess::ForwardedErrorChannel);
    setStatus(QStringLiteral("connecting"));
    QProcess *process = m_workspaceStream;
    connect(process, &QProcess::readyReadStandardOutput, this, [this, process] {
        while (process->canReadLine()) {
            const QByteArray line = process->readLine().trimmed();
            if (line.startsWith("node ")) {
                m_nodeId = line.mid(5).toUInt();
                Q_EMIT nodeIdChanged();
                setStatus(m_onTv ? QStringLiteral("tv") : QStringLiteral("running"));
                if (m_pointerShown)
                    send(QStringLiteral("pointer-stream on"));
                if (m_tvShown)
                    send(QStringLiteral("tv-stream on"));
            } else if (line.startsWith("pointer-node ")) {
                m_pointerNodeId = line.mid(13).toUInt();
                Q_EMIT pointerNodeIdChanged();
            } else if (line.startsWith("tv-node ")) {
                m_tvNodeId = line.mid(8).toUInt();
                Q_EMIT tvNodeIdChanged();
            } else if (line.startsWith("prompting ")) {
                setPrompting(line.mid(10) == "1");
            } else if (line.startsWith("error ")) {
                setStatus(QStringLiteral("error: ") + QString::fromUtf8(line.mid(6)));
            }
        }
    });
    connect(process, &QProcess::finished, this, [this, process] {
        if (m_workspaceStream != process) {
            return;
        }
        m_workspaceStream = nullptr;
        process->deleteLater();
        if (m_nodeId) {
            m_nodeId = 0;
            Q_EMIT nodeIdChanged();
        }
        if (m_pointerNodeId) {
            m_pointerNodeId = 0;
            Q_EMIT pointerNodeIdChanged();
        }
        if (m_tvNodeId) {
            m_tvNodeId = 0;
            Q_EMIT tvNodeIdChanged();
        }
        setPrompting(false);
        // The workspace went or restarted: try again at the next poll.
        m_streamedWorkspace = 0;
        setStatus(QStringLiteral("waiting for the workspace"));
    });
    process->start();
}

void AgentScreen::setPrompting(bool prompting)
{
    if (prompting == m_prompting)
        return;
    m_prompting = prompting;
    Q_EMIT promptingChanged();
}

void AgentScreen::stopWorkspaceStream()
{
    if (!m_workspaceStream) {
        return;
    }
    QProcess *process = m_workspaceStream;
    m_workspaceStream = nullptr;
    m_streamedWorkspace = 0;
    process->closeWriteChannel();
    process->terminate();
    connect(process, &QProcess::finished, process, &QObject::deleteLater);
    setPrompting(false);
    if (m_nodeId) {
        m_nodeId = 0;
        Q_EMIT nodeIdChanged();
    }
    if (m_pointerNodeId) {
        m_pointerNodeId = 0;
        Q_EMIT pointerNodeIdChanged();
    }
    if (m_tvNodeId) {
        m_tvNodeId = 0;
        Q_EMIT tvNodeIdChanged();
    }
}

void AgentScreen::send(const QString &line)
{
    if (m_workspaceStream)
        m_workspaceStream->write((line + QLatin1Char('\n')).toUtf8());
}

void AgentScreen::setPointerShown(bool shown)
{
    if (m_workspace > 0 || shown == m_pointerShown)
        return;
    m_pointerShown = shown;
    send(shown ? QStringLiteral("pointer-stream on") : QStringLiteral("pointer-stream off"));
}

void AgentScreen::setTvShown(bool shown)
{
    if (shown == m_tvShown)
        return;
    m_tvShown = shown;
    send(shown ? QStringLiteral("tv-stream on") : QStringLiteral("tv-stream off"));
}

void AgentScreen::typeText(const QString &text)
{
    if (!text.isEmpty() && m_workspace > 0 && m_workBusy && !m_held)
        takeOver();
    if (!text.isEmpty())
        send(QStringLiteral("text ") + QString::fromLatin1(text.toUtf8().toBase64()));
}

void AgentScreen::key(int code, bool pressed)
{
    send(QStringLiteral("key %1 %2").arg(code).arg(pressed ? 1 : 0));
}

QString AgentScreen::backgroundFile() const
{
    const QString runtime = qEnvironmentVariable("XDG_RUNTIME_DIR", QStringLiteral("/run/user/%1").arg(getuid()));
    const QString file = runtime + QStringLiteral("/rungic-agent-screen/director-background.jpg");
    return QFile::exists(file) ? QUrl::fromLocalFile(file).toString() : QString();
}

void AgentScreen::pointerMove(double fx, double fy)
{
    send(QStringLiteral("pointer %1 %2").arg(fx).arg(fy));
}

void AgentScreen::pointerButton(int button, bool pressed)
{
    // The user's finger on an assistant's screen while its agent works there: the user takes it over,
    // or the two would fight over one pointer (docs/114). Given back from the director, or on leaving.
    if (pressed && m_workspace > 0 && m_workBusy && !m_held)
        takeOver();
    send(QStringLiteral("button %1 %2").arg(button).arg(pressed ? 1 : 0));
}

void AgentScreen::scroll(double dx, double dy)
{
    if (dy != 0)
        send(QStringLiteral("axis 0 %1").arg(dy));
    if (dx != 0)
        send(QStringLiteral("axis 1 %1").arg(dx));
}

void AgentScreen::castToTv()
{
    // The cast button (docs/58): the TV shows this window's screen, computer mode for desktop mode's
    // and the director (this one in focus) for an assistant's; no TV yet, the Rungic app's TV picker
    // first, a TV, its controls. "source" alone left a desktop mode's TV on the director (2026-10-02).
    const QJsonObject state = bridge({{QStringLiteral("op"), QStringLiteral("tv")}, {QStringLiteral("button"), true},
                                      {QStringLiteral("source"), m_workspace},
                                      {QStringLiteral("content"), m_workspace > 0 ? QStringLiteral("director") : QStringLiteral("desktop")}});
    if (state.contains(QStringLiteral("error")))
        setStatus(state.value(QStringLiteral("error")).toString());
}

void AgentScreen::close()
{
    if (op() == QStringLiteral("agent-screen")) {
        // The assistant's screen (docs/research/91): its workspace closes unless the agent is at work
        // there (then only hidden) or an app keeps it open (the user is told). rungic-agent-screen
        // decides and does it, in a unit of its own: it outlives this window and its cgroup.
        QProcess::startDetached(QStringLiteral("systemd-run"), {QStringLiteral("--user"), QStringLiteral("--collect"), QStringLiteral("--quiet"),
                                                                QStringLiteral("rungic-agent-screen"), QStringLiteral("dismiss"),
                                                                QString::number(m_workspace)});
    } else {
        // Desktop mode: the independent desktop closes, its apps asked first (rungic-desktop-mode).
        QProcess::startDetached(QStringLiteral("systemd-run"), {QStringLiteral("--user"), QStringLiteral("--collect"), QStringLiteral("--quiet"),
                                                                QStringLiteral("rungic-desktop-mode"), QStringLiteral("dismiss")});
    }
    // exit, not quit: Qt 6 cancels quit() when a window refuses to close, and the fullscreen window
    // refuses (it goes back to the floating one): closed from fullscreen, the window came straight
    // back (the G100 S, 2026-10-05).
    QCoreApplication::exit(0);
}

Director::Director(QObject *parent)
    : QObject(parent)
{
    m_board = new BoardTile(this);
    connect(&m_poll, &QTimer::timeout, this, &Director::poll);
    m_poll.start(700);
    poll();
}

QList<QObject *> Director::screens() const
{
    QList<QObject *> out;
    for (AgentScreen *screen : m_screens)
        out.append(screen);
    if (m_boardShown)
        out.append(m_board);
    return out;
}

QObject *Director::focusTile() const
{
    if (m_focus == BoardTile::kSlot && m_boardShown)
        return m_board;
    return m_screens.value(m_focus);
}

QObject *Director::focusScreen() const
{
    if (m_focus == BoardTile::kSlot)
        return m_screens.isEmpty() ? nullptr : m_screens.first();
    return m_screens.value(m_focus);
}

void Director::poll()
{
    const QJsonObject state = bridge({{QStringLiteral("op"), QStringLiteral("director")}});
    if (state.contains(QStringLiteral("error")) || state.value(QStringLiteral("version")).toInt(-2) == m_version)
        return;
    apply(state);
}

void Director::apply(const QJsonObject &state)
{
    m_version = state.value(QStringLiteral("version")).toInt();
    QList<int> members;
    for (const QJsonValue &value : state.value(QStringLiteral("members")).toArray())
        members.append(value.toInt());
    m_boardShown = members.removeAll(BoardTile::kSlot) > 0;
    m_board->setBoard(state.value(QStringLiteral("board")).toObject().toVariantMap());
    for (int slot : m_screens.keys()) {
        if (!members.contains(slot))
            delete m_screens.take(slot);
    }
    for (int slot : members) {
        if (!m_screens.contains(slot))
            m_screens.insert(slot, new AgentScreen(slot, this));
    }
    m_focus = state.value(QStringLiteral("focus")).toInt();
    if (!m_screens.contains(m_focus) && !(m_focus == BoardTile::kSlot && m_boardShown))
        m_focus = m_screens.isEmpty() ? 0 : m_screens.firstKey();
    m_level = state.value(QStringLiteral("level")).toInt();
    Q_EMIT changed();
    // No assistant's screen left: nothing to show.
    if (m_screens.isEmpty())
        QCoreApplication::exit(0);
}

void Director::setFocus(int workspace)
{
    apply(bridge({{QStringLiteral("op"), QStringLiteral("director")}, {QStringLiteral("focus"), workspace}}));
}

void Director::close()
{
    // rungic-agent-screen decides per screen (an agent at work: hidden for this task; else closed),
    // in a unit of its own that outlives this window. Closing only the focus left the others out,
    // and the director with them (2026-10-05).
    QProcess::startDetached(QStringLiteral("systemd-run"), {QStringLiteral("--user"), QStringLiteral("--collect"), QStringLiteral("--quiet"),
                                                            QStringLiteral("rungic-agent-screen"), QStringLiteral("dismiss"), QStringLiteral("director")});
    QCoreApplication::exit(0);     // as AgentScreen::close: quit() is cancelled by the fullscreen window
}

void Director::nextLevel()
{
    apply(bridge({{QStringLiteral("op"), QStringLiteral("director")}, {QStringLiteral("level"), (m_level + 1) % 3}}));
}

