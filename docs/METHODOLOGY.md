# Technical methodology

This is the detailed companion to the [README](../README.md). The README explains how to use the program. This page says exactly what is calculated, which numbers decide a verdict, how the program was checked, and what it does not establish. Everything here describes the code as it is, so a result can be reproduced and challenged.

**In one sentence:** the program compares two antennas by asking the *same receivers* about both, which cancels each receiver's own noise and path, and then it is careful about how sure it can be.

## 1. What data goes in

| Source | What is used |
| --- | --- |
| WSPR (wspr.live) | Each report: time, receiver, receiver position, SNR, transmit power (dBm), frequency. A receiver that decodes the same transmission twice is counted once, at its strongest decode. |
| Reverse Beacon Network | The day's public history file, filtered to your callsign. Skimmer positions come from a skimmer list. A skimmer at the same site with a "-1", "-2" suffix is a separate skimmer. |
| Pasted rows | Spots copied from reversebeacon.net, read the same way. |

- All times are UTC. A day that has ended is remembered on disk so it is fetched once (wspr.live allows 20 requests a minute for the whole site). Anything from the last hour is always fetched fresh.
- Spots less than about 5 minutes old are flagged as possibly incomplete, because wspr.live fills a time slot in over several minutes.
- A receiver's position comes from the spot itself (WSPR) or the skimmer list (RBN). A receiver with no position still counts for reach but cannot be placed on a map or in a direction.

**Who was listening.** Many WSPR receivers hop between bands, and only listen to a band in some time slots. If A's transmissions land in a receiver's listening slots and B's do not, that receiver "only hears A" because of its schedule, not the antenna. So for a time-based WSPR test the program asks wspr.live which receivers decoded *anything* on that band in each slot, and keeps only receivers that were active in at least two of A's slots and at least two of B's. Two limits: decoding another station is evidence a receiver was listening, but not decoding anything is not proof it was not, and if wspr.live's list is cut off at its row limit the program says so and does not use it to drop anyone. The program also warns when a slot has far fewer active receivers than usual (below a quarter of the median, on a band with a median of at least 100), which is a known gap in some old wspr.live data.

**Left out of the comparison**
- A transmission heard by 2 or fewer receivers when ordinary ones are heard by 16 or more is treated as a skipped cycle (usually one receiver with a wrong clock).
- An A or B transmission with no partner on the other antenna is left out of the comparison, though it still shows on the maps.
- Optional filters you can set: reports at or below -27 dB (WSPR's decoding edge), receivers closer than a distance you choose, and receivers where more than a chosen share of reports sit at the floor.

## 2. How A and B are told apart and paired

- **By frequency (RBN):** spots are grouped by nearby frequencies, and you say which group was A and which was B.
- **After every transmission (WSPR):** transmissions are labelled A, B, A, B or A, B, B, A. A **round** is an A transmission and the B one next to it. Neighbouring transmissions share nearly the same propagation, so drift mostly cancels.
- **In long blocks (WSPR):** one antenna for a set time, then the other. Nothing is paired; each receiver's reports on A and on B are compared as a whole.
- **Equal time on the air:** if one side was on the air more than 15% longer, the longer side is thinned at random (with a fixed seed, so the answer is repeatable) to match.
- **Sequential tests:** when A and B ran one after the other (separate frequencies or time windows more than 10 minutes apart, mostly unpaired transmissions, or fewer than 4 blocks), the verdict says changing conditions could explain a difference.

## 3. What is being estimated

For every receiver that heard both antennas, the program works out that receiver's **relative gain**, B minus A in dB. Three kinds of number come out:

1. **Strength.** At each receiver, the typical difference between the antennas. In the paired method that is the median of the A-minus-B differences over rounds. In the long-block method each antenna's reports are turned into power, averaged, and turned back:

   `G = 10 log10( mean(10^(B/10)) / mean(10^(A/10)) )`

   The headline strength number is the mean of these receiver values, each receiver counting once.
2. **Reach.** How many receivers heard each side, and how many heard only one.
3. **Direction and distance.** Receivers are grouped into eight 45-degree sectors and distance bands, and the average relative gain is worked out in each.

The two strength methods do not always use the same observations. The paired method uses receivers that decoded both antennas in a matched pair. The linear-power method can use every decoded transmission, including ones with no partner. When the two disagree, part of the reason can be the different set of observations and not only the different arithmetic.

**Every receiver callsign counts on its own, including several at one site.** That matches how the published method counts stations, but it has a consequence: a site with five receivers gets five votes, and receivers at one site share the same path and noise, so they are not fully independent. Where several receivers share a site, the stated range can be narrower than the real uncertainty. A "one vote per site" sensitivity check is on the list of future work.

## 4. How the uncertainty is worked out

| Quantity | Method |
| --- | --- |
| Strength range, paired tests | 95% bootstrap range of the receiver average (4,000 resamples of the receivers), then widened for fading at the transmitting end (below) |
| Strength range, unpaired tests, fewer than 40 receivers | Student's t interval, because a bootstrap on a few receivers comes out too narrow |
| Per-receiver gain with at least 3 reports on each antenna | Welch's unequal-variance interval on the two means |
| A direction or distance band | 99% bootstrap range (about 14 bands are checked at once), counted only with at least 4 receivers and a difference of more than 2 dB |

**Fading at the transmitting end.** Fading moves every receiver at once, so counting receivers as independent flags differences that are only fading. For paired tests, the range is widened: with 4 or more rounds, by the measured round-to-round spread of the averages, and with fewer rounds by 0.85 dB per pair (the spread of identical-antenna pairs measured on real WSPR data, 0.84 dB over 43 pairs). The final range is the wider of the receiver-based range and this one. That is a practical correction, not a full hierarchical resampling of receivers and rounds, and the program does not claim it is one.

## 5. How a verdict is decided

**Strength** (with `N` receivers that heard both, `lo` and `hi` the range, `mean` the average):
- Fewer than 5 receivers: not enough to judge.
- Range excludes zero and the average is 1 dB or more: that side is stronger.
- Range excludes zero but the average is under 1 dB, or the whole range sits inside plus or minus 1 dB: "no meaningful difference". When the range excludes zero, the page names a small lead and says it is too little to matter.
- Otherwise: inconclusive, with the range written out in plain words ("anywhere from A ahead by x dB to B ahead by y dB").

The 1 dB line is a practical convention chosen for this program, not a statistical property of antennas. A lead under 1 dB can matter to a contest operator and not to a portable one. And a range that extends past 1 dB does not prove the antennas are equal, which is why the wording describes the lead and not equivalence.

**Reach**
- Unpaired: an exact sign test on receivers that heard only A against only B, needing at least 4 one-sided receivers.
- Paired: an exact sign (McNemar) test over rounds where one side was heard by more receivers, needing at least 6 such rounds. A paired test judges reach by round, because counting receivers as independent would turn fading into a false winner.
- A side is said to be "leaning" ahead when the lean is at least 60% likely to be real.

**Farthest receiver.** A side is called farther only when its farthest receiver is at least 25% and 1,000 km farther, and the test has at least 6 decisive rounds. A single far receiver can be luck, so a short test with very different distances says it is too short to tell. Beside it the page shows a plain count of receivers beyond 5,000 miles for each side.

## 6. How it was checked, and what that does not show

**Against Dale's study (N3HXZ, long blocks).** With his transmitter, date and schedule, the program's relative gain matches his table on the 27 stations of the first version of his table: 0.00 dB difference on average and 0.12 dB at most at any station. The 500 to 1,500 mile average was 3.87 dB against 3.86 dB. The program also counted four more stations that met his 40-spot rule; he traced that to a copy and paste slip in his spreadsheet and corrected his paper. This shows the linear-power calculation is implemented correctly. It does **not** show that the confidence ranges are right, that the short paired method works, or that a real antenna difference is recovered under changing conditions. The automated check is `tests/test_dale_study.py` (it needs the internet, so it only runs when `RBN_TEST_NETWORK=1` is set).

**Same antenna as A and B ("null" tests).** Two checks, both on real WSPR data where the true difference is zero.

*A day of WW0WWV on 20m*, cut into 47 overlapping one-hour windows (roughly 25 independent cases), with the same transmitter treated as A and as B each time. How often the program named a winner:

| Test shape | False winner |
| --- | --- |
| 2 blocks of 30 min | 5 of 45 (11%) |
| 4 blocks of 15 min | 2 of 47 (4%) |
| 6 blocks of 10 min | 2 of 46 (4%) |
| 12 blocks of 5 min | 2 of 47 (4%) |
| 30 transmissions, A, B, A, B | 2 of 47 (4%) |
| 30 transmissions, A, B, B, A | 0 of 47 |

A 95% range should be wrong about 5% of the time, so most shapes are at the expected rate, two blocks is the weak spot, and A, B, B, A did best. Typical gaps between identical antennas were 0.2 to 0.7 dB, but some windows showed 2 to 5 dB when the band was moving.

*Dale's single-antenna run (N3HXZ, 40m, 25 October 2026, 30 two-minute transmissions).* Both A, B, A, B and A, B, B, A gave "too close to call", with the average gap about 0.6 dB (range -0.1 to +1.3 dB), and the receiver-by-receiver averages stayed within about 1 dB as the minimum reports rose. A 6-block test of 10 minutes gave a false "B stronger by 2.4 dB" on the same data, because the band changed between blocks and every receiver moved together.

What these show: the paired method behaves close to its stated 95% and the long-block method with few blocks does not. What they do not show: one station, one band and one day each, with overlapping windows, so they are a good indication, not a calibration. A proper coverage study would repeat this over many days, bands and stations.

**Band conditions against RBN.** See the README section "Band conditions now". In short, predicted signal margins were compared with what skimmers actually reported for four stations' activations, and the typical error was about 7 dB (6 dB within 1,000 miles, 9 dB at 1,000 to 2,000 miles, no DX paths in the sample).

## 7. The band conditions model

For each WSPR report near you, the program restates the SNR for your power and mode:

`estimated SNR = reported SNR + 10 log10( your power / the reporting station's power ) + your antenna adjustment`

and works out the **margin** over the mode's threshold (2,500 Hz reference bandwidth): CW -5 dB, SSB +8 dB, FT8 -21 dB, WSPR -28 dB.

- **The percentage** is `Φ(margin / 8 dB)`, where Φ is the standard normal curve and 8 dB is the spread of prediction errors. The 8 dB is a rounded engineering figure, informed by the RBN comparison above (7.4 dB overall), not a fitted statistic. A margin of zero is a coin flip and +8 dB is about 84%.
- **It is not the chance of an RBN spot.** Skimmers report less often than this estimate suggests, which the README quantifies. Treat the percentages as "decodable under the model".
- **Regional percentages** are averages over the receivers actually seen in each band and region. They describe those receivers, not every receiver in the region.
- **"Power needed"** uses the 75th-percentile margin among a region's receivers, so it describes the power to reach a fairly favourable receiver, not a typical one.
- **Cells with fewer than 3 receivers** show only a count, because the receivers that reported anything are the lucky exceptions and a figure from one or two would look far too easy.
- **Band tiles:** "Open" needs at least 3 receivers rated good, "Marginal" needs at least 2 rated good or marginal, and otherwise "Quiet". These words describe the evidence in the WSPR sample, not an absolute state of the ionosphere.
- **Space weather and MUF.** The sun readings come from NOAA and are described as tendencies, not forecasts for a band. The MUF comes from ionosonde data (GIRO, republished by KC2G) and is an estimate from the nearest working sounders, not a measurement along your path.

## 8. Known limitations

- Receivers at one site count separately and can make the stated range too narrow.
- The intervals have been checked against same-antenna data, not calibrated over many independent experiments.
- Tests with very few blocks (especially 2) are the weakest, because the band can change between turns.
- WSPR only reports what a receiver decoded, so a weaker antenna is decoded less often and a decoded-only comparison can look slightly better than the antenna is.
- Whether a receiver was listening is inferred from it decoding other stations.
- The 1 dB threshold, the 25% and 1,000 km farthest-reach rule, and the 8 dB band-conditions spread are conventions, chosen and explained, not measured constants.

## 9. The numbers in one place

| Setting | Value |
| --- | --- |
| Significance level for "a real difference" | 0.05 |
| Practically equal | within plus or minus 1 dB |
| Minimum receivers that heard both, to judge strength | 5 |
| Bootstrap resamples (main range) | 4,000 |
| Direction or distance band: minimum receivers, minimum difference, range level | 4, 2 dB, 99% |
| Pair-to-pair fading spread used for few rounds | 0.85 dB |
| Decisive rounds needed to call reach (paired) | 6 |
| One-sided receivers needed to call reach (unpaired) | 4 |
| Reach called "leaning" at | 60% likely real |
| Farthest-reach call | at least 25% and 1,000 km farther |
| "Far" receiver count | beyond 5,000 miles |
| Equal-time tolerance | 15% |
| Weak-report floor (optional filter) | -27 dB |
| Band conditions prediction spread | 8 dB |
| Region needs this many receivers for a percentage | 3 |

Questions and corrections are welcome. This page and the code in `compare_stats.py` and `conditions.py` should always agree, and if they do not, the code is the truth and the page is a bug.
