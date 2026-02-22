#!/usr/bin/env python3
"""Deep probe: eUICC capabilities and EnableProfile troubleshooting."""
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
    """Send STORE DATA (E2) with proper encoding."""
    lc = format(len(bytes.fromhex(data_hex)), "02X")
    return csim(f"{cla_prp}E29100{lc}{data_hex}00", label)


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

# 1. eUICCInfo2 (BF22) — check capabilities
print("\n=== eUICCInfo2 (BF22) ===")
r = store_data(cla_prp, "BF2200", "eUICCInfo2")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, {len(body)//2} bytes")
    for i in range(0, len(body), 80):
        print(f"  {body[i:i+80]}")

# 2. GetRAT - Rules Authorisation Table (BF43)
print("\n=== GetRAT (BF43) ===")
r = store_data(cla_prp, "BF4300", "GetRAT")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, {len(body)//2} bytes")
    for i in range(0, len(body), 80):
        print(f"  {body[i:i+80]}")

# 3. ListNotification (BF28) — check pending notifications
print("\n=== ListNotification (BF28) ===")
r = store_data(cla_prp, "BF2800", "ListNotification")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, {len(body)//2} bytes")
    for i in range(0, len(body), 80):
        print(f"  {body[i:i+80]}")

# 4. GetProfilesInfo with tag list requesting everything
print("\n=== GetProfilesInfo (BF2D) full ===")
# Request with tag list: 5A (ICCID), 91 (name), 92 (spn), 93 (icon),
# 94 (iconType), 95 (class), 9F70 (state), E3 (all)
# Actually try with A0 { } to get all profiles with all fields
r = store_data(cla_prp, tlv("BF2D", tlv("A0", "")), "BF2D with A0")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, {len(body)//2} bytes")
    for i in range(0, len(body), 80):
        print(f"  {body[i:i+80]}")

# 5. Try DisableProfile first (BF32) — maybe we need to disable the "bootstrap" profile
# Try with the test ICCID that modem shows
print("\n=== DisableProfile (BF32) for test profile ===")
# Convert test ICCID 89000123456789012341 to BCD
# BCD swap: 89 00 01 23 45 67 89 01 23 41
#         -> 98 00 10 32 54 76 98 10 32 14
test_iccid_bcd = "98001032547698103214"
bf32 = tlv("BF32", tlv("5A", test_iccid_bcd))
r = store_data(cla_prp, bf32, "DisableProfile test ICCID")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, body={body}")

# 6. Try EnableProfile again after disable attempt
print("\n=== EnableProfile (BF31) after disable ===")
iccid_hex = "985571200300355174F2"
bf31 = tlv("BF31", tlv("5A", iccid_hex) + tlv("81", "01"))
r = store_data(cla_prp, bf31, "EnableProfile after disable")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, body={body}")

# 7. Verify final state
print("\n=== Final profile state ===")
r = store_data(cla_prp, "BF2D00", "BF2D")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"SW={sw}, {len(body)//2} bytes")
    try:
        raw_bytes = bytes.fromhex(body)
        idx = 0
        while idx < len(raw_bytes):
            pos = raw_bytes.find(b"\x9F\x70", idx)
            if pos < 0:
                break
            if pos + 2 < len(raw_bytes):
                tag_len = raw_bytes[pos + 2]
                if tag_len < 0x80 and pos + 3 + tag_len <= len(raw_bytes):
                    sv = raw_bytes[pos + 3]
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
