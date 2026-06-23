# Roomba V4 Controller

Cloud control for V4-protocol Roombas (Combo Essential and related models) from Python. First public documentation of the V4 command path, recovered through reverse engineering.

---

## What this is

iRobot's older Roombas (600/800/900/i/j/Braava families) speak a documented MQTT protocol that projects like [dorita980](https://github.com/koalazak/dorita980) have supported for years. The newest Combo Essential and related models speak something different — internally called the **V4 protocol** — and until now there was no public, working command path for it.

This project documents that path and ships a small Python controller that uses it.

## Status

| Capability | Status |
|---|---|
| Discovery + Gigya auth | Done |
| Full `/v2/login` flow | Done |
| MQTT connect via AWS IoT Custom Authorizer | Done |
| Command publish (`start`, `pause`, `stop`, `resume`, `dock`, `find`, `evac`, `reset`) | Done |
| Device shadow read (battery, mission, bin, pose, …) | Done |
| Auto token refresh (4 min) | Done |
| Set audio volume (`rw-settings`) | Payload identified, not live-tested |
| Schedule (`rw-schedule`) | Topic identified, payload TBD |
| Map / room cleaning (`pmap_id`, regions) | Not implemented |

## Compatibility

Anything iRobot returns with `svcDeplId: v011` in the discovery deployment should work. If you have a V4 model and it works (or doesn't), open an issue with your firmware string.

---

## The V4 protocol

Older Roombas published commands to the AWS device shadow (`$aws/things/{BLID}/shadow/update`). V4 doesn't. **Commands go to a separate iRobot-owned topic prefix.**

### Topic structure

```
{irbt_topics}/things/{BLID}/cmd
```

Where `irbt_topics` is returned by the discovery endpoint per deployment. For the current production deployment it is `v011-irbthbu`, so the full topic is:

```
v011-irbthbu/things/{BLID}/cmd
```

Payload:

```json
{
  "command": "start",
  "time": 1700000000,
  "initiator": "localApp"
}
```

### Confirmed command verbs

| Command | Effect |
|---|---|
| `start` | Begin cleaning |
| `pause` | Pause current mission |
| `stop` | Stop mission |
| `resume` | Resume paused mission |
| `dock` | Return to dock |
| `find` | Audible locate beep |
| `evac` | Empty bin into clean base |
| `reset` | Soft reset |
| `StartOnDemandOta` | Trigger OTA update check |

### State

Shadow topics are still used **for reading state** (the V4 change is only about commands):

```
$aws/things/{BLID}/shadow/get
$aws/things/{BLID}/shadow/update
$aws/things/{BLID}/shadow/get/accepted
```

Identified sub-shadows on V4 firmware: `ro-currentstate`, `ro-stats`, `ro-configinfo`, `ro-services`, `rw-settings`, `rw-software`, `rw-schedule`, `rw-constatus`.

---

## Authentication

Full chain from email + password to a working MQTT connection:

```
1. GET  disc-prod.iot.irobotapi.com/v1/discover/endpoints?country_code=XX
        -> gigya.api_key, gigya.datacenter_domain
        -> deployments[current].{httpBase, mqttAts, irbtTopics, awsRegion}

2. POST accounts.{gigya_domain}/accounts.login
        body: apiKey, loginID (email), password, targetEnv=mobile
        -> UID, UIDSignature, signatureTimestamp

3. POST {httpBase}/v2/login
        body: app_id, assume_robot_ownership, gigya={uid, signature, timestamp}
        -> iot_token, iot_signature, iot_authorizer_name, iot_clientid
        -> credentials (AWS Cognito: AccessKeyId, SecretKey, SessionToken)
        -> robots: { BLID: { name, sku, password, softwareVer, cap, ... } }

4. MQTT connect (direct TLS, port 443, ALPN x-amzn-mqtt-ca)
        endpoint: {mqttAts}
        custom authorizer:
          name      = iot_authorizer_name
          signature = iot_signature
          token header `x-irobot-auth` = iot_token
        client_id = iot_clientid
```

`iot_token` lifetime is roughly **5 minutes**. The controller re-runs steps 2–4 every 4 minutes in a background thread and reconnects transparently.

The AWS Cognito credentials from step 3 are not used for MQTT — they're for the HTTP API (maps, etc.). MQTT auth is entirely the custom authorizer.

---

## Installation

```bash
git clone https://github.com/lvigilantecorreo-commits/roomba-v4.git
cd roomba-v4
python3 -m venv .venv
source .venv/bin/activate
pip install requests awsiotsdk
```

## Usage

```bash
python roomba_controller.py
```

First run asks for your iRobot account email and password and caches them under `~/.roomba_ctl/config.json` (mode 0600, password XOR-wrapped — not strong encryption, treat the file as a secret).

Subsequent runs go straight in.

Environment overrides:

| Variable | Purpose |
|---|---|
| `ROOMBA_COUNTRY` | Country code for discovery (default `ES`) |

### Menu

```
  1) Start cleaning
  2) Pause
  3) Stop
  4) Resume
  5) Return to dock
  6) Locate (beep)
  7) Empty bin
  8) Reset
  s) Show status
  q) Quit
```

`s` prints parsed shadow state: battery (with bar), mission phase, cycle, error code (human-readable), runtime, area, bin state, floor type, position, Wi-Fi RSSI, dock status.

---

## How this was figured out

The path that worked, in order:

1. **Network reconnaissance.** Identified the Roomba's outbound traffic to AWS IoT. Captured the TLS-encrypted MQTT with `tcpdump`.

2. **mitmproxy on Android.** Installed mitmproxy CA as a system cert, opened the iRobot app, captured the discovery → Gigya → `/v2/login` chain in cleartext. This gave the API surface but not the protocol used after MQTT connect.

3. **APK extraction and analysis.** Pulled the Android app. **Gotcha: there are two iRobot apps.** The "classic" `com.irobot.home` does not contain the V4 strings. The newer `com.irobot.home.prime` (Roomba Home Prime) does.

4. **Native library inspection.** Ran `strings` on the `.so` files inside `lib/arm64-v8a/`. Looked for command-shaped tokens. Found the literal `/things/%s/cmd` — confirming the topic format, separate from device shadows.

5. **Ghidra on the relevant functions.** Disassembled around the call sites that fed `%s` into the topic format. Confirmed `irbtTopics` from the discovery response is the prefix, the BLID goes in `%s`, and the payload is the standard `{command,time,initiator}` shape.

6. **First connection attempt — WebSocket + SigV4.** Failed. The AWS Cognito credentials returned by `/v2/login` are not what authenticates MQTT.

7. **Second attempt — custom authorizer over WebSocket.** Handshake error, connection not upgraded.

8. **Third attempt — direct MQTT over TLS, port 443, ALPN `x-amzn-mqtt-ca`, custom authorizer with token in header `x-irobot-auth`.** Connect succeeded. Published `{"command":"start", ...}` to `v011-irbthbu/things/{BLID}/cmd`. The robot started cleaning.

---

## Files

```
roomba_controller.py    # Single-file CLI controller
~/.roomba_ctl/          # Created on first run
  config.json           # Email + obfuscated password + cached session (mode 0600)
```

## Disclaimer

This project is not affiliated with iRobot. It controls **your own Roomba on your own iRobot account**, programmatically, by speaking the same protocol the official app uses. Don't share your `config.json`. Don't use this against accounts or devices you don't own.

Tokens and authorizer parameters in this codebase are not secrets baked into the app — they're issued per-session by iRobot's own login endpoint after you successfully authenticate with your real credentials. There is no credential bypass, no key extraction, no spoofing.

## Credits

Inspired by [dorita980](https://github.com/koalazak/dorita980) (V1/V2/V3 protocols), [rest980](https://github.com/koalazak/rest980), and [python-irobotapi](https://github.com/mjg59/python-irobotapi) — they made the older protocols approachable and pointed the way for V4.

## About the author

Built by Ader, 16, as a summer project.

## License

MIT.
