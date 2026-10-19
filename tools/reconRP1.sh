#!/bin/bash
# RP1 recon — run this ON THE PI 5 before touching any registers.
# Just gathers facts, doesn't write anything.

echo "== Kernel / OS =="
uname -a
cat /etc/rpi-issue 2>/dev/null || echo "(no /etc/rpi-issue, might not be Raspberry Pi OS)"
echo

echo "== RP1 PCIe presence =="
lspci -d 1de4:0001 -v
echo

echo "== RP1 driver bound? =="
ls -l /sys/bus/pci/devices/*/driver 2>/dev/null | grep -i rp1
echo

echo "== RP1 PCIe BAR (this is what we'll mmap) =="
RP1_PCI=$(lspci -d 1de4:0001 -D | awk '{print $1}')
if [ -n "$RP1_PCI" ]; then
    echo "PCI address: $RP1_PCI"
    cat /sys/bus/pci/devices/"$RP1_PCI"/resource
    echo "Resource file sizes:"
    ls -la /sys/bus/pci/devices/"$RP1_PCI"/resource0 2>/dev/null
else
    echo "!! RP1 not found via lspci -- stop here, something's wrong with the board/driver."
fi
echo

echo "== clk-rp1 driver loaded + GPCLK support =="
dmesg | grep -i "clk-rp1\|rp1.*clk\|gpclk" 
echo "(if this is empty, your kernel/firmware may predate GPCLK support -- check 'rpi-eeprom-update' and 'sudo apt full-upgrade' first)"
echo

echo "== Existing clock summary (look for anything GPCLK-related) =="
cat /sys/kernel/debug/clk/clk_summary 2>/dev/null | grep -i "gp\|rp1" || echo "(debugfs clk_summary not mounted -- try: sudo mount -t debugfs none /sys/kernel/debug)"
echo

echo "== dtoverlay help for gpio/gpclk on RP1 =="
ls /boot/firmware/overlays/ 2>/dev/null | grep -i "gpclk\|gpio"
echo

echo "== Kernel source check (for exact clk-rp1.c register offsets) =="
echo "If you don't already have it:"
echo "  git clone --depth 1 -b rpi-6.6.y https://github.com/raspberrypi/linux rpi-linux-src"
echo "  then: rpi-linux-src/drivers/clk/clk-rp1.c is our source of truth for register offsets"
