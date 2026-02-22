#!/usr/bin/env python3
"""
probe_es10c_v2.py — Try various ES10c STORE DATA encodings for
GetEUICCChallenge and eUICCInfo on ISD-R.

STORE DATA (INS=E2) bypasses the SIM7600G-H APDU filter!
"""
import serial
import time
import re
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB3"

ser = serial.Serial(PORT, 115200, timeout=5)
time.sleep(0.5)
ser.reset_input_buffer()


def at(cmd, wait=5):
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    time.sleep(wait)
    return ser.read(ser.in_waiting).decode(errors="replace").strip()


def csim(apdu_hex, label=""):
    resp = at(f'AT+CSIM={len(apdu_hex)},"{apdu_hex}"', 3)
    m = re.search(r'\+CSIM:\s*\d+,"([^"]+)"', resp)
    raw = m.group(1) if m else ""
    tag = f" [{label}]" if label else ""
    print(f"  >> {apdu_hex}{tag}")
    print(f"  << {raw}")
    # Handle 61xx GET RESPONSE
    if raw and len(raw) >= 4 and raw[-4:-2] == "61":
        le = raw[-2:]
        raw2 = csim_raw(f"{apdu_hex[:2]}C00000{le}")
        return raw[:-4] + raw2  # concatenate data
    return raw


def csim_raw(apdu_hex):
    resp = at(f'AT+CSIM={len(apdu_hex)},"{apdu_hex}"', 3)
    m = re.search(r'\+CSIM:\s*\d+,"([^"]+)"', resp)
    raw = m.group(1) if m else ""
    print(f"  >> {apdu_hex} (GET RESP)")
    print(f"  << {raw}")
    return raw


def tlv(tag_hex, value_hex=""):
    """Build a TLV: tag + length + value (all hex strings)."""
    val_bytes = bytes.fromhex(value_hex) if value_hex else b""
    tag_bytes = bytes.fromhex(tag_hex)
    n = len(val_bytes)
    if n < 0x80:
        length = bytes([n])
    elif n < 0x100:
        length = bytes([0x81, n])
    else:
        length = bytes([0x82, n >> 8, n & 0xFF])
    return (tag_bytes + length + val_bytes).hex().upper()


at("AT", 1)

# === 1. Open logical channel + SELECT ISD-R ===
print("=== Setup ===")
r = csim("0070000001", "MANAGE CHANNEL")
ch = int(r[:-4], 16) if r[-4:] == "9000" and len(r) > 4 else 0
cla_std = format(ch, "02X")
cla_prp = format(0x80 | ch, "02X")

r = csim(f"{cla_std}A4040010A0000005591010FFFFFFFF8900000100", "SELECT ISD-R")
if r[-4:].startswith("61"):
    le = r[-2:]
    csim_raw(f"{cla_std}C00000{le}")

# === 2. Confirm EID works ===
print("\n=== Get EID (confirmed working) ===")
data = tlv("BF3E", tlv("5C", "5A"))
lc = format(len(bytes.fromhex(data)), "02X")
csim(f"{cla_prp}E29100{lc}{data}00", "GET EID")

# === 3. Try GetEUICCChallenge various encodings ===
print("\n=== GetEUICCChallenge attempts ===")

# Attempt 1: BF2E with empty body
data = "BF2E00"
lc = format(len(bytes.fromhex(data)), "02X")
csim(f"{cla_prp}E29100{lc}{data}00", "BF2E empty")

# Attempt 2: BF2E with 5C tag list (like Get EID)
data = tlv("BF2E", tlv("5C", "5C"))  # requesting tag 5C (challenge)
lc = format(len(bytes.fromhex(data)), "02X")
csim(f"{cla_prp}E29100{lc}{data}00", "BF2E with 5C->5C")

# Attempt 3: Original INS=BA but through STORE DATA wrapper?
# No, just try BF2E with no tag list
data = tlv("BF2E", "")
lc = format(len(bytes.fromhex(data)), "02X")
csim(f"{cla_prp}E29100{lc}{data}00", "BF2E tlv empty")

# Attempt 4: P1=11 then P1=91 (multi-block)
data = "BF2E00"
lc = format(len(bytes.fromhex(data)), "02X")
csim(f"{cla_prp}E21100{lc}{data}", "BF2E P1=11 no Le")

# Attempt 5: without Le byte
data = "BF2E00"
lc = format(len(bytes.fromhex(data)), "02X")
csim(f"{cla_prp}E29100{lc}{data}", "BF2E no Le")

# === 4. Try eUICCInfo1 (BF20) ===
print("\n=== eUICCInfo1 attempts ===")

# Attempt 1: BF20 empty
data = "BF2000"
lc = format(len(bytes.fromhex(data)), "02X")
csim(f"{cla_prp}E29100{lc}{data}00", "BF20 empty")

# Attempt 2: BF20 with tag list for common tags
data = tlv("BF20", tlv("5C", "82"))  # requesting SVN
lc = format(len(bytes.fromhex(data)), "02X")
csim(f"{cla_prp}E29100{lc}{data}00", "BF20 with 5C->82")

# Attempt 3: BF20 no Le
data = "BF2000"
lc = format(len(bytes.fromhex(data)), "02X")
csim(f"{cla_prp}E29100{lc}{data}", "BF20 no Le")

# === 5. Try eUICCInfo2 (BF22) ===
print("\n=== eUICCInfo2 attempts ===")
data = "BF2200"
lc = format(len(bytes.fromhex(data)), "02X")
csim(f"{cla_prp}E29100{lc}{data}00", "BF22 empty")

# === 6. Try direct INS=E2 for other ES10b commands ===
print("\n=== Other ES10b via STORE DATA ===")

# Try GetEUICCChallenge as-is but with INS=E2 P1=80
data = ""  # no data, like the original BA command
csim(f"{cla_prp}E2800000", "INS=E2 P1=80 (like BA)")

# Try with BF2E containing explicit empty SEQUENCE
data = "BF2E023000"  # BF2E { SEQUENCE {} }
lc = format(len(bytes.fromhex(data)), "02X")
csim(f"{cla_prp}E29100{lc}{data}00", "BF2E with empty SEQ")

# === Clean up ===
print("\n=== Close ===")
csim(f"007080{cla_std}{cla_std}", "CLOSE CHANNEL")

ser.close()
print("\nDone")
