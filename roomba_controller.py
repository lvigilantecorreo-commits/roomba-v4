#!/usr/bin/env python3
import os
import sys
import json
import time
import base64
import threading
import itertools
from pathlib import Path
from getpass import getpass

import requests
from awscrt import io, mqtt, http
from awsiot import mqtt_connection_builder


CONFIG_DIR  = Path.home() / ".roomba_ctl"
CONFIG_FILE = CONFIG_DIR / "config.json"

DISCOVERY_URL = "https://disc-prod.iot.irobotapi.com/v1/discover/endpoints"
APP_ID        = "ANDROID-C7FB240E-DF34-42D7-AE4E-A8C17079A294"
USER_AGENT    = "aspen production/4 CFNetwork/1474 Darwin/23.0.0"
TOKEN_HEADER  = "x-irobot-auth"

REFRESH_EVERY = 240    # seconds — tokens expire ~5 min
SESSION_TTL   = 200    # treat cached session older than this as stale

SHADOW_GET    = "$aws/things/{blid}/shadow/get"
SHADOW_UPDATE = "$aws/things/{blid}/shadow/update"

COMMANDS = [
    ("1", "start",  "Start cleaning"),
    ("2", "pause",  "Pause"),
    ("3", "stop",   "Stop"),
    ("4", "resume", "Resume"),
    ("5", "dock",   "Return to dock"),
    ("6", "find",   "Locate (beep)"),
    ("7", "evac",   "Empty bin"),
    ("8", "reset",  "Reset"),
]

PHASE = {
    "charge":    "Charging",
    "run":       "Cleaning",
    "stop":      "Idle",
    "pause":     "Paused",
    "hmUsrDock": "Returning to dock",
    "hmMidMsn":  "Returning (mid-mission)",
    "hmPostMsn": "Returning (post-mission)",
    "evac":      "Emptying bin",
    "chargingerror": "Charging error",
}

CYCLE = {
    "none":  "Idle",
    "clean": "Standard clean",
    "spot":  "Spot clean",
    "dock":  "Docking",
    "evac":  "Bin empty",
    "train": "Mapping run",
}

ERRORS = {
    0:  "OK",
    1:  "Left wheel off floor",
    2:  "Main brush stuck",
    5:  "Right wheel off floor",
    6:  "Cliff sensor",
    8:  "Vacuum motor",
    9:  "Bumper stuck",
    11: "Vacuum motor",
    14: "Bin missing",
    15: "Reboot required",
    16: "Bumped while docking",
    17: "Path blocked",
    18: "Docking failed",
    34: "Bin full",
    46: "Battery low",
    66: "Battery very low",
    68: "Lost, please relocate",
}


class Spinner:
    def __init__(self, label):
        self.label = label
        self._stop = threading.Event()
        self._t = None

    def __enter__(self):
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._t:
            self._t.join(timeout=1)
        sys.stdout.write("\r" + " " * (len(self.label) + 8) + "\r")
        sys.stdout.flush()

    def _run(self):
        frames = itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
        while not self._stop.is_set():
            sys.stdout.write(f"\r{next(frames)} {self.label}")
            sys.stdout.flush()
            time.sleep(0.08)


def _wrap(text, key="rcv1"):
    raw = text.encode()
    return base64.b64encode(bytes(b ^ ord(key[i % len(key)]) for i, b in enumerate(raw))).decode()

def _unwrap(text, key="rcv1"):
    raw = base64.b64decode(text.encode())
    return bytes(b ^ ord(key[i % len(key)]) for i, b in enumerate(raw)).decode()


class Store:
    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self.data = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except json.JSONDecodeError:
                self.data = {}

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @property
    def email(self):
        return self.data.get("email")

    @property
    def password(self):
        p = self.data.get("password")
        return _unwrap(p) if p else None

    def set_credentials(self, email, password):
        self.data["email"] = email
        self.data["password"] = _wrap(password)
        self.save()

    def cached_session(self):
        s = self.data.get("session")
        if not s:
            return None
        if time.time() - s.get("_at", 0) > SESSION_TTL:
            return None
        return s

    def save_session(self, session):
        s = dict(session)
        s["_at"] = int(time.time())
        self.data["session"] = s
        self.save()

    def forget_session(self):
        self.data.pop("session", None)
        self.save()

    def forget_all(self):
        self.data = {}
        self.save()


class IRobotCloud:
    def __init__(self, country="ES"):
        self.country = country
        self.http = requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT

    def discover(self):
        r = self.http.get(DISCOVERY_URL, params={"country_code": self.country}, timeout=15)
        r.raise_for_status()
        d = r.json()
        current = d["current_deployment"]
        dep = d["deployments"][current]
        return {
            "http_base":    dep["httpBase"],
            "region":       dep["awsRegion"],
            "mqtt":         dep.get("mqttAts") or dep["mqtt"],
            "irbt_topics":  dep["irbtTopics"],
            "deployment":   current,
            "gigya_key":    d["gigya"]["api_key"],
            "gigya_domain": d["gigya"]["datacenter_domain"],
        }

    def _gigya_login(self, email, password, key, domain):
        r = self.http.post(f"https://accounts.{domain}/accounts.login", data={
            "apiKey":    key,
            "targetenv": "mobile",
            "targetEnv": "mobile",
            "loginID":   email,
            "password":  password,
            "format":    "json",
        }, timeout=15)
        r.raise_for_status()
        j = r.json()
        if j.get("statusCode") != 200:
            raise RuntimeError(j.get("errorDetails") or j.get("errorMessage") or "Authentication failed")
        return {
            "uid":       j["UID"],
            "signature": j["UIDSignature"],
            "timestamp": j["signatureTimestamp"],
        }

    def _account_login(self, http_base, gigya):
        r = self.http.post(f"{http_base}/v2/login", json={
            "app_id": APP_ID,
            "assume_robot_ownership": "0",
            "gigya": {
                "signature": gigya["signature"],
                "timestamp": gigya["timestamp"],
                "uid":       gigya["uid"],
            },
        }, timeout=20)
        r.raise_for_status()
        return r.json()

    def login(self, email, password, discovery=None):
        disco = discovery or self.discover()
        gigya = self._gigya_login(email, password, disco["gigya_key"], disco["gigya_domain"])
        account = self._account_login(disco["http_base"], gigya)
        account["_discovery"] = disco
        return account


class Roomba:
    def __init__(self, store, cloud, session):
        self.store    = store
        self.cloud    = cloud
        self.session  = session
        self._load_session(session)

        self.conn     = None
        self.state    = {}
        self._lock    = threading.Lock()
        self._fresh   = threading.Event()
        self._refresh_stop = threading.Event()
        self._refresh_thread = None
        self._reconnect_lock = threading.Lock()

    def _load_session(self, session):
        robots = session.get("robots") or {}
        if not robots:
            raise RuntimeError("No robots linked to this account")
        blid = next(iter(robots))
        if len(robots) > 1 and not getattr(self, "blid", None):
            blid = self._pick(robots)
        elif getattr(self, "blid", None):
            blid = self.blid
        info = robots[blid]
        self.blid = blid
        self.name = info.get("name", blid)

        for k in ("iot_token", "iot_signature", "iot_authorizer_name", "iot_clientid"):
            if not session.get(k):
                raise RuntimeError(f"Login response missing {k}")
        self.iot_token         = session["iot_token"]
        self.iot_signature     = session["iot_signature"]
        self.authorizer_name   = session["iot_authorizer_name"]
        self.client_id         = session["iot_clientid"]

        disco = session.get("_discovery", {})
        self.endpoint    = disco["mqtt"]
        self.irbt_topics = disco["irbt_topics"]
        self.region      = disco.get("region", "us-east-1")

    def _pick(self, robots):
        print()
        items = list(robots.items())
        for i, (blid, info) in enumerate(items, 1):
            print(f"  {i}. {info.get('name', blid)}  [{blid}]")
        while True:
            try:
                n = int(input("Select robot> ").strip())
                if 1 <= n <= len(items):
                    return items[n - 1][0]
            except ValueError:
                pass

    def _cmd_topic(self):
        return f"{self.irbt_topics}/things/{self.blid}/cmd"

    def connect(self):
        loop = io.EventLoopGroup(1)
        resolver = io.DefaultHostResolver(loop)
        bootstrap = io.ClientBootstrap(loop, resolver)

        tls_opts = io.TlsContextOptions()
        tls_opts.alpn_list = ["x-amzn-mqtt-ca"]
        tls_ctx = io.ClientTlsContext(tls_opts)

        self.conn = mqtt_connection_builder.direct_with_custom_authorizer(
            endpoint=self.endpoint,
            auth_authorizer_name=self.authorizer_name,
            auth_authorizer_signature=self.iot_signature,
            auth_token_key_name=TOKEN_HEADER,
            auth_token_value=self.iot_token,
            client_id=self.client_id,
            client_bootstrap=bootstrap,
            tls_ctx=tls_ctx,
            clean_session=True,
            keep_alive_secs=30,
        )
        self.conn.connect().result(timeout=15)

        self.conn.subscribe(
            topic=SHADOW_UPDATE.format(blid=self.blid),
            qos=mqtt.QoS.AT_LEAST_ONCE,
            callback=self._on_msg,
        )[0].result(timeout=10)

        self.conn.subscribe(
            topic=f"$aws/things/{self.blid}/shadow/get/accepted",
            qos=mqtt.QoS.AT_LEAST_ONCE,
            callback=self._on_msg,
        )[0].result(timeout=10)

        self.refresh_state()
        self._start_refresh_loop()

    def _start_refresh_loop(self):
        if self._refresh_thread and self._refresh_thread.is_alive():
            return
        self._refresh_stop.clear()
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

    def _refresh_loop(self):
        while not self._refresh_stop.wait(REFRESH_EVERY):
            try:
                self._reauth_and_reconnect()
            except Exception:
                pass

    def _reauth_and_reconnect(self):
        with self._reconnect_lock:
            new_session = self.cloud.login(
                self.store.email,
                self.store.password,
                discovery=self.session.get("_discovery"),
            )
            self.store.save_session(new_session)
            self.session = new_session
            self._load_session(new_session)

            try:
                self.conn.disconnect().result(timeout=5)
            except Exception:
                pass

            loop = io.EventLoopGroup(1)
            resolver = io.DefaultHostResolver(loop)
            bootstrap = io.ClientBootstrap(loop, resolver)
            tls_opts = io.TlsContextOptions()
            tls_opts.alpn_list = ["x-amzn-mqtt-ca"]
            tls_ctx = io.ClientTlsContext(tls_opts)

            self.conn = mqtt_connection_builder.direct_with_custom_authorizer(
                endpoint=self.endpoint,
                auth_authorizer_name=self.authorizer_name,
                auth_authorizer_signature=self.iot_signature,
                auth_token_key_name=TOKEN_HEADER,
                auth_token_value=self.iot_token,
                client_id=self.client_id,
                client_bootstrap=bootstrap,
                tls_ctx=tls_ctx,
                clean_session=True,
                keep_alive_secs=30,
            )
            self.conn.connect().result(timeout=15)
            self.conn.subscribe(
                topic=SHADOW_UPDATE.format(blid=self.blid),
                qos=mqtt.QoS.AT_LEAST_ONCE,
                callback=self._on_msg,
            )[0].result(timeout=10)
            self.conn.subscribe(
                topic=f"$aws/things/{self.blid}/shadow/get/accepted",
                qos=mqtt.QoS.AT_LEAST_ONCE,
                callback=self._on_msg,
            )[0].result(timeout=10)

    def _on_msg(self, topic, payload, **_):
        try:
            data = json.loads(payload.decode())
        except Exception:
            return
        with self._lock:
            reported = data.get("state", {}).get("reported") or data.get("state") or {}
            self.state.update(reported)
            self._fresh.set()

    def refresh_state(self):
        self._fresh.clear()
        with self._reconnect_lock:
            self.conn.publish(
                topic=SHADOW_GET.format(blid=self.blid),
                payload=b"",
                qos=mqtt.QoS.AT_LEAST_ONCE,
            )
        self._fresh.wait(timeout=4)

    def send(self, command):
        payload = json.dumps({
            "command":   command,
            "time":      int(time.time()),
            "initiator": "localApp",
        }).encode()
        with self._reconnect_lock:
            self.conn.publish(
                topic=self._cmd_topic(),
                payload=payload,
                qos=mqtt.QoS.AT_LEAST_ONCE,
            )[0].result(timeout=5)

    def snapshot(self):
        with self._lock:
            return dict(self.state)

    def disconnect(self):
        self._refresh_stop.set()
        if self.conn:
            try:
                self.conn.disconnect().result(timeout=5)
            except Exception:
                pass


def render_state(s):
    if not s:
        return "  No state received yet."
    out = []
    add = out.append

    if s.get("name"):
        add(f"  Name        {s['name']}")
    if s.get("sku"):
        add(f"  Model       {s['sku']}")
    fw = s.get("softwareVer") or s.get("soft_ver")
    if fw:
        add(f"  Firmware    {fw}")

    batt = s.get("batPct")
    if batt is not None:
        filled = max(0, min(20, batt // 5))
        add(f"  Battery     {batt}%  [{'█' * filled}{'░' * (20 - filled)}]")

    cs = s.get("cleanMissionStatus") or {}
    if cs.get("phase"):
        add(f"  State       {PHASE.get(cs['phase'], cs['phase'])}")
    if cs.get("cycle"):
        add(f"  Cycle       {CYCLE.get(cs['cycle'], cs['cycle'])}")
    if cs.get("error"):
        err = cs["error"]
        add(f"  Error       {ERRORS.get(err, f'Code {err}')}")
    if cs.get("mssnM"):
        add(f"  Runtime     {cs['mssnM']} min")
    if cs.get("sqft") is not None:
        add(f"  Area        {cs['sqft']} ft²")

    bin_s = s.get("bin") or {}
    if bin_s:
        flags = []
        flags.append("present" if bin_s.get("present") else "missing")
        if bin_s.get("full"):
            flags.append("FULL")
        add(f"  Bin         {', '.join(flags)}")

    pad = s.get("detectedPad") or s.get("padWetness")
    if pad:
        add(f"  Pad/floor   {pad}")

    pose = s.get("pose") or {}
    if pose.get("point"):
        p = pose["point"]
        add(f"  Position    x={p.get('x')}  y={p.get('y')}  θ={pose.get('theta')}")

    sig = s.get("signal")
    rssi = sig.get("rssi") if isinstance(sig, dict) else sig
    if rssi is not None:
        add(f"  Wi-Fi RSSI  {rssi} dBm")

    if s.get("dock"):
        d = s["dock"]
        if d.get("known") is not None:
            add(f"  Dock        {'known' if d['known'] else 'unknown'}")

    return "\n".join(out) if out else "  (state empty)"


def ask_credentials():
    print()
    email = input("Email:    ").strip()
    pw    = getpass("Password: ")
    if not email or not pw:
        print("Empty credentials, aborting.")
        sys.exit(1)
    return email, pw


def login_flow(store, cloud, force=False):
    if force or not store.email or not store.password:
        email, pw = ask_credentials()
        store.set_credentials(email, pw)

    if not force:
        cached = store.cached_session()
        if cached:
            return cached

    with Spinner("Logging in"):
        try:
            session = cloud.login(store.email, store.password)
        except RuntimeError as e:
            print(f"Login failed: {e}")
            store.forget_all()
            sys.exit(1)
        except requests.HTTPError as e:
            print(f"Server error: {e}")
            sys.exit(1)
        except requests.RequestException as e:
            print(f"Network error: {e}")
            sys.exit(1)
    store.save_session(session)
    return session


def main():
    print("Roomba controller")

    country = os.environ.get("ROOMBA_COUNTRY", "ES")
    store   = Store()
    cloud   = IRobotCloud(country=country)
    session = login_flow(store, cloud)

    try:
        with Spinner("Connecting"):
            robot = Roomba(store, cloud, session)
            robot.connect()
    except Exception as e:
        print(f"Connection failed: {e}")
        store.forget_session()
        with Spinner("Re-authenticating"):
            session = login_flow(store, cloud, force=False)
        with Spinner("Connecting"):
            robot = Roomba(store, cloud, session)
            robot.connect()

    print(f"Connected: {robot.name}")

    try:
        while True:
            print()
            for key, _, label in COMMANDS:
                print(f"  {key}) {label}")
            print("  s) Show status")
            print("  q) Quit")
            choice = input("> ").strip().lower()

            if choice == "q":
                break

            if choice == "s":
                with Spinner("Refreshing"):
                    robot.refresh_state()
                print()
                print(render_state(robot.snapshot()))
                continue

            match = next(((c, l) for k, c, l in COMMANDS if k == choice), None)
            if not match:
                continue
            cmd, label = match
            with Spinner(f"Sending {label.lower()}"):
                try:
                    robot.send(cmd)
                except Exception as e:
                    print(f"Send failed: {e}")
    finally:
        with Spinner("Disconnecting"):
            robot.disconnect()
        print("Disconnected")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(0)
