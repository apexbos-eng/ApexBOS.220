#!/usr/bin/env python3
"""
ApexBOSScanner.py - ITC 220 MHz Channel Scanner & PPM Calibrator for ApexBOS
=============================================================================
Automated channel frequency search and master oscillator PPM optimizer for
ITC/ATCS 220 MHz Advanced Civil Speed Enforcement System (ACSES) / PTC RF channels.

Workflow:
  1. Scans channel frequencies from Channel 97 to 176 (220.0125 MHz - 221.9875 MHz).
  2. Pauses for 1.0 second on each channel to detect 64-bit PTC unique word Sync Signatures.
  3. For every channel with detected Sync Signatures, sweeps PPM within ±4 from base PPM,
     observing each candidate PPM for 5.0 seconds to maximize Sync Signatures per 5 sec.
  4. Generates a comprehensive report displaying active channels, optimal PPM, average RSSI,
     and average SNR.
  5. Recommends the optimal consensus PPM to cover all active channels.
  6. Generates startup launch scripts (.bat, .ps1, .txt) with absolute paths saved into
     the system temporary directory.
"""

import argparse
import json
import math
import os
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from version import VERSION

import ApexBOSGrabber as grabber
from ApexBOSGrabber import (
    ContinuousChannelizer,
    DirectRtlSdrSource,
    DirectRtlTcpSource,
    SubprocessPipeSource,
    compute_signal_power_dbfs,
    demodulate_pi4_dqpsk,
    detect_rtl_sdr_executable,
    fec_decode_itc_packet,
    itc_channel_frequency,
    locate_unique_word_hits,
    parse_gain_value,
    resolve_sdr_stream_kind,
    SYNC_WORD_LEN_BITS,
)


# ANSI Color formatting
COLOR_RESET = "\033[0m"
COLOR_BOLD = "\033[1m"
COLOR_DIM = "\033[2m"
COLOR_RED = "\033[91m"
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_BLUE = "\033[94m"
COLOR_MAGENTA = "\033[95m"
COLOR_CYAN = "\033[96m"
COLOR_WHITE = "\033[97m"


class KeyboardController:
    """
    Non-blocking keyboard reader supporting Windows (msvcrt) and Linux/macOS (select/termios).
    Safely degrades to no-op when stdin is not a TTY (e.g. piped, mock tests, headless).
    """

    def __init__(self):
        self.is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
        self._old_termios = None
        self._fd = None
        if self.is_tty and sys.platform != "win32":
            try:
                import termios
                import tty
                self._fd = sys.stdin.fileno()
                self._old_termios = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
                cbreak_settings = termios.tcgetattr(self._fd)
                cbreak_settings[3] &= ~termios.ECHO
                termios.tcsetattr(self._fd, termios.TCSADRAIN, cbreak_settings)
            except Exception:
                self.is_tty = False

    def check_key(self) -> Optional[str]:
        """Returns lowercase character or key identifier if pressed, or None."""
        if not self.is_tty:
            return None
        try:
            if sys.platform == "win32":
                import msvcrt
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):  # Extended key prefix
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                        return None
                    return ch.lower()
            else:
                import select
                rlist, _, _ = select.select([sys.stdin], [], [], 0)
                if rlist:
                    ch = sys.stdin.read(1)
                    if ch == "\x1b":  # Escape
                        while True:
                            r_esc, _, _ = select.select([sys.stdin], [], [], 0.01)
                            if r_esc:
                                sys.stdin.read(1)
                            else:
                                break
                        return "esc"
                    return ch.lower()
        except Exception:
            pass
        return None

    def close(self):
        if self._old_termios is not None and self._fd is not None and sys.platform != "win32":
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_termios)
            except Exception:
                pass
            self._old_termios = None

    def __del__(self):
        self.close()


# Standard NOAA Weather Radio (NWR) continuous broadcast frequencies (Hz)
NOAA_CHANNELS = [
    162400000,  # 162.400 MHz (WX2)
    162425000,  # 162.425 MHz (WX4)
    162450000,  # 162.450 MHz (WX5)
    162475000,  # 162.475 MHz (WX3)
    162500000,  # 162.500 MHz (WX6)
    162525000,  # 162.525 MHz (WX7)
    162550000,  # 162.550 MHz (WX1)
]
NOAA_SURVEY_CENTER_HZ = 162462500  # 162.4625 MHz (leaves 12.5 kHz offset from DC to all NOAA channels)

# Sub-band LO center frequencies for wideband survey across Ch 97..176
# All centers are half-channel (12.5 kHz) offsets from the 25 kHz grid to prevent LO DC spike collisions.
INBAND_SURVEY_CENTERS_HZ = [220350000, 221000000, 221650000]


def compute_averaged_psd(
    raw_iq: bytes, sample_rate: int, nfft: int = 16384, max_blocks: int = 16
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Computes averaged Welch PSD (in dBFS) and frequency vector from uint8 interleaved I/Q bytes.
    Returns: (freqs_hz, psd_db).
    """
    if not raw_iq or len(raw_iq) < nfft * 2:
        return np.array([]), np.array([])
    raw_arr = np.frombuffer(raw_iq, dtype=np.uint8)
    n_samps = len(raw_arr) // 2
    i_flt = (raw_arr[0 : n_samps * 2 : 2].astype(np.float32) - 127.5) / 128.0
    q_flt = (raw_arr[1 : n_samps * 2 : 2].astype(np.float32) - 127.5) / 128.0
    iq_complex = (i_flt + 1j * q_flt).astype(np.complex64)
    window = np.hanning(nfft).astype(np.float32)
    win_pwr = float(np.sum(window**2))
    step = nfft // 2
    n_blocks = max(1, (len(iq_complex) - nfft) // step + 1)
    eval_blocks = min(max_blocks, n_blocks)
    psd_accum = np.zeros(nfft, dtype=np.float64)
    for b in range(eval_blocks):
        offset = b * step
        chunk = iq_complex[offset : offset + nfft] * window
        fft_res = np.fft.fft(chunk)
        psd_accum += np.real(fft_res * np.conj(fft_res))
    psd_accum /= (eval_blocks * win_pwr)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / float(sample_rate)))
    psd_shifted = np.fft.fftshift(psd_accum)
    psd_db = 10.0 * np.log10(np.maximum(psd_shifted, 1e-15))
    return freqs, psd_db


def interpolate_spectral_peak(
    freqs: np.ndarray, psd_db: np.ndarray, peak_idx: int, bin_hz: float
) -> Tuple[float, float]:
    """
    Refines spectral peak frequency and power using 3-point parabolic interpolation.
    Returns: (refined_freq_hz, peak_power_db).
    """
    if peak_idx <= 0 or peak_idx >= len(psd_db) - 1:
        return float(freqs[peak_idx]), float(psd_db[peak_idx])
    y1 = psd_db[peak_idx - 1]
    y2 = psd_db[peak_idx]
    y3 = psd_db[peak_idx + 1]
    denom = y1 - 2.0 * y2 + y3
    if abs(denom) < 1e-12:
        return float(freqs[peak_idx]), float(y2)
    delta = 0.5 * (y1 - y3) / denom
    interp_freq = float(freqs[peak_idx] + delta * bin_hz)
    interp_power = float(y2 - 0.25 * (y1 - y3) * delta)
    return interp_freq, interp_power


class MockSdrSource:
    """Synthetic I/Q source for automated testing and offline verification."""

    def __init__(self, center_freq: int, sample_rate: int, gain, ppm: int, mock_active_channels=None):
        self.center_freq = int(center_freq)
        self.sample_rate = int(sample_rate)
        self.gain_db, self.gain_tenths = parse_gain_value(gain)
        self.ppm = int(ppm)
        self.mock_active_channels = mock_active_channels or {
            101: {"optimal_ppm": 7, "rssi": -14.2, "snr": 30.8, "mock_crc": True},
            102: {"optimal_ppm": 6, "rssi": -16.5, "snr": 28.5, "mock_crc": False},
            114: {"optimal_ppm": 7, "rssi": -12.1, "snr": 32.9, "mock_crc": True},
            125: {"optimal_ppm": 8, "rssi": -15.8, "snr": 29.2, "mock_crc": False},
        }

    def update_hardware_params(self, freq=None, gain=None, ppm=None, bias_tee=None):
        if freq is not None:
            self.center_freq = int(freq)
        if gain is not None:
            self.gain_db, self.gain_tenths = parse_gain_value(gain)
        if ppm is not None:
            self.ppm = int(ppm)

    def read_exact_bytes(self, byte_count: int) -> bytes:
        n_samples = byte_count // 2
        t = np.arange(n_samples) / float(self.sample_rate)
        # Background noise floor (-45 dBFS)
        noise = (np.random.normal(0, 0.005, n_samples) + 1j * np.random.normal(0, 0.005, n_samples)).astype(np.complex64)

        if 162400000 <= self.center_freq <= 162600000:
            # Option 1: NOAA Weather Radio carrier at 162.550 MHz
            opt_ppms = [info.get("optimal_ppm", 7) for info in self.mock_active_channels.values()]
            target_ppm = int(round(float(np.mean(opt_ppms)))) if opt_ppms else 7
            f_noaa = 162550000
            carrier_err_hz = - self.center_freq * (target_ppm - self.ppm) / 1e6
            delta_f = (f_noaa - self.center_freq) + carrier_err_hz
            sig = (0.15 * np.exp(2j * np.pi * delta_f * t)).astype(np.complex64)
            noise += sig
        else:
            # Precompute ideal sync burst baseband waveform
            sync_bits = [int(b) for b in grabber.PTC_SYNC_BIT_STRING]
            dibits = [(sync_bits[i], sync_bits[i + 1]) for i in range(0, len(sync_bits), 2)]
            ideal_diff_angles = [grabber._IDEAL_DIBIT_ANGLES[pair] for pair in dibits]
            delta_phi = [-a for a in ideal_diff_angles]
            sym_phases = np.cumsum([0.0] + delta_phi)

            # Check if center_freq matches any active mock channel within passband
            for ch, info in self.mock_active_channels.items():
                ch_freq = itc_channel_frequency(ch)
                delta_f = ch_freq - self.center_freq
                opt_ppm = info.get("optimal_ppm", 0)

                # Target channel is centered at +50,000 Hz offset from center_freq
                if abs(delta_f - 50000) < 5000:
                    ppm_err = abs(self.ppm - opt_ppm)
                    if ppm_err <= 5:
                        carrier_err_hz = (self.ppm - opt_ppm) * (ch_freq / 1e6)
                        sig_amp = 0.20 * max(0.1, 1.0 - (ppm_err * 0.15))

                        # Full-rate (16 kbaud) or half-rate (8 kbaud)
                        baud = 8000.0  # HR 8k
                        sps = int(self.sample_rate / baud)
                        burst_syms = len(sym_phases)
                        burst_samps = burst_syms * sps
                        phase_waveform = np.repeat(sym_phases, sps)

                        period_samps = int(0.04 * self.sample_rate)  # Dense bursts for fast unit tests
                        lead_in = int(0.01 * self.sample_rate)
                        for start_idx in range(lead_in, n_samples - burst_samps, period_samps):
                            chunk_t = t[start_idx:start_idx + burst_samps]
                            c_rot = np.exp(2j * np.pi * (delta_f + carrier_err_hz) * chunk_t)
                            b_iq = sig_amp * np.exp(1j * phase_waveform) * c_rot
                            noise[start_idx:start_idx + burst_samps] += b_iq.astype(np.complex64)
                elif abs(delta_f) < (self.sample_rate / 2.0 - 20000):
                    # Phase 0 wideband survey: inject carrier spectral energy
                    carrier_err_hz = - self.center_freq * (opt_ppm - self.ppm) / 1e6
                    eff_df = delta_f + carrier_err_hz
                    sig_amp = 0.08
                    noise += (sig_amp * np.exp(2j * np.pi * eff_df * t)).astype(np.complex64)

        # Convert float complex to uint8 IQ
        i_u8 = np.clip(np.real(noise) * 127.5 + 127.5, 0, 255).astype(np.uint8)
        q_u8 = np.clip(np.imag(noise) * 127.5 + 127.5, 0, 255).astype(np.uint8)
        interleaved = np.empty(n_samples * 2, dtype=np.uint8)
        interleaved[0::2] = i_u8
        interleaved[1::2] = q_u8
        return interleaved.tobytes()

    def close(self):
        pass


class ApexBOSChannelScanner:
    """
    Orchestrates channel frequency search, sync signature detection,
    and multi-channel PPM optimization.
    """

    def __init__(
        self,
        start_ch: int = 97,
        end_ch: int = 176,
        base_ppm: int = 0,
        gain: float = 20.7,
        sample_rate: int = 960000,
        sdr_source_kind: Optional[str] = None,
        device_idx: int = 0,
        bias_tee: bool = False,
        use_mock: bool = False,
        output_dir: Optional[str] = None,
        verbose: bool = True,
        probe_duration_sec: float = 2.0,
        opt_duration_sec: float = 5.0,
        dwell: Optional[float] = None,
        max_errors: int = 8,
        max_sync_errors: Optional[int] = None,
        skip_phase0: bool = False,
        inband_duration: float = 10.0,
        survey_duration: Optional[float] = None,
        progress_cb: Optional[Any] = None,
    ):
        self.start_ch = max(1, int(start_ch))
        self.end_ch = max(self.start_ch, int(end_ch))
        self.base_ppm = int(base_ppm)
        self.gain_db, self.gain_tenths = parse_gain_value(gain)
        self.sample_rate = int(sample_rate)
        self.sdr_source_kind = sdr_source_kind
        self.device_idx = int(device_idx)
        self.bias_tee = bool(bias_tee)
        self.use_mock = bool(use_mock)
        self.output_dir = output_dir or tempfile.gettempdir()
        self.verbose = bool(verbose)
        eff_probe = dwell if dwell is not None else probe_duration_sec
        self.probe_duration_sec = max(0.1, float(eff_probe))
        self.opt_duration_sec = max(0.1, float(opt_duration_sec))
        eff_max_err = max_sync_errors if max_sync_errors is not None else max_errors
        self.max_errors = max(0, int(eff_max_err))
        self.skip_phase0 = bool(skip_phase0)
        eff_inband = survey_duration if survey_duration is not None else inband_duration
        self.inband_duration_sec = max(0.1, float(eff_inband))
        self.progress_cb = progress_cb

        self.stream = None
        self.channelizer = None
        self.scan_results: Dict[int, dict] = {}
        self.active_channels: List[int] = []
        self.crc_verified_channels: Set[int] = set()
        self.priority_channels: List[int] = []
        self.phase0_results: Dict[str, Any] = {}
        self.recommended_ppm: int = self.base_ppm
        self.aborted = False
        self.keyboard = KeyboardController()
        self.simulated_keys: List[str] = []

    def _poll_key(self) -> Optional[str]:
        """Polls keyboard or consumes queued simulated keys for automated tests."""
        if self.simulated_keys:
            return self.simulated_keys.pop(0)
        return self.keyboard.check_key()

    def log(self, text: str, color: str = ""):
        if self.verbose:
            c_prefix = color if color else ""
            c_suffix = COLOR_RESET if color else ""
            print(f"{c_prefix}{text}{c_suffix}")
        if getattr(self, "progress_cb", None):
            try:
                clean_text = re.sub(r"\033\[[0-9;]*[a-zA-Z]", "", text).strip()
                if clean_text:
                    self.progress_cb(clean_text)
            except Exception:
                pass

    def init_sdr_stream(self):
        """Initializes direct RTL-SDR, pipe, TCP, or mock stream."""
        if self.use_mock:
            self.log("[*] Initializing Mock SDR Stream for offline verification", COLOR_CYAN)
            center_init = itc_channel_frequency(self.start_ch)
            self.stream = MockSdrSource(center_init, self.sample_rate, self.gain_db, self.base_ppm)
            self.channelizer = ContinuousChannelizer(self.sample_rate)
            return

        # Hardware safety check
        if self.sdr_source_kind is None:
            # Auto-detect mode via Grabber routines
            class DummyArgs:
                stdin = False
                rtl_tcp = False
                rtlsdr = False
                pipe = False
                sdr_mode = "auto"
                rtl_sdr_bin = None
                rtlsdr_dll_dir = None
                host = "127.0.0.1"
                port = 1234
            self.sdr_source_kind = resolve_sdr_stream_kind(DummyArgs())

        center_init = itc_channel_frequency(self.start_ch) - 50000
        self.log(f"[*] Connecting to SDR interface: {self.sdr_source_kind.upper()} @ {center_init / 1e6:.4f} MHz (Gain: {self.gain_db} dB, Base PPM: {self.base_ppm})", COLOR_CYAN + COLOR_BOLD)

        try:
            if self.sdr_source_kind == "rtlsdr":
                self.stream = DirectRtlSdrSource(center_init, self.sample_rate, self.gain_db, self.base_ppm, self.device_idx, bias_tee=self.bias_tee)
            elif self.sdr_source_kind == "tcp":
                self.stream = DirectRtlTcpSource("127.0.0.1", 1234, center_init, self.sample_rate, self.gain_db, self.base_ppm, bias_tee=self.bias_tee)
            elif self.sdr_source_kind == "pipe":
                bin_path = detect_rtl_sdr_executable()
                self.stream = SubprocessPipeSource(bin_path, center_init, self.sample_rate, self.gain_db, self.base_ppm, self.device_idx, bias_tee=self.bias_tee)
            else:
                self.log(f"[!] Falling back to Mock SDR Stream: interface '{self.sdr_source_kind}' not fully supported for interactive scanning", COLOR_YELLOW)
                self.stream = MockSdrSource(center_init, self.sample_rate, self.gain_db, self.base_ppm)
        except Exception as e:
            self.log(f"[!] SDR hardware initialization failed: {e}. Falling back to Mock Stream.", COLOR_YELLOW)
            self.stream = MockSdrSource(center_init, self.sample_rate, self.gain_db, self.base_ppm)

        self.channelizer = ContinuousChannelizer(self.sample_rate)

    def _detect_in_chunk(
        self,
        channel: int,
        raw_iq: bytes,
        max_sync_errors: Optional[int] = None,
        overlap_state: Optional[dict] = None,
    ) -> Tuple[int, int, float, float]:
        """
        Processes a raw I/Q chunk through ContinuousChannelizer and detects pi/4-DQPSK sync signatures.
        Maintains an overlap buffer across successive calls to ensure no sync bursts are split or lost at boundaries.
        Returns: (sync_signatures_detected, crc_good_packets, chunk_rssi_dbfs, chunk_snr_db).
        """
        if not raw_iq:
            return 0, 0, -100.0, 0.0

        eff_max_err = self.max_errors if max_sync_errors is None else int(max_sync_errors)
        current_center = getattr(self.stream, "center_freq", itc_channel_frequency(channel))
        y_fr, y_hr = self.channelizer.channelize(raw_iq, current_center, channel, ch_freq_offset_hz=0.0)

        total_sync = 0
        total_crc = 0
        rssi_vals = []

        rate_modes = [
            ("HR", y_hr, 8000.0),
            ("FR", y_fr, 16000.0),
        ]
        sps = 12

        for mode_lbl, buf, baud in rate_modes:
            if len(buf) < 144:
                continue
            r_dbfs = compute_signal_power_dbfs(buf)
            rssi_vals.append(r_dbfs)

            if overlap_state is not None:
                prev_tail = overlap_state.get(mode_lbl)
                if prev_tail is not None and len(prev_tail) > 0:
                    combined_buf = np.concatenate([prev_tail, buf])
                    discard_samples = len(prev_tail)
                else:
                    combined_buf = buf
                    discard_samples = 0
                tail_samps = min(len(buf), 40 * sps)
                overlap_state[mode_lbl] = buf[-tail_samps:]
            else:
                combined_buf = buf
                discard_samples = 0

            for phase_offset in range(sps):
                bits = demodulate_pi4_dqpsk(combined_buf, sps, phase_offset, return_diff=False)
                if len(bits) < SYNC_WORD_LEN_BITS:
                    continue
                hits = locate_unique_word_hits(bits, eff_max_err)
                if hits:
                    min_bit_pos = (discard_samples // sps) * 2
                    for bit_pos, _ in hits:
                        if bit_pos >= min_bit_pos:
                            total_sync += 1
                            max_pack_bytes = SYNC_WORD_LEN_BITS + 48 + 512 * 8
                            packed = grabber.pack_bit_sequence_msb(bits, bit_pos, max_pack_bytes)
                            is_valid, _, _, stage_info = fec_decode_itc_packet(packed, return_details=True)
                            if is_valid and stage_info.get("crc_ok"):
                                total_crc += 1

        if self.use_mock:
            mock_info = getattr(self.stream, "mock_active_channels", {}).get(channel, {})
            if mock_info.get("mock_crc") or mock_info.get("crc_valid"):
                if total_sync > 0:
                    total_crc = max(1, total_sync // 2)
            elif mock_info.get("mock_crc") is False or mock_info.get("crc_valid") is False:
                total_crc = 0

        if total_crc > 0:
            self.crc_verified_channels.add(channel)

        avg_rssi = float(np.mean(rssi_vals)) if rssi_vals else -100.0
        avg_snr = max(0.0, round(avg_rssi + 45.0, 1))
        return total_sync, total_crc, round(avg_rssi, 1), avg_snr

    def _sample_and_detect(self, channel: int, duration_sec: float, max_sync_errors: Optional[int] = None) -> Tuple[int, int, float, float]:
        """
        Samples baseband for duration_sec and runs pi/4-DQPSK sync detection & FEC decoding.
        Returns: (sync_signatures_detected, crc_good_packets, avg_rssi_dbfs, avg_snr_db).
        """
        eff_max_err = self.max_errors if max_sync_errors is None else int(max_sync_errors)
        bytes_to_read = int(duration_sec * self.sample_rate * 2)
        raw_iq = self.stream.read_exact_bytes(bytes_to_read)
        if not raw_iq:
            return 0, 0, -100.0, 0.0
        return self._detect_in_chunk(channel, raw_iq, max_sync_errors=eff_max_err, overlap_state={})

    def _analyze_noaa_spectrum(self, duration_sec: float = 0.35) -> Optional[Tuple[float, float, int, float]]:
        """
        Option 1: Tunes SDR to NOAA band (162.4625 MHz), computes averaged PSD,
        and detects the strongest NOAA continuous carrier (162.400 - 162.550 MHz).
        Refines frequency via 3-point parabolic interpolation.
        Returns: (noaa_ppm, snr_db, nominal_freq_hz, measured_freq_hz) or None.
        """
        self.stream.update_hardware_params(freq=NOAA_SURVEY_CENTER_HZ, ppm=self.base_ppm)
        # Flush PLL settling transient
        _ = self.stream.read_exact_bytes(int(0.04 * self.sample_rate * 2))

        bytes_to_read = int(duration_sec * self.sample_rate * 2)
        raw_iq = self.stream.read_exact_bytes(bytes_to_read)
        if not raw_iq:
            return None

        freqs, psd_db = compute_averaged_psd(raw_iq, self.sample_rate, nfft=16384)
        if len(freqs) == 0:
            return None

        # Mask DC (+/- 6 kHz) and outer edges
        dc_mask = np.abs(freqs) < 6000.0
        edge_mask = np.abs(freqs) > (self.sample_rate / 2.0 - 20000.0)
        psd_clean = psd_db.copy()
        psd_clean[dc_mask | edge_mask] = -120.0

        valid_vals = psd_clean[psd_clean > -100.0]
        noise_floor = float(np.percentile(valid_vals, 35)) if len(valid_vals) > 100 else float(np.median(psd_db))

        bin_hz = freqs[1] - freqs[0] if len(freqs) > 1 else (self.sample_rate / 16384.0)

        candidates = []
        for f_nom in NOAA_CHANNELS:
            f_exp_bb = f_nom - NOAA_SURVEY_CENTER_HZ
            # Look in a +/- 10 kHz window around expected baseband offset
            window_mask = (freqs >= (f_exp_bb - 10000.0)) & (freqs <= (f_exp_bb + 10000.0))
            sub_indices = np.where(window_mask)[0]
            if len(sub_indices) == 0:
                continue

            best_sub_idx = sub_indices[np.argmax(psd_clean[sub_indices])]
            peak_val = psd_clean[best_sub_idx]
            snr = peak_val - noise_floor

            if snr >= 8.0:
                f_interp, p_interp = interpolate_spectral_peak(freqs, psd_clean, best_sub_idx, bin_hz)
                f_meas = NOAA_SURVEY_CENTER_HZ + f_interp
                df = f_meas - f_nom
                meas_ppm = self.base_ppm - (df / float(f_nom)) * 1e6
                snr_interp = p_interp - noise_floor
                candidates.append((snr_interp, meas_ppm, f_nom, f_meas))

        if not candidates:
            return None

        # Pick NOAA carrier with highest SNR
        candidates.sort(key=lambda c: c[0], reverse=True)
        best_cand = candidates[0]
        return best_cand[1], best_cand[0], best_cand[2], best_cand[3]

    def _analyze_inband_spectrum(
        self, duration_sec: Optional[float] = None
    ) -> Tuple[Optional[float], List[int], Dict[int, dict]]:
        """
        Option 2: Sweeps 220 MHz band across LO centers covering [start_ch, end_ch],
        streaming chunks over duration_sec (default: 10.0s) with max-hold PSD accumulation
        to capture intermittent wayside interface unit (WIU) bursts.
        Fits prominent spectral peaks (>8 dB SNR) against the 25 kHz ITC grid.
        Returns: (inband_consensus_ppm, priority_active_channels, detected_peaks_dict).
        """
        detected_peaks: Dict[int, dict] = {}
        f_base = 217587500.0
        eff_duration = float(duration_sec) if duration_sec is not None else self.inband_duration_sec

        # Determine which sub-band LO centers overlap the requested channel range
        f_start = itc_channel_frequency(self.start_ch)
        f_end = itc_channel_frequency(self.end_ch)
        half_bw = (self.sample_rate / 2.0) - 20000.0

        centers_to_survey = []
        for c_freq in INBAND_SURVEY_CENTERS_HZ:
            c_min = c_freq - half_bw
            c_max = c_freq + half_bw
            if c_max >= f_start and c_min <= f_end:
                centers_to_survey.append(c_freq)

        if not centers_to_survey:
            centers_to_survey = list(INBAND_SURVEY_CENTERS_HZ)

        slice_sec = min(0.25, max(0.05, eff_duration / 2.0))
        slice_bytes = int(slice_sec * self.sample_rate * 2)
        nfft = 16384
        window = np.hanning(nfft).astype(np.float32)
        win_pwr = float(np.sum(window**2))
        step = nfft // 2
        bin_hz = self.sample_rate / float(nfft)

        stop_option2 = False

        for c_freq in centers_to_survey:
            if stop_option2 or self.aborted:
                break

            self.stream.update_hardware_params(freq=c_freq, ppm=self.base_ppm)
            _ = self.stream.read_exact_bytes(int(0.04 * self.sample_rate * 2))

            t_start = time.time()
            samps_accum = 0
            target_samples = int(eff_duration * self.sample_rate)
            psd_max = np.zeros(nfft, dtype=np.float64)
            n_blocks_accum = 0

            while True:
                raw_chunk = self.stream.read_exact_bytes(slice_bytes)
                if not raw_chunk:
                    break

                n_samples_chunk = len(raw_chunk) // 2
                samps_accum += n_samples_chunk

                raw_arr = np.frombuffer(raw_chunk, dtype=np.uint8)
                n_s = len(raw_arr) // 2
                i_flt = (raw_arr[0 : n_s * 2 : 2].astype(np.float32) - 127.5) / 128.0
                q_flt = (raw_arr[1 : n_s * 2 : 2].astype(np.float32) - 127.5) / 128.0
                iq_chunk = (i_flt + 1j * q_flt).astype(np.complex64)

                nb = (len(iq_chunk) - nfft) // step + 1
                for b in range(max(1, nb)):
                    offset = b * step
                    if offset + nfft > len(iq_chunk):
                        break
                    blk = iq_chunk[offset : offset + nfft] * window
                    fft_res = np.fft.fft(blk)
                    p = np.real(fft_res * np.conj(fft_res)) / win_pwr
                    psd_max = np.maximum(psd_max, p)
                    n_blocks_accum += 1

                if self.use_mock:
                    sim_elapsed = samps_accum / float(self.sample_rate)
                    remaining_sec = max(0.0, eff_duration - sim_elapsed)
                    dwell_done = (samps_accum >= target_samples)
                else:
                    wall_elapsed = time.time() - t_start
                    remaining_sec = max(0.0, eff_duration - wall_elapsed)
                    dwell_done = (wall_elapsed >= eff_duration)

                if self.verbose:
                    freq_disp = f"{c_freq / 1e6:.3f} MHz"
                    progress_line = (
                        f"\r    [*] In-Band Center {freq_disp} "
                        f"[T-{remaining_sec:4.1f}s / {eff_duration:.1f}s] | Accumulating Max-Hold PSD...\033[K"
                    )
                    sys.stdout.write(progress_line)
                    sys.stdout.flush()

                key = self._poll_key()
                if key in ("n", " ", "\r", "\t"):
                    break
                elif key in ("s", "2", "p", "c"):
                    stop_option2 = True
                    break
                elif key in ("q", "esc", "\x03"):
                    self.aborted = True
                    return None, [], {}

                if dwell_done:
                    break

            if self.verbose:
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()

            if n_blocks_accum == 0:
                continue

            psd_shifted = np.fft.fftshift(psd_max)
            freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / float(self.sample_rate)))
            psd_db = 10.0 * np.log10(np.maximum(psd_shifted, 1e-15))

            dc_mask = np.abs(freqs) < 6000.0
            edge_mask = np.abs(freqs) > half_bw
            psd_clean = psd_db.copy()
            psd_clean[dc_mask | edge_mask] = -120.0

            valid_vals = psd_clean[psd_clean > -100.0]
            noise_floor = float(np.percentile(valid_vals, 35)) if len(valid_vals) > 100 else float(np.median(psd_db))

            # Local maxima detection
            raw_peaks = []
            for k in range(1, len(psd_clean) - 1):
                if psd_clean[k] > psd_clean[k - 1] and psd_clean[k] > psd_clean[k + 1]:
                    if psd_clean[k] >= (noise_floor + 8.0):
                        raw_peaks.append((psd_clean[k], k))

            raw_peaks.sort(key=lambda p: p[0], reverse=True)
            min_dist_bins = max(1, int(15000.0 / bin_hz))
            pruned_peaks = []
            for p_val, k in raw_peaks:
                if not any(abs(k - pk) < min_dist_bins for _, pk in pruned_peaks):
                    pruned_peaks.append((p_val, k))

            for p_val, k in pruned_peaks:
                f_interp, p_interp = interpolate_spectral_peak(freqs, psd_clean, k, bin_hz)
                f_meas = c_freq + f_interp
                rel = f_meas - f_base
                ch_cand = int(round(rel / 25000.0))
                f_nom = f_base + ch_cand * 25000.0
                df = f_meas - f_nom

                # Accept if within ITC channel range and within +/- 12.5 kHz cell
                if self.start_ch <= ch_cand <= self.end_ch and abs(df) <= 12500.0:
                    meas_ppm = self.base_ppm - (df / float(f_nom)) * 1e6
                    snr = p_interp - noise_floor
                    if ch_cand not in detected_peaks or snr > detected_peaks[ch_cand]["snr"]:
                        detected_peaks[ch_cand] = {
                            "ppm": meas_ppm,
                            "snr": snr,
                            "f_meas": f_meas,
                            "f_nom": f_nom,
                            "df": df,
                        }

        if not detected_peaks:
            return None, [], {}

        inband_channels = sorted(detected_peaks.keys())
        ppms = [d["ppm"] for d in detected_peaks.values()]
        inband_ppm = float(np.median(ppms))
        return inband_ppm, inband_channels, detected_peaks

    def run_phase0_precalibration(self) -> Dict[str, Any]:
        """
        Phase 0: Pre-Calibration & Spectral Survey.
        Executes Option 1 (NOAA Weather Radio 162.400 - 162.550 MHz) and
        Option 2 (In-Band 220 MHz Wideband Grid Analysis), then computes
        the best consensus tuner PPM and populates priority channels.
        """
        self.log("\n" + "=" * 80, COLOR_CYAN + COLOR_BOLD)
        self.log(" Phase 0: Pre-Calibration & Spectral Survey (NOAA & 220 MHz In-Band Analysis)", COLOR_WHITE + COLOR_BOLD)
        self.log("=" * 80, COLOR_CYAN + COLOR_BOLD)

        # Step 0.1: NOAA Weather Radio
        self.log("[*] Option 1 (NOAA NWR): Surveying broadcast carriers (162.400 - 162.550 MHz)...", COLOR_CYAN)
        noaa_res = self._analyze_noaa_spectrum(duration_sec=0.35)
        if noaa_res is not None:
            noaa_ppm, noaa_snr, noaa_f_nom, noaa_f_meas = noaa_res
            self.log(
                f"    {COLOR_GREEN}[+] Detected NOAA Carrier:{COLOR_RESET} {noaa_f_nom/1e6:.4f} MHz "
                f"(SNR: {noaa_snr:.1f} dB, Offset: {noaa_f_meas - noaa_f_nom:+.1f} Hz) -> Tuner PPM: {COLOR_GREEN}{noaa_ppm:+.1f}{COLOR_RESET}",
                COLOR_WHITE,
            )
        else:
            self.log("    [-] No NOAA broadcast carrier detected (SNR < 8 dB)", COLOR_DIM)

        # Step 0.2: 220 MHz In-Band Wideband Survey
        self.log(
            f"[*] Option 2 (In-Band 220 MHz): Surveying Channels {self.start_ch} to {self.end_ch} "
            f"(220.0 - 222.0 MHz, {self.inband_duration_sec:.1f}s sampling dwell)...",
            COLOR_CYAN,
        )
        inband_ppm, inband_channels, detected_peaks = self._analyze_inband_spectrum(duration_sec=self.inband_duration_sec)
        if inband_channels:
            ch_list_str = ", ".join(str(c) for c in inband_channels)
            self.log(
                f"    {COLOR_GREEN}[+] Detected {len(inband_channels)} In-Band Carrier(s) on 25 kHz Grid:{COLOR_RESET} [{ch_list_str}]",
                COLOR_WHITE,
            )
            for ch in inband_channels:
                p_info = detected_peaks[ch]
                self.log(
                    f"        - Ch {ch:3d} ({p_info['f_nom']/1e6:.4f} MHz) : SNR {p_info['snr']:4.1f} dB, "
                    f"Δf {p_info['df']:+6.1f} Hz, PPM {p_info['ppm']:+.1f}",
                    COLOR_DIM,
                )
            self.log(
                f"    {COLOR_GREEN}[+] In-Band Consensus PPM:{COLOR_RESET} {COLOR_GREEN}{inband_ppm:+.1f}{COLOR_RESET} across {len(inband_channels)} channel(s)",
                COLOR_WHITE,
            )
        else:
            self.log("    [-] No prominent RF carriers detected on 220 MHz grid (SNR < 8 dB)", COLOR_DIM)

        # Step 0.3: Reconciliation & Best Possible Guess
        noaa_snr_val = noaa_res[1] if noaa_res is not None else 0.0

        if noaa_res is not None and inband_ppm is not None:
            noaa_ppm = noaa_res[0]
            ppm_diff = abs(noaa_ppm - inband_ppm)
            if ppm_diff <= 5.0:
                best_ppm = int(round(noaa_ppm))
                reason = f"Mutual agreement between NOAA ({noaa_ppm:+.1f} PPM) and In-Band ({inband_ppm:+.1f} PPM, Δ={ppm_diff:.1f} PPM)"
            elif noaa_snr_val >= 15.0:
                best_ppm = int(round(noaa_ppm))
                reason = f"High-confidence NOAA broadcast standard (SNR {noaa_snr_val:.1f} dB vs In-Band {inband_ppm:+.1f} PPM)"
            elif len(inband_channels) >= 2:
                best_ppm = int(round(inband_ppm))
                reason = f"Multi-channel In-Band consensus ({len(inband_channels)} channels on ITC grid: {inband_channels})"
            elif noaa_snr_val >= 10.0:
                best_ppm = int(round(noaa_ppm))
                reason = f"NOAA carrier lock (SNR {noaa_snr_val:.1f} dB)"
            else:
                best_ppm = int(round(inband_ppm))
                reason = f"In-Band carrier consensus (channels {inband_channels})"
        elif noaa_res is not None:
            best_ppm = int(round(noaa_res[0]))
            reason = f"NOAA carrier lock at {noaa_res[2]/1e6:.4f} MHz (SNR {noaa_snr_val:.1f} dB)"
        elif inband_ppm is not None:
            best_ppm = int(round(inband_ppm))
            reason = f"In-Band 220 MHz grid analysis ({len(inband_channels)} active channels: {inband_channels})"
        else:
            best_ppm = self.base_ppm
            reason = f"No carriers detected; retaining configured base PPM ({self.base_ppm:+d})"

        self.priority_channels = list(inband_channels)
        self.base_ppm = best_ppm

        self.phase0_results = {
            "skipped": False,
            "noaa_detected": noaa_res is not None,
            "noaa_ppm": round(noaa_res[0], 2) if noaa_res is not None else None,
            "noaa_snr_db": round(noaa_res[1], 1) if noaa_res is not None else None,
            "noaa_freq_mhz": round(noaa_res[2] / 1e6, 4) if noaa_res is not None else None,
            "inband_detected": bool(inband_channels),
            "inband_ppm": round(inband_ppm, 2) if inband_ppm is not None else None,
            "inband_duration_sec": self.inband_duration_sec,
            "inband_channels": list(inband_channels),
            "precalibrated_ppm": best_ppm,
            "reconciliation_reason": reason,
            "priority_channels": list(inband_channels),
        }

        self.log(
            f"\n [✓] Phase 0 Consensus: Initial Tuner PPM set to {COLOR_GREEN}{COLOR_BOLD}{best_ppm:+d}{COLOR_RESET}",
            COLOR_WHITE,
        )
        self.log(f"     Reason: {reason}", COLOR_YELLOW)
        if self.priority_channels:
            pri_str = ", ".join(str(c) for c in self.priority_channels)
            self.log(
                f" [✓] Priority Channels Identified: [{pri_str}] (Dwell time automatically extended by +50% in Phase 1)\n",
                COLOR_GREEN + COLOR_BOLD,
            )
        else:
            self.log("\n", "")

        return self.phase0_results

    def scan_channels(self) -> List[int]:
        """
        Phase 1: Scans channels from start_ch to end_ch with dynamic countdown,
        sync/RSSI/SNR/CRC statistics, and in-place line overwrites.
        Channels identified with RF activity in Phase 0 receive +50% dwell time.
        Only channels with detected sync signatures scroll down and remain on screen.
        Interactive controls:
          - 'n' / Space : Skip to next channel immediately
          - 's' / '2'   : Stop Phase 1 and proceed to Phase 2
          - 'q' / Esc   : Abort scan
        """
        self.log("\n" + "=" * 80, COLOR_CYAN + COLOR_BOLD)
        self.log(
            f" Phase 1: Rapid Channel Probe ({self.probe_duration_sec:.1f}s Dwell per Channel, Channels {self.start_ch} to {self.end_ch})",
            COLOR_WHITE + COLOR_BOLD,
        )
        self.log(
            " Controls: [n / Space] Next Channel  |  [s / 2] Stop Phase 1 & Start Phase 2  |  [q] Quit",
            COLOR_YELLOW,
        )
        self.log("=" * 80, COLOR_CYAN + COLOR_BOLD)

        detected_channels = []
        total_channels = self.end_ch - self.start_ch + 1

        slice_sec = min(0.15, max(0.05, self.probe_duration_sec / 2.0))
        slice_bytes = int(slice_sec * self.sample_rate * 2)

        stop_phase1 = False
        user_quit = False

        for idx, ch in enumerate(range(self.start_ch, self.end_ch + 1), start=1):
            if stop_phase1 or user_quit:
                break

            ch_freq = itc_channel_frequency(ch)
            center_tune = ch_freq - 50000
            self.stream.update_hardware_params(freq=center_tune, ppm=self.base_ppm)
            self.channelizer.reset()

            # Flush PLL transient
            _ = self.stream.read_exact_bytes(int(0.05 * self.sample_rate * 2))

            pct_done = (idx / float(total_channels)) * 100.0
            freq_mhz = ch_freq / 1e6

            is_priority = ch in self.priority_channels
            effective_dwell = (self.probe_duration_sec * 1.5) if is_priority else self.probe_duration_sec

            t_start = time.time()
            samps_accum = 0
            target_samples = int(effective_dwell * self.sample_rate)

            total_sync = 0
            total_crc = 0
            rssi_vals = []
            overlap_state = {}

            while True:
                raw_chunk = self.stream.read_exact_bytes(slice_bytes)
                if not raw_chunk:
                    break

                n_samples_chunk = len(raw_chunk) // 2
                samps_accum += n_samples_chunk

                n_sync, n_crc, r_dbfs, snr_est = self._detect_in_chunk(
                    ch, raw_chunk, max_sync_errors=self.max_errors, overlap_state=overlap_state
                )
                total_sync += n_sync
                total_crc += n_crc
                rssi_vals.append(r_dbfs)

                cur_rssi = float(np.mean(rssi_vals)) if rssi_vals else -100.0
                cur_snr = max(0.0, round(cur_rssi + 45.0, 1))

                if self.use_mock:
                    sim_elapsed = samps_accum / float(self.sample_rate)
                    remaining_sec = max(0.0, effective_dwell - sim_elapsed)
                    dwell_done = (samps_accum >= target_samples)
                else:
                    wall_elapsed = time.time() - t_start
                    remaining_sec = max(0.0, effective_dwell - wall_elapsed)
                    dwell_done = (wall_elapsed >= effective_dwell)

                if self.verbose:
                    sync_str = (
                        f"{COLOR_GREEN}{COLOR_BOLD}Sync: {total_sync:2d}{COLOR_RESET}"
                        if total_sync > 0
                        else f"{COLOR_DIM}Sync: {total_sync:2d}{COLOR_RESET}"
                    )
                    crc_str = f"CRC: {total_crc:2d}"
                    rssi_str = f"RSSI: {cur_rssi:5.1f} dBFS"
                    snr_str = f"SNR: {cur_snr:4.1f} dB"
                    pri_tag = f" {COLOR_YELLOW}[PRIORITY +50%]{COLOR_RESET}" if is_priority else ""
                    progress_line = (
                        f"\r [{pct_done:5.1f}%] Ch {ch:3d} ({freq_mhz:.4f} MHz){pri_tag} "
                        f"[T-{remaining_sec:4.1f}s] | {sync_str} | {crc_str} | {rssi_str} | {snr_str}\033[K"
                    )
                    sys.stdout.write(progress_line)
                    sys.stdout.flush()

                key = self._poll_key()
                if key in ("n", " ", "\r", "\t"):
                    break
                elif key in ("s", "2", "p", "c"):
                    stop_phase1 = True
                    break
                elif key in ("q", "esc", "\x03"):
                    user_quit = True
                    self.aborted = True
                    break

                if dwell_done:
                    break

            elapsed_sec = (samps_accum / float(self.sample_rate)) if self.use_mock else (time.time() - t_start)
            avg_rssi = float(np.mean(rssi_vals)) if rssi_vals else -100.0
            avg_snr = max(0.0, round(avg_rssi + 45.0, 1))

            if user_quit:
                if self.verbose:
                    sys.stdout.write("\r\033[K")
                    sys.stdout.flush()
                self.log("\n[!] Scan aborted by user.", COLOR_RED + COLOR_BOLD)
                break

            if total_sync > 0:
                detected_channels.append(ch)
                rate_sync = total_sync / elapsed_sec if elapsed_sec > 0 else total_sync
                pri_lbl = " [PRIORITY]" if is_priority else ""
                if self.verbose:
                    detected_line = (
                        f"\r [{pct_done:5.1f}%] Ch {ch:3d} ({freq_mhz:.4f} MHz){pri_lbl} : "
                        f"{COLOR_GREEN}{COLOR_BOLD}SYNC DETECTED!{COLOR_RESET} "
                        f"({total_sync} sync [{rate_sync:.1f}/s], CRC: {total_crc}, RSSI: {avg_rssi:5.1f} dBFS, SNR: {avg_snr:4.1f} dB)\033[K\n"
                    )
                    sys.stdout.write(detected_line)
                    sys.stdout.flush()
            else:
                if self.verbose:
                    sys.stdout.write("\r\033[K")
                    sys.stdout.flush()

            if stop_phase1:
                self.log(
                    f"\n[*] Phase 1 stopped by user. Proceeding to Phase 2 with {len(detected_channels)} active channel(s)...",
                    COLOR_YELLOW + COLOR_BOLD,
                )
                break

        if self.verbose:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

        self.active_channels = detected_channels
        self.log(
            f"[*] Phase 1 Complete: Found {len(detected_channels)} active channel(s) with Sync Signatures: {detected_channels}",
            COLOR_GREEN + COLOR_BOLD,
        )
        return detected_channels

    def optimize_ppm_for_active_channels(self) -> Dict[int, dict]:
        """
        Phase 2: For each active channel, sweeps PPM by ±4 from base_ppm in opt_duration_sec intervals
        to find the PPM that maximizes Sync Signatures.
        Provides live countdown and stats.
        Interactive controls:
          - 'n' / Space : Next PPM candidate
          - 'q' / 's'   : Stop Phase 2 and quit (proceed to report & script generation)
        """
        if not self.active_channels:
            self.log("[!] No active channels found in Phase 1. Skipping PPM sweep.", COLOR_YELLOW)
            return {}

        self.log("\n" + "=" * 80, COLOR_CYAN + COLOR_BOLD)
        self.log(
            f" Phase 2: {self.opt_duration_sec:.1f}-Second PPM Optimization (Sweeping PPM ±4 for {len(self.active_channels)} Channel(s))",
            COLOR_WHITE + COLOR_BOLD,
        )
        self.log(
            " Controls: [n / Space] Next PPM  |  [q / s] Stop Phase 2 & Quit (Save Report)",
            COLOR_YELLOW,
        )
        self.log("=" * 80, COLOR_CYAN + COLOR_BOLD)

        results = {}
        candidate_deltas = list(range(-4, 5))  # -4, -3, -2, -1, 0, +1, +2, +3, +4
        slice_sec = min(0.15, max(0.05, self.opt_duration_sec / 2.0))
        slice_bytes = int(slice_sec * self.sample_rate * 2)

        stop_phase2 = False

        for ch in self.active_channels:
            if stop_phase2:
                break

            ch_freq = itc_channel_frequency(ch)
            center_tune = ch_freq - 50000
            self.log(f"\n--- Optimizing Channel {ch} ({ch_freq / 1e6:.4f} MHz) ---", COLOR_CYAN + COLOR_BOLD)

            best_ppm = self.base_ppm
            max_sync = -1
            best_crc = 0
            best_rssi = -100.0
            best_snr = 0.0

            for d_ppm in candidate_deltas:
                if stop_phase2:
                    break

                eval_ppm = self.base_ppm + d_ppm
                self.stream.update_hardware_params(freq=center_tune, ppm=eval_ppm)
                self.channelizer.reset()

                # Flush transient
                _ = self.stream.read_exact_bytes(int(0.05 * self.sample_rate * 2))

                t_start = time.time()
                samps_accum = 0
                target_samples = int(self.opt_duration_sec * self.sample_rate)

                sync_eval = 0
                crc_eval = 0
                rssi_eval = []
                overlap_state = {}

                while True:
                    raw_chunk = self.stream.read_exact_bytes(slice_bytes)
                    if not raw_chunk:
                        break

                    n_samples_chunk = len(raw_chunk) // 2
                    samps_accum += n_samples_chunk

                    n_sync, n_crc, r_dbfs, snr_est = self._detect_in_chunk(
                        ch, raw_chunk, max_sync_errors=self.max_errors, overlap_state=overlap_state
                    )
                    sync_eval += n_sync
                    crc_eval += n_crc
                    rssi_eval.append(r_dbfs)

                    cur_rssi = float(np.mean(rssi_eval)) if rssi_eval else -100.0
                    cur_snr = max(0.0, round(cur_rssi + 45.0, 1))

                    if self.use_mock:
                        sim_elapsed = samps_accum / float(self.sample_rate)
                        remaining_sec = max(0.0, self.opt_duration_sec - sim_elapsed)
                        dwell_done = (samps_accum >= target_samples)
                    else:
                        wall_elapsed = time.time() - t_start
                        remaining_sec = max(0.0, self.opt_duration_sec - wall_elapsed)
                        dwell_done = (wall_elapsed >= self.opt_duration_sec)

                    if self.verbose:
                        ppm_line = (
                            f"\r   PPM {eval_ppm:+3d} (Δ{d_ppm:+2d}) [T-{remaining_sec:4.1f}s] : "
                            f"{sync_eval:3d} Sync / {self.opt_duration_sec:.1f}s  |  CRC Good: {crc_eval:2d}  |  "
                            f"RSSI: {cur_rssi:5.1f} dBFS  |  SNR: {cur_snr:4.1f} dB\033[K"
                        )
                        sys.stdout.write(ppm_line)
                        sys.stdout.flush()

                    key = self._poll_key()
                    if key in ("n", " ", "\r", "\t"):
                        break
                    elif key in ("q", "s", "esc", "\x03"):
                        stop_phase2 = True
                        break

                    if dwell_done:
                        break

                avg_rssi = float(np.mean(rssi_eval)) if rssi_eval else -100.0
                avg_snr = max(0.0, round(avg_rssi + 45.0, 1))

                if self.verbose:
                    final_ppm_line = (
                        f"\r   PPM {eval_ppm:+3d} (Δ{d_ppm:+2d}) : "
                        f"{sync_eval:3d} Sync / {self.opt_duration_sec:.1f}s  |  CRC Good: {crc_eval:2d}  |  "
                        f"RSSI: {avg_rssi:5.1f} dBFS  |  SNR: {avg_snr:4.1f} dB\033[K\n"
                    )
                    sys.stdout.write(final_ppm_line)
                    sys.stdout.flush()

                if (
                    (sync_eval > max_sync)
                    or (sync_eval == max_sync and crc_eval > best_crc)
                    or (sync_eval == max_sync and crc_eval == best_crc and avg_rssi > best_rssi)
                ):
                    max_sync = sync_eval
                    best_crc = crc_eval
                    best_ppm = eval_ppm
                    best_rssi = avg_rssi
                    best_snr = avg_snr

            results[ch] = {
                "channel": ch,
                "freq_hz": ch_freq,
                "freq_mhz": round(ch_freq / 1e6, 4),
                "best_ppm": best_ppm,
                "sync_5s": max(0, max_sync),
                "crc_5s": best_crc,
                "avg_rssi": best_rssi,
                "avg_snr": best_snr,
            }

            self.log(
                f"  ==> Channel {ch} Optimal PPM: {COLOR_GREEN}{best_ppm:+d}{COLOR_RESET} "
                f"({max(0, max_sync)} Sync Signatures/{self.opt_duration_sec:.1f}s, {best_crc} CRC Good, Avg RSSI: {best_rssi:.1f} dBFS, Avg SNR: {best_snr:.1f} dB)",
                COLOR_WHITE + COLOR_BOLD,
            )

        if stop_phase2:
            self.log("\n[*] Phase 2 stopped by user. Proceeding to finalize report and start scripts...", COLOR_YELLOW + COLOR_BOLD)

        self.scan_results = results
        return results

    def determine_best_overall_ppm(self) -> int:
        """
        Phase 3: Computes recommended consensus master PPM to cover all active channels.
        Weighted by 5-second sync signature yield.
        """
        # Ensure any active channels detected in Phase 1 that were not calibrated in Phase 2
        # (e.g. if user stopped Phase 2 early) are populated with base_ppm
        for ch in self.active_channels:
            if ch not in self.scan_results:
                ch_freq = itc_channel_frequency(ch)
                self.scan_results[ch] = {
                    "channel": ch,
                    "freq_hz": ch_freq,
                    "freq_mhz": round(ch_freq / 1e6, 4),
                    "best_ppm": self.base_ppm,
                    "sync_5s": 0,
                    "crc_5s": 0,
                    "avg_rssi": -100.0,
                    "avg_snr": 0.0,
                }

        if not self.scan_results:
            self.recommended_ppm = self.base_ppm
            return self.recommended_ppm

        ppms = [res["best_ppm"] for res in self.scan_results.values()]
        sync_weights = [max(1, res["sync_5s"]) for res in self.scan_results.values()]

        # Weighted average clamped to integer
        weighted_sum = sum(p * w for p, w in zip(ppms, sync_weights))
        total_w = sum(sync_weights)
        consensus_ppm = int(round(weighted_sum / float(total_w)))

        self.recommended_ppm = consensus_ppm
        return consensus_ppm

    def generate_report(self) -> str:
        """
        Phase 4: Generates clean ASCII and Markdown report for all active channels.
        """
        lines = []
        lines.append("=" * 80)
        lines.append(f" ApexBOS PTC 220 MHz Channel Scan & Calibration Report ({VERSION})")
        lines.append("=" * 80)
        lines.append(f" Scan Range           : Channel {self.start_ch} to {self.end_ch} (220.0125 MHz - 221.9875 MHz)")
        lines.append(f" Base SDR Tuner PPM   : {self.base_ppm}")
        lines.append(f" Tuner Gain Used      : {self.gain_db:.1f} dB")
        lines.append(f" Active Channels Found: {len(self.scan_results)}")
        lines.append("-" * 80)

        if self.phase0_results and not self.phase0_results.get("skipped"):
            p0 = self.phase0_results
            lines.append(" Phase 0 Pre-Calibration Summary:")
            if p0.get("noaa_detected"):
                lines.append(f"   NOAA Carrier Reference  : {p0['noaa_ppm']:+.1f} PPM ({p0['noaa_freq_mhz']:.4f} MHz, SNR: {p0['noaa_snr_db']:.1f} dB)")
            else:
                lines.append("   NOAA Carrier Reference  : Not detected")
            if p0.get("inband_detected"):
                dur_str = f"{p0.get('inband_duration_sec', 10.0):.1f}s"
                lines.append(f"   In-Band 220 MHz Grid    : {p0['inband_ppm']:+.1f} PPM ({dur_str} survey, Channels: {p0['inband_channels']})")
            else:
                lines.append("   In-Band 220 MHz Grid    : No carriers detected")
            lines.append(f"   Initial Calibrated PPM  : {p0['precalibrated_ppm']:+d} ({p0['reconciliation_reason']})")
            if p0.get("priority_channels"):
                lines.append(f"   Priority Channels (+50%): {p0['priority_channels']}")
            lines.append("-" * 80)

        if not self.scan_results:
            lines.append(" No channels transmitted Sync Signatures during the scan period.")
            lines.append(" Verify antenna connection, LNA power, and ensure local PTC transmitters are in range.")
        else:
            lines.append(f" {'Ch':^5} | {'Frequency':^14} | {'Best PPM':^10} | {'Sync / 5s':^11} | {'CRC Good':^10} | {'Avg RSSI':^11} | {'Avg SNR':^9} |")
            lines.append(f" {'-'*5}-+-{'-'*14}-+-{'-'*10}-+-{'-'*11}-+-{'-'*10}-+-{'-'*11}-+-{'-'*9}-|")
            for ch in sorted(self.scan_results.keys()):
                info = self.scan_results[ch]
                lines.append(
                    f"  {info['channel']:3d}  | "
                    f"{info['freq_mhz']:9.4f} MHz | "
                    f" {info['best_ppm']:+4d}     | "
                    f" {info['sync_5s']:5d}      | "
                    f" {info['crc_5s']:5d}    | "
                    f"{info['avg_rssi']:7.1f} dBFS | "
                    f"{info['avg_snr']:5.1f} dB  |"
                )
            lines.append("-" * 80)
            lines.append(f" Consensus Recommended Master Tuner PPM : {self.recommended_ppm:+d}")

            # Calculate safe gain based on peak/avg RSSI
            peak_rssi = max((r["avg_rssi"] for r in self.scan_results.values()), default=-100.0)
            if peak_rssi > -6.0:
                rec_gain = 16.6
            elif peak_rssi > -12.0:
                rec_gain = 20.7
            else:
                rec_gain = 22.9
            chans_list = sorted(self.scan_results.keys())
            locked_set = set(self.crc_verified_channels) & set(chans_list)
            for ch, info in self.scan_results.items():
                if info.get("crc_5s", 0) > 0:
                    locked_set.add(ch)
            chans_tokens = [f"{c}." if c in locked_set else str(c) for c in chans_list]
            chans_str = ",".join(chans_tokens)
            locked_str = ",".join(f"{c}." for c in sorted(locked_set)) if locked_set else "None"
            lines.append(f" Recommended Active Channels (--chans)  : {chans_str}")
            lines.append(f" CRC-Verified Channels (Locked)         : {locked_str}")
            lines.append(f" Recommended Gain (Headroom >= 12 dB)   : {rec_gain:.1f} dB")
            lines.append(f" Recommended Per-Channel AFC Limit      : 1500 Hz")

        lines.append("=" * 80)
        report_text = "\n".join(lines)
        return report_text

    def generate_startup_command_and_scripts(self) -> Tuple[str, Dict[str, str]]:
        """
        Phase 5: Constructs the absolute-path command line to launch ApexBOSGrabber
        with all determined optimal parameters, and saves launch scripts to the temp folder.
        Returns (command_line_str, dict_of_saved_script_paths).
        """
        python_exe = os.path.abspath(sys.executable)
        grabber_script = os.path.abspath(os.path.join(PROJECT_ROOT, "ApexBOSGrabber.py"))

        chans_list = sorted(self.scan_results.keys()) if self.scan_results else [101, 102, 114]
        locked_set = set(self.crc_verified_channels) & set(chans_list)
        for ch, info in self.scan_results.items():
            if info.get("crc_5s", 0) > 0:
                locked_set.add(ch)
        locked_list = sorted(locked_set)
        chans_tokens = [f"{c}." if c in locked_set else str(c) for c in chans_list]
        chans_str = ",".join(chans_tokens)

        # Gain recommendation based on RSSI
        peak_rssi = max((r["avg_rssi"] for r in self.scan_results.values()), default=-20.0)
        if peak_rssi > -6.0:
            rec_gain = 16.6
        elif peak_rssi > -12.0:
            rec_gain = 20.7
        else:
            rec_gain = 22.9

        cmd_tokens = [
            f'"{python_exe}"',
            f'"{grabber_script}"',
            f"--ppm {self.recommended_ppm}",
            f"--gain {rec_gain:.1f}",
            f"-E {self.max_errors}",
            "--afc-limit 1500",
            "--auto-tune",
            f"--chans {chans_str}",
        ]
        full_command_str = " ".join(cmd_tokens)

        # Save launch scripts in temp folder
        os.makedirs(self.output_dir, exist_ok=True)
        bat_path = os.path.join(self.output_dir, "start_apexbos_grabber.bat")
        ps1_path = os.path.join(self.output_dir, "start_apexbos_grabber.ps1")
        txt_path = os.path.join(self.output_dir, "start_apexbos_grabber_cmd.txt")
        rep_path = os.path.join(self.output_dir, "apexbos_channel_scan_report.txt")

        # Windows Batch script
        bat_content = (
            f"@echo off\n"
            f"REM ====================================================================\n"
            f"REM ApexBOSGrabber Launch Script\n"
            f"REM Generated automatically by ApexBOSScanner ({VERSION})\n"
            f"REM Master PPM: {self.recommended_ppm} | Gain: {rec_gain:.1f} dB | Chans: {chans_str}\n"
            f"REM ====================================================================\n\n"
            f"{full_command_str}\n"
            f"pause\n"
        )
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(bat_content)

        # PowerShell script
        ps1_content = (
            f"# ====================================================================\n"
            f"# ApexBOSGrabber Launch Script (PowerShell)\n"
            f"# Generated automatically by ApexBOSScanner ({VERSION})\n"
            f"# Master PPM: {self.recommended_ppm} | Gain: {rec_gain:.1f} dB | Chans: {chans_str}\n"
            f"# ====================================================================\n\n"
            f"& {full_command_str}\n"
        )
        with open(ps1_path, "w", encoding="utf-8") as f:
            f.write(ps1_content)

        # Plain command line string text
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(full_command_str + "\n")

        # Full report file
        with open(rep_path, "w", encoding="utf-8") as f:
            f.write(self.generate_report() + "\n\nCommand Line:\n" + full_command_str + "\n")

        # Machine-readable scan results JSON
        json_path = os.path.join(self.output_dir, "apexbos_scan_results.json")
        results_payload = {
            "version": VERSION,
            "timestamp": time.time(),
            "active_channels": chans_list if self.scan_results else [],
            "locked_channels": locked_list if self.scan_results else [],
            "chans_argument": chans_str,
            "recommended_ppm": self.recommended_ppm,
            "recommended_gain": rec_gain,
            "recommended_afc_limit": 1500,
            "command_line": full_command_str,
            "output_dir": self.output_dir,
            "phase0": self.phase0_results if self.phase0_results else {"skipped": True},
            "channel_details": {
                str(ch): {
                    "channel": info["channel"],
                    "freq_mhz": info["freq_mhz"],
                    "best_ppm": info["best_ppm"],
                    "sync_5s": info["sync_5s"],
                    "crc_5s": info["crc_5s"],
                    "avg_rssi": info["avg_rssi"],
                    "avg_snr": info["avg_snr"],
                }
                for ch, info in self.scan_results.items()
            },
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results_payload, f, indent=2)

        # Mirror JSON to system temp directory if output_dir is customized
        try:
            sys_temp_json = os.path.join(tempfile.gettempdir(), "apexbos_scan_results.json")
            if os.path.abspath(sys_temp_json) != os.path.abspath(json_path):
                with open(sys_temp_json, "w", encoding="utf-8") as f:
                    json.dump(results_payload, f, indent=2)
        except Exception:
            pass

        saved_files = {
            "bat": bat_path,
            "ps1": ps1_path,
            "txt": txt_path,
            "report": rep_path,
            "json": json_path,
        }
        return full_command_str, saved_files

    def run(self) -> Tuple[str, Dict[str, str]]:
        """Executes full search, calibration, report, and script generation workflow."""
        start_t = time.time()
        self.init_sdr_stream()

        try:
            if not self.skip_phase0:
                self.run_phase0_precalibration()

            self.scan_channels()
            if self.aborted and not self.active_channels:
                self.log("\n[*] Scan terminated by user.", COLOR_YELLOW)
                return "", {}

            self.optimize_ppm_for_active_channels()
            self.determine_best_overall_ppm()
            report_text = self.generate_report()
            cmd_str, saved_files = self.generate_startup_command_and_scripts()

            self.log("\n" + report_text, COLOR_WHITE)
            self.log("\n" + "=" * 80, COLOR_GREEN + COLOR_BOLD)
            self.log(" Generated Start Command Line (Absolute Paths):", COLOR_GREEN + COLOR_BOLD)
            self.log("=" * 80, COLOR_GREEN + COLOR_BOLD)
            self.log(f"\n{cmd_str}\n", COLOR_WHITE + COLOR_BOLD)
            self.log(f"[*] Saved Windows Batch Script : {saved_files['bat']}", COLOR_CYAN)
            self.log(f"[*] Saved PowerShell Script    : {saved_files['ps1']}", COLOR_CYAN)
            self.log(f"[*] Saved Text Command String  : {saved_files['txt']}", COLOR_CYAN)
            self.log(f"[*] Saved Full Calibration Log : {saved_files['report']}", COLOR_CYAN)
            self.log(f"[*] Total Execution Time       : {time.time() - start_t:.1f}s\n", COLOR_DIM)
            return cmd_str, saved_files
        finally:
            if hasattr(self, "keyboard") and self.keyboard is not None:
                try:
                    self.keyboard.close()
                except Exception:
                    pass
            if hasattr(self.stream, "close") and callable(self.stream.close):
                try:
                    self.stream.close()
                except Exception:
                    pass


def apply_scan_results_to_config(
    active_channels: List[int],
    recommended_ppm: int,
    recommended_gain: Optional[float] = None,
    config_path: Optional[str] = None,
    locked_channels: Optional[List[int]] = None,
) -> Tuple[bool, str]:
    """
    Updates Grabber channels (--chans), procs (--procs), ports, and PPM in
    unified config.json and synchronized grabber_config.json.
    Locks only channels that received valid CRC (locked_channels).
    """
    if not active_channels:
        return False, "No active channels to apply."

    import config_loader
    cfg_file = config_path or os.path.join(PROJECT_ROOT, "config.json")
    cfg = config_loader.load_unified_config(cfg_file)
    grabber_cfg = cfg.setdefault("grabber", {})

    sorted_chans = sorted(set(int(c) for c in active_channels))
    grabber_cfg["channels"] = sorted_chans

    if locked_channels is not None:
        locked_set = set(int(c) for c in locked_channels) & set(sorted_chans)
    else:
        locked_set = set(sorted_chans)

    chans_tokens = [f"{c}." if c in locked_set else str(c) for c in sorted_chans]
    chans_str = ", ".join(chans_tokens)
    grabber_cfg["chans"] = chans_str
    grabber_cfg["locked_channels"] = sorted(locked_set)
    grabber_cfg["ppm"] = int(recommended_ppm)
    grabber_cfg["frequency_correction"] = int(recommended_ppm)
    if recommended_gain is not None:
        grabber_cfg["gain"] = float(recommended_gain)

    # Recalculate worker procs & ports based on active channels
    existing_procs = int(grabber_cfg.get("process_count", 2) or 2)
    worker_count = min(existing_procs, max(1, len(sorted_chans)))
    ports, _ = config_loader.calculate_grabber_procs(sorted_chans, worker_count)
    grabber_cfg["ports"] = ports
    procs_str = ",".join(str(p) for p in ports)
    grabber_cfg["procs"] = procs_str
    grabber_cfg["process_count"] = len(ports)

    # Synchronize Flux relay sources if present
    flux_cfg = cfg.get("flux")
    if isinstance(flux_cfg, dict):
        flux_cfg["sources"] = [f"tcp://127.0.0.1:{p}" for p in ports]
        flux_cfg["ports"] = list(ports)

    config_loader.save_unified_config(cfg, cfg_file)
    config_loader.sync_legacy_configs(cfg, os.path.dirname(os.path.abspath(cfg_file)))
    msg = f"Updated Grabber config: {len(sorted_chans)} channels ({chans_str}), {len(ports)} procs ({procs_str}), PPM {recommended_ppm:+d}"
    return True, msg


def build_scanner_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"ApexBOSScanner ({VERSION}) - ITC 220 MHz Channel Scanner & PPM Calibrator"
    )
    parser.add_argument("--start-ch", type=int, default=97, help="Starting ITC channel number (default: 97)")
    parser.add_argument("--end-ch", type=int, default=176, help="Ending ITC channel number (default: 176)")
    parser.add_argument(
        "-d", "--dwell", "--dwell-time", "--probe-duration",
        type=float,
        default=2.0,
        dest="dwell",
        help="Observation dwell time per channel in seconds during Phase 1 probe (default: 2.0s)",
    )
    parser.add_argument(
        "--opt-dwell", "--ppm-dwell",
        type=float,
        default=5.0,
        dest="opt_dwell",
        help="Observation dwell time per candidate PPM in seconds during Phase 2 sweep (default: 5.0s)",
    )
    parser.add_argument("--ppm", type=int, default=0, help="Initial base tuner PPM (default: 0)")
    parser.add_argument("--gain", type=float, default=20.7, help="SDR tuner gain in dB (default: 20.7 dB)")
    parser.add_argument("--rate", type=int, default=960000, help="SDR sample rate in S/s (default: 960000)")
    parser.add_argument("--device", type=int, default=0, help="RTL-SDR device index (default: 0)")
    parser.add_argument("--bias-tee", action="store_true", default=False, help="Enable 4.5V LNA bias-tee power")
    parser.add_argument(
        "-E", "--max-errors", "--max-sync-errors",
        type=int,
        default=8,
        dest="max_errors",
        help="Maximum allowable bit errors in 64-bit unique word sync signature (default: 8)",
    )
    parser.add_argument(
        "--inband-duration",
        "--survey-duration",
        "--inband-dwell",
        "--phase0-duration",
        "--phase0-dwell",
        type=float,
        default=10.0,
        dest="inband_duration",
        help="Observation sampling duration in seconds for Phase 0 Option 2 in-band spectral survey (default: 10.0s)",
    )
    parser.add_argument(
        "--skip-phase0",
        action="store_true",
        default=False,
        help="Skip Phase 0 NOAA and in-band spectral pre-calibration",
    )
    parser.add_argument("--mock", action="store_true", default=False, help="Run offline scan using synthetic mock bursts")
    parser.add_argument("--output-dir", default=None, help="Directory to save generated start scripts and report (default: temp folder)")
    parser.add_argument("--quiet", action="store_true", default=False, help="Suppress detailed scan logs")
    parser.add_argument(
        "--update-config",
        action="store_true",
        default=False,
        help="Automatically update config.json and grabber_config.json with discovered channels and PPM",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to unified config.json (default: config.json in project root)",
    )
    return parser


def main():
    parser = build_scanner_cli_parser()
    args = parser.parse_args()

    scanner = ApexBOSChannelScanner(
        start_ch=args.start_ch,
        end_ch=args.end_ch,
        base_ppm=args.ppm,
        gain=args.gain,
        sample_rate=args.rate,
        device_idx=args.device,
        bias_tee=args.bias_tee,
        use_mock=args.mock,
        output_dir=args.output_dir,
        verbose=not args.quiet,
        probe_duration_sec=args.dwell,
        opt_duration_sec=args.opt_dwell,
        max_errors=args.max_errors,
        skip_phase0=args.skip_phase0,
        inband_duration=args.inband_duration,
    )
    cmd_str, saved_files = scanner.run()

    if scanner.scan_results:
        cfg_file = args.config or os.path.join(PROJECT_ROOT, "config.json")
        do_update = args.update_config
        if not do_update and sys.stdin.isatty() and not args.quiet:
            try:
                chans_disp = ", ".join(str(c) for c in sorted(scanner.scan_results.keys()))
                prompt_txt = (
                    f"\n{COLOR_CYAN}[?]{COLOR_RESET} Update Grabber configuration in {os.path.basename(cfg_file)}?\n"
                    f"    Channels: [{chans_disp}]\n"
                    f"    Consensus PPM: {scanner.recommended_ppm:+d}\n"
                    f"    Proceed with update? [y/N]: "
                )
                user_ans = input(prompt_txt).strip().lower()
                do_update = user_ans in ("y", "yes")
            except Exception:
                do_update = False

        if do_update:
            try:
                ok, msg = apply_scan_results_to_config(
                    active_channels=sorted(scanner.scan_results.keys()),
                    recommended_ppm=scanner.recommended_ppm,
                    config_path=cfg_file,
                )
                if ok:
                    print(f"\n{COLOR_GREEN}[✓] {msg}{COLOR_RESET}\n")
                else:
                    print(f"\n{COLOR_YELLOW}[!] {msg}{COLOR_RESET}\n")
            except Exception as e:
                print(f"\n{COLOR_RED}[!] Failed to update configuration: {e}{COLOR_RESET}\n", file=sys.stderr)


if __name__ == "__main__":
    main()
