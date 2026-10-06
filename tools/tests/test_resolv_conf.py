# SPDX-License-Identifier: MIT
"""The Linux resolver follows Android's default network (shared/platform/network-manager.py, issue #7):
the service writes /etc/resolv.conf from the DNS servers of the default network in each snapshot of
the platform bridge, here a stand-in (tools/contracts.py, quality/contracts/network.json), and leaves
a file someone wrote by hand alone."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import contracts  # noqa: E402

pytest.importorskip('gi.repository.GLib')
from test_contract_network import SNAPSHOT, WIFI, Bus, service  # noqa: E402

VPN = {**WIFI, 'handle': 9, 'interface': 'tun0', 'kind': 'vpn', 'default': True,
       'addresses': ['172.19.0.1/30'], 'dns': ['172.19.0.2'],
       'routes': [{'destination': '0.0.0.0/0', 'default': True}]}


def snapshot(*networks):
    return {**SNAPSHOT, 'networks': list(networks)}


def module_with(monkeypatch, tmp_path):
    with contracts.StandIn(['network', 'telephony'], {'network-get': SNAPSHOT}) as android:
        module = service(monkeypatch, android.path, 'shared/platform/network-manager.py', 'android_network')
    path = tmp_path / 'resolv.conf'
    monkeypatch.setattr(module, 'RESOLV_CONF', str(path))
    monkeypatch.setattr(module, 'LAST_DNS', str(tmp_path / 'state' / 'last-dns'))
    monkeypatch.setattr(module, 'gateway_servers', lambda run=None: ['192.168.31.1'])
    return module, path


# covers: desktop.network/E6
def test_the_default_networks_servers_are_written(monkeypatch, tmp_path):
    module, path = module_with(monkeypatch, tmp_path)
    path.write_text(module.RESOLV_PLACEHOLDER + '\n')               # the image as shipped
    assert module.sync_resolv_conf(snapshot(WIFI)) == 'written'
    assert path.read_text() == f'{module.RESOLV_MARKER}\nnameserver 192.168.5.1\n'
    assert oct(path.stat().st_mode & 0o777) == '0o644'
    assert module.sync_resolv_conf(snapshot(WIFI)) == 'unchanged'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['resolv.conf', 'state']   # no temporary file left
    assert (tmp_path / 'state' / 'last-dns').read_text() == '192.168.5.1\n', 'kept for the next start'


# covers: desktop.network/E6
def test_a_leftover_link_beside_the_file_is_never_written_through(monkeypatch, tmp_path):
    module, path = module_with(monkeypatch, tmp_path)
    victim = tmp_path / 'victim'
    victim.write_text('keep\n')
    (tmp_path / '.resolv.conf.rungic').symlink_to(victim)          # the old fixed temporary name
    assert module.sync_resolv_conf(snapshot(WIFI)) == 'written'
    assert victim.read_text() == 'keep\n' and 'nameserver 192.168.5.1' in path.read_text()


# covers: desktop.network/E6
def test_a_vpn_takes_over_and_its_server_goes_with_it(monkeypatch, tmp_path):
    module, path = module_with(monkeypatch, tmp_path)
    wifi_behind = {**WIFI, 'default': False}
    module.sync_resolv_conf(snapshot(wifi_behind, VPN))
    assert 'nameserver 172.19.0.2\n' in path.read_text() and '192.168.5.1' not in path.read_text()
    module.sync_resolv_conf(snapshot(WIFI))                          # the VPN is off again
    assert '172.19.0.2' not in path.read_text() and 'nameserver 192.168.5.1' in path.read_text()


# covers: desktop.network/E6
def test_no_default_network_for_a_moment_keeps_the_servers(monkeypatch, tmp_path):
    # 2026-10-06: after a reboot with a VPN the resolver was left without a server, and Codex and Raft
    # were offline. Android without a default network for a moment keeps the last servers.
    module, path = module_with(monkeypatch, tmp_path)
    module.sync_resolv_conf(snapshot(VPN))
    assert module.sync_resolv_conf(snapshot({**WIFI, 'default': False})) == 'unchanged'
    assert 'nameserver 172.19.0.2' in path.read_text()


# covers: desktop.network/E6
def test_a_restart_before_android_answers_gets_the_last_servers_never_none(monkeypatch, tmp_path):
    module, path = module_with(monkeypatch, tmp_path)
    module.sync_resolv_conf(snapshot(VPN))
    path.write_text(module.RESOLV_PLACEHOLDER + '\n')               # the image's file, e.g. a new rootfs
    assert module.sync_resolv_conf(None) == 'written'               # Android cannot be asked yet
    text = path.read_text()
    assert text.startswith(module.RESOLV_MARKER) and 'nameserver 172.19.0.2' in text
    # Never any servers from Android: the default gateway Linux sees, until Android gives some.
    (tmp_path / 'state' / 'last-dns').unlink()
    path.write_text('')
    assert module.sync_resolv_conf(None) == 'written'
    assert 'nameserver 192.168.31.1' in path.read_text()
    assert module.sync_resolv_conf(snapshot(WIFI)) == 'written'
    assert 'nameserver 192.168.5.1' in path.read_text() and '192.168.31.1' not in path.read_text()


# covers: desktop.network/E6
def test_the_gateway_is_read_from_the_routes_wifi_first(monkeypatch, tmp_path):
    import types
    module, _path = module_with(monkeypatch, tmp_path)
    routes = ('default via 10.0.0.1 dev rmnet_data0 table 1020\n'
              'default via 192.168.31.1 dev wlan0 table 1017 proto static\n')
    assert module.real_gateway_servers(lambda *a, **k: types.SimpleNamespace(stdout=routes)) == ['192.168.31.1']
    assert module.real_gateway_servers(lambda *a, **k: types.SimpleNamespace(stdout='')) == []


# covers: desktop.network/E6
def test_servers_are_checked_limited_and_link_local_keeps_its_interface(monkeypatch, tmp_path):
    module, path = module_with(monkeypatch, tmp_path)
    row = {**WIFI, 'dns': ['fe80::1%wlan0', 'not-an-address', '2001:db8::53', '192.168.5.1',
                           '192.168.5.1', '9.9.9.9', '1.1.1.1', '8.8.8.8%bad;scope']}
    module.sync_resolv_conf(snapshot(row))
    assert path.read_text().splitlines()[1:] == [
        'nameserver fe80::1%wlan0', 'nameserver 2001:db8::53', 'nameserver 192.168.5.1']


# covers: desktop.network/E7
def test_a_file_edited_by_hand_is_left_alone(monkeypatch, tmp_path):
    module, path = module_with(monkeypatch, tmp_path)
    path.write_text('nameserver 223.5.5.5\nnameserver 1.1.1.1\n')    # the G100 S, 2026-09-23
    assert module.sync_resolv_conf(snapshot(VPN)) == 'kept'
    assert path.read_text() == 'nameserver 223.5.5.5\nnameserver 1.1.1.1\n'
    link = tmp_path / 'link.conf'
    link.symlink_to(path)                                            # e.g. systemd-resolved's stub
    assert module.sync_resolv_conf(snapshot(VPN), str(link)) == 'kept'


# covers: desktop.network/E6
def test_the_service_writes_on_each_snapshot_and_not_without_one(monkeypatch, tmp_path):
    module, path = module_with(monkeypatch, tmp_path)
    bridge = module.Bridge(Bus())                                    # publish(None): Android not reachable
    assert 'nameserver 192.168.31.1' in path.read_text(), 'never left without a server'
    bridge.publish(snapshot(VPN))
    assert 'nameserver 172.19.0.2' in path.read_text()
    bridge.publish(None)                                             # unreachable again: keep what is there
    assert 'nameserver 172.19.0.2' in path.read_text()
