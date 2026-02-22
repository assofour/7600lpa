#!/usr/bin/env python3
"""Clear ALL pending eUICC notifications, then re-run download."""
import serial, time, re, sys, base64, requests, urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PORT = "/dev/ttyUSB3"
SMDP = "smdp.example.com"  # replace with your SM-DP+ address

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
    if label:
        print(f"  [{label}] >> {apdu_hex[:40]}... << SW={raw[-4:] if raw else 'NONE'}")
    # Handle 61xx chain
    all_data = raw[:-4] if len(raw) > 4 else ""
    sw = raw[-4:] if len(raw) >= 4 else ""
    while sw.startswith("61"):
        le = sw[2:]
        r2 = at(f'AT+CSIM=10,"{cla_std}C00000{le}"')
        raw2 = parse(r2)
        sw = raw2[-4:] if len(raw2) >= 4 else ""
        all_data += raw2[:-4] if len(raw2) > 4 else ""
    return all_data, sw

def store_data(data_hex, label=""):
    lc = format(len(bytes.fromhex(data_hex)), "02X")
    return csim(f"{cla_prp}E29100{lc}{data_hex}00", label)

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

# Setup
at("AT", 1)
r = at('AT+CSIM=10,"0070000001"')
raw = parse(r)
ch = int(raw[:2], 16) if raw else 0
sw = raw[-4:] if raw else ""
print(f"Channel: {ch}, SW={sw}")

if ch <= 3:
    cla_std = f"{ch:02X}"
    cla_prp = f"{0x80 | ch:02X}"
else:
    cla_std = f"{0x40 | (ch - 4):02X}"
    cla_prp = f"{0xC0 | (ch - 4):02X}"

# SELECT ISD-R
aid = "A0000005591010FFFFFFFF8900000100"
csim(f"{cla_std}A4040010{aid}", "SELECT ISD-R")

# 1. List notifications
print("\n=== Step 1: List Notifications ===")
data, sw = store_data("BF2800", "ListNotifications")
print(f"  SW={sw}, data length={len(data)//2} bytes")

# Parse seq numbers from BF28 response
seq_numbers = []
if data:
    # Find all 80 01 XX (seqNumber tags) inside BF2F containers
    hex_str = data.upper()
    # Each notification has BF2F wrapper with 80 XX XX (seqNumber)
    pos = 0
    while True:
        idx = hex_str.find("BF2F", pos)
        if idx < 0:
            break
        # Find 80 01 after BF2F
        search_start = idx + 4
        seq_idx = hex_str.find("8001", search_start)
        if seq_idx >= 0 and seq_idx < search_start + 10:
            seq_val = int(hex_str[seq_idx+4:seq_idx+6], 16)
            seq_numbers.append(seq_val)
        pos = idx + 4

print(f"  Found {len(seq_numbers)} notifications: seqNumbers={seq_numbers}")

# 2. Process each notification: retrieve, send to SM-DP+, remove
base_url = f"https://{SMDP}/gsma/rsp2/es9plus"
headers = {
    "Content-Type": "application/json",
    "X-Admin-Protocol": "gsma/rsp/v2.5.0",
}

for seq in seq_numbers:
    print(f"\n--- Notification seq={seq} ---")

    # Retrieve
    bf2b = tlv("BF2B", tlv("80", f"{seq:02X}"))
    data, sw = store_data(bf2b, f"Retrieve seq={seq}")
    if sw != "9000" or not data:
        print(f"  Retrieve failed: SW={sw}")
        # Still try to remove
    else:
        print(f"  Retrieved {len(data)//2} bytes")
        # Send to SM-DP+ (best effort)
        try:
            notif_bytes = bytes.fromhex(data)
            body = {"pendingNotification": base64.b64encode(notif_bytes).decode()}
            resp = requests.post(
                f"{base_url}/handleNotification",
                json=body, headers=headers, verify=False, timeout=15
            )
            print(f"  SM-DP+ handleNotification: HTTP {resp.status_code}")
        except Exception as e:
            print(f"  SM-DP+ error (non-fatal): {e}")

    # Remove from eUICC
    bf30 = tlv("BF30", tlv("80", f"{seq:02X}"))
    data, sw = store_data(bf30, f"Remove seq={seq}")
    print(f"  Remove: SW={sw}")

# 3. Verify cleared
print("\n=== Step 2: Verify Notifications Cleared ===")
data, sw = store_data("BF2800", "ListNotifications")
print(f"  SW={sw}, remaining data={len(data)//2} bytes")
if data:
    print(f"  Hex: {data[:200]}")

# 4. Check eUICCInfo2
print("\n=== Step 3: eUICCInfo2 ===")
data, sw = store_data(tlv("BF22", "5C00"), "eUICCInfo2")
print(f"  SW={sw}, {len(data)//2} bytes")

# Close
at(f'AT+CSIM=10,"00708000{ch:02X}"')
ser.close()
print("\nDone. Run download.py --no-ssl-verify next.")
