#!/usr/bin/env python3
"""
network_quality_node.py

ROS2 Jazzy Python 3.12 node to monitor network quality and bandwidth.
Publishes:
  - /network/quality (std_msgs/String with JSON)
  - /diagnostics (DiagnosticArray) with quality_score
"""

import rclpy
from rclpy.node import Node
from rclpy.parameter import Parameter
from std_msgs.msg import String
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue

import asyncio
import threading
import time
import os
import json
import re
import shutil
import datetime
from typing import Optional, Dict, Any, Tuple


# ---------------------------
# Helpers
# ---------------------------

def iso_ts_now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec='milliseconds') + 'Z'


def read_iface_bytes(iface: str) -> Tuple[Optional[int], Optional[int]]:
    """Read RX/TX bytes from /sys/class/net/<iface>/statistics."""
    try:
        base = f"/sys/class/net/{iface}/statistics"
        with open(os.path.join(base, "rx_bytes"), 'r') as f:
            rx = int(f.read().strip())
        with open(os.path.join(base, "tx_bytes"), 'r') as f:
            tx = int(f.read().strip())
        return rx, tx
    except Exception:
        return None, None


def iface_is_up(iface: str) -> bool:
    try:
        with open(f"/sys/class/net/{iface}/operstate", 'r') as f:
            return f.read().strip() == "up"
    except Exception:
        return False


async def run_ping_probe(host: str, count: int) -> Dict[str, Any]:
    """Run ping asynchronously and parse RTT, jitter, packet loss."""
    if shutil.which("ping") is None:
        return {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": None, "raw": "ping_missing", "times_ms": []}

    cmd = f"ping -c {count} -W 1 {host}"
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        out = (stdout.decode(errors='ignore') or "").strip()
    except Exception:
        return {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": None, "raw": "ping_failed", "times_ms": []}

    res = {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": None, "raw": out, "times_ms": []}

    # Packet loss
    for line in out.splitlines():
        if "packet loss" in line:
            m = re.search(r'(\d+(?:\.\d+)?)% packet loss', line)
            if m:
                try:
                    res["packet_loss_pct"] = float(m.group(1))
                except:
                    pass

    # RTT / jitter
    m = re.search(r'rtt .* = ([0-9./]+) ms', out)
    if m:
        parts = m.group(1).split('/')
        if len(parts) >= 4:
            try:
                res["rtt_ms"] = float(parts[1])
                res["jitter_ms"] = float(parts[3])
            except:
                pass

    # individual times
    times = []
    for line in out.splitlines():
        if 'time=' in line:
            mm = re.search(r'time=([0-9.]+) ms', line)
            if mm:
                try:
                    times.append(float(mm.group(1)))
                except:
                    pass
    if times:
        res["times_ms"] = times
        if res["rtt_ms"] is None:
            res["rtt_ms"] = sum(times)/len(times)
        if res["jitter_ms"] is None and len(times) >= 2:
            mean = sum(times)/len(times)
            var = sum((t-mean)**2 for t in times)/len(times)
            res["jitter_ms"] = var ** 0.5

    return res


# ---------------------------
# Node
# ---------------------------

class NetworkQualityNode(Node):

    def __init__(self):
        super().__init__('network_quality_node')

        # Parameters
        self.declare_parameter('probe_host', '8.8.8.8')
        self.declare_parameter('probe_interval', 5.0)
        self.declare_parameter('ping_count', 4)
        self.declare_parameter('net_iface', 'eth0')
        self.declare_parameter('verbose', False)
        self.declare_parameter('publish_diagnostics', True)

        # Internal state
        self._last_rx: Optional[int] = None
        self._last_tx: Optional[int] = None
        self._last_ts: Optional[float] = None

        # Read initial params
        self._read_params()

        # Publishers
        self.quality_pub = self.create_publisher(String, '/network/quality', 10)
        self.diag_pub = self.create_publisher(DiagnosticArray, '/diagnostics', 10) if self.publish_diagnostics else None

        # Async loop for ping
        self._async_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._async_thread.start()

        # Timer
        self.get_logger().info(f"Probing {self.probe_host} every {self.probe_interval}s on iface '{self.net_iface}' (verbose={self.verbose})")
        self.create_timer(self.probe_interval, self._trigger_probe)

        # Dynamic param callback
        self.add_on_set_parameters_callback(self._on_param_change)

    def _read_params(self):
        self.probe_host = self.get_parameter('probe_host').value
        self.probe_interval = self.get_parameter('probe_interval').value
        self.ping_count = self.get_parameter('ping_count').value
        self.net_iface = self.get_parameter('net_iface').value
        self.verbose = self.get_parameter('verbose').value
        self.publish_diagnostics = self.get_parameter('publish_diagnostics').value

    # Dynamic parameter callback
    def _on_param_change(self, params):
        for p in params:
            if p.name == 'net_iface':
                self.net_iface = p.value
                self.get_logger().info(f"Interface updated: {self.net_iface}")
            elif p.name == 'probe_host':
                self.probe_host = p.value
                self.get_logger().info(f"Probe host updated: {self.probe_host}")
            elif p.name == 'ping_count':
                self.ping_count = p.value
                self.get_logger().info(f"Ping count updated: {self.ping_count}")
            elif p.name == 'verbose':
                self.verbose = p.value
                self.get_logger().info(f"Verbose updated: {self.verbose}")
        return rclpy.parameter.SetParametersResult(successful=True)

    def _run_async_loop(self):
        asyncio.set_event_loop(self._async_loop)
        self._async_loop.run_forever()

    def _trigger_probe(self):
        asyncio.run_coroutine_threadsafe(self._do_probe(), self._async_loop)

    async def _do_probe(self):
        notes = ""
        iface_up = iface_is_up(self.net_iface)
        rx, tx = read_iface_bytes(self.net_iface)
        now = time.time()

        if not iface_up:
            # Interface down -> publish zeros
            payload = {
                "timestamp": iso_ts_now(),
                "quality_score": 0.0,
                "up_mbps": 0.0,
                "down_mbps": 0.0,
            }
            if self.verbose:
                payload.update({
                    "iface": self.net_iface,
                    "link_up": False,
                    "rtt_ms": None,
                    "jitter_ms": None,
                    "packet_loss_pct": None,
                    "notes": f"Interface {self.net_iface} is down"
                })
            self._publish(payload, 0.0)
            return

        # Interface is up -> compute bandwidth
        if rx is None or tx is None:
            up_mbps = down_mbps = 0.0
            notes += f"Could not read counters for {self.net_iface}; "
        else:
            if self._last_rx is not None and self._last_tx is not None and self._last_ts is not None:
                dt = max(1e-6, now - self._last_ts)
                down_mbps = ((rx - self._last_rx) * 8.0)/1e6 / dt
                up_mbps = ((tx - self._last_tx) * 8.0)/1e6 / dt
            else:
                down_mbps = up_mbps = 0.0
            self._last_rx = rx
            self._last_tx = tx
            self._last_ts = now

        # Ping probe
        ping_res = await run_ping_probe(self.probe_host, self.ping_count)

        # Compute quality score
        quality_score = self._compute_quality_score(ping_res, up_mbps, down_mbps)

        payload = {
            "timestamp": iso_ts_now(),
            "quality_score": round(quality_score,2),
            "up_mbps": round(up_mbps,3),
            "down_mbps": round(down_mbps,3)
        }

        if self.verbose:
            payload.update({
                "iface": self.net_iface,
                "link_up": True,
                "rtt_ms": ping_res.get("rtt_ms"),
                "jitter_ms": ping_res.get("jitter_ms"),
                "packet_loss_pct": ping_res.get("packet_loss_pct"),
                "notes": notes.strip()
            })

        self._publish(payload, quality_score)

    def _compute_quality_score(self, ping_res, up_mbps, down_mbps):
        score = 100.0
        pl = ping_res.get("packet_loss_pct")
        if pl is None:
            score -= 20
        else:
            score -= min(100, pl) * 0.9

        rtt = ping_res.get("rtt_ms")
        if rtt is None:
            score -= 10
        else:
            if rtt > 50.0:
                score -= max(0.0, (rtt-50)/10)

        bw = max(up_mbps or 0.0, down_mbps or 0.0)
        if bw > 1.0:
            score += min(10.0, bw/5.0)
        score = max(0.0, min(100.0, score))
        return round(score,2)

    def _publish(self, payload: Dict[str,Any], quality_score: float):
        try:
            self.quality_pub.publish(String(data=json.dumps(payload)))
        except Exception as e:
            self.get_logger().error(f"Failed /network/quality: {e}")

        if self.diag_pub:
            try:
                da = DiagnosticArray()
                da.header.stamp = self.get_clock().now().to_msg()
                ds = DiagnosticStatus()
                ds.name = "network/quality"
                # Map score -> level
                if quality_score >= 80: ds.level, ds.message = DiagnosticStatus.OK, "OK"
                elif quality_score >= 50: ds.level, ds.message = DiagnosticStatus.WARN, "Degraded"
                else: ds.level, ds.message = DiagnosticStatus.ERROR, "Poor/Down"

                ds.values.append(KeyValue(key="quality_score", value=str(quality_score)))
                da.status.append(ds)
                self.diag_pub.publish(da)
            except Exception as e:
                self.get_logger().error(f"Failed /diagnostics: {e}")

    def destroy_node(self):
        try:
            if hasattr(self, "_async_loop") and self._async_loop.is_running():
                self._async_loop.call_soon_threadsafe(self._async_loop.stop)
                if hasattr(self, "_async_thread"):
                    self._async_thread.join(timeout=2.0)
        except Exception:
            pass
        super().destroy_node()


# ---------------------------
# Main
# ---------------------------

def main(args=None):
    rclpy.init(args=args)
    node = NetworkQualityNode()
    try:
        executor = rclpy.executors.MultiThreadedExecutor()
        executor.add_node(node)
        executor.spin()
    finally:
        executor.shutdown()
        try:
            node.destroy_node()
        except Exception:
            pass
        rclpy.shutdown()


if __name__ == "__main__":
    main()
