// SPDX-License-Identifier: MIT
// Settings -> Services (docs/83): the services the container keeps off (service-policy.json), why,
// and their current state; turning one on or off goes through the KAuth helper
// (serviceshelper.cpp). Masks that are not in the policy are listed too: Ubuntu's own in /usr/lib
// (read-only) and any made in /etc (can be removed).

#include <KAuth/Action>
#include <KAuth/ExecuteJob>
#include <KLocalizedString>
#include <KPluginFactory>
#include <KQuickConfigModule>
#include <KUser>

#include <QDBusArgument>
#include <QDBusInterface>
#include <QDBusReply>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkInterface>
#include <QProcess>
#include <QSet>
#include <QTimer>

using namespace Qt::StringLiterals;

namespace
{
const auto POLICY = u"/usr/share/rungic/service-policy.json"_s;

bool isMask(const QString &path)
{
    const QFileInfo info(path);
    return info.isSymLink() && info.symLinkTarget() == u"/dev/null";
}

QStringList strings(const QJsonValue &value)
{
    QStringList result;
    for (const auto &item : value.toArray()) {
        result << item.toString();
    }
    return result;
}

// The name, summary and warning of each group in service-policy.json are English messages of
// this KCM's catalog (po/zh_CN/kcm_rungic_services.po, the entries marked "#: policy.json").
QString translated(const QJsonValue &value)
{
    const auto text = value.toString();
    return text.isEmpty() ? text : i18n(text.toUtf8().constData());
}

// A user service, as listed on the page.
QString userUnit(const QString &unit)
{
    return i18nc("@item a systemd user service", "%1 (user)", unit);
}

QString run(const QString &program, const QStringList &args)
{
    QProcess process;
    process.start(program, args);
    process.waitForFinished(10000);
    return QString::fromUtf8(process.readAllStandardOutput());
}

// systemctl show for the units: {unit: {property: value}}, in one call per scope.
QHash<QString, QHash<QString, QString>> show(const QStringList &units, bool user)
{
    QHash<QString, QHash<QString, QString>> result;
    if (units.isEmpty()) {
        return result;
    }
    QStringList args;
    if (user) {
        args << u"--user"_s;
    }
    args << u"show"_s << u"--property=Id,LoadState,ActiveState,UnitFileState"_s << u"--"_s << units;
    // One block per unit, in the order given, separated by an empty line.
    const auto blocks = run(u"systemctl"_s, args).split(u"\n\n"_s, Qt::SkipEmptyParts);
    for (int i = 0; i < blocks.size() && i < units.size(); ++i) {
        QHash<QString, QString> properties;
        for (const auto &line : blocks[i].split(u'\n', Qt::SkipEmptyParts)) {
            properties.insert(line.section(u'=', 0, 0), line.section(u'=', 1));
        }
        result.insert(units[i], properties);
    }
    return result;
}

QStringList masksIn(const QString &directory)
{
    QStringList result;
    for (const auto &info : QDir(directory).entryInfoList(QDir::Files | QDir::System | QDir::NoDotAndDotDot, QDir::Name)) {
        if (isMask(info.filePath())) {
            result << info.fileName();
        }
    }
    return result;
}
}

// The phone's IPv4 addresses as NetworkManager has them (the Android network bridge): where the
// local network reaches SSH. With Linux's own network (docs/116) Linux's interfaces show only
// pasta's internal link; pasta forwards port 22 from the phone's addresses. -> "address  interface".
static QStringList phoneAddresses()
{
    QStringList result;
    const auto nm = u"org.freedesktop.NetworkManager"_s;
    QDBusInterface manager(nm, u"/org/freedesktop/NetworkManager"_s, nm, QDBusConnection::systemBus());
    if (!manager.isValid()) {
        return result;
    }
    const QDBusReply<QList<QDBusObjectPath>> devices = manager.call(u"GetDevices"_s);
    if (!devices.isValid()) {
        return result;
    }
    for (const auto &device : devices.value()) {
        QDBusInterface properties(nm, device.path(), u"org.freedesktop.DBus.Properties"_s, QDBusConnection::systemBus());
        const QDBusReply<QDBusVariant> name = properties.call(u"Get"_s, nm + u".Device"_s, u"Interface"_s);
        const QDBusReply<QDBusVariant> config = properties.call(u"Get"_s, nm + u".Device"_s, u"Ip4Config"_s);
        if (!name.isValid() || !config.isValid()) {
            continue;
        }
        const auto path = qvariant_cast<QDBusObjectPath>(config.value().variant()).path();
        if (path.isEmpty() || path == u"/"_s) {
            continue;
        }
        QDBusInterface ip4(nm, path, u"org.freedesktop.DBus.Properties"_s, QDBusConnection::systemBus());
        const QDBusReply<QDBusVariant> data = ip4.call(u"Get"_s, nm + u".IP4Config"_s, u"AddressData"_s);
        if (!data.isValid()) {
            continue;
        }
        QList<QVariantMap> entries;
        data.value().variant().value<QDBusArgument>() >> entries;
        for (const auto &entry : entries) {
            const auto address = entry.value(u"address"_s).toString();
            if (!address.isEmpty()) {
                result << u"%1  %2"_s.arg(address, name.value().variant().toString());
            }
        }
    }
    return result;
}

class ServicesSettings : public KQuickConfigModule
{
    Q_OBJECT
    Q_PROPERTY(QVariantList groups MEMBER m_groups NOTIFY changed)
    Q_PROPERTY(QStringList distributionMasks MEMBER m_distributionMasks NOTIFY changed)
    Q_PROPERTY(QVariantList otherMasks MEMBER m_otherMasks NOTIFY changed)
    Q_PROPERTY(QStringList addresses MEMBER m_addresses NOTIFY changed)
    Q_PROPERTY(QString userName MEMBER m_userName CONSTANT)
    Q_PROPERTY(QString hostKey MEMBER m_hostKey NOTIFY changed)
    Q_PROPERTY(bool busy MEMBER m_busy NOTIFY busyChanged)
    Q_PROPERTY(QString error MEMBER m_error NOTIFY errorChanged)

public:
    ServicesSettings(QObject *parent, const KPluginMetaData &data)
        : KQuickConfigModule(parent, data)
        , m_userName(KUser().loginName())
    {
        setButtons(NoAdditionalButton);
        refresh();
    }

    Q_INVOKABLE void refresh()
    {
        QFile file(POLICY);
        QJsonArray policy;
        if (file.open(QIODevice::ReadOnly)) {
            policy = QJsonDocument::fromJson(file.readAll()).object().value(u"groups"_s).toArray();
        } else {
            setError(i18n("Cannot find the service list %1", POLICY));
        }
        QStringList systemUnits, userUnits;
        QSet<QString> listed;
        for (const auto &value : policy) {
            const auto group = value.toObject();
            for (const auto &unit : strings(group.value(u"system"_s))) {
                systemUnits << unit;
                listed.insert(u"system/"_s + unit);
            }
            for (const auto &unit : strings(group.value(u"user"_s))) {
                userUnits << unit;
                listed.insert(u"user/"_s + unit);
            }
        }
        const auto systemState = show(systemUnits, false);
        const auto userState = show(userUnits, true);

        m_groups.clear();
        for (const auto &value : policy) {
            const auto group = value.toObject();
            // Optional (off or on by default: enabled and disabled), or masked (masked and unmasked).
            const auto defaultState = group.value(u"default"_s).toString();
            const bool disabledKind = defaultState == u"disabled" || defaultState == u"enabled";
            const auto start = strings(group.value(u"start"_s));
            int masked = 0, total = 0, active = 0, enabled = 0, missing = 0;
            QStringList units, userRun;
            bool hasUser = false;
            for (const auto &unit : strings(group.value(u"user"_s))) {
                if (start.isEmpty() || start.contains(unit)) {
                    userRun << unit;
                }
            }
            for (const auto &scope : {u"system"_s, u"user"_s}) {
                for (const auto &unit : strings(group.value(scope))) {
                    const auto state = scope == u"system" ? systemState.value(unit) : userState.value(unit);
                    ++total;
                    units << (scope == u"user" ? userUnit(unit) : unit);
                    hasUser |= scope == u"user";
                    if (isMask(u"/etc/systemd/%1/%2"_s.arg(scope, unit))) {
                        ++masked;
                    }
                    if (state.value(u"LoadState"_s) == u"not-found") {
                        ++missing;
                    }
                    if (state.value(u"ActiveState"_s) == u"active") {
                        ++active;
                    }
                    if ((start.isEmpty() || start.contains(unit)) && state.value(u"UnitFileState"_s).startsWith(u"enabled"_s)) {
                        ++enabled;
                    }
                }
            }
            QString status;
            bool on;
            if (disabledKind) {
                on = enabled > 0 && masked == 0;
                status = masked ? i18nc("@info:status", "Masked")
                    : active   ? i18nc("@info:status", "Running")
                    : on       ? i18nc("@info:status", "On, not running")
                               : i18nc("@info:status", "Off");
            } else {
                on = masked == 0;
                status = masked == total ? i18nc("@info:status", "Masked")
                    : masked                ? i18nc("@info:status", "Partly masked")
                    : missing == total      ? i18nc("@info:status", "Allowed (not installed)")
                    : active                ? i18nc("@info:status", "Allowed, running")
                                            : i18nc("@info:status", "Allowed, not running");
            }
            m_groups << QVariantMap{
                {u"id"_s, group.value(u"id"_s).toString()},
                {u"name"_s, translated(group.value(u"name"_s))},
                {u"summary"_s, translated(group.value(u"summary"_s))},
                {u"warning"_s, translated(group.value(u"warning"_s))},
                {u"risk"_s, group.value(u"risk"_s).toString()},
                {u"evidence"_s, group.value(u"evidence"_s).toString()},
                {u"kind"_s, disabledKind ? u"optional"_s : u"masked"_s},
                {u"units"_s, units},
                {u"user"_s, hasUser},
                // An optional group's user services start or stop with the switch, in this session too.
                {u"userRun"_s, disabledKind ? userRun : QStringList()},
                {u"on"_s, on},
                {u"status"_s, status},
            };
        }

        m_distributionMasks = masksIn(u"/usr/lib/systemd/system"_s);
        for (const auto &unit : masksIn(u"/usr/lib/systemd/user"_s)) {
            m_distributionMasks << userUnit(unit);
        }
        m_otherMasks.clear();
        for (const auto &scope : {u"system"_s, u"user"_s}) {
            for (const auto &unit : masksIn(u"/etc/systemd/"_s + scope)) {
                if (!listed.contains(scope + u'/' + unit)) {
                    m_otherMasks << QVariantMap{{u"unit"_s, unit}, {u"scope"_s, scope}};
                }
            }
        }

        m_addresses = phoneAddresses();
        for (const auto &interface : m_addresses.isEmpty() ? QNetworkInterface::allInterfaces() : QList<QNetworkInterface>{}) {
            const auto flags = interface.flags();
            if (!(flags & QNetworkInterface::IsUp) || !(flags & QNetworkInterface::IsRunning) || (flags & QNetworkInterface::IsLoopBack)) {
                continue;
            }
            for (const auto &entry : interface.addressEntries()) {
                // Not the link of Linux's own network (docs/116): only Android reaches that address.
                if (entry.ip().protocol() == QAbstractSocket::IPv4Protocol && entry.ip().toString() != u"10.0.2.15"_s) {
                    m_addresses << u"%1  %2"_s.arg(entry.ip().toString(), interface.name());
                }
            }
        }
        // "256 SHA256:... root@host (ED25519)": what an SSH client shows on the first connection.
        m_hostKey.clear();
        const auto key = u"/etc/ssh/ssh_host_ed25519_key.pub"_s;
        if (QFile::exists(key)) {
            m_hostKey = run(u"ssh-keygen"_s, {u"-l"_s, u"-f"_s, key}).section(u' ', 1, 1);
        }
        Q_EMIT changed();
    }

    Q_INVOKABLE void setEnabled(const QString &group, bool enabled)
    {
        bool user = false;
        QStringList userRun;
        for (const auto &value : std::as_const(m_groups)) {
            const auto map = value.toMap();
            if (map.value(u"id"_s) == group) {
                user = map.value(u"user"_s).toBool();
                userRun = map.value(u"userRun"_s).toStringList();
            }
        }
        QStringList after;
        if (!userRun.isEmpty()) {
            after = QStringList{u"--user"_s, u"--no-block"_s, enabled ? u"start"_s : u"stop"_s, u"--"_s} + userRun;
        }
        execute(u"com.rungic.services.set"_s, {{u"group"_s, group}, {u"enabled"_s, enabled}}, user, after);
    }

    Q_INVOKABLE void unmask(const QString &unit, const QString &scope)
    {
        execute(u"com.rungic.services.unmask"_s, {{u"unit"_s, unit}, {u"scope"_s, scope}}, scope == u"user");
    }

Q_SIGNALS:
    void changed();
    void busyChanged();
    void errorChanged();

private:
    void setError(const QString &error)
    {
        m_error = error;
        Q_EMIT errorChanged();
    }

    void execute(const QString &name, const QVariantMap &args, bool userUnits, const QStringList &after = {})
    {
        KAuth::Action action(name);
        action.setHelperId(u"com.rungic.services"_s);
        action.setArguments(args);
        // The helper asks polkit, whose password dialog waits inside this D-Bus call; with the
        // default 25 s reply timeout a slower password entry was reported as a failure although
        // the helper went on to make the change (docs/85).
        action.setTimeout(10 * 60 * 1000);
        auto *job = action.execute();
        m_busy = true;
        Q_EMIT busyChanged();
        setError({});
        connect(job, &KJob::result, this, [this, job, userUnits, after] {
            const bool failed = job->error() && job->error() != KAuth::ActionReply::UserCancelledError;
            if (failed) {
                setError(job->errorText().isEmpty() ? job->errorString() : job->errorText());
            }
            if (userUnits) {
                // The helper changed /etc/systemd/user; this user's manager loads it again.
                run(u"systemctl"_s, {u"--user"_s, u"daemon-reload"_s});
            }
            if (!job->error() && !after.isEmpty()) {
                // Enabling a user service globally only takes effect at the next login.
                run(u"systemctl"_s, after);
            }
            m_busy = false;
            Q_EMIT busyChanged();
            refresh();
            // sshd-keygen runs next to ssh.socket: the host key can appear a moment later.
            QTimer::singleShot(2000, this, &ServicesSettings::refresh);
        });
        job->start();
    }

    QVariantList m_groups;
    QStringList m_distributionMasks;
    QVariantList m_otherMasks;
    QStringList m_addresses;
    QString m_userName;
    QString m_hostKey;
    bool m_busy = false;
    QString m_error;
};

K_PLUGIN_CLASS_WITH_JSON(ServicesSettings, "kcm_rungic_services.json")

#include "servicessettings.moc"
