# piwave-rp1 — porting PiFmRds-style FM transmission to the Raspberry Pi 5

> **Status: proof of concept.** Register-level FM carrier generation, real-time
> audio modulation, RDS, and software MASH dithering are implemented and verified
> locally on RP1. On-air RDS testing is still pending, and a clean
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

## Repo layout

Scripts are grouped by job:

```
tx/                     transmitters
  rp1audiotx.py         baseline mono FM audio
  rp1rdstx.py           audio + 57 kHz RDS (PS + RadioText)
  rp1mashdithertx.py    audio + software MASH noise shaping

bench/                  hardware bring-up / tests
  rp1clktest.py         read-only GPCLK register sanity check
  txtest.py             static unmodulated carrier
  txmodtest.py          sweep + audio-tone modulation test

tools/                  helpers
  txstop.py             disable GPCLK0 (panic stop)
  reconRP1.sh           read-only Pi/RP1 recon, run first
```

All commands below are written from the repo root. Run `bash tools/reconRP1.sh`
on the Pi first if the register setup is unknown.

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

## Test 2 — real audio

After writing a working transmitter for audio, I tested it with the song *Aerosol
Can* by Major Lazer:

```
notcpu@sector314:~/Desktop $ sudo python3 tx/rp1audiotx.py song.wav --freq 107.0 --rate 44100
8120320 samples @ 44100Hz = 184.1s | carrier 107.0MHz, +-75.0kHz
DIV_INT constant -> fast single-write path
Done. missed deadlines: 396/8120320 (0.005%), worst lateness: 2773.3us (budget 22.7us/sample)
Clock disabled.
```

Only 396 missed deadlines out of 8.12M, with the worst lateness at 2.7ms.

## RDS — `tx/rp1rdstx.py`

Fixed 1187.5 bps → differential + biphase → 57 kHz DSB, exactly PiFmRds's
conventions (its biphase symbol waveform was regenerated from source math and
matches PiFmRds's committed table to ~1e-16).

```
sudo python3 tx/rp1rdstx.py song.wav --freq 97.3 --pi 1234 --ps "PIWAVE  " --rt "hello from the RP1"
sudo python3 tx/rp1rdstx.py                 # no WAV: built-in 1 kHz tone, 10 s
```

What to expect on a radio: PS shows `PIWAVE  `, RadioText scrolls the `--rt`
string; audio is mono (no 19 kHz pilot — normal config). Groups 0A,0A,0A,0A,2A.

Caveats (also in the file's docstring):

- Update rate defaults to 228 kHz → 4.4 us/sample loop budget, so expect many
  more missed deadlines than the 44.1 kHz script. RDS symbols are 842 us long,
  so the protocol itself shrugs that off; audio glitches are the audible part.
  Keep tests short; `--max-seconds 60` for a first check.
- No RDS clock-time (CT) groups yet; no pre-emphasis (treble sounds dull, as
  with all scripts here).

## MASH dithering — `tx/rp1mashdithertx.py`

The RP1 divider has no hardware noise shaper, so the desired Q16.16 divider
word gets rounded to the 1-LSB grid (722 Hz at 97.3 MHz!) with *deterministic,
audio-correlated* error — the likely source of the "grainy" character. This
script quantizes through a software MASH error-feedback modulator instead,
which pushes that error above the audio band.

Measured by the built-in selftest (de-emphasis-weighted 0–15 kHz error vs
plain rounding, music-like multitone):

| update rate | best order | improvement |
|---|---|---|
| 88.2 kHz (default) | 3 | **−25 dB** (error <3.7 kHz is −43 dB) |
| 44.1 kHz | 1 | −11 dB (order 3 is only −6 dB there) |

So defaults: `--rate 88200`, order auto (3 when rate ≥ 88.2k, else 1).

```
python3 tx/rp1mashdithertx.py --selftest          # no root, no hardware, pure math
sudo python3 tx/rp1mashdithertx.py song.wav                    # order 3 @ 88.2k
sudo python3 tx/rp1mashdithertx.py song.wav --order 0          # A/B baseline
sudo python3 tx/rp1mashdithertx.py song.wav --rate 44100 --order 1
```

A/B procedure: same file, same freq, only `--order 0` vs `3` (and optionally
`--rate`). Compare with an SDR (deterministic spurs → shaped noise hash) and by
ear. The modulator output stays within ~4 word-LSBs (~3 kHz) of the exact target
and the freq safety clamp (`[4, 199] MHz`) count is printed — nothing here can
push the clock into the documented ~200 MHz near-unity-divide edge case.

## Verification done before shipping (all local, on the same files)

- RDS: waveform table equals PiFmRds's `waveforms.c` (max err 1.1e-16);
  CRC-10 equals the C implementation and the true polynomial remainder;
  `(block<<10 | checkword) mod g == offset word` for every emitted block;
  encoder → full analog chain (divider words → envelope → differential +
  biphase decode) → identical bit sequence, PS/RT/PI/TP all recovered;
  57 kHz subcarrier confirmed in the deviation spectrum (+26.6 dB vs 25–39k).
- MASH: chunked quantizer bit-exact vs the verified whole-array math for
  orders 1/2/3 and arbitrary chunk splits; bounded (max |error| 1/2/3.9/4.2
  words for orders 1/2/3/3+tpdf) and mean-preserving; spectral measurements
  as summarized above; `--tpdf` is kept but measured *worse* at 88.2 kHz
  (its flat noise outweighs the shaping gain) — leave it off.
- Both files `py_compile` clean; both disable the clock and restore RT
  throttle state on exit (KeyboardInterrupt included). `tools/txstop.py`
  remains the fallback if anything is ever left running.

## Known limitations

- **The modulation loop is still pure Python**, timed with a hybrid
  sleep/busy-wait. It proves the mechanism works end to end but can't reliably
  hit tight microsecond-level timing for clean broadcast-quality audio. A C
  rewrite (ideally with DMA-timed writes, matching the original PiFmRds
  approach) is planned.
- **RDS raises the update rate to 228 kHz**, cutting the per-sample budget to
  4.4 us. Missed deadlines are much more common there; the RDS symbols tolerate
  it, but audio may glitch.
- **No pre-emphasis** is applied, so treble sounds dull on a real radio.
  RDS also lacks clock-time (CT) groups.
- **Software MASH helps, not eliminates.** Order 3 at 88.2 kHz is the best
  measured tradeoff; at 44.1 kHz only order 1 is worth using.

## Legal / safety

FM transmission is regulated. This is for experimentation on frequencies and
power levels that don't interfere with licensed use, same disclaimer as the
original PiFmRds project. Keep it on a clear frequency, low power, short
duration, and don't radiate into anything that matters. Know your local
regulations before transmitting anything beyond a short, contained test.

## Roadmap

- [x] Confirm RP1 register map via kernel source (no guessed addresses)
- [x] Static carrier via direct register writes
- [x] Real-time FM audio modulation (proof of concept)
- [x] Characterize usable frequency range
- [x] Transmit audio
- [x] Create test file for RDS (PS/RT/PI), matching original PiFmRds feature set
- [x] Create test file for software MASH dithering of the divider word
- [ ] On-air RDS validation (PS/RT on a real receiver)
- [ ] C rewrite for clean, low-jitter modulation timing
- [ ] Wire into piwave as a new backend

## Credits

- [douxxtech/piwave](https://github.com/douxxtech/piwave)
- [dpipstudio/botwave](https://github.com/dpipstudio/botwave)
- [ChristopheJacquet/PiFmRds](https://github.com/ChristopheJacquet/PiFmRds) — the
  original technique this builds on
- Raspberry Pi's [`clk-rp1.c`](https://github.com/raspberrypi/linux) driver, the
  actual source of truth for every register offset used here
- Claude Sonnet 5
- DeepSeek V4.1 Flash