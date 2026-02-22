#!/usr/bin/env python3
"""Check eUICC profile state and residual profiles."""
import serial, time, re

ser = serial.Serial("/dev/ttyUSB3", 115200, timeout=3)
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

cla_std = (0x40 | (ch - 4)) if ch >= 4 else ch
cla_prp = (0xC0 | (ch - 4)) if ch >= 4 else (0x80 | ch)
print(f"CLA std=0x{cla_std:02X} prp=0x{cla_prp:02X}")

# SELECT ISD-R
aid = "A0000005591010FFFFFFFF8900000100"
apdu = f"{cla_std:02X}A4040010{aid}"
r = at(f'AT+CSIM={len(apdu)},"{apdu}"')
_, sw, data = parse_csim(r)
sw, _ = get_response_loop(cla_std, sw)
print(f"SELECT ISD-R: SW={sw}\n")

# ListProfiles with tags: 5A(ICCID), 9F70(state), 4F(AID)
print("=== ListProfiles (BF2D) ===")
payload = "BF2D075C055A9F704F"
lc = len(bytes.fromhex(payload))
apdu = f"{cla_prp:02X}E29100{lc:02X}{payload}00"
r = at(f'AT+CSIM={len(apdu)},"{apdu}"')
_, sw, data = parse_csim(r)
sw, data = get_response_loop(cla_std, sw, data)
print(f"ListProfiles data: {data}")
print(f"ListProfiles SW: {sw}")

# Parse profiles
if data and "E3" in data.upper():
    print("\nProfiles found in response!")
    # Each profile is wrapped in E3 tag
    pos = 0
    d = data.upper()
    while True:
        idx = d.find("E3", pos)
        if idx < 0:
            break
        plen = int(d[idx+2:idx+4], 16)
        profile_hex = d[idx+4:idx+4+plen*2]
        print(f"  Profile TLV: E3 {plen:02X} {profile_hex}")
        # Find ICCID (5A)
        sa = profile_hex.find("5A")
        if sa >= 0:
            ilen = int(profile_hex[sa+2:sa+4], 16)
            iccid = profile_hex[sa+4:sa+4+ilen*2]
            print(f"    ICCID: {iccid}")
        # Find state (9F70)
        sf = profile_hex.find("9F70")
        if sf >= 0:
            slen = int(profile_hex[sf+4:sf+6], 16)
            state = int(profile_hex[sf+6:sf+6+slen*2], 16)
            states = {0: "disabled", 1: "enabled", 2: "deleted"}
            print(f"    State: {state} ({states.get(state, 'unknown')})")
        pos = idx + 4 + plen * 2
else:
    print("\nNo profiles found (empty list)")

# GetEUICCInfo2
print("\n=== GetEUICCInfo2 (BF22) ===")
payload = "BF22005C00"
lc = len(bytes.fromhex(payload))
apdu = f"{cla_prp:02X}E29100{lc:02X}{payload}00"
r = at(f'AT+CSIM={len(apdu)},"{apdu}"')
_, sw, data = parse_csim(r)
sw, data = get_response_loop(cla_std, sw, data)
print(f"eUICCInfo2 ({len(data)//2} bytes): {data[:200]}...")
print(f"SW: {sw}")

# Close
at(f'AT+CSIM=10,"00708000{ch:02X}"')
ser.close()
