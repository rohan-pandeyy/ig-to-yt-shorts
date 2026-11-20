"""Startup checks, LAN address choice, QR rendering, bootstrap. Run: .venv/bin/python -m unittest -v"""

import io
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bootstrap  # noqa: E402
import startup  # noqa: E402
import worker  # noqa: E402

PORT = 8080


class VersionAndDependencyTest(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(startup.check_versions((3, 15, 0), '3.53.4'), [])
        self.assertEqual(startup.check_versions((3, 11, 2), '3.35.0'), [])
        self.assertIn('Python 3.11', startup.check_versions((3, 10, 9), '3.45.0')[0])
        self.assertIn('SQLite 3.35.0', startup.check_versions((3, 12, 0), '3.31.1')[0])

    def test_find_ffmpeg(self):
        self.assertEqual(startup.find_ffmpeg('local.exe', which=lambda _: '/usr/bin/ffmpeg',
                                             exists=lambda _: False), '/usr/bin/ffmpeg')
        self.assertEqual(startup.find_ffmpeg('local.exe', which=lambda _: None,
                                             exists=lambda p: p == 'local.exe'), 'local.exe')
        self.assertIsNone(startup.find_ffmpeg('local.exe', which=lambda _: None, exists=lambda _: False))

    def test_ffmpeg_instructions_per_os(self):
        self.assertIn('dnf install ffmpeg-free', startup.ffmpeg_instructions('Linux', {'ID': 'fedora'}))
        self.assertIn('apt install ffmpeg',
                      startup.ffmpeg_instructions('Linux', {'ID': 'linuxmint', 'ID_LIKE': 'ubuntu debian'}))
        self.assertIn('pacman -S ffmpeg', startup.ffmpeg_instructions('Linux', {'ID': 'arch'}))
        self.assertIn('package manager', startup.ffmpeg_instructions('Linux', {}))
        win = startup.ffmpeg_instructions('Windows')
        self.assertIn('winget install --id Gyan.FFmpeg', win)
        self.assertNotIn('sudo', win)

    def test_client_secrets(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, 'client_secrets.json')
        self.assertIn('missing', startup.check_client_secrets(path))
        with open(path, 'w') as f:
            f.write('{not json')
        self.assertIn('not valid JSON', startup.check_client_secrets(path))
        with open(path, 'w') as f:
            json.dump({'web': {}}, f)
        self.assertIn("'web' client", startup.check_client_secrets(path))
        with open(path, 'w') as f:
            json.dump({'installed': {'client_id': 'x'}}, f)
        self.assertIsNone(startup.check_client_secrets(path))


class PortTest(unittest.TestCase):
    def test_port_available(self):
        import socket
        with socket.socket() as busy:
            busy.bind(('127.0.0.1', 0))
            busy.listen()
            port = busy.getsockname()[1]
            self.assertFalse(startup.port_available('127.0.0.1', port))
        self.assertTrue(startup.port_available('127.0.0.1', port))


class LanAddressTest(unittest.TestCase):
    def choose(self, addresses, route):
        return startup.choose_lan_address(addresses, route)

    def test_wifi_with_docker_bridges(self):
        addrs = [('127.0.0.1', 'lo'), ('172.17.0.1', 'docker0'), ('172.18.0.1', 'br-c06d61661689'),
                 ('192.168.1.24', 'wlp0s20f3')]
        self.assertEqual(self.choose(addrs, '192.168.1.24'), ('192.168.1.24', [], []))

    def test_default_route_preferred_and_others_listed(self):
        addrs = [('10.0.0.5', 'enp3s0'), ('192.168.1.24', 'wlp0s20f3')]
        chosen, others, warnings = self.choose(addrs, '10.0.0.5')
        self.assertEqual((chosen, others), ('10.0.0.5', ['192.168.1.24']))
        self.assertIn('several networks', warnings[0])

    def test_vpn_default_route_is_not_used(self):
        addrs = [('10.8.0.2', 'tun0'), ('192.168.1.24', 'wlp0s20f3')]
        self.assertEqual(self.choose(addrs, '10.8.0.2')[0], '192.168.1.24')

    def test_windows_style_without_interface_names(self):
        addrs = [('192.168.56.1', None), ('192.168.1.40', None)]   # VirtualBox host-only + Wi-Fi
        chosen, others, _ = self.choose(addrs, '192.168.1.40')
        self.assertEqual((chosen, others), ('192.168.1.40', ['192.168.56.1']))

    def test_route_ip_only_known_from_route(self):
        self.assertEqual(self.choose([], '192.168.0.7')[0], '192.168.0.7')

    def test_no_lan_address(self):
        addrs = [('127.0.0.1', 'lo'), ('172.17.0.1', 'docker0'), ('169.254.10.2', 'eth0')]
        chosen, others, warnings = self.choose(addrs, None)
        self.assertIsNone(chosen)
        self.assertIn("Couldn't find a Wi-Fi/LAN address", warnings[0])
        self.assertIn('172.17.0.1', warnings[0])               # shows what it did find

    def test_dashboard_urls(self):
        addrs = [('192.168.1.24', 'wlp0s20f3')]
        self.assertEqual(startup.dashboard_urls('0.0.0.0', PORT, addrs, '192.168.1.24'),
                         ('http://localhost:8080', 'http://192.168.1.24:8080', [], []))
        local, network, _, warnings = startup.dashboard_urls('127.0.0.1', PORT, addrs, '192.168.1.24')
        self.assertIsNone(network)
        self.assertIn("other devices can't connect", warnings[0])
        self.assertEqual(startup.dashboard_urls('192.168.1.24', 9000)[1], 'http://192.168.1.24:9000')
        _, _, others, warnings = startup.dashboard_urls(
            '0.0.0.0', 8123, [('10.0.0.5', 'eth0'), ('192.168.1.24', 'wlan0')], '10.0.0.5')
        self.assertEqual(others, ['http://192.168.1.24:8123'])
        self.assertIn('http://192.168.1.24:8123', warnings[0])   # port filled in

    def test_real_machine_does_not_crash(self):
        addrs = startup.list_ipv4_addresses()
        self.assertIsInstance(addrs, list)
        startup.dashboard_urls('0.0.0.0', PORT)


def parse_half_blocks(text, ansi):
    """Turn rendered half-block text back into a dark/light matrix."""
    if ansi:
        text = re.sub(r'\x1b\[[0-9;]*m', '', text)
    light = {'█': (True, True), '▀': (True, False), '▄': (False, True), ' ': (False, False)}
    rows = []
    for line in text.split('\n'):
        top, bottom = zip(*(light[c] for c in line))
        rows += [[not t for t in top], [not b for b in bottom]]
    return rows


class QrTest(unittest.TestCase):
    URL = 'http://192.168.1.24:8080/'

    def test_half_block_rendering_matches_the_qr_matrix(self):
        matrix = startup.qr_matrix(self.URL)
        for mode in ('blocks', 'ansi'):
            rows = parse_half_blocks(startup.render_qr(self.URL, mode), mode == 'ansi')
            self.assertEqual(rows[:len(matrix)], matrix, mode)
            if len(rows) > len(matrix):                       # odd size: padded with dark
                self.assertTrue(all(rows[-1]))

    def test_quiet_zone_is_light(self):
        matrix = startup.qr_matrix(self.URL)
        self.assertFalse(any(matrix[0]) or any(row[0] for row in matrix))

    def test_qr_mode(self):
        class Stream(io.StringIO):
            def __init__(self, encoding, tty):
                super().__init__()
                self._enc, self._tty = encoding, tty

            @property
            def encoding(self):
                return self._enc

            def isatty(self):
                return self._tty

        self.assertEqual(startup.qr_mode(Stream('cp1252', True)), 'none')
        self.assertEqual(startup.qr_mode(Stream('utf-8', False)), 'blocks')     # piped / logged
        if os.name != 'nt':
            self.assertEqual(startup.qr_mode(Stream('utf-8', True)), 'ansi')
            os.environ['NO_COLOR'] = '1'
            try:
                self.assertEqual(startup.qr_mode(Stream('utf-8', True)), 'blocks')
            finally:
                del os.environ['NO_COLOR']

    def test_banner(self):
        text = startup.banner('http://localhost:8080', 'http://192.168.1.24:8080', [], [], 'blocks')
        self.assertIn('Local:   http://localhost:8080', text)
        self.assertIn('Network: http://192.168.1.24:8080', text)
        self.assertEqual(text.count('Scan this QR code'), 1)
        self.assertNotRegex(text.lower(), r'password|token|secret')
        no_lan = startup.banner('http://localhost:8080', None, [], ['No LAN'], 'blocks')
        self.assertNotIn('Scan this QR code', no_lan)
        self.assertIn('Warning: No LAN', no_lan)
        plain = startup.banner('http://localhost:8080', 'http://192.168.1.24:8080', [], [], 'none')
        self.assertNotIn('Scan this QR code', plain)
        self.assertIn("can't display a QR code", plain)
        plain.encode('ascii')                                  # printable on any terminal


class BootstrapTest(unittest.TestCase):
    def test_install_only_when_requirements_change_or_package_missing(self):
        d = tempfile.mkdtemp()
        req, stamp = os.path.join(d, 'requirements.txt'), os.path.join(d, 'stamp')
        with open(req, 'w') as f:
            f.write('flask\n')
        present = lambda name: object()  # noqa: E731
        self.assertTrue(bootstrap.needs_install(req, stamp, present))         # never installed
        with open(stamp, 'w') as f:
            f.write(bootstrap.requirements_hash(req))
        self.assertFalse(bootstrap.needs_install(req, stamp, present))        # up to date
        self.assertTrue(bootstrap.needs_install(req, stamp, lambda name: None))   # package missing
        with open(req, 'a') as f:
            f.write('qrcode\n')
        self.assertTrue(bootstrap.needs_install(req, stamp, present))         # requirements changed


class SingleInstanceTest(unittest.TestCase):
    def test_second_instance_refused_until_first_stops(self):
        lock = os.path.join(tempfile.mkdtemp(), 'lock')
        first = worker.Worker(lock_path=lock)
        first.acquire_lock()
        with self.assertRaises(RuntimeError) as ctx:
            worker.Worker(lock_path=lock).acquire_lock()
        self.assertIn('already running', str(ctx.exception))
        first.stop()
        again = worker.Worker(lock_path=lock)
        again.acquire_lock()
        again.stop()


if __name__ == '__main__':
    unittest.main()
