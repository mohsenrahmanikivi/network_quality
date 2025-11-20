# Network Quality — ROS 2 (Jazzy) Package

`network_quality` monitors a robot's Ethernet network connection and publishes metrics including latency, jitter, packet loss, and interface bandwidth.
It is designed for remote-controlled robots where network quality directly affects safety and performance.

This package runs on ROS 2 Jazzy (Ubuntu 24.04 / Raspberry Pi).

## Features

- ICMP ping probing (RTT, jitter, packet loss)
- Bandwidth estimation (upload/download Mbps)
- Interface link state detection
- Heuristic quality_score (0–100)
- requirements_met boolean for bandwidth thresholds
- Publishes:
  - /network/quality → JSON (std_msgs/String)
  - /diagnostics → diagnostic_msgs/DiagnosticArray
- Optional iperf3 on-demand throughput test (Trigger service)
- Optional echo client/server for application-level RTT & packet loss

## Repository Layout

network_quality/
├─ package.xml
├─ setup.cfg
├─ setup.py
├─ resource/
│  └─ network_quality
└─ network_quality/
   ├─ __init__.py
   ├─ network_quality_node.py
   ├─ net_echo_server.py
   └─ net_echo_client.py

## Installation (Ubuntu 24.04 + ROS 2 Jazzy)

sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-colcon-common-extensions python3-pip iperf3

mkdir -p ~/ros2_ws/src
cd ~/ros2_ws/src

git clone <your-repo-url> network_quality

cd ~/ros2_ws
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install
source install/setup.bash

## Running

ros2 run network_quality network_quality_node
ros2 topic echo /network/quality
ros2 topic echo /diagnostics

## Echo Server / Client

Operator:
ros2 run network_quality net_echo_server

Robot:
ros2 run network_quality net_echo_client

## iperf3 Throughput Test

Operator:
iperf3 -s

Robot:
ros2 param set /network_quality_node enable_iperf_on_demand true
ros2 service call /run_iperf_test std_srvs/srv/Trigger "{}"

## ROS Parameters

probe_host (default 8.8.8.8)
probe_interval (default 5.0)
ping_count (default 4)
net_iface (default eth0)
required_up_mbps (default 2.0)
required_down_mbps (default 2.0)
publish_diagnostics (default true)
enable_iperf_on_demand (default false)
iperf_duration (default 5)

## Troubleshooting

Enable debug:
ros2 run network_quality network_quality_node --ros-args --log-level debug

Ensure correct interface:
ros2 param set /network_quality_node net_iface eth0

## Contributing

PRs and issues are welcome.
