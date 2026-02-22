#!/usr/bin/env python3
"""Query eUICC profile list via STORE DATA (BF2D)."""
import serial
import time
import re
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB3"

ser = serial.Serial(PORT, 115200, timeout=5)
time.sleep(0.5)
ser.reset_input_buffer()


def at(cmd, wait=3):
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    time.sleep(wait)
    return ser.read(ser.in_waiting).decode(errors="replace").strip()


def csim(apdu_hex, label=""):
    resp = at(f'AT+CSIM={len(apdu_hex)},"{apdu_hex}"', 3)
    m = re.search(r'\+CSIM:\s*\d+,"([^"]+)"', resp)
    raw = m.group(1) if m else ""
    print(f"  >> {apdu_hex} [{label}]")
    print(f"  << {raw}")
    # Handle 61xx
    if raw and len(raw) >= 4 and raw[-4:-2] == "61":
        le = raw[-2:]
        gr = csim_get(f"{apdu_hex[:2]}C00000{le}")
        return raw[:-4] + gr
    return raw


def csim_get(apdu_hex):
    resp = at(f'AT+CSIM={len(apdu_hex)},"{apdu_hex}"', 3)
    m = re.search(r'\+CSIM:\s*\d+,"([^"]+)"', resp)
    raw = m.group(1) if m else ""
    print(f"  >> {apdu_hex} [GET RESP]")
    print(f"  << {raw}")
    if raw and len(raw) >= 4 and raw[-4:-2] == "61":
        le = raw[-2:]
        more = csim_get(f"{apdu_hex[:2]}C00000{le}")
        return raw[:-4] + more
    return raw


at("AT", 1)

# Open logical channel + SELECT ISD-R
print("=== Setup ===")
r = csim("0070000001", "MANAGE CHANNEL")
ch = int(r[:-4], 16) if r[-4:] == "9000" and len(r) > 4 else 0
cla_std = format(ch, "02X")
cla_prp = format(0x80 | ch, "02X")
print(f"Channel: {ch}, CLA std={cla_std}, prp={cla_prp}")

r = csim(f"{cla_std}A4040010A0000005591010FFFFFFFF8900000100", "SELECT ISD-R")
if r[-4:].startswith("61"):
    le = r[-2:]
    csim_get(f"{cla_std}C00000{le}")

# GetProfilesInfo (BF2D) via STORE DATA
print("\n=== GetProfilesInfo (BF2D) via STORE DATA ===")
data = "BF2D00"  # empty = all profiles
lc = format(len(bytes.fromhex(data)), "02X")
r = csim(f"{cla_prp}E29100{lc}{data}00", "BF2D empty")

if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, body_len={len(body)//2} bytes")
    for i in range(0, len(body), 80):
        print(f"  {body[i:i+80]}")

    # Try to parse profiles
    try:
        raw_bytes = bytes.fromhex(body)
        # Look for ICCID tags (5A)
        idx = 0
        while idx < len(raw_bytes):
            pos = raw_bytes.find(b"\x5A", idx)
            if pos < 0:
                break
            if pos + 1 < len(raw_bytes):
                tag_len = raw_bytes[pos + 1]
                if tag_len < 0x80 and pos + 2 + tag_len <= len(raw_bytes):
                    iccid_bytes = raw_bytes[pos + 2: pos + 2 + tag_len]
                    iccid = iccid_bytes.hex().upper()
                    print(f"  ICCID (5A): {iccid}")
            idx = pos + 1

        # Look for profileState (9F70)
        idx = 0
        while idx < len(raw_bytes):
            pos = raw_bytes.find(b"\x9F\x70", idx)
            if pos < 0:
                break
            if pos + 2 < len(raw_bytes):
                tag_len = raw_bytes[pos + 2]
                if tag_len < 0x80 and pos + 3 + tag_len <= len(raw_bytes):
                    state_val = raw_bytes[pos + 3: pos + 3 + tag_len]
                    sv = state_val[0] if state_val else 0xFF
                    states = {0: "disabled", 1: "enabled"}
                    print(f"  profileState (9F70): {sv} ({states.get(sv, 'unknown')})")
            idx = pos + 1

        # Look for profileNickname (90 or profile name)
        idx = 0
        while idx < len(raw_bytes):
            pos = raw_bytes.find(b"\x92", idx)
            if pos < 0:
                break
            if pos + 1 < len(raw_bytes):
                tag_len = raw_bytes[pos + 1]
                if 0 < tag_len < 0x80 and pos + 2 + tag_len <= len(raw_bytes):
                    name_bytes = raw_bytes[pos + 2: pos + 2 + tag_len]
                    try:
                        print(f"  serviceProviderName (92): {name_bytes.decode('utf-8')}")
                    except Exception:
                        print(f"  serviceProviderName (92): {name_bytes.hex().upper()}")
            idx = pos + 1

        # Look for profileClass (95)
        idx = 0
        while idx < len(raw_bytes):
            pos = raw_bytes.find(b"\x95", idx)
            if pos < 0:
                break
            if pos + 1 < len(raw_bytes):
                tag_len = raw_bytes[pos + 1]
                if tag_len < 0x80 and pos + 3 + tag_len <= len(raw_bytes):
                    pc = raw_bytes[pos + 2]
                    classes = {0: "test", 1: "provisioning", 2: "operational"}
                    print(f"  profileClass (95): {pc} ({classes.get(pc, 'unknown')})")
            idx = pos + 1

    except Exception as e:
        print(f"  Parse error: {e}")

# Also GetEID
print("\n=== GetEID (BF3E) ===")
data = "BF3E035C015A"
lc = format(len(bytes.fromhex(data)), "02X")
r = csim(f"{cla_prp}E29100{lc}{data}00", "GET EID")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, body_len={len(body)//2} bytes")
    # Try to extract EID from 5A tag
    try:
        raw_bytes = bytes.fromhex(body)
        pos = raw_bytes.find(b"\x5A")
        if pos >= 0 and pos + 1 < len(raw_bytes):
            eid_len = raw_bytes[pos + 1]
            eid = raw_bytes[pos + 2: pos + 2 + eid_len].hex().upper()
            print(f"  EID: {eid}")
    except Exception as e:
        print(f"  Parse error: {e}")

# Close channel
print("\n=== Close ===")
csim(f"007080{cla_std}{cla_std}", "CLOSE CHANNEL")
ser.close()
print("\nDone")
