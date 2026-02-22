#!/usr/bin/env python3
"""
Process pending eUICC notifications and retry EnableProfile.

Per SGP.22, pending notifications may block profile management.
This script:
1. Lists pending notifications
2. Retrieves each notification's full data
3. Sends to SM-DP+ via handleNotification
4. Removes from eUICC
5. Retries EnableProfile
"""
import serial
import time
import re
import sys
import json
import base64
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB3"
SMDP_ADDRESS = "smdp.example.com"  # replace with your SM-DP+ address

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


def tlv(tag_hex, value_hex=""):
    val = bytes.fromhex(value_hex) if value_hex else b""
    tag = bytes.fromhex(tag_hex)
    n = len(val)
    if n < 0x80:
        length = bytes([n])
    elif n < 0x100:
        length = bytes([0x81, n])
    else:
        length = bytes([0x82, n >> 8, n & 0xFF])
    return (tag + length + val).hex().upper()


def store_data(cla_prp, data_hex, label=""):
    lc = format(len(bytes.fromhex(data_hex)), "02X")
    return csim(f"{cla_prp}E29100{lc}{data_hex}00", label)


def store_data_chunked(cla_prp, data_hex, label="", chunk_size=120):
    """Multi-block STORE DATA for larger payloads."""
    data = bytes.fromhex(data_hex)
    chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
    resp_data = ""
    for idx, chunk in enumerate(chunks):
        last = (idx == len(chunks) - 1)
        p1 = "91" if last else "11"
        p2 = format(idx & 0xFF, "02X")
        chunk_hex = chunk.hex().upper()
        lc = format(len(chunk), "02X")
        if last:
            r = csim(f"{cla_prp}E2{p1}{p2}{lc}{chunk_hex}00",
                     f"{label} [{idx+1}/{len(chunks)}]")
        else:
            r = csim(f"{cla_prp}E2{p1}{p2}{lc}{chunk_hex}",
                     f"{label} [{idx+1}/{len(chunks)}]")
        if r and len(r) >= 4:
            sw = r[-4:]
            if sw != "9000" and not sw.startswith("61"):
                print(f"  ERROR: SW={sw}")
                return r
            resp_data = r
    return resp_data


at("AT", 1)

# Setup
print("=== Setup ===")
r = csim("0070000001", "MANAGE CHANNEL")
ch = int(r[:-4], 16) if r[-4:] == "9000" and len(r) > 4 else 0
cla_std = format(ch, "02X")
cla_prp = format(0x80 | ch, "02X")
print(f"Channel: {ch}")

r = csim(f"{cla_std}A4040010A0000005591010FFFFFFFF8900000100", "SELECT ISD-R")
if r[-4:].startswith("61"):
    csim_get(f"{cla_std}C00000{r[-2:]}")

# 1. List pending notifications
print("\n=== 1. ListNotification (BF28) ===")
r = store_data(cla_prp, "BF2800", "ListNotification")
if r and len(r) > 4:
    body = r[:-4]
    print(f"Notifications body ({len(body)//2} bytes)")

# 2. Retrieve full notification for seqNumber=0
print("\n=== 2a. RetrieveNotification seqNum=0 (BF2B) ===")
bf2b = tlv("BF2B", tlv("80", "00"))
r = store_data(cla_prp, bf2b, "RetrieveNotification seq=0")
notif0_data = b""
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, {len(body)//2} bytes")
    for i in range(0, len(body), 80):
        print(f"  {body[i:i+80]}")
    if sw == "9000" and body:
        notif0_data = bytes.fromhex(body)

# 3. Retrieve full notification for seqNumber=1
print("\n=== 2b. RetrieveNotification seqNum=1 (BF2B) ===")
bf2b = tlv("BF2B", tlv("80", "01"))
r = store_data(cla_prp, bf2b, "RetrieveNotification seq=1")
notif1_data = b""
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, {len(body)//2} bytes")
    for i in range(0, len(body), 80):
        print(f"  {body[i:i+80]}")
    if sw == "9000" and body:
        notif1_data = bytes.fromhex(body)

# 4. Send notifications to SM-DP+ via handleNotification
base_url = f"https://{SMDP_ADDRESS}/gsma/rsp2/es9plus"
headers = {
    "Content-Type": "application/json",
    "X-Admin-Protocol": "gsma/rsp/v2.5.0",
    "User-Agent": "lpac/python/1.0",
}

for seq, data in [(0, notif0_data), (1, notif1_data)]:
    if not data:
        continue
    print(f"\n=== 3. handleNotification seq={seq} → SM-DP+ ===")
    body = {
        "pendingNotification": base64.b64encode(data).decode(),
    }
    try:
        resp = requests.post(
            f"{base_url}/handleNotification",
            json=body,
            headers=headers,
            verify=False,
            timeout=30,
        )
        print(f"HTTP {resp.status_code}")
        print(f"Response: {resp.text[:200]}")
    except Exception as e:
        print(f"Error: {e}")

# 5. Remove notifications from eUICC
for seq in [0, 1]:
    print(f"\n=== 4. RemoveNotification seq={seq} (BF30) ===")
    bf30 = tlv("BF30", tlv("80", format(seq, "02X")))
    r = store_data(cla_prp, bf30, f"RemoveNotification seq={seq}")
    if r and len(r) > 4:
        sw = r[-4:]
        body = r[:-4]
        print(f"SW={sw}, body={body}")

# 6. Verify notifications cleared
print("\n=== 5. ListNotification after removal ===")
r = store_data(cla_prp, "BF2800", "ListNotification")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, {len(body)//2} bytes: {body}")

# 7. Retry EnableProfile
print("\n=== 6. EnableProfile retry ===")
iccid_hex = "985571200300355174F2"
bf31 = tlv("BF31", tlv("5A", iccid_hex) + tlv("81", "01"))
r = store_data(cla_prp, bf31, "EnableProfile")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, body={body}")
    # Parse result
    try:
        raw = bytes.fromhex(body)
        # Look for result tag 80
        pos = raw.find(b"\x80")
        if pos >= 0 and pos + 1 < len(raw):
            rlen = raw[pos + 1]
            if pos + 2 + rlen <= len(raw):
                result = raw[pos + 2]
                results = {0: "ok", 1: "iccidOrAidRequired",
                          2: "profileNotInDisabledState", 127: "undefinedError"}
                print(f"  enableResult: {result} ({results.get(result, 'unknown')})")
    except Exception:
        pass

# 8. Check profile state
print("\n=== 7. Final profile state ===")
r = store_data(cla_prp, "BF2D00", "BF2D")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    try:
        raw = bytes.fromhex(body)
        idx = 0
        while idx < len(raw):
            pos = raw.find(b"\x9F\x70", idx)
            if pos < 0:
                break
            if pos + 3 <= len(raw):
                sv = raw[pos + 3]
                states = {0: "disabled", 1: "enabled"}
                print(f"  profileState: {sv} ({states.get(sv, 'unknown')})")
            idx = pos + 1
    except Exception:
        pass

# Close
print("\n=== Close ===")
csim(f"007080{cla_std}{cla_std}", "CLOSE CHANNEL")
ser.close()
print("\nDone")
