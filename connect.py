#!/usr/bin/env python3
"""
connect.py — SGP.22 eSIM Cellular Connection & Diagnostics Tool

Runs on the Pi.  Steps:
  1  Parse QR / LPA activation code
  2  Connect to modem
  3  Check SIM status
  4  Set APN + roaming (AT+COPS=0 auto-select)
  5  Scan available operators (AT+COPS=?)
  6  Wait for network registration
  7  Activate PDP context
  8  Bring up mobile interface + DHCP
  9  Set route priority (mobile metric 10 beats WiFi/Ethernet)
  10 Ping + traceroute each target via mobile interface
  11 Restore routing
  → Write Markdown report

Usage:
    sudo python3 connect.py "LPA:1$smdp.example.com$TOKEN" --apn mvnoc.data
    sudo python3 connect.py --apn internet --no-scan
    sudo python3 connect.py --help
"""

import argparse
import datetime
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

try:
    import serial
except ImportError:
    sys.exit("ERROR: pyserial not installed — run: pip install pyserial")


# ─────────────────────────────────────────────────────────────────────────────
# Terminal colours
# ─────────────────────────────────────────────────────────────────────────────

_TTY = sys.stdout.isatty()

def _c(text, code): return f"{code}{text}\033[0m" if _TTY else text
def ok(t):   return _c(t, "\033[92m")
def warn(t): return _c(t, "\033[93m")
def err(t):  return _c(t, "\033[91m")
def info(t): return _c(t, "\033[96m")
def dim(t):  return _c(t, "\033[90m")
def bold(t): return _c(t, "\033[1m")


# ─────────────────────────────────────────────────────────────────────────────
# Reporter  — console + Markdown
# ─────────────────────────────────────────────────────────────────────────────

class Reporter:
    def __init__(self, path: str):
        self.path = Path(path)
        self._md: list[str] = []
        self._step = 0
        self._t0 = datetime.datetime.now()

    # ── internal ──────────────────────────────────────────────────────────────

    def _flush(self):
        self.path.write_text("\n".join(self._md) + "\n", encoding="utf-8")

    # ── structure ─────────────────────────────────────────────────────────────

    def header(self, title: str, subtitle: str = ""):
        now = self._t0.strftime("%Y-%m-%d %H:%M:%S")
        host = socket.gethostname()
        self._md += [
            f"# {title}",
            "",
            f"| | |",
            f"|---|---|",
            f"| **Date** | {now} |",
            f"| **Host** | {host} |",
            f"| **Report** | `{self.path.name}` |",
            "",
        ]
        if subtitle:
            self._md += [f"> {subtitle}", ""]
        self._md.append("---")
        self._flush()
        print()
        print(bold("=" * 62))
        print(bold(f"  {title}"))
        if subtitle:
            print(dim(f"  {subtitle}"))
        print(bold("=" * 62))

    def step(self, title: str):
        self._step += 1
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._md += ["", f"## Step {self._step} · {title}", "", f"*{ts}*", ""]
        self._flush()
        print()
        print(f"{bold(f'[{self._step}]')} {info(title)}")

    def divider(self):
        self._md += ["", "---", ""]
        self._flush()

    # ── content ───────────────────────────────────────────────────────────────

    def table(self, headers: list[str], rows: list[list]):
        sep = "| " + " | ".join("-" * max(len(h), 3) for h in headers) + " |"
        self._md += ["", "| " + " | ".join(headers) + " |", sep]
        for row in rows:
            self._md.append("| " + " | ".join(str(c) for c in row) + " |")
        self._md.append("")
        self._flush()

    def block(self, content: str, lang: str = ""):
        self._md += ["", f"```{lang}", content.rstrip(), "```", ""]
        self._flush()
        for line in content.rstrip().splitlines():
            print(f"      {dim(line)}")

    def note(self, text: str):
        self._md += [f"> {text}", ""]
        self._flush()

    def section(self, title: str):
        self._md += ["", f"### {title}", ""]
        self._flush()
        print(f"    {bold(title)}")

    # ── status ────────────────────────────────────────────────────────────────

    def cmd(self, label: str, value: str):
        self._md.append(f"**{label}**: `{value}`  ")
        self._flush()
        print(f"    {dim('→')} {label}: {dim(value)}")

    def result(self, label: str, value: str):
        self._md.append(f"**{label}**: `{value}`  ")
        self._flush()
        print(f"    {dim('←')} {label}: {value}")

    def ok(self, msg: str = "OK"):
        self._md += ["", f"> ✅ **{msg}**", ""]
        self._flush()
        print(f"    {ok('✓')} {ok(msg)}")

    def warn(self, msg: str):
        self._md += ["", f"> ⚠️  **{msg}**", ""]
        self._flush()
        print(f"    {warn('⚠')}  {warn(msg)}")

    def fail(self, msg: str):
        self._md += ["", f"> ❌ **{msg}**", ""]
        self._flush()
        print(f"    {err('✗')} {err(msg)}")

    # ── finalise ──────────────────────────────────────────────────────────────

    def finalize(self, success: bool):
        elapsed = (datetime.datetime.now() - self._t0).total_seconds()
        status = "✅ Completed" if success else "❌ Failed"
        self._md += [
            "", "---", "", "## Summary", "",
            f"| | |",
            f"|---|---|",
            f"| **Status** | {status} |",
            f"| **Total time** | {elapsed:.1f}s |",
            f"| **Finished** | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |",
            "",
        ]
        self._flush()
        print()
        print(bold("=" * 62))
        print(f"  {ok('Completed') if success else err('Failed')}  ({elapsed:.1f}s)")
        print(f"  Report → {bold(str(self.path))}")
        print(bold("=" * 62))
        print()


# ─────────────────────────────────────────────────────────────────────────────
# Modem — serial AT command wrapper
# ─────────────────────────────────────────────────────────────────────────────

class Modem:
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 10.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser: serial.Serial | None = None

    def open(self):
        self._ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        time.sleep(0.5)
        self._ser.reset_input_buffer()
        # Wake modem
        for _ in range(3):
            self._ser.write(b"AT\r\n")
            time.sleep(0.4)
            if "OK" in self._drain():
                break
        self._ser.write(b"ATE1\r\n")
        time.sleep(0.4)
        self._drain()

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()

    def _drain(self) -> str:
        time.sleep(0.1)
        raw = b""
        while self._ser.in_waiting:
            raw += self._ser.read(self._ser.in_waiting)
            time.sleep(0.05)
        return raw.decode(errors="replace")

    def at(self, cmd: str, wait: float = 5.0) -> str:
        self._ser.reset_input_buffer()
        self._ser.write((cmd + "\r\n").encode())
        deadline = time.time() + wait
        buf = b""
        while time.time() < deadline:
            buf += self._ser.read(max(1, self._ser.in_waiting))
            decoded = buf.decode(errors="replace")
            if re.search(r"\b(OK|ERROR)\b|CME ERROR|CMS ERROR", decoded):
                break
            time.sleep(0.05)
        return buf.decode(errors="replace").strip()

    def at_long(self, cmd: str, timeout: float = 90.0) -> str:
        """For commands that take a long time (e.g. AT+COPS=?)."""
        self._ser.reset_input_buffer()
        self._ser.write((cmd + "\r\n").encode())
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            if self._ser.in_waiting:
                buf += self._ser.read(self._ser.in_waiting)
                if re.search(r"\b(OK|ERROR)\b", buf.decode(errors="replace")):
                    break
            time.sleep(0.2)
        return buf.decode(errors="replace").strip()

    def val(self, cmd: str, pattern: str, wait: float = 5.0) -> str:
        """Run command and extract first capture group."""
        resp = self.at(cmd, wait)
        m = re.search(pattern, resp)
        return m.group(1).strip() if m else ""


# ─────────────────────────────────────────────────────────────────────────────
# Network — interface, routing, ping, traceroute
# ─────────────────────────────────────────────────────────────────────────────

class Network:
    _MOBILE_PATTERNS = [r"usb\d+", r"wwan\d+", r"wwp\S+", r"rmnet\S+"]

    def __init__(self, iface: str | None):
        self.iface = iface
        self._route_added = False

    def detect(self) -> str | None:
        try:
            out = subprocess.check_output(["ip", "link", "show"], text=True)
        except Exception:
            return None
        for line in out.splitlines():
            for pat in self._MOBILE_PATTERNS:
                m = re.match(rf"\d+: ({pat})[@:]", line)
                if m:
                    return m.group(1)
        return None

    def bring_up(self, modem_ip: str | None) -> bool:
        _run(["sudo", "ip", "link", "set", self.iface, "up"])
        time.sleep(1)

        # If modem gave us an IP, configure manually
        if modem_ip and modem_ip != "0.0.0.0":
            _run(["sudo", "ip", "addr", "flush", "dev", self.iface])
            _run(["sudo", "ip", "addr", "add", f"{modem_ip}/32", "dev", self.iface])
            _run(["sudo", "ip", "route", "add", "default", "dev", self.iface])
            return bool(self.get_ip())

        # Try DHCP
        for dhcp in (["dhclient", "-v", "-1", "-timeout", "20"],
                     ["udhcpc", "-q", "-t", "5", "-i"]):
            bin_ = dhcp[0]
            if not shutil.which(bin_):
                continue
            if bin_ == "dhclient":
                cmd = ["sudo"] + dhcp + [self.iface]
            else:
                cmd = ["sudo", "udhcpc", "-q", "-t", "5", "-i", self.iface]
            r = _run(cmd, timeout=30)
            if self.get_ip():
                return True

        return False

    def get_ip(self) -> str | None:
        try:
            out = subprocess.check_output(["ip", "addr", "show", self.iface], text=True)
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
            return m.group(1) if m else None
        except Exception:
            return None

    def set_priority(self):
        """Add default route via mobile interface with metric 10 (beats WiFi)."""
        gw = self._gateway()
        cmd_base = ["sudo", "ip", "route"]
        args = (["via", gw] if gw else []) + ["dev", self.iface, "metric", "10"]
        # try add, then replace on conflict
        for verb in ("add", "replace"):
            r = _run(cmd_base + [verb, "default"] + args)
            if r.returncode == 0:
                self._route_added = True
                return

    def restore_priority(self):
        if not self._route_added:
            return
        gw = self._gateway()
        args = (["via", gw] if gw else []) + ["dev", self.iface, "metric", "10"]
        _run(["sudo", "ip", "route", "del", "default"] + args, check=False)

    def _gateway(self) -> str | None:
        try:
            out = subprocess.check_output(
                ["ip", "route", "show", "dev", self.iface], text=True)
            m = re.search(r"via (\d+\.\d+\.\d+\.\d+)", out)
            return m.group(1) if m else None
        except Exception:
            return None

    def routes(self) -> str:
        try:
            return subprocess.check_output(["ip", "route", "show"], text=True)
        except Exception:
            return ""

    def iface_info(self) -> str:
        try:
            return subprocess.check_output(["ip", "addr", "show", self.iface], text=True)
        except Exception:
            return ""

    def ping(self, target: str, count: int = 4) -> tuple[bool, str]:
        cmd = ["ping", "-c", str(count), "-W", "5"]
        if self.iface:
            cmd += ["-I", self.iface]
        cmd.append(target)
        r = _run(cmd, timeout=40)
        return r.returncode == 0, r.stdout + r.stderr

    def traceroute(self, target: str) -> tuple[bool, str]:
        if shutil.which("traceroute"):
            cmd = ["traceroute", "-m", "20", "-w", "3"]
            if self.iface:
                cmd += ["-i", self.iface]
            cmd.append(target)
        elif shutil.which("tracepath"):
            cmd = ["tracepath", "-m", "20", target]
        else:
            return False, "traceroute/tracepath not installed"
        r = _run(cmd, timeout=120)
        return r.returncode == 0, r.stdout + r.stderr


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 60, check: bool = False):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, check=check)


def parse_lpa(code: str) -> tuple[str, str]:
    """LPA:1$<address>$<matching_id>  →  (address, matching_id)"""
    code = code.strip()
    if code.upper().startswith("LPA:"):
        parts = code.split("$", 2)
        addr = parts[1].strip() if len(parts) > 1 else ""
        mid  = parts[2].strip() if len(parts) > 2 else ""
        return addr, mid
    return "", ""


_CREG_STAT = {
    0: "Not registered, idle",
    1: "Registered (home)",
    2: "Searching",
    3: "Registration denied",
    4: "Unknown",
    5: "Registered (roaming)",
}

def _creg_parse(resp: str) -> int:
    """Extract stat from +CREG/+CGREG/+CEREG response."""
    m = re.search(r"\+C(?:G|E)?REG:\s*\d+,(\d+)", resp)
    if m: return int(m.group(1))
    m = re.search(r"\+C(?:G|E)?REG:\s*(\d+)", resp)
    return int(m.group(1)) if m else 0

def _creg_desc(stat: int) -> str:
    return _CREG_STAT.get(stat, f"stat={stat}")

def _csq_desc(resp: str) -> tuple[int, str]:
    m = re.search(r"\+CSQ:\s*(\d+)", resp)
    if not m: return 99, "Unknown"
    v = int(m.group(1))
    if v == 99: return v, "No signal"
    dbm = -113 + v * 2
    q = "Excellent" if dbm > -70 else "Good" if dbm > -85 else "Fair" if dbm > -100 else "Poor"
    return v, f"{dbm} dBm ({q})"


# ─────────────────────────────────────────────────────────────────────────────
# Steps
# ─────────────────────────────────────────────────────────────────────────────

def s_parse_qr(args, rep: Reporter) -> tuple[str, str, str]:
    rep.step("Parse Input")

    qr  = (args.qr_code or "").strip()
    apn = (args.apn or "").strip()
    smdp, mid = parse_lpa(qr) if qr else ("", "")

    if qr:
        rep.cmd("QR input", qr)
        if smdp:
            rep.result("SM-DP+ address", smdp)
            rep.result("Matching ID", mid or "(none)")
        else:
            rep.warn("Not an LPA code — treating input as plain text")
    else:
        rep.note("No QR code provided.")

    if not apn:
        apn = "internet"
        rep.warn("No --apn given, defaulting to 'internet'")

    rep.table(
        ["Field", "Value"],
        [
            ["QR / LPA code", qr or "(none)"],
            ["SM-DP+ address", smdp or "(none)"],
            ["Matching ID", mid or "(none)"],
            ["APN", apn],
        ],
    )
    rep.ok("Input ready")
    return smdp, mid, apn


def s_open_modem(modem: Modem, rep: Reporter) -> bool:
    rep.step("Connect to Modem")
    rep.cmd("Port", modem.port)
    rep.cmd("Baudrate", str(modem.baudrate))
    print(f"    Opening {modem.port}...", flush=True)
    try:
        modem.open()
        rep.ok(f"Modem connected on {modem.port}")
        return True
    except Exception as e:
        rep.fail(f"Cannot open {modem.port}: {e}")
        return False


def s_check_sim(modem: Modem, rep: Reporter) -> bool:
    rep.step("Check SIM & Signal")

    mfr   = modem.val("AT+GMI",  r"\r\n([A-Za-z].+?)\r\n")
    model = modem.val("AT+GMM",  r"\r\n([A-Za-z].+?)\r\n")
    fw    = modem.val("AT+CGMR", r"\+CGMR:\s*(.+)")
    imei  = modem.val("AT+GSN",  r"\r\n(\d{15})\r\n")

    cpin  = modem.at("AT+CPIN?")
    pin_ok = "READY" in cpin

    iccid = modem.val("AT+CCID", r"\+CCID[:\s]+(\S+)")
    imsi  = modem.val("AT+CIMI", r"\r\n(\d{10,15})\r\n")

    csq_raw = modem.at("AT+CSQ")
    csq_v, csq_s = _csq_desc(csq_raw)

    rep.table(
        ["Item", "Value"],
        [
            ["Manufacturer", mfr or "?"],
            ["Model", model or "?"],
            ["Firmware", fw or "?"],
            ["IMEI", imei or "?"],
            ["SIM PIN", "READY" if pin_ok else f"NOT READY ({cpin.strip()})"],
            ["ICCID", iccid or "?"],
            ["IMSI", imsi or "?"],
            ["Signal (CSQ)", f"{csq_v} → {csq_s}"],
        ],
    )

    if not pin_ok:
        rep.fail("SIM not ready")
    elif csq_v == 99:
        rep.warn("No signal yet — will continue")
    else:
        rep.ok(f"SIM ready · {csq_s}")

    return pin_ok


def s_set_apn_roaming(modem: Modem, apn: str, rep: Reporter):
    rep.step("Configure APN & Roaming")

    # Set APN
    cmd = f'AT+CGDCONT=1,"IP","{apn}"'
    rep.cmd("Set APN (PDP context 1)", cmd)
    resp = modem.at(cmd)
    rep.result("Response", "OK" if "OK" in resp else resp.split()[-1])

    # Automatic operator selection — permits roaming networks
    rep.cmd("Operator selection", "AT+COPS=0  (automatic, roaming allowed)")
    resp = modem.at("AT+COPS=0", wait=8)
    rep.result("Response", "OK" if "OK" in resp else resp.split()[-1])

    # Preferred network type: automatic
    rep.cmd("Network type", "AT+CNMP=2  (automatic RAT)")
    resp = modem.at("AT+CNMP=2", wait=5)
    if "ERROR" in resp:
        rep.warn("AT+CNMP not supported — modem keeps its default RAT preference")
    else:
        rep.result("Response", "OK")

    # Verify
    cgdcont = modem.at("AT+CGDCONT?")
    m = re.search(r'\+CGDCONT:\s*1,"([^"]+)","([^"]*)"', cgdcont)
    if m:
        set_apn = m.group(2)
        rep.result("Confirmed APN", set_apn)
        if set_apn == apn:
            rep.ok(f"APN = {apn}")
        else:
            rep.warn(f"APN mismatch: expected {apn!r}, got {set_apn!r}")
    else:
        rep.warn("Could not verify PDP context")

    rep.note(
        "SIM7600G-H has no AT+CROAMING command. "
        "Roaming is permitted by the SIM's home network; "
        "AT+COPS=0 ensures the modem will register on any available network."
    )


def s_scan_networks(modem: Modem, rep: Reporter) -> list[dict]:
    rep.step("Scan Available Operators")
    rep.note("Running AT+COPS=? — may take up to 90 s.")

    print(f"    {warn('Scanning networks…')}", end="", flush=True)
    resp = modem.at_long("AT+COPS=?", timeout=90)
    print(f"\r    {ok('Scan done')}              ")

    rep.block(resp)

    ACT  = {0:"GSM",2:"WCDMA",3:"GSM/EDGE",4:"HSDPA",5:"HSUPA",
            6:"HSPA+",7:"LTE",9:"5G-NR"}
    STAT = {0:"Unknown",1:"Available",2:"Current",3:"Forbidden"}

    ops = []
    for m in re.finditer(r'\((\d+),"([^"]*)","([^"]*)","([^"]*)"(?:,(\d+))?\)', resp):
        s, ln, sn, plmn, act = m.groups()
        ops.append(dict(
            stat=STAT.get(int(s), s),
            name=ln or sn or plmn,
            plmn=plmn,
            rat=ACT.get(int(act or 0), f"RAT{act}"),
        ))

    if ops:
        rep.table(
            ["Status", "Operator", "PLMN", "RAT"],
            [[o["stat"], o["name"], o["plmn"], o["rat"]] for o in ops],
        )
        rep.ok(f"{len(ops)} operator(s) found")
    else:
        rep.warn("No operators in scan result (no signal or modem busy)")

    return ops


def s_wait_registration(modem: Modem, timeout: int, rep: Reporter) -> bool:
    rep.step("Network Registration")
    rep.note(f"Polling CREG/CGREG/CEREG — timeout {timeout}s.")

    # Detach → reattach to force fresh registration
    rep.cmd("Detach", "AT+CGATT=0")
    modem.at("AT+CGATT=0", wait=8)
    time.sleep(2)
    rep.cmd("Attach", "AT+CGATT=1")
    modem.at("AT+CGATT=1", wait=15)

    deadline = time.time() + timeout
    while time.time() < deadline:
        cs  = _creg_parse(modem.at("AT+CREG?"))
        ps  = _creg_parse(modem.at("AT+CGREG?"))
        lte = _creg_parse(modem.at("AT+CEREG?"))
        csq_v, csq_s = _csq_desc(modem.at("AT+CSQ"))

        registered = any(s in (1, 5) for s in (cs, ps, lte))
        line = (f"CS={cs} PS={ps} LTE={lte}  CSQ={csq_v}({csq_s})"
                f"  {_creg_desc(max(cs,ps,lte))}")
        print(f"\r    {dim(line[:72])}   ", end="", flush=True)

        if registered:
            print()
            roaming = any(s == 5 for s in (cs, ps, lte))

            cops = modem.at("AT+COPS?")
            m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)",(\d+)', cops)
            op  = m.group(1) if m else "unknown"
            rat = {0:"GSM",2:"WCDMA",7:"LTE",9:"5G"}.get(
                int(m.group(2) if m else 0), "?")

            rep.table(
                ["Check", "Stat", "Description"],
                [
                    ["CS  (AT+CREG)",  cs,  _creg_desc(cs)],
                    ["PS  (AT+CGREG)", ps,  _creg_desc(ps)],
                    ["LTE (AT+CEREG)", lte, _creg_desc(lte)],
                    ["Signal",         csq_v, csq_s],
                    ["Operator",       op,  ""],
                    ["RAT",            rat, ""],
                    ["Roaming",        "Yes" if roaming else "No", ""],
                ],
            )
            rep.ok(f"Registered on {op} {rat} {'[roaming]' if roaming else '[home]'}")
            return True

        time.sleep(3)

    print()
    cs  = _creg_parse(modem.at("AT+CREG?"))
    ps  = _creg_parse(modem.at("AT+CGREG?"))
    lte = _creg_parse(modem.at("AT+CEREG?"))
    rep.table(
        ["Check", "Stat", "Description"],
        [
            ["CS  (AT+CREG)",  cs,  _creg_desc(cs)],
            ["PS  (AT+CGREG)", ps,  _creg_desc(ps)],
            ["LTE (AT+CEREG)", lte, _creg_desc(lte)],
        ],
    )
    rep.fail(f"Registration timed out after {timeout}s")
    return False


def s_activate_pdp(modem: Modem, rep: Reporter) -> str:
    rep.step("Activate PDP Context")

    rep.cmd("Activate", "AT+CGACT=1,1")
    resp = modem.at("AT+CGACT=1,1", wait=30)
    rep.result("Response", "OK" if "OK" in resp else resp.strip())

    addr_resp = modem.at("AT+CGPADDR=1", wait=5)
    m = re.search(r'\+CGPADDR:\s*1,"?(\d+\.\d+\.\d+\.\d+)"?', addr_resp)
    ip = m.group(1) if m else ""

    if ip and ip != "0.0.0.0":
        rep.result("Assigned IP", ip)
        rep.ok(f"PDP active — IP {ip}")
    else:
        rep.warn(f"No IP from AT+CGPADDR (resp: {addr_resp.strip()!r})")

    return ip


def s_bring_up_iface(net: Network, modem_ip: str, rep: Reporter) -> bool:
    rep.step("Bring Up Mobile Interface")

    if not net.iface:
        net.iface = net.detect()

    if not net.iface:
        rep.fail("No mobile interface detected (usb*, wwan*, rmnet*)")
        rep.note("Run `ip link show` on the Pi to list available interfaces, "
                 "then re-run with --iface <name>.")
        return False

    rep.result("Interface", net.iface)
    rep.block(net.iface_info())

    success = net.bring_up(modem_ip)
    ip = net.get_ip()

    if ip:
        rep.result("Interface IP", ip)
        rep.block(net.iface_info())
        rep.ok(f"{net.iface} is up with IP {ip}")
        return True

    rep.fail(f"No IP on {net.iface} — DHCP and manual config both failed")
    return False


def s_routing(net: Network, rep: Reporter):
    rep.step("Set Route Priority")

    rep.section("Routes before")
    rep.block(net.routes())

    rep.cmd("Add mobile default route", f"ip route add default dev {net.iface} metric 10")
    net.set_priority(  )

    rep.section("Routes after")
    rep.block(net.routes())

    rep.ok(f"{net.iface} default route at metric 10 (lower = higher priority than WiFi)")


def s_connectivity(net: Network, targets: list[str], rep: Reporter) -> bool:
    rep.step("Connectivity Test")
    all_ok = True

    for target in targets:
        rep.section(f"Target: {target}")

        # ── ping ──────────────────────────────────────────────────────────────
        ping_cmd = f"ping -c 4 -I {net.iface} {target}" if net.iface else f"ping -c 4 {target}"
        rep.cmd("Ping", ping_cmd)
        print(f"    Pinging {target}…", end="", flush=True)
        ping_ok, ping_out = net.ping(target)
        print(f"\r    {ok('ping done') if ping_ok else err('ping failed')}        ")

        rep.block(ping_out)

        m = re.search(r"(\d+) packets transmitted, (\d+) received", ping_out)
        if m:
            tx, rx = int(m.group(1)), int(m.group(2))
            loss = (tx - rx) * 100 // tx if tx else 100
            rtt = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", ping_out)
            rtt_str = f", avg RTT {rtt.group(1)} ms" if rtt else ""
            rep.result("Ping result", f"{rx}/{tx} received, {loss}% loss{rtt_str}")

        if ping_ok:
            rep.ok(f"ping {target} OK")
        else:
            rep.fail(f"ping {target} FAILED")
            all_ok = False

        # ── traceroute ────────────────────────────────────────────────────────
        tr_bin = "traceroute" if shutil.which("traceroute") else "tracepath"
        tr_iface = f"-i {net.iface}" if net.iface and tr_bin == "traceroute" else ""
        rep.cmd("Traceroute", f"{tr_bin} {tr_iface} {target}".strip())
        print(f"    Traceroute to {target}…", end="", flush=True)
        tr_ok, tr_out = net.traceroute(target)
        print(f"\r    {ok('traceroute done')}          ")

        rep.block(tr_out)
        if tr_ok:
            rep.ok(f"traceroute {target} completed")
        else:
            rep.warn(f"traceroute {target}: some hops timed out")

    return all_ok


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="connect.py",
        description="SGP.22 eSIM cellular connection & diagnostics tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("qr_code", nargs="?", default=None,
                    help="LPA activation code (LPA:1$address$token)")
    ap.add_argument("--apn", default="",
                    help="Mobile data APN  e.g. mvnoc.data")
    ap.add_argument("--port", default="/dev/ttyUSB2",
                    help="AT serial port  [/dev/ttyUSB2]")
    ap.add_argument("--iface", default=None,
                    help="Mobile network interface  [auto-detect]")
    ap.add_argument("--report", default=None,
                    help="Report output path  [report_TIMESTAMP.md]")
    ap.add_argument("--no-scan", action="store_true",
                    help="Skip operator scan")
    ap.add_argument("--timeout", type=int, default=120,
                    help="Registration timeout seconds  [120]")
    ap.add_argument("--targets", nargs="+",
                    default=["google.com", "cloudflare.com"],
                    help="Ping/traceroute targets")
    args = ap.parse_args()

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rep = Reporter(args.report or f"report_{ts}.md")
    rep.header(
        "SGP.22 Cellular Connection Report",
        f"port={args.port}  apn={args.apn or '?'}  "
        f"targets={', '.join(args.targets)}",
    )

    modem   = Modem(args.port)
    net     = Network(args.iface)
    success = False

    try:
        # 1 ── parse QR ────────────────────────────────────────────────────────
        smdp, mid, apn = s_parse_qr(args, rep)

        # 2 ── open modem ──────────────────────────────────────────────────────
        if not s_open_modem(modem, rep):
            rep.finalize(False)
            return 1

        # 3 ── SIM check ───────────────────────────────────────────────────────
        s_check_sim(modem, rep)

        # 4 ── APN + roaming ───────────────────────────────────────────────────
        s_set_apn_roaming(modem, apn, rep)

        # 5 ── network scan ────────────────────────────────────────────────────
        if args.no_scan:
            rep.step("Scan Available Operators")
            rep.warn("Skipped (--no-scan)")
        else:
            s_scan_networks(modem, rep)

        # 6 ── registration ────────────────────────────────────────────────────
        registered = s_wait_registration(modem, args.timeout, rep)

        # 7 ── PDP activation ──────────────────────────────────────────────────
        modem_ip = s_activate_pdp(modem, rep)

        # 8 ── interface ───────────────────────────────────────────────────────
        iface_ok = s_bring_up_iface(net, modem_ip, rep)

        # 9 ── routing ─────────────────────────────────────────────────────────
        if iface_ok and net.iface:
            s_routing(net, rep)
        else:
            rep.step("Set Route Priority")
            rep.warn("No mobile interface — using system default routing")

        # close serial before ping so the port is free
        modem.close()

        # 10 ── connectivity ───────────────────────────────────────────────────
        success = s_connectivity(net, args.targets, rep)

    except KeyboardInterrupt:
        print()
        rep.warn("Interrupted by user (Ctrl-C)")
    except Exception:
        rep.fail(f"Unexpected error")
        rep.block(traceback.format_exc())
    finally:
        try:
            modem.close()
        except Exception:
            pass
        if net._route_added:
            rep.step("Restore Routing")
            net.restore_priority()
            rep.ok("Mobile priority route removed — original routing restored")
        rep.divider()
        rep.finalize(success)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
