#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oscilloscope Video Player / 示波器视频播放器（声卡 L=X, R=Y）

项目名：Oscilloscope Video Player（仓库 oscilloscope-video-layer.py）
英文：Oscilloscope Video Player — play video outlines on an oscilloscope via stereo audio.
中文：示波器视频播放器 — 用声卡把视频轮廓同步画到示波器 XY 模式。

1) 预处理：视频帧 → 二值轮廓 → 写入 <video>_xy.npz
2) 播放：窗口同步原片 + 立体声扫轮廓到示波器（可选另一设备播原曲）

详见仓库根目录 README.md。

示例：
  python oscilloscope_video_player.py --list-audio
  python oscilloscope_video_player.py --video videos/cai.mp4 --speaker 2 --music-speaker 1
  python oscilloscope_video_player.py --video videos/cai.mp4 --preprocess-only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Sequence

import cv2
import numpy as np

try:
    import soundcard as sc
except ImportError:
    sc = None

try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

try:
    import soundfile as sf
except ImportError:
    sf = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Oscilloscope Video Player: preprocess video contours, then play video + XY audio in sync."
    )
    parser.add_argument("--video", type=str, default=None, help="Input video path")
    parser.add_argument(
        "--cache",
        type=str,
        default=None,
        help="Cache .npz path (default: <video>_xy.npz beside the video)",
    )
    parser.add_argument(
        "--preprocess-only",
        action="store_true",
        help="Only build the cache, do not play",
    )
    parser.add_argument(
        "--play-only",
        action="store_true",
        help="Only play an existing cache (still needs --video for on-screen display)",
    )
    parser.add_argument("--list-audio", action="store_true")
    parser.add_argument(
        "--speaker",
        type=int,
        default=None,
        help="Device index for XY scope signal (e.g. 2 = headphone/line out)",
    )
    parser.add_argument(
        "--music-speaker",
        type=int,
        default=1,
        help="Device index for original video music (default: 1 = Realtek speakers)",
    )
    parser.add_argument(
        "--no-music",
        action="store_true",
        help="Do not play the original video soundtrack",
    )
    parser.add_argument(
        "--music-volume",
        type=float,
        default=0.35,
        help="Soundtrack volume 0..1 (keep low to avoid clipping)",
    )
    parser.add_argument("--sample-rate", type=int, default=384000)
    parser.add_argument("--music-rate", type=int, default=48000, help="Music decode sample rate")
    parser.add_argument("--chunk-ms", type=float, default=20.0)
    parser.add_argument("--amplitude", type=float, default=0.95)
    parser.add_argument("--redraw-hz", type=float, default=2000.0)
    parser.add_argument("--max-dim", type=int, default=180)
    parser.add_argument("--threshold", type=int, default=128, help="0 = Otsu")
    parser.add_argument(
        "--foreground",
        choices=("auto", "dark", "light"),
        default="auto",
    )
    parser.add_argument(
        "--bg-filter",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Extract moving foreground for contours (default: on)",
    )
    parser.add_argument(
        "--bg-learn",
        type=float,
        default=0.08,
        help="Background EMA learn rate (higher = forget static scene faster)",
    )
    parser.add_argument(
        "--bg-motion-thresh",
        type=int,
        default=10,
        help="Pixel |frame-bg| / |frame-prev| above this counts as motion",
    )
    parser.add_argument(
        "--bg-warmup",
        type=int,
        default=8,
        help="Frames used to seed background before motion cutout",
    )
    parser.add_argument(
        "--motion-hold",
        type=int,
        default=10,
        help="Keep last moving mask for N frames when motion briefly stops",
    )
    parser.add_argument("--max-contours", type=int, default=8)
    parser.add_argument("--vertices", type=int, default=128)
    parser.add_argument("--min-contour-length", type=int, default=18)
    parser.add_argument(
        "--contour-mode",
        choices=("external", "list"),
        default="external",
        help="external=outer outlines only (fewer cross jumps); list=include holes",
    )
    parser.add_argument(
        "--jump-dim",
        type=float,
        default=0.02,
        help="Fraction of redraw samples allowed on long jumps between contours (lower=dimmer crosses)",
    )
    parser.add_argument("--invert-y", action="store_true", default=True)
    parser.add_argument("--no-invert-y", action="store_false", dest="invert_y")
    parser.add_argument("--swap-lr", action="store_true")
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Playback speed multiplier (1.0 = realtime)",
    )
    return parser.parse_args()


def require_audio() -> None:
    if sc is None:
        raise RuntimeError("Missing soundcard. Run: python -m pip install soundcard")
    if sd is None:
        raise RuntimeError("Missing sounddevice. Run: python -m pip install sounddevice")


def require_music_libs() -> None:
    if imageio_ffmpeg is None:
        raise RuntimeError("Missing imageio-ffmpeg. Run: python -m pip install imageio-ffmpeg")
    if sf is None:
        raise RuntimeError("Missing soundfile. Run: python -m pip install soundfile")


def print_speakers() -> None:
    require_audio()
    default = sc.default_speaker()
    for i, sp in enumerate(sc.all_speakers()):
        mark = " (default)" if sp.name == default.name else ""
        print(f"  [{i}] {sp.name}{mark}")


def select_speaker(index: int | None):
    require_audio()
    speakers = sc.all_speakers()
    if index is None:
        raise ValueError("Pass --speaker N (see --list-audio)")
    if index < 0 or index >= len(speakers):
        raise ValueError(f"--speaker out of range 0..{len(speakers) - 1}")
    return speakers[index]


def find_sd_device(
    name: str,
    *,
    samplerate: int | None = None,
    channels: int = 2,
    prefer_wasapi: bool = True,
) -> int:
    """
    Map a soundcard device name to a sounddevice output index.

    Prefer exact/near-exact matches. If samplerate is given, probe devices that
    can actually open that rate (WASAPI often rejects 384 kHz; MME usually works.
    DirectSound's check_output_settings can lie about high rates).
    """
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()

    def host_name(idx: int) -> str:
        try:
            return str(hostapis[int(devices[idx]["hostapi"])]["name"])
        except Exception:
            return ""

    def supports_rate(idx: int) -> bool:
        if samplerate is None:
            return True
        # check_output_settings is unreliable on some Windows host APIs
        # (DirectSound may claim 384 kHz then fail on open). Probe with a
        # short-lived stream instead.
        try:
            with sd.OutputStream(
                device=idx,
                channels=channels,
                dtype="float32",
                samplerate=samplerate,
                blocksize=256,
            ):
                pass
            return True
        except Exception:
            return False

    candidates: list[tuple[int, int]] = []  # (score, device_index)
    needle = name.strip().lower()
    for idx, dev in enumerate(devices):
        if int(dev.get("max_output_channels", 0)) < channels:
            continue
        n = str(dev.get("name", "")).strip()
        low = n.lower()
        score = 0
        if low == needle:
            score += 100
        elif needle.startswith(low) and len(low) >= 20:
            # MME often truncates names to ~31 chars.
            score += 95
        elif needle in low:
            score += 60
        elif low in needle and len(low) >= 12:
            score += 40
        else:
            tokens = [t for t in needle.replace("(", " ").replace(")", " ").split() if len(t) > 2]
            hits = sum(1 for t in tokens if t in low)
            if hits == 0:
                continue
            score += 10 * hits

        api = host_name(idx).lower()
        if prefer_wasapi and "wasapi" in api:
            score += 15
        elif not prefer_wasapi and samplerate and samplerate >= 96000:
            # High-rate XY: MME is the reliable path on Realtek.
            if "mme" in api:
                score += 80
            elif "wdm-ks" in api:
                score += 10
            elif "directsound" in api:
                score -= 30
            elif "wasapi" in api:
                score -= 40
        elif "wdm-ks" in api:
            score -= 5

        if "2nd" in needle and "2nd" in low:
            score += 25
        if "headphone" in needle and "headphone" in low:
            score += 25
        if "2nd" in needle and "2nd" not in low and "headphone" not in low:
            score -= 20
        if "2nd" not in needle and "2nd" in low:
            score -= 15

        candidates.append((score, idx))

    if not candidates:
        default = sd.default.device
        return default[1] if isinstance(default, (list, tuple)) else int(default)

    candidates.sort(key=lambda item: item[0], reverse=True)

    # Probe rate only on the best name matches (opening streams is slow).
    probed: list[tuple[int, int]] = []
    for score, idx in candidates[:12]:
        if samplerate is not None and not supports_rate(idx):
            probed.append((score - 1000, idx))
        else:
            if samplerate is not None:
                score += 50
            probed.append((score, idx))
            if score >= 100:
                # Good name match that opens at the requested rate — take it.
                return idx

    probed.sort(key=lambda item: item[0], reverse=True)
    best_score, best_idx = probed[0]
    if best_score < -100:
        raise RuntimeError(
            f"No output device supporting {samplerate} Hz for '{name}'. "
            f"Best candidate was {describe_sd_device(best_idx)}"
        )
    if best_score < 40:
        print(
            f"[audio] warning: weak device match for '{name}' -> "
            f"{describe_sd_device(best_idx)}"
        )
    return best_idx


def describe_sd_device(index: int) -> str:
    dev = sd.query_devices(index)
    api = sd.query_hostapis()[int(dev["hostapi"])]["name"]
    return f"#{index} {dev['name']} [{api}]"


def open_output_stream(
    *,
    samplerate: int,
    channels: int,
    device: int,
    blocksize: int,
    callback,
    latency: str | None = None,
):
    """Open OutputStream; if rate invalid, try common fallback rates."""
    rates = [samplerate]
    for alt in (192000, 96000, 48000):
        if alt not in rates:
            rates.append(alt)

    last_error = None
    for rate in rates:
        kwargs = {
            "samplerate": rate,
            "channels": channels,
            "dtype": "float32",
            "device": device,
            "blocksize": blocksize,
            "callback": callback,
        }
        if latency is not None:
            kwargs["latency"] = latency
        try:
            stream = sd.OutputStream(**kwargs)
            if rate != samplerate:
                print(
                    f"[audio] warning: {describe_sd_device(device)} "
                    f"rejected {samplerate} Hz, using {rate} Hz"
                )
            return stream, rate
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(
        f"Cannot open {describe_sd_device(device)} for output: {last_error}"
    )


def resize_gray(gray: np.ndarray, max_dim: int) -> np.ndarray:
    if max_dim <= 0:
        return gray
    h, w = gray.shape[:2]
    longest = max(h, w)
    if longest <= max_dim:
        return gray
    scale = max_dim / longest
    return cv2.resize(
        gray,
        (max(1, int(w * scale)), max(1, int(h * scale))),
        interpolation=cv2.INTER_AREA,
    )


def fit_frame_to_window(frame: np.ndarray, win_name: str) -> np.ndarray:
    """Scale frame to the current window size while preserving aspect ratio (letterbox)."""
    fh, fw = frame.shape[:2]
    if fh <= 0 or fw <= 0:
        return frame
    try:
        _x, _y, ww, wh = cv2.getWindowImageRect(win_name)
    except Exception:
        return frame
    if ww <= 1 or wh <= 1:
        return frame
    scale = min(ww / fw, wh / fh)
    nw = max(1, int(round(fw * scale)))
    nh = max(1, int(round(fh * scale)))
    if nw == fw and nh == fh and ww == fw and wh == fh:
        return frame
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    scaled = cv2.resize(frame, (nw, nh), interpolation=interp)
    if nw == ww and nh == wh:
        return scaled
    canvas = np.zeros((wh, ww, frame.shape[2] if frame.ndim == 3 else 1), dtype=frame.dtype)
    y0 = (wh - nh) // 2
    x0 = (ww - nw) // 2
    canvas[y0 : y0 + nh, x0 : x0 + nw] = scaled
    return canvas


def silhouette_mask(
    gray: np.ndarray,
    threshold: int,
    foreground: str,
) -> np.ndarray:
    if threshold <= 0:
        _v, light = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        _v, light = cv2.threshold(
            gray, int(np.clip(threshold, 1, 255)), 255, cv2.THRESH_BINARY
        )
    dark = cv2.bitwise_not(light)
    if foreground == "light":
        mask = light
    elif foreground == "dark":
        mask = dark
    else:
        light_n = int(cv2.countNonZero(light))
        dark_n = int(cv2.countNonZero(dark))
        mask = light if light_n <= dark_n else dark
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)


class StaticBackgroundFilter:
    """
    Motion-first cutout: learn a static background, then keep only the moving
    part of the silhouette for contour tracing.

    Uses |frame-bg| and |frame-prev| so both long-term static scenery and
    short-term stillness are handled. Brief motion gaps reuse the last good
    mask (--motion-hold) so a paused character does not vanish instantly.
    """

    def __init__(
        self,
        *,
        learn_rate: float = 0.08,
        motion_thresh: int = 10,
        warmup: int = 8,
        hold_frames: int = 10,
    ):
        self.learn_rate = float(np.clip(learn_rate, 0.001, 0.5))
        self.motion_thresh = max(1, int(motion_thresh))
        self.warmup = max(0, int(warmup))
        self.hold_frames = max(0, int(hold_frames))
        self.bg: np.ndarray | None = None
        self.prev: np.ndarray | None = None
        self.last_good: np.ndarray | None = None
        self.hold_left = 0
        self.frames = 0

    def refine(
        self,
        gray: np.ndarray,
        sil_mask: np.ndarray,
        threshold: int,
        foreground: str,
        min_pixels: int,
    ) -> np.ndarray:
        gray_f = gray.astype(np.float32)
        min_pixels = max(8, int(min_pixels))
        k3 = np.ones((3, 3), np.uint8)
        k5 = np.ones((5, 5), np.uint8)

        if self.bg is None or self.bg.shape != gray_f.shape:
            self.bg = gray_f.copy()
            self.prev = gray.copy()
            self.frames = 1
            self.last_good = sil_mask.copy()
            self.hold_left = self.hold_frames
            return sil_mask

        # Learn background faster where the silhouette says "not subject".
        lr = self.learn_rate
        outside = sil_mask == 0
        if np.any(outside):
            self.bg[outside] = (1.0 - lr) * self.bg[outside] + lr * gray_f[outside]
        # Very slow drift on the rest so lighting shifts track without eating the subject.
        drift = lr * 0.05
        self.bg = (1.0 - drift) * self.bg + drift * gray_f
        self.frames += 1

        bg_u8 = np.clip(np.rint(self.bg), 0, 255).astype(np.uint8)
        diff_bg = cv2.absdiff(gray, bg_u8)
        motion = cv2.threshold(
            diff_bg, self.motion_thresh, 255, cv2.THRESH_BINARY
        )[1]

        if self.prev is not None and self.prev.shape == gray.shape:
            diff_prev = cv2.absdiff(gray, self.prev)
            instant = cv2.threshold(
                diff_prev,
                max(1, int(self.motion_thresh * 0.75)),
                255,
                cv2.THRESH_BINARY,
            )[1]
            motion = cv2.bitwise_or(motion, instant)
        self.prev = gray.copy()

        if self.frames <= self.warmup:
            # Seed background; still return silhouette so early frames aren't empty.
            self.last_good = sil_mask.copy()
            self.hold_left = self.hold_frames
            return sil_mask

        motion = cv2.morphologyEx(motion, cv2.MORPH_CLOSE, k3, iterations=2)
        motion = cv2.morphologyEx(motion, cv2.MORPH_OPEN, k3, iterations=1)
        motion = cv2.dilate(motion, k5, iterations=1)

        # Keep silhouette pixels that are moving (or next to motion).
        moving_sil = cv2.bitwise_and(sil_mask, motion)

        # Also drop regions that match the learned background silhouette.
        bg_sil = silhouette_mask(bg_u8, threshold, foreground)
        bg_sil = cv2.dilate(bg_sil, k3, iterations=1)
        without_bg = cv2.bitwise_and(sil_mask, cv2.bitwise_not(bg_sil))
        moving_sil = cv2.bitwise_or(
            moving_sil, cv2.bitwise_and(without_bg, motion)
        )

        moving_sil = cv2.morphologyEx(moving_sil, cv2.MORPH_CLOSE, k5, iterations=1)
        moving_sil = cv2.morphologyEx(moving_sil, cv2.MORPH_OPEN, k3, iterations=1)

        if int(cv2.countNonZero(moving_sil)) >= min_pixels:
            self.last_good = moving_sil.copy()
            self.hold_left = self.hold_frames
            return moving_sil

        # Brief pause: reuse last moving cutout.
        if (
            self.hold_left > 0
            and self.last_good is not None
            and self.last_good.shape == sil_mask.shape
            and int(cv2.countNonZero(self.last_good)) >= min_pixels
        ):
            self.hold_left -= 1
            return self.last_good

        # Last resort: silhouette minus static background (no motion).
        if int(cv2.countNonZero(without_bg)) >= min_pixels:
            return without_bg

        return np.zeros_like(sil_mask)


def selected_contours(
    source: np.ndarray,
    min_len: int,
    max_contours: int,
    mode: str = "external",
) -> List[np.ndarray]:
    retrieval = cv2.RETR_EXTERNAL if mode == "external" else cv2.RETR_LIST
    contours, _ = cv2.findContours(source, retrieval, cv2.CHAIN_APPROX_NONE)
    usable = [c[:, 0, :] for c in contours if len(c) >= min_len]
    h, w = source.shape[:2]
    frame_area = max(1, w * h)
    usable = [
        c
        for c in usable
        if abs(cv2.contourArea(c.astype(np.float32))) < frame_area * 0.98
    ]
    usable.sort(key=len, reverse=True)
    return usable[: max(1, max_contours)]


def remove_duplicate_points(points: np.ndarray, eps: float = 0.5) -> np.ndarray:
    pts = points.reshape(-1, 2).astype(np.float32)
    if len(pts) <= 1:
        return pts
    keep = [pts[0]]
    for p in pts[1:]:
        if float(np.linalg.norm(p - keep[-1])) > eps:
            keep.append(p)
    if len(keep) >= 2 and float(np.linalg.norm(keep[0] - keep[-1])) <= eps:
        keep.pop()
    return np.asarray(keep, dtype=np.float32)


def normalize_contour_loop(contour: np.ndarray) -> np.ndarray:
    """Closed perimeter walk: CCW, start at top-most then left-most point."""
    pts = remove_duplicate_points(contour)
    if len(pts) < 3:
        return pts
    area = float(cv2.contourArea(pts.reshape(-1, 1, 2)))
    if area < 0:
        pts = pts[::-1].copy()
    # Stable start so consecutive frames don't flip the phase of the loop.
    start = int(np.lexsort((pts[:, 0], pts[:, 1]))[0])
    return np.roll(pts, -start, axis=0)


def rotate_contour_nearest(contour: np.ndarray, anchor: np.ndarray) -> np.ndarray:
    """Roll contour so its start is the vertex nearest to anchor."""
    pts = contour.reshape(-1, 2).astype(np.float32)
    if len(pts) == 0:
        return pts
    d = np.linalg.norm(pts - anchor.reshape(1, 2), axis=1)
    return np.roll(pts, -int(np.argmin(d)), axis=0)


def order_contours_for_trace(contours: Sequence[np.ndarray]) -> List[np.ndarray]:
    """
    Greedy chain of closed loops: finish one outline, enter the next at the
    nearest vertex (optionally flipped) to minimize cross-screen jumps.
    """
    if not contours:
        return []
    remaining = [normalize_contour_loop(c) for c in contours if len(c) >= 3]
    if not remaining:
        return []
    remaining.sort(key=len, reverse=True)
    ordered = [remaining.pop(0)]

    while remaining:
        anchor = ordered[-1][-1]
        best_i = 0
        best_cost = float("inf")
        best_pts: np.ndarray | None = None
        for i, cand in enumerate(remaining):
            for flipped in (False, True):
                pts = cand[::-1].copy() if flipped else cand
                # Re-normalize orientation after flip: keep CCW for consistent orbit.
                area = float(cv2.contourArea(pts.reshape(-1, 1, 2)))
                if area < 0:
                    pts = pts[::-1].copy()
                rotated = rotate_contour_nearest(pts, anchor)
                cost = float(np.linalg.norm(rotated[0] - anchor))
                # Prefer not flipping when distances are similar (stable direction).
                if flipped:
                    cost += 0.5
                if cost < best_cost:
                    best_cost = cost
                    best_i = i
                    best_pts = rotated
        assert best_pts is not None
        ordered.append(best_pts)
        remaining.pop(best_i)
    return ordered


def contour_quotas(contours: Sequence[np.ndarray], total_points: int) -> List[int]:
    if not contours:
        return []
    total_points = max(3 * len(contours), total_points)
    lengths = np.asarray([max(1, len(c)) for c in contours], dtype=np.float64)
    raw = lengths / lengths.sum() * total_points
    quotas = np.maximum(3, np.floor(raw).astype(np.int32))
    while int(quotas.sum()) > total_points:
        candidates = np.where(quotas > 3)[0]
        if len(candidates) == 0:
            break
        idx = int(candidates[np.argmax(quotas[candidates] - raw[candidates])])
        quotas[idx] -= 1
    while int(quotas.sum()) < total_points:
        idx = int(np.argmax(raw - quotas))
        quotas[idx] += 1
    return [int(v) for v in quotas]


def resample_contour(contour: np.ndarray, vertices: int) -> np.ndarray:
    """Uniform arc-length samples walking once around the closed outline."""
    vertices = max(3, int(vertices))
    points = contour.reshape(-1, 2).astype(np.float32)
    if len(points) < 2:
        return np.repeat(points[:1], vertices, axis=0) if len(points) else np.zeros((vertices, 2), np.float32)

    closed = np.vstack((points, points[:1]))
    seg = np.linalg.norm(np.diff(closed, axis=0), axis=1)
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    total = float(cum[-1])
    if total <= 1e-6:
        return np.repeat(points[:1], vertices, axis=0)

    targets = np.linspace(0.0, total, vertices, endpoint=False)
    out = np.empty((vertices, 2), dtype=np.float32)
    segment = 0
    for i, distance in enumerate(targets):
        while segment + 1 < len(cum) and cum[segment + 1] <= distance:
            segment += 1
        length = max(float(seg[segment]), 1e-6)
        t = (distance - cum[segment]) / length
        out[i] = closed[segment] * (1.0 - t) + closed[segment + 1] * t
    return out


def poly_to_unit(
    poly: np.ndarray,
    width: int,
    height: int,
    invert_y: bool,
    amp: float,
) -> np.ndarray:
    amp = float(np.clip(amp, 0.05, 1.0))
    out = np.empty_like(poly, dtype=np.float32)
    for i, (x, y) in enumerate(poly):
        xn = (float(x) / max(1, width - 1)) * 2.0 - 1.0
        yn = (float(y) / max(1, height - 1)) * 2.0 - 1.0
        if invert_y:
            yn = -yn
        out[i, 0] = xn * amp
        out[i, 1] = yn * amp
    return out


def path_to_waveform(
    unit_path: np.ndarray,
    sample_rate: int,
    redraw_hz: float,
    jump_dim: float = 0.02,
) -> np.ndarray:
    """
    Interpolate along the path for one redraw period.

    Long segments (jumps between separate outlines) get almost no dwell time
    so cross-screen diagonals stay faint. Outline edges keep the samples →
    the beam spends its time walking the rim.
    """
    if len(unit_path) < 2:
        unit_path = np.array(
            [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5], [-0.5, -0.5]],
            np.float32,
        )
    path = unit_path.astype(np.float32)
    redraw_hz = max(200.0, float(redraw_hz))
    total = max(16, int(round(sample_rate / redraw_hz)))
    seg_lens = np.linalg.norm(np.diff(path, axis=0), axis=1)
    seg_lens = np.maximum(seg_lens, 1e-6)

    med = float(np.median(seg_lens))
    jump_thresh = max(0.18, 3.0 * med)
    is_jump = seg_lens >= jump_thresh
    n_jump = int(np.count_nonzero(is_jump))
    n_draw = len(seg_lens) - n_jump

    jump_dim = float(np.clip(jump_dim, 0.0, 0.25))
    # At most a small fraction of the period on all jumps combined (min 1 samp each).
    jump_budget = n_jump
    if n_jump > 0:
        jump_budget = max(n_jump, min(n_jump * 2, int(round(total * jump_dim))))
    draw_budget = max(n_draw, total - jump_budget)

    counts = np.ones(len(seg_lens), dtype=np.int32)
    if n_draw > 0:
        draw_lens = np.where(is_jump, 0.0, seg_lens)
        weights = draw_lens / max(float(draw_lens.sum()), 1e-9)
        # Everyone on a draw edge gets a base 1; distribute the rest by length.
        base = 1 if draw_budget >= n_draw else 0
        counts = np.where(is_jump, 0, base).astype(np.int32)
        remain = draw_budget - int(counts.sum())
        if remain > 0:
            add = np.floor(weights * remain).astype(np.int32)
            counts += add
            leftover = draw_budget - int(counts.sum())
            order = np.argsort(-draw_lens)
            for idx in order:
                if leftover <= 0:
                    break
                if not is_jump[idx]:
                    counts[idx] += 1
                    leftover -= 1
        # Ensure every draw edge has ≥1 sample when possible.
        if draw_budget >= n_draw:
            counts = np.where((~is_jump) & (counts < 1), 1, counts)

    if n_jump > 0:
        counts[is_jump] = 1
        extra = jump_budget - n_jump
        if extra > 0:
            j_lens = np.where(is_jump, seg_lens, 0.0)
            jw = j_lens / max(float(j_lens.sum()), 1e-9)
            counts += np.floor(jw * extra).astype(np.int32)

    counts = np.maximum(counts, 1)
    while int(counts.sum()) > total:
        # Remove dwell from jumps first, then from longest draw edges.
        candidates = np.where((is_jump) & (counts > 1))[0]
        if len(candidates) == 0:
            candidates = np.where(counts > 1)[0]
        if len(candidates) == 0:
            break
        counts[int(candidates[np.argmax(counts[candidates])])] -= 1
    while int(counts.sum()) < total:
        draw_idxs = np.where(~is_jump)[0]
        if len(draw_idxs):
            counts[int(draw_idxs[np.argmax(seg_lens[draw_idxs])])] += 1
        else:
            counts[int(np.argmax(seg_lens))] += 1

    chunks: List[np.ndarray] = []
    for i, count in enumerate(counts):
        p0 = path[i]
        p1 = path[i + 1]
        ts = np.linspace(0.0, 1.0, int(count), endpoint=False, dtype=np.float32)
        chunks.append((p0 * (1.0 - ts[:, None]) + p1 * ts[:, None]).astype(np.float32))
    return np.ascontiguousarray(np.vstack(chunks), dtype=np.float32)


def frame_to_unit_path(
    bgr: np.ndarray,
    args: argparse.Namespace,
    bg_filter: StaticBackgroundFilter | None = None,
) -> np.ndarray:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = resize_gray(gray, args.max_dim)
    mask = silhouette_mask(gray, args.threshold, args.foreground)
    if bg_filter is not None:
        mask = bg_filter.refine(
            gray,
            mask,
            args.threshold,
            args.foreground,
            min_pixels=max(24, args.min_contour_length * 2),
        )
    h, w = mask.shape[:2]
    contours = selected_contours(
        mask,
        args.min_contour_length,
        args.max_contours,
        mode=getattr(args, "contour_mode", "external"),
    )
    contours = order_contours_for_trace(contours)
    if not contours:
        a = 0.25 * args.amplitude
        return np.array([[-a, 0], [a, 0], [0, 0], [0, -a], [0, a]], np.float32)

    quotas = contour_quotas(contours, args.vertices)
    parts: List[np.ndarray] = []
    for contour, quota in zip(contours, quotas):
        # Walk once around the outline, then close the loop back to start.
        poly = resample_contour(contour, quota)
        unit = poly_to_unit(poly, w, h, args.invert_y, args.amplitude)
        parts.append(np.vstack((unit, unit[:1])))
    return np.ascontiguousarray(np.vstack(parts), dtype=np.float32)


def default_cache_path(video: Path) -> Path:
    return video.with_name(video.stem + "_xy.npz")


def default_music_path(video: Path) -> Path:
    return video.with_name(video.stem + "_audio.wav")


def extract_music_wav(video_path: Path, wav_path: Path, sample_rate: int) -> Path:
    """Extract stereo soundtrack with bundled ffmpeg."""
    require_music_libs()
    if wav_path.exists() and wav_path.stat().st_mtime >= video_path.stat().st_mtime:
        print(f"[music] using existing {wav_path}")
        return wav_path

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "2",
        "-ar",
        str(int(sample_rate)),
        "-f",
        "wav",
        str(wav_path),
    ]
    print(f"[music] extracting soundtrack -> {wav_path.name}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not wav_path.exists():
        err = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"ffmpeg failed to extract audio:\n{err[-1000:]}")
    print(f"[music] saved {wav_path} ({wav_path.stat().st_size / 1024:.1f} KB)")
    return wav_path


def load_music(wav_path: Path, volume: float) -> tuple[np.ndarray, int]:
    require_music_libs()
    data, rate = sf.read(str(wav_path), always_2d=True, dtype="float32")
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]

    # Peak-normalize first, then apply user volume. Prevents "爆炸失真".
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    if peak > 1e-6:
        data = data / peak
    volume = float(np.clip(volume, 0.0, 1.0))
    data = np.clip(data * volume, -0.98, 0.98)
    print(f"[music] peak_in={peak:.3f} volume={volume:.2f} rate={rate}")
    return np.ascontiguousarray(data, dtype=np.float32), int(rate)


class MusicPlayer:
    """Sequential soundtrack playback (sample-accurate, no wall-clock jitter)."""

    def __init__(self, audio: np.ndarray, sample_rate: int, speed: float):
        self.audio = audio
        self.sample_rate = sample_rate
        self.speed = max(0.05, float(speed))
        self.pos = 0.0
        self.started = False
        self.channels = audio.shape[1]

    def arm(self) -> None:
        self.pos = 0.0
        self.started = True

    def callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001
        outdata.fill(0.0)
        if not self.started:
            return

        # Advance by speed-scaled frames using fractional position.
        start = int(self.pos)
        if start >= len(self.audio):
            return
        take = min(frames, len(self.audio) - start)
        if take > 0:
            outdata[:take, : self.channels] = self.audio[start : start + take]
        self.pos += frames * self.speed



def preprocess_video(args: argparse.Namespace, cache_path: Path) -> dict:
    if not args.video:
        raise ValueError("--video is required for preprocessing")
    video_path = Path(args.video)
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    bg_filter = None
    if args.bg_filter:
        bg_filter = StaticBackgroundFilter(
            learn_rate=args.bg_learn,
            motion_thresh=args.bg_motion_thresh,
            warmup=args.bg_warmup,
            hold_frames=args.motion_hold,
        )
        print(
            f"[prep] motion cutout on (learn={args.bg_learn:g}, "
            f"motion>={args.bg_motion_thresh}, warmup={args.bg_warmup}, "
            f"hold={args.motion_hold})"
        )
    else:
        print("[prep] bg-filter off")
    print(
        f"[prep] video={video_path.name} fps={fps:.3f} frames≈{total} "
        f"points={args.vertices} contours<={args.max_contours}"
    )

    paths: List[np.ndarray] = []
    started = time.perf_counter()
    index = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        paths.append(frame_to_unit_path(frame, args, bg_filter))
        index += 1
        if index % 60 == 0 or (total > 0 and index == total):
            elapsed = time.perf_counter() - started
            rate = index / max(elapsed, 1e-6)
            remain = (total - index) / max(rate, 1e-6) if total > 0 else 0.0
            print(
                f"[prep] {index}/{total or '?'}  {rate:5.1f} fps  "
                f"eta={remain:5.1f}s  last_points={len(paths[-1])}"
            )
    cap.release()

    if not paths:
        raise RuntimeError("No frames read from video")

    # Pack variable-length polylines into one array.
    counts = np.asarray([len(p) for p in paths], dtype=np.int32)
    offsets = np.zeros(len(counts) + 1, dtype=np.int64)
    offsets[1:] = np.cumsum(counts)
    packed = np.vstack(paths).astype(np.float32)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        cache_path,
        video=str(video_path.resolve()),
        fps=np.float64(fps),
        sample_rate=np.int32(args.sample_rate),
        redraw_hz=np.float64(args.redraw_hz),
        amplitude=np.float64(args.amplitude),
        counts=counts,
        offsets=offsets,
        points=packed,
        invert_y=np.bool_(args.invert_y),
        max_contours=np.int32(args.max_contours),
        vertices=np.int32(args.vertices),
        threshold=np.int32(args.threshold),
        foreground=np.asarray(args.foreground),
    )
    print(
        f"[prep] saved {cache_path}  frames={len(counts)}  "
        f"points={len(packed)}  size={cache_path.stat().st_size / 1024:.1f} KB"
    )
    return {
        "fps": fps,
        "counts": counts,
        "offsets": offsets,
        "points": packed,
        "sample_rate": args.sample_rate,
        "redraw_hz": args.redraw_hz,
    }


def load_cache(cache_path: Path) -> dict:
    data = np.load(cache_path, allow_pickle=False)
    return {
        "video": str(data["video"]) if "video" in data.files else None,
        "fps": float(data["fps"]),
        "sample_rate": int(data["sample_rate"]),
        "redraw_hz": float(data["redraw_hz"]),
        "amplitude": float(data["amplitude"]),
        "counts": data["counts"],
        "offsets": data["offsets"],
        "points": data["points"],
    }


def frame_path(cache: dict, index: int) -> np.ndarray:
    start = int(cache["offsets"][index])
    end = int(cache["offsets"][index + 1])
    return cache["points"][start:end]


class FramePlayer:
    """Audio callback loops the current frame wave; swaps only at loop boundary."""

    def __init__(
        self,
        sample_rate: int,
        redraw_hz: float,
        swap_lr: bool,
        jump_dim: float = 0.02,
    ):
        self.sample_rate = sample_rate
        self.redraw_hz = redraw_hz
        self.swap_lr = swap_lr
        self.jump_dim = jump_dim
        idle = np.zeros((64, 2), np.float32)
        self.wave = idle
        self.pending = idle
        self.generation = 0
        self.current_gen = 0
        self.pos = 0
        self.loops = 0
        self.lock = threading.Lock()

    def set_frame_path(self, unit_path: np.ndarray) -> None:
        wave = path_to_waveform(
            unit_path, self.sample_rate, self.redraw_hz, jump_dim=self.jump_dim
        )
        with self.lock:
            self.pending = wave
            self.generation += 1

    def _adopt(self) -> None:
        with self.lock:
            if self.generation != self.current_gen:
                self.wave = self.pending
                self.current_gen = self.generation
                self.pos = 0

    def callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001
        if self.pos == 0:
            self._adopt()
        wave = self.wave
        n = len(wave)
        if n <= 0:
            outdata.fill(0)
            return
        pos = self.pos % n
        filled = 0
        while filled < frames:
            take = min(frames - filled, n - pos)
            block = wave[pos : pos + take]
            if self.swap_lr:
                outdata[filled : filled + take, 0] = block[:, 1]
                outdata[filled : filled + take, 1] = block[:, 0]
            else:
                outdata[filled : filled + take] = block
            filled += take
            pos += take
            if pos >= n:
                pos = 0
                self.loops += 1
                self._adopt()
                wave = self.wave
                n = len(wave)
                if n <= 0:
                    outdata[filled:].fill(0)
                    self.pos = 0
                    return
        self.pos = pos


def play_synced(args: argparse.Namespace, cache: dict, video_path: Path) -> int:
    require_audio()
    xy_speaker = select_speaker(args.speaker)
    sample_rate = int(cache["sample_rate"])
    redraw_hz = float(cache["redraw_hz"])
    fps = float(cache["fps"])
    n_frames = len(cache["counts"])
    frame_dt = (1.0 / max(fps, 1e-3)) / max(args.speed, 0.05)

    xy_device = find_sd_device(
        xy_speaker.name,
        samplerate=sample_rate,
        channels=2,
        prefer_wasapi=False,
    )

    music_player = None
    music_device = None
    music_rate = int(args.music_rate)
    if not args.no_music:
        wav_path = default_music_path(video_path)
        extract_music_wav(video_path, wav_path, music_rate)
        music_audio, music_rate = load_music(wav_path, args.music_volume)
        music_speaker = select_speaker(args.music_speaker)
        music_device = find_sd_device(
            music_speaker.name,
            samplerate=music_rate,
            channels=2,
            prefer_wasapi=True,
        )
        if music_device == xy_device:
            raise RuntimeError(
                "Music and XY mapped to the SAME output device:\n"
                f"  music: {describe_sd_device(music_device)}\n"
                f"  xy:    {describe_sd_device(xy_device)}\n"
                "That mixes scope noise into the song and sounds like clipping.\n"
                "Pick different --music-speaker / --speaker (see --list-audio)."
            )
        music_player = MusicPlayer(music_audio, music_rate, args.speed)
        print(f"[play] music -> [{args.music_speaker}] {music_speaker.name}")
        print(f"[play] music device {describe_sd_device(music_device)}")

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video for display: {video_path}")

    player = FramePlayer(
        sample_rate, redraw_hz, args.swap_lr, jump_dim=float(args.jump_dim)
    )
    player.set_frame_path(frame_path(cache, 0))

    print(f"[play] XY/scope -> [{args.speaker}] {xy_speaker.name}")
    print(f"[play] XY device {describe_sd_device(xy_device)}")
    print(
        f"[play] frames={n_frames} video_fps={fps:.3f} "
        f"speed={args.speed:g} redraw≈{redraw_hz:g}Hz request_rate={sample_rate}"
    )
    print("[play] Scope: XY, L→CH1, R→CH2. Window focus + press q to quit.")

    stop = False
    music_stream = None
    xy_stream = None
    try:
        if music_player is not None:
            music_chunk = max(256, int(music_rate * max(5.0, args.chunk_ms) / 1000.0))
            music_stream, music_rate_opened = open_output_stream(
                samplerate=music_rate,
                channels=2,
                device=music_device,
                blocksize=music_chunk,
                callback=music_player.callback,
            )
            music_player.sample_rate = music_rate_opened
            music_stream.start()

        xy_chunk = max(256, int(sample_rate * max(5.0, args.chunk_ms) / 1000.0))
        xy_stream, xy_rate = open_output_stream(
            samplerate=sample_rate,
            channels=2,
            device=xy_device,
            blocksize=xy_chunk,
            callback=player.callback,
            latency="low",
        )
        if xy_rate != player.sample_rate:
            player.sample_rate = xy_rate
            player.set_frame_path(frame_path(cache, 0))
            print(f"[play] XY running at {xy_rate} Hz")
        xy_stream.start()

        if music_player is not None:
            music_player.arm()
        started = time.perf_counter()

        win_name = "Oscilloscope Video Player"
        # WINDOW_NORMAL = user can resize; KEEPRATIO keeps window aspect when dragging.
        cv2.namedWindow(win_name, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 360
        # Comfortable default size, capped so it fits typical screens.
        init_scale = min(1.0, 960 / max(src_w, 1), 720 / max(src_h, 1))
        cv2.resizeWindow(win_name, max(160, int(src_w * init_scale)), max(90, int(src_h * init_scale)))

        for index in range(n_frames):
            if stop:
                break
            target = started + (index + 1) * frame_dt

            ok, frame = cap.read()
            if not ok:
                frame = np.zeros((src_h, src_w, 3), np.uint8)

            player.set_frame_path(frame_path(cache, index))
            cv2.imshow(win_name, fit_frame_to_window(frame, win_name))
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                stop = True
                break

            remaining = target - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)

            if index % max(1, int(fps)) == 0:
                elapsed = time.perf_counter() - started
                print(
                    f"[play] {index + 1}/{n_frames}  "
                    f"t={elapsed:6.1f}s  loops={player.loops}"
                )
                player.loops = 0
    finally:
        if xy_stream is not None:
            xy_stream.stop()
            xy_stream.close()
        if music_stream is not None:
            music_stream.stop()
            music_stream.close()
        cap.release()
        cv2.destroyAllWindows()

    print("[play] done.")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.list_audio:
        print_speakers()
        return 0

    if args.preprocess_only and args.play_only:
        raise ValueError("Use only one of --preprocess-only / --play-only")

    args.amplitude = float(np.clip(args.amplitude, 0.05, 1.0))
    args.vertices = max(8, min(256, int(args.vertices)))
    args.max_contours = max(1, min(24, int(args.max_contours)))
    args.sample_rate = max(8000, int(args.sample_rate))
    args.redraw_hz = max(200.0, float(args.redraw_hz))
    args.bg_learn = float(np.clip(args.bg_learn, 0.001, 0.5))
    args.bg_motion_thresh = max(1, min(80, int(args.bg_motion_thresh)))
    args.bg_warmup = max(0, min(120, int(args.bg_warmup)))
    args.motion_hold = max(0, min(90, int(args.motion_hold)))
    args.jump_dim = float(np.clip(args.jump_dim, 0.0, 0.25))

    video_path = Path(args.video) if args.video else None
    if args.cache:
        cache_path = Path(args.cache)
    elif video_path is not None:
        cache_path = default_cache_path(video_path)
    else:
        raise ValueError("Provide --video and/or --cache")

    need_preprocess = not args.play_only
    if args.play_only and not cache_path.exists():
        raise FileNotFoundError(f"Cache not found: {cache_path}")
    if need_preprocess and cache_path.exists() and not args.preprocess_only:
        # Reuse cache if present unless user forces preprocess-only rebuild.
        # For normal run: if cache exists, skip preprocess.
        need_preprocess = False
        print(f"[cache] using existing {cache_path}")

    if args.preprocess_only:
        need_preprocess = True

    if need_preprocess:
        if video_path is None:
            raise ValueError("--video is required to preprocess")
        preprocess_video(args, cache_path)
        # Also extract soundtrack once during preprocess.
        if not args.no_music:
            try:
                extract_music_wav(video_path, default_music_path(video_path), args.music_rate)
            except Exception as exc:
                print(f"[music] warning: could not extract audio yet: {exc}")
        if args.preprocess_only:
            return 0

    cache = load_cache(cache_path)
    if video_path is None:
        if cache.get("video"):
            video_path = Path(cache["video"])
        else:
            raise ValueError("--video is required for on-screen playback")
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    if args.speaker is None:
        raise ValueError("Pass --speaker N for playback (see --list-audio)")

    # Prefer cache timing params; allow CLI overrides for audio device path.
    if args.sample_rate != int(cache["sample_rate"]) or args.redraw_hz != float(cache["redraw_hz"]):
        print(
            "[play] note: using cache sample_rate/redraw_hz from preprocess; "
            "re-run preprocess to change them."
        )

    return play_synced(args, cache, video_path)


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n[ovp] stopped.")
        return 0
    except Exception as exc:
        print(f"[ovp] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
