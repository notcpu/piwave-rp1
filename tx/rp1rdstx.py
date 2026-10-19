#!/usr/bin/env python3
"""
GPCLK0 FM transmit with RDS: mono audio + 57 kHz RDS subcarrier on GPIO4.

Prereqs:
  sudo apt install python3-numpy
  sudo pinctrl set 4 a0
  ffmpeg -i song.mp3 -ac 1 -ar 44100 -c:a pcm_s16le song.wav

Run:
  sudo python3 tx/rp1rdstx.py song.wav --freq 97.3 --pi 1234 --ps "PIWAVE  " --rt "hello from the RP1"
  sudo python3 tx/rp1rdstx.py                      # no WAV -> built-in 1 kHz tone, 10 s

How it works:
  Same GPCLK0 divider technique as rp1audiotx.py, but the update rate defaults to
  228 kHz so the MPX can carry the RDS subcarrier (4 samples per 57 kHz cycle:
  the carrier is exactly [0, +1, 0, -1]). MPX per sample (all in Hz):
      freq = carrier + audio_dev * audio + rds_dev * envelope * subcarrier
  RDS chain, every convention matched to PiFmRds (which decodes on real radios):
    - groups 0A,0A,0A,0A,2A repeating: 0A carries PI + TP/PTY + PS (2 chars per
      group, segments cycle 0..3), 2A carries up to 64 chars of RadioText
      (4 per group, segments cycle 0..15). Block C of 0A is 0xCDCD ("no AF"),
      same filler PiFmRds uses.
    - CRC-10 (poly 0x1B9 = x^10+x^8+x^7+x^5+x^4+x^3+1) XOR the block offset word
      A=0x0FC B=0x198 C=0x168 D=0x1B4, each block emitted MSB-first as
      16 data bits + 10 check bits.
    - whole bitstream (1187.5 bps) is differentially encoded, then biphase
      (Manchester) coded at 2375 chips/s, using the exact shaped biphase symbol
      waveform from PiFmRds (RRC pulse, alpha=1, regenerated here from source
      math; verified bit-for-bit against its waveforms.c table). 192 samples/bit
      at 228 kHz; overlapping symbols are summed, exactly like PiFmRds.

Timing caveat (expected, not a bug): the loop budget at 228 kHz is 4.4 us/sample
vs 22.7 us at 44.1 kHz, so expect a much higher missed-deadline rate than
rp1audiotx.py's 0.005%. RDS symbols are 842 us long, so the protocol itself
tolerates far more loop jitter than the per-sample budget suggests; audio
glitches are the thing you'll actually hear if the misses go up. Keep the test
short and watch the stats. If misses are ugly: try --max-seconds 60 first,
and there is no shame in running this only for a quick PS-display check.

No stereo pilot is transmitted (mono + RDS), which is a normal, valid config.
No RDS clock-time (4A/CT) groups yet. No pre-emphasis (radios will sound dull
on treble) - same known limitation as the other scripts here.

Legal: same as the rest of this folder. Low power, short test, clear frequency.
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
# RDS bit rate -> samples per bit at 228 kHz (exact); waveform length 3 bits
RDS_BITRATE = 1187.5
SAMPLES_PER_BIT = 192
WAVE_LEN = 576
SUBCARRIER = np.array([0.0, 1.0, 0.0, -1.0])  # sin(2*pi*n/4), phase 0 start
CHUNK = 1 << 20


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


# --------------------------------------------------------------------- RDS --

POLY = 0x1B9
OFFSET_WORDS = (0x0FC, 0x198, 0x168, 0x1B4)  # blocks A, B, C, D


def crc10(block):
    """10-bit remainder of block*x^10 mod g, g = x^10+x^8+x^7+x^5+x^4+x^3+1.

    Same shift-register as PiFmRds rds.c crc(); that version leaves junk in
    the top 6 bits of the 16-bit register, of which only the low 10 bits are
    ever transmitted -- masked here for clarity. Verified: for every block,
    (block<<10 | checkword) mod g == the offset word used.
    """
    crc = 0
    for _ in range(16):
        bit = (block >> 15) & 1
        block = (block << 1) & 0xFFFF
        msb = (crc >> 9) & 1
        crc = (crc << 1) & 0xFFFF
        if msb ^ bit:
            crc ^= POLY
    return crc & 0x3FF


def emit_26(bits, block, offset):
    """Append one 26-bit block (MSB first) to the bit list."""
    chk = (crc10(block) ^ offset) & 0x3FF
    for j in range(15, -1, -1):
        bits.append((block >> j) & 1)
    for j in range(9, -1, -1):
        bits.append((chk >> j) & 1)


def ascii_pad(s, n):
    out = []
    for ch in s:
        o = ord(ch)
        out.append(o if 32 <= o < 127 else 32)
        if len(out) == n:
            break
    return out + [32] * (n - len(out))


def rds_bitstream(n_bits, pi, ps, rt):
    """0A,0A,0A,0A,2A repeating, same as PiFmRds get_rds_group()."""
    ps_c = ascii_pad(ps, 8)
    rt_c = ascii_pad(rt, 64)
    bits = []
    g = 0
    ps_state = 0
    rt_state = 0
    while len(bits) < n_bits:
        if g % 5 < 4:
            b1 = pi
            b2 = 0x0400 | ps_state            # group 0A, TP=1, PS segment
            b3 = 0xCDCD                       # AF: "no AF", as in PiFmRds
            b4 = (ps_c[ps_state * 2] << 8) | ps_c[ps_state * 2 + 1]
            ps_state = (ps_state + 1) % 4
        else:
            b1 = pi
            b2 = 0x2400 | rt_state            # group 2A, RT segment 0..15
            b3 = (rt_c[rt_state * 4] << 8) | rt_c[rt_state * 4 + 1]
            b4 = (rt_c[rt_state * 4 + 2] << 8) | rt_c[rt_state * 4 + 3]
            rt_state = (rt_state + 1) % 16
        for blk, off in zip((b1, b2, b3, b4), OFFSET_WORDS):
            emit_26(bits, blk, off)
        g += 1
    return np.array(bits, dtype=np.int8)


def rrcosfilter(N, alpha, Ts, Fs):
    """RRC FIR, ported from Pydemod (shaping.py, from CommPy)."""
    T_delta = 1.0 / float(Fs)
    h = np.zeros(N, dtype=float)
    for x in range(N):
        t = (x - N / 2.0) * T_delta
        if t == 0.0:
            h[x] = 1.0 - alpha + (4 * alpha / np.pi)
        elif alpha != 0 and t == Ts / (4 * alpha):
            h[x] = (alpha / np.sqrt(2)) * (
                (1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        elif alpha != 0 and t == -Ts / (4 * alpha):
            h[x] = (alpha / np.sqrt(2)) * (
                (1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[x] = (np.sin(np.pi * t * (1 - alpha) / Ts)
                    + 4 * alpha * (t / Ts) * np.cos(np.pi * t * (1 + alpha) / Ts)) \
                / (np.pi * t * (1 - (4 * alpha * t / Ts) ** 2) / Ts)
    return h


def biphase_waveform():
    """PiFmRds's waveform_biphase[576], regenerated from source math.

    generate_waveforms.py: +1/-1 impulses 96 samples apart, RRC-shaped, an
    extract of the middle 576 samples, /2.5 amplitude. Verified against the
    committed waveforms.c table to ~1e-16.
    """
    l = 96
    sample = np.zeros(3 * l)
    sample[l] = 1
    sample[2 * l] = -1
    sf = rrcosfilter(96 * 8, 1, 1 / (2 * RDS_BITRATE), 228000 + 1)
    shaped = np.convolve(sample, sf)
    return shaped[528 - 288:528 + 288] / 2.5


def build_tables(x, in_rate, args):
    """Precompute per-sample divider words (di, df) for the whole file."""
    n = int(round(len(x) * args.rate / in_rate))
    n = max(1, n)
    base = args.freq * 1e6
    adev = args.dev * 1e3
    rdev = args.rds_dev * 1e3
    c = PLL_SYS_HZ * 65536.0

    scale = 0.95 / max(1e-9, float(np.max(np.abs(x)))) if len(x) else 0.0

    n_bits = int(np.ceil(n / SAMPLES_PER_BIT)) + 4
    bits = rds_bitstream(n_bits, args.pi, args.ps, args.rt)
    diff = np.bitwise_xor.accumulate(bits.astype(np.int64))  # differential enc.
    signs = (1.0 - 2.0 * diff)                               # 0->+1, 1->-1
    wav = biphase_waveform()

    di = np.empty(n, dtype=np.uint32)
    df = np.empty(n, dtype=np.uint32)
    clamped = 0

    src_idx = np.arange(len(x), dtype=np.float64)
    for s in range(0, n, CHUNK):
        e = min(n, s + CHUNK)
        aud = np.interp(np.arange(s, e) / args.rate * in_rate, src_idx, x)

        env = np.zeros((e - s) + WAVE_LEN, dtype=np.float64)
        i0 = max(0, (s - WAVE_LEN) // SAMPLES_PER_BIT - 1)
        i1 = min(len(signs), (e + SAMPLES_PER_BIT - 1) // SAMPLES_PER_BIT)
        for i in range(i0, i1):
            off = i * SAMPLES_PER_BIT - s
            a = max(0, off)
            b = min(env.size, off + WAVE_LEN)
            if b > a:
                env[a:b] += signs[i] * wav[a - off:b - off]
        env = env[:e - s]

        sub = SUBCARRIER[np.arange(s, e) & 3]
        freq = base + adev * scale * aud + rdev * env * sub
        fcl = np.clip(freq, FREQ_MIN_HZ, FREQ_MAX_HZ)
        clamped += int(np.count_nonzero(fcl != freq))
        words = np.rint(c / fcl).astype(np.int64)
        di[s:e] = (words >> 16).astype(np.uint32)
        df[s:e] = ((words & 0xFFFF) << 16).astype(np.uint32)

    di_first = int(di[0])
    const_int = bool(np.all(di == di[0]))
    if const_int:
        di = None
    return di, df, const_int, clamped, di_first


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("wav", nargs="?", help="16-bit PCM WAV; omit for a 1 kHz tone")
    ap.add_argument("--freq", type=float, default=97.3, help="carrier MHz")
    ap.add_argument("--dev", type=float, default=73.0, help="audio peak deviation kHz")
    ap.add_argument("--rds-dev", type=float, default=2.0, help="RDS peak deviation kHz")
    ap.add_argument("--rate", type=int, default=228000,
                    help="update rate Hz; must be a multiple of 57000 for clean RDS")
    ap.add_argument("--core", type=int, default=3, help="CPU core to pin to")
    ap.add_argument("--pi", default="1234", help="RDS PI code (hex)")
    ap.add_argument("--ps", default="PIWAVE  ", help="RDS PS, up to 8 chars")
    ap.add_argument("--rt", default="piwave-rp1 RDS test", help="RDS RadioText, up to 64 chars")
    ap.add_argument("--seconds", type=float, default=10.0, help="tone duration (no WAV)")
    ap.add_argument("--max-seconds", type=float, default=600.0,
                    help="truncate long audio (memory guard; 0 = no limit)")
    return ap.parse_args()


def main():
    args = parse_args()
    args.pi = int(args.pi, 16) & 0xFFFF

    if os.geteuid() != 0:
        sys.exit("Run with sudo.")

    if args.rate < 171000 or args.rate % 57000:
        print(f"[warn] --rate {args.rate} is not a multiple of 57000; the 57 kHz "
              f"subcarrier will be undersampled/distorted and radios may not decode.")
    if args.rate % 57000 == 0:
        cyc = args.rate // 57000
        print(f"update rate {args.rate} Hz = {cyc} samples per 57 kHz cycle")

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

    print(f"PI: {args.pi:04X}  PS: \"{args.ps[:8]}\"  RT: \"{args.rt[:64]}\"")
    print(f"audio: {src}, {dur:.1f}s | carrier {args.freq} MHz | "
          f"dev {args.dev} kHz audio + {args.rds_dev} kHz RDS")
    print(f"building divider table ({args.rate} Hz, ~{dur * args.rate / 1e6:.1f}M samples; "
          f"this takes a few seconds per minute of audio)...")
    t0 = time.time()
    di, df, const_int, clamped, di_first = build_tables(x, in_rate, args)
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
        print("Lots of misses -> try --rate 171000 or a shorter file, and compare.")
    elif missed > n * 0.001:
        print("Some misses are expected at 228 kHz (budget is only "
              f"{interval / 1000:.1f}us/sample in Python); RDS symbols are "
              "842us so the protocol itself is far more tolerant.")
    print("Clock disabled.")


if __name__ == "__main__":
    main()
