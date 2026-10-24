#!/usr/bin/env python3
"""Execution flow (4 phases):
1. Prepare filesystem/defaults (migrate legacy root data and retain Server-era config/auth)
2. Resolve update plan and stage server files (local → backup → API priority)
3. Acquire auth tokens when enabled (OAuth2 device flow + game session)
4. Parse and exec startup command with signal forwarding to server process
Only the untouched installer config from pelican-eggs/games-standalone@fe8755ce may be replaced.
Server/.version marks the Server-working-directory layout introduced in March.
"""
import os, sys, json, signal, subprocess, time, zipfile, hashlib, shutil, re, shlex, tempfile
from pathlib import Path
from contextlib import suppress
from dataclasses import dataclass, asdict
from datetime import datetime
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

C = {'R':'\033[91m', 'G':'\033[92m', 'Y':'\033[93m', 'B':'\033[94m', 'C':'\033[96m', 'N':'\033[0m'}

def log(color, msg):
    print(f"{color}{msg}{C['N']}", flush=True)

def die(msg):
    log(C['R'], msg)
    sys.exit(1)

ROOT_DIR = Path(os.getenv("ROOT_DIR", "/home/container"))
SERVER_DIR = ROOT_DIR / "Server"
SERVER_JAR = SERVER_DIR / "HytaleServer.jar"
TMP_BASE = ROOT_DIR / ".tmp"
BACKUP_BASE = ROOT_DIR / ".server-backups"
PENDING_INSTALL = ROOT_DIR / ".server-install-pending"

SERVER_VERSION = os.getenv("SERVER_VERSION", "latest")
AUTO_UPDATE = os.getenv("AUTO_UPDATE", "1") == "1"
PATCHLINE = os.getenv("PATCHLINE", "release")
TRANSPORT = os.getenv("TRANSPORT", "QUIC").upper()

FLAGS = {
    'auth': os.getenv("HYTALE_API_AUTH", "1") == "1",
    'aot': os.getenv("USE_AOT_CACHE", "1") == "1",
    'compact_headers': os.getenv("USE_COMPACT_HEADERS", "1") == "1",
    'allow_op': os.getenv("ALLOW_OP", "0") == "1",
    'early_plugins': os.getenv("ACCEPT_EARLY_PLUGINS", "0") == "1",
    'disable_sentry': os.getenv("DISABLE_SENTRY", "0") == "1",
    'ignore_broken_mods': os.getenv("IGNORE_BROKEN_MODS", "0") == "1",
    'world_backup': os.getenv("ENABLE_WORLD_BACKUP", "1") == "1",
    'server_backup': os.getenv("ENABLE_SERVER_BACKUP", "1") == "1",
}

HYTALE_PROFILE_UUID = os.getenv("HYTALE_PROFILE_UUID", "")
HYTALE_AUTH_STATE_PATH = Path(os.getenv("HYTALE_AUTH_STATE_PATH", str(ROOT_DIR / ".hytale-auth.json")))
SERVER_BACKUP_RETENTION = int(os.getenv("SERVER_BACKUP_RETENTION", "2"))

HYTALE_ASSETS_API = "https://account-data.hytale.com/game-assets"
MAVEN_BASE_URL = "https://maven.hytale.com"
HYTALE_DEVICE_AUTH_URL = "https://oauth.accounts.hytale.com/oauth2/device/auth"
HYTALE_TOKEN_URL = "https://oauth.accounts.hytale.com/oauth2/token"
HYTALE_PROFILES_URL = "https://account-data.hytale.com/my-account/get-profiles"
HYTALE_SESSION_URL = "https://sessions.hytale.com/game-session/new"
HYTALE_SESSION_LOGOUT_URL = "https://sessions.hytale.com/game-session"
OAUTH_CLIENT_ID = "hytale-server"
OAUTH_SCOPE = "openid offline auth:server"

VERSION_PATTERN = r'^[a-zA-Z0-9][a-zA-Z0-9._+-]*$'
VERSION_FILE = ".version"
PATCHLINE_FILE = ".patchline"
USER_CONFIG_FILES = ["config.json", "bans.json", "whitelist.json", "permissions.json"]
AOT_STATE_FILE = ".hytale-aot.json"
JAVA_OPTION_ENV = ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "JDK_AOT_VM_OPTIONS")

auth_state = None
server_process = None
shutting_down = False

@dataclass
class AuthState:
    access_token: str = ""
    refresh_token: str = ""
    access_expires: int = 0
    refresh_expires: int = 0
    session_token: str = ""
    identity_token: str = ""
    session_expires: int = 0
    profile_uuid: str = ""
    profile_name: str = ""

    def save(self, path):
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(asdict(self), indent=2))
        temp.chmod(0o600)
        temp.rename(path)

    @classmethod
    def load(cls, path):
        if not path.exists():
            return None
        try:
            path.chmod(0o600)
            data = json.loads(path.read_text())
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (TypeError, KeyError) as e:
            log(C['Y'], f"[auth] Auth state schema mismatch, resetting: {e}")
            return None
        except Exception as e:
            log(C['Y'], f"[auth] Failed to load auth state: {e}")
            return None

class AuthManager:
    def __init__(self, session, state_path):
        self.session = session
        self.state_path = state_path
        self.state = AuthState.load(state_path) or AuthState()
    def ensure_authenticated(self):
        if self.state.access_token and time.time() + 60 < self.state.access_expires:
            return True
        if self.state.refresh_token and self.state.refresh_expires > time.time() and self._refresh():
            return True
        return self._device_flow()

    def _store_tokens(self, data, refresh=False):
        now = int(time.time())
        self.state.access_token = data['access_token']
        if not refresh:
            self.state.refresh_token = data.get('refresh_token', '')
        self.state.access_expires = now + int(data.get('expires_in', 3600))
        if refresh and 'refresh_token' in data:
            self.state.refresh_token = data['refresh_token']
        if not refresh or 'refresh_token' in data:
            self.state.refresh_expires = now + int(data.get('refresh_expires_in', 2592000))
        self.state.save(self.state_path)
        return (self.state.access_expires - now) // 60

    def _revoke_session(self, requester, timeout, report=False):
        try:
            resp = requester.delete(HYTALE_SESSION_LOGOUT_URL,
                headers={'Authorization': f'Bearer {self.state.session_token}'}, timeout=timeout)
            if report:
                if resp.status_code in (200, 204):
                    log(C['G'], "[auth] ✓ Session terminated")
                else:
                    log(C['Y'], f"[auth] Session cleanup returned HTTP {resp.status_code} (non-fatal)")
        except Exception as e:
            log(C['Y'], f"[auth] Session cleanup error: {e}{' (non-fatal)' if report else ''}")

    def _refresh(self):
        try:
            resp = self.session.post(HYTALE_TOKEN_URL, data={
                'client_id': OAUTH_CLIENT_ID, 'grant_type': 'refresh_token',
                'refresh_token': self.state.refresh_token}, timeout=30)
            if resp.status_code == 200:
                expires_in_min = self._store_tokens(resp.json(), refresh=True)
                log(C['G'], f"[auth] ✓ Token refreshed (expires in {expires_in_min}m)")
                return True
            log(C['Y'], f"[auth] Refresh failed (HTTP {resp.status_code}): {resp.text[:300]}")
            if resp.status_code == 400:
                with suppress(Exception):
                    error = resp.json().get('error', '')
                    if error in ('invalid_grant', 'invalid_client', 'unauthorized_client'):
                        self.state.access_token = self.state.refresh_token = ''
                        self.state.access_expires = self.state.refresh_expires = 0
                        self.state_path.unlink(missing_ok=True)
        except Exception as e:
            log(C['Y'], f"[auth] Refresh error: {e}")
        return False

    def _device_flow(self):
        try:
            resp = self.session.post(HYTALE_DEVICE_AUTH_URL,
                data={'client_id': OAUTH_CLIENT_ID, 'scope': OAUTH_SCOPE}, timeout=30)
            if resp.status_code != 200:
                log(C['R'], f"[auth] Device auth failed (HTTP {resp.status_code})")
                return False
            data = resp.json()
            device_code = data['device_code']
            divider = "  ═══════════════════════════════════════════════════════════"
            log(C['C'], divider)
            log(C['C'], f"  Please visit: {data.get('verification_uri_complete', data.get('verification_uri'))}")
            if 'user_code' in data:
                log(C['C'], f"  Or enter code: {data['user_code']} at https://accounts.hytale.com/device")
            expires_in = int(data.get('expires_in', 900))
            log(C['C'], f"  Waiting for authorization (expires in {expires_in}s)...")
            log(C['C'], divider)
            interval = int(data.get('interval', 5))
            poll_start = time.time()
            while (time.time() - poll_start) < expires_in:
                time.sleep(interval)
                try:
                    token_resp = self.session.post(HYTALE_TOKEN_URL, data={
                        'client_id': OAUTH_CLIENT_ID,
                        'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                        'device_code': device_code}, timeout=30)
                    if token_resp.status_code == 200:
                        expires_in_min = self._store_tokens(token_resp.json())
                        log(C['G'], f"[auth] ✓ Authenticated (token expires in {expires_in_min}m)")
                        return True
                    try:
                        error_data = token_resp.json()
                    except ValueError:
                        log(C['R'], f"[auth] Non-JSON error response (HTTP {token_resp.status_code}): {token_resp.text[:200]}")
                        return False
                    error = error_data.get('error', '')
                    if error == 'authorization_pending':
                        continue
                    if error == 'slow_down' or token_resp.status_code in (429, 500, 502, 503):
                        interval = min(interval + 5, 60)
                        continue
                    if error in ('access_denied', 'expired_token'):
                        log(C['R'], f"[auth] Authorization {error}")
                        return False
                except Exception as e:
                    log(C['Y'], f"[auth] Token poll error: {e}")
            log(C['R'], "[auth] Authorization timeout")
        except Exception as e:
            log(C['R'], f"[auth] Device flow error: {e}")
        return False

    def authed_request(self, method, url, **kwargs):
        extra = kwargs.pop('headers', {})
        for attempt in range(2):
            resp = self.session.request(method, url,
                headers={**extra, 'Authorization': f'Bearer {self.state.access_token}'}, **kwargs)
            if attempt or resp.status_code != 401 or not (self._refresh() or self._device_flow()):
                return resp

    def ensure_session(self):
        requested = HYTALE_PROFILE_UUID
        if not requested and self.state.session_token and time.time() + 60 < self.state.session_expires:
            return True
        try:
            resp = self.authed_request('GET', HYTALE_PROFILES_URL, timeout=30)
            if resp.status_code != 200:
                log(C['R'], f"[auth] Profile fetch failed (HTTP {resp.status_code})")
                return False
            profiles = resp.json().get('profiles', [])
            selected = next((p for p in profiles if p.get('uuid') == (requested or self.state.profile_uuid)), None)
            if requested and selected is None:
                die(f"[auth] Requested profile {requested} is not available to this account")
            if selected is None:
                selected = next((p for p in profiles if p.get('uuid')), None)
            if selected is None:
                log(C['R'], "[auth] No account profiles available")
                return False
            selected_uuid = selected['uuid']
            if selected_uuid == self.state.profile_uuid and self.state.session_token and time.time() + 60 < self.state.session_expires:
                return True
            if self.state.session_token:
                self._revoke_session(self.session, 10)
            self.state.session_token = self.state.identity_token = ""
            self.state.session_expires = 0
            self.state.profile_uuid = selected_uuid
            self.state.profile_name = selected.get('username', '')
            self.state.save(self.state_path)
            resp = self.authed_request('POST', HYTALE_SESSION_URL,
                json={'uuid': selected_uuid}, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                self.state.session_token = data.get('sessionToken', '')
                self.state.identity_token = data.get('identityToken', '')
                expires_at = data.get('expiresAt', '')
                if expires_at:
                    try:
                        self.state.session_expires = int(datetime.fromisoformat(expires_at.replace('Z', '+00:00')).timestamp())
                    except Exception as e:
                        log(C['Y'], f"[auth] Failed to parse session expiry: {e}")
                        self.state.session_expires = int(time.time()) + 3600
                self.state.save(self.state_path)
                return bool(self.state.session_token)
            log(C['R'], f"[auth] Session request failed HTTP {resp.status_code}: {resp.text[:300]}")
        except Exception as e:
            log(C['R'], f"[auth] Session error: {e}")
        return False

    def cleanup(self):
        if not self.state.session_token:
            return
        log(C['B'], "[auth] Cleaning up game session")
        self._revoke_session(requests, 3, report=True)
        fresh = AuthState.load(self.state_path) or self.state
        fresh.session_token = fresh.identity_token = ""
        fresh.session_expires = 0
        fresh.save(self.state_path)
        self.state = fresh

def parse_jar_version(jar_path):
    """Extract version and patchline from JAR manifest for metadata tracking."""
    if not jar_path.exists():
        return None, None
    try:
        with zipfile.ZipFile(jar_path) as zf:
            manifest = zf.read("META-INF/MANIFEST.MF").decode('utf-8').replace('\r\n', '\n')
            manifest = manifest.replace('\n ', '')
            fields = {}
            for line in manifest.split('\n'):
                name, separator, value = line.partition(':')
                if separator:
                    fields[name.lower()] = value.strip()
            return fields.get('implementation-version'), fields.get('implementation-patchline')
    except Exception as e:
        log(C['Y'], f"[version] Failed to parse JAR manifest: {e}")
        return None, None

def safe_selector(value):
    return isinstance(value, str) and re.fullmatch(VERSION_PATTERN, value) is not None

def version_sort_key(version):
    historical = re.fullmatch(r'(\d{4}\.\d{2}\.\d{2})-[a-f0-9]+', version)
    if historical:
        date = datetime.strptime(historical.group(1), '%Y.%m.%d')
        return (0, date.year, date.month, date.day)
    semver = re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?', version)
    if semver:
        major, minor, patch, prerelease = semver.groups()
        components = prerelease.split('.') if prerelease is not None else []
        if any(not part or (part.isdigit() and len(part) > 1 and part.startswith('0')) for part in components):
            raise ValueError(f"Invalid SemVer {version}")
        suffix = (0, tuple((0, int(part)) if part.isdigit() else (1, part) for part in components)) if prerelease is not None else (1,)
        return (1, int(major), int(minor), int(patch), suffix)
    raise ValueError(f"Cannot order version {version}")

def package_identity(package_dir, expected_version=None, expected_patchline=None):
    version, patchline = parse_jar_version(package_dir / "Server" / "HytaleServer.jar")
    if not safe_selector(version) or not safe_selector(patchline):
        log(C['Y'], f"[update] Invalid JAR identity in {package_dir}")
        return None
    if not (package_dir / "Assets.zip").is_file():
        log(C['Y'], f"[update] Assets.zip missing from {package_dir}; refusing an unpaired JAR")
        return None
    for field, actual, expected in (("version", version, expected_version), ("patchline", patchline, expected_patchline)):
        if expected is not None and actual != expected:
            log(C['Y'], f"[update] Expected {field} {expected}, package contains {actual}")
            return None
    return version, patchline

def is_valid_backup(backup_path):
    if not backup_path.is_dir() or not safe_selector(backup_path.name):
        return False
    identity = package_identity(backup_path, backup_path.name, backup_path.parent.name)
    if identity is None:
        return False
    try:
        for filename, expected in zip((VERSION_FILE, PATCHLINE_FILE), identity):
            path = backup_path / "Server" / filename
            if path.exists() and path.read_text().strip() != expected:
                return False
    except (OSError, UnicodeError):
        return False
    return True

def ordered_backups(backup_base, patchline):
    patchline_dir = backup_base / patchline
    if not patchline_dir.exists():
        return []
    def order(path):
        try:
            return version_sort_key(path.name), path.name
        except ValueError:
            return (-1,), path.name
    return sorted((path for path in patchline_dir.iterdir() if is_valid_backup(path)), key=order, reverse=True)

def prune_backups(backup_base, patchline, retention, protected_version=None):
    if retention <= 0:
        return
    try:
        backups = ordered_backups(backup_base, patchline)
        backups.sort(key=lambda path: path.name != protected_version)
        for old in backups[retention:]:
            shutil.rmtree(old)
    except Exception as e:
        log(C['Y'], f"[backup] Failed to prune backups: {e}")

def backup_current_version(server_dir, backup_base):
    if not FLAGS['server_backup']:
        log(C['C'], "[backup] Server backup disabled (ENABLE_SERVER_BACKUP=0)")
        return None
    identity = package_identity(server_dir.parent)
    if identity is None:
        log(C['Y'], "[backup] Skipped (complete JAR identity and Assets.zip required)")
        return None
    version, patchline = identity
    backup_dir = backup_base / patchline / version
    temp = retired = None
    try:
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=backup_dir.parent))
        dest_server = temp / "Server"
        dest_server.mkdir()
        shutil.copy2(server_dir / "HytaleServer.jar", dest_server / "HytaleServer.jar")
        if (cache := server_dir / "HytaleServer.aot").is_file():
            shutil.copy2(cache, dest_server / cache.name)
        shutil.copy2(server_dir.parent / "Assets.zip", temp / "Assets.zip")
        (dest_server / VERSION_FILE).write_text(version)
        (dest_server / PATCHLINE_FILE).write_text(patchline)
        if package_identity(temp, version, patchline) is None:
            raise ValueError("Copied snapshot identity changed")
        sync_install_tree(temp)
        if path_present(backup_dir):
            retired = backup_dir.parent / (temp.name + "-replaced")
            backup_dir.replace(retired)
        try:
            temp.replace(backup_dir)
            temp = None
        except Exception:
            if retired is not None:
                with suppress(OSError):
                    retired.replace(backup_dir)
            raise
        for directory in (backup_dir.parent, backup_base, backup_base.parent):
            sync_install_path(directory)
        if retired is not None:
            remove_install_path(retired)
            sync_install_path(backup_dir.parent)
    except Exception as e:
        raise RuntimeError(f"[backup] Snapshot capture failed; refusing to replace live binaries: {e}") from e
    finally:
        if temp is not None:
            try:
                shutil.rmtree(temp)
            except Exception as e:
                log(C['Y'], f"[backup] Failed to clean temporary snapshot {temp}: {e}")
    log(C['G'], f"[backup] ✓ .server-backups/{patchline}/{version}/")
    return backup_dir

def sync_install_path(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def sync_install_tree(path):
    if path.is_dir():
        for child in path.iterdir():
            sync_install_tree(child)
    sync_install_path(path)

def remove_install_path(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)

def path_present(path):
    return path.exists() or path.is_symlink()

def pending_install_error(path, detail=""):
    die(f"[update] Pending installation at {path}; refusing cleanup, recovery and launch. "
        f"Restore a matched {SERVER_JAR} and {ROOT_DIR / 'Assets.zip'} pair from "
        f"{BACKUP_BASE} or the preserved source package, or complete manual repair, "
        f"before clearing {path}. Before removing any {BACKUP_BASE}/*/.snapshot-* directory, rename a "
        ".snapshot-*-replaced one back to its missing version directory after verifying its JAR identity: "
        "it can be the only intact copy of that backup. After repair, inspect and remove abandoned "
        ".server-install-* temporary directories beside the resolved live targets."
        + (f" {detail}" if detail else ""))

def ensure_no_pending_install():
    if path_present(PENDING_INSTALL):
        pending_install_error(PENDING_INSTALL)
    for path in sorted(BACKUP_BASE.glob("*/.snapshot-*")):
        pending_install_error(path, "Interrupted server snapshot capture.")

def protected_data_paths():
    return (BACKUP_BASE, PENDING_INSTALL, TMP_BASE, ROOT_DIR / "updater",
        *(base / name for base in (ROOT_DIR, SERVER_DIR)
            for name in ("mods", "earlyplugins", "earlyplugins.disabled", "universe", "worlds")))

def install_target(destination, source_dir, directory=False):
    target = destination.resolve()
    protected = (*protected_data_paths(), source_dir)
    if directory:
        protected += tuple(base / name for base in (ROOT_DIR, SERVER_DIR) for name in USER_CONFIG_FILES)
    if any(target.is_relative_to(path.resolve()) or (
            directory and path.resolve().is_relative_to(target)) for path in protected):
        raise ValueError(f"Live destination {destination} overlaps protected data: {target}")
    if not target.parent.is_dir():
        raise ValueError(f"Live destination parent does not exist: {target.parent}")
    return target

def publish_install_file(source, destination, *, text=None, directory=False):
    with tempfile.TemporaryDirectory(prefix=".server-install-", dir=destination.parent) as temp:
        payload = Path(temp) / "payload"
        if directory:
            shutil.copytree(source, payload)
        elif text is None:
            shutil.copy2(source, payload)
        else:
            payload.write_text(text)
        sync_install_tree(payload)
        if directory:
            remove_install_path(destination)
        os.replace(payload, destination)
    sync_install_path(destination.parent)

def write_pending_marker(out, marker):
    out.write(marker + "\n")
    out.flush()
    os.fsync(out.fileno())
    sync_install_path(ROOT_DIR)

def install_from_extract(extract_dir, server_dir, expected_version=None, expected_patchline=None,
        restore=False):
    ensure_no_pending_install()
    failure = ("[backup] Restore failed; refusing to run a different or partially restored version"
        if restore else "[update] Installation failed; refusing to launch potentially mixed binaries")
    if restore and not is_valid_backup(extract_dir):
        die(failure)
    began = False
    try:
        identity = package_identity(extract_dir, expected_version, expected_patchline)
        if identity is None:
            die(failure)
        version, patchline = identity
        source_server = extract_dir / "Server"
        payloads = [(source_server / "HytaleServer.jar", server_dir / "HytaleServer.jar", None),
            (extract_dir / "Assets.zip", ROOT_DIR / "Assets.zip", None)]
        cache = source_server / "HytaleServer.aot"
        licenses = source_server / "Licenses"
        if restore and cache.is_file():
            payloads.append((cache, server_dir / cache.name, None))
        if not restore:
            for filename in USER_CONFIG_FILES:
                default, destination = source_server / filename, server_dir / filename
                if path_present(default) and not any(
                        path_present(path) for path in (destination, ROOT_DIR / filename)):
                    payloads.append((default, destination, None))
            if path_present(licenses):
                if not licenses.is_dir():
                    raise ValueError(f"Invalid licenses directory: {licenses}")
                payloads.append((licenses, server_dir / "Licenses", None))
        payloads.extend(((None, server_dir / VERSION_FILE, version), (None, server_dir / PATCHLINE_FILE, patchline)))
        sources = [source for source, _, _ in payloads if source is not None]
        configs = [base / name for base in (ROOT_DIR, server_dir) for name in USER_CONFIG_FILES]
        targets = []
        for source, destination, _ in payloads:
            target = install_target(destination, extract_dir, directory=source == licenses)
            if source is not None and source != licenses and (not source.is_file()
                    or (target.exists() and not target.is_file())):
                raise ValueError(f"Invalid installation file: {source} -> {target}")
            if source == licenses and any(path.resolve().is_relative_to(target)
                    for path in (ROOT_DIR, server_dir, *sources)):
                raise ValueError(f"Licenses destination contains live installation or source data: {target}")
            for other in targets:
                if (target.is_relative_to(other) or other.is_relative_to(target)
                        or (target.exists() and other.exists() and target.samefile(other))):
                    raise ValueError(f"Aliased installation destinations: {target} and {other}")
            for config in configs:
                if destination != config and path_present(config) and (
                        target == config.resolve() or (target.exists() and config.exists() and target.samefile(config))):
                    raise ValueError(f"Installation destination aliases user configuration: {destination} -> {config}")
            targets.append(target)
        for source in sources:
            if any(target.exists() and source.samefile(target) for target in targets):
                raise ValueError(f"Source aliases a live installation destination: {source}")
        sizes = [(target, source.stat().st_size) for target, source in zip(targets, sources[:2])]
        marker = f"Source package: {extract_dir}"
        with PENDING_INSTALL.open("x") as out:
            began = True
            write_pending_marker(out, marker)
        snapshot = backup_current_version(server_dir, BACKUP_BASE)
        (server_dir / AOT_STATE_FILE).unlink(missing_ok=True)
        if not restore or not cache.is_file():
            (server_dir / "HytaleServer.aot").unlink(missing_ok=True)
        sync_install_path(server_dir)
        for (source, _, text), target in zip(payloads, targets):
            publish_install_file(source, target, text=text, directory=source == licenses)
        sync_install_path(server_dir)
        sync_install_path(ROOT_DIR)
        if (package_identity(server_dir.parent, version, patchline) is None
                or any(not path.is_file() or path.stat().st_size != size for path, size in sizes)):
            raise ValueError("Published JAR/Assets pair failed final identity or size checks")
        consumed = None if restore else next((path for path in (
            ROOT_DIR / "updater" / "staging", TMP_BASE / "api-download")
            if path.resolve() == extract_dir.resolve()), None)
        if consumed is not None:
            remove_install_path(consumed)
            sync_install_path(consumed.parent)
        PENDING_INSTALL.unlink()
        try:
            sync_install_path(ROOT_DIR)
        except Exception:
            with suppress(OSError), PENDING_INSTALL.open("x") as out:
                write_pending_marker(out, marker)
            raise
    except Exception as e:
        if began or path_present(PENDING_INSTALL):
            pending_install_error(PENDING_INSTALL, str(e))
        log(C['Y'], f"[update] Installation preflight failed; live files unchanged: {e}")
        die(failure)
    if snapshot is not None:
        prune_backups(BACKUP_BASE, snapshot.parent.name, SERVER_BACKUP_RETENTION, snapshot.name)
    return identity

def apply_staged_update(requested_version, patchline):
    staging = ROOT_DIR / "updater" / "staging"
    if not (staging / "Server" / "HytaleServer.jar").exists():
        return False
    if package_identity(staging, requested_version, patchline) is None:
        log(C['Y'], "[update] Keeping staged update: it does not satisfy the requested version/patchline")
        return False
    log(C['B'], "[update] Applying verified staged update")
    identity = install_from_extract(staging, SERVER_DIR, requested_version, patchline)
    log(C['G'], f"[update] ✓ Applied {identity[0]}")
    return identity

def maven_version_exists(session, patchline, version):
    try:
        resp = session.get(f"{MAVEN_BASE_URL}/{patchline}/com/hypixel/hytale/Server/maven-metadata.xml", timeout=30)
        return resp.status_code == 200 and version in re.findall(r'<version>\s*([^<]+)', resp.text)
    except Exception as e:
        log(C['Y'], f"[maven] Failed to fetch metadata: {e}")
        return False

def api_latest(session, auth_mgr, patchline):
    if not auth_mgr.ensure_authenticated():
        return None
    try:
        resp = auth_mgr.authed_request('GET', f"{HYTALE_ASSETS_API}/version/{patchline}.json", timeout=15)
        if resp.status_code != 200:
            return None
        manifest = session.get(resp.json()['url'], timeout=15).json()
        version = manifest.get('version')
        if not safe_selector(version) or manifest.get('patchline', patchline) != patchline:
            log(C['Y'], "[update] Remote manifest has an invalid version or mismatching patchline")
            return None
        log(C['C'], f"[update] Latest {patchline}: {version}")
        return version
    except Exception:
        return None

def content_hash(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()

def aot_input_hash(command, launch_jar):
    release = Path(shutil.which(command[0])).resolve(strict=True).parents[1] / "release"
    jars = [SERVER_JAR.resolve(strict=True)]
    if not launch_jar.samefile(SERVER_JAR):
        jars.append(launch_jar.resolve(strict=True))
    return hashlib.sha256(json.dumps({
        "argv": command, "cwd": str(SERVER_DIR.resolve()),
        "option_environment": {name: os.getenv(name) for name in JAVA_OPTION_ENV},
        "java_release": (str(release), content_hash(release)),
        "jar_stats": [(stat.st_size, stat.st_mtime_ns)
            for stat in (path.stat() for path in jars)],
        "files": [(str(path), content_hash(path) if path.exists() else None)
            for path in jars + [SERVER_DIR / "config.json"]],
    }, sort_keys=True).encode()).hexdigest()

def api_download(session, auth_mgr, patchline, target_dir, target_version=None):
    if not safe_selector(patchline) or (target_version is not None and not safe_selector(target_version)):
        log(C['R'], "[api] Invalid requested version or patchline")
        return False
    for attempt in range(3):
        try:
            resp = auth_mgr.authed_request('GET', f"{HYTALE_ASSETS_API}/version/{patchline}.json", timeout=30)
            if resp.status_code != 200:
                continue
            manifest_data = session.get(resp.json()['url'], timeout=30).json()
            version, download_url, sha256_expected = manifest_data['version'], manifest_data['download_url'], manifest_data.get('sha256')
            if not safe_selector(version) or manifest_data.get('patchline', patchline) != patchline:
                log(C['R'], "[api] Remote manifest identity does not match the requested patchline")
                return False
            if target_version is not None and version != target_version:
                log(C['R'], f"[api] Requested {target_version}, remote now serves {version}; refusing an unmatched package")
                return False
            if not sha256_expected:
                log(C['Y'], "[api] No SHA-256 in manifest, refusing download")
                continue
            log(C['C'], f"[api] Remote: {version}")
            signed_dl = auth_mgr.authed_request('GET', f"{HYTALE_ASSETS_API}/{download_url}", timeout=30).json()['url']
            shutil.rmtree(target_dir, ignore_errors=True)
            zip_path = target_dir / "server.zip"
            target_dir.mkdir(parents=True, exist_ok=True)
            resp = session.get(signed_dl, stream=True, timeout=900)
            if resp.status_code != 200:
                log(C['Y'], f"[api] Download failed (HTTP {resp.status_code})")
                continue
            total = int(resp.headers.get('content-length', 0))
            if total > 0:
                free = shutil.disk_usage(target_dir).free
                needed = int(total * 1.5)
                if free < needed:
                    log(C['R'], f"[api] Insufficient space: {free / (1024**2):.0f} MB free, {needed / (1024**2):.0f} MB needed")
                    return False
            downloaded = 0
            with open(zip_path, 'wb') as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        print(f"\r[api] {downloaded/(1024*1024):.1f}/{total/(1024*1024):.1f} MB ({100*downloaded/total:.0f}%)", end='', file=sys.stderr)
            if total > 0:
                print(file=sys.stderr)
            if content_hash(zip_path) != sha256_expected:
                log(C['Y'], "[api] SHA-256 mismatch, retrying")
                zip_path.unlink()
                continue
            log(C['G'], "[api] ✓ Verified")
            log(C['B'], "[api] Extracting...")
            with zipfile.ZipFile(zip_path) as zf:
                target_resolved = target_dir.resolve()
                for member in zf.infolist():
                    dest = (target_dir / member.filename).resolve()
                    if not dest.is_relative_to(target_resolved):
                        log(C['Y'], f"[api] Skipping unsafe zip entry: {member.filename}")
                        continue
                    if member.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(dest, 'wb') as dst:
                            shutil.copyfileobj(src, dst)
            zip_path.unlink()
            return package_identity(target_dir, version, patchline) is not None
        except Exception as e:
            log(C['Y'], f"[api] Attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    return False

def plan_update(session, auth_mgr, server_version, patchline, local_version, local_patchline,
        staged_applied=False):
    has_jar = SERVER_JAR.is_file()
    if server_version == "latest":
        if has_jar and local_patchline == patchline and (staged_applied or not AUTO_UPDATE):
            return "none", "", None
        if has_jar and not AUTO_UPDATE:
            if backups := ordered_backups(BACKUP_BASE, patchline):
                return "backup", backups[0].name, backups[0]
        if remote := api_latest(session, auth_mgr, patchline):
            if has_jar and local_version == remote and local_patchline == patchline:
                return "none", "", None
            backup_path = BACKUP_BASE / patchline / remote
            if is_valid_backup(backup_path):
                return "backup", remote, backup_path
            return "api", remote, None
        if not has_jar:
            return "api", "", None
        if local_patchline != patchline:
            die(f"[update] Cannot obtain {patchline}; installed JAR belongs to {local_patchline or 'unknown'}")
        log(C['Y'], "[update] API check failed, running existing server")
        return "none", "", None
    if has_jar and local_version == server_version and local_patchline == patchline:
        return "none", server_version, None
    backup_path = BACKUP_BASE / patchline / server_version
    if is_valid_backup(backup_path):
        return "backup", server_version, backup_path
    if not maven_version_exists(session, patchline, server_version):
        die(f"[update] Requested version {server_version} ({patchline}) is not available")
    remote = api_latest(session, auth_mgr, patchline)
    if remote != server_version:
        die(f"[update] Requested version {server_version} ({patchline}) has no matching full API package (latest: {remote or 'unavailable'})")
    return "api", server_version, None

def migrate_legacy_layout():
    moves, placeholders = [], set()
    server_layout = (path_present(SERVER_JAR) and not path_present(ROOT_DIR / "HytaleServer.jar")
        and path_present(SERVER_DIR / VERSION_FILE))
    empty_directory = lambda path: not path.is_symlink() and path.is_dir() and not any(path.iterdir())
    def installer_placeholder(path):
        return empty_directory(path) or (not server_layout and path == SERVER_DIR / "config.json"
            and not path.is_symlink() and path.is_file()
            and hashlib.sha256(json.dumps(json.loads(path.read_text()), sort_keys=True).encode()).hexdigest()
            == "45660abd7036984a9e1da182144ddf80d206f7a34ead1bb345034f7f463532cc")
    def queue(source, destination, directory=False):
        if not path_present(source):
            return
        if source.exists() and not (source.is_dir() if directory else source.is_file()):
            raise ValueError(f"Unsupported legacy entry or symlink target type: {source}")
        if path_present(destination):
            if directory and empty_directory(source) and destination.is_dir():
                return
            if (directory or (source.name == "config.json" and destination.is_file())) and installer_placeholder(destination):
                placeholders.add(destination)
            else:
                raise ValueError(f"Ambiguous populated root/Server layout: {source} and {destination}; "
                    "reconcile these paths manually; automatic merging is disabled")
        if source.lstat().st_dev != source.parent.stat().st_dev or source.lstat().st_dev != destination.parent.stat().st_dev:
            raise ValueError(f"Cross-filesystem migration is unsupported: {source} -> {destination}")
        moves.append((source, destination))
    try:
        if not server_layout:
            for filename in USER_CONFIG_FILES:
                queue(ROOT_DIR / filename, SERVER_DIR / filename)
        credentials = ("auth.enc", "auth.key")
        server_credentials = any(path_present(SERVER_DIR / name) for name in credentials)
        if not server_layout and any(path_present(ROOT_DIR / name) for name in credentials):
            if server_credentials:
                raise ValueError(f"Conflicting native credentials between {ROOT_DIR} and {SERVER_DIR}; "
                    "reconcile the stores manually; automatic mixing is disabled")
            for name in credentials:
                queue(ROOT_DIR / name, SERVER_DIR / name)
        for name in ("Licenses", "logs", "universe", "builtin", "worlds", "mods"):
            queue(ROOT_DIR / name, SERVER_DIR / name, directory=True)
        early_dir, early_off = SERVER_DIR / "earlyplugins", SERVER_DIR / "earlyplugins.disabled"
        early_source, early_dest = (early_off, early_dir) if FLAGS['early_plugins'] else (early_dir, early_off)
        root_early = ROOT_DIR / "earlyplugins"
        if path_present(root_early) and not empty_directory(root_early):
            if path_present(early_source) and not empty_directory(early_source):
                raise ValueError(f"Ambiguous early-plugin directories: {root_early} and {early_source}")
            queue(root_early, early_dest, directory=True)
        else:
            queue(early_source, early_dest, directory=True)
        for filename in (VERSION_FILE, PATCHLINE_FILE):
            if not path_present(SERVER_DIR / filename):
                queue(ROOT_DIR / filename, SERVER_DIR / filename)
        queue(ROOT_DIR / "HytaleServer.jar", SERVER_JAR)
        old_roots = [source.parent.resolve() / source.name for source, _ in moves]
        new_roots = [destination.parent.resolve() / destination.name for _, destination in moves]
        for source, destination in moves:
            old, new = source.parent.resolve() / source.name, destination.parent.resolve() / destination.name
            paths = [source]
            if source.is_dir() and not source.is_symlink():
                paths.extend(Path(directory) / name
                    for directory, directories, files in os.walk(source, followlinks=False)
                    for name in directories + files)
            for link in paths:
                if not link.is_symlink():
                    continue
                raw = link.readlink()
                literal = Path(os.path.abspath(raw if raw.is_absolute() else link.parent.resolve() / raw))
                target = link.resolve(strict=not raw.is_absolute())
                new_link = new / link.relative_to(source)
                if raw.is_absolute():
                    safe = not any(path.is_relative_to(root)
                        for path in (literal, target) for root in old_roots + new_roots)
                elif not source.is_symlink() and literal.is_relative_to(old) and target.is_relative_to(old):
                    safe = True
                else:
                    safe = link.parent.resolve() == new_link.parent and not any(
                        path.is_relative_to(root) for path in (literal, target) for root in old_roots + new_roots)
                if not safe:
                    raise ValueError(f"Symlink meaning would change during migration: {link} -> {raw}; repair manually")
        for path in placeholders:
            if path.lstat().st_dev != path.parent.stat().st_dev or not installer_placeholder(path):
                raise ValueError(f"Unsafe installer placeholder: {path}")
        for path in placeholders:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
            sync_install_path(path.parent)
        for source, destination in moves:
            if path_present(destination):
                raise FileExistsError(f"Migration destination appeared: {destination}")
            os.rename(source, destination)
            sync_install_path(source.parent)
            if destination.parent != source.parent:
                sync_install_path(destination.parent)
        if FLAGS['early_plugins'] and not path_present(early_dir):
            early_dir.mkdir()
            sync_install_path(SERVER_DIR)
    except Exception as e:
        die(f"[migrate] Refusing launch with an incomplete or unsupported migration: {e}")

def validate_transport(transport):
    if transport not in ("QUIC", "QUICHE"):
        die(f"[startup] Unsupported transport {transport!r}; use QUIC or QUICHE (an alternative QUIC backend, not TCP)")

NATIVE_VALUE_OPTIONS = set(("log bind transport prefab-cache assets mods early-plugins "
    "generate-asset-schema generate-config-schema world-gen backup-frequency backup-dir "
    "backup-max-count backup-archive-max-count owner-name owner-uuid client-pid universe "
    "force-network-flush migrations migrate-worlds boot-command auth-mode session-token "
    "identity-token recovery-mode").split())
NATIVE_OPTIONS = NATIVE_VALUE_OPTIONS | set(("b t help version bare bootstrap disable-cpb-build "
    "allow-install-dir accept-early-plugins validate-assets validate-prefabs validate-world-gen "
    "shutdown-after-validate disable-file-watcher disable-sentry disable-asset-compare backup "
    "singleplayer event-debug ignore-broken-mods allow-op verify-worlds").split())

def native_options(arguments):
    found, index = {}, 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            break
        name, value, attached = None, None, False
        if argument.startswith("-"):
            prefix, separator, value = (argument[2:] if argument.startswith("--") else argument[1:]).partition("=")
            matches = [option for option in NATIVE_OPTIONS if option.startswith(prefix)]
            name = prefix if prefix in NATIVE_OPTIONS else matches[0] if len(matches) == 1 else None
            attached = bool(separator)
        if name is None and not argument.startswith("--") and argument.startswith(("-t", "-b")):
            name, value, attached = argument[1], argument[2:], len(argument) > 2
        name = {"t": "transport", "b": "bind"}.get(name, name)
        if name is not None:
            if not attached and (name in NATIVE_VALUE_OPTIONS or name == "validate-prefabs"):
                value = None
                if index + 1 < len(arguments) and arguments[index + 1] != "--" and (
                        name != "validate-prefabs" or not arguments[index + 1].startswith("-")):
                    index += 1
                    value = arguments[index]
            found.setdefault(name, []).append(value)
        index += 1
    return found

def backup_destination(value, native=False, options=None):
    try:
        path = Path(value) if native else Path(value).expanduser()
        path = (path if path.is_absolute() else (SERVER_DIR if native else ROOT_DIR) / path).resolve()
        if any(root.resolve().is_relative_to(path) or (path.exists() and any(
                path.samefile(parent) for parent in (root.resolve(), *root.resolve().parents)))
                for root in (ROOT_DIR, SERVER_DIR)):
            raise ValueError("root, Server and their ancestors are not dedicated backup destinations")
        protected = [*protected_data_paths(), ROOT_DIR / "Assets.zip",
            *(base / name for base in (ROOT_DIR, SERVER_DIR)
                for name in ("builtin", "logs", "Licenses", "HytaleServer.jar", "HytaleServer.aot",
                    VERSION_FILE, PATCHLINE_FILE, *USER_CONFIG_FILES))]
        for name in ("mods", "early-plugins", "universe", "assets"):
            for supplied in (options or {}).get(name, []):
                if supplied is not None:
                    for entry in supplied.split(",") if name in ("mods", "early-plugins") else (supplied,):
                        extra = Path(entry)
                        protected.append(extra if extra.is_absolute() else SERVER_DIR / extra)
        for reserved in protected:
            reserved = reserved.resolve()
            if (path.is_relative_to(reserved) or reserved.is_relative_to(path)
                    or (path.exists() and reserved.exists() and path.samefile(reserved))):
                raise ValueError(f"overlaps protected data at {reserved}")
        return str(path)
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        die(f"[backup] Unsafe backup destination {value!r}: {e}")

def backup_integer(name, default):
    value = os.getenv(name, "").strip() or str(default)
    digits = value.lstrip("+-").lstrip("0") or "0"
    if (not re.fullmatch(r"[+-]?[0-9]+", value) or len(digits) > 10
            or int(digits) > (2147483648 if value.startswith("-") else 2147483647)):
        die(f"[backup] {name} must be a signed 32-bit integer")
    return value

def managed_server_flags(options):
    flags = []
    if "transport" in options:
        for transport in options["transport"]:
            validate_transport(transport)
        transport = options["transport"][-1]
    else:
        transport = TRANSPORT
        validate_transport(transport)
        flags.extend(["--transport", transport])
    for enabled, name in (("allow_op", "allow-op"), ("early_plugins", "accept-early-plugins"),
            ("disable_sentry", "disable-sentry"), ("ignore_broken_mods", "ignore-broken-mods"),
            ("world_backup", "backup")):
        if FLAGS[enabled] and name not in options:
            flags.append("--" + name)
    if "backup-dir" in options:
        for value in options["backup-dir"]:
            if value is None:
                die("[backup] --backup-dir requires a dedicated destination")
            backup_destination(value, native=True, options=options)
    elif FLAGS['world_backup']:
        flags.extend(["--backup-dir", backup_destination(os.getenv("BACKUP_DIR", "") or "Backups", options=options)])
    if FLAGS['world_backup']:
        for option, name, default in (("backup-frequency", "BACKUP_FREQUENCY", 30),
                ("backup-max-count", "BACKUP_MAX_COUNT", 5), ("backup-archive-max-count", "BACKUP_ARCHIVE_MAX_COUNT", 5)):
            if option not in options:
                flags.extend(["--" + option, backup_integer(name, default)])
    return flags, transport

def startup_command():
    startup_json = os.getenv("STARTUP_JSON", "")
    try:
        if startup_json:
            cmd = json.loads(startup_json)
            if not isinstance(cmd, list) or not all(isinstance(arg, str) for arg in cmd):
                die("[startup] STARTUP_JSON must be a JSON array of strings")
        else:
            startup = os.getenv("STARTUP", "")
            for pattern in (r'\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}', r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}'):
                startup = re.sub(pattern, lambda m: os.getenv(m.group(1), ""), startup)
            cmd = shlex.split(startup)
    except ValueError as e:
        die(f"[startup] Invalid STARTUP: {e}")
    if not cmd:
        die("[startup] Empty STARTUP")
    return cmd

def handle_signal(signum, frame):
    global shutting_down, server_process
    if not shutting_down and server_process:
        shutting_down = True
        try:
            os.killpg(os.getpgid(server_process.pid), signum)
        except Exception as e:
            log(C['Y'], f"[signal] Failed to kill process group: {e}")
            server_process.send_signal(signum)

def main():
    global auth_state, server_process, PATCHLINE
    ensure_no_pending_install()
    if SERVER_VERSION == "previous":
        die("[update] SERVER_VERSION=previous is unsupported; set latest or an exact version")
    os.umask(0o077)
    os.environ.setdefault('TZ', 'UTC')
    start_time = time.time()
    aot_reused = False
    os.chdir(ROOT_DIR)
    TMP_BASE.mkdir(exist_ok=True)
    SERVER_DIR.mkdir(exist_ok=True)
    try:
        for path in (SERVER_JAR, ROOT_DIR / "HytaleServer.jar", ROOT_DIR / "Assets.zip"):
            install_target(path, TMP_BASE)
    except (OSError, RuntimeError, ValueError) as e:
        die(f"[update] Unsupported live installation layout: {e}")
    # Server jar expects start.sh to exist when using /update command
    (ROOT_DIR / "start.sh").touch(mode=0o644, exist_ok=True)
    for item in TMP_BASE.iterdir():
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)
    migrate_legacy_layout()
    # Override patchline from config.json if set (takes precedence over env var)
    if (config_file := SERVER_DIR / "config.json").exists():
        with suppress(Exception):
            if (pl := json.loads(config_file.read_text()).get("Update", {}).get("Patchline", "")) in ("release", "pre-release"):
                PATCHLINE = pl
    if not safe_selector(PATCHLINE) or not safe_selector(SERVER_VERSION):
        die("[update] SERVER_VERSION and PATCHLINE must be path-safe version selectors")
    # Load version info
    local_version = local_patchline = ""
    if SERVER_JAR.exists():
        local_version, local_patchline = parse_jar_version(SERVER_JAR)
        if safe_selector(local_version) and safe_selector(local_patchline):
            for filename, value in ((VERSION_FILE, local_version), (PATCHLINE_FILE, local_patchline)):
                publish_install_file(None, SERVER_DIR / filename, text=value)
    requested_version = SERVER_VERSION if SERVER_VERSION != "latest" else None
    staged_applied = apply_staged_update(requested_version, PATCHLINE)
    if staged_applied:
        local_version, local_patchline = staged_applied
    log(C['C'], f"Current version : {local_version or 'none'} ({local_patchline or 'unknown'})")
    log(C['C'], f"Active patchline: {PATCHLINE}")
    log(C['C'], f"Requested build : {SERVER_VERSION}")
    # Create HTTP session
    session = requests.Session()
    retry = Retry(total=5, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "HytaleServerLauncher/1.0"})
    auth_mgr = AuthManager(session, HYTALE_AUTH_STATE_PATH)
    plan, target, backup_path = plan_update(session, auth_mgr, SERVER_VERSION, PATCHLINE,
        local_version, local_patchline, bool(staged_applied))
    if plan != "none":
        log(C['C'], f"[update] Plan: {plan}" + (f" (target {target})" if target else ""))
    # Execute plan
    if plan == "backup":
        log(C['B'], f"[backup] Restoring {target} from backup")
        install_from_extract(backup_path, SERVER_DIR, target, PATCHLINE, restore=True)
        log(C['G'], "[backup] ✓ Restored")
    elif plan == "api":
        authenticated = auth_mgr.ensure_authenticated()
        download_dir = TMP_BASE / "api-download"
        if authenticated and api_download(session, auth_mgr, PATCHLINE, download_dir, target_version=target or None):
            installed_version, _ = install_from_extract(download_dir, SERVER_DIR, target or None, PATCHLINE)
            log(C['G'], f"[api] ✓ Updated to {installed_version}")
        elif SERVER_VERSION != "latest" or not SERVER_JAR.exists():
            die(f"[update] Download failed for requested {target or SERVER_VERSION} ({PATCHLINE})"
                if authenticated else "[auth] Authentication required to install the requested version")
        else:
            failure = "[update] Download failed" if authenticated else "[auth] Auth failed"
            log(C['Y'], f"{failure}, running existing server")
    final_version, final_patchline = parse_jar_version(SERVER_JAR)
    if not safe_selector(final_version) or final_patchline != PATCHLINE:
        die(f"[update] Installed JAR does not match active patchline {PATCHLINE}")
    if requested_version is not None and final_version != requested_version:
        die(f"[update] Requested {requested_version} ({PATCHLINE}), installed JAR is {final_version} ({final_patchline})")
    # Authentication for server startup
    if FLAGS['auth']:
        if auth_mgr.ensure_authenticated() and auth_mgr.ensure_session():
            tokens = auth_mgr.state
            os.environ.update({
                'HYTALE_SERVER_SESSION_TOKEN': tokens.session_token,
                'HYTALE_SERVER_IDENTITY_TOKEN': tokens.identity_token,
                'HYTALE_SERVER_SESSION_EXPIRES': str(tokens.session_expires),
                'HYTALE_SERVER_OAUTH_ACCESS_TOKEN': tokens.access_token,
                'HYTALE_SERVER_OAUTH_REFRESH_TOKEN': tokens.refresh_token,
                'HYTALE_SERVER_OAUTH_ACCESS_EXPIRES': str(tokens.access_expires),
                'HYTALE_PROFILE_UUID': tokens.profile_uuid,
                'HYTALE_PROFILE_NAME': tokens.profile_name})
            now = int(time.time())
            a_exp, s_exp, r_exp = (
                f"{(expires - now) // unit}{suffix}" if expires > now else "expired"
                for expires, unit, suffix in (
                    (tokens.access_expires, 60, "m"),
                    (tokens.session_expires, 60, "m"),
                    (tokens.refresh_expires, 3600, "h")))
            log(C['G'], f"[auth] ✓ Tokens ready (access in {a_exp}, session in {s_exp}, refresh in {r_exp})")
            auth_state = auth_mgr
        else:
            if HYTALE_PROFILE_UUID:
                die(f"[auth] Could not acquire a session for requested profile {HYTALE_PROFILE_UUID}")
            log(C['Y'], "[auth] Continuing without pre-acquired tokens")
    cmd = startup_command()
    os.chdir(SERVER_DIR)
    # Build JVM and server flags for automatic injection
    jvm_flags = [f"-Djava.io.tmpdir={TMP_BASE}", "-Dterminal.jline=false", "-Dterminal.ansi=true"]
    effective_transport = None

    # Inject flags if this is a Java command with -jar
    if cmd and any("java" in str(arg).lower() for arg in cmd[:3]) and "-jar" in cmd:
        jar_flag_idx = cmd.index("-jar")
        jar_idx = jar_flag_idx + 1
        if jar_idx >= len(cmd):
            die("[startup] -jar requires a server JAR path")
        launch_jar = Path(cmd[jar_idx])
        if not launch_jar.is_absolute():
            launch_jar = SERVER_DIR / launch_jar
        launch_version, launch_patchline = parse_jar_version(launch_jar)
        if launch_version and launch_patchline and (launch_version != final_version or launch_patchline != PATCHLINE):
            die("[startup] STARTUP JAR does not match the selected server version/patchline")
        pre_jar, post_jar = cmd[:jar_flag_idx], cmd[jar_idx+1:]
        server_flags, effective_transport = managed_server_flags(native_options(post_jar))
        option_environment = {name: os.getenv(name) for name in JAVA_OPTION_ENV}
        try:
            explicit_jvm = pre_jar[1:] + [
                option for name, value in option_environment.items() if name != "JDK_AOT_VM_OPTIONS"
                for option in shlex.split(value or "")]
        except ValueError:
            explicit_jvm = None
        if FLAGS['compact_headers'] and explicit_jvm is not None and not any(
                option in ("-XX:+UseCompactObjectHeaders", "-XX:-UseCompactObjectHeaders")
                for option in explicit_jvm):
            jvm_flags.append("-XX:+UseCompactObjectHeaders")
        # Drop injections the user already specified to avoid duplicate-arg crashes (joptsimple rejects repeated value options)
        pre_names = {a.split("=", 1)[0] for a in (explicit_jvm if explicit_jvm is not None else pre_jar)}
        jvm_flags = [f for f in jvm_flags if f.split("=", 1)[0] not in pre_names]
        cmd = pre_jar + jvm_flags + cmd[jar_flag_idx:jar_idx+1] + server_flags + post_jar
        if FLAGS['aot']:
            try:
                if Path(cmd[0]).name != "java" or explicit_jvm is None:
                    raise ValueError("custom wrapper or opaque JVM options")
                if any("\\" in (value or "") for value in option_environment.values()) or any(
                        arg.startswith(("@", "-XX:VMOptionsFile=", "-XX:Flags=")) or arg == "--disable-@files"
                        for arg in cmd + explicit_jvm + shlex.split(option_environment["JDK_AOT_VM_OPTIONS"] or "")):
                    raise ValueError("opaque argument or options file")
                if any(re.match(
                        r"^-Xshare(?:[:=]|$)|^-XX:[+-]?(?:AOT|CDS|\w*(?:Shared|Archive)|DumpLoadedClassList|RecordDynamicDumpInfo)",
                        option) for option in explicit_jvm):
                    raise ValueError("manual AOT/CDS options")
                if os.pathsep in cmd[cmd.index("-jar") + 1] or "*" in cmd[cmd.index("-jar") + 1]:
                    raise ValueError("launch JAR cannot be validated as one classpath entry")
                cache, state_file = SERVER_DIR / "HytaleServer.aot", SERVER_DIR / AOT_STATE_FILE
                training_file = cache.with_name(cache.name + ".config")
                input_sha256 = aot_input_hash(cmd, launch_jar)
                try:
                    state = json.loads(state_file.read_text())
                    reusable = (isinstance(state, dict) and state.get("input_sha256") == input_sha256
                        and cache.is_file() and cache.stat().st_size > 0
                        and not path_present(training_file))
                except (OSError, ValueError, TypeError):
                    reusable = False
                if not reusable:
                    cache.unlink(missing_ok=True)
                    training_file.unlink(missing_ok=True)
                    publish_install_file(None, state_file, text=json.dumps({"input_sha256": input_sha256}) + "\n")
                mode = "AOTCache" if reusable else "AOTCacheOutput"
                aot_jar_index = cmd.index("-jar")
                aot_flags = ["-Xlog:aot", "-XX:+VerifySharedSpaces", f"-XX:{mode}={cache}"]
                cmd = cmd[:aot_jar_index] + aot_flags + cmd[aot_jar_index:]
                aot_reused = reusable
                log(C['C'], "[aot] Using AOT cache" if reusable else "[aot] Creating AOT cache")
            except Exception as e:
                log(C['Y'], f"[aot] Automatic cache bypassed: {e}")
    # Warn if host UDP buffers are smaller than recommended for QUIC traffic to improve latency
    if effective_transport in ("QUIC", "QUICHE"):
        for buf_type, buf_path in [("receive", "/proc/sys/net/core/rmem_max"), ("send", "/proc/sys/net/core/wmem_max")]:
            with suppress(Exception):
                if (current := int(Path(buf_path).read_text().strip())) < 2097152:
                    log(C['Y'], f"[quic] UDP {buf_type} buffer low ({current} bytes)")
    # Final version confirmation before launch
    log(C['G'], f"[startup] Launching: {final_version} ({final_patchline})")
    log(C['G'], f"[startup] ✓ Ready in {int(time.time() - start_time)}s")
    log(C['C'], f"[startup] Command: {' '.join(cmd)}")
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, handle_signal)
    try:
        server_process = subprocess.Popen(cmd, preexec_fn=os.setsid)
        exit_code = server_process.wait()
        if aot_reused and exit_code != 0 and not shutting_down:
            try:
                state_file.unlink(missing_ok=True)
            except OSError as e:
                log(C['Y'], f"[aot] Failed to invalidate cache input stamp: {e}")
        sys.exit(exit_code)
    finally:
        if auth_state:
            auth_state.cleanup()

if __name__ == "__main__":
    main()
