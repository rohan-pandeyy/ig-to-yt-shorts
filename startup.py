"""
startup.py
----------
Startup checks and the "dashboard is running" banner (URLs + terminal QR code).
Used by server.py; kept free of side effects so it can be tested.
"""

import ipaddress
import json
import os
import platform
import shutil
import socket
import sqlite3
import sys

MIN_PYTHON = (3, 11)          # yt-dlp requires 3.11+
MIN_SQLITE = (3, 35, 0)       # UPDATE ... RETURNING

# Interface name prefixes that are never the Wi-Fi/Ethernet a phone can reach.
VIRTUAL_IFACE_PREFIXES = ('lo', 'docker', 'br-', 'veth', 'virbr', 'vmnet', 'vboxnet', 'tun', 'tap',
                          'wg', 'tailscale', 'zt', 'lxc', 'lxd', 'cni', 'flannel', 'podman', 'vEthernet')


# ---------- environment checks ----------

def check_versions(version_info=None, sqlite_version=None):
    """Return a list of problems (empty if fine)."""
    version_info = version_info or sys.version_info
    sqlite_version = sqlite_version or sqlite3.sqlite_version
    problems = []
    if tuple(version_info[:2]) < MIN_PYTHON:
        problems.append(f"Python {'.'.join(map(str, MIN_PYTHON))} or newer is required "
                        f"(this is {version_info[0]}.{version_info[1]}).")
    if tuple(int(x) for x in sqlite_version.split('.')[:3]) < MIN_SQLITE:
        problems.append(f"SQLite {'.'.join(map(str, MIN_SQLITE))} or newer is required "
                        f"(this Python has {sqlite_version}). Install a newer Python.")
    return problems


def find_ffmpeg(local_exe, which=shutil.which, exists=os.path.exists):
    """Path to ffmpeg on PATH or the project's local copy, else None."""
    return which('ffmpeg') or (local_exe if exists(local_exe) else None)


def _os_release():
    try:
        with open('/etc/os-release', encoding='utf-8') as f:
            pairs = (line.rstrip().split('=', 1) for line in f if '=' in line)
            return {k: v.strip('"') for k, v in pairs}
    except OSError:
        return {}


def ffmpeg_instructions(system=None, os_release=None):
    system = system or platform.system()
    if system == 'Windows':
        return ("FFmpeg was not found. Install it, then open a NEW terminal and run start.bat again:\n"
                "    winget install --id Gyan.FFmpeg -e\n"
                "  (or download a build from https://www.gyan.dev/ffmpeg/builds/ and add its bin folder to PATH)")
    if system == 'Darwin':
        return "FFmpeg was not found. Install it with:  brew install ffmpeg"
    rel = os_release if os_release is not None else _os_release()
    ids = {rel.get('ID', '')} | set(rel.get('ID_LIKE', '').split())
    if ids & {'fedora', 'rhel', 'centos'}:
        cmd = "sudo dnf install ffmpeg-free     (or 'ffmpeg' from RPM Fusion)"
    elif ids & {'debian', 'ubuntu'}:
        cmd = "sudo apt install ffmpeg"
    elif ids & {'arch'}:
        cmd = "sudo pacman -S ffmpeg"
    elif ids & {'opensuse', 'suse'}:
        cmd = "sudo zypper install ffmpeg"
    else:
        cmd = "install the 'ffmpeg' package with your distribution's package manager"
    return f"FFmpeg was not found. Install it with:\n    {cmd}\n  then run start.sh again."


def check_client_secrets(path):
    """Return a problem message, or None if the OAuth client file looks right."""
    help_text = ("Follow 'Google API Setup' in README.md: create a Desktop-app OAuth client and save "
                 f"its JSON as:\n    {path}")
    if not os.path.exists(path):
        return "YouTube credentials (client_secrets.json) are missing.\n  " + help_text
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return "client_secrets.json is not valid JSON.\n  " + help_text
    if 'installed' not in data:
        kind = next(iter(data), 'unknown') if isinstance(data, dict) else 'unknown'
        return (f"client_secrets.json is a '{kind}' client; it must be a 'Desktop app' client.\n  "
                + help_text)
    return None


def port_available(host, port):
    """True if nothing else is listening on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != 'nt':   # same option the web server uses; ignores sockets in TIME_WAIT
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host or '0.0.0.0', port))
            return True
        except OSError:
            return False


# ---------- network addresses ----------

def default_route_ip():
    """The address this PC uses to reach other networks (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('10.255.255.255', 1))
            return s.getsockname()[0]
    except OSError:
        return None


def list_ipv4_addresses():
    """[(ip, interface name or None)] for this machine, best effort, no extra packages."""
    found = {}
    if hasattr(socket, 'if_nameindex') and sys.platform.startswith('linux'):
        import fcntl
        import struct
        for _, name in socket.if_nameindex():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    packed = fcntl.ioctl(s.fileno(), 0x8915,  # SIOCGIFADDR
                                         struct.pack('256s', name[:15].encode()))
                found.setdefault(socket.inet_ntoa(packed[20:24]), name)
            except OSError:
                pass                              # interface has no IPv4 address
    try:  # Windows (and macOS) list all adapter addresses for the host name
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.setdefault(info[4][0], None)
    except OSError:
        pass
    return sorted(found.items())


def _is_lan(ip):
    addr = ipaddress.ip_address(ip)
    return addr.is_private and not addr.is_loopback and not addr.is_link_local


def _is_virtual(iface):
    return bool(iface) and iface.startswith(VIRTUAL_IFACE_PREFIXES)


def _lan_rank(ip):
    # Home Wi-Fi is almost always 192.168.x.x, then 10.x, then 172.16-31.x (often Docker).
    first = ip.split('.')[0]
    return {'192': 0, '10': 1, '172': 2}.get(first, 3)


def choose_lan_address(addresses, route_ip):
    """Pick the address a phone on the same Wi-Fi should use.

    Returns (chosen_ip or None, other_lan_ips, warnings).
    """
    usable = [ip for ip, iface in addresses if _is_lan(ip) and not _is_virtual(iface)]
    if route_ip and _is_lan(route_ip) and route_ip not in usable \
            and not any(ip == route_ip and _is_virtual(i) for ip, i in addresses):
        usable.append(route_ip)                  # e.g. Windows, where interface names are unknown
    usable.sort(key=lambda ip: (ip != route_ip, _lan_rank(ip), ip))
    warnings = []
    if not usable:
        seen = [ip for ip, _ in addresses if not ipaddress.ip_address(ip).is_loopback]
        warnings.append("Couldn't find a Wi-Fi/LAN address for this PC, so phones may not reach the "
                        "dashboard. Check that this PC is connected to your Wi-Fi or router."
                        + (f" Addresses found: {', '.join(seen)}" if seen else ""))
        return None, [], warnings
    chosen, others = usable[0], usable[1:]
    if others:
        warnings.append(f"This PC is on several networks. The QR code uses {chosen}; if your phone "
                        f"can't connect, try: " + ', '.join(f'http://{ip}:<port>' for ip in others))
    return chosen, others, warnings


def dashboard_urls(host, port, addresses=None, route_ip=None):
    """Return (local_url, network_url or None, other_urls, warnings) for the banner."""
    local = f"http://localhost:{port}"
    if host in ('127.0.0.1', 'localhost', '::1'):
        return local, None, [], [f"Listening on {host} only: other devices can't connect. "
                                 "Start without --host to allow your phone."]
    if host not in ('0.0.0.0', '', '::'):
        return local, f"http://{host}:{port}", [], []
    addresses = list_ipv4_addresses() if addresses is None else addresses
    route_ip = default_route_ip() if route_ip is None else route_ip
    chosen, others, warnings = choose_lan_address(addresses, route_ip)
    warnings = [w.replace('<port>', str(port)) for w in warnings]
    network = f"http://{chosen}:{port}" if chosen else None
    return local, network, [f"http://{ip}:{port}" for ip in others], warnings


# ---------- terminal QR code ----------

def _enable_windows_ansi(stream):
    """Turn on ANSI colours in the Windows console. Returns True if available."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)              # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # VT processing
    except Exception:
        return False


def qr_mode(stream=None):
    """'ansi' (explicit black/white, works on any theme), 'blocks' (Unicode half blocks
    in the terminal's own colours) or 'none' (Unicode can't be printed: plain ASCII
    characters don't fill their cells, so a QR drawn with them doesn't scan)."""
    stream = stream or sys.stdout
    encoding = getattr(stream, 'encoding', None) or 'ascii'
    try:
        '█▀▄'.encode(encoding)
        unicode_ok = True
    except (UnicodeEncodeError, LookupError):
        unicode_ok = False
    if not unicode_ok:
        return 'none'
    is_tty = hasattr(stream, 'isatty') and stream.isatty()
    if not is_tty or os.environ.get('NO_COLOR'):
        return 'blocks'
    if os.name == 'nt' and not _enable_windows_ansi(stream):
        return 'blocks'
    return 'ansi'


def qr_matrix(data):
    import qrcode
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()                   # True = dark module, includes the quiet zone


def render_qr(data, mode='ansi'):
    """Render a QR code as terminal text.

    'Light' modules are drawn as glyphs and dark modules left blank, which reads correctly
    on the usual light-on-dark terminal. In 'ansi' mode the colours are forced to white on
    black so it scans on any terminal theme.
    """
    matrix = qr_matrix(data)
    size = len(matrix)
    lines = []
    glyph = {(False, False): '█', (False, True): '▀', (True, False): '▄', (True, True): ' '}
    for y in range(0, size, 2):
        top = matrix[y]
        bottom = matrix[y + 1] if y + 1 < size else [True] * size
        line = ''.join(glyph[(t, b)] for t, b in zip(top, bottom))
        if mode == 'ansi':
            line = f"\x1b[97;40m{line}\x1b[0m"
        lines.append(line)
    return '\n'.join(lines)


def banner(local_url, network_url, other_urls, warnings, mode):
    """The text printed once the dashboard is listening."""
    out = ["", "Dashboard is running!", "", f"Local:   {local_url}"]
    if network_url:
        out.append(f"Network: {network_url}")
    for url in other_urls:
        out.append(f"         {url}")
    for w in warnings:
        out += ["", f"Warning: {w}"]
    if network_url and mode == 'none':
        out += ["", "(This terminal can't display a QR code; type the Network address on your phone.)"]
    elif network_url:
        out += ["", "Scan this QR code with your phone to open the dashboard:", "",
                render_qr(network_url + '/', mode)]
    if network_url:
        out += ["", "Phone can't connect? It must be on the same Wi-Fi; see README > Troubleshooting."]
    out += ["", "Press Ctrl+C to stop.", ""]
    return '\n'.join(out)
