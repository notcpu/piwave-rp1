# piwave-rp1 — porting PiFmRds-style FM transmission to the Raspberry Pi 5

> **Status: proof of concept.** Register-level FM carrier generation and real-time
> audio modulation are confirmed working on RP1. This repo currently documents the
> reverse-engineering process and includes working test scripts; a clean
> production-quality backend (C, DMA-timed) is in progress.

## Background

[piwave](https://github.com/douxxtech/piwave) wraps **PiFmRds**, which generates FM
by directly poking the Raspberry Pi's clock generator to phase-modulate `GPCLK0` on
GPIO4. That trick works by `mmap`-ing `/dev/mem` at a hardcoded physical address for
the BCM2835/2711 clock manager (`CM_GP0CTL`/`CM_GP0DIV`).

**This breaks on the Pi 5.** GPIO and clocks no longer live on the main SoC. They
moved to **RP1**, a separate I/O coprocessor attached over PCIe. Different address
space, different register layout, no `/dev/mem` shortcut. This repo documents
porting the technique to RP1 from scratch, straight from kernel source, without
guessing register addresses.

## How it works

RP1 shows up as a standard PCIe device (`1de4:0001`, driver `rp1`). One of its BARs
(the 4MB one, flagged `[virtual]` in `lspci -v`) maps RP1's internal address space
**1:1** onto a `resource1` sysfs file. As root, that file can be `mmap()`'d directly
— no device tree overlay, no `/dev/mem`, no kernel module required.

```
RP1 clocks block (on-chip address 0x40018000)
  = offset 0x18000 in /sys/bus/pci/devices/<rp1-pci-addr>/resource1
```

GPCLK0 is still GPIO4 ALT0 on RP1, same pin as the original hack.

### Register map (clk_gp0)

| Register           | Offset (from clocks block base) |
|--------------------|----------------------------------|
| `GPCLK_OE_CTRL`    | `0x000` (shared across gp0/gp1/gp2, bit 0/1/2) |
| `CLK_GP0_CTRL`     | `0x174` |
| `CLK_GP0_DIV_INT`  | `0x178` |
| `CLK_GP0_DIV_FRAC` | `0x17c` |

`CLK_GP0_CTRL` bit 11 = `ENABLE`. Bits 5-9 = `AUXSRC` (parent clock select — index 6
selects `pll_sys`, which runs at 200MHz per the device tree's
`assigned-clock-rates`).

### Divider math

The divider is Q16.16 fixed-point:

```
div         = round((parent_rate << 16) / target_rate)
div_int     = div >> 16
div_frac    = (div & 0xFFFF) << 16
actual_rate = (parent_rate << 16) / div
```

Confirmed against `rp1_clock_choose_div()`/`rp1_clock_recalc_rate()` in
`drivers/clk/clk-rp1.c` — they're exact inverses of each other.

### The 100MHz software cap

The in-kernel `clk-rp1` driver hardcodes `max_freq = 100MHz` on the GPCLK channels
and **hard-rejects** any `clk_set_rate()` request above it (not a soft clamp — see
the driver's own rate-calc code). This only applies to the Linux clock framework's
call path. Writing the registers directly, as this project does, **bypasses it
entirely**. Confirmed working well above 100MHz (see test results below).

## Test results

Frequency sweep test (±75kHz sine sweep at 1Hz) across the tunable range:

**Working range: ~5–7MHz (floor) to 199MHz (ceiling). Antenna used was the bare exposed GPIO.**

| Frequency    | Result |
|--------------|--------|
| 3–5MHz       | Pass, quiet (likely antenna length mismatch, not a clock limitation) |
| 10–48MHz     | Pass |
| 50.5–141MHz  | Pass |
| 150MHz       | Pass — very noisy |
| 195–198MHz   | Pass |
| **199MHz**   | Pass — practical hard ceiling |
| 200MHz       | Toggles on/off instead of sweeping — hits the near-unity-divide edge case the driver's own rounding-tolerance code warns about |

Sound character varies with how "clean" the divider ratio is relative to the
200MHz parent — rational ratios (e.g. 150MHz = 4/3) produce noticeably different
spur/harmonic behavior than others. Not yet fully characterized.

## Known limitations

- **No MASH/sigma-delta dithering.** RP1's GPCLK fractional divider is a plain
  truncating divider (confirmed absent from `clk-rp1.c`), unlike the original
  BCM2835 hack's noise-shaped PLL. Expect audibly grainier modulation until this
  is compensated for in software, or accepted as a known characteristic.
- **Current modulation loop is pure Python**, timed with a hybrid sleep/busy-wait.
  It proves the mechanism works end to end but can't reliably hit tight
  microsecond-level timing for clean broadcast-quality audio. A C rewrite
  (ideally with DMA-timed writes, matching the original PiFmRds approach) is
  planned.

## Scripts in this repo

- `rp1clktest.py` — sanity-checks the BAR-offset mapping theory,
  read-only, no writes
- `txtest.py` — enables a static, unmodulated carrier
- `txstop.py` — disables the clock (the hardware keeps running
  independently of any process until this is run)
- `txmodtest.py` — two-stage test: a visual sweep, then a real
  FM-modulated audio tone

## Legal / safety

FM transmission is regulated. This is for experimentation on frequencies and
power levels that don't interfere with licensed use, same disclaimer as the
original PiFmRds project. Know your local regulations before transmitting
anything beyond a short, contained test.

## Roadmap

- [x] Confirm RP1 register map via kernel source (no guessed addresses)
- [x] Static carrier via direct register writes
- [x] Real-time FM audio modulation (proof of concept)
- [x] Characterize usable frequency range
- [ ] C rewrite for clean, low-jitter modulation timing
- [ ] Wire into piwave as a new backend
- [ ] RDS support (PS/RT/PI), matching original PiFmRds feature set

## Credits

- [douxxtech/piwave](https://github.com/douxxtech/piwave)
- [ChristopheJacquet/PiFmRds](https://github.com/ChristopheJacquet/PiFmRds) — the
  original technique this builds on
- Raspberry Pi's [`clk-rp1.c`](https://github.com/raspberrypi/linux) driver, the
  actual source of truth for every register offset used here
