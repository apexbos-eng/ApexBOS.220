#!/usr/bin/env python3
"""
ApexBOSChroma.py - PTC 220 MHz Real-Time Spectrum Analyzer & Waterfall Studio
Copyright (c) 2026 AB. Licensed under the MIT License.

Provides a GQRX/SDR#-style live FFT spectrum and scrolling waterfall tailored
specifically to the ITC 220 MHz PTC band (Channels 97-176, 220.0125-221.9875 MHz):
  - Runs alongside ApexBOSGrabber from the SAME RTL-SDR via a gated non-blocking
    ZMQ XPUB I/Q snapshot tap (zero Grabber overhead when Chroma is closed).
  - Dynamic Wideband mode: optionally expands Grabber's sample rate to 2.304 MS/s
    (full Ch 97-176) while Chroma is open, automatically reverting when closed.
  - Live PPM, Gain, Bias-T, Auto-PPM, and channel add/remove controls via
    Grabber's non-blocking ZMQ REP control bridge.
  - Fading RSSI peak-hold envelope + optional infinite Max-Hold + ADC clipping alert.
  - PTC 25 kHz channel ruler highlighting monitored and locked channels.
  - Real-time packet "blips" flowing down the waterfall (from Grabber's worker
    ZMQ PUB sockets) with optional WIU ID / src->dst / message-type / Δf overlays
    and hover/click packet inspection.
  - Zoomable, pannable, resizable Pygame interface with fast NumPy ring-buffer
    waterfall rendering and Standalone SDR / Mock offline fallback modes.
"""

import os
import sys

# --- LINUX NVIDIA / NOUVEAU & XORG GLAMOR CRASH FIX ---
# MUST BE SET BEFORE PYGAME IS IMPORTED!
if sys.platform.startswith("linux"):
    os.environ["SDL_RENDER_DRIVER"] = "software"
    os.environ["SDL_FRAMEBUFFER_ACCELERATION"] = "0"
    os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"
    os.environ["GALLIUM_DRIVER"] = "llvmpipe"
    os.environ["SDL_VIDEO_X11_XSHM"] = "0"
    os.environ["SDL_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR"] = "0"

import glob
import json
import math
import time
import queue
import argparse
import threading
import collections
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

try:
    import zmq
except ImportError:
    zmq = None

import config_loader
from version import VERSION

# ---------------------------------------------------------------------------
# PTC Band Constants (Standard ITC 220 MHz Band: Ch 97 - 176)
# ---------------------------------------------------------------------------
PTC_CH_MIN = 97
PTC_CH_MAX = 176
PTC_BASE_HZ = 217587500
PTC_STEP_HZ = 25000

DEFAULT_CHROMA_IQ_PORT = 18099
DEFAULT_CHROMA_CTRL_PORT = 18098
WIDEBAND_SAMPLE_RATE = 2304000  # 12 * 192 kS/s (covers full Ch 97-176 cleanly)
WIDEBAND_CENTER_HZ = 221000000

# Standard R820T/R820T2 RTL-SDR discrete gain steps (dB)
RTLSDR_GAIN_STEPS = [
    0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7,
    22.9, 25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4,
    43.9, 44.5, 48.0, 49.6,
]


def itc_channel_frequency(channel_num: int) -> int:
    """ITC 220 MHz channel center frequency in Hz."""
    return PTC_BASE_HZ + int(channel_num) * PTC_STEP_HZ


def freq_to_nearest_channel(freq_hz: float) -> Tuple[int, float]:
    """Returns (nearest_channel_num, offset_hz_from_channel_center)."""
    ch = int(round((float(freq_hz) - PTC_BASE_HZ) / float(PTC_STEP_HZ)))
    offset = float(freq_hz) - itc_channel_frequency(ch)
    return ch, offset


# ---------------------------------------------------------------------------
# Colormap LUT Builders (256 x 3 uint8 RGB arrays for vectorized NumPy blits)
# ---------------------------------------------------------------------------
def _interp_lut(stops: List[Tuple[float, Tuple[int, int, int]]]) -> np.ndarray:
    xs = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    stop_x = np.array([s[0] for s in stops], dtype=np.float32)
    lut = np.zeros((256, 3), dtype=np.uint8)
    for c in range(3):
        stop_v = np.array([s[1][c] for s in stops], dtype=np.float32)
        lut[:, c] = np.clip(np.interp(xs, stop_x, stop_v), 0, 255).astype(np.uint8)
    return lut


def build_color_palettes() -> Dict[str, np.ndarray]:
    return {
        "SDR#": _interp_lut([
            (0.00, (4, 8, 22)),
            (0.18, (8, 24, 72)),
            (0.36, (12, 78, 155)),
            (0.52, (18, 175, 210)),
            (0.68, (60, 225, 110)),
            (0.80, (245, 230, 45)),
            (0.91, (255, 105, 20)),
            (0.97, (255, 25, 25)),
            (1.00, (255, 255, 255)),
        ]),
        "Turbo": _interp_lut([
            (0.00, (18, 14, 48)),
            (0.16, (42, 78, 215)),
            (0.34, (24, 175, 230)),
            (0.52, (78, 225, 115)),
            (0.70, (235, 220, 45)),
            (0.86, (248, 105, 28)),
            (1.00, (180, 22, 22)),
        ]),
        "Viridis": _interp_lut([
            (0.00, (12, 8, 24)),
            (0.20, (68, 1, 84)),
            (0.40, (59, 82, 139)),
            (0.60, (33, 145, 140)),
            (0.80, (94, 201, 98)),
            (1.00, (253, 231, 37)),
        ]),
        "Inferno": _interp_lut([
            (0.00, (4, 4, 10)),
            (0.22, (58, 15, 105)),
            (0.48, (140, 41, 129)),
            (0.72, (222, 73, 104)),
            (0.88, (254, 159, 68)),
            (1.00, (252, 253, 191)),
        ]),
        "Phosphor": _interp_lut([
            (0.00, (4, 10, 6)),
            (0.30, (8, 48, 22)),
            (0.62, (22, 145, 58)),
            (0.85, (90, 245, 120)),
            (1.00, (230, 255, 235)),
        ]),
    }


COLOR_PALETTES = build_color_palettes()
PALETTE_NAMES = list(COLOR_PALETTES.keys())


# ---------------------------------------------------------------------------
# FFT Spectrum, Fading Peak-Hold, and Ring-Buffer Waterfall Engine
# ---------------------------------------------------------------------------
class ChromaSpectrumEngine:
    """
    Computes windowed FFT power spectra (dBFS), maintains an EMA-smoothed live
    trace, a decaying peak-hold envelope, an optional infinite max-hold trace,
    and a raw float32 dBFS ring-buffer waterfall for lossless zoom/pan/contrast.
    """

    def __init__(
        self,
        fft_size: int = 4096,
        max_rows: int = 480,
        ema_alpha: float = 0.35,
        peak_hold_sec: float = 1.6,
        peak_decay_db_per_sec: float = 9.0,
    ):
        self.fft_size = int(fft_size)
        self.max_rows = int(max_rows)
        self.ema_alpha = float(ema_alpha)
        self.peak_hold_sec = float(peak_hold_sec)
        self.peak_decay_db_per_sec = float(peak_decay_db_per_sec)

        self.center_freq: int = WIDEBAND_CENTER_HZ
        self.sample_rate: int = WIDEBAND_SAMPLE_RATE
        self.noise_floor_db: float = -80.0
        self.peak_overall_db: float = -100.0
        self.adc_clipping: bool = False
        self.clip_ratio: float = 0.0
        self.frames_processed: int = 0
        self.last_update_ts: float = 0.0
        self._ref_noise_floor_db: Optional[float] = None
        self.agc_comp_db: float = 0.0

        self._init_buffers(self.fft_size)

    def _init_buffers(self, fft_size: int):
        self.fft_size = int(fft_size)
        self._window = np.hanning(self.fft_size).astype(np.float32)
        # Coherent power normalization for 8-bit RTL-SDR full-scale (127.5)
        self._win_norm = float(np.sum(self._window)) * 127.5
        if self._win_norm <= 0:
            self._win_norm = float(self.fft_size) * 127.5

        self.live_db = np.full(self.fft_size, -100.0, dtype=np.float32)
        self.ema_db = np.full(self.fft_size, -100.0, dtype=np.float32)
        self.fade_peak_db = np.full(self.fft_size, -100.0, dtype=np.float32)
        self.fade_peak_ts = np.zeros(self.fft_size, dtype=np.float64)
        self.max_hold_db = np.full(self.fft_size, -100.0, dtype=np.float32)
        self._has_ema = False
        self._ref_noise_floor_db = None
        self.agc_comp_db = 0.0

        # Ring buffer of raw dBFS rows (most recent row at (write_idx - 1) % max_rows)
        self.wf_db = np.full((self.max_rows, self.fft_size), -110.0, dtype=np.float32)
        self.wf_ts = np.zeros(self.max_rows, dtype=np.float64)
        self.wf_center = np.zeros(self.max_rows, dtype=np.int64)
        self.wf_rate = np.zeros(self.max_rows, dtype=np.int64)
        self.write_idx = 0
        self.rows_filled = 0
        self._last_decay_ts = time.monotonic()

    def set_fft_size(self, new_size: int):
        new_size = int(new_size)
        if new_size != self.fft_size and new_size in (1024, 2048, 4096, 8192, 16384):
            self._init_buffers(new_size)

    def reset_peaks(self):
        self.fade_peak_db[:] = self.ema_db
        self.fade_peak_ts[:] = time.monotonic()
        self.max_hold_db[:] = self.ema_db

    def clear_waterfall(self):
        self.wf_db.fill(-110.0)
        self.wf_ts.fill(0.0)
        self.wf_center.fill(0)
        self.wf_rate.fill(0)
        self.write_idx = 0
        self.rows_filled = 0

    def process_iq_bytes(
        self,
        raw_iq: bytes,
        center_freq: int,
        sample_rate: int,
        now_mono: Optional[float] = None,
        paused: bool = False,
        agc_stabilize: bool = False,
        sample_format: str = "u8",
    ) -> Optional[np.ndarray]:
        """
        Computes vectorized multi-segment adaptive Welch FFT across raw uint8 ('u8')
        or signed 16-bit CS16 ('s16', e.g. SDRplay RSP1B / SoapySDR) interleaved I/Q bytes
        and updates EMA spectrum, fading RSSI peak-hold, max-hold, and waterfall ring buffer.
        When agc_stabilize=True (hardware AGC active), tracks the 25th-percentile
        noise floor to cancel R820T AGC gain-pumping zebra stripes.
        Returns latest dBFS array or None if input is too short.
        """
        if not raw_iq or len(raw_iq) < 64:
            return None
        now_m = time.monotonic() if now_mono is None else float(now_mono)

        if sample_format == "s16":
            n_i16 = len(raw_iq) // 2
            if n_i16 < 2:
                return None
            raw_i16 = np.frombuffer(raw_iq[:n_i16 * 2], dtype="<i2")
            clip_count = int(np.count_nonzero((raw_i16 >= 32700) | (raw_i16 <= -32700)))
            self.clip_ratio = clip_count / float(len(raw_i16))
            arr = raw_i16.astype(np.float32) * (127.5 / 32768.0)
        else:
            raw_u8 = np.frombuffer(raw_iq, dtype=np.uint8)
            if len(raw_u8) < 2:
                return None
            # ADC Clipping check (0 or 255 saturation)
            clip_count = int(np.count_nonzero((raw_u8 == 0) | (raw_u8 == 255)))
            self.clip_ratio = clip_count / float(len(raw_u8))
            arr = raw_u8.astype(np.float32) - 127.5

        n_samps = len(arr) // 2
        iq = arr[0:n_samps * 2:2] + 1j * arr[1:n_samps * 2:2]
        # Remove DC spike (RTL-SDR zero-IF DC offset)
        if n_samps > 16:
            iq = iq - np.mean(iq)

        # If tuning or sample rate changed, reset EMA & peak-hold so old bins don't smear
        if int(center_freq) != self.center_freq or int(sample_rate) != self.sample_rate:
            self.center_freq = int(center_freq)
            self.sample_rate = int(sample_rate)
            self._has_ema = False
            self._ref_noise_floor_db = None
            self.agc_comp_db = 0.0
            self.fade_peak_db.fill(-100.0)
            self.max_hold_db.fill(-100.0)

        # Vectorized multi-segment adaptive burst-preserving Welch FFT:
        # - Noise bins use smooth 16-segment Welch average (eliminates horizontal ripple)
        # - Genuine short PTC bursts (p_max >> p_avg) smoothly blend toward p_max
        nfft = self.fft_size
        inv_norm2 = np.float32(1.0 / (self._win_norm * self._win_norm))
        if n_samps < nfft:
            padded = np.zeros(nfft, dtype=np.complex64)
            padded[:n_samps] = iq
            spec = np.fft.fftshift(np.fft.fft(padded * self._window))
            mag2 = (spec.real * spec.real + spec.imag * spec.imag) * inv_norm2
            db = (10.0 * np.log10(np.maximum(mag2, 1e-12))).astype(np.float32)
        else:
            step = max(nfft // 2, 1)
            starts = list(range(0, n_samps - nfft + 1, step))
            max_segs = 16 if nfft <= 4096 else (8 if nfft <= 8192 else 4)
            if len(starts) > max_segs:
                idxs = np.linspace(0, len(starts) - 1, max_segs, dtype=int)
                starts = [starts[i] for i in idxs]
            batch = np.empty((len(starts), nfft), dtype=np.complex64)
            for row_i, st in enumerate(starts):
                batch[row_i] = iq[st:st + nfft]
            batch *= self._window[np.newaxis, :]
            spec = np.fft.fftshift(np.fft.fft(batch, axis=1), axes=1)
            mag2 = (spec.real * spec.real + spec.imag * spec.imag) * inv_norm2
            if len(starts) == 1:
                p_blend = mag2[0]
            else:
                p_avg = np.mean(mag2, axis=0)
                p_max = np.max(mag2, axis=0)
                ratio = p_max / np.maximum(p_avg, 1e-12)
                burst_w = np.clip((ratio - 2.5) / 4.0, 0.0, 0.65).astype(np.float32)
                p_blend = burst_w * p_max + (1.0 - burst_w) * p_avg
            db = (10.0 * np.log10(np.maximum(p_blend, 1e-12))).astype(np.float32)

        raw_nf = float(np.percentile(db, 25))
        raw_peak = float(np.max(db))
        self.adc_clipping = (self.clip_ratio > 0.002) or (raw_peak >= -1.0)

        # Cancel R820T hardware AGC gain-pumping zebra stripes when Gain is in AGC mode
        if agc_stabilize:
            if self._ref_noise_floor_db is None:
                self._ref_noise_floor_db = raw_nf
            elif not paused:
                self._ref_noise_floor_db = 0.985 * self._ref_noise_floor_db + 0.015 * raw_nf
            delta = float(np.clip(self._ref_noise_floor_db - raw_nf, -12.0, 12.0))
            db = (db + delta).astype(np.float32)
            self.agc_comp_db = delta
        else:
            self._ref_noise_floor_db = raw_nf
            self.agc_comp_db = 0.0

        self.live_db = db
        self.noise_floor_db = float(np.percentile(db, 25))
        self.peak_overall_db = float(np.max(db))

        if not self._has_ema:
            self.ema_db[:] = db
            self.fade_peak_db[:] = db
            self.fade_peak_ts[:] = now_m
            self.max_hold_db[:] = db
            self._has_ema = True
            self._last_decay_ts = now_m
        elif not paused:
            a = self.ema_alpha
            self.ema_db = (a * db + (1.0 - a) * self.ema_db).astype(np.float32)

            # Update fading peak-hold
            dt = max(0.0, min(0.5, now_m - self._last_decay_ts))
            self._last_decay_ts = now_m

            expired = (now_m - self.fade_peak_ts) > self.peak_hold_sec
            if np.any(expired):
                decayed = self.fade_peak_db[expired] - float(self.peak_decay_db_per_sec * dt)
                self.fade_peak_db[expired] = np.maximum(decayed, self.ema_db[expired])

            higher = db >= self.fade_peak_db
            if np.any(higher):
                self.fade_peak_db[higher] = db[higher]
                self.fade_peak_ts[higher] = now_m

            np.maximum(self.max_hold_db, db, out=self.max_hold_db)

        if not paused:
            idx = self.write_idx
            self.wf_db[idx] = db
            self.wf_ts[idx] = now_m
            self.wf_center[idx] = self.center_freq
            self.wf_rate[idx] = self.sample_rate
            self.write_idx = (idx + 1) % self.max_rows
            if self.rows_filled < self.max_rows:
                self.rows_filled += 1

        self.frames_processed += 1
        self.last_update_ts = now_m
        return db

    def get_ordered_waterfall_indices(self, n_rows: int) -> np.ndarray:
        """Returns ring-buffer row indices from newest (top) to oldest."""
        count = min(int(n_rows), self.rows_filled)
        if count <= 0:
            return np.empty(0, dtype=np.int32)
        newest = (self.write_idx - 1) % self.max_rows
        return (newest - np.arange(count, dtype=np.int32)) % self.max_rows

    def get_visible_row_timeline(
        self,
        height_px: int,
        rows_per_frame_px: int = 1,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Returns (asc_timestamps, asc_row_offsets) for all waterfall rows currently
        visible inside `height_px`, sorted oldest -> newest for exact np.interp
        lookup so packet blips stay pixel-locked to their waterfall rows with
        zero depth-amplified jitter.
        """
        h = max(1, int(height_px))
        r_step = max(1, int(rows_per_frame_px))
        n_data_rows = min(self.rows_filled, (h + r_step - 1) // r_step)
        if n_data_rows <= 0:
            return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64)
        row_idxs = self.get_ordered_waterfall_indices(n_data_rows)
        desc_ts = self.wf_ts[row_idxs]
        # Reverse to oldest -> newest (monotonically non-decreasing)
        asc_ts = desc_ts[::-1]
        asc_rows = np.arange(len(row_idxs) - 1, -1, -1, dtype=np.float64)
        return asc_ts, asc_rows

    def timestamp_to_waterfall_y(
        self,
        ts: float,
        wf_top: int,
        wf_height: int,
        rows_per_frame_px: int,
        asc_ts: np.ndarray,
        asc_rows: np.ndarray,
        paused: bool = False,
        pause_ts: float = 0.0,
    ) -> Optional[int]:
        """
        Maps a monotonic blip timestamp directly onto the rendered waterfall Y pixel
        using exact row-timestamp interpolation (eliminating rate-estimation jitter).
        Returns None if the blip is outside the visible waterfall span.
        """
        if len(asc_ts) == 0:
            return None
        if paused and pause_ts > 0.0 and ts > pause_ts:
            return None
        if ts < asc_ts[0]:
            return None
        r_step = max(1, int(rows_per_frame_px))
        if ts >= asc_ts[-1]:
            if (ts - asc_ts[-1]) > 0.75:
                return None
            row_f = 0.0
        elif len(asc_ts) == 1:
            row_f = 0.0
        else:
            row_f = float(np.interp(ts, asc_ts, asc_rows))
        by = int(round(wf_top + (row_f + 0.5) * r_step))
        if by < wf_top or by > wf_top + wf_height - 3:
            return None
        return max(wf_top + 2, by)

    def render_waterfall_rgb(
        self,
        view_f_min: float,
        view_f_max: float,
        width_px: int,
        height_px: int,
        min_db: float,
        max_db: float,
        lut: np.ndarray,
        rows_per_frame_px: int = 1,
    ) -> np.ndarray:
        """
        Vectorized NumPy waterfall rasterizer.
        Maps visible frequency window [view_f_min, view_f_max] across width_px
        columns and renders ring-buffer history into a (height_px, width_px, 3) uint8 RGB image.
        """
        w = max(1, int(width_px))
        h = max(1, int(height_px))
        img = np.empty((h, w, 3), dtype=np.uint8)
        img[:, :, :] = lut[0]

        if self.rows_filled <= 0 or view_f_max <= view_f_min:
            return img

        r_step = max(1, int(rows_per_frame_px))
        n_data_rows = min(self.rows_filled, (h + r_step - 1) // r_step)
        row_idxs = self.get_ordered_waterfall_indices(n_data_rows)
        if len(row_idxs) == 0:
            return img

        col_freqs = np.linspace(view_f_min, view_f_max, w, dtype=np.float64)
        span_db = max(1.0, float(max_db) - float(min_db))
        inv_span = 255.0 / span_db

        # Group contiguous rows sharing the same (center, rate) for ultra-fast vectorized indexing
        rates = self.wf_rate[row_idxs]
        centers = self.wf_center[row_idxs]

        # Fast path: all visible rows share current center & rate
        if np.all(rates == rates[0]) and np.all(centers == centers[0]) and rates[0] > 0:
            c0 = float(centers[0])
            r0 = float(rates[0])
            f_start = c0 - r0 * 0.5
            bin_f = (col_freqs - f_start) * (self.fft_size / r0)
            valid = (bin_f >= 0.0) & (bin_f < self.fft_size)
            bin_i = np.clip(np.round(bin_f).astype(np.int32), 0, self.fft_size - 1)
            sub_db = self.wf_db[row_idxs][:, bin_i]
            if not np.all(valid):
                sub_db[:, ~valid] = min_db - 20.0
            q = np.clip((sub_db - min_db) * inv_span, 0, 255).astype(np.uint8)
            rgb_rows = lut[q]
        else:
            rgb_rows = np.empty((len(row_idxs), w, 3), dtype=np.uint8)
            rgb_rows[:, :, :] = lut[0]
            for out_r, ring_r in enumerate(row_idxs):
                r0 = float(self.wf_rate[ring_r])
                if r0 <= 0:
                    continue
                c0 = float(self.wf_center[ring_r])
                f_start = c0 - r0 * 0.5
                bin_f = (col_freqs - f_start) * (self.fft_size / r0)
                valid = (bin_f >= 0.0) & (bin_f < self.fft_size)
                bin_i = np.clip(np.round(bin_f).astype(np.int32), 0, self.fft_size - 1)
                row_db = self.wf_db[ring_r, bin_i]
                q = np.clip((row_db - min_db) * inv_span, 0, 255).astype(np.uint8)
                row_rgb = lut[q]
                if not np.all(valid):
                    row_rgb[~valid] = lut[0]
                rgb_rows[out_r] = row_rgb

        if r_step == 1:
            n_copy = min(h, rgb_rows.shape[0])
            img[:n_copy] = rgb_rows[:n_copy]
        else:
            expanded = np.repeat(rgb_rows, r_step, axis=0)
            n_copy = min(h, expanded.shape[0])
            img[:n_copy] = expanded[:n_copy]
        return img

    def sample_curve_for_view(
        self,
        db_array: np.ndarray,
        view_f_min: float,
        view_f_max: float,
        width_px: int,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Maps a full-FFT dBFS array to `width_px` screen columns using max-pooling
        when multiple FFT bins fall inside a single pixel (so narrow PTC peaks never vanish!).
        Returns (values_db[width_px], valid_mask[width_px]).
        """
        w = max(2, int(width_px))
        if self.sample_rate <= 0 or view_f_max <= view_f_min:
            return np.full(w, -110.0, dtype=np.float32), np.zeros(w, dtype=bool)

        f_start = self.center_freq - self.sample_rate * 0.5
        col_edges = np.linspace(view_f_min, view_f_max, w + 1, dtype=np.float64)
        bin_edges = (col_edges - f_start) * (self.fft_size / float(self.sample_rate))

        col_centers = 0.5 * (bin_edges[:-1] + bin_edges[1:])
        valid = (col_centers >= 0.0) & (col_centers < self.fft_size)

        bins_per_px = (bin_edges[-1] - bin_edges[0]) / float(w)
        if bins_per_px <= 1.25:
            # Zoomed in: direct interpolation
            idx = np.clip(col_centers, 0.0, self.fft_size - 1.0)
            vals = np.interp(idx, np.arange(self.fft_size, dtype=np.float64), db_array).astype(np.float32)
        else:
            # Zoomed out: max-pool bins per pixel so narrow bursts remain bright & visible
            b0 = np.clip(np.floor(bin_edges[:-1]).astype(np.int32), 0, self.fft_size - 1)
            b1 = np.clip(np.ceil(bin_edges[1:]).astype(np.int32), 1, self.fft_size)
            b1 = np.maximum(b1, b0 + 1)
            vals = np.empty(w, dtype=np.float32)
            for i in range(w):
                if valid[i]:
                    vals[i] = float(np.max(db_array[b0[i]:b1[i]]))
                else:
                    vals[i] = -110.0

        if not np.all(valid):
            vals[~valid] = -110.0
        return vals, valid


# ---------------------------------------------------------------------------
# Packet Blip Enrichment & Waterfall Correlation
# ---------------------------------------------------------------------------
PKT_TYPE_COLORS = {
    "WIU": (65, 240, 125),
    "BASE": (55, 220, 255),
    "CNTL": (255, 165, 45),
    "TIME": (195, 115, 255),
    "QSTAT": (250, 225, 70),
    "ACK": (160, 195, 255),
    "EMP": (0, 0, 0),
    "ATCS": (70, 185, 255),
    "EOT": (255, 85, 155),
    "HOT": (255, 200, 65),
    "DPU": (255, 145, 45),
    "RLC": (205, 115, 255),
}

# Multi-band profiles allowing Chroma to inspect 220 MHz PTC (Grabber),
# 900 MHz ATCS (ApexBOSLatch), or 450 MHz EOT/HOT/DPU/RLC (ApexBOSFred)
BAND_PROFILES: Dict[str, Dict[str, Any]] = {
    "ptc": {
        "label": "PTC 220",
        "iq_port": DEFAULT_CHROMA_IQ_PORT,
        "ctrl_port": DEFAULT_CHROMA_CTRL_PORT,
        "worker_ports": [18071],
        "center_hz": WIDEBAND_CENTER_HZ,
        "rate_hz": WIDEBAND_SAMPLE_RATE,
        "channels": None,  # Uses standard PTC Ch 97-176 grid
    },
    "atcs": {
        "label": "ATCS 935",
        "iq_port": 18299,
        "ctrl_port": 18298,
        "worker_ports": [18201],
        "center_hz": 936437500,
        "rate_hz": 1920000,
        "channels": {
            1: (935887500, "F-1"), 2: (935937500, "F-2"), 3: (935987500, "F-3"),
            4: (936887500, "F-4"), 5: (936937500, "F-5"), 6: (936987500, "F-6"),
            11: (896887500, "F-1M"), 12: (896937500, "F-2M"), 13: (896987500, "F-3M"),
            14: (897887500, "F-4M"), 15: (897937500, "F-5M"), 16: (897987500, "F-6M"),
        },
    },
    "atcs_mcp": {
        "label": "ATCS 896",
        "iq_port": 18299,
        "ctrl_port": 18298,
        "worker_ports": [18201],
        "center_hz": 897437500,
        "rate_hz": 1920000,
        "channels": {
            11: (896887500, "MCP1"), 12: (896937500, "MCP2"), 13: (896987500, "MCP3"),
            14: (897887500, "MCP4"), 15: (897937500, "MCP5"), 16: (897987500, "MCP6"),
        },
    },
    "fred": {
        "label": "FRED 457",
        "iq_port": 18399,
        "ctrl_port": 18398,
        "worker_ports": [18301],
        "center_hz": 457943750,
        "rate_hz": 960000,
        "channels": {
            1: (457937500, "EOT"),
            2: (452937500, "HOT"),
            3: (452925000, "DPU1"),
            4: (452950000, "DPU2"),
            5: (457925000, "DPU3"),
            6: (457950000, "DPU4"),
            7: (452931250, "RLC1"),
            8: (452943750, "RLC2"),
            9: (452956250, "RLC3"),
            10: (452962500, "RLC4"),
            11: (452968750, "RLC5"),
        },
    },
    "fred452": {
        "label": "FRED 452",
        "iq_port": 18399,
        "ctrl_port": 18398,
        "worker_ports": [18301],
        "center_hz": 452946875,
        "rate_hz": 960000,
        "channels": {
            2: (452937500, "HOT"),
            3: (452925000, "DPU1"),
            4: (452950000, "DPU2"),
            7: (452931250, "RLC1"),
            8: (452943750, "RLC2"),
            9: (452956250, "RLC3"),
            10: (452962500, "RLC4"),
            11: (452968750, "RLC5"),
        },
    },
}


def classify_blip_category(type_name: str, pkt_type: int) -> str:
    tn = (type_name or "").upper()
    if "EOT" in tn:
        return "EOT"
    if "HOT" in tn:
        return "HOT"
    if "DPU" in tn:
        return "DPU"
    if "RLC" in tn:
        return "RLC"
    if "ATCS" in tn:
        return "ATCS"
    # EMP (Class D) packets: all 0x50-0x5F and 0x70-0x7F types (0x50, 0x51, 0x70, 0x71, etc.)
    if (0x50 <= pkt_type <= 0x5F) or (0x70 <= pkt_type <= 0x7F) or "EMP" in tn or "CLASS D" in tn or tn == "UNICAST":
        return "EMP"
    if pkt_type in (0x33, 0x34) or "WIU" in tn or "WAYSIDE" in tn or "STATUS" in tn:
        return "WIU"
    if pkt_type == 0x02 or "BASE" in tn or "BEACON" in tn:
        return "BASE"
    if pkt_type in (0x01, 0x04, 0x05) or "CNTL" in tn or "CTRL" in tn or "LOCO" in tn:
        return "CNTL"
    if pkt_type == 0x06 or "TIME" in tn or "SYNC" in tn:
        return "TIME"
    if "QSTAT" in tn:
        return "QSTAT"
    if "ACK" in tn:
        return "ACK"
    return "OTHER"


class PacketRuleEnricher:
    """
    Optionally loads parse_packets.py rules and rrdata.json inside Chroma's own
    process so blips can display WIU IDs, src->dst addresses, and Railroad names
    without adding any load to Grabber.
    """

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.rrdata: Dict[str, str] = {}
        self.rules: List[dict] = []
        self._parse_fn = None
        self.atcs_decoder = None
        self._load_assets()
        try:
            import ApexBOSLatch as _latch_mod
            self.atcs_decoder = _latch_mod.AtcsRulesDecoder(config_dir=os.path.join(self.base_dir, "config"))
        except Exception:
            self.atcs_decoder = None

    def _load_assets(self):
        # 1. Load rrdata.json for Railroad lookup
        for cand in (
            os.path.join(self.base_dir, "config", "rrdata.json"),
            os.path.join(self.base_dir, "rrdata.json"),
        ):
            if os.path.isfile(cand):
                try:
                    with open(cand, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    if isinstance(d, dict):
                        self.rrdata = {str(k): str(v) for k, v in d.items()}
                    break
                except Exception:
                    pass

        # 2. Optionally load parse_packets engine + rule files
        try:
            import parse_packets
            if hasattr(parse_packets, "parse_packet"):
                self._parse_fn = parse_packets.parse_packet
            rule_candidates = [
                os.path.join(self.base_dir, "ptc_status_parser.json"),
            ] + sorted(glob.glob(os.path.join(self.base_dir, "config", "*rule*.json")))
            for rp in rule_candidates:
                if os.path.basename(rp).lower() == "atcs_rules.json":
                    continue
                if os.path.isfile(rp):
                    try:
                        with open(rp, "r", encoding="utf-8") as f:
                            r = json.load(f)
                        if isinstance(r, list):
                            self.rules.extend(r)
                        elif isinstance(r, dict):
                            self.rules.append(r)
                    except Exception:
                        pass
        except Exception:
            pass

    def lookup_railroad(self, addr_val: Any) -> str:
        if not addr_val or not self.rrdata:
            return ""
        s = str(addr_val).strip()
        for digits in (s, s.lstrip("0")):
            for klen in (3, 4, 2):
                if len(digits) >= klen and digits[:klen] in self.rrdata:
                    return self.rrdata[digits[:klen]]
        return ""

    def enrich(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        """Extracts WIU/Node ID, src, dst, railroad, and concise tag for waterfall blips."""
        if meta.get("protocol") == "atcs" and getattr(self, "atcs_decoder", None) is not None:
            try:
                meta = self.atcs_decoder.decode(meta)
            except Exception:
                pass
        tel = meta.get("telemetry") if isinstance(meta.get("telemetry"), dict) else {}
        wiu = tel.get("wiu_id") or tel.get("radio_id") or tel.get("tid") or ""
        src = tel.get("src") or tel.get("src_id") or ""
        dst = tel.get("dst") or tel.get("dst_id") or ""
        parsed_extra: Dict[str, Any] = {}

        raw_hex = str(meta.get("hex") or "").strip()
        if raw_hex and self._parse_fn and self.rules:
            try:
                res = self._parse_fn(raw_hex, self.rules)
                if isinstance(res, dict):
                    parsed_extra = res
                    if not wiu:
                        wiu = str(res.get("_node_id_") or res.get("wiu_id") or res.get("addr") or "")
                    if not src:
                        for k in ("_src_Id_", "_src_id_", "src_id", "src", "source"):
                            if res.get(k) not in (None, ""):
                                src = str(res[k])
                                break
                    if not dst:
                        for k in ("_dst_Id_", "_dst_id_", "dst_id", "dst", "dest", "destination"):
                            if res.get(k) not in (None, ""):
                                dst = str(res[k])
                                break
            except Exception:
                pass

        pkt_type = int(meta.get("type", 0) or 0)
        type_name = str(meta.get("type_name") or f"0x{pkt_type:02X}")
        cat = classify_blip_category(type_name, pkt_type)
        rr = self.lookup_railroad(wiu or src)

        if cat == "EOT" and meta.get("unit_id") is not None:
            psi = meta.get("pressure_psi", "?")
            short_lbl = f"EOT #{int(meta['unit_id']):05d} {psi}PSI"
        elif cat == "HOT" and meta.get("unit_id") is not None:
            short_lbl = f"HOT #{int(meta['unit_id']):05d}"
        elif cat == "DPU":
            lid = meta.get("loco_id", meta.get("unit_id", ""))
            thr = meta.get("throttle", "")
            short_lbl = f"DPU #{lid} {thr}".strip()
        elif cat == "RLC":
            ocu = int(meta.get("unit_id", 0) or 0)
            short_lbl = f"RLC 0x{ocu:04X}"
        elif cat == "ATCS" and (meta.get("mcp_addr") or meta.get("ctc_id")):
            st_n = str(meta.get("atcs_station") or "").strip()
            toks = meta.get("ctc_tokens") or []
            tok_s = f" {','.join(toks[:3])}" if toks else ""
            if st_n:
                short_lbl = f"ATCS {st_n[:10]}{tok_s}"
            else:
                short_lbl = f"ATCS {str(meta.get('ctc_id') or meta.get('mcp_addr'))[-6:]}{tok_s}"
        elif cat == "EMP":
            if src and dst:
                short_lbl = f"EMP 0x{pkt_type:02X} {src}->{dst}"
            elif wiu:
                short_lbl = f"EMP 0x{pkt_type:02X} {wiu}"
            else:
                short_lbl = f"EMP 0x{pkt_type:02X}"
        elif src and dst:
            short_lbl = f"{src}->{dst}"
        elif wiu:
            prefix = "BASE" if cat == "BASE" else ("TID" if tel.get("tid") else "WIU")
            short_lbl = f"{prefix} {wiu}"
        else:
            short_lbl = cat if cat != "OTHER" else type_name[:8]

        if rr:
            short_lbl = f"{rr}:{short_lbl}"

        return {
            "category": cat,
            "color": PKT_TYPE_COLORS.get(cat, (235, 235, 120)),
            "wiu": str(wiu),
            "src": str(src),
            "dst": str(dst),
            "railroad": rr,
            "short_label": short_lbl,
            "parsed": parsed_extra,
        }


class PacketBlipSubscriber:
    """
    Background ZMQ SUB thread subscribing to Grabber's existing worker PUB ports
    (zero impact on Grabber) and recording timestamped packet blips for Chroma.
    """

    def __init__(self, enricher: PacketRuleEnricher, max_blips: int = 800):
        self.enricher = enricher
        self.lock = threading.Lock()
        self.blips: collections.deque = collections.deque(maxlen=max_blips)
        self.channel_stats: Dict[int, Dict[str, Any]] = {}
        self.last_aux_summary: Dict[str, Tuple[float, str]] = {}
        self._ports: Tuple[int, ...] = ()
        self._stop_ev = threading.Event()
        self._ports_changed = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, ports: List[int]):
        self.update_ports(ports)
        if self._thread is None or not self._thread.is_alive():
            self._stop_ev.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def update_ports(self, ports: List[int]):
        clean = tuple(sorted({int(p) for p in ports if p and int(p) > 0}))
        if clean != self._ports:
            self._ports = clean
            self._ports_changed.set()

    def stop(self):
        self._stop_ev.set()
        self._ports_changed.set()

    def add_blip(self, meta: Dict[str, Any], now_mono: Optional[float] = None):
        t_m = time.monotonic() if now_mono is None else float(now_mono)
        ch = int(meta.get("channel", 0) or 0)
        freq_hz = float(meta.get("freq_hz") or (itc_channel_frequency(ch) if ch > 0 else 0.0))
        df_hz = float(meta.get("freq_offset_hz", 0.0) or 0.0)
        rssi = float(meta.get("rssi_dbfs", meta.get("rssi", -100.0)) or -100.0)
        snr = float(meta.get("snr_db", max(0.0, rssi + 45.0)) or 0.0)
        en = self.enricher.enrich(meta)

        blip = {
            "ts": t_m,
            "wall_time": str(meta.get("timestamp") or time.strftime("%H:%M:%S")),
            "channel": ch,
            "freq_hz": freq_hz,
            "freq_offset_hz": df_hz,
            "exact_freq_hz": freq_hz + df_hz,
            "rssi": rssi,
            "snr": snr,
            "rate": str(meta.get("rate") or "FR"),
            "type": int(meta.get("type", 0) or 0),
            "type_name": str(meta.get("type_name") or ""),
            "sync_errors": int(meta.get("sync_errors", 0) or 0),
            "length": int(meta.get("length", 0) or 0),
            "hex": str(meta.get("hex") or ""),
            "category": en["category"],
            "color": en["color"],
            "wiu": en["wiu"],
            "src": en["src"],
            "dst": en["dst"],
            "railroad": en["railroad"],
            "short_label": en["short_label"],
            "telemetry": meta.get("telemetry") or {},
            "filtered": bool(meta.get("filtered", False)),
        }
        with self.lock:
            self.blips.append(blip)
            if en["category"] == "ATCS":
                scroll_s = str(meta.get("_scroll_ptc_") or en["short_label"])
                self.last_aux_summary["atcs"] = (t_m, scroll_s)
            elif en["category"] in ("EOT", "HOT", "DPU", "RLC"):
                scroll_s = str(meta.get("_scroll_ptc_") or en["short_label"])
                self.last_aux_summary["fred"] = (t_m, scroll_s)
            if ch > 0:
                st = self.channel_stats.setdefault(ch, {"times": collections.deque(maxlen=300), "last_rssi": rssi, "last_df": df_hz, "last_wiu": ""})
                st["times"].append(t_m)
                st["last_rssi"] = rssi
                st["last_df"] = df_hz
                if en["wiu"]:
                    st["last_wiu"] = en["wiu"]

    def get_blips_snapshot(self) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.blips)

    def get_channel_ppm_counts(self, now_mono: float, window_sec: float = 60.0) -> Dict[int, int]:
        out: Dict[int, int] = {}
        cutoff = now_mono - window_sec
        with self.lock:
            for ch, st in self.channel_stats.items():
                dq = st["times"]
                while dq and dq[0] < cutoff:
                    dq.popleft()
                if dq:
                    out[ch] = len(dq)
        return out

    def _run(self):
        if zmq is None:
            return
        ctx = zmq.Context()
        sub = None
        try:
            while not self._stop_ev.is_set():
                if self._ports_changed.is_set() or sub is None:
                    self._ports_changed.clear()
                    if sub is not None:
                        try:
                            sub.close(linger=0)
                        except Exception:
                            pass
                        sub = None
                    ports = self._ports
                    if ports:
                        sub = ctx.socket(zmq.SUB)
                        sub.setsockopt(zmq.RCVHWM, 200)
                        sub.setsockopt(zmq.LINGER, 0)
                        sub.setsockopt(zmq.SUBSCRIBE, b"")
                        for p in ports:
                            try:
                                sub.connect(f"tcp://127.0.0.1:{p}")
                            except Exception:
                                pass
                    else:
                        time.sleep(0.3)
                        continue

                if sub is None:
                    time.sleep(0.2)
                    continue

                try:
                    if sub.poll(timeout=150):
                        frames = sub.recv_multipart(flags=zmq.NOBLOCK)
                        # Worker publishes [mosfet_frame, hex_bytes, meta_json, (optional rssi)]
                        meta_obj = None
                        for fr in frames:
                            if fr and fr[:1] == b"{":
                                try:
                                    meta_obj = json.loads(fr.decode("utf-8"))
                                    break
                                except Exception:
                                    pass
                        if isinstance(meta_obj, dict) and "channel" in meta_obj:
                            self.add_blip(meta_obj)
                except Exception:
                    time.sleep(0.05)
        finally:
            if sub is not None:
                try:
                    sub.close(linger=0)
                except Exception:
                    pass
            try:
                ctx.term()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Grabber ZMQ Client + Standalone SDR & Mock IQ Sources
# ---------------------------------------------------------------------------
class GrabberIQClient:
    """
    Connects to Grabber's gated XPUB IQ tap (default tcp://127.0.0.1:18099)
    and REQ/REP control bridge (default tcp://127.0.0.1:18098).
    """

    def __init__(
        self,
        iq_port: int = DEFAULT_CHROMA_IQ_PORT,
        ctrl_port: int = DEFAULT_CHROMA_CTRL_PORT,
        host: str = "127.0.0.1",
        on_packet_meta: Optional[Any] = None,
    ):
        self.host = host
        self.iq_port = int(iq_port)
        self.ctrl_port = int(ctrl_port)
        self._on_packet_meta = on_packet_meta
        self.connected = False
        self.last_rx_ts = 0.0
        self.latest_meta: Dict[str, Any] = {}
        self._iq_queue: queue.Queue = queue.Queue(maxsize=2)
        self._stop_ev = threading.Event()
        self._sub_thread: Optional[threading.Thread] = None
        self._ctrl_lock = threading.Lock()

    def start(self):
        if zmq is None:
            return
        self._stop_ev.clear()
        self._sub_thread = threading.Thread(target=self._sub_loop, daemon=True)
        self._sub_thread.start()

    def stop(self):
        self._stop_ev.set()

    def send_control(self, payload: Dict[str, Any], timeout_ms: int = 450) -> Optional[Dict[str, Any]]:
        if zmq is None or self.ctrl_port <= 0:
            return None
        with self._ctrl_lock:
            ctx = zmq.Context.instance()
            sock = ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.LINGER, 0)
            sock.setsockopt(zmq.RCVTIMEO, int(timeout_ms))
            sock.setsockopt(zmq.SNDTIMEO, int(timeout_ms))
            try:
                sock.connect(f"tcp://{self.host}:{self.ctrl_port}")
                sock.send(json.dumps(payload).encode("utf-8"))
                raw = sock.recv()
                resp = json.loads(raw.decode("utf-8"))
                if isinstance(resp, dict) and resp.get("ok"):
                    self.connected = True
                    self.latest_meta.update(resp)
                return resp
            except Exception:
                return None
            finally:
                try:
                    sock.close(linger=0)
                except Exception:
                    pass

    def pop_latest_iq(self) -> Optional[Tuple[Dict[str, Any], bytes]]:
        latest = None
        while True:
            try:
                latest = self._iq_queue.get_nowait()
            except queue.Empty:
                break
        return latest

    def _sub_loop(self):
        ctx = zmq.Context()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.RCVHWM, 64)
        sub.setsockopt(zmq.LINGER, 0)
        sub.setsockopt(zmq.SUBSCRIBE, b"IQ")
        if self._on_packet_meta is not None:
            sub.setsockopt(zmq.SUBSCRIBE, b"PKT")
        try:
            sub.connect(f"tcp://{self.host}:{self.iq_port}")
            while not self._stop_ev.is_set():
                try:
                    if sub.poll(timeout=150):
                        while not self._stop_ev.is_set():
                            try:
                                frames = sub.recv_multipart(flags=zmq.NOBLOCK)
                            except Exception:
                                break
                            if not frames:
                                continue
                            topic = frames[0]
                            if topic == b"IQ" and len(frames) >= 3:
                                meta = json.loads(frames[1].decode("utf-8"))
                                iq_bytes = frames[2]
                                self.connected = True
                                self.last_rx_ts = time.monotonic()
                                self.latest_meta.update(meta)
                                try:
                                    self._iq_queue.put_nowait((meta, iq_bytes))
                                except queue.Full:
                                    try:
                                        self._iq_queue.get_nowait()
                                    except queue.Empty:
                                        pass
                                    try:
                                        self._iq_queue.put_nowait((meta, iq_bytes))
                                    except queue.Full:
                                        pass
                            elif topic == b"PKT" and len(frames) >= 2 and self._on_packet_meta is not None:
                                try:
                                    pkt_meta = json.loads(frames[1].decode("utf-8"))
                                    if isinstance(pkt_meta, dict) and "channel" in pkt_meta:
                                        self._on_packet_meta(pkt_meta)
                                except Exception:
                                    pass
                except Exception:
                    time.sleep(0.05)
        finally:
            try:
                sub.close(linger=0)
            except Exception:
                pass
            try:
                ctx.term()
            except Exception:
                pass


class StandaloneSDRThread:
    """Optional direct RTL-SDR / Mock IQ producer when Grabber is not running."""

    def __init__(
        self,
        center_freq: int = WIDEBAND_CENTER_HZ,
        sample_rate: int = WIDEBAND_SAMPLE_RATE,
        gain: Any = 20.7,
        ppm: int = 0,
        device_idx: int = 0,
        bias_tee: bool = True,
        use_mock: bool = False,
        blip_sub: Optional[PacketBlipSubscriber] = None,
        sdr_cfg: Optional[Dict[str, Any]] = None,
    ):
        self.center_freq = int(center_freq)
        self.sample_rate = int(sample_rate)
        self.gain = gain
        self.ppm = int(ppm)
        self.requested_device_idx = int(device_idx)
        self.device_idx = int(device_idx)
        self.bias_tee = bool(bias_tee)
        self.use_mock = bool(use_mock)
        self.blip_sub = blip_sub
        self.sdr_cfg = dict(sdr_cfg or {})

        self._iq_queue: queue.Queue = queue.Queue(maxsize=2)
        self._cmd_queue: queue.Queue = queue.Queue()
        self._stop_ev = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stream: Any = None
        self._stream_kind: str = ""
        self.active = False
        self.error_msg: str = ""

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        self._stop_ev.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True, timeout: float = 2.5):
        """
        Signals the SDR reader thread to stop and (when wait=True) waits for it
        to close its USB handle / subprocess so a subsequent open never hits
        LIBUSB_ERROR_ACCESS (-3) on Windows or LIBUSB_ERROR_BUSY (-6) on Linux.
        """
        self._stop_ev.set()
        thr = self._thread
        if wait and thr is not None and thr.is_alive() and thr is not threading.current_thread():
            thr.join(timeout=min(0.6, float(timeout)))
            if thr.is_alive():
                # Unblock a potentially stalled subprocess pipe or TCP socket
                st = self._stream
                if st is not None:
                    proc = getattr(st, "process", None)
                    if proc is not None:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=0.35)
                        except Exception:
                            try:
                                proc.kill()
                                proc.wait(timeout=0.35)
                            except Exception:
                                pass
                    sock = getattr(st, "sock", None)
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass
                thr.join(timeout=max(0.5, float(timeout) - 0.6))
        while True:
            try:
                self._iq_queue.get_nowait()
            except queue.Empty:
                break

    def update_hw(self, **kwargs):
        if "ppm" in kwargs and kwargs["ppm"] is not None:
            self.ppm = int(kwargs["ppm"])
        if "gain" in kwargs and kwargs["gain"] is not None:
            self.gain = kwargs["gain"]
        if "bias_tee" in kwargs and kwargs["bias_tee"] is not None:
            self.bias_tee = bool(kwargs["bias_tee"])
        if "freq" in kwargs and kwargs["freq"] is not None:
            self.center_freq = int(kwargs["freq"])
        if "rate" in kwargs and kwargs["rate"] is not None:
            self.sample_rate = int(kwargs["rate"])
        self._cmd_queue.put(kwargs)

    def _drain_iq_queue(self):
        while True:
            try:
                self._iq_queue.get_nowait()
            except queue.Empty:
                break

    def _open_rtlsdr_with_retry(self, grabber_mod, dev_idx: int):
        """Opens DirectRtlSdrSource with brief retries for WinUSB/libusb release settle."""
        last_err = None
        for attempt in range(3):
            if self._stop_ev.is_set():
                return None
            try:
                return grabber_mod.DirectRtlSdrSource(
                    self.center_freq, self.sample_rate, self.gain, self.ppm, dev_idx, bias_tee=self.bias_tee
                )
            except Exception as e:
                last_err = e
                msg = str(e).upper()
                transient = ("ACCESS" in msg) or ("BUSY" in msg) or ("-3" in msg) or ("-6" in msg)
                if attempt < 2 and transient:
                    time.sleep(0.18)
                    continue
                break
        if last_err is not None:
            raise last_err
        return None

    def _open_pipe_with_probe(self, grabber_mod, bin_p: str, read_bytes: int):
        """
        Opens SubprocessPipeSource and reads an initial probe chunk to verify rtl_sdr
        actually claimed the dongle and streamed IQ. Automatically retries transient USB
        busy, falls back to bias_tee=False for older rtl_sdr builds, and falls back to
        device index 0 if the requested device index is absent.
        """
        attempts = [(self.device_idx, self.bias_tee, 0.0), (self.device_idx, self.bias_tee, 0.25)]
        if self.bias_tee:
            attempts.append((self.device_idx, False, 0.1))
        if self.device_idx != 0:
            attempts.append((0, self.bias_tee, 0.1))
            if self.bias_tee:
                attempts.append((0, False, 0.1))

        last_err = f"rtl_sdr exited immediately without IQ data (dev #{self.device_idx})"
        for dev_try, bt_try, delay_s in attempts:
            if self._stop_ev.is_set():
                return None, b""
            if delay_s > 0:
                time.sleep(delay_s)
            if self._stop_ev.is_set():
                return None, b""
            st = None
            try:
                st = grabber_mod.SubprocessPipeSource(
                    bin_p, self.center_freq, self.sample_rate, self.gain, self.ppm, dev_try, bias_tee=bt_try
                )
                self._stream = st
                probe = st.read_exact_bytes(read_bytes)
                if probe and len(probe) >= 128:
                    self.device_idx = int(dev_try)
                    self.bias_tee = bool(bt_try)
                    return st, probe
                rc = st.process.poll() if st.process is not None else None
                last_err = f"rtl_sdr exited (rc={rc}) on dev #{dev_try} @ {self.center_freq/1e6:.3f} MHz"
            except Exception as e:
                last_err = str(e)
            if st is not None:
                try:
                    st.close()
                except Exception:
                    pass
                self._stream = None
        raise RuntimeError(last_err)

    def pop_latest_iq(self) -> Optional[Tuple[Dict[str, Any], bytes]]:
        latest = None
        while True:
            try:
                latest = self._iq_queue.get_nowait()
            except queue.Empty:
                break
        return latest

    def _run(self):
        if self.use_mock:
            self._run_mock()
            return
        stream = None
        try:
            import ApexBOSGrabber as grabber
            cfg = self.sdr_cfg
            class _Args:
                stdin = False
                rtl_tcp = False
                rsp_tcp = False
                soapy = False
                rtlsdr = False
                pipe = False
                sdr_mode = str(cfg.get("sdr_mode") or "auto")
                rtl_sdr_bin = cfg.get("rtl_sdr_bin")
                rsp_tcp_bin = cfg.get("rsp_tcp_bin")
                rtlsdr_dll_dir = cfg.get("rtlsdr_dll_dir")
                host = str(cfg.get("rtl_tcp_host") or "127.0.0.1")
                port = int(cfg.get("rtl_tcp_port") or 1234)
            kind = grabber.resolve_sdr_stream_kind(_Args())
            self._stream_kind = kind
            sample_fmt = "u8"
            bps = 2
            prefetched_raw = b""
            pipe_bin_path = ""

            if kind == "rtlsdr":
                try:
                    stream = self._open_rtlsdr_with_retry(grabber, self.device_idx)
                except Exception:
                    if self.device_idx != 0 and not self._stop_ev.is_set():
                        self.device_idx = 0
                        stream = self._open_rtlsdr_with_retry(grabber, 0)
                    else:
                        raise
            elif kind == "tcp":
                stream = grabber.DirectRtlTcpSource(
                    _Args.host, _Args.port, self.center_freq, self.sample_rate, self.gain, self.ppm, bias_tee=self.bias_tee
                )
            elif kind == "rsptcp":
                stream = grabber.DirectRspTcpSource(
                    _Args.host,
                    _Args.port,
                    self.center_freq,
                    self.sample_rate,
                    self.gain,
                    self.ppm,
                    bias_tee=self.bias_tee,
                    extended_16bit=bool(cfg.get("rsp_extended", False)),
                    lna_state=int(cfg.get("rsp_lna_state", 0) or 0),
                    antenna=cfg.get("rsp_antenna"),
                    notch=cfg.get("rsp_notch"),
                )
                sample_fmt = stream.sample_format
                bps = stream.bytes_per_sample
                self.sample_rate = int(stream.sample_rate)
            elif kind == "soapy":
                try:
                    stream = grabber.DirectSoapySdrSource(
                        self.center_freq,
                        self.sample_rate,
                        self.gain,
                        self.ppm,
                        self.device_idx,
                        bias_tee=self.bias_tee,
                        soapy_args=cfg.get("soapy_args"),
                        lna_state=cfg.get("rsp_lna_state"),
                        antenna=cfg.get("rsp_antenna"),
                    )
                except Exception:
                    if self.device_idx != 0 and not self._stop_ev.is_set():
                        self.device_idx = 0
                        stream = grabber.DirectSoapySdrSource(
                            self.center_freq,
                            self.sample_rate,
                            self.gain,
                            self.ppm,
                            0,
                            bias_tee=self.bias_tee,
                            soapy_args=cfg.get("soapy_args"),
                            lna_state=cfg.get("rsp_lna_state"),
                            antenna=cfg.get("rsp_antenna"),
                        )
                    else:
                        raise
                sample_fmt = stream.sample_format
                bps = stream.bytes_per_sample
                self.sample_rate = int(stream.sample_rate)
            elif kind == "pipe":
                pipe_bin_path = grabber.detect_rtl_sdr_executable(_Args.rtl_sdr_bin)
                probe_samps = 16384 if int(self.sample_rate) <= 300000 else 32768
                stream, prefetched_raw = self._open_pipe_with_probe(grabber, pipe_bin_path, probe_samps * bps)
            else:
                raise RuntimeError(f"Unsupported standalone SDR mode '{kind}'")

            if stream is None or self._stop_ev.is_set():
                return

            self._stream = stream
            self.active = True
            applied_freq = int(self.center_freq)
            applied_rate = int(getattr(stream, "sample_rate", self.sample_rate) or self.sample_rate)
            self.sample_rate = applied_rate
            read_bytes = (16384 if applied_rate <= 300000 else 32768) * bps
            pipe_empty_retries = 0

            while not self._stop_ev.is_set():
                # Coalesce any pending hardware parameter updates (including live band retunes)
                merged_kw: Dict[str, Any] = {}
                while True:
                    try:
                        kw = self._cmd_queue.get_nowait()
                        for k, v in kw.items():
                            if v is not None:
                                merged_kw[k] = v
                    except queue.Empty:
                        break

                if merged_kw:
                    retuned = ("freq" in merged_kw) or ("rate" in merged_kw)
                    stream.update_hardware_params(
                        freq=merged_kw.get("freq"),
                        rate=merged_kw.get("rate"),
                        gain=merged_kw.get("gain"),
                        ppm=merged_kw.get("ppm"),
                        bias_tee=merged_kw.get("bias_tee"),
                    )
                    if "freq" in merged_kw:
                        applied_freq = int(merged_kw["freq"])
                    if "rate" in merged_kw:
                        applied_rate = int(getattr(stream, "sample_rate", merged_kw["rate"]) or merged_kw["rate"])
                        self.sample_rate = applied_rate
                        read_bytes = (16384 if applied_rate <= 300000 else 32768) * bps
                    if retuned:
                        prefetched_raw = b""
                        self._drain_iq_queue()
                        # Purge 1 stale buffer from the USB FIFO / subprocess pipe after retuning
                        if kind != "pipe":
                            try:
                                stream.read_exact_bytes(read_bytes)
                            except Exception:
                                pass

                if prefetched_raw:
                    raw = prefetched_raw
                    prefetched_raw = b""
                else:
                    raw = stream.read_exact_bytes(read_bytes)

                if not raw:
                    if self._stop_ev.is_set():
                        break
                    if kind == "pipe" and getattr(stream, "process", None) is not None and stream.process.poll() is not None:
                        pipe_empty_retries += 1
                        if pipe_empty_retries <= 2:
                            try:
                                stream.close()
                            except Exception:
                                pass
                            self._stream = None
                            stream, prefetched_raw = self._open_pipe_with_probe(grabber, pipe_bin_path, read_bytes)
                            if stream is None:
                                break
                            self._stream = stream
                            continue
                        raise RuntimeError(f"rtl_sdr subprocess stopped unexpectedly on dev #{self.device_idx}")
                    time.sleep(0.02)
                    continue

                pipe_empty_retries = 0
                meta = {
                    "center_freq": applied_freq,
                    "sample_rate": applied_rate,
                    "sample_format": sample_fmt,
                    "ppm": self.ppm,
                    "gain_db": float(self.gain) if str(self.gain).replace(".", "", 1).isdigit() else 0.0,
                    "bias_tee": self.bias_tee,
                }
                try:
                    self._iq_queue.put_nowait((meta, raw))
                except queue.Full:
                    try:
                        self._iq_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._iq_queue.put_nowait((meta, raw))
                    except queue.Full:
                        pass
                time.sleep(0.025)
        except Exception as e:
            if not self._stop_ev.is_set():
                self.error_msg = str(e)
            self.active = False
        finally:
            self.active = False
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            self._stream = None

    def _run_mock(self):
        """Generates realistic synthetic 220 MHz PTC bursts & packet blips for offline testing/demo."""
        self.active = True
        rng = np.random.default_rng(220)
        n_samps = 16384
        t = np.arange(n_samps, dtype=np.float32) / float(self.sample_rate)
        mock_sites = [
            (101, 0.35, 140.0, 0x02, "BASE_BEACON", {"radio_id": "10A4C2", "base_ch": 101}, "FR"),
            (102, 0.55, -210.0, 0x33, "WIU_STATUS", {"wiu_id": "04F219"}, "FR"),
            (114, 0.42, 85.0, 0x05, "LOCO_CNTL", {"tid": "882101", "src": "BASE_101", "dst": "LOCO_8821"}, "HR"),
            (144, 0.48, -60.0, 0x34, "WIU_STATUS_EXT", {"wiu_id": "09C310"}, "FR"),
            (162, 0.30, 310.0, 0x06, "TIME_SYNC", {"tid": "000101"}, "HR"),
        ]
        frame_idx = 0
        last_rate = int(self.sample_rate)
        while not self._stop_ev.is_set():
            # Drain any live control updates (including in-place band retunes)
            while not self._cmd_queue.empty():
                try:
                    self._cmd_queue.get_nowait()
                except queue.Empty:
                    break
            if int(self.sample_rate) != last_rate:
                last_rate = int(self.sample_rate)
                t = np.arange(n_samps, dtype=np.float32) / float(max(1, last_rate))
            gain_f = float(self.gain) if str(self.gain).replace(".", "", 1).isdigit() else 20.7
            gain_scale = 0.4 + max(0.0, min(50.0, gain_f)) / 35.0
            noise = (rng.standard_normal(n_samps) + 1j * rng.standard_normal(n_samps)).astype(np.complex64) * (2.2 * gain_scale)
            sig = noise

            # PPM shifts apparent signal frequencies slightly
            ppm_shift_hz = self.ppm * 221.0
            for idx, (ch, amp, df, ptype, tname, tel, rate_lbl) in enumerate(mock_sites):
                # Intermittent PTC bursts (base channel fires more often)
                duty = 0.45 if ptype == 0x02 else 0.14
                if rng.random() < duty:
                    f_off = float(itc_channel_frequency(ch) - self.center_freq) + df + ppm_shift_hz
                    if abs(f_off) < self.sample_rate * 0.48:
                        # Band-limited ~16 kHz modulated burst envelope
                        bw_mod = np.exp(1j * np.cumsum(rng.choice([-0.025, 0.025], size=n_samps).astype(np.float32)))
                        carrier = np.exp(2j * np.pi * f_off * t).astype(np.complex64)
                        sig += carrier * bw_mod * float(amp * 36.0 * gain_scale)
                        if self.blip_sub is not None and (frame_idx % 2 == 0):
                            rssi_est = -32.0 + 18.0 * math.log10(max(0.1, amp * gain_scale))
                            self.blip_sub.add_blip({
                                "channel": ch,
                                "freq_hz": itc_channel_frequency(ch),
                                "freq_offset_hz": round(df + ppm_shift_hz, 1),
                                "rssi_dbfs": round(rssi_est, 1),
                                "snr_db": round(max(5.0, rssi_est + 45.0), 1),
                                "rate": rate_lbl,
                                "type": ptype,
                                "type_name": tname,
                                "sync_errors": int(rng.integers(0, 3)),
                                "length": 38,
                                "hex": f"490026{ptype:02X}00" + "A5" * 16,
                                "timestamp": time.strftime("%H:%M:%S"),
                                "telemetry": dict(tel),
                            })

            i_u8 = np.clip(np.round(sig.real + 127.5), 0, 255).astype(np.uint8)
            q_u8 = np.clip(np.round(sig.imag + 127.5), 0, 255).astype(np.uint8)
            inter = np.empty(n_samps * 2, dtype=np.uint8)
            inter[0::2] = i_u8
            inter[1::2] = q_u8
            meta = {
                "center_freq": self.center_freq,
                "sample_rate": self.sample_rate,
                "ppm": self.ppm,
                "gain_db": gain_f,
                "bias_tee": self.bias_tee,
                "channels": [101, 102, 114, 144],
                "locked_channels": [101, 144],
                "wideband": True,
                "auto_ppm": False,
            }
            try:
                self._iq_queue.put_nowait((meta, inter.tobytes()))
            except queue.Full:
                try:
                    self._iq_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._iq_queue.put_nowait((meta, inter.tobytes()))
                except queue.Full:
                    pass
            frame_idx += 1
            time.sleep(0.04)


# ---------------------------------------------------------------------------
# Pygame GUI Application: ApexBOSChromaApp
# ---------------------------------------------------------------------------
COLOR_BG = (10, 14, 20)
COLOR_PANEL = (16, 22, 32)
COLOR_BORDER = (52, 68, 92)
COLOR_GRID = (30, 42, 58)
COLOR_TEXT = (228, 236, 248)
COLOR_TEXT_DIM = (135, 148, 168)
COLOR_ACCENT = (255, 185, 45)
COLOR_CYAN = (45, 220, 255)
COLOR_GREEN = (55, 230, 120)
COLOR_RED = (245, 65, 65)
COLOR_BTN = (28, 38, 54)
COLOR_BTN_ON = (36, 98, 142)
COLOR_BTN_WARN = (185, 48, 48)


class ApexBOSChromaApp:
    """
    Resizable, zoomable, pannable Pygame PTC Spectrum & Waterfall Analyzer.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        iq_port: int = DEFAULT_CHROMA_IQ_PORT,
        ctrl_port: int = DEFAULT_CHROMA_CTRL_PORT,
        standalone: bool = False,
        use_mock: bool = False,
        wideband: bool = False,
        width: int = 1280,
        height: int = 760,
        band: Optional[str] = None,
    ):
        import pygame
        self.pygame = pygame
        pygame.init()
        if not pygame.font.get_init():
            pygame.font.init()

        self._explicit_band: bool = (band is not None)
        self.band: str = str(band or "ptc").strip().lower()
        if self.band not in BAND_PROFILES:
            self.band = "ptc"
        self._band_profile = BAND_PROFILES[self.band]
        if self.band != "ptc":
            if iq_port == DEFAULT_CHROMA_IQ_PORT:
                iq_port = int(self._band_profile["iq_port"])
            if ctrl_port == DEFAULT_CHROMA_CTRL_PORT:
                ctrl_port = int(self._band_profile["ctrl_port"])

        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_path = config_path or os.path.join(self.base_dir, config_loader.CONFIG_FILENAME)
        self.config = config_loader.load_unified_config(self.config_path) if os.path.exists(self.config_path) else {}
        self.aux_ports_cfg = config_loader.get_aux_demod_ports(self.config)
        self.detected_bands: Set[str] = set()
        self._last_aux_probe: float = 0.0
        grab_cfg = self.config.get("grabber", {})

        self.width = max(800, int(width))
        self.height = max(520, int(height))
        self.screen = pygame.display.set_mode((self.width, self.height), pygame.RESIZABLE)
        pygame.display.set_caption(f"ApexBOSChroma — {self._band_profile['label']} Spectrum & Waterfall ({VERSION})")

        self.clock = pygame.time.Clock()
        self.font_xs = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 10)
        self.font_sm = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 11)
        self.font_sm_bold = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 11, bold=True)
        self.font_md = pygame.font.SysFont("Consolas,DejaVu Sans Mono,monospace", 13, bold=True)

        # Core DSP engine
        self.engine = ChromaSpectrumEngine(fft_size=4096, max_rows=520)
        self.engine.center_freq = int(self._band_profile["center_hz"])
        self.engine.sample_rate = int(self._band_profile["rate_hz"])

        # Frequency viewport (Hz): default spans active band profile
        if self.band == "ptc":
            self.full_ptc_min_hz = float(itc_channel_frequency(PTC_CH_MIN) - 50000)
            self.full_ptc_max_hz = float(itc_channel_frequency(PTC_CH_MAX) + 50000)
        else:
            half_bw = float(self._band_profile["rate_hz"]) * 0.5
            self.full_ptc_min_hz = float(self._band_profile["center_hz"]) - half_bw
            self.full_ptc_max_hz = float(self._band_profile["center_hz"]) + half_bw
        self.view_f_min: float = self.full_ptc_min_hz
        self.view_f_max: float = self.full_ptc_max_hz
        if self.band in ("fred", "fred452"):
            self.fit_to_captured_band()

        # dB display range
        self.min_db: float = -95.0
        self.max_db: float = -10.0

        # Display toggles
        self.palette_idx: int = 0
        self.show_fade_peaks: bool = True
        self.show_max_hold: bool = False
        self.show_blips: bool = True
        self.show_blip_labels: bool = True
        self.paused: bool = False
        self.pause_mono_ts: float = 0.0
        self.wf_row_px: int = 2  # 2 px per waterfall row gives crisp readable bursts

        # Hardware / Grabber state mirrored in UI
        self.mode: str = "mock" if use_mock else ("standalone" if standalone else "grabber")
        self.iq_port = int(iq_port)
        self.ctrl_port = int(ctrl_port)
        self.want_wideband = bool(wideband)
        self.wideband_active = False

        ex_chans_raw = str(grab_cfg.get("chans") or "")
        _, ex_ch_list, ex_locked = config_loader.parse_channels_with_locks(ex_chans_raw) if ex_chans_raw else ([], list(grab_cfg.get("channels") or [101, 102]), set(grab_cfg.get("locked_channels") or []))
        self.monitored_channels: Set[int] = set(int(c) for c in ex_ch_list)
        self.locked_channels: Set[int] = set(int(c) for c in ex_locked)
        self.ppm: int = int(grab_cfg.get("ppm", grab_cfg.get("frequency_correction", 0)) or 0)
        g_raw = grab_cfg.get("gain", 20.7)
        self.gain_db: float = float(g_raw) if str(g_raw).replace(".", "", 1).isdigit() else 0.0
        self.bias_tee: bool = bool(grab_cfg.get("bias_tee", 1))
        self.auto_ppm: bool = bool(grab_cfg.get("auto_ppm", True))
        self.device_idx: int = int(grab_cfg.get("device", 0) or 0)

        if self.band != "ptc":
            self.worker_ports = list(self._band_profile["worker_ports"])
        else:
            ex_ports = grab_cfg.get("ports") or []
            if not ex_ports and grab_cfg.get("procs"):
                ex_ports = [int(p.strip()) for p in str(grab_cfg["procs"]).split(",") if p.strip().isdigit()]
            self.worker_ports = [int(p) for p in ex_ports] if ex_ports else [18071]

        # Packet blip enrichment & ZMQ SUB
        self.enricher = PacketRuleEnricher(self.base_dir)
        self.blip_sub = PacketBlipSubscriber(self.enricher)

        # Sources
        self.grabber_client: Optional[GrabberIQClient] = None
        self.standalone_sdr: Optional[StandaloneSDRThread] = None
        self._last_ctrl_poll = 0.0
        self._last_wf_rate_est = 25.0

        # Interaction state
        self.buttons: Dict[str, Tuple[Any, Any]] = {}
        self.channel_hit_rects: List[Tuple[Any, int]] = []
        self.blip_hit_rects: List[Tuple[Any, Dict[str, Any]]] = []
        self.selected_blip: Optional[Dict[str, Any]] = None
        self._dragging_pan = False
        self._drag_start_x = 0
        self._drag_start_fmin = 0.0
        self._drag_start_fmax = 0.0
        self._dragging_db = False
        self._drag_start_y = 0
        self._drag_start_mindb = 0.0
        self._drag_start_maxdb = 0.0
        self.status_toast = "Initializing ApexBOSChroma..."
        self.toast_ts = time.monotonic()

        self._init_sources()

    def _init_sources(self):
        if self.mode == "mock":
            self._start_standalone_for_active_band(use_mock=True)
            self.wideband_active = (self.band == "ptc")
            self.set_toast(f"Running in Mock {self._band_profile['label']} mode (synthetic bursts & blips).")
        elif self.mode == "standalone":
            if self.gain_db <= 0.0:
                self.gain_db = 20.7
            self._start_standalone_for_active_band(use_mock=False)
            self.wideband_active = (self.band == "ptc")
            self.set_toast(
                f"Started Standalone SDR ({self._band_profile['label']} @ {self._band_profile['center_hz']/1e6:.4f} MHz, "
                f"{self._band_profile['rate_hz']/1e6:.3f} MS/s, dev #{self._standalone_device_for_band()})."
            )
        else:
            # Auto-detect running PTC, ATCS (Latch), and EOT/HOT (Fred) demodulators on their assigned ports
            self._probe_aux_demods(initial=True)
            self.grabber_client = GrabberIQClient(iq_port=self.iq_port, ctrl_port=self.ctrl_port, on_packet_meta=self.blip_sub.add_blip)
            self.grabber_client.start()
            self.blip_sub.start(self._effective_blip_ports())
            state = self.grabber_client.send_control({"cmd": "get_state"}, timeout_ms=350)
            if state and state.get("ok"):
                self._sync_state_from_dict(state)
                if self.want_wideband and not self.wideband_active and self.band == "ptc":
                    self._toggle_wideband(force_val=True)
                self.fit_to_captured_band()
                self.set_toast(f"Attached to {self._band_profile['label']} ({self.engine.sample_rate/1e6:.3f} MS/s @ {self.engine.center_freq/1e6:.4f} MHz).")
            else:
                self.set_toast(f"Waiting for {self._band_profile['label']} IQ tap... (Click 'Standalone' to open SDR directly)")

    def _effective_blip_ports(self) -> List[int]:
        """Returns active band worker ports plus any auto-detected running ATCS/FRED ZMQ PUB ports."""
        ports = list(self.worker_ports)
        for bk in ("atcs", "fred"):
            if bk in self.detected_bands:
                zp = int(self.aux_ports_cfg[bk]["zmq_port"])
                if zp not in ports:
                    ports.append(zp)
        return ports

    def _apply_band_profile(self, band_key: str):
        """Applies band frequency/port profile for 'ptc', 'atcs', or 'fred'."""
        self.band = band_key if band_key in BAND_PROFILES else "ptc"
        self._band_profile = BAND_PROFILES[self.band]
        if self.band == "ptc":
            self.iq_port = int(self.aux_ports_cfg["ptc"]["chroma_port"])
            self.ctrl_port = int(self.aux_ports_cfg["ptc"]["ctrl_port"])
            grab_cfg = self.config.get("grabber", {})
            ex_ports = grab_cfg.get("ports") or []
            if not ex_ports and grab_cfg.get("procs"):
                ex_ports = [int(p.strip()) for p in str(grab_cfg["procs"]).split(",") if p.strip().isdigit()]
            self.worker_ports = [int(p) for p in ex_ports] if ex_ports else [18071]
        else:
            aux_k = "atcs" if self.band in ("atcs", "atcs_mcp") else "fred"
            self.iq_port = int(self.aux_ports_cfg[aux_k]["chroma_port"])
            self.ctrl_port = int(self.aux_ports_cfg[aux_k]["ctrl_port"])
            self.worker_ports = [int(self.aux_ports_cfg[aux_k]["zmq_port"])]
        self.engine.clear_waterfall()
        self.engine.center_freq = int(self._band_profile["center_hz"])
        self.engine.sample_rate = int(self._band_profile["rate_hz"])
        if self.band == "ptc":
            self.full_ptc_min_hz = float(itc_channel_frequency(PTC_CH_MIN) - 50000)
            self.full_ptc_max_hz = float(itc_channel_frequency(PTC_CH_MAX) + 50000)
        else:
            half_bw = float(self._band_profile["rate_hz"]) * 0.5
            self.full_ptc_min_hz = float(self._band_profile["center_hz"]) - half_bw
            self.full_ptc_max_hz = float(self._band_profile["center_hz"]) + half_bw
        self.view_f_min, self.view_f_max = self.full_ptc_min_hz, self.full_ptc_max_hz
        if self.band in ("fred", "fred452"):
            self.fit_to_captured_band()
        self.pygame.display.set_caption(f"ApexBOSChroma — {self._band_profile['label']} Spectrum & Waterfall ({VERSION})")

    def _probe_aux_demods(self, initial: bool = False):
        """
        Detects running PTC Grabber, ApexBOSLatch (ATCS), and ApexBOSFred (EOT/HOT) on their
        assigned ports. Preserves PTC whenever PTC is running, auto-selects ATCS/FRED if PTC
        is absent on startup, and auto-subscribes to live ATCS/FRED packets in all bands.
        """
        if self.mode == "mock":
            return
        detected: Set[str] = set()
        ptc_ctrl = int(self.aux_ports_cfg["ptc"]["ctrl_port"])
        if config_loader.is_tcp_port_open("127.0.0.1", ptc_ctrl, timeout=0.08):
            detected.add("ptc")
        for bk in ("atcs", "fred"):
            z_p = int(self.aux_ports_cfg[bk]["zmq_port"])
            c_p = int(self.aux_ports_cfg[bk]["ctrl_port"])
            if config_loader.is_tcp_port_open("127.0.0.1", z_p, timeout=0.08) or config_loader.is_tcp_port_open("127.0.0.1", c_p, timeout=0.08):
                detected.add(bk)

        prev = set(self.detected_bands)
        self.detected_bands = detected

        if initial and not self._explicit_band and "ptc" not in detected:
            if "atcs" in detected:
                self._apply_band_profile("atcs")
            elif "fred" in detected:
                self._apply_band_profile("fred")

        if not initial and detected != prev:
            self.blip_sub.update_ports(self._effective_blip_ports())
            newly = sorted(detected - prev)
            if newly:
                self.set_toast(f"Auto-detected running demodulator(s): {', '.join(b.upper() for b in newly)}")

    def shutdown(self):
        if self.grabber_client is not None:
            if self.wideband_active:
                try:
                    self.grabber_client.send_control({"cmd": "set_wideband", "enabled": False}, timeout_ms=250)
                except Exception:
                    pass
            self.grabber_client.stop()
        if self.standalone_sdr is not None:
            self.standalone_sdr.stop()
        self.blip_sub.stop()

    def set_toast(self, msg: str):
        self.status_toast = msg
        self.toast_ts = time.monotonic()

    def _sync_state_from_dict(self, d: Dict[str, Any], sync_engine_rf: bool = True):
        if sync_engine_rf:
            if "center_freq" in d and d["center_freq"]:
                self.engine.center_freq = int(d["center_freq"])
            if "sample_rate" in d and d["sample_rate"]:
                self.engine.sample_rate = int(d["sample_rate"])
        if "ppm" in d and d["ppm"] is not None:
            self.ppm = int(d["ppm"])
        if "gain_db" in d and d["gain_db"] is not None:
            self.gain_db = float(d["gain_db"])
        if "bias_tee" in d and d["bias_tee"] is not None and self.mode == "grabber":
            self.bias_tee = bool(d["bias_tee"])
        if "auto_ppm" in d and d["auto_ppm"] is not None:
            self.auto_ppm = bool(d["auto_ppm"])
        if "wideband" in d and d["wideband"] is not None:
            self.wideband_active = bool(d["wideband"])
        if "channels" in d and isinstance(d["channels"], (list, tuple)):
            self.monitored_channels = {int(c) for c in d["channels"]}
        if "locked_channels" in d and isinstance(d["locked_channels"], (list, tuple)):
            self.locked_channels = {int(c) for c in d["locked_channels"]}
        if "worker_ports" in d and isinstance(d["worker_ports"], (list, tuple)) and d["worker_ports"]:
            new_ports = [int(p) for p in d["worker_ports"]]
            if new_ports != self.worker_ports:
                self.worker_ports = new_ports
                self.blip_sub.update_ports(self._effective_blip_ports())

    # ----- Geometry & Coordinate Helpers -----
    def _layout_rects(self):
        pygame = self.pygame
        top_h = 66
        footer_h = 26
        left_axis_w = 56
        right_pad = 14
        ruler_h = 30

        plot_w = max(200, self.width - left_axis_w - right_pad)
        avail_h = max(200, self.height - top_h - footer_h - ruler_h)
        spec_h = max(110, int(avail_h * 0.36))
        wf_h = max(90, avail_h - spec_h)

        spec_rect = pygame.Rect(left_axis_w, top_h, plot_w, spec_h)
        ruler_rect = pygame.Rect(left_axis_w, top_h + spec_h, plot_w, ruler_h)
        wf_rect = pygame.Rect(left_axis_w, top_h + spec_h + ruler_h, plot_w, wf_h)
        axis_rect = pygame.Rect(0, top_h, left_axis_w, spec_h)
        return top_h, footer_h, spec_rect, ruler_rect, wf_rect, axis_rect

    def freq_to_x(self, freq_hz: float, plot_rect) -> float:
        span = max(1.0, self.view_f_max - self.view_f_min)
        return plot_rect.x + ((float(freq_hz) - self.view_f_min) / span) * plot_rect.width

    def x_to_freq(self, x_px: float, plot_rect) -> float:
        frac = (float(x_px) - plot_rect.x) / float(max(1, plot_rect.width))
        return self.view_f_min + frac * (self.view_f_max - self.view_f_min)

    def db_to_y(self, db_val: float, spec_rect) -> int:
        span = max(1.0, self.max_db - self.min_db)
        norm = (float(db_val) - self.min_db) / span
        norm = max(0.0, min(1.0, norm))
        return int(spec_rect.bottom - norm * spec_rect.height)

    def zoom_at_freq(self, anchor_freq: float, factor: float):
        """Zooms horizontal frequency window around anchor_freq (factor < 1 zooms in)."""
        cur_span = self.view_f_max - self.view_f_min
        min_span = 15000.0 if self.band in ("fred", "fred452") else 30000.0
        max_span = max(self.full_ptc_max_hz - self.full_ptc_min_hz, float(self.engine.sample_rate) * 1.15)
        new_span = max(min_span, min(max_span, cur_span * factor))
        frac = (anchor_freq - self.view_f_min) / max(1.0, cur_span)
        new_min = anchor_freq - frac * new_span
        new_max = new_min + new_span
        self._clamp_view(new_min, new_max)

    def _clamp_view(self, f_min: float, f_max: float):
        span = f_max - f_min
        band_lo = min(self.full_ptc_min_hz, self.engine.center_freq - self.engine.sample_rate * 0.55)
        band_hi = max(self.full_ptc_max_hz, self.engine.center_freq + self.engine.sample_rate * 0.55)
        if f_min < band_lo:
            f_min = band_lo
            f_max = f_min + span
        if f_max > band_hi:
            f_max = band_hi
            f_min = max(band_lo, f_max - span)
        self.view_f_min = f_min
        self.view_f_max = f_max

    def fit_to_captured_band(self):
        if self.band in ("fred", "fred452"):
            # Tightly frame the active FRED 457/452 channel group (12.5 kHz channels) while SDR runs
            # at alias-free 960 kS/s; users can still zoom out with mouse wheel up to full captured span.
            custom_chans = self._band_profile.get("channels") or {}
            cap_lo = self.engine.center_freq - self.engine.sample_rate * 0.5
            cap_hi = self.engine.center_freq + self.engine.sample_rate * 0.5
            mon = getattr(self, "monitored_channels", None) or set()
            in_band_freqs = [
                float(fc)
                for ch, (fc, _) in custom_chans.items()
                if cap_lo <= fc <= cap_hi and (not mon or ch in mon)
            ]
            if not in_band_freqs:
                in_band_freqs = [float(fc) for _, (fc, _) in custom_chans.items() if cap_lo <= fc <= cap_hi]
            if in_band_freqs:
                lo_f = min(in_band_freqs)
                hi_f = max(in_band_freqs)
                span_ch = hi_f - lo_f
                pad = max(25000.0, span_ch * 0.45)
                self.view_f_min = lo_f - pad
                self.view_f_max = hi_f + pad
                return
        if self.engine.sample_rate >= 2000000:
            self.view_f_min = self.full_ptc_min_hz
            self.view_f_max = self.full_ptc_max_hz
        else:
            half = self.engine.sample_rate * 0.52
            self.view_f_min = self.engine.center_freq - half
            self.view_f_max = self.engine.center_freq + half

    # ----- Control Actions -----
    def _adjust_ppm(self, delta: int):
        new_ppm = max(-150, min(150, self.ppm + int(delta)))
        if self.mode == "grabber" and self.grabber_client:
            if self.auto_ppm:
                # Automatically pause Grabber Auto-PPM so it doesn't fight manual tweak
                self.grabber_client.send_control({"cmd": "set_auto_ppm", "enabled": False})
                self.auto_ppm = False
            resp = self.grabber_client.send_control({"cmd": "set_ppm", "ppm": new_ppm})
            if resp and resp.get("ok"):
                self._sync_state_from_dict(resp)
                self.set_toast(f"Grabber PPM -> {self.ppm:+d} (Auto-PPM paused).")
            else:
                self.set_toast("Grabber not responding to PPM command.")
        elif self.standalone_sdr:
            self.ppm = new_ppm
            self.standalone_sdr.update_hw(ppm=new_ppm)
            self.set_toast(f"SDR PPM -> {self.ppm:+d}")

    def _adjust_gain(self, step_dir: int = 0, set_agc: bool = False):
        if set_agc:
            new_gain = 0.0
        else:
            if self.gain_db <= 0.0:
                idx = 12 if step_dir > 0 else 10  # jump from AGC to ~20.7 dB
            else:
                nearest = min(range(len(RTLSDR_GAIN_STEPS)), key=lambda i: abs(RTLSDR_GAIN_STEPS[i] - self.gain_db))
                idx = max(1, min(len(RTLSDR_GAIN_STEPS) - 1, nearest + step_dir))
            new_gain = RTLSDR_GAIN_STEPS[idx]

        if self.mode == "grabber" and self.grabber_client:
            resp = self.grabber_client.send_control({"cmd": "set_gain", "gain": "0" if new_gain <= 0 else f"{new_gain:.1f}"})
            if resp and resp.get("ok"):
                self._sync_state_from_dict(resp)
                lbl = "AGC" if self.gain_db <= 0 else f"{self.gain_db:.1f} dB"
                self.set_toast(f"Grabber Gain -> {lbl}")
            else:
                self.set_toast("Grabber not responding to Gain command.")
        elif self.standalone_sdr:
            self.gain_db = new_gain
            self.standalone_sdr.update_hw(gain=new_gain)
            lbl = "AGC" if new_gain <= 0 else f"{new_gain:.1f} dB"
            self.set_toast(f"SDR Gain -> {lbl}")

    def _toggle_bias_tee(self):
        new_b = not self.bias_tee
        if self.mode == "grabber" and self.grabber_client:
            resp = self.grabber_client.send_control({"cmd": "set_bias_tee", "bias_tee": new_b})
            if resp and resp.get("ok"):
                self._sync_state_from_dict(resp)
                self.set_toast(f"Grabber Bias-T -> {'ON' if self.bias_tee else 'OFF'}")
        elif self.standalone_sdr:
            self.bias_tee = new_b
            self.standalone_sdr.update_hw(bias_tee=new_b)
            self.set_toast(f"SDR Bias-T -> {'ON' if self.bias_tee else 'OFF'}")

    def _toggle_auto_ppm(self):
        if self.mode == "grabber" and self.grabber_client:
            resp = self.grabber_client.send_control({"cmd": "set_auto_ppm", "enabled": not self.auto_ppm})
            if resp and resp.get("ok"):
                self._sync_state_from_dict(resp)
                self.set_toast(f"Grabber Auto-PPM -> {'ON' if self.auto_ppm else 'OFF'}")

    def _toggle_wideband(self, force_val: Optional[bool] = None):
        if self.mode != "grabber" or not self.grabber_client:
            return
        target = (not self.wideband_active) if force_val is None else bool(force_val)
        resp = self.grabber_client.send_control({"cmd": "set_wideband", "enabled": target}, timeout_ms=1000)
        if resp and resp.get("ok"):
            # Do not pre-mutate engine.center_freq/sample_rate before retuned IQ arrives;
            # process_iq_bytes() switches them atomically when the new-rate IQ chunk lands.
            self._sync_state_from_dict(resp, sync_engine_rf=False)
            target_sr = int(resp.get("sample_rate") or self.engine.sample_rate)
            target_cf = int(resp.get("center_freq") or self.engine.center_freq)
            if self.band == "ptc":
                if target_sr >= 2000000:
                    self.view_f_min = self.full_ptc_min_hz
                    self.view_f_max = self.full_ptc_max_hz
                else:
                    half = target_sr * 0.52
                    self.view_f_min = target_cf - half
                    self.view_f_max = target_cf + half
            else:
                self.fit_to_captured_band()
            lbl = "WIDEBAND (2.304 MS/s, Ch 97-176)" if self.wideband_active else f"NATIVE ({target_sr/1e6:.3f} MS/s)"
            self.set_toast(f"Grabber Bandwidth -> {lbl}")
        else:
            self.set_toast("Grabber not connected (unable to switch bandwidth).")

    def _toggle_channel_in_grabber(self, ch: int):
        if self.mode != "grabber" or not self.grabber_client:
            return
        if ch in self.monitored_channels:
            resp = self.grabber_client.send_control({"cmd": "remove_channel", "channel": ch})
        else:
            resp = self.grabber_client.send_control({"cmd": "add_channel", "channel": ch})
        if resp and resp.get("ok"):
            # Keep engine.center_freq/sample_rate locked to the actual incoming IQ metadata
            # so live spectrum curves never jump or misalign before retuned IQ arrives.
            self._sync_state_from_dict(resp, sync_engine_rf=False)
            action = "Monitoring" if ch in self.monitored_channels else "Removed"
            self.set_toast(f"{action} PTC Ch {ch} ({itc_channel_frequency(ch)/1e6:.4f} MHz)")
        elif resp and resp.get("error"):
            self.set_toast(f"Ch {ch}: {resp['error']}")

    def _standalone_device_for_band(self, band_key: Optional[str] = None) -> int:
        bk = band_key or self.band
        if bk in ("atcs", "atcs_mcp"):
            return int(self.aux_ports_cfg.get("atcs", {}).get("device", self.device_idx))
        if bk in ("fred", "fred452"):
            return int(self.aux_ports_cfg.get("fred", {}).get("device", self.device_idx))
        return int(self.device_idx)

    def _start_standalone_for_active_band(self, use_mock: bool = False):
        prof = self._band_profile
        c_hz = int(prof["center_hz"])
        r_hz = int(prof["rate_hz"])
        dev = self._standalone_device_for_band()
        bt_target = bool(self.bias_tee) if self.band == "ptc" else False
        self.engine.center_freq = c_hz
        self.engine.sample_rate = r_hz

        cur = self.standalone_sdr
        if cur is not None and cur.is_alive() and cur.use_mock == use_mock and cur.active:
            same_dev = (dev == cur.device_idx) or (dev == cur.requested_device_idx)
            if use_mock or same_dev:
                # Retune the already-open SDR in place (avoids USB close/reopen races & LIBUSB_ERROR_ACCESS)
                cur.update_hw(freq=c_hz, rate=r_hz, bias_tee=bt_target)
                return

        if cur is not None:
            # Synchronously close & join the previous SDR handle/subprocess before opening a new one
            cur.stop(wait=True, timeout=2.5)
            self.standalone_sdr = None
            if not use_mock:
                time.sleep(0.08)

        self.standalone_sdr = StandaloneSDRThread(
            center_freq=c_hz,
            sample_rate=r_hz,
            gain=self.gain_db or 20.7,
            ppm=self.ppm,
            device_idx=dev,
            bias_tee=bt_target,
            use_mock=use_mock,
            blip_sub=self.blip_sub if use_mock else None,
            sdr_cfg=self.config.get("grabber") or {},
        )
        self.standalone_sdr.start()

    def _switch_to_standalone(self):
        if self.mode == "standalone":
            return
        if self.grabber_client:
            self.grabber_client.stop()
            self.grabber_client = None
        self.mode = "standalone"
        if self.gain_db <= 0.0:
            self.gain_db = 20.7
        self._start_standalone_for_active_band(use_mock=False)
        self.wideband_active = (self.band == "ptc")
        self.set_toast(
            f"Switched to Standalone SDR ({self._band_profile['label']} @ {self._band_profile['center_hz']/1e6:.4f} MHz, "
            f"{self._band_profile['rate_hz']/1e6:.3f} MS/s, dev #{self._standalone_device_for_band()})."
        )

    def _cycle_band(self):
        order = ["ptc", "atcs", "atcs_mcp", "fred", "fred452"] if self.mode in ("standalone", "mock") else ["ptc", "atcs", "fred"]
        nxt = order[(order.index(self.band) + 1) % len(order)] if self.band in order else "ptc"
        self._explicit_band = True
        self._apply_band_profile(nxt)
        if self.mode == "grabber":
            if self.grabber_client:
                self.grabber_client.stop()
            self.grabber_client = GrabberIQClient(iq_port=self.iq_port, ctrl_port=self.ctrl_port, on_packet_meta=self.blip_sub.add_blip)
            self.grabber_client.start()
            self.blip_sub.update_ports(self._effective_blip_ports())
            self.set_toast(f"Switched Chroma band -> {self._band_profile['label']} (IQ:{self.iq_port}, Ctrl:{self.ctrl_port})")
        else:
            self._start_standalone_for_active_band(use_mock=(self.mode == "mock"))
            self.wideband_active = (self.band == "ptc")
            self.set_toast(
                f"Standalone SDR -> {self._band_profile['label']} @ {self._band_profile['center_hz']/1e6:.4f} MHz "
                f"({self._band_profile['rate_hz']/1e6:.3f} MS/s, dev #{self._standalone_device_for_band()})"
            )

    def _cycle_fft_size(self):
        sizes = [2048, 4096, 8192, 16384]
        idx = (sizes.index(self.engine.fft_size) + 1) % len(sizes) if self.engine.fft_size in sizes else 1
        self.engine.set_fft_size(sizes[idx])
        self.set_toast(f"FFT Size -> {sizes[idx]} bins ({self.engine.sample_rate / sizes[idx]:.0f} Hz/bin)")

    def _cycle_palette(self):
        self.palette_idx = (self.palette_idx + 1) % len(PALETTE_NAMES)
        self.set_toast(f"Waterfall Palette -> {PALETTE_NAMES[self.palette_idx]}")

    def _toggle_pause(self):
        self.paused = not self.paused
        self.pause_mono_ts = time.monotonic() if self.paused else 0.0
        self.set_toast("Waterfall PAUSED (click any blip to inspect)" if self.paused else "Waterfall RESUMED")

    # ----- Data Polling -----
    def poll_sources(self):
        now_m = time.monotonic()
        if self.mode == "grabber" and (now_m - self._last_aux_probe) >= 3.0:
            self._last_aux_probe = now_m
            self._probe_aux_demods(initial=False)
        item = None
        if self.mode == "grabber" and self.grabber_client:
            item = self.grabber_client.pop_latest_iq()
            # Only poll control port when IQ tap is quiet (>= 2s), avoiding periodic UI stalls
            if (
                item is None
                and (now_m - self.grabber_client.last_rx_ts) >= 2.0
                and (now_m - self._last_ctrl_poll) >= 2.0
            ):
                self._last_ctrl_poll = now_m
                st = self.grabber_client.send_control({"cmd": "get_state"}, timeout_ms=200)
                if st and st.get("ok"):
                    self._sync_state_from_dict(st)
        elif self.standalone_sdr:
            item = self.standalone_sdr.pop_latest_iq()
            if self.standalone_sdr.error_msg:
                self.set_toast(f"Standalone SDR error: {self.standalone_sdr.error_msg[:65]}")
                self.standalone_sdr.error_msg = ""

        if item is not None:
            meta, iq_bytes = item
            # Sync UI state without pre-mutating engine RF params so process_iq_bytes detects rate/freq changes
            self._sync_state_from_dict(meta, sync_engine_rf=False)
            c_freq = int(meta.get("center_freq") or self.engine.center_freq)
            s_rate = int(meta.get("sample_rate") or self.engine.sample_rate)
            s_fmt = str(meta.get("sample_format") or "u8")
            agc_active = (self.gain_db <= 0.0) and (self.mode != "mock")
            prev_ts = self.engine.last_update_ts
            self.engine.process_iq_bytes(
                iq_bytes,
                center_freq=c_freq,
                sample_rate=s_rate,
                now_mono=now_m,
                paused=self.paused,
                agc_stabilize=agc_active,
                sample_format=s_fmt,
            )
            if prev_ts > 0 and (now_m - prev_ts) > 0.005 and not self.paused:
                inst_rate = 1.0 / (now_m - prev_ts)
                self._last_wf_rate_est = 0.85 * self._last_wf_rate_est + 0.15 * min(60.0, max(2.0, inst_rate))

    # ----- Rendering -----
    def _draw_btn(self, key: str, rect, label: str, bg=COLOR_BTN, fg=COLOR_TEXT, cb=None):
        pygame = self.pygame
        pygame.draw.rect(self.screen, bg, rect, border_radius=3)
        pygame.draw.rect(self.screen, COLOR_BORDER, rect, 1, border_radius=3)
        surf = self.font_sm_bold.render(label, True, fg)
        self.screen.blit(surf, surf.get_rect(center=rect.center))
        if cb is not None:
            self.buttons[key] = (rect, cb)

    def _draw_toolbar(self, top_h: int):
        pygame = self.pygame
        self.buttons.clear()
        pygame.draw.rect(self.screen, COLOR_PANEL, (0, 0, self.width, top_h))
        pygame.draw.line(self.screen, COLOR_BORDER, (0, top_h - 1), (self.width, top_h - 1), 1)

        # Row 1: Connection, BW, PPM, Gain, Bias-T
        y1, h1 = 6, 24
        x = 8

        is_live = (
            (self.mode == "grabber" and self.grabber_client and (time.monotonic() - self.grabber_client.last_rx_ts) < 3.0)
            or (self.standalone_sdr and self.standalone_sdr.active)
        )
        if self.mode == "mock":
            badge_txt, badge_bg = "MOCK PTC", (75, 55, 130)
        elif self.mode == "standalone":
            badge_txt, badge_bg = ("SDR DIRECT", (32, 115, 78)) if is_live else ("SDR ERROR", COLOR_BTN_WARN)
        else:
            badge_txt, badge_bg = ("GRABBER LIVE", (32, 115, 78)) if is_live else ("WAIT GRABBER", (125, 85, 25))

        bw = self.font_sm_bold.size(badge_txt)[0] + 14
        self._draw_btn("mode_badge", pygame.Rect(x, y1, bw, h1), badge_txt, bg=badge_bg)
        x += bw + 6

        band_txt = f"Band:{self._band_profile['label']}"
        band_w = self.font_sm_bold.size(band_txt)[0] + 10
        self._draw_btn("cycle_band", pygame.Rect(x, y1, band_w, h1), band_txt, bg=(42, 62, 92), cb=self._cycle_band)
        x += band_w + 6

        if self.mode == "grabber" and not is_live:
            sw_txt = "Standalone SDR"
            sw_w = self.font_sm_bold.size(sw_txt)[0] + 12
            self._draw_btn("go_standalone", pygame.Rect(x, y1, sw_w, h1), sw_txt, bg=(52, 72, 110), cb=self._switch_to_standalone)
            x += sw_w + 6

        if self.mode == "grabber":
            sr_lbl = f"{self.engine.sample_rate/1e3:.0f}k" if self.engine.sample_rate < 1000000 else f"{self.engine.sample_rate/1e6:.2f}M"
            wb_txt = "BW:Wide(2.30M)" if self.wideband_active else f"BW:Native({sr_lbl})"
            wb_w = self.font_sm_bold.size(wb_txt)[0] + 12
            self._draw_btn("toggle_wb", pygame.Rect(x, y1, wb_w, h1), wb_txt, bg=COLOR_BTN_ON if self.wideband_active else COLOR_BTN, cb=self._toggle_wideband)
            x += wb_w + 8

        # PPM controls
        for lbl, delta in (("-5", -5), ("-1", -1)):
            r = pygame.Rect(x, y1, 24, h1)
            self._draw_btn(f"ppm_{lbl}", r, lbl, cb=lambda d=delta: self._adjust_ppm(d))
            x += 26
        ppm_txt = f"PPM:{self.ppm:+d}"
        ppm_w = max(62, self.font_sm_bold.size(ppm_txt)[0] + 10)
        pygame.draw.rect(self.screen, (20, 28, 40), (x, y1, ppm_w, h1), border_radius=3)
        pygame.draw.rect(self.screen, COLOR_BORDER, (x, y1, ppm_w, h1), 1, border_radius=3)
        ps = self.font_sm_bold.render(ppm_txt, True, COLOR_ACCENT)
        self.screen.blit(ps, ps.get_rect(center=(x + ppm_w // 2, y1 + h1 // 2)))
        x += ppm_w + 2
        for lbl, delta in (("+1", 1), ("+5", 5)):
            r = pygame.Rect(x, y1, 24, h1)
            self._draw_btn(f"ppm_{lbl}", r, lbl, cb=lambda d=delta: self._adjust_ppm(d))
            x += 26
        x += 4

        if self.mode == "grabber":
            ap_txt = "AutoPPM:ON" if self.auto_ppm else "AutoPPM:OFF"
            ap_w = self.font_sm_bold.size(ap_txt)[0] + 10
            self._draw_btn("auto_ppm", pygame.Rect(x, y1, ap_w, h1), ap_txt, bg=COLOR_BTN_ON if self.auto_ppm else COLOR_BTN, cb=self._toggle_auto_ppm)
            x += ap_w + 8

        # Gain controls
        self._draw_btn("gain_dn", pygame.Rect(x, y1, 22, h1), "-", cb=lambda: self._adjust_gain(-1))
        x += 24
        g_txt = "Gain:AGC" if self.gain_db <= 0.0 else f"Gain:{self.gain_db:.1f}dB"
        g_w = max(78, self.font_sm_bold.size(g_txt)[0] + 10)
        pygame.draw.rect(self.screen, (20, 28, 40), (x, y1, g_w, h1), border_radius=3)
        pygame.draw.rect(self.screen, COLOR_ACCENT if self.gain_db <= 0.0 else COLOR_BORDER, (x, y1, g_w, h1), 1, border_radius=3)
        gs = self.font_sm_bold.render(g_txt, True, COLOR_ACCENT if self.gain_db <= 0.0 else COLOR_CYAN)
        self.screen.blit(gs, gs.get_rect(center=(x + g_w // 2, y1 + h1 // 2)))
        x += g_w + 2
        self._draw_btn("gain_up", pygame.Rect(x, y1, 22, h1), "+", cb=lambda: self._adjust_gain(1))
        x += 24
        self._draw_btn("gain_agc", pygame.Rect(x, y1, 34, h1), "AGC", bg=COLOR_BTN_ON if self.gain_db <= 0 else COLOR_BTN, cb=lambda: self._adjust_gain(set_agc=True))
        x += 40

        # Bias-T
        bt_txt = "Bias-T:ON" if self.bias_tee else "Bias-T:OFF"
        bt_w = self.font_sm_bold.size(bt_txt)[0] + 12
        self._draw_btn("bias_t", pygame.Rect(x, y1, bt_w, h1), bt_txt, bg=(35, 118, 72) if self.bias_tee else COLOR_BTN, cb=self._toggle_bias_tee)
        x += bt_w + 8

        if self.engine.adc_clipping:
            cl_txt = "! ADC CLIP !"
            cl_w = self.font_sm_bold.size(cl_txt)[0] + 12
            self._draw_btn("clip_warn", pygame.Rect(x, y1, cl_w, h1), cl_txt, bg=COLOR_BTN_WARN)

        # Row 2: FFT, Palette, Peaks, Blips, Contrast, Pause, Fit
        y2, h2 = 36, 24
        x = 8
        row2_items = [
            ("fft", f"FFT:{self.engine.fft_size}", COLOR_BTN, self._cycle_fft_size),
            ("pal", f"Pal:{PALETTE_NAMES[self.palette_idx]}", COLOR_BTN, self._cycle_palette),
            ("fade", f"FadePk:{'On' if self.show_fade_peaks else 'Off'}", COLOR_BTN_ON if self.show_fade_peaks else COLOR_BTN, self._toggle_fade_peaks),
            ("maxh", f"MaxHold:{'On' if self.show_max_hold else 'Off'}", COLOR_BTN_ON if self.show_max_hold else COLOR_BTN, self._toggle_max_hold),
            ("rst_pk", "ResetPk", COLOR_BTN, self.engine.reset_peaks),
            ("blips", f"Blips:{'On' if self.show_blips else 'Off'}", COLOR_BTN_ON if self.show_blips else COLOR_BTN, self._toggle_blips),
            ("labels", f"IDs:{'On' if self.show_blip_labels else 'Off'}", COLOR_BTN_ON if self.show_blip_labels else COLOR_BTN, self._toggle_blip_labels),
            ("min_dn", "Min-", COLOR_BTN, lambda: self._shift_db(min_d=-5.0)),
            ("min_up", "Min+", COLOR_BTN, lambda: self._shift_db(min_d=5.0)),
            ("max_dn", "Max-", COLOR_BTN, lambda: self._shift_db(max_d=-5.0)),
            ("max_up", "Max+", COLOR_BTN, lambda: self._shift_db(max_d=5.0)),
            ("pause", "RESUMED" if not self.paused else "PAUSED", COLOR_BTN_WARN if self.paused else COLOR_BTN, self._toggle_pause),
            ("fit", "Fit(Home)", COLOR_BTN, self.fit_to_captured_band),
        ]
        for key, lbl, bg, cb in row2_items:
            bw = self.font_sm_bold.size(lbl)[0] + 12
            if x + bw > self.width - 8:
                break
            self._draw_btn(key, pygame.Rect(x, y2, bw, h2), lbl, bg=bg, cb=cb)
            x += bw + 4

    def _toggle_fade_peaks(self):
        self.show_fade_peaks = not self.show_fade_peaks

    def _toggle_max_hold(self):
        self.show_max_hold = not self.show_max_hold

    def _toggle_blips(self):
        self.show_blips = not self.show_blips

    def _toggle_blip_labels(self):
        self.show_blip_labels = not self.show_blip_labels

    def _shift_db(self, min_d: float = 0.0, max_d: float = 0.0):
        new_min = max(-130.0, min(self.max_db - 15.0, self.min_db + min_d))
        new_max = max(new_min + 15.0, min(10.0, self.max_db + max_d))
        self.min_db, self.max_db = new_min, new_max

    def _draw_spectrum_pane(self, spec_rect, axis_rect):
        pygame = self.pygame
        pygame.draw.rect(self.screen, (11, 16, 24), spec_rect)

        # Shade captured passband vs out-of-band edges
        cap_lo = self.engine.center_freq - self.engine.sample_rate * 0.5
        cap_hi = self.engine.center_freq + self.engine.sample_rate * 0.5
        x_lo = max(spec_rect.left, int(self.freq_to_x(cap_lo, spec_rect)))
        x_hi = min(spec_rect.right, int(self.freq_to_x(cap_hi, spec_rect)))
        if x_lo > spec_rect.left:
            pygame.draw.rect(self.screen, (6, 8, 12), (spec_rect.left, spec_rect.top, x_lo - spec_rect.left, spec_rect.height))
        if x_hi < spec_rect.right:
            pygame.draw.rect(self.screen, (6, 8, 12), (x_hi, spec_rect.top, spec_rect.right - x_hi, spec_rect.height))

        # Monitored & locked channel vertical tint bands
        band_surf = pygame.Surface((spec_rect.width, spec_rect.height), pygame.SRCALPHA)
        custom_chans = self._band_profile.get("channels")
        if custom_chans:
            half_bw_hz = 6250  # 12.5 kHz narrowband channel width for Fred (EOT/HOT/DPU/RLC) & Latch (ATCS)
            for ch in self.monitored_channels:
                if ch not in custom_chans:
                    continue
                fc = custom_chans[ch][0]
                if fc + half_bw_hz < self.view_f_min or fc - half_bw_hz > self.view_f_max:
                    continue
                bx0 = int(self.freq_to_x(fc - half_bw_hz, spec_rect)) - spec_rect.x
                bx1 = int(self.freq_to_x(fc + half_bw_hz, spec_rect)) - spec_rect.x
                bw = max(2, bx1 - bx0)
                col = (255, 195, 55, 28) if ch in self.locked_channels else (45, 220, 255, 24)
                pygame.draw.rect(band_surf, col, (bx0, 0, bw, spec_rect.height))
        else:
            for ch in self.monitored_channels:
                fc = itc_channel_frequency(ch)
                if fc + 12500 < self.view_f_min or fc - 12500 > self.view_f_max:
                    continue
                bx0 = int(self.freq_to_x(fc - 12500, spec_rect)) - spec_rect.x
                bx1 = int(self.freq_to_x(fc + 12500, spec_rect)) - spec_rect.x
                bw = max(2, bx1 - bx0)
                col = (255, 195, 55, 28) if ch in self.locked_channels else (45, 220, 255, 24)
                pygame.draw.rect(band_surf, col, (bx0, 0, bw, spec_rect.height))
        self.screen.blit(band_surf, spec_rect.topleft)

        # Horizontal dBFS gridlines
        db_step = 10 if (self.max_db - self.min_db) <= 75 else 15
        first_db = int(math.ceil(self.min_db / db_step) * db_step)
        for dbv in range(first_db, int(self.max_db) + 1, db_step):
            gy = self.db_to_y(dbv, spec_rect)
            pygame.draw.line(self.screen, COLOR_GRID, (spec_rect.left, gy), (spec_rect.right, gy), 1)
            lbl = self.font_xs.render(f"{dbv:>4d}", True, COLOR_TEXT_DIM)
            self.screen.blit(lbl, (axis_rect.right - lbl.get_width() - 6, gy - 5))

        # Vertical channel gridlines
        if custom_chans:
            for ch, (fc, _) in custom_chans.items():
                if fc < self.view_f_min or fc > self.view_f_max:
                    continue
                cx = int(self.freq_to_x(fc, spec_rect))
                if spec_rect.left <= cx <= spec_rect.right:
                    gcol = (40, 72, 92) if ch in self.monitored_channels else (22, 31, 44)
                    pygame.draw.line(self.screen, gcol, (cx, spec_rect.top), (cx, spec_rect.bottom), 1)
        else:
            first_ch = max(PTC_CH_MIN, int(math.floor((self.view_f_min - PTC_BASE_HZ) / PTC_STEP_HZ)))
            last_ch = min(PTC_CH_MAX, int(math.ceil((self.view_f_max - PTC_BASE_HZ) / PTC_STEP_HZ)))
            px_per_ch = (PTC_STEP_HZ / max(1.0, self.view_f_max - self.view_f_min)) * spec_rect.width
            ch_step = 1 if px_per_ch >= 14 else (2 if px_per_ch >= 7 else 5)
            for ch in range(first_ch, last_ch + 1):
                if ch % ch_step != 0 and ch not in self.monitored_channels:
                    continue
                cx = int(self.freq_to_x(itc_channel_frequency(ch), spec_rect))
                if spec_rect.left <= cx <= spec_rect.right:
                    gcol = (40, 72, 92) if ch in self.monitored_channels else (22, 31, 44)
                    pygame.draw.line(self.screen, gcol, (cx, spec_rect.top), (cx, spec_rect.bottom), 1)

        # Noise floor horizontal reference line
        nf_y = self.db_to_y(self.engine.noise_floor_db, spec_rect)
        if spec_rect.top <= nf_y <= spec_rect.bottom:
            for dx in range(spec_rect.left, spec_rect.right, 10):
                pygame.draw.line(self.screen, (70, 95, 115), (dx, nf_y), (min(spec_rect.right, dx + 5), nf_y), 1)

        # Curves: Max-Hold, Fading Peak-Hold, and Live EMA Spectrum
        w_px = spec_rect.width
        xs = np.arange(spec_rect.left, spec_rect.left + w_px, dtype=np.int32)

        def _curve_points(arr_db: np.ndarray) -> List[Tuple[int, int]]:
            vals, valid = self.engine.sample_curve_for_view(arr_db, self.view_f_min, self.view_f_max, w_px)
            span = max(1.0, self.max_db - self.min_db)
            norm = np.clip((vals - self.min_db) / span, 0.0, 1.0)
            ys = (spec_rect.bottom - norm * spec_rect.height).astype(np.int32)
            return [(int(xs[i]), int(ys[i])) for i in range(w_px) if valid[i]]

        if self.engine.frames_processed > 0:
            if self.show_max_hold:
                pts_mh = _curve_points(self.engine.max_hold_db)
                if len(pts_mh) >= 2:
                    pygame.draw.lines(self.screen, (185, 95, 255), False, pts_mh, 1)

            if self.show_fade_peaks:
                pts_fp = _curve_points(self.engine.fade_peak_db)
                if len(pts_fp) >= 2:
                    pygame.draw.lines(self.screen, (255, 195, 55), False, pts_fp, 1)

            pts_ema = _curve_points(self.engine.ema_db)
            if len(pts_ema) >= 2:
                # Translucent fill beneath live spectrum curve
                poly = [(pts_ema[0][0], spec_rect.bottom)] + pts_ema + [(pts_ema[-1][0], spec_rect.bottom)]
                fill_s = pygame.Surface((spec_rect.width, spec_rect.height), pygame.SRCALPHA)
                shifted = [(px - spec_rect.x, py - spec_rect.y) for px, py in poly]
                pygame.draw.polygon(fill_s, (35, 185, 240, 48), shifted)
                self.screen.blit(fill_s, spec_rect.topleft)
                pygame.draw.lines(self.screen, COLOR_CYAN, False, pts_ema, 2)

        # Border & Y-axis title
        pygame.draw.rect(self.screen, COLOR_BORDER, spec_rect, 1)
        ax_lbl = self.font_xs.render("dBFS", True, COLOR_TEXT_DIM)
        self.screen.blit(ax_lbl, (8, spec_rect.top + 4))
        nf_lbl = self.font_xs.render(f"NF:{self.engine.noise_floor_db:.0f}", True, (110, 150, 175))
        self.screen.blit(nf_lbl, (4, spec_rect.bottom - 14))

    def _draw_channel_ruler(self, ruler_rect, now_mono: float):
        pygame = self.pygame
        self.channel_hit_rects.clear()
        pygame.draw.rect(self.screen, (18, 25, 36), ruler_rect)
        pygame.draw.line(self.screen, COLOR_BORDER, (ruler_rect.left, ruler_rect.top), (ruler_rect.right, ruler_rect.top), 1)
        pygame.draw.line(self.screen, COLOR_BORDER, (ruler_rect.left, ruler_rect.bottom - 1), (ruler_rect.right, ruler_rect.bottom - 1), 1)

        ch_ppm = self.blip_sub.get_channel_ppm_counts(now_mono, 60.0)
        custom_chans = self._band_profile.get("channels")
        if custom_chans:
            vis_chans = sorted(
                [(ch, fc, short_name) for ch, (fc, short_name) in custom_chans.items() if self.view_f_min <= fc <= self.view_f_max],
                key=lambda item: item[1],
            )
            min_px_gap = 999.0
            for idx_c in range(1, len(vis_chans)):
                g = abs(self.freq_to_x(vis_chans[idx_c][1], ruler_rect) - self.freq_to_x(vis_chans[idx_c - 1][1], ruler_rect))
                if g < min_px_gap:
                    min_px_gap = g
            last_lbl_right = -1000
            last_freq_right = -1000
            half_w_hz = 6250  # 12.5 kHz channel width for EOT, HOT, DPU, RLC, and ATCS
            for ch, fc, short_name in vis_chans:
                cx = int(self.freq_to_x(fc, ruler_rect))
                if cx < ruler_rect.left or cx > ruler_rect.right:
                    continue
                bx0 = max(ruler_rect.left, int(self.freq_to_x(fc - half_w_hz, ruler_rect)))
                bx1 = min(ruler_rect.right, int(self.freq_to_x(fc + half_w_hz, ruler_rect)))
                bw = max(6, bx1 - bx0)
                ch_rect = pygame.Rect(bx0, ruler_rect.top + 1, bw, ruler_rect.height - 2)
                self.channel_hit_rects.append((ch_rect, ch))

                is_mon = ch in self.monitored_channels
                is_lock = ch in self.locked_channels
                if is_mon:
                    bg = (55, 50, 22) if is_lock else (20, 56, 72)
                    bd = COLOR_ACCENT if is_lock else COLOR_CYAN
                    pygame.draw.rect(self.screen, bg, ch_rect)
                    pygame.draw.rect(self.screen, bd, ch_rect, 1)

                # Activity mini-bar at top of channel slot (matches PTC Band)
                cnt = ch_ppm.get(ch, 0)
                if cnt > 0:
                    bar_w = max(2, min(bw - 2, int(bw * min(1.0, cnt / 20.0))))
                    pygame.draw.rect(self.screen, COLOR_GREEN, (bx0 + 1, ruler_rect.top + 2, bar_w, 3))

                pygame.draw.line(self.screen, COLOR_ACCENT if is_lock else (COLOR_CYAN if is_mon else COLOR_BORDER), (cx, ruler_rect.top), (cx, ruler_rect.top + 4), 1)

                # Line 1: channel number / mnemonic (mirrors PTC channel flag top line)
                if min_px_gap >= 40.0:
                    tag = f"{ch}:{short_name}." if is_lock else f"{ch}:{short_name}"
                elif min_px_gap >= 24.0:
                    tag = f"{short_name}." if is_lock else short_name
                else:
                    tag = f"{ch}." if is_lock else str(ch)
                col = COLOR_ACCENT if is_lock else (COLOR_CYAN if is_mon else COLOR_TEXT)
                ts = self.font_xs.render(tag, True, col)
                lx = cx - ts.get_width() // 2
                if lx >= last_lbl_right + 2:
                    self.screen.blit(ts, (lx, ruler_rect.top + 5))
                    last_lbl_right = lx + ts.get_width()

                # Line 2: channel frequency in MHz (mirrors PTC channel flag second line)
                freq_txt = f"{fc/1e6:.5f}" if (fc % 1000) != 0 else f"{fc/1e6:.4f}"
                fs = self.font_xs.render(freq_txt, True, COLOR_TEXT_DIM)
                fx = cx - fs.get_width() // 2
                if fx >= last_freq_right + 3:
                    self.screen.blit(fs, (fx, ruler_rect.top + 16))
                    last_freq_right = fx + fs.get_width()
            return

        first_ch = max(PTC_CH_MIN, int(math.floor((self.view_f_min - PTC_BASE_HZ) / PTC_STEP_HZ)))
        last_ch = min(PTC_CH_MAX, int(math.ceil((self.view_f_max - PTC_BASE_HZ) / PTC_STEP_HZ)))
        px_per_ch = (PTC_STEP_HZ / max(1.0, self.view_f_max - self.view_f_min)) * ruler_rect.width
        lbl_step = 1 if px_per_ch >= 28 else (2 if px_per_ch >= 15 else (5 if px_per_ch >= 7 else 10))

        for ch in range(first_ch, last_ch + 1):
            fc = itc_channel_frequency(ch)
            cx = int(self.freq_to_x(fc, ruler_rect))
            if cx < ruler_rect.left or cx > ruler_rect.right:
                continue
            bx0 = max(ruler_rect.left, int(self.freq_to_x(fc - 12500, ruler_rect)))
            bx1 = min(ruler_rect.right, int(self.freq_to_x(fc + 12500, ruler_rect)))
            bw = max(6, bx1 - bx0)
            ch_rect = pygame.Rect(bx0, ruler_rect.top + 1, bw, ruler_rect.height - 2)
            self.channel_hit_rects.append((ch_rect, ch))

            is_mon = ch in self.monitored_channels
            is_lock = ch in self.locked_channels
            if is_mon:
                bg = (55, 50, 22) if is_lock else (20, 56, 72)
                bd = COLOR_ACCENT if is_lock else COLOR_CYAN
                pygame.draw.rect(self.screen, bg, ch_rect)
                pygame.draw.rect(self.screen, bd, ch_rect, 1)

            # Activity mini-bar at top of channel slot
            cnt = ch_ppm.get(ch, 0)
            if cnt > 0:
                bar_w = max(2, min(bw - 2, int(bw * min(1.0, cnt / 20.0))))
                pygame.draw.rect(self.screen, COLOR_GREEN, (bx0 + 1, ruler_rect.top + 2, bar_w, 3))

            pygame.draw.line(self.screen, COLOR_BORDER, (cx, ruler_rect.top), (cx, ruler_rect.top + 4), 1)
            if (ch % lbl_step == 0) or (is_mon and px_per_ch >= 14):
                tag = f"{ch}." if is_lock else str(ch)
                col = COLOR_ACCENT if is_lock else (COLOR_CYAN if is_mon else COLOR_TEXT_DIM)
                ts = self.font_xs.render(tag, True, col)
                self.screen.blit(ts, (cx - ts.get_width() // 2, ruler_rect.top + 6))
                if px_per_ch >= 68:
                    fs = self.font_xs.render(f"{fc/1e6:.4f}", True, COLOR_TEXT_DIM)
                    self.screen.blit(fs, (cx - fs.get_width() // 2, ruler_rect.top + 16))

    def _draw_waterfall_and_blips(self, wf_rect, now_mono: float):
        pygame = self.pygame
        self.blip_hit_rects.clear()
        lut = COLOR_PALETTES[PALETTE_NAMES[self.palette_idx]]

        rgb = self.engine.render_waterfall_rgb(
            self.view_f_min,
            self.view_f_max,
            wf_rect.width,
            wf_rect.height,
            self.min_db,
            self.max_db,
            lut,
            rows_per_frame_px=self.wf_row_px,
        )
        wf_surf = pygame.image.frombuffer(rgb.tobytes(), (wf_rect.width, wf_rect.height), "RGB")
        self.screen.blit(wf_surf, wf_rect.topleft)

        # Subtle monitored channel edge ticks on waterfall
        custom_chans = self._band_profile.get("channels")
        for ch in self.monitored_channels:
            if custom_chans:
                if ch not in custom_chans:
                    continue
                fc = custom_chans[ch][0]
            else:
                fc = itc_channel_frequency(ch)
            if fc < self.view_f_min or fc > self.view_f_max:
                continue
            cx = int(self.freq_to_x(fc, wf_rect))
            col = (255, 195, 55) if ch in self.locked_channels else (45, 220, 255)
            pygame.draw.line(self.screen, col, (cx, wf_rect.top), (cx, wf_rect.top + 6), 2)

        # Flowing Packet Blips locked directly to waterfall row timestamps (zero depth jitter)
        if self.show_blips:
            asc_ts, asc_rows = self.engine.get_visible_row_timeline(
                wf_rect.height, rows_per_frame_px=self.wf_row_px
            )
            blips = self.blip_sub.get_blips_snapshot()
            occupied_lbl_boxes: List[Any] = []

            for b in reversed(blips):
                by = self.engine.timestamp_to_waterfall_y(
                    b["ts"],
                    wf_rect.top,
                    wf_rect.height,
                    self.wf_row_px,
                    asc_ts,
                    asc_rows,
                    paused=self.paused,
                    pause_ts=self.pause_mono_ts,
                )
                if by is None:
                    continue
                fx = b["exact_freq_hz"]
                if fx < self.view_f_min or fx > self.view_f_max:
                    continue
                bx = int(self.freq_to_x(fx, wf_rect))
                bcol = b["color"]

                # Draw blip marker (EMP packets render as high-contrast pure black dots)
                is_emp = (b.get("category") == "EMP")
                if is_emp:
                    pygame.draw.circle(self.screen, (245, 248, 255), (bx, by), 5)
                    pygame.draw.circle(self.screen, (0, 0, 0), (bx, by), 4)
                else:
                    pygame.draw.circle(self.screen, (10, 12, 18), (bx, by), 5)
                    pygame.draw.circle(self.screen, bcol, (bx, by), 3)
                hit_r = pygame.Rect(bx - 7, by - 7, 14, 14)
                self.blip_hit_rects.append((hit_r, b))

                if self.show_blip_labels:
                    lbl_fg = (0, 0, 0) if is_emp else bcol
                    lbl_s = self.font_xs.render(b["short_label"], True, lbl_fg)
                    lw, lh = lbl_s.get_size()
                    lx = min(wf_rect.right - lw - 4, bx + 7)
                    ly = by - lh // 2
                    lrect = pygame.Rect(lx - 2, ly - 1, lw + 4, lh + 2)
                    if not any(lrect.colliderect(ob) for ob in occupied_lbl_boxes):
                        occupied_lbl_boxes.append(lrect)
                        bg_s = pygame.Surface((lrect.width, lrect.height), pygame.SRCALPHA)
                        if is_emp:
                            bg_s.fill((235, 240, 248, 225))
                            pygame.draw.rect(bg_s, (0, 0, 0, 255), (0, 0, lrect.width, lrect.height), 1)
                        else:
                            bg_s.fill((8, 12, 18, 185))
                        self.screen.blit(bg_s, lrect.topleft)
                        self.screen.blit(lbl_s, (lx, ly))

        pygame.draw.rect(self.screen, COLOR_BORDER, wf_rect, 1)

    def _draw_crosshair_and_inspector(self, spec_rect, ruler_rect, wf_rect):
        pygame = self.pygame
        mx, my = pygame.mouse.get_pos()

        # Crosshair across spectrum & waterfall
        in_spec = spec_rect.collidepoint(mx, my)
        in_wf = wf_rect.collidepoint(mx, my)
        if in_spec or in_wf:
            pygame.draw.line(self.screen, (120, 140, 165), (mx, spec_rect.top), (mx, wf_rect.bottom), 1)
            if in_spec:
                pygame.draw.line(self.screen, (120, 140, 165), (spec_rect.left, my), (spec_rect.right, my), 1)

        # Check hover over a packet blip (or display clicked selected_blip)
        inspect_b = self.selected_blip
        if in_wf and self.show_blips:
            for hr, b in self.blip_hit_rects:
                if hr.collidepoint(mx, my):
                    inspect_b = b
                    break

        if inspect_b is not None:
            lines = [
                f"PKT [{inspect_b['category']}] {inspect_b['type_name']} ({inspect_b['rate']}) @ {inspect_b['wall_time']}",
                f"Ch {inspect_b['channel']} ({inspect_b['freq_hz']/1e6:.4f} MHz) | dF: {inspect_b['freq_offset_hz']:+.1f} Hz | RSSI: {inspect_b['rssi']:.1f} dBFS | SNR: {inspect_b['snr']:.1f} dB",
            ]
            id_parts = []
            if inspect_b["railroad"]:
                id_parts.append(f"RR: {inspect_b['railroad']}")
            if inspect_b["wiu"]:
                id_parts.append(f"WIU/ID: {inspect_b['wiu']}")
            if inspect_b["src"] or inspect_b["dst"]:
                id_parts.append(f"Route: {inspect_b['src'] or '?'} -> {inspect_b['dst'] or '?'}")
            id_parts.append(f"SyncErr: {inspect_b['sync_errors']} | Len: {inspect_b['length']}B")
            lines.append(" | ".join(id_parts))
            if inspect_b["hex"]:
                lines.append(f"HEX: {inspect_b['hex'][:68]}{'...' if len(inspect_b['hex']) > 68 else ''}")

            cw = max(self.font_sm.size(l)[0] for l in lines) + 20
            ch = len(lines) * 16 + 14
            cx = min(wf_rect.right - cw - 8, max(wf_rect.left + 8, mx + 14))
            cy = max(wf_rect.top + 8, min(wf_rect.bottom - ch - 8, my - ch - 8))
            card = pygame.Surface((cw, ch), pygame.SRCALPHA)
            card.fill((12, 18, 28, 235))
            is_emp_card = (inspect_b.get("category") == "EMP")
            border_col = (220, 228, 240) if is_emp_card else inspect_b["color"]
            hdr_col = (245, 248, 255) if is_emp_card else inspect_b["color"]
            pygame.draw.rect(card, border_col, (0, 0, cw, ch), 1, border_radius=5)
            for idx, ln in enumerate(lines):
                c = hdr_col if idx == 0 else COLOR_TEXT
                card.blit(self.font_sm.render(ln, True, c), (10, 7 + idx * 16))
            self.screen.blit(card, (cx, cy))

    def _draw_footer(self, footer_h: int, spec_rect, wf_rect):
        pygame = self.pygame
        fy = self.height - footer_h
        pygame.draw.rect(self.screen, COLOR_PANEL, (0, fy, self.width, footer_h))
        pygame.draw.line(self.screen, COLOR_BORDER, (0, fy), (self.width, fy), 1)

        mx, my = pygame.mouse.get_pos()
        cursor_info = ""
        if spec_rect.collidepoint(mx, my) or wf_rect.collidepoint(mx, my):
            f_cur = self.x_to_freq(mx, spec_rect)
            db_str = ""
            if spec_rect.collidepoint(mx, my):
                db_cur = self.min_db + ((spec_rect.bottom - my) / float(max(1, spec_rect.height))) * (self.max_db - self.min_db)
                db_str = f" | {db_cur:.1f} dBFS"
            custom_chans = self._band_profile.get("channels")
            if custom_chans:
                near_ch, (near_fc, near_lbl) = min(custom_chans.items(), key=lambda kv: abs(f_cur - kv[1][0]))
                ch_off = f_cur - float(near_fc)
                cursor_info = f"Cursor: {f_cur/1e6:.5f} MHz ({near_lbl} {ch_off/1e3:+.1f} kHz){db_str}  |  "
            else:
                ch_near, ch_off = freq_to_nearest_channel(f_cur)
                cursor_info = f"Cursor: {f_cur/1e6:.4f} MHz (Ch {ch_near} {ch_off/1e3:+.1f} kHz){db_str}  |  "

        fps = self.clock.get_fps()
        agc_tag = f" | AGC-Stab:{self.engine.agc_comp_db:+.1f}dB" if (self.gain_db <= 0.0 and self.mode != "mock") else ""
        aux_parts: List[str] = []
        if self.detected_bands:
            aux_live = [b.upper() for b in ("ptc", "atcs", "fred") if b in self.detected_bands]
            if any(b in self.detected_bands for b in ("atcs", "fred")):
                aux_parts.append(f"Live:[{'/'.join(aux_live)}]")
        with self.blip_sub.lock:
            for ak in ("atcs", "fred"):
                ent = self.blip_sub.last_aux_summary.get(ak)
                if ent and (time.monotonic() - ent[0]) < 120.0:
                    aux_parts.append(ent[1][:54])
        aux_str = (" | " + " | ".join(aux_parts)) if aux_parts else ""
        info = (
            f"{cursor_info}"
            f"Center: {self.engine.center_freq/1e6:.4f} MHz | Rate: {self.engine.sample_rate/1e6:.3f} MS/s | "
            f"Mon: {len(self.monitored_channels)} ch ({len(self.locked_channels)} lock){agc_tag} | "
            f"dB:[{self.min_db:.0f},{self.max_db:.0f}] | {fps:.0f} FPS{aux_str} | {self.status_toast}"
        )
        surf = self.font_sm.render(info, True, COLOR_TEXT)
        self.screen.blit(surf, (10, fy + 5))

    def draw(self):
        self.screen.fill(COLOR_BG)
        now_m = time.monotonic()
        top_h, footer_h, spec_rect, ruler_rect, wf_rect, axis_rect = self._layout_rects()
        self._draw_toolbar(top_h)
        self._draw_spectrum_pane(spec_rect, axis_rect)
        self._draw_channel_ruler(ruler_rect, now_m)
        self._draw_waterfall_and_blips(wf_rect, now_m)
        self._draw_crosshair_and_inspector(spec_rect, ruler_rect, wf_rect)
        self._draw_footer(footer_h, spec_rect, wf_rect)

    # ----- Event Handling -----
    def handle_event(self, event) -> bool:
        pygame = self.pygame
        _, _, spec_rect, ruler_rect, wf_rect, axis_rect = self._layout_rects()

        if event.type == pygame.QUIT:
            return False
        elif event.type == pygame.VIDEORESIZE:
            self.width = max(800, event.w)
            self.height = max(520, event.h)
            self.screen = pygame.display.set_mode((self.width, self.height), pygame.RESIZABLE)
        elif event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                if self.selected_blip is not None:
                    self.selected_blip = None
                else:
                    return False
            elif event.key == pygame.K_SPACE:
                self._toggle_pause()
            elif event.key == pygame.K_HOME:
                self.fit_to_captured_band()
            elif event.key == pygame.K_r:
                self.engine.reset_peaks()
                self.set_toast("Reset spectrum peaks & max-hold.")
            elif event.key == pygame.K_p:
                self._cycle_palette()
            elif event.key == pygame.K_w:
                self._toggle_wideband()
            elif event.key == pygame.K_b:
                self._toggle_blips()
            elif event.key == pygame.K_i:
                self._toggle_blip_labels()
            elif event.key in (pygame.K_EQUALS, pygame.K_PLUS):
                self.zoom_at_freq(0.5 * (self.view_f_min + self.view_f_max), 0.78)
            elif event.key == pygame.K_MINUS:
                self.zoom_at_freq(0.5 * (self.view_f_min + self.view_f_max), 1.28)
        elif event.type == pygame.MOUSEWHEEL:
            mx, my = pygame.mouse.get_pos()
            if spec_rect.collidepoint(mx, my) or wf_rect.collidepoint(mx, my) or ruler_rect.collidepoint(mx, my):
                anchor_f = self.x_to_freq(mx, spec_rect)
                factor = 0.80 if event.y > 0 else 1.25
                self.zoom_at_freq(anchor_f, factor)
            elif axis_rect.collidepoint(mx, my):
                self._shift_db(min_d=-5.0 if event.y < 0 else 5.0)
        elif event.type == pygame.MOUSEBUTTONDOWN:
            mx, my = event.pos
            if event.button == 1:
                for _, (rect, cb) in self.buttons.items():
                    if rect.collidepoint(mx, my):
                        cb()
                        return True
                if ruler_rect.collidepoint(mx, my):
                    for crect, ch in self.channel_hit_rects:
                        if crect.collidepoint(mx, my):
                            self._toggle_channel_in_grabber(ch)
                            return True
                if wf_rect.collidepoint(mx, my) and self.show_blips:
                    for hr, b in self.blip_hit_rects:
                        if hr.collidepoint(mx, my):
                            self.selected_blip = b
                            return True
                    self.selected_blip = None
                if spec_rect.collidepoint(mx, my) or wf_rect.collidepoint(mx, my):
                    self._dragging_pan = True
                    self._drag_start_x = mx
                    self._drag_start_fmin = self.view_f_min
                    self._drag_start_fmax = self.view_f_max
                elif axis_rect.collidepoint(mx, my):
                    self._dragging_db = True
                    self._drag_start_y = my
                    self._drag_start_mindb = self.min_db
                    self._drag_start_maxdb = self.max_db
            elif event.button == 3:
                if ruler_rect.collidepoint(mx, my):
                    for crect, ch in self.channel_hit_rects:
                        if crect.collidepoint(mx, my):
                            self._toggle_channel_in_grabber(ch)
                            return True
                elif spec_rect.collidepoint(mx, my) or wf_rect.collidepoint(mx, my):
                    self.fit_to_captured_band()
        elif event.type == pygame.MOUSEBUTTONUP:
            if event.button == 1:
                self._dragging_pan = False
                self._dragging_db = False
        elif event.type == pygame.MOUSEMOTION:
            mx, my = event.pos
            if self._dragging_pan:
                dx = mx - self._drag_start_x
                span = self._drag_start_fmax - self._drag_start_fmin
                df = -(dx / float(max(1, spec_rect.width))) * span
                self._clamp_view(self._drag_start_fmin + df, self._drag_start_fmax + df)
            elif self._dragging_db:
                dy = my - self._drag_start_y
                db_shift = (dy / float(max(1, spec_rect.height))) * (self._drag_start_maxdb - self._drag_start_mindb)
                new_min = max(-130.0, min(-20.0, self._drag_start_mindb + db_shift))
                new_max = new_min + (self._drag_start_maxdb - self._drag_start_mindb)
                if new_max <= 10.0:
                    self.min_db, self.max_db = new_min, new_max
        return True

    def run(self):
        pygame = self.pygame
        running = True
        try:
            while running:
                for ev in pygame.event.get():
                    if not self.handle_event(ev):
                        running = False
                        break
                if not running:
                    break
                self.poll_sources()
                self.draw()
                pygame.display.flip()
                self.clock.tick(30)
        finally:
            self.shutdown()
            pygame.quit()


def build_chroma_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"ApexBOSChroma ({VERSION}) - PTC 220 MHz Spectrum Analyzer & Waterfall"
    )
    parser.add_argument("--config", default=None, help="Path to unified config.json")
    parser.add_argument("--band", default=None, choices=["ptc", "atcs", "atcs_mcp", "fred", "fred452"], help="RF band profile (auto-detects PTC/ATCS/FRED if omitted)")
    parser.add_argument("--chroma-port", type=int, default=DEFAULT_CHROMA_IQ_PORT, help=f"Demodulator XPUB IQ tap port (default: {DEFAULT_CHROMA_IQ_PORT})")
    parser.add_argument("--ctrl-port", type=int, default=DEFAULT_CHROMA_CTRL_PORT, help=f"Grabber REP control port (default: {DEFAULT_CHROMA_CTRL_PORT})")
    parser.add_argument("--wideband", action="store_true", default=False, help="Automatically expand Grabber to 2.304 MS/s wideband on connect")
    parser.add_argument("--standalone", action="store_true", default=False, help="Open RTL-SDR directly without attaching to Grabber")
    parser.add_argument("--mock", action="store_true", default=False, help="Run with synthetic 220 MHz PTC bursts and packet blips")
    parser.add_argument("--width", type=int, default=1280, help="Initial window width (default: 1280)")
    parser.add_argument("--height", type=int, default=760, help="Initial window height (default: 760)")
    return parser


def main():
    parser = build_chroma_cli_parser()
    args = parser.parse_args()
    app = ApexBOSChromaApp(
        config_path=args.config,
        iq_port=args.chroma_port,
        ctrl_port=args.ctrl_port,
        standalone=args.standalone,
        use_mock=args.mock,
        wideband=args.wideband,
        width=args.width,
        height=args.height,
        band=args.band,
    )
    app.run()


if __name__ == "__main__":
    main()
