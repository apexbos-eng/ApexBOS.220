# ApexBOSGrabber: High-Performance 220 MHz SDR Receiver & Demodulator

> [!IMPORTANT]
> **Disclaimer & Intended Use**: **ApexBOS is NOT a real Positive Train Control (PTC) system and is not affiliated with any railroad or government agency.** ApexBOS is an experimental reference tool designed for amateur train enthusiasts and radio telemetry hobbyists. It attempts to decode 220 MHz digital telemetry packets to facilitate hobbyist research and visualize the status of elements defined on a layout. It must never be used for train movement control, safety-critical railway operations, or commercial dispatching.

**ApexBOSGrabber** is a multi-channel Software-Defined Radio (SDR) receiver, physical-layer demodulator, forward error correction (FEC) decoding engine, and real-time telemetry streaming daemon designed for amateur reception and analysis of 220 MHz digital railway telemetry communications.

ApexBOSGrabber simultaneously monitors multiple RF channels across the 220–222 MHz band, extracts $\pi/4$-DQPSK bursts, performs Viterbi and Reed-Solomon decoding, validates CRC integrity, dynamically corrects frequency offsets, and publishes decoded messages via ZeroMQ PUB sockets and UDP multicast for ingestion by the ApexBOS Dispatcher Engine and ApexBOSFlux network relays.

---

## Dashboard Overview

ApexBOSGrabber features an interactive ANSI terminal dashboard providing live RF metrics, sliding-window decoding health, per-process worker loads, and real-time decoded packet inspection:

![ApexBOSGrabber Main Dashboard](images/GrabberMainScreen.png)

The dashboard is structured into six functional zones:
1. **Hardware & RF Header**: Tuner frequency, sample rate, gain, bias-tee, and SDR source.
2. **I/Q Flow & Pipeline Health**: Sample ingestion throughput, burst count, peak RSSI, and active channel list.
3. **Pipeline Detection & Decoding Integrity**: Big-matrix failure rate indicator, sliding-window FEC health, and sync/packet statistics.
4. **Worker Process Distribution Table**: Multi-core load distribution, assigned channels, port allocations, and per-worker throughput.
5. **Recent Decoded Packets & Telemetry**: Real-time packet scrolling with RSSI, SNR, frequency error ($\Delta f$), and decoded telemetry metadata.
6. **Status Bar & Interactive CLI**: Live status updates and on-the-fly hardware/channel configuration controls.

---

## Operating Modes & SDR Ingestion

ApexBOSGrabber supports six input architectures—covering RTL-SDR dongles, **SDRplay RSP receivers (RSP1B, RSP1A, RSPdx, RSPduo)**, and generic **SoapySDR** hardware (Airspy, HackRF, LimeSDR, PlutoSDR)—selectable via CLI flags or automatically discovered at launch:

```mermaid
flowchart TD
    subgraph Sources["SDR Input Sources"]
        S1["RTL-SDR Subprocess Pipe (--pipe)"]
        S2["RTL-TCP Network Client (--rtl-tcp)"]
        S3["Direct Python rtlsdr (--rtlsdr)"]
        S4["SDRplay RSP-TCP (--rsp-tcp / -E 16-bit)"]
        S5["SoapySDR CS16 (--soapy / SoapySDRPlay3)"]
        S6["Standard Input Stream (--stdin)"]
    end

    subgraph Core["ApexBOSGrabber Reader Process"]
        R1["Reader Thread / Ingestion Buffer (u8 & s16)"]
        R2["Hardware Auto-Tuner & AFC Engine"]
        R3["Dynamic Channel Allocator + Chroma XPUB Tap"]
    end

    subgraph Workers["Worker Demodulator Processes"]
        W0["Worker #0 (Demod + Viterbi + RS + CRC)"]
        W1["Worker #1 (Demod + Viterbi + RS + CRC)"]
        W2["Worker #2 (Demod + Viterbi + RS + CRC)"]
        W3["Worker #3 (Demod + Viterbi + RS + CRC)"]
    end

    subgraph Outputs["Network Outputs"]
        ZMQ["ZeroMQ PUB (Ports 18071..18131)"]
        UDP["UDP Broadcast / Multicast"]
        ENG["ApexBOS Dispatcher Engine / Chroma"]
    end

    S1 --> R1
    S2 --> R1
    S3 --> R1
    S4 --> R1
    S5 --> R1
    S6 --> R1

    R1 --> R2
    R1 --> R3

    R3 -->|Chunked I/Q Banks| W0
    R3 -->|Chunked I/Q Banks| W1
    R3 -->|Chunked I/Q Banks| W2
    R3 -->|Chunked I/Q Banks| W3

    W0 --> ZMQ
    W1 --> ZMQ
    W2 --> ZMQ
    W3 --> ZMQ

    W0 --> UDP
    W1 --> UDP
    W2 --> UDP
    W3 --> UDP

    ZMQ --> ENG
    UDP --> ENG
```

### 1. Pipe Mode (`--pipe`) - Default
Spawns the native `rtl_sdr` binary as an optimized subprocess, passing raw I/Q samples directly through `stdout` using a 256 KB memory pipe. This provides low overhead and zero memory copies on both Linux and Windows.

### 2. RTL-TCP Mode (`--rtl-tcp`)
Connects over TCP (`127.0.0.1:1234` by default) to an active `rtl_tcp` server. Enables running the SDR receiver on a remote Raspberry Pi, server, or container while running Grabber on a workstation. Socket buffers are tuned to 2 MB to prevent drops under high burst rates.

### 3. Direct RTLSDR Mode (`--rtlsdr`)
Communicates directly with the USB dongle inside the Python process using `pyrtlsdr` (`ctypes` binding to `librtlsdr`). Operates on Linux, macOS, and Windows (with `rtlsdr.dll` and WinUSB driver). Employs 16 KB USB bulk-transfer alignment.

### 4. SDRplay RSP-TCP Mode (`--rsp-tcp`, `--sdr-mode rsptcp`)
Connects to SDRplay's official **`rsp_tcp`** server (`SDRplay/RSPTCPServer`) for **RSP1B, RSP1A, RSPdx, and RSPduo** receivers (requires official **SDRplay API v3.15+** for RSP1B):
- **Standard 8-bit Mode** (`--rsp-tcp`): Compatible with any standard `rsp_tcp` instance (`rsp_tcp -a 127.0.0.1 -p 1234 -T`). Automatically enforces SDRplay native ADC sample rates $\ge 2.112\text{ MS/s}$ (multiples of $192\text{ kS/s}$, e.g. `2.304 MS/s` for full Ch 97–176 coverage).
- **Extended 16-bit Mode** (`--rsp-tcp --rsp-16bit`): Auto-detects the 45-byte `RSP0` extended capability handshake from `rsp_tcp -E -b 16`, streams full **14-bit ADC dynamic range** over signed 16-bit `CS16` (`s16`) samples with zero impact on RTL-SDR performance, and maps Grabber gain, AGC, LNA state (`--rsp-lna-state`), 4.7 V Bias-T (`0x25`), antenna port (`--rsp-antenna`), and broadcast notch filters (`--rsp-notch`) to native `rsp_tcp` extended commands (`0x1F`–`0x25`).
- **Auto-Launch**: If no `rsp_tcp` server is listening locally on `127.0.0.1`, Grabber can automatically launch `rsp_tcp` (from `--rsp-tcp-bin`, `PATH`, or standard SDRplay install folders) and stop it cleanly on exit:
```bash
python ApexBOSGrabber.py --rsp-tcp --rsp-16bit --rate 2304000 --chans 101,102
```

### 5. SoapySDR Mode (`--soapy`, `--sdr-mode soapy`)
Native direct-USB 16-bit `CS16` (`s16`) streaming via Python **SoapySDR** bindings—unlocking **SDRplay RSP1B / RSP1A / RSPdx / RSPduo** (via `SoapySDRPlay3` + SDRplay API v3.15+), **Airspy R2 / Mini**, **HackRF One**, **LimeSDR**, **PlutoSDR**, and **RTL-SDR** without any external helper server:
```bash
# SDRplay RSP1B via SoapySDRPlay3:
python ApexBOSGrabber.py --soapy --soapy-args "driver=sdrplay" --rate 2304000 --chans 101,102

# RTL-SDR or Airspy via SoapySDR:
python ApexBOSGrabber.py --soapy --soapy-args "driver=rtlsdr" --chans 101,102
```
*Note for Windows users*: Because PyPI does not publish official Windows `SoapySDR` wheels for Python 3.12–3.14, `--soapy` is easiest on Linux, macOS, or Windows Conda-Forge/PothosSDR environments; Windows users running standard Python should prefer **`--rsp-tcp`**.

### 6. Stdin Mode (`--stdin`)
Reads raw interleaved I/Q samples (`--sample-format u8` 8-bit unsigned by default, or `--sample-format s16` 16-bit signed `CS16`) from standard input, enabling playback of pre-recorded RTL-SDR or RSP1B RF captures or piping from tools like `gqrx`, `hackrf_transfer`, or `airspy_rx`:
```bash
rtl_sdr -f 220487500 -s 960000 - | python ApexBOSGrabber.py --stdin
# Replay a 16-bit CS16 SDRplay recording:
python ApexBOSGrabber.py --stdin --sample-format s16 --rate 2304000 < rsp1b_221mhz.cs16
```

### 7. Automatic Source Detection
When launched without explicit mode flags, Grabber automatically probes available interfaces:
1. Checks if `rtl_sdr` executable is in `PATH` (uses `--pipe` if found).
2. Checks if an `rtl_tcp` / `rsp_tcp` server is listening on port `1234` (engages TCP mode if active).
3. Checks if `pyrtlsdr` is installed and operational (engages `--rtlsdr`).
4. Checks if `rtl_tcp` binary is installed in `PATH`.
5. Checks if SDRplay `rsp_tcp` binary is installed (engages `--rsp-tcp`).
6. Checks if `SoapySDR` Python bindings are installed (engages `--soapy`).
7. If no source is found, prints a comprehensive setup guide and exits gracefully.

---

## Demodulation & Decoding Concepts

### 1. RF Channelization & Bandwidth Scaling
220 MHz telemetry channels operate across designated 25 kHz channel spacings in the 220–222 MHz frequency band:
$$f_c = 217{,}587{,}500\text{ Hz} + (\text{channel} \times 25{,}000\text{ Hz})$$

When the requested channel selection spans more than the base sample rate (192 kS/s), Grabber automatically calculates the optimal center frequency and scales the hardware sample rate to a multiple of 192 kHz (e.g. 960 kS/s or 1.92 MS/s) to cover all channels in a single SDR tuner passband:
$$f_{\text{center}} = \frac{\min(f_c) + \max(f_c)}{2}$$

### 2. Digital Downconversion (DDC) & Channel Extraction
For each active channel assigned to a worker:
1. The raw hardware I/Q stream is frequency-shifted to baseband by mixing with a complex digital local oscillator:
   $$x_{\text{base}}(t) = x_{\text{in}}(t) \cdot e^{-j 2\pi (f_c - f_{\text{center}}) t}$$
2. The signal is decimated to the symbol processing rate.
3. A matched Root-Raised-Cosine (RRC) filter ($\alpha = 0.2$) is applied to maximize signal-to-noise ratio and eliminate inter-symbol interference (ISI).

### 3. $\pi/4$-DQPSK Demodulation
ITC transmissions use $\pi/4$-DQPSK modulation at 16 kbaud (32 kbps Full Rate / 16 kbps Half Rate).
Consecutive symbols $S_k$ encode dibits into phase changes:
$$\Delta\theta_k = \arg(S_k \cdot S_{k-1}^*)$$

| Dibit ($b_0 b_1$) | Phase Shift ($\Delta\theta_k$) |
| :---: | :---: |
| `00` | $+45^\circ \; (+\pi/4)$ |
| `01` | $+135^\circ \; (+3\pi/4)$ |
| `11` | $-135^\circ \; (-3\pi/4)$ |
| `10` | $-45^\circ \; (-\pi/4)$ |

### 4. Synchronization & Unique Word (UW) Correlation
Transmissions begin with a 64-bit unique word synchronization signature. Grabber continuously evaluates correlation against both normal and polarity-inverted bit patterns. Bursts with Hamming distance $\le 8$ (configurable with `-E`) are locked and aligned for FEC decoding.

### 5. Forward Error Correction (FEC)
1. **Viterbi Convolutional Decoding**:
   - Rate: $R = 1/2$, Constraint Length: $K = 7$.
   - Polynomial generator: $(171_8, 133_8)$.
   - De-interleaved and de-scrambled via a 3-bit LFSR polynomial.
   - Decodes the packet header containing length, modulation mode, and packet type.
2. **Reed-Solomon Block Error Correction**:
   - Code: $RS(16, 12)$ over $\text{GF}(2^4)$ with primitive polynomial $x^4 + x + 1$.
   - Each codeword consists of 12 data nibbles and 4 parity nibbles.
   - Capable of detecting and correcting up to 2 symbol errors per 16-nibble block.
3. **CRC-16/ARC Payload Verification**:
   - Polynomial: $x^{16} + x^{15} + x^2 + 1$ (`0xA001`, reversed `0x8005`).
   - Ensures zero undetected bit errors before forwarding packets to downstream clients.

### 6. Frame Formatting, Deduplication & Outbound Control-Packet Filter
Packets passing CRC validation are formatted into the standard ApexBOSFlux protocol frame:
```
[ 0x49 ('I') ] [ Length (2 bytes BE) ] [ Packet Type (1 byte) ] [ Flags (0x00) ] [ Payload... ]
```
Because locomotives and base stations often transmit repeatedly or across overlapping channels, an adaptive deduplication filter suppresses identical frames seen within the configured expiration window (`--dedup-ttl`, default: 1.0s).

#### Optional Outbound Control-Packet Filter (Default: ON)
By default (`--filter-ctrl` / `filter_ctrl: true`), ApexBOSGrabber suppresses high-volume, non-layout housekeeping packets—**`QSTAT/ACK` (`0x01`)**, **`ACQ` (`0x04`)**, and **`CNTL` (`0x05`)**—from being forwarded over its outbound ZeroMQ `PUB` and UDP feeds to Flux, Engine, and Spy, significantly reducing downstream network and CPU noise:
- **Still Counted in Grabber Statistics**: Filtered packets remain fully counted in `Packets`, `PPM`, worker tables, and recent packet logs (tagged with a dim `[FLT]` badge).
- **Always Displayed on ApexBOSChroma**: When [ApexBOSChroma](CHROMA_README.md) is connected, Grabber mirrors **every** demodulated packet—including filtered `0x01`/`0x04`/`0x05` frames—over its gated `b"PKT"` XPUB channel (`18099`), so Chroma's waterfall always shows every live RF burst accurately.
- **Toggle Anytime**: Disable at launch via `--no-filter-ctrl` (aliases `--no-filter`, `--send-all`), set `"filter_ctrl": false` in `config.json`, or toggle live in the console via `filter off` / `filter on`.

#### Automatic Terminal ANSI Detection & Plain-Text Fallback
On startup, Grabber probes stdout/console VT100 support via `config_loader.init_terminal_mode()`. If run in a non-ANSI environment (such as Windows Legacy Console, `TERM=dumb`, `NO_COLOR=1`, or redirected log pipes), Grabber automatically switches to clean plain-text scroll mode (`--no-tui --no-color`) so logs stay readable with zero garbled escape sequences.

### 7. Signal Power, dBFS Calibration & ADC Clipping Detection
ApexBOSGrabber evaluates RMS signal power for incoming RF bursts and raw I/Q sample chunks in decibels relative to full scale:
$$P_{\text{dBFS}} = 10 \log_{10}\left(\frac{\langle I^2 + Q^2 \rangle}{127.5^2}\right)$$

#### Why Positive dBFS Numbers Occur ($\ge 0.0\text{ dBFS}$)
The RTL-SDR hardware digitizes RF signals with an 8-bit ADC, producing unsigned values from $0$ to $255$. Centering around DC yields $[-127.5, +127.5]$ for both $I$ (in-phase) and $Q$ (quadrature) components:
- The denominator $127.5^2$ represents the power of a circular full-scale continuous-wave (CW) sinusoid ($I^2 + Q^2 = 127.5^2$), mapping to exactly $0.0\text{ dBFS}$.
- However, the ADC physical clipping boundary is a 2D square in the complex plane: $[-127.5, +127.5] \times [-127.5, +127.5]$.
- When incoming RF bursts saturate the receiver front-end, both $I$ and $Q$ clip simultaneously against the rails:
  $$I_{\max}^2 + Q_{\max}^2 = 127.5^2 + 127.5^2 = 2 \times 127.5^2$$
- Evaluating this in the dBFS formula yields:
  $$10 \log_{10}\left(\frac{2 \times 127.5^2}{127.5^2}\right) = 10 \log_{10}(2) \approx +3.01\text{ dBFS}$$

#### Positive RSSI as a High-Gain & Clipping Indicator
In ApexBOSGrabber, positive dBFS numbers (such as `+1.5 dBFS` in the dashboard screenshot) are **intentionally preserved** as an explicit diagnostic indicator that the tuner gain is set too high for incoming transmitter bursts, driving the ADC converter into digital saturation.

#### Dynamic 60-Second Sliding Window & TUI Alerts
1. **60-Second Sliding Window Decay**: Rather than latching an all-time maximum from process startup, `Peak RSSI` tracks the highest burst level over a rolling 60-second window. Momentary startup spikes or single-burst transients automatically expire after 60 seconds of clean operation.
2. **Visual TUI Clipping Warning**: When `Peak RSSI >= 0.0 dBFS`, the dashboard immediately displays high-visibility visual alerts:
   - Header bar tags the gain in bold red: `Gain: 42.0 dB [CLIP: GAIN HIGH]`.
   - Global metrics line highlights Peak RSSI in red: `Peak RSSI: 1.5 dBFS [CLIPPING: Gain may be set too high!]`.
   - A dedicated warning banner appears: `[!] ADC CLIPPING DETECTED (Peak RSSI >= 0.0 dBFS): Gain may be set too high! Use 'g <gain>' to reduce.`
3. **Instant Peak RSSI Reset on Gain Adjustment**: Whenever tuner gain is adjusted (e.g. typing `g 38.0` or `g auto`), or when the auto-tuner modulates gain, all peak RSSI tracking buffers across the application, sliding windows, and worker processes are immediately reset to `-100.0 dBFS`, instantly clearing the clipping alert.

#### Troubleshooting ADC Clipping & Persistent Positive dBFS Alerts
If the `[CLIPPING: Gain may be set too high!]` alert appears persistently on the dashboard or will not clear, use the following guide to diagnose and resolve the issue:

1. **Beware the "0.01" Gain Trap (Accidental Hardware AGC)**:
   - Tuner gain stages are discrete hardware steps, not continuous analog values:
     $$\text{Supported Steps (dB): } [0.9,\; 1.4,\; 2.7,\; 3.7,\; 7.7,\; 8.7,\; 12.5,\; 14.4,\; \dots,\; 48.0,\; 49.6]$$
   - In `librtlsdr` and RTL-SDR hardware drivers, passing gain `0` or `0.0` is the reserved instruction that commands the hardware to enable **Automatic Gain Control (AGC)**; it does *not* set 0 dB manual gain.
   - When entering fractional values like `g 0.01` or `g 0.0`, the value rounds to 0 tenths of a dB (`0`), which actually activates full Hardware AGC!
   - Under AGC mode, the tuner automatically pumps its internal LNA and mixer gain up to maximum ($\approx 40\text{--}49\text{ dB}$) during quiet intervals, guaranteeing hard ADC saturation whenever RF bursts arrive.
   - **Resolution**: To test minimum manual gain, always set the lowest discrete hardware step:
     ```text
     g 0.9
     ```
     *(or `g 1.4`)*. Setting `g 0.9` forces the tuner out of AGC and locks it into minimum manual hardware amplification.

2. **Bias-Tee (4.5V DC) Short Circuit on DC-Grounded Antennas**:
   - ApexBOSGrabber enables the internal Bias-Tee by default (`Bias: ON`, `--bias-tee 1`) to power inline low-noise pre-amplifiers (LNAs).
   - Many passive VHF antennas (such as 5/8-wave whips with base matching coils, folded dipoles, J-poles, or magnetic loops) present a **direct DC short circuit to ground** at the coaxial connector by design.
   - Connecting a DC-grounded antenna while the Bias-Tee is active shorts the +4.5V power rail directly into RF ground. This pulls down the internal voltage rails of the Rafael Micro tuner chip and drives the I/Q mixer outputs directly into the ADC digital supply rails ($0$ and $255$), producing permanent digital clipping and positive dBFS on all channels regardless of signal presence or gain setting.
   - Alternatively, if the antenna has an active inline LNA, the amplifier may be injecting $+20\text{ dB}$ to $+35\text{ dB}$ of amplification before the signal reaches the SDR, severely overdriving the 8-bit converter.
   - **Resolution**: Disable the Bias-Tee in the interactive TUI:
     ```text
     b 0
     ```
     *(or start with CLI flag `-b 0` / `--bias-tee 0`)*. If the clipping alert clears and signals return to normal negative dBFS, the antenna was DC-grounded or over-amplified.

3. **Step-by-Step Diagnostic Routine**:
   When the clipping alert triggers, follow this sequence:
   1. **Disable Bias-Tee**: Type `b 0`.
   2. **Set Lowest Manual Gain**: Type `g 0.9` (do not use `0` or `0.01`).
   3. **Check Peak RSSI**: Confirm that Peak RSSI drops to normal negative values (e.g. $-10\text{ dBFS}$ to $-50\text{ dBFS}$) and the clipping alert clears.
   4. **Step Gain Up Gradually**: Increment gain in steps (e.g. `g 14.4`, `g 25.4`, `g 36.4`) until desired reception sensitivity is achieved without re-triggering the clipping alert on active bursts.

4. **Out-of-Band RF Overload & Recommended Filtering**:
   The RTL-SDR features an 8-bit ADC with only $\approx 48\text{ dB}$ of effective dynamic range and wide, unshielded front-end filters. High-gain outdoor or rooftop antennas capture massive total RF energy across the entire spectrum. If clipping persists even with `b 0` and `g 0.9`, strong out-of-band transmitters are saturating the front end before digital channelization:
   - **High-Power Broadcast FM (88–108 MHz)**: 50–100 kW commercial FM radio towers.
   - **VHF Commercial Paging / Public Safety (150–174 MHz)**: High-power local simulcast transmitters (especially 152–158 MHz paging).
   - **NOAA Weather Radio (162.400–162.550 MHz)**.
   - **Cellular / LTE Base Stations (700–900 MHz)**.

   **Recommended Filters for High-RF Environments**:
   - **220 MHz Bandpass Filter (Recommended)**: A dedicated 220–222 MHz SAW or helical bandpass filter (e.g., 220 MHz bandpass filter with SMA connectors). This provides sharp out-of-band attenuation (> 40 dB rejection below 215 MHz and above 225 MHz), eliminating FM, paging, and cellular overload while passing 220 MHz channels with < 2 dB insertion loss.
   - **Broadcast FM Notch / Bandstop Filter (88–108 MHz)**: Attenuates high-power commercial FM broadcast stations by 30–50 dB while passing 220 MHz signals unimpeded.
   - **VHF High-Band Paging Notch Filter**: Attenuates 152–158 MHz paging spikes in dense urban rail corridors.

---

## Detailed Dashboard Field Reference

Reference guide matching the fields shown in [`GrabberMainScreen.png`](images/GrabberMainScreen.png):

### 1. Header Bar
```
ApexBOSGrabber (v26.1127.08) | Elapsed: 00:04:12 | SDR: PIPE | Rate: 960000 S/s [AUTO] | Gain: AGC | Bias: ON | PPM: 6 | Err: 8 | Center: 220.4875 MHz
```
- **`v26.1127.08`**: Active unified ApexBOS release version (from `version.py`).
- **`Elapsed: 00:04:12`**: Elapsed execution time since Grabber launched (`HH:MM:SS`).
- **`SDR: PIPE`**: Active ingestion mode (`PIPE`, `TCP`, `RTLSDR`, `RSPTCP`, `SOAPY`, or `STDIN`).
- **`Rate: 960000 S/s [AUTO]`**: Hardware sampling rate. `[AUTO]` denotes that Grabber automatically expanded the sample rate from 192 kS/s to 960 kS/s to span all requested channels.
- **`Gain: AGC`**: Tuner gain setting. Displays `AGC` when auto-gain is active, or fixed gain in dB (e.g. `42.0 dB`). When incoming bursts clip the ADC (`Peak RSSI >= 0.0 dBFS`), an alert tag `[CLIP: GAIN HIGH]` warns in bold red that the gain is set too high.
- **`Bias: ON`**: Status of internal 4.5V LNA bias-tee power (`ON` or `OFF`).
- **`PPM: 6`**: Master oscillator frequency correction offset applied to the tuner.
- **`Err: 8`**: Active maximum unique word Hamming bit error tolerance (`-E`, modifiable on-the-fly via `e <err>`).
- **`Center: 220.4875 MHz`**: Tuner RF local oscillator center frequency.

### 2. I/Q Flow & Global Metrics
```
I/Q Flow: Reader 1,165,352,000 S (2222.73 MB, 7874 chunks) | Workers Demod: 3,611,200,000 S (24400 banks)
Bursts: 938895  Pending Banks: 6  Peak RSSI: 1.5 dBFS  Packets: 15518  PPM: 693 (Raw: 13,753)  Dropped: 0
Channels (8 active, 7 base*): 101*, 102*, 113*, 114*, 125*, 126*, 127*, 131
Channel AFC Offsets (Δf) : [DISABLED - Offsets 0Hz] (use 'afc on' to enable)
Avg Packet Freq Offset (Δf) : +24.5 Hz [OPTIMAL: |Δf| < 100 Hz] | Auto-PPM: ON (7s check)
```
- **`I/Q Flow: Reader`**: Samples received, total megabytes read, and chunk blocks dispatched by the ingestion reader process.
- **`Workers Demod`**: Cumulative samples and sample banks processed across all worker child processes.
- **`Bursts: 938895`**: Total RF carrier bursts detected across all monitored channels.
- **`Pending Banks: 6`**: Number of I/Q banks buffered in worker IPC queues. Green ($\le 4$) indicates real-time processing; yellow/red indicates worker backlog.
- **`Peak RSSI: 1.5 dBFS`**: Maximum burst signal strength observed within the dynamic 60-second sliding window. Values $\ge 0.0\text{ dBFS}$ (such as `1.5 dBFS`) indicate ADC digital clipping/saturation caused by tuner gain being set too high, triggering an immediate high-visibility red `[CLIPPING: Gain may be set too high!]` alert. When gain is adjusted with `g <gain>` or auto-tuning, peak RSSI tracking is immediately reset to `-100.0 dBFS` across all worker processes. See [Troubleshooting ADC Clipping & Persistent Positive dBFS Alerts](#troubleshooting-adc-clipping--persistent-positive-dbfs-alerts) for step-by-step resolution.
- **`Packets: 15518`**: Total valid, CRC-verified, unique packets decoded since startup.
- **`PPM: 693 (Raw: 13,753)`**: Packets per minute. `693` is post-deduplication unique packet rate sent to clients; `13,753` is pre-deduplication raw RF packet rate.
- **`Dropped: 0`**: Packets dropped due to IPC queue saturation.
- **`Channels`**: List of monitored channels:
  - `*` (e.g. `101*`): Base station channel dynamically discovered and protected from idle timeouts.
  - `.` (e.g. `102.`): Locked channel pinned by user command or CLI configuration.
- **`Channel AFC Offsets (Δf)`**: Real-time frequency correction applied to individual channels by the Automatic Frequency Control loop to compensate for transmitter crystal drift (capped at $\pm 500\text{ Hz}$). Disabled by default; enable via `--afc` flag or `afc on` terminal command.
- **`Avg Packet Freq Offset (Δf)`**: The rolling sliding-window average measured frequency error across all channels for decoded packets. Displays `[OPTIMAL: |Δf| < 100 Hz]`, `[HIGH: Δf > +100 Hz -> PPM -1]`, or `[LOW: Δf < -100 Hz -> PPM +1]`.
- **`Auto-PPM`**: Real-time operational state of the automatic PPM tracker (`ON (7s check)` or `OFF`). Automatically evaluates every 7 seconds and nudges PPM $\pm 1$ until $|\Delta f| \le 100\text{ Hz}$.

### 3. Pipeline Detection & Decoding Integrity (60s Window)
```
  [ 70.4% ]  Total Demod Failure Rate: 70.4% [HIGH (66-98%)]
             5s Short Window: 1,219/4,118 CRC/Sync ( 29.6% match)
             60s Total Window: 68.4% fail (13,753/43,498) | PID Setpoint: 0.0%
  Packet Sync Signatures   : 43,498 detected (window)  |  Lifetime: 938,895
  Viterbi Headers Decoded  : Good: 28,264  | Failures: 15,234  | Bad in Good: 53.9% (35.0% failure rate)
  RS Payloads Processed    : Good: 13,753  | Failures: 14,511  | Bad in Good: 105.5% (51.3% of 28,264 processed)
  CRC Matched Integrity    : Good: 13,753  | Failures: 0       | Bad in Good:  0.0% ( 0.0% failure rate)
  Packets per Minute Rate  : Unique: 693 Pkts/m (dedup) | Raw Demod: 13,753 Pkts/m (pre-dedup) | Lifetime Unique: 15,518
```
- **Large Digit Matrix (`70.4%`)**: Instantaneous total demodulation failure rate over the short evaluation window, color-coded by severity:
  - Green (`< 35%`): `NOMINAL`
  - Yellow (`35 - 65%`): `MODERATE`
  - Orange (`66 - 98%`): `HIGH`
  - Red (`> 98%`): `CRITICAL`
  - Gray (`--`): `NO SYNC` — When 0 sync signatures are detected within the observation window (e.g. quiet band, disconnected antenna, or tuner off-frequency), the matrix displays `--` and `0/0 CRC/Sync (--.-% match)` rather than a deceptive `0.0% [OPTIMAL]`.
- **`Auto-Tune PID`**: Status of automated gain/PPM tuning algorithm and active proportional, integral, and derivative terms. When 0 sync signatures are detected, the PID loop automatically stops (`[STOPPED: NO SYNC]`), resets P/I/D terms to 0.0, and holds tuner gain and PPM constant to prevent integral windup.
- **`Packet Sync Signatures`**: Number of valid 64-bit unique word correlations detected in the 60s window and lifetime.
- **`Viterbi Headers Decoded`**: Good vs corrupted packet headers passing Viterbi error correction.
- **`RS Payloads Processed`**: Good vs uncorrectable payload blocks evaluated by Reed-Solomon $RS(16, 12)$.
- **`CRC Matched Integrity`**: Good vs failed CRC-16 checks.
- **`Bad in Good`**: Ratio of corrupted packets relative to good packets ($(\text{bad} / \text{good}) \times 100\%$). Useful for tracking RF noise floors and co-channel interference.

### 4. Per-Process Worker Distribution Table
```
Proc | ZMQ Port | Chns     | Sync    | CRC G/B    | Pkts/m (Dedup/Raw) |
#0   | 18101    | 101,125  | 247,530 | 103,428/10 | 248 / 4,658        |
#1   | 18114    | 102,126  | 170,843 | 57,526/0   | 115 / 2,490        |
#2   | 18126    | 113,127  | 276,058 | 63,648/4   | 148 / 2,686        |
#3   | 18131    | 114,131  | 244,464 | 85,083/12  | 182 / 3,919        |
```
- **`Proc`**: Worker process ID (`#0`, `#1`, etc.).
- **`ZMQ Port`**: Port assigned to this worker's local ZeroMQ publisher.
- **`Chns`**: ITC channels allocated to this worker process.
- **`Sync`**: Cumulative sync words detected by this worker.
- **`CRC G/B`**: Ratio of Good CRC packets to Bad CRC packets decoded by this worker.
- **`Pkts/m (Dedup/Raw)`**: Channel packet throughput per worker process.

### 5. Recent Decoded Packets & Telemetry
```
[08:47:53.548] Ch 125 (HR) | RSSI: -6.0 SNR:39.0 Δf:+981Hz | WIU | 49000A3300B5A9AF06BC8208..
[08:47:53.619] Ch 114 (HR) | RSSI: -6.5 SNR:38.5 Δf:-70Hz  | WIU | 49000B3300AAE8CD4B628208..
```
- **`[08:47:53.548]`**: Packet reception timestamp with millisecond resolution.
- **`Ch 125 (HR)`**: Channel number and transmission rate:
  - `(FR)`: Full Rate (32 kbps / 16 kbaud).
  - `(HR)`: Half Rate (16 kbps / 8 kbaud).
- **`RSSI: -6.0`**: Received Signal Strength Indicator in dBFS.
- **`SNR: 39.0`**: Estimated Signal-to-Noise Ratio in dB.
- **`Δf: +981Hz`**: Measured carrier frequency error of the transmitting station.
- **`WIU / Base / EMP`**: High-level packet classification.
- **`49000A33...`**: Extracted metadata (e.g. WIU ID, Switch Status, Base GPS Coordinates, or Raw Hex).

---

## Command Line Reference

```bash
python ApexBOSGrabber.py [OPTIONS]
```

### General & Configuration
| Flag | Description | Default |
| :--- | :--- | :--- |
| `--config <path>` | Load configuration options from a JSON file | `None` |
| `--gen-config` | Generate a template configuration file with defaults and exit | — |
| `--no-color` | Disable ANSI terminal colors | `False` |
| `--no-tui` | Disable full-screen ANSI TUI and print plain scrolling lines (auto-enabled on non-ANSI / redirected consoles) | `False` |
| `--full` | Print full packet hex and decoding details | `False` |

### SDR Hardware & Tuning
| Flag | Description | Default |
| :--- | :--- | :--- |
| `--pipe` | Read I/Q from native `rtl_sdr` binary pipe | `True` |
| `--rtl-tcp` | Connect to an RTL-TCP server socket | `False` |
| `--rsp-tcp`, `--rsptcp` | Connect to SDRplay `rsp_tcp` server (RSP1B / RSP1A / RSPdx / RSPduo) | `False` |
| `--rsp-tcp-bin <path>` | Path to SDRplay `rsp_tcp` executable for local auto-launch | `None` |
| `--rsp-16bit`, `--rsp-extended` | Enable SDRplay `rsp_tcp` Extended Mode (`-E`) with 14/16-bit `CS16` I/Q | `False` |
| `--rsp-lna-state <0..9>` | SDRplay LNA state (`0` = max sensitivity) | `0` |
| `--rsp-antenna <port>` | Optional SDRplay / SoapySDR antenna port selection | `None` |
| `--rsp-notch <mask>` | Optional SDRplay `rsp_tcp` broadcast notch filter bitmask | `None` |
| `--soapy`, `--soapysdr` | Native 16-bit `CS16` direct-USB streaming via `SoapySDR` | `False` |
| `--soapy-args <str>` | SoapySDR device selector (e.g. `"driver=sdrplay"`, `"driver=rtlsdr"`) | `None` |
| `--sample-format <fmt>` | Raw IQ sample format override: `auto`, `u8` (8-bit), or `s16` (16-bit `CS16`) | `auto` |
| `--rtlsdr`, `--pyrtlsdr` | Direct in-process access via `pyrtlsdr` package | `False` |
| `--sdr-mode <mode>` | Explicit interface: `auto`, `pipe`, `tcp`, `rtlsdr`, `rsptcp`, `soapy`, `stdin` | `auto` |
| `--stdin` | Read raw interleaved I/Q from standard input (`u8` or `s16`) | `False` |
| `--auto` | Automatically probe and select available SDR source | `False` |
| `--host <ip>` | RTL-TCP / RSP-TCP host address | `127.0.0.1` |
| `--port <port>` | RTL-TCP / RSP-TCP port | `1234` |
| `--device <idx>` | SDR USB device index | `0` |
| `--rate <sps>` | SDR sampling rate in S/s | `192000` |
| `--gain <dB>` | Tuner gain in dB or tenths (or `0` for AGC) | `0` (AGC) |
| `-b`, `--bias-tee <0\|1>`| Power inline antenna LNA via bias-tee | `1` (ON) |
| `--ppm <int>` | Master tuner frequency correction in PPM | `0` |
| `--auto-ppm` / `--no-auto-ppm` | Automatically adjust master PPM based on packet frequency offsets ($\Delta f$) | `True` |
| `--auto-tune` | Enable automated PID tuning of gain and PPM | `False` |

### Channels & Demodulation
| Flag | Description | Default |
| :--- | :--- | :--- |
| `--chans <list>` | Comma-separated channels (e.g. `101,102.,107`); suffix `.` or `l` locks channel | `channels.json` |
| `--chans-file <path>` | Path to JSON channels file | `channels.json` |
| `--channel-timeout <s>`| Inactivity seconds before dropping non-base channels | `300.0` (5 min) |
| `--afc` / `--no-afc` | Enable/disable dynamic per-channel AFC frequency tracking | `False` |
| `--afc-limit <Hz>` | Maximum AFC offset allowed per channel | `500.0` Hz |
| `--dedup-ttl <sec>` | Packet deduplication window in seconds | `1.0` s |
| `-E`, `--max-errors <n>`| Maximum unique word Hamming bit errors (modifiable at runtime via `e <err>`) | `8` |

### Networking & IPC
| Flag | Description | Default |
| :--- | :--- | :--- |
| `--procs <ports>` | Comma-separated ZeroMQ PUB ports | `5555` |
| `--workers <count>` | Number of worker processes (overrides `--procs` port count) | Process count |
| `--udp-port <port>` | Base UDP broadcast port | Same as ZMQ |
| `--filter-ctrl` / `--no-filter-ctrl` | Filter `QSTAT/ACK` (`0x01`), `ACQ` (`0x04`), and `CNTL` (`0x05`) from outbound ZMQ/UDP feeds (still counted in stats & displayed on Chroma). Aliases: `--no-filter`, `--send-all`. | `True` (ON) |
| `--no-rssi` | Emit raw hex without RSSI prepended (legacy mode) | `False` |

---

## Interactive Keyboard Commands

While ApexBOSGrabber is running, you can adjust parameters in real time directly from the terminal prompt:

| Command | Action | Example |
| :--- | :--- | :--- |
| `p <ppm>` | Set master frequency correction in PPM | `p 5` |
| `g <gain>` | Set tuner gain (`0` or `auto` for AGC, or dB value) | `g 42.0` |
| `b <0\|1>` | Toggle LNA antenna bias-tee power | `b 1` |
| `e <err>` | Set maximum unique word Hamming bit errors (`-E`) at runtime | `e 8` |
| `autopm <on\|off>` | Toggle or configure automatic PPM adjustment based on packet $\Delta f$ | `autopm on` |
| `+ <chan>` | Add channel (suffix `.` or `l` to lock) | `+ 102.` |
| `- <chan>` | Remove channel | `- 102` |
| `c`, `chans` | List all active, locked, and base channels, with the elapsed time since the last packet was received (e.g. `101 (locked.) (15s ago)`, `102 (base*) (2s ago)`) | `c` |
| `tune <on\|off>`| Enable or disable automated PID tuner | `tune on` |
| `afc <on\|off>` | Enable or disable per-channel AFC frequency correction | `afc off` |
| `dedup <sec>` | Adjust deduplication window in seconds | `dedup 2.0` |
| `filter <on\|off\|status>` | Toggle outbound control-packet filter (`0x01`/`0x04`/`0x05`) live (alias: `flt`) | `filter off` |
| `lock <chan>` / `unlock <chan>` | Pin or unpin an active channel against idle pruning | `lock 102` |
| `t <sec>` | Set channel inactivity timeout | `t 600` |
| `q` | Cleanly terminate all worker processes and exit | `q` |

---

## Channel Search & Calibration (`ApexBOSScanner.py`)

ApexBOS includes a dedicated automated channel search and frequency calibration utility: **`ApexBOSScanner.py`**.

It automatically discovers active Positive Train Control (PTC) and ACSES channels across the 220 MHz band, measures channel health metrics, optimizes master receiver crystal PPM offsets, and generates startup scripts to launch ApexBOSGrabber with optimized parameters.

### Operational Workflow
1. **Phase 1: Rapid Channel Probe (Channels 97 to 176)**
   - Tunes the SDR sequentially across ITC channels 97 to 176 (220.0125 MHz to 221.9875 MHz).
   - Offsets the local oscillator (LO) by 50 kHz below each target channel to isolate the signal from tuner zero-IF DC center spikes.
   - Observes baseband I/Q for the specified dwell duration (default: `2.0` seconds; configurable with `--dwell <sec>`) to catch periodic beacons and status reports, probing for the 64-bit $\pi/4$-DQPSK PTC sync word signature (`PTC_SYNC_BIT_STRING`).
2. **Phase 2: PPM Sweep & Optimization (±4 PPM)**
   - For every active channel detected in Phase 1, sweeps the master frequency correction from `base_ppm - 4` to `base_ppm + 4` in 9 discrete steps.
   - Observes each candidate PPM for 5.0 seconds (or custom `--opt-dwell <sec>`), recording sync signature hits, CRC-good packets, average burst RSSI (dBFS), and signal-to-noise ratio (SNR).
   - Identifies the per-channel optimal PPM that maximizes sync signatures and CRC integrity.
3. **Phase 3: Consensus Master PPM Determination**
   - Evaluates active channels to calculate the optimal consensus master tuner PPM covering all active transmitters.
4. **Phase 4: Calibration & Performance Report**
   - Generates an on-screen tabular report and saves `apexbos_channel_scan_report.txt` in the system temporary directory (`tempfile.gettempdir()`).
5. **Phase 5: Automated Startup Script Generation**
   - Creates ready-to-run startup scripts configured with the discovered active channels and consensus PPM, using absolute paths for both the Python interpreter and `ApexBOSGrabber.py`:
     - Windows Batch: `%TEMP%\start_apexbos_grabber.bat`
     - PowerShell: `$env:TEMP\start_apexbos_grabber.ps1`
     - Plain text command line: `%TEMP%\start_apexbos_grabber_cmd.txt`

### Interactive Controls During Scanning
While `ApexBOSScanner.py` is actively running, you can interact with the scan in real time:

| Key | Phase | Action |
| :--- | :--- | :--- |
| `n` or `Space` / `Enter` | Phase 1 (Probe) | Move immediately to the next channel without waiting for the dwell timer to finish |
| `s` or `2` / `p` | Phase 1 (Probe) | Safely stop Phase 1 probe and proceed immediately to Phase 2 (PPM optimization) with discovered channels |
| `q` or `Esc` | Phase 1 (Probe) | Cancel and abort scan |
| `n` or `Space` / `Enter` | Phase 2 (PPM Sweep) | Skip to the next candidate PPM offset |
| `q` or `s` / `Esc` | Phase 2 (PPM Sweep) | Safely stop Phase 2 and quit (finalizes report and writes ready-to-run Grabber startup scripts to `%TEMP%`) |

### Terminal Display & Selective Scrolling
- **Real-Time Live Countdown & Telemetry**: As each channel is being probed, an in-place status line dynamically reports:
  ```text
  [ 15.0%] Ch 101 (220.1125 MHz) [T- 2.8s] | Sync:  2 | CRC:  0 | RSSI: -14.5 dBFS | SNR: 30.5 dB
  ```
- **Selective Scrolling**: If no sync signatures are detected during a channel's dwell, the line is overwritten in-place without scrolling down. The screen scrolls down (`\n`) **only when a channel with sync signatures is detected**, keeping the terminal clean so only active, relevant channels remain on the screen.

### Running the Scanner
```bash
# Scan default channel range 97 to 176 with 2.0s dwell per channel:
python ApexBOSScanner.py

# Increase dwell time to 3.0 or 5.0 seconds per channel to catch intermittent/slow transmitters:
python ApexBOSScanner.py --dwell 3.5
python ApexBOSScanner.py -d 5.0

# Scan with custom dwell, base PPM, fixed gain, and bias-tee:
python ApexBOSScanner.py --dwell 3.0 --ppm 6 --gain 33.8 --bias-tee

# Scan custom channel range (e.g. channels 101 to 135) with 4.0s dwell:
python ApexBOSScanner.py --start-ch 101 --end-ch 135 -d 4.0

# Fast offline mock verification mode:
python ApexBOSScanner.py --mock --start-ch 100 --end-ch 115
```

---

## Example Configuration (`grabber_config.json`)

```json
{
    "pipe": true,
    "stdin": false,
    "rtl_sdr_bin": null,
    "rtl_tcp": false,
    "rtlsdr": false,
    "host": "127.0.0.1",
    "port": 1234,
    "device": 0,
    "rate": 192000,
    "gain": "0",
    "bias_tee": 1,
    "ppm": 0,
    "bank_size": 148000,
    "max_errors": 8,
    "auto_tune": false,
    "tune_interval": 5.0,
    "channel_afc": true,
    "afc_limit": 500.0,
    "dedup_ttl": 1.0,
    "filter_ctrl": true,
    "chans": null,
    "chans_file": null,
    "channel_timeout": 300.0,
    "procs": "5555",
    "workers": null,
    "zmq_port": null,
    "udp_port": null,
    "no_color": false,
    "full": false,
    "all": false,
    "no_rssi": false
}
```
Load configuration file:
```bash
python ApexBOSGrabber.py --config grabber_config.json
```

---

## License

This software is released under the **MIT License**.

Copyright (c) 2026 AB

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

