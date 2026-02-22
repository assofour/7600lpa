#!/usr/bin/env python3
"""Delete residual profile by ICCID, then re-run download."""
import serial, time, re, subprocess, sys

PORT = "/dev/ttyUSB3"
ICCID_HEX = "985571200300355174F2"  # The ICCID that already exists

ser = serial.Serial(PORT, 115200, timeout=5)
time.sleep(0.5)
ser.reset_input_buffer()

def at(cmd, wait=2):
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode())
    time.sleep(wait)
    return ser.read(ser.in_waiting).decode(errors="replace").strip()

def parse(r):
    m = re.search(r'\+CSIM:\s*\d+,"([^"]+)"', r)
    return m.group(1) if m else ""

def csim(apdu_hex, label=""):
    r = at(f'AT+CSIM={len(apdu_hex)},"{apdu_hex}"')
    raw = parse(r)
    sw = raw[-4:] if len(raw) >= 4 else ""
    data = raw[:-4] if len(raw) > 4 else ""
    # Handle 61xx chain
    all_data = data
    while sw.startswith("61"):
        le = sw[2:]
        r2 = at(f'AT+CSIM=10,"{cla_std}C00000{le}"')
        raw2 = parse(r2)
        sw = raw2[-4:] if len(raw2) >= 4 else ""
        all_data += raw2[:-4] if len(raw2) > 4 else ""
    print(f"  [{label}] SW={sw} data={all_data[:80]}{'...' if len(all_data)>80 else ''}")
    return all_data, sw

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

def store_data(data_hex, label=""):
    lc = format(len(bytes.fromhex(data_hex)), "02X")
    return csim(f"{cla_prp}E29100{lc}{data_hex}00", label)

# Setup
at("AT", 1)
r = at('AT+CSIM=10,"0070000001"')
raw = parse(r)
ch = int(raw[:2], 16) if raw else 0
print(f"Channel: {ch}")

if ch <= 3:
    cla_std = f"{ch:02X}"
    cla_prp = f"{0x80 | ch:02X}"
else:
    cla_std = f"{0x40 | (ch - 4):02X}"
    cla_prp = f"{0xC0 | (ch - 4):02X}"

# SELECT ISD-R
aid = "A0000005591010FFFFFFFF8900000100"
csim(f"{cla_std}A4040010{aid}", "SELECT ISD-R")

# 1. Try DisableProfile first (BF32) - needed before delete
print("\n=== Step 1: DisableProfile (BF32) by ICCID ===")
iccid_len = len(bytes.fromhex(ICCID_HEX))
inner = f"5A{iccid_len:02X}{ICCID_HEX}"
inner_len = len(bytes.fromhex(inner))
bf32 = f"BF32{inner_len:02X}{inner}"
data, sw = store_data(bf32, "DisableProfile")

# 2. DeleteProfile (BF33) by ICCID
print("\n=== Step 2: DeleteProfile (BF33) by ICCID ===")
bf33 = f"BF33{inner_len:02X}{inner}"
data, sw = store_data(bf33, "DeleteProfile")
if data:
    print(f"  Response: {data}")
    # Parse result (80 01 XX)
    try:
        raw_bytes = bytes.fromhex(data)
        pos = raw_bytes.find(b"\x80\x01")
        if pos >= 0:
            result = raw_bytes[pos + 2]
            results = {0: "ok", 1: "iccidOrAidRequired",
                      2: "profileNotInDisabledState",
                      3: "disallowedByPolicy",
                      127: "undefinedError"}
            print(f"  deleteResult: {result} ({results.get(result, 'unknown')})")
    except Exception as e:
        print(f"  Parse error: {e}")

# 3. Also try delete by ISD-P AID (in case the profile has a different reference)
print("\n=== Step 3: DeleteProfile (BF33) with empty ICCID (last installed) ===")
bf33_empty = tlv("BF33", tlv("5A", ""))
data, sw = store_data(bf33_empty, "DeleteProfile empty")
if data:
    print(f"  Response: {data}")

# 4. ListProfiles to verify
print("\n=== Step 4: ListProfiles ===")
data, sw = store_data(tlv("BF2D", "5C00"), "ListProfiles")

# 5. Close channel
at(f'AT+CSIM=10,"00708000{ch:02X}"')
ser.close()

print(f"\n=== Done. Now run: sudo python3 download.py --no-ssl-verify ===")
