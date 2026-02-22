#!/usr/bin/env python3
"""Cancel any pending eUICC session, then run download."""
import serial, time, re, subprocess, sys

PORT = "/dev/ttyUSB3"
ser = serial.Serial(PORT, 115200, timeout=3)
time.sleep(0.5)

def at(cmd):
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode())
    time.sleep(1)
    resp = ser.read(ser.in_waiting).decode(errors="replace")
    print(f">> {cmd}")
    for line in resp.strip().split("\n"):
        l = line.strip()
        if l and l != cmd:
            print(f"<< {l}")
    print()
    return resp

def parse_csim(r):
    m = re.search(r'\+CSIM:\s*\d+,"([0-9A-Fa-f]+)"', r)
    if not m:
        return "", "", ""
    raw = m.group(1)
    return raw, raw[-4:], raw[:-4]

def get_response_loop(cla_std, sw, data=""):
    all_data = data
    while sw.upper().startswith("61"):
        le = sw[2:]
        r = at(f'AT+CSIM=10,"{cla_std:02X}C00000{le}"')
        _, sw, data = parse_csim(r)
        all_data += data
    return sw, all_data

# Open channel
r = at('AT+CSIM=10,"0070000001"')
raw, sw, _ = parse_csim(r)
ch = int(raw[:2], 16)
print(f"Channel: {ch}")

if ch <= 3:
    cla_std = ch
    cla_prp = 0x80 | ch
else:
    cla_std = 0x40 | (ch - 4)
    cla_prp = 0xC0 | (ch - 4)

# SELECT ISD-R
aid = "A0000005591010FFFFFFFF8900000100"
apdu = f"{cla_std:02X}A4040010{aid}"
r = at(f'AT+CSIM={len(apdu)},"{apdu}"')
_, sw, _ = parse_csim(r)
sw, _ = get_response_loop(cla_std, sw)
print(f"SELECT ISD-R: SW={sw}\n")

# 1. CancelSession (BF41) — reason=endUserRejection (0)
print("=== CancelSession (BF41) ===")
# BF41 03 80 01 00 = CancelSession reason=0
payload = "BF4103800100"
lc = len(bytes.fromhex(payload))
apdu = f"{cla_prp:02X}E29100{lc:02X}{payload}00"
r = at(f'AT+CSIM={len(apdu)},"{apdu}"')
_, sw, data = parse_csim(r)
sw, data = get_response_loop(cla_std, sw, data)
print(f"CancelSession: SW={sw}, Data={data}")

# 2. ListNotifications (BF28)
print("\n=== ListNotifications (BF28) ===")
# BF28 00 = list all
payload = "BF2800"
lc = len(bytes.fromhex(payload))
apdu = f"{cla_prp:02X}E29100{lc:02X}{payload}00"
r = at(f'AT+CSIM={len(apdu)},"{apdu}"')
_, sw, data = parse_csim(r)
sw, data = get_response_loop(cla_std, sw, data)
print(f"ListNotifications: SW={sw}, Data={data}")

# 3. ListProfiles (BF2D)
print("\n=== ListProfiles (BF2D) ===")
payload = "BF2D025C00"
lc = len(bytes.fromhex(payload))
apdu = f"{cla_prp:02X}E29100{lc:02X}{payload}00"
r = at(f'AT+CSIM={len(apdu)},"{apdu}"')
_, sw, data = parse_csim(r)
sw, data = get_response_loop(cla_std, sw, data)
print(f"ListProfiles: SW={sw}, Data={data}")

# Close channel
at(f'AT+CSIM=10,"00708000{ch:02X}"')
ser.close()

print("\n=== Now running download.py ===\n")
sys.exit(subprocess.call(["sudo", "python3", "download.py", "--no-ssl-verify", "--debug"]))
