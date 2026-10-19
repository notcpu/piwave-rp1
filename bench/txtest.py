#!/usr/bin/env python3
"""
RP1 GPCLK0 static carrier test.
Requires: GPIO4 already muxed to ALT0 via `sudo pinctrl set 4 a0` BEFORE running this.

This writes real hardware registers. Read it before running it.
Generates an UNMODULATED carrier -- just proving the chain works end to end.
Point your RTL-SDR at GPIO4 (pin 7) to confirm.
"""
import mmap
import os
import struct
import sys

PCI_ADDR = "0002:01:00.0"          # <-- confirm matches your lspci output
RESOURCE = f"/sys/bus/pci/devices/{PCI_ADDR}/resource1"

CLOCKS_BASE_ONCHIP = 0x40018000
CLOCKS_OFFSET_IN_BAR = CLOCKS_BASE_ONCHIP - 0x40000000  # 0x18000, page-aligned

# Offsets relative to clocks block base
GPCLK_OE_CTRL   = 0x00000
CLK_GP0_CTRL    = 0x00174
CLK_GP0_DIV_INT = 0x00178
CLK_GP0_DIV_FRAC = 0x0017c

CLK_CTRL_ENABLE      = 1 << 11
CLK_CTRL_AUXSRC_MASK = 0x000003e0
CLK_CTRL_AUXSRC_SHIFT = 5
GP0_OE_MASK = 1 << 0

PLL_SYS_PARENT_INDEX = 6   # index of "pll_sys" in clk_gp0's .parents[] array
PLL_SYS_RATE_HZ = 200_000_000  # from the DT assigned-clock-rates

# ---- CHANGE THIS to a frequency that's actually clear where you are ----
TARGET_FREQ_HZ = 97_300_000

PAGE = mmap.PAGESIZE

def choose_div(parent_rate, rate):
    """Mirrors rp1_clock_choose_div()'s Q16.16 fixed-point math."""
    div = round((parent_rate << 16) / rate)
    div_int = div >> 16
    div_frac = (div & 0xFFFF) << 16
    actual_rate = (parent_rate << 16) / div
    return div_int, div_frac, actual_rate

def main():
    if os.geteuid() != 0:
        print("Run with sudo.")
        sys.exit(1)

    div_int, div_frac, actual = choose_div(PLL_SYS_RATE_HZ, TARGET_FREQ_HZ)
    print(f"Target: {TARGET_FREQ_HZ/1e6:.4f} MHz")
    print(f"Actual: {actual/1e6:.4f} MHz  (div_int={div_int}, div_frac=0x{div_frac:08x})")

    confirm = input("Pin GPIO4 already set to ALT0 (a0)? Type YES to proceed: ")
    if confirm != "YES":
        print("Aborted.")
        sys.exit(0)

    fd = os.open(RESOURCE, os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                  offset=CLOCKS_OFFSET_IN_BAR)

    def read32(off):
        return struct.unpack_from("<I", m, off)[0]

    def write32(off, val):
        struct.pack_into("<I", m, off, val)

    # 1. Program the divider (clock should be disabled or glitches happen -- it already is)
    write32(CLK_GP0_DIV_INT, div_int)
    write32(CLK_GP0_DIV_FRAC, div_frac)

    # 2. Select pll_sys as source (AUXSRC field) and set ENABLE, preserving unknown bits
    ctrl = read32(CLK_GP0_CTRL)
    ctrl &= ~CLK_CTRL_AUXSRC_MASK
    ctrl |= (PLL_SYS_PARENT_INDEX << CLK_CTRL_AUXSRC_SHIFT) & CLK_CTRL_AUXSRC_MASK
    ctrl |= CLK_CTRL_ENABLE
    write32(CLK_GP0_CTRL, ctrl)

    # 3. Output-enable last, matching the driver's own sequencing
    oe = read32(GPCLK_OE_CTRL)
    oe |= GP0_OE_MASK
    write32(GPCLK_OE_CTRL, oe)

    print()
    print(f"CLK_GP0_CTRL    now = 0x{read32(CLK_GP0_CTRL):08x}")
    print(f"CLK_GP0_DIV_INT now = 0x{read32(CLK_GP0_DIV_INT):08x}")
    print(f"GPCLK_OE_CTRL   now = 0x{read32(GPCLK_OE_CTRL):08x}")
    print()
    print(f"If this worked, GPIO4 (pin 7) should now be outputting ~{actual/1e6:.3f} MHz.")
    print("Point the RTL-SDR at it (bare wire on GPIO4 is enough for a close-range test).")

    m.close()
    os.close(fd)

if __name__ == "__main__":
    main()
