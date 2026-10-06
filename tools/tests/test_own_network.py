# SPDX-License-Identifier: MIT
"""Linux's own network (shared/platform/own-network.py, docs/116): pasta runs as the app's uid with
only the capabilities it needs, the relays pass on only Linux's root and user, the status reads
pasta's route, and the switch is written where the Android side reads it."""
import importlib.machinery
import importlib.util
import socket
import struct
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load():
    path = ROOT / 'shared/platform/own-network.py'
    loader = importlib.machinery.SourceFileLoader('own_network', str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader('own_network', loader))
    loader.exec_module(module)
    return module


# covers: desktop.network/E9
def test_pasta_runs_as_the_app_with_its_link_and_ports():
    argv = load().pasta_argv(10353, '/run/x/netns')
    assert argv[:4] == ['setpriv', '--reuid=10353', '--regid=10353', '--clear-groups']
    caps = '+sys_admin,+net_admin,+net_bind_service'
    assert f'--inh-caps={caps}' in argv and f'--ambient-caps={caps}' in argv
    assert '--runas' not in argv, 'pasta drops to the uid before entering the namespace and then cannot'
    tail = argv[argv.index('pasta'):]
    assert tail[tail.index('--netns') + 1] == '/run/x/netns'
    for flag, value in (('-I', 'eth0'), ('-a', '10.0.2.15'), ('-g', '10.0.2.2'), ('-t', '22'), ('-T', 'auto')):
        assert tail[tail.index(flag) + 1] == value


# covers: desktop.network/E9
def test_a_relay_passes_on_root_and_the_user_and_refuses_others(monkeypatch):
    module = load()
    for uid, relayed in ((0, True), (1000, True), (1001, False)):
        client, peer = socket.socketpair()
        upstream, service = socket.socketpair()
        monkeypatch.setattr(module, 'peer_uid', lambda _conn, uid=uid: uid)
        connected = []
        thread = threading.Thread(target=module.relay_connection,
                                  args=(client, 'com.rungic.device.v1', lambda name: connected.append(name) or upstream))
        thread.start()
        if relayed:
            peer.sendall(b'ping')
            assert service.recv(4) == b'ping'
            service.sendall(b'pong')
            assert peer.recv(4) == b'pong'
            peer.close()
            service.close()
        thread.join(5)
        assert not thread.is_alive()
        assert connected == (['com.rungic.device.v1'] if relayed else [])
        for s in (peer, upstream, service):
            s.close()


# covers: desktop.network/E9
def test_status_reads_pastas_default_route():
    module = load()
    gateway = format(struct.unpack('<I', socket.inet_aton('10.0.2.2'))[0], '08X')
    header = 'Iface\tDestination\tGateway\tFlags\n'
    assert module.default_route_via_gateway(header + f'eth0\t00000000\t{gateway}\t0003\n')
    assert not module.default_route_via_gateway(header + 'wlan0\t00000000\t011FA8C0\t0003\n')
    assert not module.default_route_via_gateway(header)


# covers: desktop.network/E9
def test_the_switch_is_written_for_the_next_start(monkeypatch, tmp_path):
    module = load()
    monkeypatch.setattr(module, 'MODE_FILE', str(tmp_path / 'host' / 'network-mode'))
    module.mode(True)
    assert (tmp_path / 'host' / 'network-mode').read_text() == 'own\n'
    module.mode(False)
    assert (tmp_path / 'host' / 'network-mode').read_text() == 'shared\n'


def test_the_host_daemon_needs_an_app_uid():
    module = load()
    for argv in (['x', 'host'], ['x', 'host', '--app-uid', '0']):
        try:
            module.main(argv)
        except SystemExit as stop:
            assert stop.code
        else:
            raise AssertionError(argv)
