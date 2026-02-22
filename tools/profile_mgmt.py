#!/usr/bin/env python3
"""Test various profile management operations on the eUICC."""
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

ICCID = "985571200300355174F2"
ISDP_AID = "A0000005591010FFFFFFFF8900001100"

# 1. SetNickname (BF29) — test basic profile metadata modification
print("\n=== 1. SetNickname (BF29) ===")
nickname = "546573744573696D"  # "TestEsim" in hex
bf29 = tlv("BF29", tlv("5A", ICCID) + tlv("90", nickname))
r = store_data(cla_prp, bf29, "SetNickname")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"  SW={sw}, body={body}")
    # BF29 { 80 01 XX } where XX: 0=ok, 127=undefinedError
    try:
        raw = bytes.fromhex(body)
        pos = raw.find(b"\x80")
        if pos >= 0 and pos + 2 <= len(raw):
            print(f"  result: {raw[pos+2]} ({'ok' if raw[pos+2]==0 else 'undefinedError' if raw[pos+2]==127 else 'unknown'})")
    except Exception:
        pass

# 2. DeleteProfile (BF33) — try to delete and re-download
print("\n=== 2. DeleteProfile (BF33) ===")
bf33 = tlv("BF33", tlv("5A", ICCID))
r = store_data(cla_prp, bf33, "DeleteProfile")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"  SW={sw}, body={body}")
    try:
        raw = bytes.fromhex(body)
        pos = raw.find(b"\x80")
        if pos >= 0 and pos + 2 <= len(raw):
            result = raw[pos+2]
            results = {0: "ok", 1: "iccidOrAidRequired",
                      2: "profileNotInDisabledState", 127: "undefinedError"}
            print(f"  deleteResult: {result} ({results.get(result, 'unknown')})")
    except Exception:
        pass

# 3. Check if profile was deleted
print("\n=== 3. Verify profiles after delete ===")
r = store_data(cla_prp, "BF2D00", "BF2D")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    print(f"  SW={sw}, {len(body)//2} bytes")
    for i in range(0, len(body), 80):
        print(f"  {body[i:i+80]}")
    if len(body) <= 10:
        print("  (empty — profile deleted)")
    else:
        try:
            raw = bytes.fromhex(body)
            pos = raw.find(b"\x9F\x70")
            if pos >= 0 and pos + 3 <= len(raw):
                sv = raw[pos + 3]
                print(f"  profileState: {sv} ({'disabled' if sv==0 else 'enabled' if sv==1 else 'unknown'})")
        except Exception:
            pass

# 4. Try SELECT ISD-P directly and check status
print("\n=== 4. SELECT ISD-P directly ===")
r = csim(f"{cla_std}A4040010{ISDP_AID}", "SELECT ISD-P")
if r and len(r) >= 4:
    sw = r[-4:]
    print(f"  SW={sw}")
    if sw.startswith("61"):
        csim_get(f"{cla_std}C00000{sw[2:]}")

# 5. Try GetEUICCInfo2 for memory info after all operations
print("\n=== 5. Memory check (extCardResource) ===")
# Re-select ISD-R first
csim(f"{cla_std}A4040010A0000005591010FFFFFFFF8900000100", "Re-SELECT ISD-R")
r = store_data(cla_prp, "BF2200", "eUICCInfo2")
if r and len(r) > 4:
    sw = r[-4:]
    body = r[:-4]
    try:
        raw = bytes.fromhex(body)
        # Find extCardResource (tag 84)
        pos = raw.find(b"\x84")
        if pos >= 0:
            elen = raw[pos + 1]
            edata = raw[pos + 2: pos + 2 + elen]
            print(f"  extCardResource: {edata.hex().upper()}")
            # Parse sub-TLVs
            i = 0
            while i < len(edata):
                t = edata[i]
                l = edata[i + 1]
                v = edata[i + 2: i + 2 + l]
                if t == 0x81:
                    print(f"    installedApps: {int.from_bytes(v, 'big')}")
                elif t == 0x82:
                    nvm = int.from_bytes(v, 'big')
                    print(f"    freeNVM: {nvm} bytes ({nvm/1024:.0f} KB)")
                elif t == 0x83:
                    ram = int.from_bytes(v, 'big')
                    print(f"    freeRAM: {ram} bytes ({ram/1024:.0f} KB)")
                i += 2 + l
    except Exception as e:
        print(f"  Parse error: {e}")

# Close
print("\n=== Close ===")
csim(f"007080{cla_std}{cla_std}", "CLOSE CHANNEL")
ser.close()
print("\nDone")
