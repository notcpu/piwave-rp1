#!/usr/bin/env python3
"""
GPCLK0 FM transmit with software MASH noise-shaping of the divider word.

Why this exists:
  The RP1 GPCLK0 divider is a plain Q16.16 fractional divider with no hardware
  MASH/noise shaper. rp1audiotx.py rounds the desired divider word to the
  register's 1-LSB grid; that error is tiny in Hz but it is *deterministic* and
  correlated with the audio, so it dresses up as low-frequency firmware tones
  riding the signal ("grainy"). This script quantizes the same desired word
  through a software MASH error-feedback modulator instead, which noise-shapes
  that error up out of the audio band.

  Numbers from the built-in selftest (de-emphasis-weighted 0-15 kHz error,
  relative to plain rounding, for a music-like multitone):
      @ 88.2 kHz update: order 1: -19 dB | order 2: -23 dB | order 3: -25 dB
      @ 44.1 kHz update: order 1 is best (-11 dB); order 3 only -6 dB, because
      there is less bandwidth above 15 kHz to push the noise into.
  So the defaults here are --rate 88200 with auto order (3 at >=88.2 kHz,
  else 1). One word-LSB is ~722 Hz of carrier deviation at 97.3 MHz, so the
  shaped error is at most a few kHz of noise, far inside the deviation budget.

Run:
  sudo python3 tx/rp1mashdithertx.py song.wav --freq 97.3        # order picked
  sudo python3 tx/rp1mashdithertx.py song.wav --order 0          # A/B baseline
  sudo python3 tx/rp1mashdithertx.py song.wav --order 3 --tpdf   # TPDF variant (selftest: usually worse)
  python3 tx/rp1mashdithertx.py --selftest   # no root, no hardware, pure math

A/B procedure: transmit the same file twice, changing only --order (0 vs 3)
and/or --tpdf. Compare on an SDR (watch the deterministic divider spurs turn
into a noise hash) and by ear on the radio. The modulator only ever moves the
word a few LSBs around the exact target, so nothing here can push the clock
anywhere the frequency clamp below doesn't already allow - and the clamp
count is printed (it should be 0). 44.1 kHz always stays available
(--rate 44100 --order 1 on a busy Pi) at a smaller but still real benefit.

Same prereqs/legal notes as rp1audiotx.py; no pre-emphasis, so radios still
sound dull on treble - that is a separate known limitation.
"""
import argparse, gc, mmap, os, struct, sys, time, wave
import numpy as np

PCI_ADDR = "0002:01:00.0"
RESOURCE = f"/sys/bus/pci/devices/{PCI_ADDR}/resource1"
CLOCKS_OFFSET_IN_BAR = 0x40018000 - 0x40000000

GPCLK_OE_CTRL    = 0x000
CLK_GP0_CTRL     = 0x174
CLK_GP0_DIV_INT  = 0x178
CLK_GP0_DIV_FRAC = 0x17c

CLK_CTRL_ENABLE = 1 << 11
AUXSRC_MASK, AUXSRC_SHIFT = 0x3E0, 5
GP0_OE = 1 << 0
PLL_SYS_IDX = 6
PLL_SYS_HZ = 200_000_000

RT_THROTTLE_PATHS = ("/proc/sys/kernel/sched_rt_runtime_us",
                     "/proc/sys/kernel/rt_runtime_us")

# safety clamps on the carrier frequency used for divider math (Hz)
FREQ_MIN_HZ, FREQ_MAX_HZ = 4e6, 199e6
CHUNK = 1 << 20
Q32 = 1 << 32
Q32F = float(Q32)


def load_wav(path):
    with wave.open(path, "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        sys.exit("Need 16-bit PCM WAV. Convert with:\n"
                 "  ffmpeg -i in.mp3 -ac 1 -ar 44100 -c:a pcm_s16le out.wav")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


class GPCLK0:
    def __init__(self):
        self.fd = os.open(RESOURCE, os.O_RDWR | os.O_SYNC)
        self.m = mmap.mmap(self.fd, mmap.PAGESIZE, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE,
                           offset=CLOCKS_OFFSET_IN_BAR)

    def r(self, off):
        return struct.unpack_from("<I", self.m, off)[0]

    def w(self, off, val):
        struct.pack_into("<I", self.m, off, val)

    def enable(self):
        c = self.r(CLK_GP0_CTRL)
        c &= ~AUXSRC_MASK
        c |= (PLL_SYS_IDX << AUXSRC_SHIFT) & AUXSRC_MASK
        c |= CLK_CTRL_ENABLE
        self.w(CLK_GP0_CTRL, c)
        self.w(GPCLK_OE_CTRL, self.r(GPCLK_OE_CTRL) | GP0_OE)

    def disable(self):
        self.w(GPCLK_OE_CTRL, self.r(GPCLK_OE_CTRL) & ~GP0_OE)
        self.w(CLK_GP0_CTRL, self.r(CLK_GP0_CTRL) & ~CLK_CTRL_ENABLE)

    def close(self):
        self.m.close()
        os.close(self.fd)


def go_realtime(core):
    try:
        os.sched_setaffinity(0, {core})
    except Exception as e:
        print(f"[warn] couldn't pin to core {core}: {e}")
    rt_path, orig = None, None
    for path in RT_THROTTLE_PATHS:
        try:
            with open(path) as f:
                orig = f.read().strip()
            with open(path, "w") as f:
                f.write("-1")
            rt_path = path
            break
        except Exception as e:
            print(f"[warn] couldn't disable RT throttling via {path} "
                  f"(expect dropouts): {e}")
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(80))
    except Exception as e:
        print(f"[warn] couldn't set SCHED_FIFO: {e}")
    gc.collect()
    gc.disable()
    return rt_path, orig


def restore_realtime(rt_path, orig):
    gc.enable()
    if rt_path is not None and orig is not None:
        try:
            with open(rt_path, "w") as f:
                f.write(orig)
        except Exception:
            pass


# -------------------------------------------------- divider word noise shaping --

class MashState:
    """Accumulator carry-in state, carried across mash_quantize() calls.

    Residues are kept in [0, 2^32); the carry history (c2, c3a, c3b) is what
    the first samples of the next chunk need for their delta terms.
    """

    __slots__ = ("s", "e1", "e2", "c2", "c3a", "c3b")

    def __init__(self):
        self.s = 0
        self.e1 = 0
        self.e2 = 0
        self.c2 = 0
        self.c3a = 0
        self.c3b = 0


def mash_quantize(dw, order, tpdf=False, rng=None, st=None):
    """Quantize desired Q16.16 divider words `dw` (float64) to integers.

    order 0: plain round-to-nearest (baseline; no shaping)
    order 1: floor + stage-1 carry        - (1-z^-1)   shaping
    order 2: MASH 1-1                     - (1-z^-1)^2 shaping
    order 3: MASH 1-1-1                   - (1-z^-1)^3 shaping

    `st` carries accumulator state between chunks (fresh if None), so the
    result is identical to running the whole sequence in one call. Exact
    integer math: the fraction lives in Q32, carries are floor divisions, and
    with `tpdf` an optional +-half-LSB triangular dither is added to stage 1
    (same rng seed = same dither; the shaped error stays bounded by ~2^order
    word-LSBs either way).
    """
    dw = np.asarray(dw, dtype=np.float64)
    if dw.size == 0:
        return np.rint(dw).astype(np.int64)
    if order == 0:
        return np.rint(dw).astype(np.int64)
    if st is None:
        st = MashState()

    Di = np.floor(dw).astype(np.int64)
    x = np.rint((dw - Di) * Q32F).astype(np.int64)
    np.clip(x, 0, Q32 - 1, out=x)
    if tpdf:
        if rng is None:
            rng = np.random.default_rng(1)
        x += (rng.integers(0, 1 << 31, x.size, dtype=np.int64)
              + rng.integers(0, 1 << 31, x.size, dtype=np.int64) - (1 << 31))

    # stage 1 carries
    S = np.cumsum(x)
    S += st.s
    c1 = np.empty_like(S)
    c1[0] = S[0] >> 32
    c1[1:] = (S[1:] >> 32) - (S[:-1] >> 32)
    out = c1

    if order >= 2:
        # stage 2 carries (input = stage-1 residue)
        E1 = np.cumsum(S & 0xFFFFFFFF)
        E1 += st.e1
        c2 = np.empty_like(E1)
        c2[0] = E1[0] >> 32
        c2[1:] = (E1[1:] >> 32) - (E1[:-1] >> 32)
        c2prev = np.roll(c2, 1)
        c2prev[0] = st.c2
        out = out + c2 - c2prev

    if order >= 3:
        # stage 3 carries (input = stage-2 residue)
        E2 = np.cumsum(E1 & 0xFFFFFFFF)
        E2 += st.e2
        c3 = np.empty_like(E2)
        c3[0] = E2[0] >> 32
        c3[1:] = (E2[1:] >> 32) - (E2[:-1] >> 32)
        c3p1 = np.roll(c3, 1)
        c3p1[0] = st.c3a
        c3p2 = np.roll(c3, 2)
        c3p2[0] = st.c3b
        if c3p2.size > 1:
            c3p2[1] = st.c3a
        out = out + c3 - 2 * c3p1 + c3p2

    # carry state out to the next chunk
    st.s = int(S[-1] & 0xFFFFFFFF)
    if order >= 2:
        st.e1 = int(E1[-1] & 0xFFFFFFFF)
        st.c2 = int(c2[-1])
    if order >= 3:
        st.e2 = int(E2[-1] & 0xFFFFFFFF)
        prev_c3a = st.c3a
        st.c3a = int(c3[-1])
        st.c3b = int(c3[-2]) if c3.size > 1 else prev_c3a

    return Di + out


def build_table(x, in_rate, args):
    """Precompute per-sample divider words (di, df) for the whole file."""
    n = int(round(len(x) * args.rate / in_rate))
    n = max(1, n)
    base = args.freq * 1e6
    dev = args.dev * 1e3
    c = PLL_SYS_HZ * 65536.0

    scale = 0.95 / max(1e-9, float(np.max(np.abs(x)))) if len(x) else 0.0

    di = np.empty(n, dtype=np.uint32)
    df = np.empty(n, dtype=np.uint32)
    clamped = 0
    st = MashState()
    rng = np.random.default_rng(1)
    src_idx = np.arange(len(x), dtype=np.float64)

    for s in range(0, n, CHUNK):
        e = min(n, s + CHUNK)
        aud = np.interp(np.arange(s, e) / args.rate * in_rate, src_idx, x)
        freq = base + dev * scale * aud
        fcl = np.clip(freq, FREQ_MIN_HZ, FREQ_MAX_HZ)
        clamped += int(np.count_nonzero(fcl != freq))
        words = mash_quantize(c / fcl, args.order, args.tpdf, rng, st)
        di[s:e] = (words >> 16).astype(np.uint32)
        df[s:e] = ((words & 0xFFFF) << 16).astype(np.uint32)

    di_first = int(di[0])
    const_int = bool(np.all(di == di[0]))
    if const_int:
        di = None
    return di, df, const_int, clamped, di_first


# ---------------------------------------------------------------- selftest --

def selftest(args):
    """Spectral check of the shaper - pure math, no root, no hardware."""
    FS = args.rate
    n = int(FS * 12)
    t = np.arange(n) / FS
    rng = np.random.default_rng(1)
    sig = sum(0.25 * np.sin(2 * np.pi * f0 * t + rng.uniform(0, 7))
              for f0 in (300, 1000, 3000, 7000, 12000))
    freq = args.freq * 1e6 + args.dev * 1e3 * sig
    dw = (PLL_SYS_HZ * 65536.0) / freq
    hz_per_word = (args.freq * 1e6) ** 2 / (PLL_SYS_HZ * 65536.0)

    win = np.hanning(n)
    fq = np.fft.rfftfreq(n, 1 / FS)
    deemph = 1.0 / np.sqrt(1 + (2 * np.pi * 50e-6 * fq) ** 2)

    print(f"selftest @ {FS/1000:.1f} kHz, {n} samples, carrier {args.freq} MHz")
    print(f"1 word-LSB = {hz_per_word:.0f} Hz at this carrier")
    print(f"{'order':>6} {'max|e|w':>8} {'rms Hz':>8} | "
          f"octave-band error power, dB rel. order-0 total | deemph 0-15k")
    print(f"{'':>6} {'':>8} {'':>8} | {'<3.7k':>7} {'3.7-7.4k':>8} "
          f"{'7.4-11k':>8} {'11-15k':>8} {'15k+':>7} | dB vs ord0")
    base_total = None
    for order, tpdf in ((0, False), (1, False), (2, False), (3, False), (3, True)):
        q = mash_quantize(dw, order, tpdf,
                          rng=np.random.default_rng(1) if tpdf else None)
        err = q - dw
        E = np.abs(np.fft.rfft(err * win)) ** 2
        if base_total is None:
            base_total = E.sum()
        bands = [E[(fq > a) & (fq < b)].sum() for a, b in
                 ((0, 3700), (3700, 7400), (7400, 11000), (11000, 15000),
                  (15000, FS / 2))]
        band15 = (E[(fq > 0) & (fq < 15000)]
                  * deemph[(fq > 0) & (fq < 15000)] ** 2).sum()
        band_db = " ".join(f"{10*np.log10(max(b, 1e-30)/base_total):>7.1f}"
                           for b in bands)
        label = f"{order}" + ("+tpdf" if tpdf else "")
        print(f"{label:>6} {np.abs(err).max():>8.1f} "
              f"{np.sqrt(np.mean(err**2))*hz_per_word:>8.1f} | {band_db} | "
              f"{10*np.log10(band15/base_total):>11.1f}")
    print("\nNegative dB = less error power than plain rounding in that band.")
    print("The win is largest in <7.4 kHz; >=11 kHz power is *pushed up* on")
    print("purpose (a radio's 15 kHz filter/de-emphasis never hears it).")


# ------------------------------------------------------------------- main --

def parse_args():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("wav", nargs="?", help="16-bit PCM WAV; omit for a 1 kHz tone")
    ap.add_argument("--freq", type=float, default=97.3, help="carrier MHz")
    ap.add_argument("--dev", type=float, default=75.0, help="peak deviation kHz")
    ap.add_argument("--rate", type=int, default=88200,
                    help="update rate Hz (88200 measured best; 44100 also works)")
    ap.add_argument("--order", type=int, choices=(0, 1, 2, 3), default=None,
                    help="MASH order; default auto: 3 when rate>=88.2k, else 1")
    ap.add_argument("--tpdf", action="store_true",
                    help="add +-half-word TPDF dither to stage 1; measured at "
                         "88.2k: its flat noise outweighs the shaping win, so "
                         "usually leave off (kept for experiments)")
    ap.add_argument("--core", type=int, default=3, help="CPU core to pin to")
    ap.add_argument("--seconds", type=float, default=10.0, help="tone duration (no WAV)")
    ap.add_argument("--max-seconds", type=float, default=600.0,
                    help="truncate long audio (memory guard; 0 = no limit)")
    ap.add_argument("--selftest", action="store_true",
                    help="run the spectral shaper check and exit (no root needed)")
    return ap.parse_args()


def main():
    args = parse_args()
    if args.order is None:
        args.order = 3 if args.rate >= 88200 else 1
    if args.selftest:
        selftest(args)
        return

    if os.geteuid() != 0:
        sys.exit("Run with sudo.")

    if args.wav:
        x, in_rate = load_wav(args.wav)
        if args.max_seconds and len(x) / in_rate > args.max_seconds:
            keep = int(args.max_seconds * in_rate)
            print(f"[info] truncating audio to {args.max_seconds:.0f} s "
                  f"({len(x)} -> {keep} input samples)")
            x = np.ascontiguousarray(x[:keep])
        dur = len(x) / in_rate
        src = args.wav
    else:
        in_rate = args.rate
        t = np.arange(int(args.seconds * in_rate)) / in_rate
        x = 0.8 * np.sin(2 * np.pi * 1000.0 * t)
        dur = args.seconds
        src = f"{args.seconds:.0f}s 1 kHz tone"

    hz_per_word = (args.freq * 1e6) ** 2 / (PLL_SYS_HZ * 65536.0)
    print(f"audio: {src}, {dur:.1f}s | carrier {args.freq} MHz, "
          f"dev {args.dev} kHz | 1 word LSB ~ {hz_per_word:.0f} Hz")
    print(f"shaper: MASH order {args.order}"
          + (" + TPDF dither" if args.tpdf else "")
          + f" @ {args.rate} Hz update (budget {1e6/args.rate:.1f}us/sample)")
    n_est = int(round(dur * args.rate))
    print(f"allocating divider tables ({n_est} samples, ~{n_est * 8 / 1e6:.0f} MB "
          f"worst case) and quantizing...")
    t0 = time.time()
    di, df, const_int, clamped, di_first = build_table(x, in_rate, args)
    n = len(df)
    print(f"table built in {time.time() - t0:.1f}s, {n} samples @ {args.rate} Hz "
          f"= {n / args.rate:.1f}s")
    if clamped:
        print(f"[warn] {clamped} samples hit the frequency safety clamp "
              f"[{FREQ_MIN_HZ/1e6:.0f}-{FREQ_MAX_HZ/1e6:.0f} MHz] (should be 0)")
    print("DIV_INT constant -> fast single-write path" if const_int
          else "DIV_INT changes in this range -> slower dual-write path")

    mv_f = memoryview(df)
    mv_i = memoryview(di) if not const_int else None
    ns = 1e9 / args.rate
    interval = int(ns)
    pk = struct.Struct("<I").pack_into
    pc = time.perf_counter_ns

    clk = GPCLK0()
    rt_path, orig_throttle = go_realtime(args.core)
    m = clk.m
    missed = 0
    worst = 0
    try:
        clk.w(CLK_GP0_DIV_INT, di_first)
        clk.w(CLK_GP0_DIV_FRAC, int(df[0]))
        clk.enable()
        time.sleep(0.05)
        t0 = pc() + 100_000_000

        if const_int:
            for i in range(n):
                t = t0 + int(i * ns)
                now = pc()
                while now < t:
                    now = pc()
                late = now - t
                if late > worst:
                    worst = late
                if late > interval:
                    missed += 1
                pk(m, CLK_GP0_DIV_FRAC, mv_f[i])
        else:
            last_i = di_first
            for i in range(n):
                t = t0 + int(i * ns)
                now = pc()
                while now < t:
                    now = pc()
                late = now - t
                if late > worst:
                    worst = late
                if late > interval:
                    missed += 1
                vi = mv_i[i]
                if vi != last_i:
                    pk(m, CLK_GP0_DIV_INT, vi)
                    last_i = vi
                pk(m, CLK_GP0_DIV_FRAC, mv_f[i])
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        clk.disable()
        clk.close()
        restore_realtime(rt_path, orig_throttle)

    print(f"Done. missed deadlines: {missed}/{n} ({100.0 * missed / max(1, n):.3f}%), "
          f"worst lateness: {worst / 1000:.1f}us (budget {interval / 1000:.1f}us/sample)")
    if missed > n * 0.01:
        print("Lots of misses -> fall back to --rate 44100 --order 1.")
    print("Clock disabled.")


if __name__ == "__main__":
    main()
