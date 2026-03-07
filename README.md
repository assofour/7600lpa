# 7600lpa — SGP.22 LPA for SIM7600G-H

A Python implementation of the **GSMA SGP.22 Local Profile Assistant (LPA)** that runs on a Raspberry Pi with a SIMCOM SIM7600G-H modem. Downloads, installs, enables, and manages eSIM profiles via AT+CSIM — no phone or PC/SC reader required.

## What It Does

Full end-to-end eSIM profile lifecycle:

```
SM-DP+ Server                    Raspberry Pi + SIM7600G-H + eUICC card
─────────────────────────         ────────────────────────────────────────
                                  1. SELECT ISD-R (logical channel)
                                  2. GetEUICCChallenge (BF2E)
                                  3. GetEUICCInfo1 (BF20)
  4. initiateAuthentication  ←──  sends challenge + euiccInfo1
  5. returns serverSigned1   ──→  AuthenticateServer (BF38)
  6. authenticateClient      ←──  sends euicc auth response
  7. returns smdpSigned2     ──→  PrepareDownload (BF21)
  8. getBoundProfilePackage  ←──  sends prepare response
  9. returns BPP (48KB)      ──→  LoadBoundProfilePackage (STORE DATA × 54 segments)
                                  10. EnableProfile (BF31) ✓
                                  11. AT+CFUN=1,1 → modem reboots on new profile
```

**Tested and working** with [Linksfield Networks](https://linksfield.net) eSIM and [Wireless Panda](https://wirelesspanda.com) in the United States. Full profile download, enable, and LTE data connection with 166ms latency to google.com.

## Hardware

| Component | Details |
|-----------|---------|
| Host | Raspberry Pi 5, Pi OS Bookworm |
| Modem | SIMCOM SIM7600G-H M.2 (firmware `LE20B04SIM7600G-H-M2`) |
| eSIM card | 9eSIM removable eUICC (Kigen chip, SGP.22 v2.3.0) |
| AT port | `/dev/ttyUSB2` or `/dev/ttyUSB3` (may change after reboot) |

> **Note**: The SIM7600G-H does NOT have a built-in eUICC. The 9eSIM is a separately purchased physical eSIM card inserted into the SIM slot.

## Quick Start

### 1. Install

```bash
git clone https://git.emmc.cc/edward/7600lpa.git
cd 7600lpa
pip install -r requirements.txt
```

### 2. Download & Enable

Pass the LPA activation code from your eSIM QR code directly:
```bash
# From QR code (format: LPA:1$<address>$<matchingId>)
sudo python3 download.py --lpa 'LPA:1$smdp.example.com$MATCHING-ID' --no-ssl-verify

# Or via separate flags
sudo python3 download.py --smdp smdp.example.com --mid MATCHING-ID --no-ssl-verify
```

Optionally, copy `config.yaml.example` to `config.yaml` to set defaults for transport port, logging, etc:
```bash
cp config.yaml.example config.yaml
```

The `--no-ssl-verify` flag is needed because GSMA RSP2 Root CI certificates are not in the OS trust store.

After success, reboot the modem:
```bash
echo 'AT+CFUN=1,1' | sudo socat - /dev/ttyUSB2,b115200,crnl

# Wait 30s, then verify
echo 'AT+COPS?' | sudo socat - /dev/ttyUSB2,b115200,crnl
# Expected: +COPS: 0,0,"Your Operator",7
```

### 3. Profile Management

```bash
# List all installed profiles
sudo python3 download.py --list

# Enable a profile by ICCID (then reboot modem with AT+CFUN=1,1)
sudo python3 download.py --enable 8955170230005315472

# Disable an active profile
sudo python3 download.py --disable 8955170230005315472

# Delete a profile (must be disabled first)
sudo python3 download.py --disable 8955170230005315472
sudo python3 download.py --delete 8955170230005315472
```

### 4. Connect to Internet

```bash
# Stop ModemManager, start QMI data, configure wwan0
sudo systemctl stop ModemManager
sudo qmicli -d /dev/cdc-wdm0 --wds-start-network='apn=mvnoc.data,ip-type=4' --client-no-release-cid
sudo ip link set wwan0 down
echo Y | sudo tee /sys/class/net/wwan0/qmi/raw_ip
sudo ip link set wwan0 up

# Get settings from QMI and apply
sudo qmicli -d /dev/cdc-wdm0 --wds-get-current-settings
# Then manually: sudo ip addr add <IP>/<mask> dev wwan0
#                sudo ip route add default via <gateway> dev wwan0 metric 100
```

Or use `connect.py` for an interactive connection manager.

## CLI Reference

```
usage: download.py [-h] [--lpa LPA] [--smdp SMDP] [--mid MID]
                   [--list] [--delete ICCID] [--disable ICCID] [--enable ICCID]
                   [--config CONFIG] [--eid EID] [--port PORT]
                   [--no-ssl-verify] [--mock] [--debug]

Download:
  --lpa LPA             LPA activation code: 'LPA:1$<address>$<matchingId>'
  --smdp SMDP           SM-DP+ FQDN (overrides config)
  --mid MID             Matching ID (overrides config)

Profile management:
  --list                List installed profiles and exit
  --enable ICCID        Enable a profile by ICCID and exit
  --disable ICCID       Disable a profile by ICCID and exit
  --delete ICCID        Delete a profile by ICCID and exit

General:
  --config CONFIG       Config file [config.yaml]
  --port PORT           Serial port override
  --no-ssl-verify       Skip TLS cert verification
  --mock                Use MockTransport (no hardware)
  --debug               Enable DEBUG logging
```

## Project Structure

```
7600lpa/
├── download.py          # Main entry: profile download, enable, disable, delete, list
├── transport.py         # APDU transport (MockTransport, RealTransport, QmiTransport)
├── lpa_manager.py       # ISD-R selection, EID retrieval, eUICCInfo2
├── main.py              # Bootstrap: SELECT ISD-R → GET EID → GET eUICCInfo2
├── connect.py           # Cellular connection manager (APN, QMI, routing)
├── config.yaml.example  # Configuration template (copy to config.yaml)
├── requirements.txt     # Python dependencies
├── tools/               # Diagnostic and debug utilities
│   ├── enable_debug.py        # EnableProfile diagnostic (A0-wrapper tests)
│   ├── enable_profile.py      # Standalone EnableProfile probe
│   ├── check_profiles.py      # List eUICC profiles and state
│   ├── probe_es10c.py         # ES10c command probing
│   ├── probe_es10c_v2.py      # ES10c v2 extended probing
│   ├── probe_profiles.py      # Profile metadata inspection
│   ├── profile_mgmt.py        # Enable/disable/delete profiles
│   ├── cancel_and_download.py # Cancel pending session + re-download
│   ├── delete_and_download.py # Delete profile + re-download
│   ├── clear_notifications.py # Clear eUICC notification queue
│   └── process_notifications.py # Process and send pending notifications
└── docs/
    └── progress.md      # Development log and debugging history
```

## Key Technical Details

### STORE DATA Bypass

The SIM7600G-H firmware filters certain APDU instructions via AT+CSIM:
- **Blocked**: SELECT by AID on ch0, GET EUICC CHALLENGE (INS=BA), GET STATUS (INS=F2)
- **Allowed**: MANAGE CHANNEL (INS=70), STORE DATA (INS=E2), GET DATA (INS=CA)

All ES10b/ES10c commands are sent via STORE DATA (INS=E2) to ISD-R, bypassing the filter entirely. This is the same mechanism used by GlobalPlatform for secure channel operations.

### The A0 Wrapper Bug (EnableProfile Fix)

The critical bug that blocked EnableProfile for weeks: SGP.22 uses `AUTOMATIC TAGS` in its ASN.1 module. Per ASN.1 encoding rules, when IMPLICIT tagging is applied to a CHOICE type, it becomes EXPLICIT. The `profileIdentifier` field in `EnableProfileRequest` is a CHOICE inside a SEQUENCE, requiring an explicit `A0` (context-specific constructed [0]) wrapper.

```
WRONG  (undefinedError 127):
  BF31 { 5A 0A <ICCID> 81 01 01 }

CORRECT (enableResult 0 = ok):
  BF31 { A0 { 5A 0A <ICCID> } 81 01 FF }
         ^^                        ^^
         EXPLICIT [0] wrapper      DER canonical TRUE
```

This does NOT affect DeleteProfile (BF33), which uses a different ASN.1 structure where the CHOICE is not wrapped in a SEQUENCE.

### BPP Segmentation

The Bound Profile Package (~48KB) must be segmented into separate STORE DATA sessions per lpac reference:

| Segment | Content | Typical Size |
|---------|---------|-------------|
| 1 | BF36 header + BF23 (InitialiseSecureChannel) | ~191 bytes |
| 2 | A0 (ConfigureISDP) | ~76 bytes |
| 3 | A1 header | 3 bytes |
| 4 | A1 child: StoreMetadata (tag 88) | ~247 bytes |
| 5 | A2 (ReplaceSessionKeys) | ~76 bytes |
| 6 | A3 header | 4 bytes |
| 7-54 | A3 children: profile elements (tag 86) | ~1004 bytes each |

Each segment uses its own P2 counter. Intermediate blocks use P1=0x11, final block P1=0x91.

### Transport Modes

- **`real`** — AT+CSIM over serial. Opens a logical channel, selects ISD-R, channel-encodes all APDUs. This is the primary and fully working mode.
- **`mock`** — Software simulation with synthesized responses. For development without hardware.
- **`qmi`** — QMI UIM via `/dev/cdc-wdm0`. Opens logical channel but `SEND_APDU` fails on this firmware. Not usable for profile operations.

## eSIM Ecosystem

```
eUICC card vendor    →  Physical eUICC card (removable eSIM)
                              ↕ SIM slot
eSIM retailer        →  Activation code (SM-DP+ address + MatchingID)
                              ↓
SM-DP+ server        →  Profile download via SGP.22 ES9+
                              ↓
MNO / MVNO           →  IMSI/Ki credentials inside the profile
                              ↓
Radio network        →  LTE/5G access (direct or via roaming)
```

## Requirements

- Python 3.10+
- `pyserial`, `PyYAML`, `requests`
- Linux (serial port access)
- `qmicli` (optional, for cellular data setup)
- `socat` (optional, for quick AT commands)

## License

MIT
