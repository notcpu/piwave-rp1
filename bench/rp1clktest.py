#!/usr/bin/env python3
"""
Read-only RP1 clock register sanity check.
Confirms resource1 (the 4MB 'virtual' BAR) maps RP1-internal addresses
1:1, by reading GPCLK_OE_CTRL and checking bit 0 (clk_gp0's OE bit,
which we already know is enabled at 50MHz from clk_summary).

NO WRITES. Run with sudo.
"""
import mmap
import os
import struct

PCI_ADDR = "0002:01:00.0"   # from your lspci output -- change if it differs
RESOURCE = f"/sys/bus/pci/devices/{PCI_ADDR}/resource1"

# RP1-internal address of the clocks block
CLOCKS_BASE_ONCHIP = 0x40018000
# If resource1 maps 0x40000000-0x403fffff 1:1, this is the file offset
CLOCKS_OFFSET_IN_BAR = CLOCKS_BASE_ONCHIP - 0x40000000  # should be 0x18000

GPCLK_OE_CTRL = 0x00000  # relative to clocks block base
CLK_GP0_CTRL  = 0x00174
CLK_GP0_DIV_INT = 0x00178

PAGE = mmap.PAGESIZE  # 4096, and 0x18000 is page-aligned, convenient

def main():
    if os.geteuid() != 0:
        print("Run with sudo -- reading PCI BAR resource files needs root.")
        return

    print(f"Opening {RESOURCE}")
    fd = os.open(RESOURCE, os.O_RDONLY | os.O_SYNC)
    try:
        m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED, mmap.PROT_READ,
                      offset=CLOCKS_OFFSET_IN_BAR)
    except Exception as e:
        print(f"mmap failed: {e}")
        print("If this errors with 'Invalid argument', the offset math is wrong")
        print("and resource1 does NOT map 1:1 the way we're guessing.")
        os.close(fd)
        return

    def read32(rel_offset):
        return struct.unpack_from("<I", m, rel_offset)[0]

    oe_ctrl = read32(GPCLK_OE_CTRL)
    gp0_ctrl = read32(CLK_GP0_CTRL)
    gp0_div = read32(CLK_GP0_DIV_INT)

    print(f"GPCLK_OE_CTRL   = 0x{oe_ctrl:08x}")
    print(f"CLK_GP0_CTRL    = 0x{gp0_ctrl:08x}")
    print(f"CLK_GP0_DIV_INT = 0x{gp0_div:08x}")
    print()

    if oe_ctrl & 0x1:
        print(">>> bit 0 IS set -- matches clk_gp0 already being enabled.")
        print(">>> Offset math checks out. resource1 maps 1:1 as expected.")
    else:
        print(">>> bit 0 NOT set -- either clk_gp0 isn't actually enabled")
        print(">>> right now, or our offset assumption is wrong. Don't")
        print(">>> proceed to writes until this is sorted out.")

    m.close()
    os.close(fd)

if __name__ == "__main__":
    main()
