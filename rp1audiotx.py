#!/usr/bin/env python3
"""
GPCLK0 real-audio FM test: plays a WAV file as FM on GPIO4.

Prereqs:
  sudo apt install python3-numpy
  sudo pinctrl set 4 a0
  ffmpeg -i song.mp3 -ac 1 -ar 44100 -c:a pcm_s16le song.wav   (16-bit PCM WAV)

Run:
  sudo python3 rp1_gpclk0_audio_test.py song.wav --freq 97.3 --rate 44100

How it works:
  * Whole file is converted UP FRONT into a table of DIV_FRAC values, so the
    hot loop is just: wait for deadline -> one 32-bit register store.
  * Only DIV_FRAC is written per sample when DIV_INT stays constant (true for
    +-75kHz around most carriers). Otherwise falls back to a dual-write path.
  * Pins itself to one core, SCHED_FIFO, GC off, and TEMPORARILY disables the
    kernel's RT throttling (otherwise it forces a ~50ms dropout every second
    for a spin loop). Everything is restored on exit.
  * Prints missed-deadline stats so we learn what rate Python can really hold.

No pre-emphasis is applied, so on a real radio (which de-emphasizes) treble
will sound dull. Expected for now. Keep tests short and low power, on a
frequency that's clear where you are.
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

RT_THROTTLE = "/proc/sys/kernel/sched_rt_runtime_us"


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


def resample(x, sr_in, sr_out):
    if sr_in == sr_out:
        return x
    n_out = int(len(x) * sr_out / sr_in)
    return np.interp(np.arange(n_out) / sr_out, np.arange(len(x)) / sr_in, x)


def build_table(x, base_hz, dev_hz):
    peak = max(1e-9, float(np.max(np.abs(x))))
    x = np.clip(x / peak * 0.95, -1.0, 1.0)
    f = base_hz + dev_hz * x
    div = np.rint((PLL_SYS_HZ * 65536.0) / f).astype(np.int64)
    di = (div >> 16).astype(np.uint32)
    df = ((div & 0xFFFF) << 16).astype(np.uint32)
    return di, df


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
    orig = None
    try:
        os.sched_setaffinity(0, {core})
    except Exception as e:
        print(f"[warn] couldn't pin to core {core}: {e}")
    try:
        with open(RT_THROTTLE) as f:
            orig = f.read().strip()
        with open(RT_THROTTLE, "w") as f:
            f.write("-1")
    except Exception as e:
        print(f"[warn] couldn't disable RT throttling (expect dropouts): {e}")
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(80))
    except Exception as e:
        print(f"[warn] couldn't set SCHED_FIFO: {e}")
    gc.collect()
    gc.disable()
    return orig


def restore_realtime(orig):
    gc.enable()
    if orig is not None:
        try:
            with open(RT_THROTTLE, "w") as f:
                f.write(orig)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--freq", type=float, default=97.3, help="carrier MHz")
    ap.add_argument("--dev", type=float, default=75.0, help="peak deviation kHz")
    ap.add_argument("--rate", type=int, default=44100, help="update rate Hz")
    ap.add_argument("--core", type=int, default=3, help="CPU core to pin to")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run with sudo.")

    base_hz = args.freq * 1e6
    x, sr = load_wav(args.wav)
    x = resample(x, sr, args.rate)
    di, df = build_table(x, base_hz, args.dev * 1e3)
    n = len(df)
    const_int = bool(np.all(di == di[0]))

    print(f"{n} samples @ {args.rate}Hz = {n / args.rate:.1f}s | "
          f"carrier {args.freq}MHz, +-{args.dev}kHz")
    print("DIV_INT constant -> fast single-write path" if const_int
          else "DIV_INT changes in this range -> slower dual-write path")

    mv_f = memoryview(df)
    mv_i = memoryview(di)
    pk = struct.Struct("<I").pack_into
    pc = time.perf_counter_ns
    ns = 1e9 / args.rate
    interval = int(ns)

    clk = GPCLK0()
    orig_throttle = go_realtime(args.core)
    m = clk.m
    missed = 0
    worst = 0
    try:
        clk.w(CLK_GP0_DIV_INT, int(di[0]))
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
            last_i = int(di[0])
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
        restore_realtime(orig_throttle)

    print(f"Done. missed deadlines: {missed}/{n} ({100.0 * missed / max(1, n):.3f}%), "
          f"worst lateness: {worst / 1000:.1f}us (budget {interval / 1000:.1f}us/sample)")
    if missed > n * 0.001:
        print("Lots of misses -> try a lower --rate (32000/22050/16000) and compare.")
    print("Clock disabled.")


if __name__ == "__main__":
    main()
