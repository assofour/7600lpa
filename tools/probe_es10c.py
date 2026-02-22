#!/usr/bin/env python3
"""
probe_es10c.py — Try ES10c STORE DATA (INS=E2) commands on eUICC.

ES10c wraps all commands inside STORE DATA (INS=E2, P1=91).
The modem may NOT filter INS=E2 the same way it filters INS=BA/CA.

Reference: KORE OmniSIM Local Profile Management Guide
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


def csim(apdu_hex):
    resp = at(f'AT+CSIM={len(apdu_hex)},"{apdu_hex}"', 3)
    m = re.search(r'\+CSIM:\s*\d+,"([^"]+)"', resp)
    raw = m.group(1) if m else ""
    print(f"  >> {apdu_hex}")
    print(f"  << {raw}")
    return raw


at("AT", 1)

# === 1. Open logical channel ===
print("=== Open logical channel ===")
r = csim("0070000001")
ch = int(r[:-4], 16) if r[-4:] == "9000" and len(r) > 4 else 0
print(f"Channel: {ch}")
cla_std = format(ch, "02X")            # standard inter-industry
cla_prp = format(0x80 | ch, "02X")     # proprietary

# === 2. SELECT ISD-R on logical channel ===
print("\n=== SELECT ISD-R ===")
r = csim(f"{cla_std}A4040010A0000005591010FFFFFFFF8900000100")
if r[-4:].startswith("61"):
    le = r[-2:]
    r = csim(f"{cla_std}C00000{le}")

# === 3. ES10c: Get EID via STORE DATA (INS=E2) ===
# BF3E { 5C { 5A } } → request EID
# APDU: CLA=83 INS=E2 P1=91 P2=00 Lc=06 Data=BF3E035C015A Le=00
print("\n=== ES10c: Get EID (STORE DATA E2) ===")
r = csim(f"{cla_prp}E2910006BF3E035C015A00")
if r and r[-4:] == "9000":
    print(f"  ** EID response data: {r[:-4]}")
elif r and r[-4:-2] == "61":
    le = r[-2:]
    r2 = csim(f"{cla_prp}C00000{le}")
    print(f"  ** EID data: {r2[:-4] if r2[-4:] == '9000' else r2}")

# Also try with standard CLA
print("\n=== ES10c: Get EID (std CLA) ===")
r = csim(f"{cla_std}E2910006BF3E035C015A00")
if r and r[-4:] == "9000":
    print(f"  ** EID response data: {r[:-4]}")
elif r and r[-4:-2] == "61":
    le = r[-2:]
    r2 = csim(f"{cla_std}C00000{le}")
    print(f"  ** EID data: {r2[:-4] if r2[-4:] == '9000' else r2}")

# === 4. ES10c: Get Profiles (STORE DATA) ===
# BF2D { 5C { 5A, 4F, 9F70 } } → list profiles
# Data: BF2D 06 5C 04 5A 4F 9F70
print("\n=== ES10c: GetProfilesInfo (STORE DATA E2) ===")
r = csim(f"{cla_prp}E2910009BF2D065C045A4F9F7000")
if r and r[-4:] == "9000":
    print(f"  ** Profiles data: {r[:-4]}")
elif r and r[-4:-2] == "61":
    le = r[-2:]
    r2 = csim(f"{cla_prp}C00000{le}")
    print(f"  ** Profiles data: {r2[:-4] if r2[-4:] == '9000' else r2}")

# === 5. ES10c: Get eUICCInfo (STORE DATA) ===
print("\n=== ES10c: eUICCInfo (STORE DATA E2) ===")
# Try BF20 (eUICCInfo1) via STORE DATA
r = csim(f"{cla_prp}E2910004BF20020000")
if r and r[-4:] == "9000":
    print(f"  ** eUICCInfo1 data: {r[:-4]}")
elif r and r[-4:-2] == "61":
    le = r[-2:]
    r2 = csim(f"{cla_prp}C00000{le}")
    print(f"  ** eUICCInfo1 data: {r2[:-4] if r2[-4:] == '9000' else r2}")

# === 6. Also try LPM applet (KORE/OmniSIM approach) ===
print("\n=== Try LPM applet SELECT ===")
# LPM AID: A000000815030040030902231003
r = csim(f"{cla_std}A404000EA000000815030040030902231003")
if r[-4:] == "9000":
    print("  ** LPM applet found!")
    # Get profiles via LPM
    r = csim(f"{cla_prp}180000FF")
    print(f"  ** LPM profiles: {r}")
elif r[-4:].startswith("61"):
    le = r[-2:]
    r2 = csim(f"{cla_std}C00000{le}")
    print(f"  ** LPM SELECT response: {r2}")
else:
    print(f"  ** LPM applet not found (SW={r[-4:]})")

# === 7. Try GetEUICCChallenge via STORE DATA ===
print("\n=== ES10c: GetEUICCChallenge via STORE DATA ===")
# BF2E {} → empty request body for challenge
r = csim(f"{cla_prp}E2910004BF2E020000")
if r and r[-4:] == "9000":
    print(f"  ** Challenge data: {r[:-4]}")
elif r and r[-4:-2] == "61":
    le = r[-2:]
    r2 = csim(f"{cla_prp}C00000{le}")
    print(f"  ** Challenge data: {r2[:-4] if r2[-4:] == '9000' else r2}")

# Clean up
print("\n=== Close channel ===")
csim(f"007080{cla_std}{cla_std}")

ser.close()
print("\n=== Done ===")
