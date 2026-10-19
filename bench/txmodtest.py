#!/usr/bin/env python3
"""
GPCLK0 modulation test -- Stage 1 (sweep) and Stage 2 (audio tone).
GPIO4 must already be muxed to ALT0 (sudo pinctrl set 4 a0).

This is a proof-of-concept, not broadcast quality. Pure Python can't
reliably hit tight microsecond-level timing -- expect jitter/grit,
especially in Stage 2. That's the whole reason PiFmRds originally used
DMA-timed writes instead of a userspace loop. We're proving the register
math and mechanism work before ever reaching for C.
"""
import mmap, os, struct, sys, time, math, argparse

PCI_ADDR = "0002:01:00.0"
RESOURCE = f"/sys/bus/pci/devices/{PCI_ADDR}/resource1"
CLOCKS_OFFSET_IN_BAR = 0x40018000 - 0x40000000

GPCLK_OE_CTRL    = 0x00000
CLK_GP0_CTRL     = 0x00174
CLK_GP0_DIV_INT  = 0x00178
CLK_GP0_DIV_FRAC = 0x0017c

CLK_CTRL_ENABLE       = 1 << 11
CLK_CTRL_AUXSRC_MASK  = 0x000003e0
CLK_CTRL_AUXSRC_SHIFT = 5
GP0_OE_MASK = 1 << 0
PLL_SYS_PARENT_INDEX = 6
PLL_SYS_RATE_HZ = 200_000_000

BASE_FREQ_HZ = 103_000_000   # match whatever worked in the carrier test

def choose_div(parent_rate, rate):
    div = round((parent_rate << 16) / rate)
    return div >> 16, (div & 0xFFFF) << 16

class GPCLK0:
    def __init__(self):
        self.fd = os.open(RESOURCE, os.O_RDWR | os.O_SYNC)
        self.m = mmap.mmap(self.fd, mmap.PAGESIZE, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE,
                            offset=CLOCKS_OFFSET_IN_BAR)

    def read32(self, off): return struct.unpack_from("<I", self.m, off)[0]
    def write32(self, off, val): struct.pack_into("<I", self.m, off, val)

    def set_freq(self, freq_hz):
        di, df = choose_div(PLL_SYS_RATE_HZ, freq_hz)
        self.write32(CLK_GP0_DIV_INT, di)
        self.write32(CLK_GP0_DIV_FRAC, df)

    def enable(self):
        ctrl = self.read32(CLK_GP0_CTRL)
        ctrl &= ~CLK_CTRL_AUXSRC_MASK
        ctrl |= (PLL_SYS_PARENT_INDEX << CLK_CTRL_AUXSRC_SHIFT) & CLK_CTRL_AUXSRC_MASK
        ctrl |= CLK_CTRL_ENABLE
        self.write32(CLK_GP0_CTRL, ctrl)
        oe = self.read32(GPCLK_OE_CTRL)
        self.write32(GPCLK_OE_CTRL, oe | GP0_OE_MASK)

    def disable(self):
        oe = self.read32(GPCLK_OE_CTRL)
        self.write32(GPCLK_OE_CTRL, oe & ~GP0_OE_MASK)
        ctrl = self.read32(CLK_GP0_CTRL)
        self.write32(CLK_GP0_CTRL, ctrl & ~CLK_CTRL_ENABLE)

    def close(self):
        self.m.close(); os.close(self.fd)

def precise_sleep_until(target_ns):
    """Hybrid sleep+spin -- time.sleep is too coarse alone for this."""
    now = time.perf_counter_ns()
    remaining = target_ns - now
    if remaining > 500_000:  # >0.5ms left, sleep most of it
        time.sleep((remaining - 200_000) / 1e9)
    while time.perf_counter_ns() < target_ns:
        pass

def stage1_sweep(clk, deviation_hz=75_000, sweep_rate_hz=1.0, duration_s=8, update_hz=200):
    print(f"Stage 1: sweeping ±{deviation_hz/1000:.0f}kHz around "
          f"{BASE_FREQ_HZ/1e6:.3f}MHz at {sweep_rate_hz}Hz for {duration_s}s")
    print("Watch the SDR waterfall -- you should see the line move up and down smoothly.")
    clk.enable()
    start = time.perf_counter_ns()
    interval_ns = int(1e9 / update_hz)
    n = int(duration_s * update_hz)
    for i in range(n):
        t = i / update_hz
        f = BASE_FREQ_HZ + deviation_hz * math.sin(2 * math.pi * sweep_rate_hz * t)
        clk.set_freq(f)
        precise_sleep_until(start + (i + 1) * interval_ns)
    print("Stage 1 done.")

def stage2_tone(clk, tone_hz=440, deviation_hz=75_000, sample_rate=8000, duration_s=5):
    print(f"Stage 2: FM-modulating a {tone_hz}Hz test tone, "
          f"{deviation_hz/1000:.0f}kHz deviation, {sample_rate}Hz sample rate, "
          f"{duration_s}s. Grab a real FM radio and tune to "
          f"{BASE_FREQ_HZ/1e6:.1f}MHz.")
    clk.enable()
    start = time.perf_counter_ns()
    interval_ns = int(1e9 / sample_rate)
    n = int(duration_s * sample_rate)
    for i in range(n):
        t = i / sample_rate
        f = BASE_FREQ_HZ + deviation_hz * math.sin(2 * math.pi * tone_hz * t)
        clk.set_freq(f)
        precise_sleep_until(start + (i + 1) * interval_ns)
    print("Stage 2 done.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["1", "2"], help="1=sweep test, 2=audio tone test")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("Run with sudo."); sys.exit(1)

    clk = GPCLK0()
    try:
        if args.stage == "1":
            stage1_sweep(clk)
        else:
            stage2_tone(clk)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        clk.disable()
        clk.close()
        print("Clock disabled, cleaned up.")

if __name__ == "__main__":
    main()
