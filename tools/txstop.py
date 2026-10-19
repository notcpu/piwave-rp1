#!/usr/bin/env python3
"""Disable GPCLK0 -- run this to shut the carrier off."""
import mmap, os, struct, sys

PCI_ADDR = "0002:01:00.0"
RESOURCE = f"/sys/bus/pci/devices/{PCI_ADDR}/resource1"
CLOCKS_OFFSET_IN_BAR = 0x40018000 - 0x40000000

GPCLK_OE_CTRL = 0x00000
CLK_GP0_CTRL  = 0x00174
CLK_CTRL_ENABLE = 1 << 11
GP0_OE_MASK = 1 << 0

def main():
    if os.geteuid() != 0:
        print("Run with sudo."); sys.exit(1)

    fd = os.open(RESOURCE, os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(fd, mmap.PAGESIZE, mmap.MAP_SHARED,
                  mmap.PROT_READ | mmap.PROT_WRITE, offset=CLOCKS_OFFSET_IN_BAR)

    def read32(off): return struct.unpack_from("<I", m, off)[0]
    def write32(off, val): struct.pack_into("<I", m, off, val)

    # Output-enable off first, then the clock itself
    oe = read32(GPCLK_OE_CTRL)
    write32(GPCLK_OE_CTRL, oe & ~GP0_OE_MASK)

    ctrl = read32(CLK_GP0_CTRL)
    write32(CLK_GP0_CTRL, ctrl & ~CLK_CTRL_ENABLE)

    print(f"GPCLK_OE_CTRL now = 0x{read32(GPCLK_OE_CTRL):08x}")
    print(f"CLK_GP0_CTRL  now = 0x{read32(CLK_GP0_CTRL):08x}")
    print("GPCLK0 disabled.")

    m.close(); os.close(fd)

if __name__ == "__main__":
    main()
