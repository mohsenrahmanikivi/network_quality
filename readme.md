# Network Quality — ROS 2 (Jazzy) Package

> **Keep calm and probe the link.**  
> A small, opinionated ROS 2 node that watches your robot's Ethernet connection like a hawk — measuring latency, jitter, packet loss, and interface bandwidth — and tells you when it's time to stop streaming full-HD video and hand control to local autonomy.

This repository contains a lightweight, deployable ROS 2 package (`network_quality`) built for **ROS 2 Jazzy** (Ubuntu 24.04) on Raspberry Pi or similar hardware. It is intended for remote-controlled robots where network quality affects safety and operator experience.

---

## What it does (TL;DR)

- Periodically pings a configurable host to get **RTT, jitter and packet loss**.  
- Reads `/sys/class/net/<iface>/statistics` to estimate **upload/download Mbps** (passive, low-cost).  
- Computes a simple **quality_score (0–100)** and checks if required bandwidth is available.  
- Publishes machine-readable JSON on `/network/quality` and a `diagnostic_msgs/DiagnosticArray` on `/diagnostics`.  
- Optionally runs an **iperf3** throughput test on-demand (service) and includes an **echo server/client** pair for application-level RTT/loss testing.

---

## Repo layout (what you get)

```
network_quality/
├─ package.xml
├─ setup.cfg
├─ setup.py
├─ resource/
│  └─ network_quality
└─ network_quality/
   ├─ __init__.py
   ├─ network_quality_node.py      # main monitor node
   ├─ net_echo_server.py           # optional operator echo server
   └─ net_echo_client.py           # optional robot echo client
```

---

## Quick install (Raspberry Pi / Ubuntu 24.04 + ROS 2 Jazzy)

> Assumes ROS 2 Jazzy is installed and working. If not, see ROS 2 Jazzy docs.

```bash
# install helper tools (iperf3 optional)
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-colcon-common-extensions python3-pip iperf3

# workspace
mkdir -p ~/ros2_ws/src
cd ~/ros2_ws/src

# clone this repo into the src/ directory
git clone <your-repo-url> network_quality

# build
cd ~/ros2_ws
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install
source install/setup.bash
```

---

## Run (recommended quickstart)

Start the monitor node (defaults: `eth0`, probe `8.8.8.8`, every 5s):

```bash
source /opt/ros/jazzy/setup.bash
source ~/ros2_ws/install/setup.bash
ros2 run network_quality network_quality_node
```

See what it publishes (human- and machine-friendly):

```bash
ros2 topic echo /network/quality   # JSON payload
ros2 topic echo /diagnostics       # diagnostic_msgs/DiagnosticArray
```

Operator-side echo server:

```bash
ros2 run network_quality net_echo_server
```

Robot-side echo client (measures app-level RTT & loss):

```bash
ros2 run network_quality net_echo_client
```

---

## What the node publishes (`/network/quality` JSON)

The node publishes a JSON string with keys:

- `timestamp` — ISO8601 UTC timestamp  
- `probe_host` — host used for ping probes  
- `rtt_ms` — average round-trip time (ms) or `null`  
- `jitter_ms` — jitter estimate (ms) or `null`  
- `packet_loss_pct` — ping loss percentage  
- `up_mbps` / `down_mbps` — measured interface throughput (Mbps) or `null`  
- `iface` — interface name (e.g. `eth0`)  
- `link_up` — boolean (operstate)  
- `quality_score` — heuristic 0..100 (higher = better)  
- `requirements_met` — boolean: whether required up/down Mbps are satisfied  
- `up_gap_mbps`, `down_gap_mbps` — how many Mbps short of required (0 if met)  
- `raw_ping` — truncated ping output (debug)  
- `notes` — free text

Example:

```json
{
  "timestamp":"2025-11-20T12:34:56.789Z",
  "probe_host":"8.8.8.8",
  "rtt_ms":12.34,
  "jitter_ms":0.54,
  "packet_loss_pct":0.0,
  "up_mbps":0.12,
  "down_mbps":1.84,
  "iface":"eth0",
  "link_up":true,
  "quality_score":85.23,
  "requirements_met":false,
  "up_gap_mbps":1.88,
  "down_gap_mbps":0.16,
  "raw_ping":"PING 8.8.8.8 ...",
  "notes":""
}
```

---

## Key parameters (runtime via `ros2 param`)

- `probe_host` (string) — default: `8.8.8.8`  
- `probe_interval` (float) — default: `5.0` seconds  
- `ping_count` (int) — default: `4` pings per probe  
- `net_iface` (string) — default: `eth0`  
- `required_up_mbps` / `required_down_mbps` (float) — default: `2.0` each  
- `publish_diagnostics` (bool) — default: `true`  
- `enable_iperf_on_demand` (bool) — default: `false`  
- `iperf_duration` (int) — default: `5` seconds

Set a parameter example:

```bash
ros2 param set /network_quality_node net_iface enp3s0
```

---

## Optional: iperf3 on-demand (use with care)

1. Start iperf3 server on the operator/server:
```bash
iperf3 -s
```

2. Enable iperf service on the node:
```bash
ros2 param set /network_quality_node enable_iperf_on_demand true
```

3. Call the service (Trigger):
```bash
ros2 service call /run_iperf_test std_srvs/srv/Trigger "{}"
```

**Warning:** iperf3 consumes bandwidth and may interfere with teleoperation — use sparingly.

---

## Why use app-level echo?

ICMP (`ping`) is useful but can be deprioritized by routers. The included `net_echo_server` / `net_echo_client` measure **application-level** RTT and packet loss on the exact topics/channels you use for teleoperation — which is what truly matters for safety.

---

## Troubleshooting (common hiccups)

- **No output on `/network/quality`:**  
  - Ensure node is running: `ros2 node list`  
  - Run with debug logging: `ros2 run network_quality network_quality_node --ros-args --log-level debug`

- **`ping missing` / no ping output:**  
  - Install ping: `sudo apt install iputils-ping` and test `ping 8.8.8.8` from shell.

- **Bandwidth stays `null` or 0:**  
  - The node needs two samples; wait two intervals.  
  - Check interface name: `ip a` and set `net_iface` accordingly.  
  - Inspect: `cat /sys/class/net/<iface>/statistics/{rx_bytes,tx_bytes}`

- **Echo client/server won't talk across internet:**  
  - ROS 2 DDS discovery is not NAT-friendly. Use a VPN, DDS relay, or run server on an accessible host.

- **`colcon build` failures:**  
  - Make sure you sourced ROS Jazzy and have `python3-colcon-common-extensions` installed. Inspect `log/latest_build`.

---

## License & contributions

- Licensed under Apache-2.0.  
- Contributions welcome — open issues, send PRs, and be kind in commit messages.

---

## Final notes (short & human)

This code is intentionally pragmatic: it gives you quick, meaningful metrics that map to teleoperation safety decisions (throttle video, switch to local autonomy, or warn the operator). It is not a full network lab — it's a trustworthy watchdog. Use it, tweak the thresholds, and let me know if you want a typed ROS message instead of JSON, Prometheus exporter, or an automatic safety actor that responds to `quality_score` drops.

Happy probing! 🛰️
