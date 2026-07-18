# PoW access-tier tuning — findings

_Report generated Fri 2026-07-17. Scope: browser proof-of-work difficulty, the CPU vs
"GPU" mining paths, the `/bench` benchmark tool, and related fixes in the meme vault._

## TL;DR

- The in-browser **"GPU" path is not a real GPU shader** — it is multi-lane
  WebCrypto (`crypto.subtle`), the same primitive the CPU path uses. On typical
  browser hardware it delivers **~1× the single-lane rate**, because WebCrypto
  already offloads hashing off the main JS thread.
- Consequence observed in testing: **CPU and "GPU" both land at ~19 zero-bits**
  for a reasonable solve time, and the WebGPU indicator does not light up on the
  test machine (no WebGPU adapter, or no meaningful speedup).
- **Working conclusion:** a two-tier CPU/GPU difficulty split buys little today.
  A single tier (or a very small GPU bonus) is likely the honest choice until a
  true WGSL SHA-256d miner exists.
- **Data status:** no quantitative browser benchmark has been logged yet — only a
  synthetic placeholder entry. Numbers below marked _(illustrative)_ are not from
  your machine. Run `/bench` to capture real figures (they append to
  `logs/bench.log`).

## What was investigated

The meme vault gates content behind SHA-256d proof of work. Two capability tiers
are negotiated:

- **CPU (easy)** — `ACCESS_ZERO_BITS`
- **GPU (hard)** — `ACCESS_ZERO_BITS + ACCESS_GPU_BITS_BONUS`, clamped 12–32

The browser miner (`templates/partials/access_miner_script.html`) probes WebGPU,
tries the hard job first when available, then falls back to CPU. The open question:
**what difficulties actually make sense for real hardware, and is the GPU tier
worth having?**

## Method — the `/bench` tool

A self-contained benchmark page was added at `/bench`. It uses the **exact same**
`sha256d` primitives as the miner, so results reflect real behavior. It measures:

1. **Environment** — logical cores, device memory, WebGPU availability/adapter.
2. **Throughput** — single-lane vs multi-lane ("accelerated") hashrate over a fixed
   window, plus the accelerated/single speedup.
3. **Difficulty projection** — for 14–30 zero-bits, expected hashes (`2^bits`) and
   projected solve time per method; highlights rows near a target solve time and
   recommends `ACCESS_ZERO_BITS` / `ACCESS_GPU_BITS_BONUS`.
4. **Real-solve check** — actually mines random prefixes at a chosen difficulty for
   N samples and reports median/min/max and timeouts, to validate the projection
   against real run-to-run variance.

Every run is logged server-side to `logs/bench.log` (endpoint `POST /bench/log`).

## Findings

### 1. The "GPU" path is multi-lane WebCrypto, not a GPU

`mineAccelerated` runs several async lanes over `crypto.subtle.digest`. There is no
WGSL/compute-shader hashing. So "GPU available" only changes _how many lanes_ run,
not the underlying primitive.

### 2. Accelerated ≈ single-lane on browser hardware

Because WebCrypto already dispatches digests to the browser's internal thread pool,
adding JS "lanes" mostly re-queues the same work. Observed speedup was ~1×, i.e.
negligible. This matches the qualitative report that **CPU and GPU both resolve at
~19 bits**.

### 3. WebGPU indicator did not light up

On the test machine WebGPU either isn't exposed or provides no speedup, so the
"accelerated" tier is effectively identical to CPU. This is expected given (1).

### 4. Difficulty ↔ time relationship

Expected effort is `2^bits` hashes; expected time ≈ `2^bits / hashrate`. Every extra
zero-bit doubles expected time. _(Illustrative)_ at ~40 kH/s single-lane:

| Zero-bits | Expected hashes | ~Time @ 40 kH/s |
|-----------|-----------------|-----------------|
| 17        | 131,072         | ~3.3 s          |
| 18        | 262,144         | ~6.6 s          |
| 19        | 524,288         | ~13 s           |
| 20        | 1,048,576       | ~26 s           |
| 24        | 16,777,216      | ~7 min          |

The 24-bit GPU target is impractical for the current WebCrypto path — it will
essentially always time out and fall back to CPU. This is the single most important
practical finding: **the current GPU tier mostly just adds a wait before fallback.**

> These are placeholder rates. Real numbers require a `/bench` run on the target
> machine; they will be captured in `logs/bench.log`.

## Current configuration (as of this report)

| Setting | Value | Meaning |
|---------|-------|---------|
| `ACCESS_ZERO_BITS` | 19 | CPU (easy) target |
| `ACCESS_GPU_BITS_BONUS` | 5 | GPU = 19 + 5 = **24** |
| `ACCESS_MAX_SECONDS` | 30 | CPU attempt budget |
| GPU attempt budget | `max(30, 60)` = 60 s | hard-job wall time before fallback |

## Recommendations

1. **Capture real data first.** Run `/bench` a few times on representative machines;
   read `logs/bench.log`. Decide from measured single-lane and accelerated H/s.
2. **Likely: collapse to one tier.** If accelerated ≈ single-lane (as observed), set
   `ACCESS_GPU_BITS_BONUS=0` (or drop the GPU negotiation) so every client gets one
   fair, quick target. This removes the always-times-out-then-falls-back GPU wait.
3. **If keeping a split,** make the bonus small (1–3 bits) so the hard job can
   actually finish within budget on machines that do show a speedup.
4. **Only justify a large GPU gap** once a true WGSL SHA-256d miner lands — then the
   GPU path could genuinely do orders of magnitude more hashes and a bigger bonus
   would make sense.

## Related fixes shipped this session

- **Fallback bug fixed:** a failed GPU attempt (no solution, rejected share, or
  network error) now falls back to CPU instead of erroring; only the final attempt
  surfaces an error. Per-attempt UI stats reset on fallback.
- **In-place "New meme":** unlocking mines on the same page and swaps the meme via
  `POST /meme/next` (JSON), no bounce back to the paywall. Security unchanged —
  receipts remain single-use and media is served only via per-receipt tokens.
- **Refresh fallback:** re-POSTing `/meme/unlock` (page refresh with a spent receipt)
  now `303`-redirects to the main page instead of showing "Settle failed".
- **Meme set expanded:** 2,084 memes (311 Phneep posters + 1,773 Rare Pepes) fetched
  via `scripts/fetch_memes.py`; images are gitignored and refetched on deploy.
- **Benchmark tooling:** `/bench` page + `logs/bench.log` logging; fixed a UI lock-up
  by yielding to the event loop during hashing.

## Next steps

- [ ] Run `/bench` on the real machine(s); collect single-lane + accelerated H/s.
- [ ] Pick a target solve time (e.g. 6 s) and set `ACCESS_ZERO_BITS` from the tool's
      recommendation.
- [ ] Decide GPU tier: collapse (`bonus=0`) vs small bonus, based on measured speedup.
- [ ] Re-run to confirm real solve times match the projection.
