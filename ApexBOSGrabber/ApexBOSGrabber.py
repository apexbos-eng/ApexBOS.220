#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# ApexBOSGrabber.py
#
# MIT License
#
# Copyright (c) 2026 AB
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# ==============================================================================
"""
ApexBOSGrabber.py

ApexBOS High-Performance ITC Demodulator & Decoder

MIT License
Copyright (c) 2026 AB

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Key Features:
- Native pi/4-DQPSK differential phase demodulation across 12 symbol sampling phases.
- Vectorized 1D convolution 64-bit unique word detector (forward & bit-reversed polarities).
- Viterbi K=7 rate-1/2 convolutional header decoder with 3-bit LFSR polynomial integrity check.
- High-speed header caching & guessing: caches valid headers and guesses common patterns with CRC verification.
- Block de-interleaving with shortened parity relocation supporting packets up to 350+ bytes.
- Reed-Solomon RS(16, 12) block forward error correction over GF(256) poly 0x11d with block-level syndrome fast-path and caching.
- Strict CRC-16/ARC (0xA001) frame verification across all packets.
- Real-time sliding window error rate statistics (60s rolling window) with instant reset on tuning changes (PPM, Gain, Bias-Tee).
- Full telemetry extraction: Beacons (0x02), Time (0x06), CNTL (0x05), WIU (0x33/0x34), and Class D (0x70).
- Multi-process pipelining with live I/Q sample throughput tracking and load-shedding protection.
- ZeroMQ PUB (MosFET b'I' binary frame + raw hex string + metadata JSON) and UDP datagram sockets.
- Dynamic on-the-fly tuning of PPM, Gain (dB or tenths), Bias-Tee, and active channels with automatic SDR retuning.
- Automatic SDR sample rate scaling (to integer multiples of 192k) when channel span requires wider bandwidth.
- Live colored ASCII terminal dashboard with real-time packet rates (PPM), sliding-window error rates, and telemetry.
"""

import argparse
import collections
import json
import math
import multiprocessing
import os
import queue
import re
import select
import signal
import socket
import struct
import subprocess
import shutil
import sys
import threading
import time
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List

# Ensure project modules can be imported
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Dependency Verification & Missing Package Detection
# ---------------------------------------------------------------------------
REQUIRED_PACKAGES = [
    ("numpy", "numpy", "High-performance array and DSP math"),
    ("zmq", "pyzmq", "ZeroMQ IPC message streaming"),
]

_MISSING_DEPENDENCIES = []
for _mod, _pkg, _desc in REQUIRED_PACKAGES:
    try:
        __import__(_mod)
    except ImportError:
        _MISSING_DEPENDENCIES.append((_pkg, _desc))


def check_required_dependencies(exit_on_failure: bool = True) -> List[str]:
    """
    Checks if all required external Python packages are installed.
    If any imports failed, prints installation commands and exits if exit_on_failure=True.
    Returns list of missing pip package names.
    """
    if _MISSING_DEPENDENCIES and exit_on_failure:
        py_cmd = sys.executable or "python"
        pip_pkgs = " ".join(p for p, _ in _MISSING_DEPENDENCIES)
        print("\n" + "=" * 76)
        print(" [!] ApexBOSGrabber: Missing Required Python Packages")
        print("=" * 76)
        print("The following package(s) must be installed to run ApexBOSGrabber:\n")
        for pkg, desc in _MISSING_DEPENDENCIES:
            print(f"  * {pkg:12s} - {desc}")
        print("\nPlease run the following command to install all missing packages:")
        print(f"    {py_cmd} -m pip install {pip_pkgs}\n")
        print("Or install individually:")
        for pkg, _ in _MISSING_DEPENDENCIES:
            print(f"    pip install {pkg}")
        print("=" * 76 + "\n")
        sys.exit(1)
    return [p for p, _ in _MISSING_DEPENDENCIES]


if _MISSING_DEPENDENCIES and __name__ == "__main__":
    check_required_dependencies(exit_on_failure=True)

try:
    import numpy as np
except ImportError:
    np = None

from version import VERSION, __version__

# Python 3.6 - 3.9 compatibility
HAS_BIT_COUNT = hasattr(int, "bit_count")

# ---------------------------------------------------------------------------
# ANSI Terminal Colors & Styling
# ---------------------------------------------------------------------------
COLOR_RESET = "\033[0m"
COLOR_BOLD = "\033[1m"
COLOR_DIM = "\033[90m"
COLOR_RED = "\033[91m"
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_BLUE = "\033[94m"
COLOR_MAGENTA = "\033[95m"
COLOR_CYAN = "\033[96m"
COLOR_WHITE = "\033[97m"

# High-contrast background color styles for Total Demod Failure Rate
BG_FAIL_GREEN = "\033[48;5;28m"         # Vibrant green for failure rate < 15.0%
BG_FAIL_AMBER = "\033[48;5;214m"        # Amber for failure rate 15.1% - 45.0%
BG_FAIL_ORANGE = "\033[48;5;208m"       # Orange for failure rate 45.1% - 66.0%
BG_FAIL_DARK_ORANGE = "\033[48;5;166m"   # Dark orange for failure rate 66.1% - 98.0%
BG_FAIL_RED = "\033[48;5;196m"           # Red for high failure rate > 98.1%

# Text contrast styles
FG_CONTRAST_BLACK = "\033[30m\033[1m"   # High contrast dark text on bright amber
FG_CONTRAST_WHITE = "\033[97m\033[1m"   # High contrast white text on colored backgrounds

class FailureRateStyle(tuple):
    """Encapsulates background color, status label, status color, and high-contrast foreground text style."""
    def __new__(cls, bg: str, tag: str, status_color: str, fg_contrast: str = FG_CONTRAST_WHITE):
        obj = super().__new__(cls, (bg, tag, status_color))
        obj.bg = bg
        obj.tag = tag
        obj.status_color = status_color
        obj.fg_contrast = fg_contrast
        return obj

    @property
    def fg(self):
        return self.fg_contrast

# 3-row half-block matrix digits for high-contrast, larger visibility on dashboard
LARGE_BLOCK_FONT_UNICODE = {
    "0": ["█▀█", "█ █", "▀▀▀"],
    "1": [" █ ", " █ ", " ▀ "],
    "2": ["▀▀█", "█▀▀", "▀▀▀"],
    "3": ["▀▀█", " ▀█", "▀▀▀"],
    "4": ["█ █", "▀▀█", "  ▀"],
    "5": ["█▀▀", " ▀█", "▀▀▀"],
    "6": ["█▀▀", "█▀█", "▀▀▀"],
    "7": ["▀▀█", "  █", "  ▀"],
    "8": ["█▀█", "█▀█", "▀▀▀"],
    "9": ["█▀█", " ▀█", "▀▀▀"],
    ".": [" ", " ", "█"],
    "%": ["▀ █", " █ ", "█ ▀"],
    " ": ["   ", "   ", "   "],
}

LARGE_BLOCK_FONT_ASCII = {
    "0": ["[#]", "| |", "[#]"],
    "1": [" # ", " # ", "###"],
    "2": ["##]", "[##", "###"],
    "3": ["##]", " -#", "##]"],
    "4": ["# #", "###", "  #"],
    "5": ["[##", "##]", "##]"],
    "6": ["[##", "[#]", "[#]"],
    "7": ["##]", "  #", "  #"],
    "8": ["[#]", "[#]", "[#]"],
    "9": ["[#]", "##]", "  #"],
    ".": [" ", " ", "."],
    "%": ["o/ ", " / ", "/o "],
    " ": ["   ", "   ", "   "],
}


def get_large_block_font():
    """Returns Unicode block font if terminal encoding supports it, else ASCII fallback."""
    try:
        enc = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
        "█▀".encode(enc)
        return LARGE_BLOCK_FONT_UNICODE
    except Exception:
        return LARGE_BLOCK_FONT_ASCII


def get_failure_rate_bg_and_tag(rate: float):
    """
    Returns FailureRateStyle (bg_ansi, label_str, status_color, fg_contrast) depending on failure rate:
    - rate < 15.0%: Green background, bold white text (<15%)
    - 15.0% < rate <= 45.0%: Amber background, high-contrast dark text (15.1% - 45%)
    - 45.0% < rate <= 66.0%: Orange background, bold white text (45.1% - 66%)
    - 66.0% < rate <= 98.0%: Dark orange background, bold white text (66.1% - 98%)
    - rate > 98.0%: Red background, bold white text (>98.1%)
    """
    if rate <= 15.0:
        return FailureRateStyle(BG_FAIL_GREEN, "OPTIMAL (<15%)", COLOR_GREEN, FG_CONTRAST_WHITE)
    elif rate <= 45.0:
        return FailureRateStyle(BG_FAIL_AMBER, "MODERATE (15-45%)", COLOR_YELLOW, FG_CONTRAST_BLACK)
    elif rate <= 66.0:
        return FailureRateStyle(BG_FAIL_ORANGE, "ELEVATED (45-66%)", COLOR_YELLOW, FG_CONTRAST_WHITE)
    elif rate <= 98.0:
        return FailureRateStyle(BG_FAIL_DARK_ORANGE, "HIGH (66-98%)", COLOR_YELLOW, FG_CONTRAST_WHITE)
    else:
        return FailureRateStyle(BG_FAIL_RED, "CRITICAL (>98.1%)", COLOR_RED, FG_CONTRAST_WHITE)

DEFAULT_SAMPLE_RATE = 192000
DEFAULT_CHUNK_SIZE = 148000  # I/Q samples
SLIDING_WINDOW_SECONDS = 60.0
SHORT_SLIDING_WINDOW_SECONDS = 5.0

# ---------------------------------------------------------------------------
# Protocol Constants & Bit Patterns
# ---------------------------------------------------------------------------
# 64-bit ITC unique word ("f337eeb637660672" MSB-first per nibble)
PTC_SYNC_BIT_STRING = "1111001100110111111011101011011000110111011001100000011001110010"
SYNC_WORD_LEN_BITS = 64
SYNC_WORD_INT64 = int(PTC_SYNC_BIT_STRING, 2)
SYNC_WORD_REV_INT64 = int(PTC_SYNC_BIT_STRING[::-1], 2)

# Pre-computed bipolar unique word vectors for vectorized cross-correlation
UW_FWD_BIPOLAR = np.array([1.0 if c == "1" else -1.0 for c in PTC_SYNC_BIT_STRING], dtype=np.float32)
UW_REV_BIPOLAR = np.array([1.0 if c == "1" else -1.0 for c in PTC_SYNC_BIT_STRING[::-1]], dtype=np.float32)

# Pre-computed ideal differential phase angles for the 32-symbol unique word
_SYNC_BITS_FWD = [int(c) for c in PTC_SYNC_BIT_STRING]
_SYNC_BITS_REV = [int(c) for c in PTC_SYNC_BIT_STRING[::-1]]
_IDEAL_DIBIT_ANGLES = {
    (0, 0): -math.pi / 4.0,       # -45 deg (fourth quadrant: imag <= 0, real >= 0)
    (1, 0): +math.pi / 4.0,       # +45 deg (first quadrant: imag > 0, real >= 0)
    (1, 1): +3.0 * math.pi / 4.0, # +135 deg (second quadrant: imag > 0, real < 0)
    (0, 1): -3.0 * math.pi / 4.0, # -135 deg (third quadrant: imag <= 0, real < 0)
}
SYNC_IDEAL_DIFF_ANGLES_FWD = np.array(
    [_IDEAL_DIBIT_ANGLES[(_SYNC_BITS_FWD[k], _SYNC_BITS_FWD[k + 1])] for k in range(0, 64, 2)],
    dtype=np.float32,
)
SYNC_IDEAL_DIFF_ANGLES_REV = np.array(
    [_IDEAL_DIBIT_ANGLES[(_SYNC_BITS_REV[k], _SYNC_BITS_REV[k + 1])] for k in range(0, 64, 2)],
    dtype=np.float32,
)


def estimate_sync_frequency_offset(diff_symbols_32: np.ndarray, baud_rate: float = 16000.0) -> Tuple[float, float, bool]:
    """
    Data-aided frequency offset and residual phase error estimator based on the 32-symbol unique word.
    Uses differential phase error variance to automatically resolve forward vs. reversed sync polarity.
    Returns:
    - delta_f_hz: Estimated carrier frequency offset in Hertz
    - mean_phase_error_rad: Mean phase rotation per symbol in radians
    - is_reversed_polarity: True if sync hit matched reversed bit pattern
    """
    if len(diff_symbols_32) < 32:
        return 0.0, 0.0, False

    d32 = diff_symbols_32[:32]
    pe_fwd = np.angle(d32 * np.exp(-1j * SYNC_IDEAL_DIFF_ANGLES_FWD))
    pe_rev = np.angle(d32 * np.exp(-1j * SYNC_IDEAL_DIFF_ANGLES_REV))

    var_fwd = float(np.var(pe_fwd))
    var_rev = float(np.var(pe_rev))

    if var_fwd <= var_rev:
        mean_err = float(np.mean(pe_fwd))
        is_rev = False
    else:
        mean_err = float(np.mean(pe_rev))
        is_rev = True

    Ts = 1.0 / max(1.0, float(baud_rate))
    delta_f = -mean_err / (2.0 * math.pi * Ts)
    return float(delta_f), float(mean_err), is_rev

# Header Convolutional Code: K=7, Rate 1/2
CONVOLUTIONAL_POLY_0 = 0x6D  # Tap mask A
CONVOLUTIONAL_POLY_1 = 0x4F  # Tap mask B

# ---------------------------------------------------------------------------
# Galois Field GF(2^8) & Reed-Solomon Tables
# ---------------------------------------------------------------------------
GALOIS_FIELD_EXP = [0] * 512
GALOIS_FIELD_LOG = [0] * 256
_lfsr_val = 1
for _idx in range(255):
    GALOIS_FIELD_EXP[_idx] = _lfsr_val
    GALOIS_FIELD_LOG[_lfsr_val] = _idx
    _lfsr_val <<= 1
    if _lfsr_val & 0x100:
        _lfsr_val ^= 0x11D
for _idx in range(255, 512):
    GALOIS_FIELD_EXP[_idx] = GALOIS_FIELD_EXP[_idx - 255]


def gf_mult(val_a: int, val_b: int) -> int:
    """Multiplication in GF(2^8)."""
    if not val_a or not val_b:
        return 0
    return GALOIS_FIELD_EXP[GALOIS_FIELD_LOG[val_a] + GALOIS_FIELD_LOG[val_b]]


def gf_inverse(val: int) -> int:
    """Multiplicative inverse in GF(2^8)."""
    return GALOIS_FIELD_EXP[255 - GALOIS_FIELD_LOG[val]]


# ---------------------------------------------------------------------------
# Frequency Calculations
# ---------------------------------------------------------------------------
def itc_channel_frequency(channel_num: int) -> int:
    """ITC frequency formula: 217 587 500 + (channel# * 25 000)."""
    return 217587500 + int(channel_num) * 25000


def calculate_optimal_center_freq(channel_list, max_bandwidth: int = DEFAULT_SAMPLE_RATE) -> int:
    """
    Calculates center frequency for the channel list.
    If the channel span is within bandwidth, centers at the midpoint of all channels.
    If the channel span exceeds bandwidth, centers on the primary downlink cluster (<= 136).
    """
    if not channel_list:
        return itc_channel_frequency(101)
    sorted_ch = sorted(channel_list)
    freqs = [itc_channel_frequency(ch) for ch in sorted_ch]
    span = max(freqs) - min(freqs)
    if span <= max_bandwidth:
        return int(round((min(freqs) + max(freqs)) / 2.0))

    base_chans = [ch for ch in sorted_ch if ch <= 136]
    if base_chans:
        base_freqs = [itc_channel_frequency(ch) for ch in base_chans]
        cluster = [f for f in base_freqs if f - min(base_freqs) <= max_bandwidth - 25000]
        if cluster:
            return int(round((min(cluster) + max(cluster)) / 2.0))

    cluster = [f for f in freqs if f - min(freqs) <= max_bandwidth - 25000]
    return int(round((min(cluster) + max(cluster)) / 2.0))


def load_channel_selection(arg_channels=None, json_path=None, return_locked=False):
    """
    Loads active channels from CLI argument, JSON file, or workspace default.
    Supports suffix 'l', 'p', or '.' to lock channels in (e.g. '102.', '102l', '102p').
    Returns sorted channel list, or (sorted_channel_list, set_of_locked_channels) if return_locked=True.
    """
    channel_set = set()
    locked_set = set()
    if arg_channels:
        for item in arg_channels.split(","):
            item = item.strip()
            if not item:
                continue
            if "-" in item:
                parts = item.split("-", 1)
                s_str, e_str = parts[0].strip(), parts[1].strip()
                m_s = re.match(r"^(\d+)([lpLP\.]?)$", s_str)
                m_e = re.match(r"^(\d+)([lpLP\.]?)$", e_str)
                if m_s and m_e:
                    s_ch, e_ch = int(m_s.group(1)), int(m_e.group(1))
                    is_lock = bool(m_s.group(2) or m_e.group(2))
                    rng = list(range(min(s_ch, e_ch), max(s_ch, e_ch) + 1))
                    channel_set.update(rng)
                    if is_lock:
                        locked_set.update(rng)
            else:
                m = re.match(r"^(\d+)([lpLP\.]?)$", item)
                if m:
                    ch = int(m.group(1))
                    channel_set.add(ch)
                    if m.group(2):
                        locked_set.add(ch)
                else:
                    digits = re.sub(r"[^\d]", "", item)
                    if digits:
                        channel_set.add(int(digits))
        sorted_ch = sorted(channel_set)
        return (sorted_ch, locked_set) if return_locked else sorted_ch

    target_json = json_path or os.path.join(PROJECT_ROOT, "channels.json")
    if os.path.exists(target_json):
        try:
            with open(target_json, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                for entry in cfg.get("channels", []):
                    if "channel" in entry:
                        ch = int(entry["channel"])
                        channel_set.add(ch)
                        if entry.get("locked") or entry.get("permanent"):
                            locked_set.add(ch)
            if channel_set:
                sorted_ch = sorted(channel_set)
                return (sorted_ch, locked_set) if return_locked else sorted_ch
        except Exception:
            pass

    default_chans = [101, 102, 107, 108, 113, 114, 125, 126]
    return (default_chans, locked_set) if return_locked else default_chans


def distribute_channels_across_workers(channel_list, worker_count: int):
    """Distributes channels evenly across worker processes."""
    if worker_count <= 0:
        return []
    sorted_unique = sorted(set(channel_list))
    buckets = [[] for _ in range(worker_count)]
    for idx, ch in enumerate(sorted_unique):
        buckets[idx % worker_count].append(ch)
    return buckets


# ---------------------------------------------------------------------------
# Baseband Decimator & Continuous Channelizer
# ---------------------------------------------------------------------------
class WindowedSincFilter:
    """Windowed-sinc FIR low-pass filter and integer decimator."""
    def __init__(self, decimation_factor: int, normalized_cutoff: float, tap_count: int):
        self.factor = max(1, decimation_factor)
        taps = tap_count if tap_count % 2 != 0 else tap_count + 1
        half_m = (taps - 1) / 2.0
        h_vals = []
        for i in range(taps):
            t = i - half_m
            sinc = 2.0 * normalized_cutoff if t == 0 else math.sin(2.0 * math.pi * normalized_cutoff * t) / (math.pi * t)
            hann = 0.5 - 0.5 * math.cos(2.0 * math.pi * i / (taps - 1))
            h_vals.append(sinc * hann)
        sum_h = sum(h_vals)
        self.taps = np.array([x / sum_h for x in h_vals], dtype=np.float32)
        self.tail = np.zeros(0, dtype=np.complex64)

    def reset(self):
        self.tail = np.zeros(0, dtype=np.complex64)

    def process(self, complex_in: np.ndarray) -> np.ndarray:
        if len(self.tail) > 0:
            full_in = np.concatenate([self.tail, complex_in])
        else:
            full_in = complex_in
        if len(full_in) < len(self.taps):
            self.tail = full_in
            return np.zeros(0, dtype=np.complex64)
        self.tail = full_in[-(len(self.taps) - 1):]
        filtered = np.convolve(full_in, self.taps[::-1], mode="valid")
        if self.factor > 1:
            return filtered[::self.factor]
        return filtered


class ContinuousChannelizer:
    """
    Channelizer maintaining continuous local oscillator phase and filter state across consecutive sample chunks.
    Maintains per-channel decimation filters to avoid state collision across channels.
    Decimates wideband SDR input down to 192 kS/s (Full-Rate) and 96 kS/s (Half-Rate).
    """
    def __init__(self, input_rate: int = DEFAULT_SAMPLE_RATE):
        self.input_rate = input_rate
        self.phase_accumulators = {}
        self.dec_fr = self.input_rate // 192000 if self.input_rate >= 192000 else 1
        self.decimators_fr = {}
        self.decimators_hr = {}

    def _get_decimators(self, target_ch: int):
        if target_ch not in self.decimators_fr:
            self.decimators_fr[target_ch] = WindowedSincFilter(
                self.dec_fr, 0.45 * 192000.0 / self.input_rate, 16 * self.dec_fr + 1
            )
            self.decimators_hr[target_ch] = WindowedSincFilter(
                2, 0.45 * 96000.0 / 192000.0, 33
            )
        return self.decimators_fr[target_ch], self.decimators_hr[target_ch]

    def reset(self):
        self.phase_accumulators.clear()
        self.decimators_fr.clear()
        self.decimators_hr.clear()

    def channelize(self, raw_iq_bytes: bytes, center_hz: int, target_ch: int, ch_freq_offset_hz: float = 0.0):
        """
        Mixes raw I/Q bytes to channel baseband with optional per-channel frequency offset compensation.
        Produces:
        - y_fr: 192,000 S/s baseband (sps = 12 for Full-Rate 16 kbaud)
        - y_hr: 96,000 S/s baseband (sps = 12 for Half-Rate 8 kbaud)
        """
        arr = np.frombuffer(raw_iq_bytes, dtype=np.uint8).astype(np.float32) - 127.5
        n_samples = len(arr) // 2
        iq = arr[0::2] + 1j * arr[1::2]

        freq_offset = float(itc_channel_frequency(target_ch) + ch_freq_offset_hz - center_hz)
        phase0 = self.phase_accumulators.get(target_ch, 0.0)
        dt = 1.0 / self.input_rate
        indices = np.arange(n_samples)
        lo_mixing_table = np.exp(-2j * np.pi * (phase0 + freq_offset * indices * dt))
        next_phase = (phase0 + freq_offset * n_samples * dt) % 1.0
        self.phase_accumulators[target_ch] = float(next_phase)

        mixed = iq * lo_mixing_table
        dec_fr, dec_hr = self._get_decimators(target_ch)
        y_fr = dec_fr.process(mixed)
        y_hr = dec_hr.process(y_fr) if len(y_fr) > 33 else np.zeros(0, dtype=np.complex64)
        return y_fr, y_hr


# ---------------------------------------------------------------------------
# Power & RSSI Measurements
# ---------------------------------------------------------------------------
def compute_signal_power_dbfs(raw_or_complex) -> float:
    """Calculates RMS signal power in dBFS."""
    if isinstance(raw_or_complex, (bytes, bytearray)):
        arr = np.frombuffer(raw_or_complex, dtype=np.uint8).astype(np.float32) - 127.5
        if len(arr) < 2:
            return -100.0
        p_mean = np.mean(arr[0::2] ** 2 + arr[1::2] ** 2)
    elif isinstance(raw_or_complex, np.ndarray):
        if len(raw_or_complex) == 0:
            return -100.0
        p_mean = np.mean(np.abs(raw_or_complex) ** 2)
    else:
        return -100.0

    if p_mean <= 1e-12:
        return -100.0
    return float(10.0 * math.log10(p_mean / (127.5 * 127.5)))


# ---------------------------------------------------------------------------
# Native PHY: pi/4-DQPSK Demodulator & Sync Correlator
# ---------------------------------------------------------------------------
def demodulate_pi4_dqpsk(
    baseband_samples: np.ndarray,
    samples_per_symbol: int,
    sampling_phase: int,
    freq_offset_hz: float = 0.0,
    baud_rate: float = 16000.0,
    return_diff: bool = False,
):
    """
    Differential pi/4-DQPSK demodulator.
    diff = conj(sub[1:]) * sub[:-1]
    b0 = (diff.imag > 0.0)
    b1 = (diff.real < 0.0)
    If freq_offset_hz is specified, de-rotates diff by the expected phase error per symbol.
    If return_diff is True, returns (bit_stream, diff). Otherwise returns bit_stream.
    """
    sub = baseband_samples[sampling_phase::samples_per_symbol]
    if len(sub) < 2:
        empty = np.zeros(0, dtype=np.uint8)
        return (empty, np.zeros(0, dtype=np.complex64)) if return_diff else empty

    diff = np.conj(sub[1:]) * sub[:-1]
    if freq_offset_hz != 0.0:
        Ts = 1.0 / max(1.0, float(baud_rate))
        phase_bias = -2.0 * math.pi * float(freq_offset_hz) * Ts
        diff = diff * np.exp(-1j * phase_bias)

    dibit_b0 = (diff.imag > 0.0).astype(np.uint8)
    dibit_b1 = (diff.real < 0.0).astype(np.uint8)

    bit_stream = np.empty(len(dibit_b0) * 2, dtype=np.uint8)
    bit_stream[0::2] = dibit_b0
    bit_stream[1::2] = dibit_b1
    return (bit_stream, diff) if return_diff else bit_stream


def locate_unique_word_hits(bit_array: np.ndarray, max_hamming_errors: int = 8):
    """
    Slides 64-bit unique word over the bitstream using vectorized NumPy convolution.
    Tests both forward and bit-reversed polarities with Hamming distance <= max_hamming_errors.
    """
    num_bits = len(bit_array)
    if num_bits < SYNC_WORD_LEN_BITS:
        return []

    bipolar = np.where(bit_array > 0, 1.0, -1.0).astype(np.float32)
    corr_fwd = np.convolve(bipolar, UW_FWD_BIPOLAR[::-1], mode="valid")
    corr_rev = np.convolve(bipolar, UW_REV_BIPOLAR[::-1], mode="valid")

    err_fwd = np.round((64.0 - corr_fwd) * 0.5).astype(np.int32)
    err_rev = np.round((64.0 - corr_rev) * 0.5).astype(np.int32)
    best_err = np.minimum(err_fwd, err_rev)

    hit_indices = np.where(best_err <= max_hamming_errors)[0]
    hits = []
    last_pos = -100
    for p in hit_indices:
        p_int = int(p)
        if p_int - last_pos >= SYNC_WORD_LEN_BITS:
            hits.append((p_int, int(best_err[p_int])))
            last_pos = p_int

    return hits


def pack_bit_sequence_msb(bit_array: np.ndarray, start_pos: int, bit_count: int) -> bytes:
    """Packs bit array MSB-first into bytes."""
    end_pos = min(len(bit_array), start_pos + bit_count)
    selected = bit_array[start_pos:end_pos]
    full_bytes = len(selected) // 8
    out = bytearray(full_bytes + (1 if len(selected) % 8 else 0))

    for i in range(full_bytes):
        chunk = selected[i * 8:(i + 1) * 8]
        val = 0
        for b in chunk:
            val = (val << 1) | int(b)
        out[i] = val

    rem = len(selected) % 8
    if rem > 0:
        val = 0
        for b in selected[full_bytes * 8:]:
            val = (val << 1) | int(b)
        out[full_bytes] = val << (8 - rem)

    return bytes(out)


# ---------------------------------------------------------------------------
# FEC: Viterbi K=7 Rate-1/2 Header Decoder with Caching & Guessing
# ---------------------------------------------------------------------------
# Fast lookaside caches for confirmed valid headers
VITERBI_HEADER_CACHE = {}   # raw 6 bytes -> (is_valid, length, mode, type)
KNOWN_VALID_HEADERS = {}    # int48 -> (is_valid, length, mode, type)


def register_confirmed_header(header_bytes_6: bytes, decoded_tuple: tuple):
    """Caches confirmed valid header to optimize future lookups."""
    if len(header_bytes_6) >= 6:
        raw_hdr = bytes(header_bytes_6[:6])
        if len(VITERBI_HEADER_CACHE) < 4096:
            VITERBI_HEADER_CACHE[raw_hdr] = decoded_tuple
        raw_int = int.from_bytes(raw_hdr, "big")
        if len(KNOWN_VALID_HEADERS) < 256:
            KNOWN_VALID_HEADERS[raw_int] = decoded_tuple


def viterbi_trellis_decode_candidates(coded_bits_48: list, max_candidates: int = 8) -> list:
    """
    List-Viterbi decoder returning candidate information bit sequences (up to max_candidates),
    ordered by path metric cost (lowest cost first).
    Enables recovering valid headers when open-ended trellis states have near-tie metrics.
    """
    inf = 1 << 20
    path_metrics = [0] + [inf] * 63
    predecessors = [[-1] * 64 for _ in range(24)]

    for t in range(24):
        r0 = coded_bits_48[2 * t]
        r1 = coded_bits_48[2 * t + 1]
        new_path_metrics = [inf] * 64

        for st in range(64):
            if path_metrics[st] >= inf:
                continue
            for u in (0, 1):
                idx = (st << 1) | u
                ns = idx & 0x3F
                bm = ((bin(idx & CONVOLUTIONAL_POLY_0).count("1") % 2) ^ r0) + \
                     ((bin(idx & CONVOLUTIONAL_POLY_1).count("1") % 2) ^ r1)
                cost = path_metrics[st] + bm
                if cost < new_path_metrics[ns]:
                    new_path_metrics[ns] = cost
                    predecessors[t][ns] = st

        path_metrics = new_path_metrics

    sorted_states = sorted(range(64), key=lambda s: path_metrics[s])
    min_cost = path_metrics[sorted_states[0]]
    if min_cost >= inf:
        return []

    candidates = []
    for st in sorted_states[:max_candidates]:
        if path_metrics[st] > min_cost + 4:
            break
        out_bits = [0] * 24
        curr_st = st
        for t in range(23, -1, -1):
            out_bits[t] = curr_st & 1
            curr_st = predecessors[t][curr_st]
        candidates.append((out_bits, path_metrics[st]))

    return candidates


def viterbi_trellis_decode(coded_bits_48: list) -> list:
    """
    Viterbi decoder for 48 coded bits -> 24 information bits (K=7, rate 1/2).
    Generators: CONVOLUTIONAL_POLY_0 = 0x6D, CONVOLUTIONAL_POLY_1 = 0x4F.
    Returns the minimum metric path bits.
    """
    candidates = viterbi_trellis_decode_candidates(coded_bits_48, max_candidates=1)
    return candidates[0][0] if candidates else [0] * 24


def viterbi_decode_header_candidates(encoded_header_bytes: bytes, max_candidates: int = 8) -> list:
    """
    Decodes the 6 header bytes following the 64-bit unique word.
    Uses List-Viterbi trellis exploration and returns all candidate header tuples
    (is_valid, length, mode, type) that pass the 3-bit LFSR polynomial integrity check.
    """
    if len(encoded_header_bytes) < 6:
        return []

    raw_hdr = bytes(encoded_header_bytes[:6])
    cached = VITERBI_HEADER_CACHE.get(raw_hdr)
    if cached is not None:
        return [cached]

    # Fast guess lookup against known valid headers with Hamming distance <= 3
    raw_int = int.from_bytes(raw_hdr, "big")
    if HAS_BIT_COUNT:
        for known_int, decoded_tuple in KNOWN_VALID_HEADERS.items():
            if (raw_int ^ known_int).bit_count() <= 3:
                VITERBI_HEADER_CACHE[raw_hdr] = decoded_tuple
                return [decoded_tuple]
    else:
        for known_int, decoded_tuple in KNOWN_VALID_HEADERS.items():
            if bin(raw_int ^ known_int).count("1") <= 3:
                VITERBI_HEADER_CACHE[raw_hdr] = decoded_tuple
                return [decoded_tuple]

    # Full trellis decode with multi-candidate traceback
    coded_bits = []
    for k in range(48):
        byte_val = encoded_header_bytes[k >> 3]
        coded_bits.append((byte_val >> (7 - (k & 7))) & 1)

    candidates = viterbi_trellis_decode_candidates(coded_bits, max_candidates=max_candidates)
    valid_candidates = []

    for info_bits, cost in candidates:
        v_word = 0
        for b in info_bits:
            v_word = (v_word << 1) | b

        # 3-bit LFSR check: reduce bits 23..3 with poly 0x0B
        v_lfsr = v_word
        for bit_pos in range(23, 2, -1):
            if (v_lfsr >> bit_pos) & 1:
                v_lfsr ^= 0x0B << (bit_pos - 3)
        check_ok = (v_lfsr & 7) == 0
        if not check_ok:
            continue

        pkt_length = (v_word >> 15) & 0x1FF
        pkt_mode = (v_word >> 11) & 0xF
        pkt_type = (v_word >> 3) & 0xFF

        type_valid = (pkt_type != 0)
        is_valid = type_valid and (0 < pkt_length <= 511) and (0 <= pkt_mode <= 7)

        if is_valid:
            res = (True, pkt_length, pkt_mode, pkt_type)
            valid_candidates.append(res)

    if valid_candidates:
        best_res = valid_candidates[0]
        if len(VITERBI_HEADER_CACHE) < 4096:
            VITERBI_HEADER_CACHE[raw_hdr] = best_res
        if len(KNOWN_VALID_HEADERS) < 256:
            KNOWN_VALID_HEADERS[raw_int] = best_res

    return valid_candidates


def viterbi_decode_header(encoded_header_bytes: bytes):
    """
    Decodes the 6 header bytes following the 64-bit unique word.
    Optimized with fast lookups against known valid cached headers and List-Viterbi.
    Supports packets up to 350+ bytes (0 < pkt_length <= 511).
    Returns (is_valid, length, mode, type).
    """
    candidates = viterbi_decode_header_candidates(encoded_header_bytes, max_candidates=8)
    return candidates[0] if candidates else (False, 0, 0, 0)


# ---------------------------------------------------------------------------
# FEC: Reed-Solomon RS(16, 12) & CRC-16/ARC
# ---------------------------------------------------------------------------
RS_BLOCK_CACHE = {}  # 16-byte bytes -> (corrected_block, err_count)


def reed_solomon_block_decode(block_16):
    """
    Decodes one 16-byte block under shortened RS(16, 12) over GF(256) poly 0x11d.
    Uses syndrome check fast-path and LRU block cache for repetitive payloads/padding.
    Returns (corrected_bytes_16, error_count) or (original, -1) on uncorrectable.
    """
    r_bytes = list(block_16)
    nroots = 4

    # Calculate syndromes S_0 .. S_3
    syndromes = [0] * nroots
    has_errors = False
    for i in range(nroots):
        alpha_i = GALOIS_FIELD_EXP[i]
        acc = 0
        for b in r_bytes:
            acc = b ^ gf_mult(acc, alpha_i)
        syndromes[i] = acc
        if acc != 0:
            has_errors = True

    if not has_errors:
        return r_bytes, 0

    block_key = bytes(block_16)
    cached = RS_BLOCK_CACHE.get(block_key)
    if cached is not None:
        return list(cached[0]), cached[1]

    # Berlekamp-Massey algorithm
    C_poly = [0] * (nroots + 1)
    B_poly = [0] * (nroots + 1)
    C_poly[0] = 1
    B_poly[0] = 1
    L_deg = 0
    m_shift = 1
    b_val = 1

    for n_step in range(nroots):
        d_val = syndromes[n_step]
        for i in range(1, L_deg + 1):
            d_val ^= gf_mult(C_poly[i], syndromes[n_step - i])

        if d_val == 0:
            m_shift += 1
        else:
            T_poly = list(C_poly)
            mult_factor = gf_mult(d_val, gf_inverse(b_val))
            for i in range(nroots + 1 - m_shift):
                if B_poly[i]:
                    C_poly[i + m_shift] ^= gf_mult(mult_factor, B_poly[i])

            if 2 * L_deg <= n_step:
                L_deg = n_step + 1 - L_deg
                B_poly = T_poly
                b_val = d_val
                m_shift = 1
            else:
                m_shift += 1

    if L_deg <= 0 or L_deg > 2:
        return r_bytes, -1

    # Chien search
    errpos = []
    n_len = 16
    for i in range(n_len):
        li = n_len - 1 - i
        acc = 0
        for j in range(L_deg + 1):
            acc ^= gf_mult(C_poly[j], GALOIS_FIELD_EXP[((255 - li) * j) % 255])
        if acc == 0:
            errpos.append(i)

    if len(errpos) != L_deg:
        return r_bytes, -1

    # Forney formula
    omega = [0] * nroots
    for i in range(nroots):
        acc = 0
        for j in range(min(i, L_deg) + 1):
            acc ^= gf_mult(C_poly[j], syndromes[i - j])
        omega[i] = acc

    for k in range(len(errpos)):
        pos = errpos[k]
        li = n_len - 1 - pos
        x_inv = GALOIS_FIELD_EXP[(255 - li) % 255]
        numerator = 0
        for i in range(nroots):
            numerator ^= gf_mult(omega[i], GALOIS_FIELD_EXP[(i * GALOIS_FIELD_LOG[x_inv]) % 255])
        denominator = 0
        for j in range(1, L_deg + 1):
            if j & 1:
                denominator ^= gf_mult(C_poly[j], GALOIS_FIELD_EXP[((j - 1) * GALOIS_FIELD_LOG[x_inv]) % 255])
        if denominator:
            r_bytes[pos] ^= gf_mult(gf_mult(GALOIS_FIELD_EXP[li], numerator), gf_inverse(denominator))

    # Verify that all 4 syndromes of corrected block are strictly 0 (prevents false corrections)
    for i in range(nroots):
        alpha_i = GALOIS_FIELD_EXP[i]
        acc = 0
        for b in r_bytes:
            acc = b ^ gf_mult(acc, alpha_i)
        if acc != 0:
            return list(block_16), -1

    if len(RS_BLOCK_CACHE) < 2048:
        RS_BLOCK_CACHE[block_key] = (tuple(r_bytes), L_deg)
    return r_bytes, L_deg


def calculate_crc16_arc(data_bytes, length: int) -> int:
    """CRC-16/ARC (0xA001 reversed poly) over length bytes."""
    crc = 0
    for i in range(length):
        crc ^= data_bytes[i]
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if (crc & 1) else 0)
    return crc & 0xFFFF


def deinterleave_and_decode_blocks(
    data_slice: bytes, pkt_len: int, num_rs_blocks: int, l_max: Optional[int] = None
) -> Optional[list]:
    """
    De-interleaves data_slice into num_rs_blocks and RS(16, 12) decodes each block.
    Supports:
    - Monolithic interleaving (l_max=None): single matrix across all blocks
    - Chunked interleaving (l_max=12 or 6): partitions into groups of at most l_max blocks
    Returns extracted information byte stream on success, or None on failure.
    """
    if l_max is None or num_rs_blocks <= l_max:
        total_burst_bytes = pkt_len + 4 * num_rs_blocks
        if len(data_slice) < total_burst_bytes:
            return None

        deinterleaved_code_matrix = [0] * (num_rs_blocks * 16)
        k_ptr = 0
        for col_idx in range(16):
            for blk_idx in range(num_rs_blocks):
                pos = col_idx + 16 * blk_idx
                if pos < total_burst_bytes:
                    deinterleaved_code_matrix[pos] = data_slice[k_ptr]
                    k_ptr += 1

        for j in range(4):
            deinterleaved_code_matrix[num_rs_blocks * 16 - 1 - j] = deinterleaved_code_matrix[total_burst_bytes - 1 - j]
            deinterleaved_code_matrix[total_burst_bytes - 1 - j] = 0

        extracted_symbol_stream = []
        for blk_idx in range(num_rs_blocks):
            block_16 = deinterleaved_code_matrix[blk_idx * 16:(blk_idx + 1) * 16]
            corrected_block, err_count = reed_solomon_block_decode(block_16)
            if err_count < 0:
                return None
            extracted_symbol_stream.extend(corrected_block[:12])
        return extracted_symbol_stream

    # Grouped / Chunked de-interleaving with l_max
    extracted_symbol_stream = []
    bytes_consumed = 0
    rem_blocks = num_rs_blocks
    rem_data = pkt_len

    while rem_blocks > 0:
        cur_l = min(rem_blocks, l_max)
        is_last_chunk = (cur_l == rem_blocks)
        if is_last_chunk:
            cur_data_bytes = rem_data
            cur_burst_bytes = cur_data_bytes + 4 * cur_l
        else:
            cur_data_bytes = cur_l * 12
            cur_burst_bytes = cur_l * 16

        if len(data_slice) < bytes_consumed + cur_burst_bytes:
            return None

        chunk_slice = data_slice[bytes_consumed:bytes_consumed + cur_burst_bytes]
        bytes_consumed += cur_burst_bytes

        chunk_deint = [0] * (cur_l * 16)
        k_ptr = 0
        for col in range(16):
            for blk in range(cur_l):
                pos = col + 16 * blk
                if pos < cur_burst_bytes:
                    chunk_deint[pos] = chunk_slice[k_ptr]
                    k_ptr += 1

        if is_last_chunk:
            for j in range(4):
                chunk_deint[cur_l * 16 - 1 - j] = chunk_deint[cur_burst_bytes - 1 - j]
                chunk_deint[cur_burst_bytes - 1 - j] = 0

        for blk in range(cur_l):
            blk16 = chunk_deint[blk * 16:(blk + 1) * 16]
            corrected_block, err_count = reed_solomon_block_decode(blk16)
            if err_count < 0:
                return None
            extracted_symbol_stream.extend(corrected_block[:12])

        rem_blocks -= cur_l
        rem_data -= cur_data_bytes

    return extracted_symbol_stream


def fec_decode_itc_packet(packed_bytes: bytes, return_details: bool = False):
    """
    De-interleaves, RS-decodes, and CRC-verifies an ITC bit-packed stream.
    Supports packets up to 350+ bytes with both monolithic and chunked (L_max=12) interleaving.
    If return_details is True, returns (is_valid, packet_type, payload_bytes, stage_info).
    Otherwise returns (is_valid, packet_type, payload_bytes).
    """
    stage_info = {
        "header_ok": False,
        "rs_ok": False,
        "crc_ok": False,
        "pkt_type": 0,
        "pkt_len": 0,
    }
    if len(packed_bytes) < 14:
        if return_details:
            return False, 0, b"", stage_info
        return False, 0, b""

    cand_headers = viterbi_decode_header_candidates(packed_bytes[8:14], max_candidates=4)
    if not cand_headers:
        if return_details:
            return False, 0, b"", stage_info
        return False, 0, b""

    for hdr_ok, pkt_len, pkt_mode, pkt_type in cand_headers:
        if pkt_len <= 0 or pkt_len > 511:
            continue

        b_std = (pkt_len + 11) // 12
        b_alt = (pkt_len + 12) // 12
        cand_blocks = [b_std] if b_std == b_alt else [b_std, b_alt]

        header_accepted = False

        for num_rs_blocks in cand_blocks:
            total_burst_bytes = pkt_len + 4 * num_rs_blocks
            if num_rs_blocks <= 0 or num_rs_blocks > 64 or total_burst_bytes <= 0 or len(packed_bytes) < 14 + total_burst_bytes:
                continue

            if not header_accepted:
                stage_info["rs_ok"] = False
                stage_info["crc_ok"] = False
                stage_info["pkt_type"] = pkt_type
                stage_info["pkt_len"] = pkt_len
                stage_info["header_ok"] = True
                header_accepted = True

            data_slice = packed_bytes[14:14 + total_burst_bytes]

            # Interleaver strategies: for long packets (>12 blocks), test chunked L_max=12, monolithic, and L_max=6
            if num_rs_blocks > 12:
                l_max_options = [12, None, 6]
            elif num_rs_blocks > 6:
                l_max_options = [None, 6]
            else:
                l_max_options = [None]

            for l_max in l_max_options:
                extracted_symbol_stream = deinterleave_and_decode_blocks(
                    data_slice, pkt_len, num_rs_blocks, l_max=l_max
                )
                if extracted_symbol_stream is None:
                    continue

                stage_info["rs_ok"] = True

                # Verify CRC-16/ARC (always mandatory)
                if calculate_crc16_arc(extracted_symbol_stream, pkt_len) != 0:
                    continue
                stage_info["crc_ok"] = True

                # Register confirmed header in cache
                register_confirmed_header(packed_bytes[8:14], (True, pkt_len, pkt_mode, pkt_type))

                # Strip CRC/FCS trailer
                if pkt_type in (0x06, 0x07, 0x31, 0x33, 0x34) and pkt_len >= 6:
                    payload_len = pkt_len - 6
                else:
                    payload_len = max(0, pkt_len - 2)

                payload = bytes(extracted_symbol_stream[:payload_len])
                if return_details:
                    return True, pkt_type, payload, stage_info
                return True, pkt_type, payload

    if return_details:
        return False, 0, b"", stage_info
    return False, 0, b""


# ---------------------------------------------------------------------------
# Telemetry Parser
# ---------------------------------------------------------------------------
def lookup_itc_packet_name(packet_type: int) -> str:
    """Human-readable packet type name."""
    names = {
        0x01: "QSTAT",
        0x02: "BEACON",
        0x03: "ACK",
        0x04: "ACQ",
        0x05: "CNTL",
        0x06: "TIME",
        0x07: "T07",
        0x31: "STAT31",
        0x33: "WIU",
        0x34: "WIU",
        0x70: "UNICAST",
    }
    return names.get(packet_type, f"TYPE_{packet_type:#04x}")


def extract_packet_telemetry(packet_type: int, payload: bytes) -> dict:
    """Parses packet payload into structured telemetry."""
    info = {"type_name": lookup_itc_packet_name(packet_type)}
    if not payload:
        return info

    try:
        if packet_type == 0x02 and len(payload) >= 22:
            radio_id = (payload[1] << 16) | (payload[2] << 8) | payload[3]
            lat_raw = struct.unpack(">i", payload[4:8])[0]
            lon_raw = struct.unpack(">i", payload[8:12])[0]
            info.update({
                "radio_id": f"{radio_id:06X}",
                "lat": round(lat_raw / 600000.0, 6),
                "lon": round(lon_raw / 600000.0, 6),
                "base_ch": payload[12],
                "base_rate": payload[13],
                "util": payload[14],
                "ch_list": list(payload[16:22]),
            })

        elif packet_type == 0x06 and len(payload) >= 9:
            tid = (payload[0] << 16) | (payload[1] << 8) | payload[2]
            year = ((payload[3] << 8) | payload[4]) & 0x7FFF
            month = payload[5]
            day = payload[6]
            hour = payload[7]
            minute = payload[8]
            second = payload[9] if len(payload) > 9 else 0
            info.update({
                "tid": f"{tid:06X}",
                "utc_time": f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}",
            })

        elif packet_type == 0x05 and len(payload) >= 3:
            tid = (payload[0] << 16) | (payload[1] << 8) | payload[2]
            seq = payload[3] if len(payload) > 3 else 0
            info.update({
                "tid": f"{tid:06X}",
                "seq": seq,
                "data_hex": payload[4:20].hex().upper() if len(payload) > 4 else "",
            })

        elif packet_type in (0x33, 0x34) and len(payload) >= 3:
            wiu_id = (payload[0] << 16) | (payload[1] << 8) | payload[2]
            info["wiu_id"] = f"{wiu_id:06X}"
            if len(payload) >= 19:
                info["indicators"] = payload[3:19].hex().upper()
                sw_byte = payload[19] if len(payload) > 19 else 0
                sw_map = {0: "I", 1: "N", 2: "R", 3: "X"}
                info["switch"] = sw_map.get(sw_byte & 3, "?")

        elif (packet_type == 0x70 or packet_type == 0x71 or packet_type == 0x50 or packet_type == 0x51) and len(payload) >= 6:
            info["emsg_name"] = f"Class D type: 0x{packet_type:x}"
        else:
            if len(payload) >= 3:
                tid = (payload[0] << 16) | (payload[1] << 8) | payload[2]
                info["tid"] = f"{tid:06X}"
    except Exception:
        pass

    return info


def extract_base_packet_channels(packet_type: int, telemetry: dict) -> tuple:
    """
    Extracts base station's own channel and broadcast channel list from decoded packet telemetry.
    Returns (base_own_channel_or_None, list_of_valid_channels).
    """
    base_ch = None
    ch_list = []
    if packet_type == 0x02 or "base_ch" in telemetry or "ch_list" in telemetry:
        raw_b = telemetry.get("base_ch")
        if isinstance(raw_b, int) and 0 < raw_b < 255:
            base_ch = raw_b
        raw_list = telemetry.get("ch_list", [])
        if isinstance(raw_list, (list, tuple)):
            for c in raw_list:
                if isinstance(c, int) and 0 < c < 255 and c not in ch_list:
                    ch_list.append(c)
    return base_ch, ch_list


# ---------------------------------------------------------------------------
# SDR Stream Readers: Pipe, Stdin, RTL-TCP
# ---------------------------------------------------------------------------
def detect_rtl_sdr_executable(custom_path=None) -> str:
    """Finds rtl_sdr binary in system PATH or workspace."""
    if custom_path and os.path.exists(custom_path):
        return custom_path
    bin_name = "rtl_sdr.exe" if os.name == "nt" else "rtl_sdr"
    found = shutil.which(bin_name)
    if found:
        return found
    candidates = [
        bin_name,
        os.path.join(PROJECT_ROOT, bin_name),
        "/opt/homebrew/bin/rtl_sdr",
        "/usr/local/bin/rtl_sdr",
        "/usr/bin/rtl_sdr",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return bin_name


def is_rtl_sdr_available(custom_path=None) -> bool:
    """Checks if an rtl_sdr executable is actually present on disk or in system PATH."""
    bin_path = detect_rtl_sdr_executable(custom_path)
    if not bin_path:
        return False
    if os.path.isabs(bin_path) and os.path.exists(bin_path):
        return True
    if os.path.exists(bin_path):
        return True
    if shutil.which(bin_path):
        return True
    return False


def check_rtl_tcp_running(host: str = "127.0.0.1", port: int = 1234, timeout: float = 0.4) -> bool:
    """Checks if an active RTL-TCP server is listening on host:port."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (socket.error, OSError):
        return False


def detect_rtl_tcp_executable() -> Optional[str]:
    """Finds rtl_tcp executable in PATH or workspace."""
    bin_name = "rtl_tcp.exe" if os.name == "nt" else "rtl_tcp"
    found = shutil.which(bin_name)
    if found:
        return found
    candidates = [
        bin_name,
        os.path.join(PROJECT_ROOT, bin_name),
        "/opt/homebrew/bin/rtl_tcp",
        "/usr/local/bin/rtl_tcp",
        "/usr/bin/rtl_tcp",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def is_rtl_tcp_available() -> bool:
    """Checks if an rtl_tcp executable is available in PATH or workspace."""
    return detect_rtl_tcp_executable() is not None


def is_rtlsdr_package_available() -> Tuple[bool, Optional[str]]:
    """
    Checks if rtlsdr (pyrtlsdr) package is installed and can load the underlying librtlsdr.
    Returns (True, None) on success, or (False, error_reason).
    """
    try:
        import rtlsdr
    except ImportError:
        return False, "Python 'pyrtlsdr' package is not installed (run: pip install pyrtlsdr)"
    except Exception as e:
        return False, f"Error importing 'rtlsdr': {e}"

    try:
        from rtlsdr import RtlSdr
        return True, None
    except Exception as e:
        return False, f"'rtlsdr' package installed but backend dynamic library failed to load: {e}"


def print_sdr_setup_guide():
    """Prints comprehensive setup instructions for all supported SDR modes and exits."""
    py_cmd = sys.executable or "python"
    print("\n" + "=" * 78)
    print(" [!] ApexBOSGrabber: No RTL-SDR Interface or Device Available")
    print("=" * 78)
    print("ApexBOSGrabber requires an RTL-SDR source to ingest 220 MHz PTC telemetry.")
    print("No active SDR source was detected. Please configure one of the modes below:\n")

    print("1. PIPE MODE (Default - Osmocom rtl_sdr command-line utility):")
    print("   Install the rtl_sdr binary and ensure it is in your system PATH:")
    print("   * Windows: Download 64-bit RelWithDebInfo binaries from osmocom.org or")
    print("              rtl-sdr.com, and place 'rtl_sdr.exe' in this directory or PATH.")
    print("   * Ubuntu/Debian:  sudo apt-get install rtl-sdr")
    print("   * macOS:          brew install librtlsdr")
    print("   Launch Grabber:   python ApexBOSGrabber.py --pipe\n")

    print("2. RTL-TCP MODE (Local or Network RTL-TCP Server):")
    print("   Connects to an rtl_tcp daemon listening over a network or localhost socket:")
    print("   * Start the server:  rtl_tcp -a 127.0.0.1 -p 1234")
    print("   * Launch Grabber:    python ApexBOSGrabber.py --rtl-tcp --host 127.0.0.1 --port 1234\n")

    print("3. DIRECT RTLSDR MODE (Python pyrtlsdr package):")
    print("   Communicates directly with the USB dongle in Python via ctypes:")
    print(f"   * Install package:   {py_cmd} -m pip install pyrtlsdr")
    print("   * Windows note:      Requires 'rtlsdr.dll' and 'libusb-1.0.dll' in PATH or")
    print("                        current folder, plus WinUSB driver installed via Zadig.")
    print("   * Launch Grabber:    python ApexBOSGrabber.py --rtlsdr\n")

    print("4. STDIN STREAM MODE (Pipe raw I/Q from external tool or file):")
    print("   * Run:               rtl_sdr -f 220712500 -s 192000 - | python ApexBOSGrabber.py --stdin")
    print("=" * 78 + "\n")


def resolve_sdr_stream_kind(cli_args) -> str:
    """
    Resolves the SDR stream kind based on CLI flags, defaulting to pipe,
    and automatically probing alternate modes if pipe is unavailable.
    """
    # 1. Explicit user selections
    if getattr(cli_args, "stdin", False):
        return "stdin"
    if getattr(cli_args, "rtl_tcp", False):
        return "tcp"
    if getattr(cli_args, "rtlsdr", False):
        return "rtlsdr"
    if getattr(cli_args, "pipe", False):
        return "pipe"

    # 2. Default: prefer pipe if rtl_sdr executable is actually present
    custom_bin = getattr(cli_args, "rtl_sdr_bin", None)
    if is_rtl_sdr_available(custom_bin):
        return "pipe"

    # 3. Auto-detection fallback:
    # 3a. Check if an RTL-TCP server is actively listening on host:port
    host = getattr(cli_args, "host", "127.0.0.1")
    port = getattr(cli_args, "port", 1234)
    if check_rtl_tcp_running(host, port):
        print(f"[*] Auto-detected active RTL-TCP server at {host}:{port} -> selecting TCP mode")
        return "tcp"

    # 3b. Check if rtlsdr (pyrtlsdr) package is installed and operational
    rtlsdr_ok, _ = is_rtlsdr_package_available()
    if rtlsdr_ok:
        print("[*] Auto-detected installed 'rtlsdr' package -> selecting direct RTLSDR mode")
        return "rtlsdr"

    # 3c. Check if rtl_tcp executable is available in PATH
    if is_rtl_tcp_available():
        print("[*] Auto-detected 'rtl_tcp' executable in system PATH -> selecting TCP mode")
        return "tcp"

    # If none are available, default to pipe so that when run() is called,
    # the setup guide is displayed.
    return "pipe"


def parse_gain_value(gain_arg) -> Tuple[float, int]:
    """
    Parses gain argument into (gain_db: float, gain_tenths: int).
    Handles 'auto', 'agc', 0, values in dB (e.g. 42 or 42.0), and tenths of dB (e.g. 420).
    """
    if gain_arg is None:
        return 0.0, 0
    if isinstance(gain_arg, str):
        g_clean = gain_arg.strip().lower()
        if g_clean in ("auto", "agc", "0", "0.0"):
            return 0.0, 0
        try:
            val = float(g_clean)
        except ValueError:
            return 0.0, 0
    else:
        val = float(gain_arg)

    if val <= 0.0:
        return 0.0, 0
    if val <= 50.0:
        gain_db = float(val)
        gain_tenths = int(round(val * 10.0))
    else:
        gain_tenths = int(round(val))
        gain_db = round(gain_tenths / 10.0, 1)
    return gain_db, gain_tenths


class SubprocessPipeSource:
    """Spawns rtl_sdr as a subprocess and reads stdout."""
    def __init__(self, binary_path: str, center_freq: int, sample_rate: int, gain, ppm: int, device_idx: int = 0, bias_tee: bool = True):
        self.bin = binary_path
        self.center_freq = int(center_freq)
        self.sample_rate = int(sample_rate)
        self.gain_db, self.gain_tenths = parse_gain_value(gain)
        self.ppm = int(ppm)
        self.device = int(device_idx)
        self.bias_tee = bool(bias_tee)
        self.process = None
        self._spawn()

    def _spawn(self):
        gain_str = "0" if self.gain_db <= 0 else f"{self.gain_db:.1f}"
        cmd = [
            self.bin,
            "-f", str(self.center_freq),
            "-s", str(self.sample_rate),
            "-g", gain_str,
            "-p", str(self.ppm),
            "-d", str(self.device),
        ]
        if self.bias_tee:
            cmd.extend(["-b", "1"])
        cmd.append("-")

        flags = 0x08000000 if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                bufsize=262144,
            )
        except Exception as e:
            if self.bias_tee:
                # Retry without -b flag if older rtl_sdr binary rejects it
                try:
                    cmd_fallback = [
                        self.bin,
                        "-f", str(self.center_freq),
                        "-s", str(self.sample_rate),
                        "-g", gain_str,
                        "-p", str(self.ppm),
                        "-d", str(self.device),
                        "-",
                    ]
                    self.process = subprocess.Popen(
                        cmd_fallback,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        creationflags=flags,
                        bufsize=262144,
                    )
                    return
                except Exception:
                    pass
            self.process = None
            raise RuntimeError(f"Failed to start rtl_sdr ({self.bin}): {e}")

    def update_hardware_params(self, freq=None, gain=None, ppm=None, bias_tee=None):
        restart_needed = False
        if freq is not None and int(freq) != self.center_freq:
            self.center_freq = int(freq)
            restart_needed = True
        if gain is not None:
            g_db, g_tenths = parse_gain_value(gain)
            if (g_db, g_tenths) != (self.gain_db, self.gain_tenths):
                self.gain_db, self.gain_tenths = g_db, g_tenths
                restart_needed = True
        if ppm is not None and int(ppm) != self.ppm:
            self.ppm = int(ppm)
            restart_needed = True
        if bias_tee is not None and bool(bias_tee) != self.bias_tee:
            self.bias_tee = bool(bias_tee)
            restart_needed = True

        if restart_needed and self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=0.5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self._spawn()

    def read_exact_bytes(self, byte_count: int) -> bytes:
        if not self.process or not self.process.stdout:
            return b""
        buf = bytearray(byte_count)
        view = memoryview(buf)
        pos = 0
        while pos < byte_count:
            chunk = self.process.stdout.read(byte_count - pos)
            if not chunk:
                break
            view[pos:pos + len(chunk)] = chunk
            pos += len(chunk)
        return bytes(buf[:pos])

    def close(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=0.5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None


class StandardInputStreamSource:
    """Reads raw samples from standard input."""
    def __init__(self):
        pass

    def update_hardware_params(self, freq=None, gain=None, ppm=None, bias_tee=None):
        pass

    def read_exact_bytes(self, byte_count: int) -> bytes:
        buf = bytearray(byte_count)
        view = memoryview(buf)
        pos = 0
        while pos < byte_count:
            chunk = sys.stdin.buffer.read(byte_count - pos)
            if not chunk:
                break
            view[pos:pos + len(chunk)] = chunk
            pos += len(chunk)
        return bytes(buf[:pos])

    def close(self):
        pass


class DirectRtlTcpSource:
    """Connects to RTL-TCP server socket directly."""
    def __init__(self, host: str, port: int, center_freq: int, sample_rate: int, gain, ppm: int, bias_tee: bool = True):
        self.host = host
        self.port = port
        self.center_freq = center_freq
        self.sample_rate = sample_rate
        self.gain_db, self.gain_tenths = parse_gain_value(gain)
        self.ppm = ppm
        self.bias_tee = bool(bias_tee)
        self.sock = None
        self._connect()

    def _connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Configure large TCP receive buffer (2 MB) on Windows/Linux to prevent packet drops under load
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        except Exception:
            pass
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

        self.sock.settimeout(5.0)
        self.sock.connect((self.host, self.port))
        hdr = b""
        while len(hdr) < 12:
            chunk = self.sock.recv(12 - len(hdr))
            if not chunk:
                raise ConnectionError("RTL-TCP closed during handshake")
            hdr += chunk

        # Hardware safety: RTL2832U chip physically rejects sample rates <= 225 kHz or between 300-900 kHz.
        # Ensure hardware receives a valid multiple of 192,000 (minimum 960,000 S/s).
        sdr_hardware_rate = self.sample_rate
        if sdr_hardware_rate < 960000:
            sdr_hardware_rate = 960000
        elif sdr_hardware_rate % 192000 != 0:
            sdr_hardware_rate = int(math.ceil(sdr_hardware_rate / 192000.0) * 192000)

        self._send_command(0x02, sdr_hardware_rate)
        self._send_command(0x01, self.center_freq)
        self._send_command(0x05, self.ppm, signed=True)
        if self.gain_tenths == 0:
            self._send_command(0x03, 0)
            self._send_command(0x08, 1)
        else:
            self._send_command(0x03, 1)
            self._send_command(0x04, self.gain_tenths)
        if self.bias_tee:
            try:
                self._send_command(0x0E, 1)
            except Exception:
                pass

        # Allow rtl_tcp a moment to apply PLL registers, then drain any stale pre-tune buffers
        time.sleep(0.05)
        self.sock.setblocking(False)
        try:
            while True:
                drain = self.sock.recv(65536)
                if not drain:
                    break
        except (BlockingIOError, socket.error):
            pass
        self.sock.setblocking(True)
        self.sock.settimeout(2.0)

    def _send_command(self, cmd_id: int, arg_val: int, signed: bool = False):
        if not self.sock:
            return
        if signed:
            msg = bytes([cmd_id]) + struct.pack(">i", int(arg_val))
        else:
            msg = bytes([cmd_id]) + struct.pack(">I", int(arg_val) & 0xFFFFFFFF)
        self.sock.sendall(msg)

    def update_hardware_params(self, freq=None, gain=None, ppm=None, bias_tee=None):
        if freq is not None:
            self.center_freq = int(freq)
            self._send_command(0x01, self.center_freq)
        if gain is not None:
            self.gain_db, self.gain_tenths = parse_gain_value(gain)
            if self.gain_tenths == 0:
                self._send_command(0x03, 0)
                self._send_command(0x08, 1)
            else:
                self._send_command(0x03, 1)
                self._send_command(0x04, self.gain_tenths)
        if ppm is not None:
            self.ppm = int(ppm)
            self._send_command(0x05, self.ppm, signed=True)
        if bias_tee is not None:
            self.bias_tee = bool(bias_tee)
            try:
                self._send_command(0x0E, 1 if self.bias_tee else 0)
            except Exception:
                pass

    def read_exact_bytes(self, byte_count: int) -> bytes:
        if not self.sock:
            return b""
        buf = bytearray(byte_count)
        view = memoryview(buf)
        pos = 0
        while pos < byte_count:
            try:
                chunk = self.sock.recv(byte_count - pos)
            except socket.timeout:
                continue
            if not chunk:
                break
            view[pos:pos + len(chunk)] = chunk
            pos += len(chunk)
        return bytes(buf[:pos])

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


class DirectRtlSdrSource:
    """
    Direct in-process RTL-SDR hardware interface utilizing the 'rtlsdr' (pyrtlsdr) package.
    Works across Linux, macOS, and Windows (with rtlsdr.dll and WinUSB driver).
    """
    def __init__(self, center_freq: int, sample_rate: int, gain, ppm: int, device_idx: int = 0, bias_tee: bool = True, sdr_client=None):
        self.device_idx = int(device_idx)
        self.center_freq = int(center_freq)
        self.sample_rate = int(sample_rate)
        self.gain_db, self.gain_tenths = parse_gain_value(gain)
        self.ppm = int(ppm)
        self.bias_tee = bool(bias_tee)
        self.sdr = sdr_client
        self._buffer = bytearray()
        if self.sdr is None:
            self._init_device()

    def _init_device(self):
        try:
            from rtlsdr import RtlSdr
        except ImportError as e:
            raise RuntimeError(
                f"The 'rtlsdr' package is not installed: {e}\n"
                f"Please run: {sys.executable} -m pip install pyrtlsdr"
            )
        try:
            self.sdr = RtlSdr(device_index=self.device_idx)
        except Exception as e:
            raise RuntimeError(f"Failed to open RTL-SDR device index {self.device_idx}: {e}")

        # Hardware safety: Ensure hardware rate complies with RTL2832U constraints
        hw_rate = self.sample_rate
        if hw_rate < 960000:
            hw_rate = 960000
        elif hw_rate % 192000 != 0:
            hw_rate = int(math.ceil(hw_rate / 192000.0) * 192000)

        self.sdr.sample_rate = hw_rate
        self.sdr.center_freq = self.center_freq
        self.sdr.freq_correction = self.ppm

        if self.gain_tenths == 0:
            self.sdr.gain = 'auto'
        else:
            self.sdr.gain = self.gain_db

        if self.bias_tee:
            try:
                if hasattr(self.sdr, "set_bias_tee"):
                    self.sdr.set_bias_tee(True)
            except Exception:
                pass

    def update_hardware_params(self, freq=None, gain=None, ppm=None, bias_tee=None):
        if not self.sdr:
            return
        if freq is not None and int(freq) != self.center_freq:
            self.center_freq = int(freq)
            try:
                self.sdr.center_freq = self.center_freq
            except Exception:
                pass
        if gain is not None:
            g_db, g_tenths = parse_gain_value(gain)
            if (g_db, g_tenths) != (self.gain_db, self.gain_tenths):
                self.gain_db, self.gain_tenths = g_db, g_tenths
                try:
                    if self.gain_tenths == 0:
                        self.sdr.gain = 'auto'
                    else:
                        self.sdr.gain = self.gain_db
                except Exception:
                    pass
        if ppm is not None and int(ppm) != self.ppm:
            self.ppm = int(ppm)
            try:
                self.sdr.freq_correction = self.ppm
            except Exception:
                pass
        if bias_tee is not None and bool(bias_tee) != self.bias_tee:
            self.bias_tee = bool(bias_tee)
            try:
                if hasattr(self.sdr, "set_bias_tee"):
                    self.sdr.set_bias_tee(self.bias_tee)
            except Exception:
                pass

    def read_exact_bytes(self, byte_count: int) -> bytes:
        if not self.sdr:
            return b""
        chunk_align = 16384
        while len(self._buffer) < byte_count:
            needed = byte_count - len(self._buffer)
            read_size = max(chunk_align, ((needed + 511) // 512) * 512)
            try:
                raw = self.sdr.read_bytes(read_size)
                if not raw:
                    break
                self._buffer.extend(raw)
            except Exception:
                break
        if len(self._buffer) < byte_count:
            res = bytes(self._buffer)
            self._buffer.clear()
            return res
        res = bytes(self._buffer[:byte_count])
        del self._buffer[:byte_count]
        return res

    def close(self):
        if self.sdr:
            try:
                self.sdr.close()
            except Exception:
                pass
            self.sdr = None
        self._buffer.clear()


# ---------------------------------------------------------------------------
# Cross-Platform Queue Utilities
# ---------------------------------------------------------------------------
def safe_queue_size(q, default: int = 0) -> int:
    """
    Safely retrieves multiprocessing.Queue size.
    Handles platforms like macOS (Darwin) where POSIX sem_getvalue() is unsupported
    and calling qsize() raises NotImplementedError.
    """
    try:
        return q.qsize()
    except (NotImplementedError, AttributeError, OSError):
        return default


# ---------------------------------------------------------------------------
# Worker Demodulator Process
# ---------------------------------------------------------------------------
def native_demod_worker(
    worker_index: int,
    zmq_bind_port: int,
    udp_dest_port: int,
    assigned_channel_list: list,
    initial_center_freq: int,
    input_sample_rate: int,
    max_sync_errors: int,
    data_queue: multiprocessing.Queue,
    control_queue: multiprocessing.Queue,
    metrics_queue: multiprocessing.Queue,
    shutdown_flag: multiprocessing.Event,
    no_rssi: bool = False,
    afc_enabled: bool = True,
    afc_limit_hz: float = 500.0,
    dedup_ttl_sec: float = 1.0,
):
    """
    Dedicated worker process implementing native demodulation & decoding:
    - Channelizes assigned channels using ContinuousChannelizer.
    - Sweeps 12 sampling phases for both FR (192 k) and HR (96 k).
    - Vectorized 64-bit unique word correlation.
    - Viterbi K=7 rate 1/2 header decoding with fast-path header caching.
    - RS(16, 12) de-interleaving and CRC-16/ARC verification supporting up to 350+ bytes.
    - ZeroMQ PUB (MosFET b'I' binary + raw hex + JSON) and UDP datagram broadcasting.
    """
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, AttributeError):
        pass

    import zmq

    # Setup ZeroMQ Publisher Socket
    zmq_ctx = zmq.Context()
    pub_socket = zmq_ctx.socket(zmq.PUB)
    pub_socket.setsockopt(zmq.LINGER, 0)
    pub_bound = False
    try:
        pub_socket.bind(f"tcp://*:{zmq_bind_port}")
        pub_bound = True
    except Exception as e:
        metrics_queue.put({
            "type": "error",
            "worker_idx": worker_index,
            "msg": f"Failed to bind ZMQ port {zmq_bind_port}: {e}",
        })

    # Setup UDP Socket
    udp_socket = None
    try:
        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except Exception:
        udp_socket = None

    active_channels = list(assigned_channel_list)
    current_center_freq = initial_center_freq
    channelizer = ContinuousChannelizer(input_sample_rate)

    # Rolling sample buffers per channel (retaining 200 ms continuous for bursts up to 350+ bytes)
    channel_buffers_fr = {ch: np.zeros(0, dtype=np.complex64) for ch in active_channels}
    channel_buffers_hr = {ch: np.zeros(0, dtype=np.complex64) for ch in active_channels}
    channel_freq_offsets = {ch: 0.0 for ch in active_channels}
    afc_enabled = bool(afc_enabled)
    afc_limit_hz = abs(float(afc_limit_hz))
    recent_packet_cache = collections.deque(maxlen=250)

    total_bursts_detected = 0
    total_packets_decoded = 0
    total_dropped_bursts = 0
    total_samples_proc = 0
    total_banks_proc = 0
    total_sync_detected = 0
    total_headers_good = 0
    total_headers_bad = 0
    total_rs_good = 0
    total_rs_bad = 0
    total_crc_good = 0
    total_crc_bad = 0
    peak_burst_rssi = -100.0
    interval_peak_burst_rssi = -100.0
    worker_burst_rssi_history = collections.deque()

    last_reported_sync = 0
    last_reported_hdr_g = 0
    last_reported_hdr_b = 0
    last_reported_rs_g = 0
    last_reported_rs_b = 0
    last_reported_crc_g = 0
    last_reported_crc_b = 0
    last_metrics_time = time.time()

    # Drain any stale startup chunks that accumulated while the worker process was initializing
    while not data_queue.empty():
        try:
            data_queue.get_nowait()
        except queue.Empty:
            break

    try:
        while not shutdown_flag.is_set():
            # 1. Check control queue
            while not control_queue.empty():
                try:
                    req = control_queue.get_nowait()
                    if req.get("cmd") == "update_channels":
                        active_channels = list(req["channels"])
                        current_center_freq = req["center_freq"]
                        channelizer.reset()
                        channel_buffers_fr = {ch: np.zeros(0, dtype=np.complex64) for ch in active_channels}
                        channel_buffers_hr = {ch: np.zeros(0, dtype=np.complex64) for ch in active_channels}
                        for c in active_channels:
                            if c not in channel_freq_offsets:
                                channel_freq_offsets[c] = 0.0
                    elif req.get("cmd") == "set_afc":
                        if "enabled" in req:
                            afc_enabled = bool(req["enabled"])
                            if not afc_enabled:
                                for c in channel_freq_offsets:
                                    channel_freq_offsets[c] = 0.0
                        if "limit_hz" in req:
                            afc_limit_hz = abs(float(req["limit_hz"]))
                            for c in channel_freq_offsets:
                                channel_freq_offsets[c] = max(-afc_limit_hz, min(afc_limit_hz, channel_freq_offsets[c]))
                    elif req.get("cmd") == "set_dedup":
                        if "ttl_sec" in req:
                            dedup_ttl_sec = max(0.0, float(req["ttl_sec"]))
                    elif req.get("cmd") == "reset_peak_rssi":
                        peak_burst_rssi = -100.0
                        interval_peak_burst_rssi = -100.0
                        worker_burst_rssi_history.clear()
                except queue.Empty:
                    break

            # 2. Receive sample bank from reader queue
            try:
                raw_bank, bank_rssi = data_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            total_samples_proc += len(raw_bank) // 2
            total_banks_proc += 1
            queue_backlog = safe_queue_size(data_queue, 0)

            # 3. Channelize and demodulate across assigned channels with per-channel frequency offset compensation
            for ch in active_channels:
                ch_df = channel_freq_offsets.get(ch, 0.0) if afc_enabled else 0.0
                y_fr, y_hr = channelizer.channelize(raw_bank, current_center_freq, ch, ch_freq_offset_hz=ch_df)

                # Maintain rolling buffer (expanded to 500ms FR / 600ms HR to capture long bursts up to 350+ bytes across chunks)
                full_fr = np.concatenate([channel_buffers_fr.get(ch, np.zeros(0, dtype=np.complex64)), y_fr])
                full_hr = np.concatenate([channel_buffers_hr.get(ch, np.zeros(0, dtype=np.complex64)), y_hr])
                if len(full_fr) > 96000:
                    full_fr = full_fr[-96000:]
                if len(full_hr) > 57600:
                    full_hr = full_hr[-57600:]
                channel_buffers_fr[ch] = full_fr
                channel_buffers_hr[ch] = full_hr

                # Test both Full-Rate (192 k, sps=12) and Half-Rate (96 k, sps=12)
                rate_modes = [
                    ("FR", full_fr, len(y_fr)),
                    ("HR", full_hr, len(y_hr)),
                ]

                for mode_label, baseband_buf, new_len in rate_modes:
                    if len(baseband_buf) < 144:  # At least 12 symbols
                        continue

                    current_baud = 16000.0 if mode_label == "FR" else 8000.0
                    burst_rssi = compute_signal_power_dbfs(baseband_buf[-new_len:])
                    if burst_rssi > interval_peak_burst_rssi:
                        interval_peak_burst_rssi = burst_rssi
                    if burst_rssi > peak_burst_rssi:
                        peak_burst_rssi = burst_rssi

                    # Load shedding only when queue backlog is near full (> 15) and burst is at noise floor (< -65 dBFS)
                    if queue_backlog > 15 and burst_rssi < -65.0:
                        total_dropped_bursts += 1
                        continue

                    # Sweep all 12 sampling phases
                    sps = 12
                    for phase_offset in range(sps):
                        bits, diff = demodulate_pi4_dqpsk(
                            baseband_buf,
                            sps,
                            phase_offset,
                            return_diff=True,
                        )
                        if len(bits) < SYNC_WORD_LEN_BITS:
                            continue

                        sync_hits = locate_unique_word_hits(bits, max_sync_errors)
                        total_bursts_detected += len(sync_hits)
                        total_sync_detected += len(sync_hits)

                        for bit_pos, sync_err in sync_hits:
                            sym_pos = bit_pos // 2
                            # Estimate residual frequency offset on unique word
                            if sym_pos + 32 <= len(diff):
                                burst_df, mean_err, is_rev = estimate_sync_frequency_offset(
                                    diff[sym_pos:sym_pos + 32],
                                    baud_rate=current_baud,
                                )
                            else:
                                burst_df, mean_err, is_rev = 0.0, 0.0, False

                            # Pack unique word (64 bits) + header (48 bits) + up to 512 bytes (supporting up to 350+ byte bursts)
                            max_pack_bytes = SYNC_WORD_LEN_BITS + 48 + 512 * 8
                            packed = pack_bit_sequence_msb(bits, bit_pos, max_pack_bytes)
                            is_valid, pkt_type, payload, stage_info = fec_decode_itc_packet(packed, return_details=True)

                            # AFC Fine De-rotation: If initial decode failed and residual phase rotation > 2.3 deg,
                            # attempt fine de-rotation of burst differential symbols
                            if not is_valid and abs(mean_err) > 0.04 and sym_pos + 32 <= len(diff):
                                max_syms = min(len(diff) - sym_pos, 32 + 24 + 512 * 4)
                                diff_slice = diff[sym_pos:sym_pos + max_syms]
                                diff_corrected = diff_slice * np.exp(-1j * mean_err)
                                c_b0 = (diff_corrected.imag > 0.0).astype(np.uint8)
                                c_b1 = (diff_corrected.real < 0.0).astype(np.uint8)
                                c_bits = np.empty(len(c_b0) * 2, dtype=np.uint8)
                                c_bits[0::2] = c_b0
                                c_bits[1::2] = c_b1
                                packed_c = pack_bit_sequence_msb(c_bits, 0, max_pack_bytes)
                                is_valid_c, pkt_type_c, payload_c, stage_info_c = fec_decode_itc_packet(packed_c, return_details=True)
                                if is_valid_c:
                                    is_valid = True
                                    pkt_type = pkt_type_c
                                    payload = payload_c
                                    stage_info = stage_info_c

                            # Update channel's running frequency offset (EMA) on confirmed CRC
                            if afc_enabled and is_valid and stage_info.get("crc_ok"):
                                cur_avg = channel_freq_offsets.get(ch, 0.0)
                                total_df = cur_avg + burst_df
                                channel_freq_offsets[ch] = max(-afc_limit_hz, min(afc_limit_hz, round(0.90 * cur_avg + 0.10 * total_df, 1)))

                            # 1. Header status
                            if not stage_info["header_ok"]:
                                total_headers_bad += 1
                                continue
                            total_headers_good += 1

                            # 2. RS payload status
                            if not stage_info["rs_ok"]:
                                total_rs_bad += 1
                                continue
                            total_rs_good += 1

                            # 3. CRC integrity status
                            if not stage_info["crc_ok"]:
                                total_crc_bad += 1
                                continue
                            total_crc_good += 1

                            if not is_valid or not payload:
                                continue

                            # Deduplication key and 1.0-second expiration check
                            dedup_key = (ch, pkt_type, payload[:20])
                            now_t = time.time()
                            if dedup_ttl_sec > 0:
                                # Clean up expired cache entries older than dedup_ttl_sec (1.0s default)
                                while recent_packet_cache and now_t - recent_packet_cache[0][1] >= dedup_ttl_sec:
                                    recent_packet_cache.popleft()

                                # Suppress duplicates seen within the expiration window (< 1.0s).
                                # After 1.0 second has elapsed, a packet must be emitted even if duplicate is cached.
                                if any(k == dedup_key and now_t - t < dedup_ttl_sec for k, t in recent_packet_cache):
                                    continue
                                recent_packet_cache.append((dedup_key, now_t))

                            total_packets_decoded += 1

                            # MosFET frame structure: b'I' (0x49), length (2 bytes BE), type (1 byte), flags (0x00), payload
                            mosfet_frame = bytes([
                                0x49,
                                (len(payload) >> 8) & 0xFF,
                                len(payload) & 0xFF,
                                pkt_type,
                                0x00,
                            ]) + payload
                            raw_hex_str = mosfet_frame.hex().upper()
                            pure_hex_bytes = raw_hex_str.encode("ascii")

                            # Parse telemetry
                            telemetry = extract_packet_telemetry(pkt_type, payload)

                            # Build metadata JSON
                            eff_ch_offset = channel_freq_offsets.get(ch, 0.0) if afc_enabled else 0.0
                            total_burst_df = round(eff_ch_offset + burst_df, 1)
                            metadata = {
                                "channel": ch,
                                "freq_hz": itc_channel_frequency(ch),
                                "freq_offset_hz": total_burst_df,
                                "ch_avg_offset_hz": round(eff_ch_offset, 1),
                                "rssi_dbfs": round(burst_rssi, 2),
                                "rssi": round(burst_rssi, 2),
                                "snr_db": round(max(0.0, burst_rssi + 45.0), 1),
                                "rate": mode_label,
                                "duplex": "Full-Duplex (32k)" if mode_label == "FR" else "Half-Duplex (16k)",
                                "type": pkt_type,
                                "type_name": telemetry.get("type_name", ""),
                                "sync_errors": sync_err,
                                "length": len(payload),
                                "hex": raw_hex_str,
                                "timestamp": datetime.now().isoformat(),
                                "telemetry": telemetry,
                                "stats": {
                                    "sync_detected": total_sync_detected,
                                    "headers_good": total_headers_good,
                                    "headers_bad": total_headers_bad,
                                    "rs_good": total_rs_good,
                                    "rs_bad": total_rs_bad,
                                    "crc_good": total_crc_good,
                                    "crc_bad": total_crc_bad,
                                },
                            }
                            meta_bytes = json.dumps(metadata).encode("utf-8")

                            # Determine ZMQ & UDP payloads according to no_rssi configuration
                            if no_rssi:
                                hex_output_bytes = pure_hex_bytes
                                zmq_frames = [mosfet_frame, pure_hex_bytes, meta_bytes]
                            else:
                                hex_rssi_str = f"{raw_hex_str} RSSI:{burst_rssi:.1f}"
                                hex_output_bytes = hex_rssi_str.encode("ascii")
                                rssi_frame = f"RSSI:{burst_rssi:.1f}".encode("ascii")
                                zmq_frames = [mosfet_frame, hex_output_bytes, meta_bytes, rssi_frame]

                            # 1. Publish to ZMQ PUB socket
                            if pub_socket is not None:
                                if not pub_bound:
                                    try:
                                        pub_socket.bind(f"tcp://*:{zmq_bind_port}")
                                        pub_bound = True
                                    except Exception:
                                        pass
                                if pub_bound:
                                    try:
                                        pub_socket.send_multipart(zmq_frames, flags=zmq.NOBLOCK)
                                    except Exception:
                                        pass

                            # 2. Transmit to UDP socket
                            if udp_socket:
                                try:
                                    udp_socket.sendto(hex_output_bytes + b"\n", ("127.0.0.1", udp_dest_port))
                                except Exception:
                                    pass

                            # Notify stats queue
                            metrics_queue.put({
                                "type": "packet_event",
                                "worker_idx": worker_index,
                                "channel": ch,
                                "rate": mode_label,
                                "pkt_type": pkt_type,
                                "desc": telemetry.get("type_name", f"Type {pkt_type:#04x}"),
                                "rssi": burst_rssi,
                                "snr": metadata["snr_db"],
                                "hex": raw_hex_str,
                                "telemetry": telemetry,
                                "time_str": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                                "freq_offset_hz": total_burst_df,
                            })

            # Periodic worker metrics report with deltas for sliding window
            now = time.time()
            if now - last_metrics_time >= 0.5:
                delta_sync = total_sync_detected - last_reported_sync
                delta_hdr_g = total_headers_good - last_reported_hdr_g
                delta_hdr_b = total_headers_bad - last_reported_hdr_b
                delta_rs_g = total_rs_good - last_reported_rs_g
                delta_rs_b = total_rs_bad - last_reported_rs_b
                delta_crc_g = total_crc_good - last_reported_crc_g
                delta_crc_b = total_crc_bad - last_reported_crc_b

                last_reported_sync = total_sync_detected
                last_reported_hdr_g = total_headers_good
                last_reported_hdr_b = total_headers_bad
                last_reported_rs_g = total_rs_good
                last_reported_rs_b = total_rs_bad
                last_reported_crc_g = total_crc_good
                last_reported_crc_b = total_crc_bad

                if interval_peak_burst_rssi > -99.0:
                    worker_burst_rssi_history.append((now, interval_peak_burst_rssi))
                    interval_peak_burst_rssi = -100.0
                while worker_burst_rssi_history and (now - worker_burst_rssi_history[0][0] > 60.0):
                    worker_burst_rssi_history.popleft()
                peak_burst_rssi = max((x[1] for x in worker_burst_rssi_history), default=-100.0)

                metrics_queue.put({
                    "type": "worker_report",
                    "worker_idx": worker_index,
                    "bursts": total_bursts_detected,
                    "packets": total_packets_decoded,
                    "dropped": total_dropped_bursts,
                    "peak_rssi": peak_burst_rssi,
                    "backlog": queue_backlog,
                    "samples_proc": total_samples_proc,
                    "banks_proc": total_banks_proc,
                    "sync_detected": total_sync_detected,
                    "headers_good": total_headers_good,
                    "headers_bad": total_headers_bad,
                    "rs_good": total_rs_good,
                    "rs_bad": total_rs_bad,
                    "crc_good": total_crc_good,
                    "crc_bad": total_crc_bad,
                    "delta_sync": delta_sync,
                    "delta_hdr_g": delta_hdr_g,
                    "delta_hdr_b": delta_hdr_b,
                    "delta_rs_g": delta_rs_g,
                    "delta_rs_b": delta_rs_b,
                    "delta_crc_g": delta_crc_g,
                    "delta_crc_b": delta_crc_b,
                    "channel_freq_offsets": dict(channel_freq_offsets) if afc_enabled else {c: 0.0 for c in channel_freq_offsets},
                })
                last_metrics_time = now

    except KeyboardInterrupt:
        pass
    finally:
        try:
            pub_socket.close()
            zmq_ctx.term()
        except Exception:
            pass
        if udp_socket:
            try:
                udp_socket.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# SDR Reader Worker Process
# ---------------------------------------------------------------------------
def sdr_stream_reader_worker(
    stream_kind: str,
    sdr_binary: str,
    host_addr: str,
    tcp_port: int,
    device_num: int,
    initial_center_freq: int,
    sample_rate: int,
    tuner_gain,
    ppm_correction: int,
    chunk_samples: int,
    worker_queues: list,
    control_queue: multiprocessing.Queue,
    metrics_queue: multiprocessing.Queue,
    shutdown_flag: multiprocessing.Event,
    bias_tee: bool = True,
):
    """
    Dedicated SDR reader process:
    Acquires sample chunks from rtl_sdr pipe, stdin, or rtl_tcp and fans them out.
    """
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, AttributeError):
        pass

    stream = None
    center_freq = initial_center_freq
    gain = tuner_gain
    ppm = ppm_correction
    bias = bias_tee
    bytes_per_chunk = chunk_samples * 2

    total_iq_samples = 0
    total_iq_bytes = 0
    total_chunks = 0
    last_reader_report_time = time.time()

    try:
        if stream_kind == "pipe":
            stream = SubprocessPipeSource(sdr_binary, center_freq, sample_rate, gain, ppm, device_num, bias_tee=bias)
        elif stream_kind == "stdin":
            stream = StandardInputStreamSource()
        elif stream_kind == "tcp":
            stream = DirectRtlTcpSource(host_addr, tcp_port, center_freq, sample_rate, gain, ppm, bias_tee=bias)
        elif stream_kind == "rtlsdr":
            stream = DirectRtlSdrSource(center_freq, sample_rate, gain, ppm, device_num, bias_tee=bias)
    except Exception as e:
        metrics_queue.put({
            "type": "error",
            "worker_idx": -1,
            "msg": f"SDR stream init failed ({stream_kind}): {e}",
        })
        shutdown_flag.set()
        return

    g_db, g_tenths = parse_gain_value(gain)
    metrics_queue.put({
        "type": "reader_status",
        "stream": stream_kind,
        "center_freq": center_freq,
        "gain_db": g_db,
        "gain": g_tenths,
        "ppm": ppm,
        "bias_tee": bias,
    })

    try:
        while not shutdown_flag.is_set():
            # Process hardware command requests
            while not control_queue.empty():
                try:
                    cmd_obj = control_queue.get_nowait()
                    cmd = cmd_obj.get("cmd")
                    if cmd == "set_freq":
                        center_freq = cmd_obj["freq"]
                        if stream:
                            stream.update_hardware_params(freq=center_freq)
                    elif cmd == "set_gain":
                        gain = cmd_obj["gain"]
                        if stream:
                            stream.update_hardware_params(gain=gain)
                    elif cmd == "set_ppm":
                        ppm = cmd_obj["ppm"]
                        if stream:
                            stream.update_hardware_params(ppm=ppm)
                    elif cmd == "set_bias_tee":
                        bias = bool(cmd_obj["bias_tee"])
                        if stream:
                            stream.update_hardware_params(bias_tee=bias)
                except queue.Empty:
                    break

            raw_chunk = stream.read_exact_bytes(bytes_per_chunk)
            if not raw_chunk or len(raw_chunk) < bytes_per_chunk:
                time.sleep(0.01)
                continue

            chunk_len = len(raw_chunk)
            total_iq_bytes += chunk_len
            total_iq_samples += chunk_len // 2
            total_chunks += 1

            bank_rssi = compute_signal_power_dbfs(raw_chunk)

            # Fan out to worker queues with automatic backlog drain
            for q in worker_queues:
                try:
                    if q.qsize() > 6:
                        while q.qsize() > 2:
                            try:
                                q.get_nowait()  # Drain stale backlog to keep pipeline real-time
                            except queue.Empty:
                                break
                except (NotImplementedError, AttributeError, OSError):
                    pass

                try:
                    q.put_nowait((raw_chunk, bank_rssi))
                except queue.Full:
                    # On platforms like macOS where qsize() raises NotImplementedError, or under sudden burst saturation,
                    # drain oldest banks to ensure pipeline remains strictly real-time
                    try:
                        for _ in range(4):
                            q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait((raw_chunk, bank_rssi))
                    except queue.Full:
                        pass

            now_t = time.time()
            if now_t - last_reader_report_time >= 0.5:
                metrics_queue.put({
                    "type": "reader_report",
                    "samples_read": total_iq_samples,
                    "bytes_read": total_iq_bytes,
                    "chunks_read": total_chunks,
                })
                last_reader_report_time = now_t

    except KeyboardInterrupt:
        pass
    finally:
        if stream:
            stream.close()


# ---------------------------------------------------------------------------
# Cross-Platform Terminal Support (Windows 11, Linux, macOS)
# ---------------------------------------------------------------------------
def init_cross_platform_terminal(no_color_flag: bool = False) -> bool:
    """
    Initializes terminal capabilities for Windows 10/11, Linux, and macOS.
    Enables ANSI Virtual Terminal Processing on Windows if available.
    Returns True if ANSI escape sequences (colors, cursor positioning) are supported.
    """
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if no_color_flag or os.environ.get("NO_COLOR"):
        return False

    if os.name == "nt":
        vt_ok = False
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            # 1. Try STD_OUTPUT_HANDLE (-11)
            h_out = kernel32.GetStdHandle(-11)
            mode = wintypes.DWORD()
            if kernel32.GetConsoleMode(h_out, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                if kernel32.SetConsoleMode(h_out, mode.value | 0x0004):
                    vt_ok = True

            # 2. Also try CONOUT$ handle directly
            h_con = kernel32.CreateFileW("CONOUT$", 0x40000000 | 0x80000000, 2, None, 3, 0, None)
            if h_con and h_con != -1:
                mode_c = wintypes.DWORD()
                if kernel32.GetConsoleMode(h_con, ctypes.byref(mode_c)):
                    if kernel32.SetConsoleMode(h_con, mode_c.value | 0x0004):
                        vt_ok = True
                kernel32.CloseHandle(h_con)
        except Exception:
            pass

        # Windows Terminal (WT_SESSION) or ConEmu/VSCode terminal natively supports VT
        if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
            return True
        return vt_ok

    # Linux / macOS
    if sys.stdout.isatty() or os.environ.get("TERM") not in (None, "", "dumb"):
        return True
    return False


class TerminalCommandListener(threading.Thread):
    """
    Cross-platform interactive terminal command listener for Windows 11, Linux, and macOS.
    Reads commands asynchronously from standard input and dispatches them to the application.
    Maintains an active input buffer so screen updates do not erase what the user is typing.
    Compatible with Windows Terminal (ConPTY), CMD, PowerShell, Linux shells, and macOS Terminal.
    """
    def __init__(self, dispatch_fn, shutdown_flag, app=None):
        super().__init__(name="TerminalCommandListener", daemon=True)
        self.dispatch_fn = dispatch_fn
        self.shutdown_flag = shutdown_flag
        self.app = app
        self._buffer = ""
        self._lock = threading.Lock()

    def get_current_buffer(self) -> str:
        with self._lock:
            return self._buffer

    def run(self):
        # Interactive mode under Windows with console TTY
        is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
        if sys.platform == "win32" and is_tty:
            import msvcrt
            while not self.shutdown_flag.is_set():
                try:
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        if ch in ("\r", "\n"):
                            with self._lock:
                                cmd = self._buffer
                                self._buffer = ""
                            if cmd.strip():
                                self.dispatch_fn(cmd.strip())
                        elif ch in ("\x08", "\x7f"):  # Backspace
                            with self._lock:
                                if self._buffer:
                                    self._buffer = self._buffer[:-1]
                        elif ch == "\x03":  # Ctrl+C
                            self.shutdown_flag.set()
                            break
                        elif ch in ("\x00", "\xe0"):  # Extended key prefix (arrows, fn keys)
                            if msvcrt.kbhit():
                                msvcrt.getwch()
                        elif ch in ("q", "Q") and not self._buffer:
                            self.shutdown_flag.set()
                            break
                        elif ch in ("?", "h", "H") and not self._buffer:
                            self.dispatch_fn("h")
                        elif ord(ch) >= 32:
                            with self._lock:
                                self._buffer += ch
                    else:
                        time.sleep(0.02)
                except (EOFError, KeyboardInterrupt):
                    break
                except Exception:
                    time.sleep(0.05)
            return

        # Native Linux / macOS console keyboard listener using termios and tty
        if sys.platform != "win32" and is_tty:
            try:
                import termios
                import tty
                import select
                fd = sys.stdin.fileno()
                old_settings = termios.tcgetattr(fd)
                try:
                    # Put terminal in cbreak mode and disable local echo
                    tty.setcbreak(fd)
                    cbreak_settings = termios.tcgetattr(fd)
                    cbreak_settings[3] &= ~termios.ECHO
                    termios.tcsetattr(fd, termios.TCSADRAIN, cbreak_settings)

                    while not self.shutdown_flag.is_set():
                        try:
                            rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
                            if not rlist:
                                continue
                            ch = sys.stdin.read(1)
                            if not ch:
                                continue

                            if ch in ("\r", "\n"):
                                with self._lock:
                                    cmd = self._buffer.strip()
                                    self._buffer = ""
                                if cmd:
                                    self.dispatch_fn(cmd)
                            elif ch in ("\x08", "\x7f"):  # Backspace
                                with self._lock:
                                    if self._buffer:
                                        self._buffer = self._buffer[:-1]
                            elif ch == "\x03":  # Ctrl+C
                                self.shutdown_flag.set()
                                break
                            elif ch == "\x1b":  # Escape / ANSI sequence (arrow keys, etc.)
                                while True:
                                    r_esc, _, _ = select.select([sys.stdin], [], [], 0.02)
                                    if r_esc:
                                        sys.stdin.read(1)
                                    else:
                                        break
                            elif ch in ("q", "Q") and not self._buffer:
                                self.shutdown_flag.set()
                                break
                            elif ch in ("?", "h", "H") and not self._buffer:
                                self.dispatch_fn("h")
                            elif ord(ch) >= 32:
                                with self._lock:
                                    self._buffer += ch
                        except Exception:
                            time.sleep(0.02)
                finally:
                    try:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    except Exception:
                        pass
                return
            except Exception:
                pass

        # Fallback line-reading loop for redirected streams, pipes, and non-interactive environments
        eof_count = 0
        while not self.shutdown_flag.is_set():
            try:
                line = sys.stdin.readline()
                if not line:
                    eof_count += 1
                    # Avoid tight spin loop if stdin is closed or redirected from /dev/null
                    if eof_count > 3 and not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
                        time.sleep(0.5)
                        continue
                    time.sleep(0.05)
                    continue
                eof_count = 0
                cmd_line = line.strip()
                if cmd_line:
                    self.dispatch_fn(cmd_line)
            except (EOFError, KeyboardInterrupt):
                break
            except Exception:
                time.sleep(0.05)


# ---------------------------------------------------------------------------
# Lazy PID Auto-Tuner
# ---------------------------------------------------------------------------
class LazyPidAutoTuner:
    """
    Automated Lazy PID Auto-Tuner for ApexBOSGrabber.
    Minimizes combined failure rates in Viterbi header decoding and Reed-Solomon payload correction
    by performing small bounded adjustments in PPM and Tuner Gain.

    Constraints:
    - Adjustments stay strictly within +-4 PPM and +-20 tenths of a dB (2.0 dB) from base settings.
    - Gain tuning is disabled when gain is set to AGC (base_gain_tenths == 0).
    - Evaluates with fast responsive interval (default: 5.0s interval) to allow rapid convergence.
    """
    def __init__(
        self,
        base_ppm: int = 0,
        base_gain_tenths: int = 0,
        enabled: bool = False,
        eval_interval: float = 5.0,
        kp: float = 1.2,
        ki: float = 0.15,
        kd: float = 0.25,
        min_samples: int = 4,
        max_offset_ppm: int = 4,
        max_offset_gain_tenths: int = 20,
    ):
        self.enabled = bool(enabled)
        self.eval_interval = float(eval_interval)
        self.kp = float(kp)
        self.ki = float(ki)
        self.kd = float(kd)
        self.min_samples = int(min_samples)
        self.max_offset_ppm = int(max_offset_ppm)
        self.max_offset_gain_tenths = int(max_offset_gain_tenths)

        self.base_ppm = int(base_ppm)
        self.base_gain_tenths = int(base_gain_tenths)

        # Offsets from base (strictly clamped)
        self.offset_ppm = 0
        self.offset_gain_tenths = 0

        # PID state
        self.integral_error = 0.0
        self.prev_error_rate: Optional[float] = None
        self.last_tune_time = 0.0
        self.last_adjustment_time = 0.0
        self.settling_time = 1.0
        self.last_p = 0.0
        self.last_i = 0.0
        self.last_d = 0.0

        # Search direction and alternation
        self.turn = "ppm"
        self.last_adjusted_param: Optional[str] = None
        self.dir_ppm = 1
        self.dir_gain = 5  # tenths of dB (+0.5 dB)
        self.no_progress_count = 0
        self.last_action_desc = "Initialized"

    def set_base_ppm(self, base_ppm: int):
        self.base_ppm = int(base_ppm)
        self.offset_ppm = 0
        self.prev_error_rate = None
        self.integral_error = 0.0

    def set_base_gain_tenths(self, base_gain_tenths: int):
        self.base_gain_tenths = int(base_gain_tenths)
        self.offset_gain_tenths = 0
        self.prev_error_rate = None
        self.integral_error = 0.0

    def get_effective_ppm(self) -> int:
        return self.base_ppm + self.offset_ppm

    def get_effective_gain_tenths(self) -> int:
        if self.base_gain_tenths == 0:
            return 0
        return self.base_gain_tenths + self.offset_gain_tenths

    def get_pid_terms(self) -> Tuple[float, float, float]:
        """Returns the latest evaluated (P, I, D) terms."""
        return (self.last_p, self.last_i, self.last_d)

    def reset_offsets(self):
        self.offset_ppm = 0
        self.offset_gain_tenths = 0
        self.prev_error_rate = None
        self.integral_error = 0.0
        self.last_p = 0.0
        self.last_i = 0.0
        self.last_d = 0.0
        self.last_adjusted_param = None
        self.last_action_desc = "Offsets reset to base"

    def compute_short_window_demod_fail_rate(self, rolling_deltas, current_time: float) -> Tuple[float, int, int]:
        """
        Calculates total demod failure rate as percentage of CRC matched packets to total Packet Sync Signatures seen
        over the short sliding window (SHORT_SLIDING_WINDOW_SECONDS).
        Returns (failure_rate_fraction, total_sync_seen, total_crc_matched).
        """
        short_window = max(self.eval_interval, SHORT_SLIDING_WINDOW_SECONDS)
        settling_window = min(self.settling_time, self.eval_interval * 0.2)
        min_t = current_time - short_window
        if self.last_adjustment_time > 0:
            min_t = max(min_t, self.last_adjustment_time + settling_window)

        recent_records = [d for d in rolling_deltas if d[0] >= min_t]
        if not recent_records and rolling_deltas:
            recent_records = list(rolling_deltas)

        if not recent_records:
            return 0.0, 0, 0

        sync_seen = sum(d[1] for d in recent_records)
        crc_matched = sum(d[6] for d in recent_records)

        if sync_seen == 0:
            return 0.0, 0, 0

        fail_count = max(0, sync_seen - crc_matched)
        fail_rate = fail_count / sync_seen
        return fail_rate, sync_seen, crc_matched

    def step(self, rolling_deltas, current_time: float) -> Optional[dict]:
        """
        Executes one lazy evaluation step.
        Locks onto total demod failure rate over the short sliding window.
        Returns a dict describing adjustments:
        e.g. {"ppm": new_ppm} or {"gain_tenths": new_gain_tenths, "gain_db": new_gain_db}
        or None if no adjustment is made.
        """
        if not self.enabled:
            return None

        if current_time - self.last_tune_time < self.eval_interval:
            return None

        settling_window = min(self.settling_time, self.eval_interval * 0.2)
        if self.last_adjustment_time > 0 and (current_time - self.last_adjustment_time) < settling_window:
            return None

        err_rate, sync_seen, crc_matched = self.compute_short_window_demod_fail_rate(rolling_deltas, current_time)
        if sync_seen < self.min_samples:
            return None

        self.last_tune_time = current_time

        # Update PID terms locking onto demod failure rate (setpoint: 0.0)
        e = err_rate
        dt = self.eval_interval
        self.integral_error = max(-2.0, min(2.0, self.integral_error + e * (dt / 10.0)))

        delta_e = (e - self.prev_error_rate) if self.prev_error_rate is not None else 0.0

        p_term = self.kp * e
        i_term = self.ki * self.integral_error
        d_term = self.kd * delta_e
        pid_u = p_term + i_term + d_term

        self.last_p = round(p_term, 3)
        self.last_i = round(i_term, 3)
        self.last_d = round(d_term, 3)

        # If zero demod failure rate (100% CRC matched), hold current settings
        if e == 0.0:
            self.prev_error_rate = 0.0
            self.last_action_desc = (
                f"Optimal (0.0% demod fail: {crc_matched:,}/{sync_seen:,} CRC matched) "
                f"[P:{self.last_p:+.2f} I:{self.last_i:+.2f} D:{self.last_d:+.2f}]"
            )
            return None

        # Check search gradient feedback on demod failure rate
        if self.prev_error_rate is not None:
            if delta_e > 0.005:
                # Performance degraded: reverse direction of parameter that was modified
                if self.last_adjusted_param == "ppm":
                    self.dir_ppm = -self.dir_ppm
                elif self.last_adjusted_param == "gain":
                    self.dir_gain = -self.dir_gain
                self.no_progress_count = 0
            elif delta_e < -0.005:
                # Performance improved: keep direction
                self.no_progress_count = 0
            else:
                self.no_progress_count += 1
                if self.no_progress_count >= 2:
                    if self.last_adjusted_param == "ppm":
                        self.dir_ppm = -self.dir_ppm
                    elif self.last_adjusted_param == "gain":
                        self.dir_gain = -self.dir_gain
                    self.no_progress_count = 0

        self.prev_error_rate = e

        can_tune_gain = (self.base_gain_tenths > 0)
        target_param = self.turn
        if target_param == "gain" and not can_tune_gain:
            target_param = "ppm"

        adjustment = None

        if target_param == "ppm":
            self.turn = "gain" if can_tune_gain else "ppm"
            step_val = 1 if self.dir_ppm >= 0 else -1
            cand = self.offset_ppm + step_val
            if cand > self.max_offset_ppm:
                cand = self.max_offset_ppm
                self.dir_ppm = -1
            elif cand < -self.max_offset_ppm:
                cand = -self.max_offset_ppm
                self.dir_ppm = 1

            if cand != self.offset_ppm:
                self.offset_ppm = cand
                new_ppm = self.base_ppm + self.offset_ppm
                self.last_adjustment_time = current_time
                self.last_adjusted_param = "ppm"
                self.last_action_desc = (
                    f"PPM -> {new_ppm} (Δ{self.offset_ppm:+d}) | "
                    f"Demod Fail: {err_rate*100:.1f}% ({crc_matched:,}/{sync_seen:,} CRC) "
                    f"[P:{self.last_p:+.2f} I:{self.last_i:+.2f} D:{self.last_d:+.2f}]"
                )
                adjustment = {"ppm": new_ppm}

        elif target_param == "gain" and can_tune_gain:
            self.turn = "ppm"
            step_val = 5 if self.dir_gain >= 0 else -5
            cand = self.offset_gain_tenths + step_val
            if cand > self.max_offset_gain_tenths:
                cand = self.max_offset_gain_tenths
                self.dir_gain = -5
            elif cand < -self.max_offset_gain_tenths:
                cand = -self.max_offset_gain_tenths
                self.dir_gain = 5

            if cand != self.offset_gain_tenths:
                self.offset_gain_tenths = cand
                new_gain_tenths = self.base_gain_tenths + self.offset_gain_tenths
                new_gain_db = round(new_gain_tenths / 10.0, 1)
                self.last_adjustment_time = current_time
                self.last_adjusted_param = "gain"
                delta_db = self.offset_gain_tenths / 10.0
                self.last_action_desc = (
                    f"Gain -> {new_gain_db:.1f} dB (Δ{delta_db:+.1f} dB) | "
                    f"Demod Fail: {err_rate*100:.1f}% ({crc_matched:,}/{sync_seen:,} CRC) "
                    f"[P:{self.last_p:+.2f} I:{self.last_i:+.2f} D:{self.last_d:+.2f}]"
                )
                adjustment = {
                    "gain_tenths": new_gain_tenths,
                    "gain_db": new_gain_db,
                }

        return adjustment


# ---------------------------------------------------------------------------
# Application Controller
# ---------------------------------------------------------------------------
class ApexBOSGrabberApplication:
    def __init__(self, cli_args):
        self.args = cli_args
        self.use_color = init_cross_platform_terminal(cli_args.no_color)
        self.sample_rate = cli_args.rate
        self.chunk_size = cli_args.bank_size
        self.gain_db, self.gain_tenths = parse_gain_value(cli_args.gain)
        self.gain = self.gain_tenths
        self.ppm = cli_args.ppm
        self.device = cli_args.device
        self.max_errors = cli_args.max_errors
        self.bias_tee = bool(getattr(cli_args, "bias_tee", 1))
        self.no_rssi = bool(getattr(cli_args, "no_rssi", False))
        self.config_path = getattr(cli_args, "config", None)

        # Determine worker ports
        if cli_args.workers is not None and cli_args.workers > 0:
            num_workers = cli_args.workers
            base_port = cli_args.zmq_port or 5555
            self.zmq_ports = [base_port + i for i in range(num_workers)]
        elif cli_args.procs:
            self.zmq_ports = [int(p.strip()) for p in cli_args.procs.split(",") if p.strip()]
        else:
            self.zmq_ports = [5555]
        self.worker_count = len(self.zmq_ports)
        self.udp_ports = [cli_args.udp_port or p for p in self.zmq_ports]

        # SDR Source mode
        self.stream_kind = resolve_sdr_stream_kind(cli_args)
        self.sdr_binary = detect_rtl_sdr_executable(cli_args.rtl_sdr_bin)

        # Channels and center frequency
        self.channels, self.locked_channels = load_channel_selection(
            cli_args.chans, cli_args.chans_file, return_locked=True
        )
        self.locked_channels = set(self.locked_channels)
        freqs = [itc_channel_frequency(ch) for ch in self.channels]
        span = max(freqs) - min(freqs) if freqs else 0

        # Auto-scale sample rate if channel span exceeds default rate and user did not explicitly set --rate
        if cli_args.rate == DEFAULT_SAMPLE_RATE and span + 50000 > self.sample_rate:
            needed_rate = int(math.ceil((span + 50000) / 192000.0) * 192000)
            self.sample_rate = max(needed_rate, 960000)
            self.rate_auto_scaled = True
        else:
            self.rate_auto_scaled = False

        # Ensure sample rate is hardware-compliant for RTL-SDR sources (pipe, tcp, and direct rtlsdr)
        # RTL2832U chip physical ADC limit: valid rates are >= 900,000 S/s.
        if self.stream_kind in ("pipe", "tcp", "rtlsdr"):
            min_hardware_rate = 960000
            if self.sample_rate < min_hardware_rate:
                self.sample_rate = min_hardware_rate
                self.rate_auto_scaled = True
            elif self.sample_rate % 192000 != 0:
                self.sample_rate = int(math.ceil(self.sample_rate / 192000.0) * 192000)

        self.center_freq = calculate_optimal_center_freq(self.channels, max_bandwidth=self.sample_rate)

        # Channels, base channel tracking & inactivity timeout
        self.channel_timeout = float(getattr(cli_args, "channel_timeout", 300.0))
        self.base_channels = set()
        self.start_time = time.time()
        self.last_inactivity_check = self.start_time
        self.channel_last_seen = {ch: self.start_time for ch in self.channels}
        self.channel_last_activity: Dict[int, float] = {}

        # Preserve first channel specified in --chans (or config/default)
        raw_chans_cli = getattr(cli_args, "chans", None)
        if raw_chans_cli:
            first_item = raw_chans_cli.split(",")[0].strip()
            if "-" in first_item:
                self.initial_first_channel = int(first_item.split("-")[0].strip())
            else:
                self.initial_first_channel = int(first_item)
        elif self.channels:
            self.initial_first_channel = self.channels[0]
        else:
            self.initial_first_channel = 101

        # IPC
        self.shutdown_flag = multiprocessing.Event()
        self.metrics_queue = multiprocessing.Queue()
        self.reader_ctrl_queue = multiprocessing.Queue()
        self.worker_data_queues = [multiprocessing.Queue(maxsize=30) for _ in range(self.worker_count)]
        self.worker_ctrl_queues = [multiprocessing.Queue() for _ in range(self.worker_count)]

        # Reader metrics state
        self.reader_samples_read = 0
        self.reader_bytes_read = 0
        self.reader_chunks_read = 0

        # Sliding window error rate deltas
        self.rolling_deltas = collections.deque()

        # Dashboard metrics state
        self.total_bursts = 0
        self.total_packets = 0
        self.peak_rssi = -100.0
        self.peak_rssi_window = collections.deque()
        self.packet_timestamps = collections.deque()
        self.worker_packet_timestamps = [collections.deque() for _ in range(self.worker_count)]
        self.worker_raw_deltas = [collections.deque() for _ in range(self.worker_count)]
        self.worker_stats = {
            i: {
                "bursts": 0, "dropped": 0, "packets": 0, "peak_rssi": -100.0, "backlog": 0,
                "samples_proc": 0, "banks_proc": 0,
                "sync_detected": 0, "headers_good": 0, "headers_bad": 0,
                "rs_good": 0, "rs_bad": 0, "crc_good": 0, "crc_bad": 0,
            }
            for i in range(self.worker_count)
        }
        self.recent_events = collections.deque(maxlen=15)
        self.channel_freq_offsets: Dict[int, float] = {}
        raw_afc = getattr(cli_args, "channel_afc", True)
        if isinstance(raw_afc, str):
            self.afc_enabled = raw_afc.strip().lower() in ("1", "on", "true", "yes", "enable", "start")
        else:
            self.afc_enabled = bool(raw_afc)
        self.afc_limit_hz = abs(float(getattr(cli_args, "afc_limit", 500.0)))
        self.dedup_ttl = float(getattr(cli_args, "dedup_ttl", 1.0))
        self.status_banner = f"Initialized ({self.stream_kind.upper()})"
        self.cmd_thread: Optional[TerminalCommandListener] = None

        # Base hardware tuning parameters & Lazy PID Auto-Tuner
        self.base_ppm = self.ppm
        self.base_gain_tenths = self.gain_tenths
        self.base_gain_db = self.gain_db
        auto_tune_flag = bool(getattr(cli_args, "auto_tune", False))
        tune_interval = float(getattr(cli_args, "tune_interval", 5.0))
        self.auto_tuner = LazyPidAutoTuner(
            base_ppm=self.base_ppm,
            base_gain_tenths=self.base_gain_tenths,
            enabled=auto_tune_flag,
            eval_interval=tune_interval,
            max_offset_ppm=4,
            max_offset_gain_tenths=20,
        )

    def step_auto_tuner(self, current_time: float):
        if not hasattr(self, "auto_tuner") or not self.auto_tuner or not self.auto_tuner.enabled:
            return
        adj = self.auto_tuner.step(self.rolling_deltas, current_time)
        if adj:
            if "ppm" in adj:
                self.ppm = adj["ppm"]
                self.reader_ctrl_queue.put({"cmd": "set_ppm", "ppm": self.ppm})
                self.status_banner = f"Auto-Tune PID: {self.auto_tuner.last_action_desc}"
            elif "gain_tenths" in adj:
                self.gain_tenths = adj["gain_tenths"]
                self.gain_db = adj["gain_db"]
                self.gain = self.gain_tenths
                self.reader_ctrl_queue.put({"cmd": "set_gain", "gain": self.gain_tenths})
                self.reset_peak_rssi()
                self.status_banner = f"Auto-Tune PID: {self.auto_tuner.last_action_desc}"

    def colorize(self, code: str, text: str) -> str:
        return f"{code}{text}{COLOR_RESET}" if self.use_color else text

    def reset_peak_rssi(self):
        """Resets peak RSSI tracking across application, sliding window, and worker processes."""
        self.peak_rssi = -100.0
        if hasattr(self, "peak_rssi_window"):
            self.peak_rssi_window.clear()
        if hasattr(self, "worker_stats"):
            for st in self.worker_stats.values():
                st["peak_rssi"] = -100.0
        if hasattr(self, "worker_ctrl_queues"):
            for q in self.worker_ctrl_queues:
                try:
                    q.put({"cmd": "reset_peak_rssi"})
                except Exception:
                    pass

    def _clear_sliding_windows(self):
        self.rolling_deltas.clear()
        if hasattr(self, "worker_raw_deltas"):
            for q in self.worker_raw_deltas:
                q.clear()
        self.reset_peak_rssi()

    def handle_terminal_input(self, command_line: str):
        raw_cmd = command_line.strip()
        if not raw_cmd:
            return

        # Normalize separators: convert '=' or ':' into space
        normalized = raw_cmd.replace("=", " ").replace(":", " ")
        tokens = normalized.split()
        if not tokens:
            return
        cmd = tokens[0].lower()

        # Handle attached arguments: e.g. "+107", "-107", "p12", "p-5", "g42", "b1"
        if cmd.startswith("+") and len(cmd) > 1 and cmd[1:].replace(",", "").isdigit():
            tokens = ["+", cmd[1:]] + tokens[1:]
            cmd = "+"
        elif cmd.startswith("-") and len(cmd) > 1 and cmd[1:].replace(",", "").isdigit():
            tokens = ["-", cmd[1:]] + tokens[1:]
            cmd = "-"
        elif (cmd.startswith("p") and len(cmd) > 1 and (cmd[1:].isdigit() or (cmd[1] == "-" and cmd[2:].isdigit()))):
            tokens = ["p", cmd[1:]] + tokens[1:]
            cmd = "p"
        elif (cmd.startswith("g") and len(cmd) > 1 and (cmd[1:].replace(".", "").isdigit() or cmd[1:].lower() in ("auto", "agc"))):
            tokens = ["g", cmd[1:]] + tokens[1:]
            cmd = "g"
        elif (cmd.startswith("b") and len(cmd) > 1 and cmd[1:].lower() in ("0", "1", "on", "off", "true", "false")):
            tokens = ["b", cmd[1:]] + tokens[1:]
            cmd = "b"
        elif cmd.startswith("dedup") and len(cmd) > 5 and cmd[5:].replace(".", "").isdigit():
            tokens = ["dedup", cmd[5:]] + tokens[1:]
            cmd = "dedup"

        if cmd in ("p", "ppm") and len(tokens) > 1:
            try:
                new_ppm = int(tokens[1])
                self.ppm = new_ppm
                self.base_ppm = new_ppm
                if hasattr(self, "auto_tuner") and self.auto_tuner:
                    self.auto_tuner.set_base_ppm(new_ppm)
                self.reader_ctrl_queue.put({"cmd": "set_ppm", "ppm": self.ppm})
                self._clear_sliding_windows()  # Reset sliding window on tuner change
                self.status_banner = f"PPM adjusted to {self.ppm} (window reset)"
            except ValueError:
                self.status_banner = f"Invalid PPM value: '{tokens[1]}'"

        elif cmd in ("g", "gain") and len(tokens) > 1:
            try:
                self.gain_db, self.gain_tenths = parse_gain_value(tokens[1])
                self.gain = self.gain_tenths
                self.base_gain_tenths = self.gain_tenths
                self.base_gain_db = self.gain_db
                if hasattr(self, "auto_tuner") and self.auto_tuner:
                    self.auto_tuner.set_base_gain_tenths(self.gain_tenths)
                self.reader_ctrl_queue.put({"cmd": "set_gain", "gain": self.gain})
                self._clear_sliding_windows()  # Reset sliding window on tuner change
                g_lbl = f"{self.gain_db:.1f} dB" if self.gain_tenths > 0 else "AGC"
                self.status_banner = f"Gain adjusted to {g_lbl} (window reset)"
            except ValueError:
                self.status_banner = f"Invalid Gain value: '{tokens[1]}'"

        elif cmd in ("b", "bias") and len(tokens) > 1:
            arg = tokens[1].lower()
            if arg in ("1", "on", "true", "yes", "enable"):
                self.bias_tee = True
            elif arg in ("0", "off", "false", "no", "disable"):
                self.bias_tee = False
            else:
                self.status_banner = f"Invalid bias value: '{tokens[1]}' (use 1/0 or on/off)"
                return
            self.reader_ctrl_queue.put({"cmd": "set_bias_tee", "bias_tee": self.bias_tee})
            self._clear_sliding_windows()  # Reset sliding window on bias change
            self.status_banner = f"Bias tee {'enabled (ON)' if self.bias_tee else 'disabled (OFF)'} (window reset)"

        elif cmd in ("+", "add") and len(tokens) > 1:
            try:
                raw_items = " ".join(tokens[1:]).replace(",", " ").split()
                added_ch = []
                locked_ch = []
                already_active = []
                now_t = time.time()

                for raw in raw_items:
                    raw = raw.strip()
                    if not raw:
                        continue
                    m = re.match(r"^(\d+)([lpLP\.]?)$", raw)
                    if not m:
                        continue
                    ch = int(m.group(1))
                    is_lock = bool(m.group(2))
                    if is_lock:
                        self.locked_channels.add(ch)
                        locked_ch.append(ch)

                    if ch not in self.channels:
                        added_ch.append(ch)
                        self.channel_last_seen[ch] = now_t
                    else:
                        already_active.append(ch)

                if added_ch:
                    self.channels = sorted(set(self.channels) | set(added_ch))
                    self._update_pipeline_channels()
                    self._clear_sliding_windows()
                    lock_desc = f" (locked: {locked_ch})" if locked_ch else ""
                    self.status_banner = f"Added channel(s): {added_ch}{lock_desc} (re-arranged workers)"
                elif locked_ch:
                    self.status_banner = f"Locked active channel(s): {locked_ch}"
                elif already_active:
                    self.status_banner = f"Channel(s) already active: {already_active}"
                else:
                    self.status_banner = "Invalid channel number(s)"
            except Exception as e:
                self.status_banner = f"Error adding channel: {e}"

        elif cmd in ("-", "del", "remove") and len(tokens) > 1:
            try:
                raw_items = " ".join(tokens[1:]).replace(",", " ").split()
                del_nums = []
                for raw in raw_items:
                    m = re.match(r"^(\d+)", raw.strip())
                    if m:
                        del_nums.append(int(m.group(1)))
                cand_remaining = [c for c in self.channels if c not in del_nums]
                if not cand_remaining:
                    kept = self._pick_channel_to_keep(list(self.channels))
                    cand_remaining = [kept]
                    del_nums = [c for c in del_nums if c != kept]
                self.channels = sorted(cand_remaining)
                for c in del_nums:
                    self.channel_last_seen.pop(c, None)
                    self.base_channels.discard(c)
                    self.locked_channels.discard(c)
                self._update_pipeline_channels()
                self._clear_sliding_windows()
                self.status_banner = f"Removed channel(s): {del_nums} (re-arranged workers)"
            except ValueError:
                self.status_banner = "Invalid channel number"

        elif cmd in ("lock", "pin") and len(tokens) > 1:
            try:
                raw_items = " ".join(tokens[1:]).replace(",", " ").split()
                nums = []
                for raw in raw_items:
                    m = re.match(r"^(\d+)", raw.strip())
                    if m:
                        nums.append(int(m.group(1)))
                for ch in nums:
                    self.locked_channels.add(ch)
                    if ch not in self.channels:
                        self.channels = sorted(self.channels + [ch])
                        self.channel_last_seen[ch] = time.time()
                self._update_pipeline_channels()
                self.status_banner = f"Locked channel(s): {nums}"
            except Exception:
                self.status_banner = "Invalid channel to lock"

        elif cmd in ("unlock", "unpin") and len(tokens) > 1:
            try:
                raw_items = " ".join(tokens[1:]).replace(",", " ").split()
                nums = []
                for raw in raw_items:
                    m = re.match(r"^(\d+)", raw.strip())
                    if m:
                        nums.append(int(m.group(1)))
                for ch in nums:
                    self.locked_channels.discard(ch)
                self.status_banner = f"Unlocked channel(s): {nums}"
            except Exception:
                self.status_banner = "Invalid channel to unlock"

        elif cmd in ("c", "ch", "chans", "channels"):
            now = time.time()
            ch_info = []
            for c in self.channels:
                age = int(now - self.channel_last_seen.get(c, self.start_time))
                flags = []
                if c in getattr(self, "locked_channels", set()):
                    flags.append("locked.")
                if c in self.base_channels:
                    flags.append("base*")
                flag_str = f" ({', '.join(flags)})" if flags else f" ({age}s ago)"
                ch_info.append(f"{c}{flag_str}")
            self.status_banner = f"Channels ({len(self.channels)}): {', '.join(ch_info)}"

        elif cmd in ("tune", "pid", "autotune", "auto-tune"):
            if not hasattr(self, "auto_tuner") or not self.auto_tuner:
                self.status_banner = "Auto-Tune PID tuner not initialized"
            elif len(tokens) > 1:
                sub = tokens[1].lower()
                if sub in ("1", "on", "true", "enable", "start"):
                    self.auto_tuner.enabled = True
                    g_lbl = f"{self.base_gain_db:.1f} dB" if self.base_gain_tenths > 0 else "AGC"
                    self.status_banner = f"Auto-Tune PID enabled (Base PPM: {self.base_ppm}, Base Gain: {g_lbl})"
                elif sub in ("0", "off", "false", "disable", "stop"):
                    self.auto_tuner.enabled = False
                    self.auto_tuner.reset_offsets()
                    self.ppm = self.base_ppm
                    self.gain_tenths = self.base_gain_tenths
                    self.gain_db = self.base_gain_db
                    self.gain = self.gain_tenths
                    self.reader_ctrl_queue.put({"cmd": "set_ppm", "ppm": self.ppm})
                    self.reader_ctrl_queue.put({"cmd": "set_gain", "gain": self.gain_tenths})
                    self.reset_peak_rssi()
                    g_lbl = f"{self.base_gain_db:.1f} dB" if self.base_gain_tenths > 0 else "AGC"
                    self.status_banner = f"Auto-Tune PID disabled (restored base PPM: {self.base_ppm}, Gain: {g_lbl})"
                elif sub in ("status", "info"):
                    st = "ON" if self.auto_tuner.enabled else "OFF"
                    d_p = self.ppm - self.base_ppm
                    d_g = (self.gain_tenths - self.base_gain_tenths) / 10.0
                    g_lbl = f"ΔGain: {d_g:+.1f} dB" if self.base_gain_tenths > 0 else "Gain: AGC"
                    p_val, i_val, d_val = self.auto_tuner.get_pid_terms()
                    self.status_banner = (
                        f"Auto-Tune PID: {st} | ΔPPM: {d_p:+d}, {g_lbl} | "
                        f"PID [P: {p_val:+.2f}, I: {i_val:+.2f}, D: {d_val:+.2f}] | "
                        f"{self.auto_tuner.last_action_desc}"
                    )
                elif sub in ("speed", "rate", "interval", "int") and len(tokens) > 2:
                    try:
                        new_iv = max(1.0, float(tokens[2]))
                        self.auto_tuner.eval_interval = new_iv
                        self.status_banner = f"Auto-Tune PID evaluation interval set to {new_iv:.1f}s"
                    except ValueError:
                        self.status_banner = f"Invalid tune interval '{tokens[2]}'"
                elif sub in ("window", "bound", "bounds", "limit", "limits") and len(tokens) > 3:
                    try:
                        ppm_lim = abs(int(tokens[2]))
                        gain_lim = abs(int(tokens[3]))
                        self.auto_tuner.max_offset_ppm = ppm_lim
                        self.auto_tuner.max_offset_gain_tenths = gain_lim
                        self.status_banner = f"Auto-Tune PID bounds set to ±{ppm_lim} PPM, ±{gain_lim/10.0:.1f} dB Gain"
                    except ValueError:
                        self.status_banner = f"Invalid tune bounds parameters"
                else:
                    self.status_banner = f"Invalid tune option '{tokens[1]}'. Use 'tune on', 'tune off', or 'tune interval <sec>'"
            else:
                if self.auto_tuner.enabled:
                    self.auto_tuner.enabled = False
                    self.auto_tuner.reset_offsets()
                    self.ppm = self.base_ppm
                    self.gain_tenths = self.base_gain_tenths
                    self.gain_db = self.base_gain_db
                    self.gain = self.gain_tenths
                    self.reader_ctrl_queue.put({"cmd": "set_ppm", "ppm": self.ppm})
                    self.reader_ctrl_queue.put({"cmd": "set_gain", "gain": self.gain_tenths})
                    self.reset_peak_rssi()
                    g_lbl = f"{self.base_gain_db:.1f} dB" if self.base_gain_tenths > 0 else "AGC"
                    self.status_banner = f"Auto-Tune PID disabled (restored base PPM: {self.base_ppm}, Gain: {g_lbl})"
                else:
                    self.auto_tuner.enabled = True
                    g_lbl = f"{self.base_gain_db:.1f} dB" if self.base_gain_tenths > 0 else "AGC"
                    self.status_banner = f"Auto-Tune PID enabled (Base PPM: {self.base_ppm}, Base Gain: {g_lbl})"

        elif cmd in ("afc", "chafc", "freq", "freq-corr"):
            if len(tokens) > 1:
                sub = tokens[1].lower()
                if sub in ("1", "on", "true", "yes", "enable", "start"):
                    self.afc_enabled = True
                    msg = {"cmd": "set_afc", "enabled": True, "limit_hz": self.afc_limit_hz}
                    for q in self.worker_ctrl_queues:
                        q.put(msg)
                    self.status_banner = f"Channel AFC enabled (window: ±{self.afc_limit_hz:.0f} Hz)"
                elif sub in ("0", "off", "false", "no", "disable", "stop"):
                    self.afc_enabled = False
                    self.channel_freq_offsets.clear()
                    msg = {"cmd": "set_afc", "enabled": False}
                    for q in self.worker_ctrl_queues:
                        q.put(msg)
                    self.status_banner = "Channel AFC disabled (offsets reset to 0 Hz)"
                elif sub in ("limit", "window", "max", "win", "cap") and len(tokens) > 2:
                    try:
                        new_lim = abs(float(tokens[2]))
                        self.afc_limit_hz = new_lim
                        msg = {"cmd": "set_afc", "limit_hz": self.afc_limit_hz}
                        for q in self.worker_ctrl_queues:
                            q.put(msg)
                        self.status_banner = f"Channel AFC window set to ±{self.afc_limit_hz:.0f} Hz"
                    except ValueError:
                        self.status_banner = f"Invalid AFC limit '{tokens[2]}'"
                elif sub in ("status", "info"):
                    st = "ON" if self.afc_enabled else "OFF"
                    self.status_banner = f"Channel AFC: {st} (window: ±{self.afc_limit_hz:.0f} Hz, tracking {len(self.channel_freq_offsets)} ch)"
                else:
                    try:
                        new_lim = abs(float(tokens[1]))
                        self.afc_limit_hz = new_lim
                        msg = {"cmd": "set_afc", "limit_hz": self.afc_limit_hz}
                        for q in self.worker_ctrl_queues:
                            q.put(msg)
                        self.status_banner = f"Channel AFC window set to ±{self.afc_limit_hz:.0f} Hz"
                    except ValueError:
                        self.status_banner = f"Invalid AFC option '{tokens[1]}'. Use 'afc on', 'afc off', or 'afc limit <hz>'"
            else:
                if self.afc_enabled:
                    self.afc_enabled = False
                    self.channel_freq_offsets.clear()
                    msg = {"cmd": "set_afc", "enabled": False}
                    for q in self.worker_ctrl_queues:
                        q.put(msg)
                    self.status_banner = "Channel AFC disabled (offsets reset to 0 Hz)"
                else:
                    self.afc_enabled = True
                    msg = {"cmd": "set_afc", "enabled": True, "limit_hz": self.afc_limit_hz}
                    for q in self.worker_ctrl_queues:
                        q.put(msg)
                    self.status_banner = f"Channel AFC enabled (window: ±{self.afc_limit_hz:.0f} Hz)"

        elif cmd in ("dedup", "dup"):
            if len(tokens) > 1:
                sub = tokens[1].lower()
                if sub in ("status", "info"):
                    self.status_banner = f"Dedup Window: {self.dedup_ttl:.2f}s"
                else:
                    val_str = tokens[2] if sub in ("window", "ttl", "sec", "win", "limit", "size") and len(tokens) > 2 else tokens[1]
                    try:
                        new_ttl = max(0.0, float(val_str))
                        self.dedup_ttl = new_ttl
                        msg = {"cmd": "set_dedup", "ttl_sec": self.dedup_ttl}
                        for q in self.worker_ctrl_queues:
                            q.put(msg)
                        self.status_banner = f"Dedup window size set to {self.dedup_ttl:.2f}s"
                    except ValueError:
                        self.status_banner = f"Invalid dedup window '{val_str}' (e.g. 'dedup 1.0')"
            else:
                self.status_banner = f"Dedup Window: {self.dedup_ttl:.2f}s (use 'dedup <sec>' to change)"

        elif cmd in ("q", "quit", "exit"):
            self.shutdown_flag.set()

        elif cmd in ("?", "h", "help"):
            self.status_banner = "Commands: p <ppm> | g <gain|auto> | b <0|1> | tune <on|off> | afc <on|off> | dedup <sec> | + <ch> | - <ch> | chans | q"

    def _pick_channel_to_keep(self, pool: Optional[List[int]] = None) -> int:
        """
        Picks at least one channel to retain when channels would otherwise be exhausted:
        1. If a locked channel is present in the list, keep that one (preferring 102 if locked).
        2. If 102 is specified in the list, keep that one.
        3. Otherwise keep the last remaining channel that received activity.
        4. If none ever received activity, keep the first channel specified in the --chans command line option.
        """
        candidate_list = list(pool) if pool else list(self.channels)
        locked = getattr(self, "locked_channels", set())
        if 102 in candidate_list and 102 in locked:
            return 102
        for c in candidate_list:
            if c in locked:
                return c

        if 102 in candidate_list:
            return 102

        # Channels in candidate_list that have received activity (seen after start_time or in channel_last_activity)
        active_candidates = []
        for c in candidate_list:
            act_t = max(self.channel_last_activity.get(c, 0.0), self.channel_last_seen.get(c, 0.0))
            if act_t > self.start_time:
                active_candidates.append((c, act_t))

        if active_candidates:
            # Channel with the most recent activity
            active_candidates.sort(key=lambda item: item[1], reverse=True)
            return active_candidates[0][0]

        # If none ever received activity, keep the first channel specified in --chans
        if hasattr(self, "initial_first_channel") and self.initial_first_channel:
            return self.initial_first_channel

        return candidate_list[0] if candidate_list else 101

    def process_packet_channels(self, packet_type: int, telemetry: dict, heard_channel: Optional[int] = None) -> list:
        """
        Inspects base packet's own channel and its channel list, marks them as base-derived,
        and dynamically adds any missing channels to the application, re-arranging workers.
        Returns list of newly added channels.
        """
        now_t = time.time()
        if heard_channel is not None and isinstance(heard_channel, int) and 0 < heard_channel < 255:
            self.channel_last_seen[heard_channel] = now_t
            self.channel_last_activity[heard_channel] = now_t

        base_ch, ch_list = extract_base_packet_channels(packet_type, telemetry)
        base_candidates = set()
        if base_ch is not None:
            base_candidates.add(base_ch)
        for c in ch_list:
            base_candidates.add(c)
        if packet_type == 0x02 and heard_channel is not None and isinstance(heard_channel, int) and 0 < heard_channel < 255:
            base_candidates.add(heard_channel)

        newly_added = []
        if base_candidates:
            self.base_channels.update(base_candidates)
            missing = [c for c in sorted(base_candidates) if c not in self.channels]
            if missing:
                self.channels = sorted(set(self.channels) | set(missing))
                for c in missing:
                    self.channel_last_seen[c] = now_t
                self._update_pipeline_channels()
                self.rolling_deltas.clear()
                self.status_banner = f"Dynamically added base channel(s): {missing} (re-arranged workers)"
                newly_added = missing
        return newly_added

    def check_channel_timeouts(self, current_time: float) -> list:
        """
        Drops channels that have not received a packet within self.channel_timeout,
        unless the channel came from a base or is locked by user. Re-arranges workers if any are dropped.
        Always keeps at least one channel in the list according to retention rules:
        1. Keep locked channels if present.
        2. Keep 102 if present.
        3. Keep the last remaining channel that received activity.
        4. If none ever received activity, keep the first channel specified in --chans.
        Returns list of dropped channels.
        """
        dropped = []
        for c in list(self.channels):
            if c in self.base_channels or c in getattr(self, "locked_channels", set()):
                continue
            last_t = self.channel_last_seen.get(c, self.start_time)
            if current_time - last_t >= self.channel_timeout:
                dropped.append(c)

        if dropped:
            remaining = [c for c in self.channels if c not in dropped]
            if not remaining:
                kept = self._pick_channel_to_keep(list(self.channels))
                remaining = [kept]
                dropped = [c for c in dropped if c != kept]
            if dropped:
                self.channels = sorted(remaining)
                for c in dropped:
                    self.channel_last_seen.pop(c, None)
                self._update_pipeline_channels()
                self.rolling_deltas.clear()
                self.status_banner = f"Dropped inactive channel(s): {dropped} (re-arranged workers)"
        return dropped

    def _update_pipeline_channels(self):
        new_freq = calculate_optimal_center_freq(self.channels, max_bandwidth=self.sample_rate)
        if new_freq != self.center_freq:
            self.center_freq = new_freq
            self.reader_ctrl_queue.put({"cmd": "set_freq", "freq": self.center_freq})

        buckets = distribute_channels_across_workers(self.channels, self.worker_count)
        for i, b in enumerate(buckets):
            self.worker_ctrl_queues[i].put({
                "cmd": "update_channels",
                "channels": b,
                "center_freq": self.center_freq,
            })

    def process_metrics(self):
        """Processes all pending IPC messages from worker and reader queues."""
        while not self.metrics_queue.empty():
            try:
                m = self.metrics_queue.get_nowait()
                m_type = m.get("type")
                if m_type == "worker_report":
                    idx = m.get("worker_idx", 0)
                    self.worker_stats[idx] = {
                        "bursts": m.get("bursts", 0),
                        "dropped": m.get("dropped", 0),
                        "packets": m.get("packets", 0),
                        "peak_rssi": m.get("peak_rssi", -100.0),
                        "backlog": m.get("backlog", 0),
                        "samples_proc": m.get("samples_proc", 0),
                        "banks_proc": m.get("banks_proc", 0),
                        "sync_detected": m.get("sync_detected", 0),
                        "headers_good": m.get("headers_good", 0),
                        "headers_bad": m.get("headers_bad", 0),
                        "rs_good": m.get("rs_good", 0),
                        "rs_bad": m.get("rs_bad", 0),
                        "crc_good": m.get("crc_good", 0),
                        "crc_bad": m.get("crc_bad", 0),
                    }
                    w_peak = m.get("peak_rssi", -100.0)
                    now_t = time.time()
                    if w_peak > -99.0:
                        if hasattr(self, "peak_rssi_window"):
                            self.peak_rssi_window.append((now_t, w_peak))
                            while self.peak_rssi_window and (now_t - self.peak_rssi_window[0][0]) > 60.0:
                                self.peak_rssi_window.popleft()
                            self.peak_rssi = max((x[1] for x in self.peak_rssi_window), default=-100.0)
                        else:
                            if w_peak > self.peak_rssi:
                                self.peak_rssi = w_peak
                    if "channel_freq_offsets" in m:
                        if self.afc_enabled:
                            self.channel_freq_offsets.update(m["channel_freq_offsets"])
                        else:
                            self.channel_freq_offsets.clear()

                    # Record delta in sliding window
                    now_t = time.time()
                    delta_cg = m.get("delta_crc_g", 0)
                    self.rolling_deltas.append((
                        now_t,
                        m.get("delta_sync", 0),
                        m.get("delta_hdr_g", 0),
                        m.get("delta_hdr_b", 0),
                        m.get("delta_rs_g", 0),
                        m.get("delta_rs_b", 0),
                        delta_cg,
                        m.get("delta_crc_b", 0),
                    ))
                    if delta_cg > 0 and hasattr(self, "worker_raw_deltas") and idx < len(self.worker_raw_deltas):
                        self.worker_raw_deltas[idx].append((now_t, delta_cg))

                elif m_type == "reader_report":
                    self.reader_samples_read = m.get("samples_read", 0)
                    self.reader_bytes_read = m.get("bytes_read", 0)
                    self.reader_chunks_read = m.get("chunks_read", 0)

                elif m_type == "packet_event":
                    self.total_packets += 1
                    now_t = time.time()
                    self.packet_timestamps.append(now_t)
                    self.worker_packet_timestamps[m["worker_idx"]].append(now_t)
                    self.recent_events.append(m)
                    p_rssi = m.get("rssi")
                    if p_rssi is not None and float(p_rssi) > -99.0:
                        if hasattr(self, "peak_rssi_window"):
                            self.peak_rssi_window.append((now_t, float(p_rssi)))
                            while self.peak_rssi_window and (now_t - self.peak_rssi_window[0][0]) > 60.0:
                                self.peak_rssi_window.popleft()
                            self.peak_rssi = max((x[1] for x in self.peak_rssi_window), default=-100.0)
                        else:
                            if float(p_rssi) > self.peak_rssi:
                                self.peak_rssi = float(p_rssi)
                    if "freq_offset_hz" in m and "channel" in m:
                        if self.afc_enabled:
                            self.channel_freq_offsets[m["channel"]] = m["freq_offset_hz"]
                    if "channel" in m and isinstance(m["channel"], int):
                        self.channel_last_activity[m["channel"]] = now_t

                    # Inspect base packet channels & dynamically add missing ones (re-arranges workers)
                    self.process_packet_channels(
                        m.get("pkt_type", 0),
                        m.get("telemetry", {}),
                        heard_channel=m.get("channel")
                    )

                elif m_type == "error":
                    self.status_banner = f"ERROR: {m.get('msg')}"

                elif m_type == "reader_status":
                    g_label = f"{m.get('gain_db', 0):.1f} dB" if m.get('gain_db', 0) > 0 else "AGC"
                    b_label = "Bias:ON" if m.get('bias_tee', False) else "Bias:OFF"
                    self.status_banner = f"SDR {m.get('stream').upper()} active @ {m.get('center_freq')/1e6:.4f} MHz ({g_label}, {b_label})"
            except queue.Empty:
                break

    def run(self):
        # 0. Check SDR availability before spawning worker processes
        if self.stream_kind == "pipe" and not is_rtl_sdr_available(self.sdr_binary):
            print_sdr_setup_guide()
            sys.exit(1)
        elif self.stream_kind == "rtlsdr":
            ok, err = is_rtlsdr_package_available()
            if not ok:
                py_cmd = sys.executable or "python"
                print("\n" + "=" * 76)
                print(" [!] ApexBOSGrabber: Direct RTL-SDR Mode Error")
                print("=" * 76)
                print(f"Details: {err}\n")
                print("To install the Python package, run:")
                print(f"    {py_cmd} -m pip install pyrtlsdr\n")
                if os.name == "nt":
                    print("On Windows, pyrtlsdr also requires:")
                    print("  1. 'rtlsdr.dll' and 'libusb-1.0.dll' in your PATH or current directory.")
                    print("  2. WinUSB driver installed for Interface 0 using Zadig (https://zadig.akeo.ie).\n")
                print("=" * 76 + "\n")
                sys.exit(1)

        buckets = distribute_channels_across_workers(self.channels, self.worker_count)

        # 1. Spawn Worker Processes
        workers = []
        for i in range(self.worker_count):
            p = multiprocessing.Process(
                target=native_demod_worker,
                args=(
                    i,
                    self.zmq_ports[i],
                    self.udp_ports[i],
                    buckets[i],
                    self.center_freq,
                    self.sample_rate,
                    self.max_errors,
                    self.worker_data_queues[i],
                    self.worker_ctrl_queues[i],
                    self.metrics_queue,
                    self.shutdown_flag,
                    self.no_rssi,
                    self.afc_enabled,
                    self.afc_limit_hz,
                    self.dedup_ttl,
                ),
                daemon=True,
            )
            p.start()
            workers.append(p)

        # 2. Spawn SDR Reader Process
        reader_proc = multiprocessing.Process(
            target=sdr_stream_reader_worker,
            args=(
                self.stream_kind,
                self.sdr_binary,
                getattr(self.args, "host", "127.0.0.1"),
                getattr(self.args, "port", 1234),
                self.device,
                self.center_freq,
                self.sample_rate,
                self.gain_db,
                self.ppm,
                self.chunk_size,
                self.worker_data_queues,
                self.reader_ctrl_queue,
                self.metrics_queue,
                self.shutdown_flag,
                self.bias_tee,
            ),
            daemon=True,
        )
        reader_proc.start()

        # 3. Start Command Listener Thread
        self.cmd_thread = TerminalCommandListener(self.handle_terminal_input, self.shutdown_flag, app=self)
        self.cmd_thread.start()

        last_render = time.time()
        try:
            while not self.shutdown_flag.is_set():
                self.process_metrics()

                now = time.time()
                while self.packet_timestamps and now - self.packet_timestamps[0] > 60:
                    self.packet_timestamps.popleft()
                for q in self.worker_packet_timestamps:
                    while q and now - q[0] > 60:
                        q.popleft()
                if hasattr(self, "worker_raw_deltas"):
                    for q in self.worker_raw_deltas:
                        while q and now - q[0][0] > 60:
                            q.popleft()

                # Clean rolling deltas older than sliding window
                while self.rolling_deltas and (now - self.rolling_deltas[0][0]) > SLIDING_WINDOW_SECONDS:
                    self.rolling_deltas.popleft()
                if hasattr(self, "peak_rssi_window"):
                    while self.peak_rssi_window and (now - self.peak_rssi_window[0][0]) > 60.0:
                        self.peak_rssi_window.popleft()
                    if self.peak_rssi_window:
                        self.peak_rssi = max((x[1] for x in self.peak_rssi_window), default=-100.0)
                    else:
                        self.peak_rssi = -100.0

                # Check channel inactivity (drop non-base channels after 5 min timeout, re-arranges workers)
                if now - self.last_inactivity_check >= 1.0:
                    self.last_inactivity_check = now
                    self.check_channel_timeouts(now)

                # Automated lazy PID tuning step
                self.step_auto_tuner(now)

                if now - last_render >= 0.5:
                    self.render_screen(now)
                    last_render = now

                time.sleep(0.05)

        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown_flag.set()
            time.sleep(0.2)
            reader_proc.terminate()
            for p in workers:
                p.terminate()
            print(self.colorize(COLOR_YELLOW, "\nApexBOSGrabber pipeline terminated."))

    def render_screen(self, current_time: float):
        if hasattr(self, "worker_raw_deltas"):
            for q in self.worker_raw_deltas:
                while q and current_time - q[0][0] > 60:
                    q.popleft()
        if hasattr(self, "peak_rssi_window"):
            while self.peak_rssi_window and (current_time - self.peak_rssi_window[0][0]) > 60.0:
                self.peak_rssi_window.popleft()
            if self.peak_rssi_window:
                self.peak_rssi = max((x[1] for x in self.peak_rssi_window), default=-100.0)
            else:
                self.peak_rssi = -100.0

        overall_ppm = len(self.packet_timestamps)
        total_b = sum(s["bursts"] for s in self.worker_stats.values())
        total_p = sum(s["packets"] for s in self.worker_stats.values())
        max_backlog = max((s["backlog"] for s in self.worker_stats.values()), default=0)
        total_dropped = sum(s["dropped"] for s in self.worker_stats.values())

        # Cumulative lifetime totals
        life_sync = sum(s.get("sync_detected", 0) for s in self.worker_stats.values())
        life_hdr_g = sum(s.get("headers_good", 0) for s in self.worker_stats.values())
        life_hdr_b = sum(s.get("headers_bad", 0) for s in self.worker_stats.values())
        life_rs_g = sum(s.get("rs_good", 0) for s in self.worker_stats.values())
        life_rs_b = sum(s.get("rs_bad", 0) for s in self.worker_stats.values())
        life_crc_g = sum(s.get("crc_good", 0) for s in self.worker_stats.values())
        life_crc_b = sum(s.get("crc_bad", 0) for s in self.worker_stats.values())

        # Sliding window totals (past 60 seconds)
        win_sync = sum(d[1] for d in self.rolling_deltas)
        win_hdr_g = sum(d[2] for d in self.rolling_deltas)
        win_hdr_b = sum(d[3] for d in self.rolling_deltas)
        win_rs_g = sum(d[4] for d in self.rolling_deltas)
        win_rs_b = sum(d[5] for d in self.rolling_deltas)
        win_crc_g = sum(d[6] for d in self.rolling_deltas)
        win_crc_b = sum(d[7] for d in self.rolling_deltas)

        # Short sliding window totals (past SHORT_SLIDING_WINDOW_SECONDS)
        short_cutoff = current_time - SHORT_SLIDING_WINDOW_SECONDS
        short_deltas = [d for d in self.rolling_deltas if d[0] >= short_cutoff]
        short_sync = sum(d[1] for d in short_deltas)
        short_crc_g = sum(d[6] for d in short_deltas)
        short_matched_pct = (short_crc_g / short_sync * 100.0) if short_sync > 0 else 0.0
        short_demod_fail_pct = max(0.0, 100.0 - short_matched_pct) if short_sync > 0 else 0.0

        # 60s window demod failure and match rate
        win_matched_pct = (win_crc_g / win_sync * 100.0) if win_sync > 0 else 0.0
        win_demod_fail_pct = max(0.0, 100.0 - win_matched_pct) if win_sync > 0 else 0.0

        # Percentage of bad in good: (bad / good) * 100 for sliding window
        hdr_bad_in_good = (win_hdr_b / win_hdr_g * 100.0) if win_hdr_g > 0 else (100.0 if win_hdr_b > 0 else 0.0)
        hdr_tot = win_hdr_g + win_hdr_b
        hdr_err_rate = (win_hdr_b / hdr_tot * 100.0) if hdr_tot > 0 else 0.0

        rs_processed = win_rs_g + win_rs_b
        rs_bad_in_good = (win_rs_b / win_rs_g * 100.0) if win_rs_g > 0 else (100.0 if win_rs_b > 0 else 0.0)
        rs_err_rate = (win_rs_b / rs_processed * 100.0) if rs_processed > 0 else 0.0

        crc_tot = win_crc_g + win_crc_b
        crc_bad_in_good = (win_crc_b / win_crc_g * 100.0) if win_crc_g > 0 else (100.0 if win_crc_b > 0 else 0.0)
        crc_err_rate = (win_crc_b / crc_tot * 100.0) if crc_tot > 0 else 0.0

        # Total I/Q sample statistics
        total_worker_samples = sum(s.get("samples_proc", 0) for s in self.worker_stats.values())
        total_worker_banks = sum(s.get("banks_proc", 0) for s in self.worker_stats.values())

        is_clipping = (self.peak_rssi >= 0.0)
        gain_str = f"{self.gain_db:.1f} dB" if self.gain_db > 0 else "AGC"
        gain_disp = self.colorize(COLOR_RED + COLOR_BOLD, f"{gain_str} [CLIP: GAIN HIGH]") if is_clipping else self.colorize(COLOR_WHITE, gain_str)

        lines = []
        lines.append(self.colorize(COLOR_CYAN + COLOR_BOLD, "=" * 80))
        lines.append(
            self.colorize(COLOR_CYAN + COLOR_BOLD, f" ApexBOSGrabber ({VERSION}) ")
            + self.colorize(COLOR_DIM, "| SDR: ") + self.colorize(COLOR_WHITE + COLOR_BOLD, self.stream_kind.upper())
            + self.colorize(COLOR_DIM, " | Rate: ") + self.colorize(COLOR_WHITE, f"{self.sample_rate} S/s" + (" [AUTO]" if self.rate_auto_scaled else ""))
            + self.colorize(COLOR_DIM, " | Gain: ") + gain_disp
            + self.colorize(COLOR_DIM, " | Bias: ") + self.colorize(COLOR_GREEN if self.bias_tee else COLOR_DIM, "ON" if self.bias_tee else "OFF")
            + self.colorize(COLOR_DIM, " | PPM: ") + self.colorize(COLOR_WHITE, f"{self.ppm}")
            + self.colorize(COLOR_DIM, " | Center: ") + self.colorize(COLOR_GREEN, f"{self.center_freq / 1e6:.4f} MHz")
        )
        lines.append(self.colorize(COLOR_CYAN + COLOR_BOLD, "=" * 80))

        # Overall summary lines
        lines.append(
            f" I/Q Flow: Reader {self.colorize(COLOR_WHITE + COLOR_BOLD, f'{self.reader_samples_read:,}')} S "
            f"({self.reader_bytes_read / (1024*1024):.2f} MB, {self.reader_chunks_read} chunks) | "
            f"Workers Demod: {self.colorize(COLOR_GREEN + COLOR_BOLD, f'{total_worker_samples:,}')} S ({total_worker_banks} banks)"
        )
        if is_clipping:
            rssi_color = COLOR_RED + COLOR_BOLD
            rssi_str = f"{self.peak_rssi:.1f} dBFS [CLIPPING: Gain may be set too high!]"
        else:
            rssi_color = COLOR_MAGENTA + COLOR_BOLD
            rssi_str = f"{self.peak_rssi:.1f} dBFS"
        lines.append(
            f" Bursts: {self.colorize(COLOR_WHITE + COLOR_BOLD, str(total_b))}  "
            f"Pending Banks: {self.colorize(COLOR_GREEN if max_backlog <= 4 else (COLOR_YELLOW if max_backlog <= 12 else COLOR_RED), str(max_backlog))}  "
            f"Peak RSSI: {self.colorize(rssi_color, rssi_str)}  "
            f"Packets: {self.colorize(COLOR_GREEN + COLOR_BOLD, str(total_p))}  "
            f"PPM: {self.colorize(COLOR_GREEN + COLOR_BOLD, str(overall_ppm))} ({self.colorize(COLOR_CYAN, f'Raw: {win_crc_g:,}')})  "
            f"Dropped: {self.colorize(COLOR_RED if total_dropped else COLOR_DIM, str(total_dropped))}"
        )
        if is_clipping:
            lines.append(
                self.colorize(
                    COLOR_RED + COLOR_BOLD,
                    " [!] ADC CLIPPING DETECTED (Peak RSSI >= 0.0 dBFS): Gain may be set too high! Use 'g <gain>' to reduce."
                )
            )
        def _fmt_ch(c):
            s = str(c)
            if c in getattr(self, "locked_channels", set()):
                s += "."
            if c in self.base_channels:
                s += "*"
            return s

        ch_display = ", ".join(_fmt_ch(c) for c in self.channels)
        if len(ch_display) > 55:
            ch_display = ch_display[:52] + "..."
        locked_cnt = len(getattr(self, "locked_channels", set()))
        locked_tag = f", {locked_cnt} locked." if locked_cnt else ""
        lines.append(
            f" Channels ({len(self.channels)} active{locked_tag}, {len(self.base_channels)} base*): "
            f"{self.colorize(COLOR_CYAN, ch_display)}"
        )
        afc_on = getattr(self, "afc_enabled", True)
        if afc_on:
            if hasattr(self, "channel_freq_offsets") and self.channel_freq_offsets:
                active_offsets = [
                    f"{c}: {self.channel_freq_offsets[c]:+.0f}Hz"
                    for c in sorted(self.channel_freq_offsets.keys())
                    if c in self.channels and abs(self.channel_freq_offsets[c]) >= 1.0
                ]
                if active_offsets:
                    offsets_str = ", ".join(active_offsets)
                    if len(offsets_str) > 55:
                        offsets_str = offsets_str[:52] + "..."
                    lines.append(
                        f" Channel AFC Offsets (Δf) : "
                        f"{self.colorize(COLOR_YELLOW, offsets_str)}"
                    )
        else:
            lines.append(
                f" Channel AFC Offsets (Δf) : "
                f"{self.colorize(COLOR_DIM, '[DISABLED - Offsets 0Hz]')}"
            )
        lines.append(self.colorize(COLOR_DIM, "-" * 80))

        # Pipeline Statistics & Failure Rates (60s Sliding Window)
        tune_on = self.auto_tuner.enabled if hasattr(self, "auto_tuner") and self.auto_tuner else False
        p_val, i_val, d_val = self.auto_tuner.get_pid_terms() if hasattr(self, "auto_tuner") and self.auto_tuner else (0.0, 0.0, 0.0)
        pid_str = f" [P: {p_val:+.2f}, I: {i_val:+.2f}, D: {d_val:+.2f}]"
        if tune_on:
            d_p = self.ppm - self.base_ppm
            d_g = (self.gain_tenths - self.base_gain_tenths) / 10.0
            g_str = f", ΔG: {d_g:+.1f} dB" if self.base_gain_tenths > 0 else " [Gain: AGC]"
            tune_badge = self.colorize(COLOR_GREEN + COLOR_BOLD, f"ON (ΔPPM: {d_p:+d}{g_str})") + self.colorize(COLOR_CYAN, pid_str)
        else:
            tune_badge = self.colorize(COLOR_DIM, "OFF") + self.colorize(COLOR_DIM, pid_str)

        lines.append(
            self.colorize(COLOR_BOLD, f" Pipeline Detection & Decoding Integrity ({int(SLIDING_WINDOW_SECONDS)}s Sliding Window) | Auto-Tune PID: ")
            + tune_badge
            + self.colorize(COLOR_BOLD, ":")
        )
        if self.use_color:
            style = get_failure_rate_bg_and_tag(short_demod_fail_pct)
            bg_code = style.bg
            status_lbl = style.tag
            status_color = style.status_color
            fg_code = getattr(style, "fg_contrast", COLOR_WHITE + COLOR_BOLD)
            font = get_large_block_font()
            rate_str = f"{short_demod_fail_pct:5.1f}%"
            rows_num = [
                " ".join(font.get(c, ["   ", "   ", "   "])[r] for c in rate_str)
                for r in range(3)
            ]
            max_w = max(len(r) for r in rows_num)

            r0_num = f"{bg_code}{fg_code} {rows_num[0].ljust(max_w)} {COLOR_RESET}"
            r1_num = f"{bg_code}{fg_code} {rows_num[1].ljust(max_w)} {COLOR_RESET}"
            r2_num = f"{bg_code}{fg_code} {rows_num[2].ljust(max_w)} {COLOR_RESET}"

            tag_badge = f"{bg_code}{fg_code} {short_demod_fail_pct:5.1f}% [{status_lbl}] {COLOR_RESET}"
            r0_text = f"{self.colorize(COLOR_BOLD, 'Total Demod Failure Rate:')} {tag_badge}"
            r1_text = (
                f"{int(SHORT_SLIDING_WINDOW_SECONDS)}s Short Window: {self.colorize(COLOR_WHITE + COLOR_BOLD, f'{short_crc_g:,}')}/"
                f"{self.colorize(COLOR_WHITE + COLOR_BOLD, f'{short_sync:,}')} CRC/Sync "
                f"({self.colorize(COLOR_GREEN if short_matched_pct >= 90 else COLOR_YELLOW, f'{short_matched_pct:5.1f}% match')})"
            )
            r2_text = (
                f"60s Total Window: {self.colorize(COLOR_WHITE, f'{win_demod_fail_pct:5.1f}% fail')} "
                f"({win_crc_g:,}/{win_sync:,}) | {self.colorize(COLOR_DIM, 'PID Setpoint: 0.0%')}"
            )

            lines.append(f"  {r0_num}  {r0_text}")
            lines.append(f"  {r1_num}  {r1_text}")
            lines.append(f"  {r2_num}  {r2_text}")
        else:
            lines.append(
                f"  Total Demod Failure Rate : "
                f"[{short_demod_fail_pct:5.1f}%] "
                f"({int(SHORT_SLIDING_WINDOW_SECONDS)}s: {short_crc_g:,}/{short_sync:,} CRC/Sync, {short_matched_pct:5.1f}% match) | "
                f"60s: {win_demod_fail_pct:5.1f}% ({win_crc_g:,}/{win_sync:,})"
            )
        lines.append(
            f"  Packet Sync Signatures   : {self.colorize(COLOR_WHITE + COLOR_BOLD, f'{win_sync:,}')} detected (window)  |  "
            f"Lifetime: {self.colorize(COLOR_DIM, f'{life_sync:,}')}"
        )
        lines.append(
            f"  Viterbi Headers Decoded  : Good: {self.colorize(COLOR_GREEN, f'{win_hdr_g:,}')}  | "
            f"Failures: {self.colorize(COLOR_RED if win_hdr_b else COLOR_DIM, f'{win_hdr_b:,}')}  | "
            f"Bad in Good: {self.colorize(COLOR_YELLOW if hdr_bad_in_good > 5 else COLOR_GREEN, f'{hdr_bad_in_good:5.1f}%')} "
            f"({hdr_err_rate:4.1f}% failure rate)"
        )
        lines.append(
            f"  RS Payloads Processed    : Good: {self.colorize(COLOR_GREEN, f'{win_rs_g:,}')}  | "
            f"Failures: {self.colorize(COLOR_RED if win_rs_b else COLOR_DIM, f'{win_rs_b:,}')}  | "
            f"Bad in Good: {self.colorize(COLOR_YELLOW if rs_bad_in_good > 5 else COLOR_GREEN, f'{rs_bad_in_good:5.1f}%')} "
            f"({rs_err_rate:4.1f}% of {rs_processed:,} processed)"
        )
        lines.append(
            f"  CRC Matched Integrity    : Good: {self.colorize(COLOR_GREEN, f'{win_crc_g:,}')}  | "
            f"Failures: {self.colorize(COLOR_RED if win_crc_b else COLOR_DIM, f'{win_crc_b:,}')}  | "
            f"Bad in Good: {self.colorize(COLOR_YELLOW if crc_bad_in_good > 5 else COLOR_GREEN, f'{crc_bad_in_good:5.1f}%')} "
            f"({crc_err_rate:4.1f}% failure rate)"
        )
        lines.append(
            f"  Packets per Minute Rate  : "
            f"Unique: {self.colorize(COLOR_GREEN + COLOR_BOLD, f'{overall_ppm:,}')} Pkts/m (dedup)  |  "
            f"Raw Demod: {self.colorize(COLOR_CYAN + COLOR_BOLD, f'{win_crc_g:,}')} Pkts/m (pre-dedup)  |  "
            f"Lifetime Unique: {self.colorize(COLOR_WHITE, f'{total_p:,}')}"
        )
        lines.append(self.colorize(COLOR_DIM, "-" * 80))

        # Per-Process Table
        table_hdr = " Proc | ZMQ Port | Chns           | Sync         | CRC G/B               | Pkts/m (Dedup/Raw) |"
        lines.append(self.colorize(COLOR_BOLD, table_hdr))
        buckets = distribute_channels_across_workers(self.channels, self.worker_count)
        for i in range(self.worker_count):
            st = self.worker_stats.get(i, {})
            ch_str = ",".join(str(c) for c in buckets[i])
            if len(ch_str) > 14:
                ch_str = ch_str[:11] + "..."
            proc_ppm = len(self.worker_packet_timestamps[i])
            raw_proc_ppm = sum(d[1] for d in self.worker_raw_deltas[i]) if (hasattr(self, "worker_raw_deltas") and i < len(self.worker_raw_deltas)) else 0
            ppm_display = f"{proc_ppm:,} / {raw_proc_ppm:,}"
            sync_c = st.get("sync_detected", 0)
            sync_str = f"{sync_c:,}"
            crc_g = st.get("crc_good", 0)
            crc_b = st.get("crc_bad", 0)
            crc_str = f"{crc_g:,}/{crc_b:,}"
            lines.append(
                f" #{i:<3} | {self.zmq_ports[i]:<8} | {ch_str:<14} | "
                f"{sync_str:<12} | {crc_str:<21} | {ppm_display:<18} |"
            )
        lines.append(self.colorize(COLOR_DIM, "-" * len(table_hdr)))

        # Recent Decoded Packets & Telemetry
        lines.append(self.colorize(COLOR_BOLD, " Recent Decoded Packets (Viterbi K=7 + RS(16,12) + CRC-16/ARC verified):"))
        if not self.recent_events:
            lines.append(self.colorize(COLOR_DIM, "   (Listening for RF bursts across sampling phases...)"))
        else:
            for ev in list(self.recent_events)[-6:]:
                t_str = ev["time_str"]
                ch = ev["channel"]
                rate = ev["rate"]
                snr = ev["snr"]
                rssi = ev["rssi"]
                desc = ev["desc"]
                telem = ev.get("telemetry", {})

                detail_str = ""
                if "lat" in telem and "lon" in telem:
                    detail_str = f"Base {telem.get('radio_id')} ({telem['lat']:.4f}, {telem['lon']:.4f})"
                elif "utc_time" in telem:
                    detail_str = f"Time {telem['utc_time']}"
                elif "switch" in telem:
                    detail_str = f"WIU {telem.get('wiu_id')} Sw:{telem['switch']}"
                elif "emsg_name" in telem:
                    detail_str = f"EMP {telem['emsg_name']}"
                else:
                    detail_str = ev["hex"][:24] + ".."

                df_val = ev.get("freq_offset_hz")
                df_str = f" Δf:{df_val:+.0f}Hz" if df_val is not None else ""
                lines.append(
                    f"   [{t_str}] Ch {ch:3d} ({rate}) | RSSI:{rssi:5.1f} SNR:{snr:4.1f}{df_str} | "
                    f"{self.colorize(COLOR_GREEN, desc):<10} | {self.colorize(COLOR_YELLOW, detail_str)}"
                )

        lines.append(self.colorize(COLOR_DIM, "-" * 80))
        lines.append(f" Status: {self.colorize(COLOR_WHITE, self.status_banner)}")
        lines.append(self.colorize(COLOR_DIM, " Controls: 'p <ppm>', 'g <gain|auto>', 'b <0|1>', 'tune <on|off>', 'afc <on|off>', 'dedup <sec>', '+ <ch>', '- <ch>', 'chans', 'q' to quit"))

        # Cross-platform screen refresh: VT cursor-home for Windows 11/Linux/macOS, or native clear fallback
        if self.use_color:
            sys.stdout.write("\033[H\033[2J")
        else:
            clear_cmd = "cls" if os.name == "nt" else "clear"
            try:
                os.system(clear_cmd)
            except Exception:
                sys.stdout.write("\n" * 2)

        current_cmd = self.cmd_thread.get_current_buffer() if hasattr(self, "cmd_thread") and self.cmd_thread else ""
        sys.stdout.write("\n".join(lines) + f"\n Command > {current_cmd}")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Backward Compatibility Alias
# ---------------------------------------------------------------------------
EmpCDemodApplication = ApexBOSGrabberApplication


# ---------------------------------------------------------------------------
# Default Configuration & Parser Helpers
# ---------------------------------------------------------------------------
def get_grabber_default_config() -> dict:
    """Returns the default Grabber configuration dictionary."""
    return {
        "pipe": True,
        "stdin": False,
        "rtl_sdr_bin": None,
        "rtl_tcp": False,
        "rtlsdr": False,
        "host": "127.0.0.1",
        "port": 1234,
        "device": 0,
        "rate": DEFAULT_SAMPLE_RATE,
        "gain": "0",
        "bias_tee": 1,
        "ppm": 0,
        "bank_size": DEFAULT_CHUNK_SIZE,
        "max_errors": 8,
        "auto_tune": False,
        "tune_interval": 5.0,
        "channel_afc": True,
        "afc_limit": 500.0,
        "dedup_ttl": 1.0,
        "chans": None,
        "chans_file": None,
        "channel_timeout": 300.0,
        "procs": "5555",
        "workers": None,
        "zmq_port": None,
        "udp_port": None,
        "no_color": False,
        "full": False,
        "all": False,
        "no_rssi": False,
    }


def generate_default_config(filepath: str = "grabber_config.json") -> str:
    """Writes an example configuration file with default values."""
    data = get_grabber_default_config()
    target_dir = os.path.dirname(os.path.abspath(filepath))
    if target_dir and not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        f.write("\n")
    return filepath


class ApexBOSGrabberArgumentParser(argparse.ArgumentParser):
    """
    CLI Argument Parser for ApexBOSGrabber with optional --config JSON support.
    When --config is specified, configuration values from the JSON file override
    parser defaults, while explicit command-line flags retain precedence.
    """
    def parse_args(self, args=None, namespace=None):
        pre_parser = argparse.ArgumentParser(add_help=False)
        pre_parser.add_argument("--config", default=None)
        pre_args, _ = pre_parser.parse_known_args(args)

        if getattr(pre_args, "config", None):
            cfg_path = pre_args.config
            if not os.path.exists(cfg_path):
                self.error(f"Configuration file '{cfg_path}' not found.")
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
            except Exception as exc:
                self.error(f"Failed to load configuration file '{cfg_path}': {exc}")

            if isinstance(cfg_data, dict):
                if "grabber" in cfg_data and isinstance(cfg_data["grabber"], dict):
                    merged = dict(cfg_data["grabber"])
                    for k, v in cfg_data.items():
                        if k != "grabber" and k not in merged:
                            merged[k] = v
                    cfg_data = merged

                clean_cfg = {}
                for k, v in cfg_data.items():
                    norm_k = str(k).strip().lower().replace("-", "_")
                    if norm_k in ("chans", "procs") and isinstance(v, (list, tuple)):
                        v = ",".join(str(item).strip() for item in v)
                    elif norm_k == "bias_tee" and isinstance(v, bool):
                        v = 1 if v else 0
                    elif norm_k == "gain" and not isinstance(v, str) and v is not None:
                        v = str(v)
                    clean_cfg[norm_k] = v

                # Handle aliases
                if "afc" in clean_cfg and "channel_afc" not in clean_cfg:
                    clean_cfg["channel_afc"] = clean_cfg["afc"]
                if "enable_afc" in clean_cfg and "channel_afc" not in clean_cfg:
                    clean_cfg["channel_afc"] = clean_cfg["enable_afc"]
                if "auto_tune_interval" in clean_cfg and "tune_interval" not in clean_cfg:
                    clean_cfg["tune_interval"] = clean_cfg["auto_tune_interval"]
                if "dedup_window" in clean_cfg and "dedup_ttl" not in clean_cfg:
                    clean_cfg["dedup_ttl"] = clean_cfg["dedup_window"]
                if "dedup_sec" in clean_cfg and "dedup_ttl" not in clean_cfg:
                    clean_cfg["dedup_ttl"] = clean_cfg["dedup_sec"]
                if "rtl_port" in clean_cfg and "port" not in clean_cfg:
                    clean_cfg["port"] = clean_cfg["rtl_port"]
                if "pyrtlsdr" in clean_cfg and "rtlsdr" not in clean_cfg:
                    clean_cfg["rtlsdr"] = clean_cfg["pyrtlsdr"]

                self.set_defaults(**clean_cfg)

        return super().parse_args(args=args, namespace=namespace)


# ---------------------------------------------------------------------------
# CLI Parser
# ---------------------------------------------------------------------------
def build_cli_parser():
    parser = ApexBOSGrabberArgumentParser(
        description=f"ApexBOSGrabber ({VERSION}) - High-Performance ITCR/PTC SDR Demodulator & Decoder"
    )
    # Configuration File
    parser.add_argument(
        "--config",
        default=None,
        help="Path to JSON configuration file providing option values (e.g. 'grabber_config.json')",
    )
    parser.add_argument(
        "--gen-config",
        action="store_true",
        help="Generate an example configuration file with default values and exit",
    )

    # SDR Source Mode
    parser.add_argument("--pipe", action="store_true", help="Read raw I/Q from rtl_sdr subprocess pipe (default)")
    parser.add_argument("--stdin", action="store_true", help="Read raw I/Q from standard input")
    parser.add_argument("--rtl-sdr-bin", default=None, help="Custom path to rtl_sdr executable")
    parser.add_argument("--rtl-tcp", action="store_true", help="Connect directly to an RTL-TCP server")
    parser.add_argument(
        "--rtlsdr", "--pyrtlsdr",
        dest="rtlsdr",
        action="store_true",
        default=False,
        help="Read raw I/Q directly using the Python rtlsdr package (pyrtlsdr)",
    )
    parser.add_argument(
        "--auto", "--auto-detect",
        dest="auto_mode",
        action="store_true",
        default=False,
        help="Automatically probe and select available SDR source (rtl_sdr, rtl_tcp, or pyrtlsdr)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="RTL-TCP server host (default: 127.0.0.1)")
    parser.add_argument("--port", "--rtl-port", dest="port", type=int, default=1234, help="RTL-TCP port (default: 1234)")
    parser.add_argument("--device", type=int, default=0, help="RTL-SDR device index (default: 0)")

    # RF & Baseband Parameters
    parser.add_argument("--rate", type=int, default=DEFAULT_SAMPLE_RATE, help="SDR sample rate in S/s (default: 192000, auto-scaled if channels span wider bandwidth)")
    parser.add_argument("--gain", default="0", help="Tuner gain in dB or tenths of dB (0 for auto/AGC, e.g. '42', '42.0', or '420')")
    parser.add_argument("-b", "--bias-tee", dest="bias_tee", type=int, default=1, choices=[0, 1], help="Enable bias tee to power inline LNA / antenna amp (default: 1 = on, matches c_demod)")
    parser.add_argument("--ppm", type=int, default=0, help="Frequency correction in PPM (default: 0)")
    parser.add_argument("--bank-size", type=int, default=DEFAULT_CHUNK_SIZE, help=f"I/Q samples per bank (default: {DEFAULT_CHUNK_SIZE})")
    parser.add_argument("-E", "--max-errors", dest="max_errors", type=int, default=8, help="Max unique word Hamming bit errors (default: 8)")
    parser.add_argument("--auto-tune", action="store_true", default=False, help="Enable automated fast PID adjustments in PPM and Gain to minimize error rates (default: disabled)")
    parser.add_argument("--tune-interval", "--auto-tune-interval", type=float, default=5.0, help="Evaluation interval in seconds for automated PID tuner (default: 5.0s)")
    parser.add_argument("--pid", dest="auto_tune", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--channel-afc", "--afc", "--enable-afc",
        dest="channel_afc",
        nargs="?",
        const=True,
        default=True,
        help="Enable per-channel dynamic frequency error correction (default: enabled, use --no-afc to disable)",
    )
    parser.add_argument(
        "--no-channel-afc", "--no-afc", "--disable-afc",
        dest="channel_afc",
        action="store_false",
        help="Disable per-channel dynamic frequency error correction",
    )
    parser.add_argument("--afc-limit", "--afc-window", "--max-afc-offset", dest="afc_limit", type=float, default=500.0, help="Maximum allowable per-channel frequency correction in Hz (default: 500.0 Hz, cap: ±500 Hz)")
    parser.add_argument("--dedup-window", "--dedup-ttl", "--dedup-sec", dest="dedup_ttl", type=float, default=1.0, help="Duplicate packet deduplication expiration window in seconds (default: 1.0s, after 1.0s duplicate is re-emitted)")

    # Channels
    parser.add_argument("--chans", default=None, help="Comma-separated channels (e.g. '101,102.,107' or '101-134'); suffix 'l', 'p', or '.' locks channel in")
    parser.add_argument("--chans-file", default=None, help="Path to JSON channels file (e.g. channels.json)")
    parser.add_argument("--channel-timeout", type=float, default=300.0, help="Inactivity timeout in seconds before dropping non-base channels (default: 300 = 5 min)")

    # Multiprocessing & Network Outputs
    parser.add_argument("--procs", default="5555", help="Comma-separated ZMQ PUB ports (e.g. '5555,5556')")
    parser.add_argument("--workers", type=int, default=None, help="Number of worker processes (overrides --procs port count)")
    parser.add_argument("--zmq-port", type=int, default=None, help="Base ZMQ PUB port (default: 5555)")
    parser.add_argument("--udp-port", type=int, default=None, help="Base UDP port (default: same as ZMQ port)")

    # Display & Compatibility
    parser.add_argument("--no-color", action="store_true", help="Disable colored terminal output")
    parser.add_argument("--full", action="store_true", help="Print full packet hex and details")
    parser.add_argument("--all", action="store_true", help="Print all messages including unknown transmitters")
    parser.add_argument("--no-rssi", action="store_true", help="Send raw hex strings without RSSI to ZMQ and UDP (for legacy applications expecting pure hex)")

    return parser


def main():
    check_required_dependencies(exit_on_failure=True)

    parser = build_cli_parser()
    args = parser.parse_args()

    if getattr(args, "gen_config", False):
        target_path = args.config or "grabber_config.json"
        generate_default_config(target_path)
        print(f"[*] Generated default Grabber configuration: {target_path}")
        return 0

    if sys.platform == "win32":
        try:
            # Terminate lingering legacy s2p.exe demodulator processes squatting on ZMQ ports
            subprocess.run(["taskkill", "/F", "/IM", "s2p.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    app = ApexBOSGrabberApplication(args)
    app.run()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
