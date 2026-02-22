"""
transport.py — SGP.22 LPA Transport Layer
Supports MockTransport, RealTransport (AT+CSIM), and QmiTransport (QMI UIM).
"""

from __future__ import annotations

import logging
import os
import select
import struct
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# APDU Response
# ---------------------------------------------------------------------------

class APDUResponse:
    """Parsed APDU response: data bytes + status words SW1/SW2."""

    def __init__(self, data: bytes, sw1: int, sw2: int) -> None:
        self.data = data
        self.sw1 = sw1
        self.sw2 = sw2
        self.sw = (sw1 << 8) | sw2

    @property
    def success(self) -> bool:
        return self.sw == 0x9000

    @property
    def sw_hex(self) -> str:
        return f"{self.sw:04X}"

    def __repr__(self) -> str:
        data_str = self.data.hex().upper() if self.data else "(none)"
        return f"APDUResponse(data={data_str}, SW={self.sw_hex})"


# ---------------------------------------------------------------------------
# Abstract Transport
# ---------------------------------------------------------------------------

class Transport(ABC):
    """Abstract APDU transport. Subclasses implement _send_raw(); this class
    handles the ISO 7816-4 GET RESPONSE (61xx) and wrong-Le (6Cxx) loops."""

    @abstractmethod
    def connect(self) -> None:
        """Open the channel (serial port, mock init, …)."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the channel."""

    @abstractmethod
    def _send_raw(self, apdu: bytes) -> APDUResponse:
        """Send one APDU, return the raw card response with NO post-processing."""

    def _get_response_cla(self) -> int:
        """CLA byte for GET RESPONSE. Override for channel-aware transports."""
        return 0x00

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_raw(self, apdu: bytes) -> bytes:
        """Send an APDU on the basic channel (channel 0) without channel encoding.
        Returns raw response bytes (data only, no SW). Used for TERMINAL CAPABILITY etc."""
        response = self._send_raw(apdu)
        return bytes([response.sw1, response.sw2])

    def send_apdu(self, apdu: bytes) -> APDUResponse:
        """
        Send an APDU with automatic:
          • GET RESPONSE loop (SW1=0x61): card signals more data available.
            Iteratively issue 00 C0 00 00 <SW2> until a non-61 SW is seen.
          • Wrong Le retry   (SW1=0x6C): resend APDU with correct Le=SW2.
        """
        response = self._send_raw(apdu)
        accumulated = bytearray(response.data)

        # ---- 6Cxx: resend with correct Le (one shot) ----
        if response.sw1 == 0x6C:
            # SW2=0 means Le should be 0x00 (=256 bytes per ISO 7816-4)
            fixed_apdu = apdu[:4] + bytes([response.sw2])
            response = self._send_raw(fixed_apdu)
            accumulated = bytearray(response.data)

        # ---- 61xx: GET RESPONSE loop ----
        # GET RESPONSE CLA must match the channel. _get_response_cla()
        # provides the right encoding; base class defaults to 0x00.
        gr_cla = self._get_response_cla()
        while response.sw1 == 0x61:
            le_byte = response.sw2  # 0x00 means "up to 256"
            logger.debug("GET RESPONSE ← %d bytes pending",
                         response.sw2 if response.sw2 else 256)
            get_resp = bytes([gr_cla, 0xC0, 0x00, 0x00, le_byte])
            response = self._send_raw(get_resp)
            accumulated += response.data

        return APDUResponse(bytes(accumulated), response.sw1, response.sw2)

    # Convenience context manager
    def __enter__(self) -> Transport:
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()


# ---------------------------------------------------------------------------
# MockTransport
# ---------------------------------------------------------------------------

# ISD-R AID per GSMA SGP.32 Annex B
_ISDR_AID = bytes.fromhex("A0000005591010FFFFFFFF8900000100")

# Simulated EID (32 BCD digits = 16 bytes)
_MOCK_EID = bytes.fromhex("89880000000000000000000000000001")


def _tlv(tag: bytes, value: bytes) -> bytes:
    """Minimal BER-TLV encoder (lengths up to 65535)."""
    n = len(value)
    if n < 0x80:
        length_enc = bytes([n])
    elif n < 0x100:
        length_enc = bytes([0x81, n])
    else:
        length_enc = bytes([0x82, n >> 8, n & 0xFF])
    return tag + length_enc + value


class MockTransport(Transport):
    """
    Simulates AT+CSIM exchanges without physical hardware.

    Supported commands:
      • SELECT ISD-R (AID: A0000005591010FFFFFFFF8900000100) → 9000
      • GET DATA BF3E (EID)        → BF3E{ 5A{ <mock EID> } }  + 9000
      • GET DATA BF22 (eUICCInfo2) → minimal stub               + 9000
      • Everything else            → 6D00 (INS not supported)
    """

    def __init__(self) -> None:
        self._selected = None

    def connect(self) -> None:
        logger.info("[Mock] Transport connected")

    def disconnect(self) -> None:
        logger.info("[Mock] Transport disconnected")

    def _send_raw(self, apdu: bytes) -> APDUResponse:
        logger.debug("[Mock] >> %s", apdu.hex().upper())

        if len(apdu) < 4:
            return APDUResponse(b"", 0x67, 0x00)  # Wrong length

        cla, ins, p1, p2 = apdu[0], apdu[1], apdu[2], apdu[3]

        # ------ SELECT by AID (INS=A4, P1=04) ------
        if ins == 0xA4 and p1 == 0x04:
            lc = apdu[4] if len(apdu) > 4 else 0
            aid = apdu[5 : 5 + lc]
            if aid == _ISDR_AID:
                self._selected = "ISDR"
                logger.debug("[Mock] << SELECT ISD-R → 9000")
                return APDUResponse(b"", 0x90, 0x00)
            logger.debug("[Mock] << SELECT unknown AID → 6A82")
            return APDUResponse(b"", 0x6A, 0x82)  # File not found

        # ------ GET DATA (CLA=80, INS=CA) ------
        if cla == 0x80 and ins == 0xCA:

            # BF3E — EID
            if p1 == 0xBF and p2 == 0x3E:
                inner = _tlv(b"\x5A", _MOCK_EID)          # 5A <len> <EID>
                body  = _tlv(b"\xBF\x3E", inner)           # BF3E <len> <inner>
                logger.debug("[Mock] << GET DATA EID → %s 9000", body.hex().upper())
                return APDUResponse(body, 0x90, 0x00)

            # BF22 — eUICCInfo2 (minimal stub for now)
            if p1 == 0xBF and p2 == 0x22:
                # svn "2.2.0" (03 02 00), profileVersion, loaderVersion stubs
                svn     = _tlv(b"\x82", bytes([0x02, 0x02, 0x00]))  # SVN 2.2.0
                fw_ver  = _tlv(b"\x84", b"\x00\x01")               # firmwareVer stub
                inner   = svn + fw_ver
                body    = _tlv(b"\xBF\x22", inner)
                logger.debug("[Mock] << GET DATA eUICCInfo2 → %s 9000", body.hex().upper())
                return APDUResponse(body, 0x90, 0x00)

        logger.warning("[Mock] Unhandled APDU: %s → 6D00", apdu.hex().upper())
        return APDUResponse(b"", 0x6D, 0x00)  # INS not supported


# ---------------------------------------------------------------------------
# RealTransport  (SIM7600G-H via AT+CSIM)
# ---------------------------------------------------------------------------

class RealTransport(Transport):
    """
    Sends APDUs via AT+CSIM over a serial port (SIM7600G-H).

    On connect, opens a logical channel via MANAGE CHANNEL and selects ISD-R
    on that channel — this bypasses the modem's SELECT-by-AID filter on the
    basic channel.  All subsequent APDUs have their CLA byte channel-encoded.

    Typical port on Pi 5 + SIM7600G-H:
      /dev/ttyUSB2  (AT command channel — check with `ls /dev/ttyUSB*`)
    """

    _ISDR_AID = bytes.fromhex("A0000005591010FFFFFFFF8900000100")

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 10.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial = None
        self._channel: int | None = None      # logical channel number
        self._isdr_selected: bool = False

    def _cla_for_channel(self, proprietary: bool = False) -> int:
        """Encode CLA byte for the current logical channel per ISO 7816-4.
        Channels 1-3: basic (0x0X / 0x8X), channels 4-19: extended (0x4X / 0xCX)."""
        ch = self._channel or 0
        if ch <= 3:
            return (0x80 if proprietary else 0x00) | ch
        else:
            return (0xC0 if proprietary else 0x40) | (ch - 4)

    def connect(self) -> None:
        try:
            import serial  # pyserial
        except ImportError:
            raise RuntimeError("pyserial not installed — run: pip install pyserial")

        import serial as _serial_mod
        self._serial = _serial_mod.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
        )
        # Sanity check
        self._serial.write(b"AT\r\n")
        resp = self._serial.read_until(b"OK\r\n")
        if b"OK" not in resp:
            raise RuntimeError("Modem not responding to AT command")
        logger.info("[Real] Connected: %s @ %d baud", self.port, self.baudrate)

        # ── Open logical channel ──────────────────────────────────────────────
        # The modem blocks SELECT-by-AID (P1=04) on the basic channel (CLA=0x00).
        # MANAGE CHANNEL is not filtered, so we open ch1 and SELECT ISD-R there.
        try:
            ch_raw = self._at_csim("0070000001")   # MANAGE CHANNEL open
            ch_sw  = ch_raw[-4:]
            ch_data = ch_raw[:-4]
            if ch_sw == "9000" and ch_data:
                self._channel = int(ch_data, 16)
                logger.info("[Real] Logical channel %d opened", self._channel)

                # SELECT ISD-R on the logical channel
                cla = format(self._cla_for_channel(proprietary=False), "02X")
                aid_hex = self._ISDR_AID.hex().upper()
                lc  = format(len(self._ISDR_AID), "02X")
                sel_raw = self._at_csim(f"{cla}A40400{lc}{aid_hex}")
                sel_sw  = sel_raw[-4:]

                # Handle 61xx (GET RESPONSE)
                if sel_sw.startswith("61"):
                    le = sel_sw[2:] or "FF"
                    gr_raw = self._at_csim(f"{cla}C00000{le}")
                    sel_sw = gr_raw[-4:]

                if sel_sw == "9000":
                    self._isdr_selected = True
                    logger.info("[Real] ISD-R selected on channel %d", self._channel)
                else:
                    logger.warning("[Real] SELECT ISD-R returned SW=%s", sel_sw)
            else:
                logger.warning(
                    "[Real] MANAGE CHANNEL returned SW=%s — using basic channel", ch_sw)
        except RuntimeError as exc:
            logger.warning("[Real] Could not open logical channel: %s — using basic channel", exc)

    def disconnect(self) -> None:
        # Close logical channel if open
        if self._channel is not None:
            try:
                ch = self._channel
                self._at_csim(f"00708000{ch:02X}")   # MANAGE CHANNEL close
                logger.debug("[Real] Logical channel %d closed", ch)
            except Exception:
                pass
            self._channel = None
            self._isdr_selected = False

        if self._serial and self._serial.is_open:
            self._serial.close()
            logger.info("[Real] Disconnected")

    def _get_response_cla(self) -> int:
        """GET RESPONSE CLA with correct channel encoding (standard, non-proprietary)."""
        if self._channel and self._isdr_selected:
            return self._cla_for_channel(proprietary=False)
        return 0x00

    # ------------------------------------------------------------------

    def _at_csim(self, apdu_hex: str) -> str:
        """
        Issue AT+CSIM=<len>,"<HEX>" and return the raw response hex string.
        SIM7600G-H response format: +CSIM: <len>,"<HEX>"
        """
        cmd = f'AT+CSIM={len(apdu_hex)},"{apdu_hex}"\r\n'
        self._serial.write(cmd.encode())
        logger.debug("[Real] AT >> %s", cmd.strip())

        csim_line = ""
        while True:
            line = self._serial.readline().decode(errors="replace").strip()
            if not line:
                continue
            if line.startswith("+CSIM:"):
                csim_line = line
            if line in ("OK", "ERROR") or line.startswith("+CME ERROR"):
                break

        if not csim_line:
            raise RuntimeError("No +CSIM response received from modem")

        # +CSIM: 4,"9000"  →  split on first comma
        _, payload = csim_line.split(",", 1)
        return payload.strip().strip('"')

    def _send_raw(self, apdu: bytes) -> APDUResponse:
        # ── Intercept SELECT ISD-R ───────────────────────────────────────────
        # ISD-R is already selected on the logical channel; skip re-selection.
        if (self._isdr_selected
                and len(apdu) >= 5 + len(self._ISDR_AID)
                and apdu[1] == 0xA4 and apdu[2] == 0x04
                and apdu[5: 5 + len(self._ISDR_AID)] == self._ISDR_AID):
            logger.info("[Real] SELECT ISD-R skipped (already on ch%d)", self._channel)
            return APDUResponse(b"", 0x90, 0x00)

        # ── Channel-encode CLA ───────────────────────────────────────────────
        # ISO 7816-4: channels 1-3 use basic CLA (bits 1-0),
        # channels 4-19 use extended CLA (0x40/0xC0 base).
        if self._channel and self._isdr_selected and apdu:
            cla = apdu[0]
            proprietary = (cla & 0x80) != 0
            new_cla = self._cla_for_channel(proprietary=proprietary)
            apdu = bytes([new_cla]) + apdu[1:]

        apdu_hex = apdu.hex().upper()
        raw = self._at_csim(apdu_hex)
        logger.debug("[Real] AT << %s", raw)

        if len(raw) < 4:
            raise RuntimeError(f"Response too short: '{raw}'")

        sw_hex   = raw[-4:]
        data_hex = raw[:-4]

        sw1 = int(sw_hex[:2], 16)
        sw2 = int(sw_hex[2:], 16)
        data = bytes.fromhex(data_hex) if data_hex else b""

        return APDUResponse(data, sw1, sw2)

    def send_raw(self, apdu: bytes) -> bytes:
        """Send APDU on basic channel (no channel encoding). For TERMINAL CAPABILITY etc."""
        apdu_hex = apdu.hex().upper()
        raw = self._at_csim(apdu_hex)
        logger.debug("[Real] send_raw << %s", raw)
        if len(raw) < 4:
            raise RuntimeError(f"Response too short: '{raw}'")
        sw_hex = raw[-4:]
        return bytes.fromhex(sw_hex)


# ---------------------------------------------------------------------------
# QmiTransport  (SIM7600G-H via QMI UIM / /dev/cdc-wdm0)
# ---------------------------------------------------------------------------
#
# Why: AT+CSIM blocks SELECT-by-AID (P1=04) on this modem, so we can't reach
# the ISD-R applet via the serial AT channel.  QMI UIM has no such filter.
#
# Protocol sketch
# ───────────────
# QMUX frame  (over /dev/cdc-wdm0, one frame per read/write):
#   [0x01][Length(2LE)][Flags(1)][SvcID(1)][ClientID(1)][QMI-SDU...]
#   Length = total bytes after the IF_TYPE byte (i.e. frame_total - 1)
#
# CTL SDU  (SvcID=0x00):
#   [CT(1)][TxID(1)][MsgID(2LE)][TLVLen(2LE)][TLVs...]
#
# Service SDU  (SvcID≠0x00):
#   [CT(1)][TxID(2LE)][MsgID(2LE)][TLVLen(2LE)][TLVs...]
#
# TLV:  [Type(1)][Len(2LE)][Value(Len bytes)]
#
# Session flow:
#   1. CTL SYNC  (0x0027)  – reset transaction counters
#   2. CTL ALLOCATE_CLIENT (0x0022, TLV 0x01=UIM) → uim_client_id
#   3. UIM OPEN_LOGICAL_CHANNEL (0x0047, slot+AID) → channel_id
#   4. UIM SEND_APDU (0x003B, slot+apdu+channel)   per command
#   5. UIM CLOSE_LOGICAL_CHANNEL (0x0048) on exit
#   6. CTL RELEASE_CLIENT (0x0023)

class QmiTransport(Transport):
    """
    Sends APDUs to the ISD-R via the QMI UIM service on /dev/cdc-wdm0.

    Requires ModemManager to be stopped first (main.py handles this via
    config qmi.stop_mm = true).

    Usage in config.yaml:
        transport:
          mode: "qmi"
          device: "/dev/cdc-wdm0"
          slot: 1
    """

    # ── QMI service IDs ──────────────────────────────────────────────────────
    _SVC_CTL = 0x00
    _SVC_UIM = 0x0B

    # ── CTL message IDs ──────────────────────────────────────────────────────
    _CTL_SYNC           = 0x0027
    _CTL_ALLOC_CLIENT   = 0x0022
    _CTL_RELEASE_CLIENT = 0x0023

    # ── UIM message IDs ──────────────────────────────────────────────────────
    _UIM_OPEN_LC  = 0x0047
    _UIM_SEND_APDU = 0x003B
    _UIM_CLOSE_LC = 0x0048

    _ISDR_AID = bytes.fromhex("A0000005591010FFFFFFFF8900000100")

    def __init__(self, device: str = "/dev/cdc-wdm0",
                 slot: int = 1, timeout: float = 10.0) -> None:
        self.device  = device
        self.slot    = slot
        self.timeout = timeout
        self._fd         = None
        self._uim_client = 0
        self._channel_id = None
        self._ctl_tx     = 1    # CTL uses 1-byte tx-id
        self._svc_tx     = 1    # Service uses 2-byte tx-id

    # ── TLV helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _make_tlv(t: int, v: bytes) -> bytes:
        return struct.pack("<BH", t, len(v)) + v

    @staticmethod
    def _parse_tlvs(data: bytes) -> dict:
        out, i = {}, 0
        while i + 3 <= len(data):
            t = data[i]
            l = struct.unpack_from("<H", data, i + 1)[0]
            out[t] = data[i + 3: i + 3 + l]
            i += 3 + l
        return out

    # ── QMUX framing ──────────────────────────────────────────────────────────

    def _build_ctl(self, msg_id: int, tlvs: bytes = b"") -> bytes:
        """CTL service SDU: CT(1) TxID(1) MsgID(2) TLVLen(2) TLVs"""
        sdu = struct.pack("<BBHH", 0x00, self._ctl_tx & 0xFF, msg_id, len(tlvs)) + tlvs
        self._ctl_tx = (self._ctl_tx + 1) & 0xFF
        length = 5 + len(sdu)           # 2(len)+1(flags)+1(svc)+1(cli) + len(sdu)
        return struct.pack("<BHBBB", 0x01, length, 0x00, self._SVC_CTL, 0x00) + sdu

    def _build_svc(self, svc: int, cli: int, msg_id: int, tlvs: bytes = b"") -> bytes:
        """Service SDU: CT(1) TxID(2) MsgID(2) TLVLen(2) TLVs"""
        sdu = struct.pack("<BHHH", 0x00, self._svc_tx, msg_id, len(tlvs)) + tlvs
        self._svc_tx = (self._svc_tx + 1) & 0xFFFF
        length = 5 + len(sdu)
        return struct.pack("<BHBBB", 0x01, length, 0x00, svc, cli) + sdu

    def _parse_frame(self, frame: bytes):
        """Return (svc_id, msg_id, ct, tlvs_dict).  Raises on bad frame."""
        if len(frame) < 12 or frame[0] != 0x01:
            raise ValueError(f"Bad QMUX frame ({len(frame)}B): {frame[:16].hex()}")
        svc_id = frame[4]
        sdu    = frame[6:]
        if svc_id == self._SVC_CTL:
            ct, _tx, msg_id, msg_len = struct.unpack_from("<BBHH", sdu)
            tlv_data = sdu[6: 6 + msg_len]
        else:
            ct, _tx, msg_id, msg_len = struct.unpack_from("<BHHH", sdu)
            tlv_data = sdu[7: 7 + msg_len]
        return svc_id, msg_id, ct, self._parse_tlvs(tlv_data)

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _write(self, data: bytes) -> None:
        os.write(self._fd, data)
        logger.debug("[QMI] >> %s", data.hex().upper())

    def _read(self, timeout: float = None) -> bytes:
        t = timeout if timeout is not None else self.timeout
        r, _, _ = select.select([self._fd], [], [], t)
        if not r:
            raise RuntimeError("QMI read timeout")
        data = os.read(self._fd, 4096)
        logger.debug("[QMI] << %s", data.hex().upper())
        return data

    def _drain(self) -> None:
        """Discard any bytes buffered from a previous session."""
        while True:
            r, _, _ = select.select([self._fd], [], [], 0.3)
            if not r:
                break
            try:
                os.read(self._fd, 4096)
            except OSError:
                break

    @staticmethod
    def _qmi_ok(tlvs: dict) -> bool:
        """True when result TLV (0x02) says success (or is absent)."""
        if 0x02 not in tlvs:
            return True
        return struct.unpack_from("<H", tlvs[0x02])[0] == 0

    # ── Request / response helpers ────────────────────────────────────────────

    def _ctl_req(self, msg_id: int, tlvs: bytes = b"") -> dict:
        self._write(self._build_ctl(msg_id, tlvs))
        while True:
            frame = self._read()
            svc, mid, ct, resp_tlvs = self._parse_frame(frame)
            if svc == self._SVC_CTL and mid == msg_id:
                return resp_tlvs          # caller checks _qmi_ok

    def _uim_req(self, msg_id: int, tlvs: bytes = b"",
                 timeout: float = None) -> dict:
        self._write(self._build_svc(self._SVC_UIM, self._uim_client,
                                    msg_id, tlvs))
        while True:
            frame = self._read(timeout)
            svc, mid, ct, resp_tlvs = self._parse_frame(frame)
            # ct==0x02 = response; ct==0x04 = indication (skip)
            if svc == self._SVC_UIM and mid == msg_id and ct == 0x02:
                return resp_tlvs

    # ── Transport interface ───────────────────────────────────────────────────

    def connect(self) -> None:
        logger.info("[QMI] Opening %s", self.device)
        self._fd = os.open(self.device, os.O_RDWR | os.O_NONBLOCK)
        self._drain()

        # 1. CTL SYNC
        logger.info("[QMI] CTL SYNC")
        self._ctl_req(self._CTL_SYNC)

        # 2. Allocate UIM client
        # Response TLV 0x01 = [service_type(1), client_id(1)]
        logger.info("[QMI] Allocating UIM client")
        tlvs = self._make_tlv(0x01, bytes([self._SVC_UIM]))
        resp = self._ctl_req(self._CTL_ALLOC_CLIENT, tlvs)
        if not self._qmi_ok(resp) or 0x01 not in resp or len(resp[0x01]) < 2:
            raise RuntimeError("QMI: failed to allocate UIM client")
        self._uim_client = resp[0x01][1]
        logger.info("[QMI] UIM client id = %d", self._uim_client)

        # 3. Open logical channel to ISD-R
        # AID TLV value = raw AID bytes (no inline length prefix; TLV header has it)
        logger.info("[QMI] Opening logical channel to ISD-R")
        aid  = self._ISDR_AID
        tlvs = (self._make_tlv(0x01, bytes([self.slot])) +
                self._make_tlv(0x10, aid))
        resp = self._uim_req(self._UIM_OPEN_LC, tlvs)
        if not self._qmi_ok(resp) or 0x10 not in resp:
            err = resp.get(0x02, b"")
            raise RuntimeError(f"QMI: failed to open ISD-R channel: {err.hex()}")
        self._channel_id = resp[0x10][0]
        logger.info("[QMI] ISD-R on logical channel %d", self._channel_id)

    def disconnect(self) -> None:
        try:
            if self._channel_id is not None:
                tlvs = (self._make_tlv(0x01, bytes([self.slot])) +
                        self._make_tlv(0x10, bytes([self._channel_id])))
                self._uim_req(self._UIM_CLOSE_LC, tlvs, timeout=3.0)
                self._channel_id = None
        except Exception as exc:
            logger.debug("[QMI] close channel: %s", exc)

        try:
            if self._uim_client:
                tlvs = self._make_tlv(0x01, bytes([self._SVC_UIM,
                                                   self._uim_client]))
                self._ctl_req(self._CTL_RELEASE_CLIENT, tlvs)
                self._uim_client = 0
        except Exception as exc:
            logger.debug("[QMI] release client: %s", exc)

        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
        except Exception:
            pass

        logger.info("[QMI] Disconnected")

    def _send_raw(self, apdu: bytes) -> APDUResponse:
        if not apdu:
            raise ValueError("Empty APDU")

        # OPEN_LOGICAL_CHANNEL already selected ISD-R on connect(); a redundant
        # SELECT ISD-R via SEND_APDU is rejected by the modem — return 9000.
        if (len(apdu) >= 5 and apdu[1] == 0xA4 and apdu[2] == 0x04 and
                apdu[5:5 + len(self._ISDR_AID)] == self._ISDR_AID):
            logger.info("[QMI] SELECT ISD-R skipped (already open on ch %d)",
                        self._channel_id)
            return APDUResponse(b"", 0x90, 0x00)

        # QMI routes the APDU to the correct logical channel via TLV 0x10.
        # Do NOT encode the channel in the CLA byte — QMI handles that.
        logger.debug("[QMI] APDU >> %s (ch=%s)", apdu.hex().upper(),
                     self._channel_id)

        # SEND_APDU TLV 0x01 = QmiUimSession
        #   Provisioning/card sessions: session_type (1 byte only, no AID field)
        #   Non-provisioning sessions:  session_type (1 byte) + AID (uint8-array)
        # session_type 0x0A = NON_PROV_SLOT_1: references ISD-R by AID;
        # TLV 0x10 (channel_id) routes the APDU to the already-open channel.
        aid     = self._ISDR_AID
        session = bytes([0x0A]) + bytes([len(aid)]) + aid
        tlvs = (self._make_tlv(0x01, session) +
                self._make_tlv(0x02, apdu) +
                self._make_tlv(0x10, bytes([self._channel_id])))

        resp = self._uim_req(self._UIM_SEND_APDU, tlvs, timeout=30.0)

        if not self._qmi_ok(resp):
            err = resp.get(0x02, b"")
            raise RuntimeError(f"QMI SEND_APDU failed: result TLV={err.hex()}")

        if 0x10 not in resp:
            raise RuntimeError("QMI SEND_APDU: no response TLV in reply")

        # Response TLV 0x10 = raw APDU response bytes (SW1 SW2 at end)
        r_bytes = resp[0x10]

        if len(r_bytes) < 2:
            raise RuntimeError(f"APDU response too short: {r_bytes.hex()}")

        sw1, sw2 = r_bytes[-2], r_bytes[-1]
        data     = r_bytes[:-2]
        logger.debug("[QMI] APDU << data=%s SW=%02X%02X",
                     data.hex().upper() if data else "(none)", sw1, sw2)
        return APDUResponse(data, sw1, sw2)
