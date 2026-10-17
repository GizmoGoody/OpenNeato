#!/usr/bin/env python3
"""Hand a Botvac D3-D7 over to a Vacuula server, using the OpenNeato bridge
for the serial half instead of the robot's USB port.

What happens, in order:

  1. The Vacuula server is checked and its certificate downloaded.
  2. Vacuula's Gen3 install scripts are fetched (pinned commit) and bundled with
     the certificate and the server address into the tar.gz the robot expects.
  3. The archive is POSTed to the OpenNeato bridge, which streams it to the robot
     over the debug UART ("Upload Tarball") and then tells the robot to reset
     its WiFi. The bridge pauses its own polling (quiet window).
  4. The robot comes up with its setup access point. Join that network from this
     computer; the script waits for the robot's provisioning API, re-arms the
     bridge's quiet window, sends your home WiFi credentials, the Vacuula
     hostnames and the hook that runs the install script, then follows progress.
  5. Success is the robot showing up in the Vacuula server's robot list. The
     robot's console is tailed through the bridge meanwhile, best effort: the
     install script reboots the robot at the end, which also reboots the bridge
     (it is powered from the robot), so console lines are a hint, not the verdict.

Only the standard library is used. See docs/vacuula.md for the background and
caveats (firmware versions, the quiet window, two controllers on one robot).

Vacuula's scripts are AGPL-3.0 and are downloaded at run time from
https://github.com/vacuula/installer; nothing of theirs is bundled here.
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import io
import json
import os
import ssl
import sys
import tarfile
import time
import urllib.error
import urllib.request
from typing import NoReturn

# vacuula/installer commit whose Gen3 scripts this flow was written against.
SCRIPTS_REF = "6cab3876608cd820fb0c176b83f8c79e16c5a936"
SCRIPTS_BASE = "https://raw.githubusercontent.com/vacuula/installer/{ref}/src/assets/scripts/gen3/"
SCRIPT_FILES = ("init.sh", "vacuula.sh")

# The robot's setup-mode provisioning API (same values Vacuula's installer uses).
ROBOT_AP_URL = "https://192.168.219.1:4443"
ROBOT_AP_HEADERS = {
    "accept": "application/vnd.neato.beehive.v1+json",
    "content-type": "application/json",
    "authorization": "Token token=vacuula-is-awesome",
    "user-agent": "okhttp/4.9.1",
}
ROBOT_PROGRESS_STEPS = {1: "joining WiFi", 2: "internet check", 3: "reaching the server", 4: "robot ready"}

UPLOAD_TIMEOUT_S = 90
AP_WAIT_TIMEOUT_S = 600
PROVISION_TIMEOUT_S = 300
CONSOLE_TIMEOUT_S = 150
ROBOT_APPEAR_TIMEOUT_S = 600
QUIET_REARM_MS = 900_000  # 15 min: the install runs, the robot reboots, the server link comes up


def log(msg: str) -> None:
    print(f"[provision] {msg}", flush=True)


def fail(msg: str) -> NoReturn:
    print(f"[provision] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


class Unreachable(Exception):
    """The host did not answer properly (refused, timed out, TLS failed, dropped mid-response)."""


# -- HTTP helpers ---------------------------------------------------------------


def http(url: str, method: str = "GET", data: bytes | None = None, headers: dict | None = None,
         timeout: float = 10.0, ctx: ssl.SSLContext | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, http.client.HTTPException, OSError, ssl.SSLError) as e:
        raise Unreachable(f"{url}: {e}") from e


def http_json(url: str, **kw) -> tuple[int, object]:
    status, body = http(url, **kw)
    try:
        return status, json.loads(body.decode("utf-8")) if body else None
    except ValueError:
        return status, body.decode("utf-8", errors="replace")


def robot_ap_context() -> ssl.SSLContext:
    """The robot's setup API speaks old TLS with a self-signed certificate."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for setter in (lambda: ctx.set_ciphers("ALL:@SECLEVEL=0"),
                   lambda: setattr(ctx, "minimum_version", ssl.TLSVersion.TLSv1)):
        try:
            setter()
        except (ssl.SSLError, ValueError):
            pass
    return ctx


# -- Step 1: server -------------------------------------------------------------


def check_server(base: str) -> tuple[dict, str]:
    status, info = http_json(f"{base}/api/server")
    if status != 200 or not isinstance(info, dict):
        fail(f"{base}/api/server answered {status}; is the Vacuula server running there?")
    status, cert = http(f"{base}/api/cert")
    cert_text = cert.decode("utf-8", errors="replace")
    if status != 200 or "BEGIN CERTIFICATE" not in cert_text:
        fail(f"{base}/api/cert did not return a certificate (HTTP {status})")
    log(f"Vacuula server {info.get('version', '?')} at {base}, certificate fetched")
    return info, cert_text


def server_robots(base: str) -> dict:
    """Vacuula's robot list keyed by serial: {serial: {type, connected, name}}."""
    try:
        status, data = http_json(f"{base}/api/robots", timeout=10)
    except Unreachable:
        return {}
    return data if status == 200 and isinstance(data, dict) else {}


# -- Step 2: archive ------------------------------------------------------------


def fetch_scripts(ref: str, scripts_dir: str | None) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    if scripts_dir:
        for name in SCRIPT_FILES:
            try:
                with open(os.path.join(scripts_dir, name), "rb") as f:
                    files[name] = f.read()
            except OSError as e:
                fail(f"cannot read {name} from {scripts_dir}: {e}")
        log(f"Using Vacuula scripts from {scripts_dir}")
        return files
    base = SCRIPTS_BASE.format(ref=ref)
    for name in SCRIPT_FILES:
        status, body = http(base + name, timeout=20)
        if status != 200 or not body:
            fail(f"could not download {name} from vacuula/installer@{ref[:10]} (HTTP {status})")
        files[name] = body
    log(f"Fetched Vacuula Gen3 scripts from vacuula/installer@{ref[:10]}")
    return files


def build_archive(scripts: dict[str, bytes], cert: str, server_ip: str) -> bytes:
    """Same layout as Vacuula's installer: scripts, cert.pem and a two-line config
    (server IP, then a nip.io name for it) at the archive root."""
    nip = "vacuula-local-" + server_ip.replace(".", "-") + ".nip.io"
    members = dict(scripts)
    members["cert.pem"] = cert.encode("utf-8")
    members["config"] = f"{server_ip}\n{nip}\n".encode("utf-8")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
    archive = buf.getvalue()
    log(f"Archive built: {len(archive)} bytes ({', '.join(members)})")
    return archive


# -- Step 3: bridge -------------------------------------------------------------


def bridge_status(bridge: str) -> dict:
    status, data = http_json(f"{bridge}/api/vacuula/status", timeout=15)
    if status != 200 or not isinstance(data, dict):
        fail(f"{bridge}/api/vacuula/status answered {status}: {data}")
    return data


def mac_from_serial(serial: str) -> str:
    parts = serial.split(",")
    if len(parts) >= 2 and len(parts[1].strip()) == 12:
        return parts[1].strip().lower()
    fail(f"could not extract the robot's MAC from serial {serial!r}")


def upload_via_bridge(bridge: str, archive: bytes, reset: bool, force: bool) -> dict:
    status = bridge_status(bridge)
    log(f"Robot via bridge: {status.get('model')} firmware {status.get('firmware')} "
        f"(supported={status.get('firmwareSupported')}, skey={status.get('skeyReady')})")
    if status.get("quiet"):
        fail("the bridge is in its quiet window from a previous run; "
             f"POST {bridge}/api/vacuula/resume or wait {status.get('quietRemainingMs', 0) // 1000}s")
    if not status.get("skeyReady"):
        fail("the bridge has not identified the robot yet (no SKey); wait for it and retry")
    if not force and not status.get("firmwareSupported"):
        fail(f"robot firmware {status.get('firmware')} is not one Vacuula's scripts know; "
             "update/factory-reset the robot to 4.5.3-189 or 4.6.0-72, or pass --force")

    url = f"{bridge}/api/vacuula/upload?reset={1 if reset else 0}&force={1 if force else 0}"
    code, resp = http_json(url, method="POST", data=archive,
                           headers={"content-type": "application/octet-stream"}, timeout=30)
    if code != 202:
        fail(f"bridge refused the upload (HTTP {code}): {resp}")
    log("Bridge accepted the archive, streaming it to the robot...")

    deadline = time.time() + UPLOAD_TIMEOUT_S
    last = ""
    while time.time() < deadline:
        time.sleep(1.0)
        s = bridge_status(bridge)
        line = f"{s.get('phase')} {s.get('bytesSent')}/{s.get('bytesTotal')}"
        if line != last:
            log(f"  upload: {line}")
            last = line
        if s.get("phase") == "done":
            log(f"Robot accepted the archive: {s.get('message')}")
            return s
        if s.get("phase") == "failed":
            fail(f"upload failed: {s.get('message')}")
    fail("timed out waiting for the upload to finish")


def wait_reset_sent(bridge: str) -> int:
    """Returns the console sequence number to tail from."""
    for _ in range(20):
        s = bridge_status(bridge)
        if s.get("resetError"):
            fail(f"the bridge could not send the network reset: {s['resetError']}")
        if s.get("resetSent"):
            log("Network reset sent; the robot is bringing up its setup access point")
            return int(s.get("logNext") or 0)
        time.sleep(0.5)
    fail("the bridge did not confirm the network reset; check its log")


def rearm_quiet(bridge: str, ms: int) -> None:
    try:
        http(f"{bridge}/api/vacuula/quiet?ms={ms}", method="POST", timeout=5)
        log(f"Bridge quiet window re-armed for {ms // 60000} min")
    except Unreachable as e:
        log(f"could not re-arm the bridge quiet window ({e}); continuing")


def resume_bridge(bridge: str) -> None:
    try:
        http(f"{bridge}/api/vacuula/resume", method="POST", timeout=5)
    except Unreachable:
        pass


# -- Step 4: robot setup network ------------------------------------------------


def wait_for_robot_ap(ctx: ssl.SSLContext) -> dict:
    log("Connect this computer to the robot's setup WiFi network now "
        "(a new network broadcast by the robot). Waiting for it...")
    deadline = time.time() + AP_WAIT_TIMEOUT_S
    while time.time() < deadline:
        try:
            status, info = http_json(f"{ROBOT_AP_URL}/info", headers=ROBOT_AP_HEADERS, timeout=5, ctx=ctx)
            if status == 200 and isinstance(info, dict):
                log(f"Robot reachable in setup mode: {json.dumps(info)[:200]}")
                return info
        except Unreachable:
            pass
        time.sleep(5)
    fail("the robot's setup network never became reachable")


def provision_robot(ctx: ssl.SSLContext, mac: str, ssid: str, password: str) -> None:
    body = {
        "name": f"gen3-{mac}",
        "password": password,
        "server_urls": {"beehive": "vacuula", "ntp": "pool.ntp.org", "nucleo": "vacuula"},
        "ssid": ssid,
        # Vacuula keeps the robot on UTC year-round; the server applies the local zone.
        "timezone": "Atlantic/Reykjavik",
        "user_id": "vacuula-user",
        # The robot evaluates this once the settings are applied; it extracts and runs init.sh
        # from the uploaded archive, which must therefore still be in the robot's temporary
        # storage (same power cycle as the upload).
        "utc_offset": "UTC+0:00UTC+0:00;{ /bin/tar xOzf /tmp/upload init.sh|/bin/sh &}",
    }
    try:
        status, resp = http_json(f"{ROBOT_AP_URL}/robot/initialize", method="PUT",
                                 data=json.dumps(body).encode("utf-8"), headers=ROBOT_AP_HEADERS,
                                 timeout=20, ctx=ctx)
    except Unreachable as e:
        fail(f"robot initialize request failed: {e}")
    if status // 100 != 2:
        fail(f"robot refused initialize (HTTP {status}): {resp}")
    log(f"Robot accepted the configuration for network {ssid!r}; it is joining your WiFi")


def follow_robot_progress(ctx: ssl.SSLContext) -> None:
    deadline = time.time() + PROVISION_TIMEOUT_S
    last_step = 0
    failures = 0
    while time.time() < deadline:
        time.sleep(3)
        try:
            status, data = http_json(f"{ROBOT_AP_URL}/robot/wifi_networks/new/progress",
                                     headers=ROBOT_AP_HEADERS, timeout=5, ctx=ctx)
        except Unreachable:
            failures += 1
            if failures >= 5:
                log("Setup network gone; the robot has moved to your WiFi")
                return
            continue
        failures = 0
        step = data.get("step") if status == 200 and isinstance(data, dict) else None
        if isinstance(step, int) and step != last_step:
            last_step = step
            log(f"  robot: step {step} ({ROBOT_PROGRESS_STEPS.get(step, '?')})")
        if isinstance(step, int) and step >= 4:
            return
    log("No more progress reports from the setup network; moving on")


# -- Step 5: console via the bridge, then the server ------------------------------


def tail_console(bridge: str, since: int) -> None:
    """Best effort: print what the robot says on its debug UART until the install
    script reboots it (which also reboots the bridge) or the timeout passes."""
    log("Reconnect this computer to your normal network. Tailing the robot console via the bridge...")
    deadline = time.time() + CONSOLE_TIMEOUT_S
    unreachable = 0
    while time.time() < deadline:
        time.sleep(2)
        try:
            status, data = http_json(f"{bridge}/api/vacuula/log?since={since}", timeout=5)
        except Unreachable:
            unreachable += 1
            if unreachable >= 5:
                log("Bridge unreachable; the robot (and the bridge with it) is probably rebooting")
                return
            continue
        unreachable = 0
        if status != 200 or not isinstance(data, dict):
            continue
        for line in data.get("lines", []):
            text = line.get("text", "")
            print(f"  robot> {text}", flush=True)
            if "vacuula-installer-failed" in text:
                log("The robot's install script reported a failure")
                return
            if "Rebooting" in text:
                log("Install script finished on the robot; it is rebooting")
                return
        since = data.get("next", since)
    log("Console tail timed out (the bridge may have missed the lines); checking the server instead")


def wait_for_robot_on_server(base: str, before: dict, mac: str) -> dict | None:
    log(f"Waiting for the robot to connect to {base} (up to {ROBOT_APPEAR_TIMEOUT_S // 60} min)...")
    deadline = time.time() + ROBOT_APPEAR_TIMEOUT_S
    while time.time() < deadline:
        robots = server_robots(base)
        for serial, info in robots.items():
            is_new = serial not in before
            is_ours = mac in serial.lower().replace(":", "")
            if (is_new or is_ours) and info.get("connected"):
                log(f"Robot {serial} is connected to Vacuula as {info.get('name')!r} ({info.get('type')})")
                return {serial: info}
        time.sleep(10)
    return None


# -- main ------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bridge", required=True, help="OpenNeato bridge host or IP (e.g. neato.local)")
    p.add_argument("--server", required=True, help="Vacuula server LAN IP (what the robot will connect to)")
    p.add_argument("--server-port", type=int, default=80, help="Vacuula web/API port (default 80)")
    p.add_argument("--ssid", help="home WiFi network the robot should join")
    p.add_argument("--password", help="home WiFi password (prompted if omitted)")
    p.add_argument("--no-reset", action="store_true", help="upload only; do not reset the robot's WiFi")
    p.add_argument("--force", action="store_true", help="skip the model/firmware gate on the bridge")
    p.add_argument("--scripts-ref", default=SCRIPTS_REF, help="vacuula/installer git ref for the Gen3 scripts")
    p.add_argument("--scripts-dir", help="use local copies of init.sh and vacuula.sh instead of downloading")
    p.add_argument("--archive-out", help="also write the generated tar.gz to this path")
    p.add_argument("--skip-upload", action="store_true", help="skip the bridge upload (robot already has the archive)")
    p.add_argument("--skip-provision", action="store_true", help="stop after the upload and network reset")
    args = p.parse_args()

    bridge = args.bridge if "://" in args.bridge else f"http://{args.bridge}"
    bridge = bridge.rstrip("/")
    server_base = f"http://{args.server}:{args.server_port}" if args.server_port != 80 else f"http://{args.server}"
    if "." not in args.server or not all(part.isdigit() for part in args.server.split(".")):
        fail("--server must be the server's IPv4 address; the robot cannot resolve names on your LAN")

    _, cert = check_server(server_base)
    robots_before = server_robots(server_base)
    scripts = fetch_scripts(args.scripts_ref, args.scripts_dir)
    archive = build_archive(scripts, cert, args.server)
    if args.archive_out:
        try:
            with open(args.archive_out, "wb") as f:
                f.write(archive)
        except OSError as e:
            fail(f"cannot write {args.archive_out}: {e}")
        log(f"Archive written to {args.archive_out}")

    status = bridge_status(bridge)
    mac = mac_from_serial(status.get("serial", ""))
    log_since = int(status.get("logNext") or 0)

    if not args.skip_upload:
        upload_via_bridge(bridge, archive, reset=not args.no_reset, force=args.force)
        if not args.no_reset:
            log_since = wait_reset_sent(bridge)

    if args.no_reset or args.skip_provision:
        log("Done with the serial half. Remember: the archive lives in the robot's temporary storage, "
            "so finish provisioning before the robot is power-cycled.")
        return 0

    ssid = args.ssid or input("Home WiFi network name (SSID): ").strip()
    if not ssid:
        fail("an SSID is required")
    password = args.password if args.password is not None else getpass.getpass("Home WiFi password: ")

    ctx = robot_ap_context()
    wait_for_robot_ap(ctx)
    provision_robot(ctx, mac, ssid, password)
    follow_robot_progress(ctx)
    rearm_quiet(bridge, QUIET_REARM_MS)
    tail_console(bridge, log_since)
    found = wait_for_robot_on_server(server_base, robots_before, mac)
    resume_bridge(bridge)

    if found:
        log(f"Hand-over complete. Open {server_base}/ to see the robot.")
        return 0
    log("The robot did not show up on the Vacuula server in time. Check the server log "
        "(Komodo/docker logs), the robot's console lines above, and that the robot rejoined your WiFi.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Unreachable as e:
        fail(f"cannot reach {e}")
    except KeyboardInterrupt:
        fail("interrupted")
