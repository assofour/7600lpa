#!/usr/bin/env python3
"""
EnableProfile diagnostic — tests CORRECT A0-wrapped encoding.

ROOT CAUSE FOUND: SGP.22 uses AUTOMATIC TAGS. For EnableProfileRequest,
profileIdentifier is a CHOICE inside a SEQUENCE, which gets EXPLICIT [0]
tagging → tag A0 wrapper is REQUIRED around the 5A (ICCID).

WRONG (what we were sending):
  BF31 { 5A 0A {iccid} 81 01 01 }

CORRECT (A0 wrapper for EXPLICIT [0] on CHOICE):
  BF31 { A0 { 5A 0A {iccid} } 81 01 FF }

This explains why DeleteProfile (BF33) worked — it's a direct CHOICE,
not a SEQUENCE containing a CHOICE, so no A0 wrapper needed.

Usage:
    sudo python3 enable_debug.py [PORT]
    sudo python3 enable_debug.py /dev/ttyUSB2
"""
import serial
import time
import re
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB2"
BAUD = 115200
TIMEOUT = 5

ICCID_HEX = "985571200300355174F2"
ISDR_AID = "A0000005591010FFFFFFFF8900000100"

print(f"=== EnableProfile Diagnostic (A0-wrapper fix) ===")
print(f"Port: {PORT}, Baud: {BAUD}")
print()

ser = serial.Serial(PORT, BAUD, timeout=TIMEOUT)
time.sleep(0.5)
ser.reset_input_buffer()

step = 0


def at_raw(cmd, wait=2):
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    time.sleep(wait)
    resp = ser.read(ser.in_waiting).decode(errors="replace")
    return resp


def at_csim(apdu_hex, label=""):
    global step
    step += 1
    cmd = f'AT+CSIM={len(apdu_hex)},"{apdu_hex}"'
    print(f"--- Step {step}: {label} ---")
    print(f"  TX: {cmd}")

    apdu_bytes = bytes.fromhex(apdu_hex)
    if len(apdu_bytes) >= 4:
        cla, ins, p1, p2 = apdu_bytes[:4]
        print(f"  CLA={cla:02X} INS={ins:02X} P1={p1:02X} P2={p2:02X}", end="")
        if len(apdu_bytes) > 5:
            lc = apdu_bytes[4]
            body = apdu_bytes[5:5 + lc]
            print(f" Lc={lc:02X} Data={body.hex().upper()}", end="")
            if len(apdu_bytes) > 5 + lc:
                print(f" Le={apdu_bytes[5+lc]:02X}", end="")
        print()

    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    time.sleep(3)
    raw_resp = ser.read(ser.in_waiting).decode(errors="replace")

    for line in raw_resp.strip().split("\n"):
        line = line.strip()
        if line:
            print(f"    {line}")

    m = re.search(r'\+CSIM:\s*(\d+),"([^"]*)"', raw_resp)
    if not m:
        print(f"  ERROR: No +CSIM response!")
        print()
        return "", "", raw_resp

    resp_hex = m.group(2)
    sw = resp_hex[-4:] if len(resp_hex) >= 4 else ""
    data = resp_hex[:-4] if len(resp_hex) > 4 else ""
    print(f"  SW={sw} Data={data if data else '(none)'}")
    print()
    return data, sw, raw_resp


def get_response_chain(cla_hex, data, sw):
    all_data = data
    while sw[:2].upper() == "61":
        le = sw[2:4]
        d, sw, _ = at_csim(f"{cla_hex}C00000{le}", "GET RESPONSE")
        all_data += d
    return all_data, sw


def parse_enable_response(data_hex):
    if not data_hex:
        print("  [No response data]")
        return -1
    try:
        raw = bytes.fromhex(data_hex)
        if raw[0:2] != b"\xBF\x31":
            print(f"  Unexpected outer tag: {raw[0:2].hex().upper()}")
            return -1
        pos = 2
        if raw[pos] < 0x80:
            olen = raw[pos]; pos += 1
        elif raw[pos] == 0x81:
            olen = raw[pos + 1]; pos += 2
        else:
            olen = (raw[pos + 1] << 8) | raw[pos + 2]; pos += 3
        inner = raw[pos:pos + olen]
        if len(inner) >= 3 and inner[0:2] == b"\x80\x01":
            result = inner[2]
            names = {0: "ok", 1: "iccidOrAidRequired", 2: "profileNotInEnabledState",
                     3: "disallowedByPolicy", 5: "wrongProfileReenabling",
                     6: "catBusy", 127: "undefinedError"}
            print(f"  >>> enableResult = {result} ({names.get(result, 'UNKNOWN')})")
            return result
        else:
            print(f"  Inner: {inner.hex().upper()}")
            return -1
    except Exception as e:
        print(f"  Parse error: {e}")
        return -1


def tlv(tag_hex, val_hex):
    val = bytes.fromhex(val_hex) if val_hex else b""
    tag = bytes.fromhex(tag_hex)
    n = len(val)
    if n < 0x80:
        length = bytes([n])
    elif n < 0x100:
        length = bytes([0x81, n])
    else:
        length = bytes([0x82, n >> 8, n & 0xFF])
    return (tag + length + val).hex().upper()


# ========================================================================
# MAIN
# ========================================================================

# 0. AT check
print("=== 0. AT Check ===")
r = at_raw("AT", 1)
if "OK" not in r:
    print("FATAL: Modem not responding. Power cycle needed?")
    ser.close()
    sys.exit(1)
print(f"  OK")
print()

# 1. Open logical channel
print("=== 1. MANAGE CHANNEL (Open) ===")
data, sw, _ = at_csim("0070000001", "MANAGE CHANNEL OPEN")
if sw == "9000" and data:
    ch = int(data, 16)
else:
    print("FATAL: Cannot open logical channel")
    ser.close()
    sys.exit(1)

if ch <= 3:
    cla_std = f"{ch:02X}"
    cla_prp = f"{0x80 | ch:02X}"
else:
    cla_std = f"{0x40 | (ch - 4):02X}"
    cla_prp = f"{0xC0 | (ch - 4):02X}"
print(f"  Channel={ch} CLA_std=0x{cla_std} CLA_prp=0x{cla_prp}")
print()

# 2. SELECT ISD-R
print("=== 2. SELECT ISD-R ===")
lc = f"{len(bytes.fromhex(ISDR_AID)):02X}"
data, sw, _ = at_csim(f"{cla_std}A40400{lc}{ISDR_AID}", "SELECT ISD-R")
if sw[:2] == "61":
    data, sw = get_response_chain(cla_std, data, sw)
print(f"  SELECT result: SW={sw}")
print()

# 3. ListProfiles
print("=== 3. ListProfiles (BF2D) ===")
payload = "BF2D075C055A9F704F"
lc_val = len(bytes.fromhex(payload))
apdu = f"{cla_prp}E29100{lc_val:02X}{payload}00"
data, sw, _ = at_csim(apdu, "STORE DATA: ListProfiles")
if sw[:2] == "61":
    data, sw = get_response_chain(cla_std, data, sw)
if data:
    print(f"  Profiles ({len(data) // 2} bytes): {data[:200]}")
    # Quick ICCID/state extraction
    try:
        raw = bytes.fromhex(data)
        pos = 0
        while pos < len(raw):
            idx = raw.find(b"\x5A", pos)
            if idx < 0:
                break
            ilen = raw[idx + 1]
            iccid = raw[idx + 2:idx + 2 + ilen].hex().upper()
            # Find 9F70 near this ICCID
            sidx = raw.find(b"\x9F\x70", idx)
            state = raw[sidx + 3] if sidx >= 0 and sidx < idx + 40 else -1
            sname = {0: "disabled", 1: "enabled"}.get(state, f"{state}")
            print(f"  >>> ICCID={iccid} state={sname}")
            pos = idx + 2 + ilen
    except Exception:
        pass

has_profile = bool(data and "5A" in data.upper())
if not has_profile:
    print("  WARNING: No profiles found! Need to download first.")
print()

# ========================================================================
# 4. CORRECT EnableProfile — A0-wrapped (THE FIX)
# ========================================================================
print("=" * 60)
print("=== 4. EnableProfile CORRECT encoding (A0 wrapper) ===")
print("=" * 60)
print()
print("  SGP.22 ASN.1 EnableProfileRequest:")
print("    BF31 SEQUENCE {")
print("      A0 [0] EXPLICIT CHOICE {  <-- THIS WAS MISSING!")
print("        5A ICCID")
print("      }")
print("      81 [1] IMPLICIT BOOLEAN refreshFlag")
print("    }")
print()

# Build: BF31 { A0 { 5A 0A {iccid} } 81 01 FF }
iccid_tlv = tlv("5A", ICCID_HEX)                    # 5A 0A {10 bytes}
a0_wrapped = tlv("A0", iccid_tlv)                    # A0 0C {5A 0A ...}
refresh_true = "8101FF"                               # [1] BOOLEAN TRUE (DER)
bf31_correct = tlv("BF31", a0_wrapped + refresh_true) # BF31 { A0 {5A} 81 FF }

print(f"  CORRECT BF31: {bf31_correct}")
print(f"  Breakdown: BF31 {{ A0 {{ {iccid_tlv} }} {refresh_true} }}")

lc_val = len(bytes.fromhex(bf31_correct))
apdu = f"{cla_prp}E29100{lc_val:02X}{bf31_correct}00"
print(f"  Full APDU: {apdu}")
print()

data, sw, _ = at_csim(apdu, "STORE DATA: EnableProfile (A0-wrapped, refresh=TRUE)")
if sw[:2] == "61":
    data, sw = get_response_chain(cla_std, data, sw)

result = parse_enable_response(data)
print()

# ========================================================================
# 5. If still fails, try A0-wrapped WITHOUT refreshFlag
# ========================================================================
if result != 0:
    print("=== 5. EnableProfile A0-wrapped, NO refreshFlag ===")
    bf31_norefresh = tlv("BF31", a0_wrapped)
    lc_val = len(bytes.fromhex(bf31_norefresh))
    apdu = f"{cla_prp}E29100{lc_val:02X}{bf31_norefresh}00"
    print(f"  BF31: {bf31_norefresh}")

    data, sw, _ = at_csim(apdu, "STORE DATA: EnableProfile (A0, no refresh)")
    if sw[:2] == "61":
        data, sw = get_response_chain(cla_std, data, sw)
    result = parse_enable_response(data)
    print()

# ========================================================================
# 6. If still fails, try A0-wrapped with refresh=FALSE
# ========================================================================
if result != 0:
    print("=== 6. EnableProfile A0-wrapped, refreshFlag=FALSE ===")
    refresh_false = "810100"
    bf31_rfalse = tlv("BF31", a0_wrapped + refresh_false)
    lc_val = len(bytes.fromhex(bf31_rfalse))
    apdu = f"{cla_prp}E29100{lc_val:02X}{bf31_rfalse}00"
    print(f"  BF31: {bf31_rfalse}")

    data, sw, _ = at_csim(apdu, "STORE DATA: EnableProfile (A0, refresh=FALSE)")
    if sw[:2] == "61":
        data, sw = get_response_chain(cla_std, data, sw)
    result = parse_enable_response(data)
    print()

# ========================================================================
# 7. Try A0-wrapped with ISD-P AID instead of ICCID
# ========================================================================
if result != 0:
    print("=== 7. EnableProfile A0-wrapped, by ISD-P AID ===")
    isdp_aid = "A0000005591010FFFFFFFF8900001100"
    aid_tlv = tlv("4F", isdp_aid)
    a0_aid = tlv("A0", aid_tlv)
    bf31_aid = tlv("BF31", a0_aid + refresh_true)
    lc_val = len(bytes.fromhex(bf31_aid))
    apdu = f"{cla_prp}E29100{lc_val:02X}{bf31_aid}00"
    print(f"  BF31: {bf31_aid}")

    data, sw, _ = at_csim(apdu, "STORE DATA: EnableProfile (A0+AID)")
    if sw[:2] == "61":
        data, sw = get_response_chain(cla_std, data, sw)
    result = parse_enable_response(data)
    print()

# ========================================================================
# 8. Try OLD encoding (without A0) for comparison
# ========================================================================
if result != 0:
    print("=== 8. EnableProfile OLD encoding (NO A0) — for comparison ===")
    bf31_old = tlv("BF31", tlv("5A", ICCID_HEX) + "810101")
    lc_val = len(bytes.fromhex(bf31_old))
    apdu = f"{cla_prp}E29100{lc_val:02X}{bf31_old}00"
    print(f"  OLD BF31: {bf31_old}")

    data, sw, _ = at_csim(apdu, "STORE DATA: EnableProfile (OLD, no A0)")
    if sw[:2] == "61":
        data, sw = get_response_chain(cla_std, data, sw)
    parse_enable_response(data)
    print()

# ========================================================================
# 9. Try STORE DATA without Le byte (case-3 APDU)
# ========================================================================
if result != 0:
    print("=== 9. EnableProfile A0-wrapped, case-3 APDU (no Le) ===")
    lc_val = len(bytes.fromhex(bf31_correct))
    apdu_no_le = f"{cla_prp}E29100{lc_val:02X}{bf31_correct}"
    print(f"  APDU (no Le): {apdu_no_le}")

    data, sw, _ = at_csim(apdu_no_le, "STORE DATA: EnableProfile (no Le)")
    if sw[:2] == "61":
        data, sw = get_response_chain(cla_std, data, sw)
    result = parse_enable_response(data)
    print()

# ========================================================================
# 10. Try on basic channel (ch0, CLA=80)
# ========================================================================
if result != 0:
    print("=== 10. EnableProfile A0-wrapped on ch0 (CLA=80) ===")
    lc_val = len(bytes.fromhex(bf31_correct))
    apdu_ch0 = f"80E29100{lc_val:02X}{bf31_correct}00"

    data, sw, _ = at_csim(apdu_ch0, "STORE DATA ch0: EnableProfile")
    if sw[:2] == "61":
        data, sw = get_response_chain("00", data, sw)
    result = parse_enable_response(data)
    print()

# ========================================================================
# 11. Also try DisableProfile with A0 wrapper (should also be fixed)
# ========================================================================
if result != 0:
    print("=== 11. DisableProfile (BF32) with A0 wrapper ===")
    bf32 = tlv("BF32", a0_wrapped + refresh_true)
    lc_val = len(bytes.fromhex(bf32))
    apdu = f"{cla_prp}E29100{lc_val:02X}{bf32}00"
    print(f"  BF32: {bf32}")

    data, sw, _ = at_csim(apdu, "STORE DATA: DisableProfile (A0-wrapped)")
    if sw[:2] == "61":
        data, sw = get_response_chain(cla_std, data, sw)
    if data:
        print(f"  Response: {data}")
    print()

# ========================================================================
# 12. Final profile state check
# ========================================================================
print("=== 12. Final ListProfiles ===")
payload = "BF2D075C055A9F704F"
lc_val = len(bytes.fromhex(payload))
apdu = f"{cla_prp}E29100{lc_val:02X}{payload}00"
data, sw, _ = at_csim(apdu, "STORE DATA: ListProfiles (final)")
if sw[:2] == "61":
    data, sw = get_response_chain(cla_std, data, sw)
if data:
    try:
        raw = bytes.fromhex(data)
        pos = 0
        while pos < len(raw):
            idx = raw.find(b"\x5A", pos)
            if idx < 0:
                break
            ilen = raw[idx + 1]
            iccid = raw[idx + 2:idx + 2 + ilen].hex().upper()
            sidx = raw.find(b"\x9F\x70", idx)
            state = raw[sidx + 3] if sidx >= 0 and sidx < idx + 40 else -1
            sname = {0: "disabled", 1: "enabled"}.get(state, f"{state}")
            print(f"  >>> ICCID={iccid} state={sname}")
            pos = idx + 2 + ilen
    except Exception:
        pass
print()

# Close
print("=== Close ===")
at_csim(f"00708000{ch:02X}", "CLOSE CHANNEL")
ser.close()

print("\n=== DONE ===")
if result == 0:
    print("SUCCESS! EnableProfile worked!")
    print("Run: AT+CFUN=1,1 to reboot modem and activate the new profile.")
else:
    print("EnableProfile still failing. Check output above for clues.")
