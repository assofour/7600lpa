#!/usr/bin/env python3
"""
main.py — SGP.22 LPA bootstrap test
Reads transport mode from config.yaml, selects ISD-R, prints EID.
"""

import logging
import sys
from pathlib import Path

import yaml

from transport import MockTransport, RealTransport, QmiTransport
from lpa_manager import LPAManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"[ERROR] config.yaml not found at {p.resolve()}")
    with p.open() as f:
        return yaml.safe_load(f)


def setup_logging(cfg: dict) -> None:
    log_cfg = cfg.get("logging", {})
    logging.basicConfig(
        level=getattr(logging, log_cfg.get("level", "INFO"), logging.INFO),
        format=log_cfg.get("format", "%(asctime)s [%(levelname)s] %(message)s"),
    )


def build_transport(cfg: dict):
    t_cfg = cfg["transport"]
    mode  = t_cfg.get("mode", "mock").lower()
    if mode == "mock":
        return MockTransport()
    if mode == "real":
        return RealTransport(
            port     = t_cfg["port"],
            baudrate = int(t_cfg.get("baudrate", 115200)),
            timeout  = float(t_cfg.get("timeout", 10.0)),
        )
    if mode == "qmi":
        if t_cfg.get("stop_mm", True):
            import subprocess, shutil
            if shutil.which("systemctl"):
                subprocess.run(["sudo", "systemctl", "stop", "ModemManager"],
                               check=False, capture_output=True)
        return QmiTransport(
            device  = t_cfg.get("device", "/dev/cdc-wdm0"),
            slot    = int(t_cfg.get("slot", 1)),
            timeout = float(t_cfg.get("timeout", 10.0)),
        )
    sys.exit(f"[ERROR] Unknown transport mode: '{mode}' (expected mock | real | qmi)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()
    setup_logging(cfg)
    log = logging.getLogger("main")

    mode = cfg["transport"].get("mode", "mock").upper()
    log.info("━" * 50)
    log.info("eSIM LPA  —  transport mode: %s", mode)
    log.info("━" * 50)

    transport = build_transport(cfg)

    with transport:
        lpa = LPAManager(transport)

        # ── 1. SELECT ISD-R ────────────────────────────────────────────
        log.info("[Step 1] SELECT ISD-R")
        lpa.select_isdr()

        # ── 2. EID ─────────────────────────────────────────────────────
        log.info("[Step 2] GET EID")
        eid_override = cfg["euicc"].get("eid_override", "").strip()
        if eid_override:
            eid = eid_override.upper()
            log.info("EID override from config: %s", eid)
        else:
            try:
                eid = lpa.get_eid()
            except RuntimeError as exc:
                log.warning("EID unavailable: %s", exc)
                eid = None

        # ── 3. eUICCInfo2 ───────────────────────────────────────────────
        log.info("[Step 3] GET eUICCInfo2 (BF22)")
        try:
            info2 = lpa.get_euicc_info2()
            svn = info2.get("svn", b"")
            svn_str = ".".join(str(b) for b in svn) if svn else "unknown"
            log.info("eUICCInfo2 OK  (SVN: %s, %d field(s))", svn_str, len(info2))
        except RuntimeError as exc:
            log.warning("eUICCInfo2 unavailable: %s", exc)
            info2 = {}

    # ── Summary ─────────────────────────────────────────────────────────
    separator = "═" * 52
    print()
    print(separator)
    print(f"  Mode          : {mode}")
    print(f"  EID           : {eid or '(not available — check card packaging or set eid_override)'}")
    if info2:
        svn = info2.get("svn", b"")
        print(f"  eSIM SVN      : {'.'.join(str(b) for b in svn)}")
    print(separator)
    print()


if __name__ == "__main__":
    main()
