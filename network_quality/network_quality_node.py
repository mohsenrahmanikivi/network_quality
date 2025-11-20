#!/usr/bin/env python3
"""
network_quality_node.py

Monitors Ethernet network quality (latency, jitter, packet loss, bandwidth).
Publishes JSON on /network/quality (std_msgs/String) and diagnostics on /diagnostics
(diagnostic_msgs/DiagnosticArray). Optional on-demand iperf3 service (std_srvs/Trigger).

This version:
- Uses a dedicated asyncio event loop running in a background thread (works on Python 3.12)
- Validates the network interface before reading counters
- Uses subprocess.run with argument lists (safer) for iperf3
- Ensures ping timeout passed as integer to `ping -W`
- Cleans up asyncio loop/thread on shutdown
- Robust error handling and informative logging
"""

from __future__ import annotations

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from std_srvs.srv import Trigger

import asyncio
import json
import time
import datetime
import os
import re
import shutil
import subprocess
import threading
from typing import Optional, Dict, Any, Tuple


# ----------------------
# Helpers
# ----------------------

def iso_ts_now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec='milliseconds') + 'Z'


def safe_int(x, default: int = 1) -> int:
    try:
        return int(x)
    except Exception:
        return default


def read_iface_bytes(iface: str) -> Tuple[Optional[int], Optional[int]]:
    """Return (rx_bytes, tx_bytes) or (None, None) on error."""
    try:
        base = f"/sys/class/net/{iface}/statistics"
        rx_path = os.path.join(base, "rx_bytes")
        tx_path = os.path.join(base, "tx_bytes")
        with open(rx_path, 'r') as f:
            rx = int(f.read().strip())
        with open(tx_path, 'r') as f:
            tx = int(f.read().strip())
        return rx, tx
    except Exception:
        return None, None


def iface_is_up(iface: str) -> bool:
    try:
        path = f"/sys/class/net/{iface}/operstate"
        with open(path, 'r') as f:
            state = f.read().strip()
        return state == "up"
    except Exception:
        return False


async def run_ping_probe(host: str, count: int, timeout_per_ping_s: int = 1) -> Dict[str, Any]:
    """
    Run system ping asynchronously and parse results.
    Returns dict with keys: rtt_ms, jitter_ms, packet_loss_pct, raw, times_ms
    """
    if shutil.which("ping") is None:
        return {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": 100.0, "raw": "ping missing", "times_ms": []}

    # ensure integer timeout for -W
    timeout_sec = safe_int(timeout_per_ping_s, default=1)
    cmd = f"ping -c {int(count)} -W {timeout_sec} {host}"
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        out = (stdout.decode(errors='ignore') or "").strip()
        err = (stderr.decode(errors='ignore') or "").strip()
    except Exception as e:
        return {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": 100.0, "raw": f"ping failed: {e}", "times_ms": []}

    res = {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": 100.0, "raw": out or err, "times_ms": []}

    # parse packet loss line
    for line in (out or "").splitlines():
        if "packet loss" in line:
            m = re.search(r'(\d+(?:\.\d+)?)% packet loss', line)
            if m:
                try:
                    res["packet_loss_pct"] = float(m.group(1))
                except Exception:
                    pass

    # parse rtt line (linux)
    m = re.search(r'rtt .* = ([0-9./]+) ms', out or "")
    if m:
        parts = m.group(1).split('/')
        if len(parts) >= 4:
            try:
                res["rtt_ms"] = float(parts[1])
                res["jitter_ms"] = float(parts[3])
            except Exception:
                pass

    # fallback parse individual reply lines (time=XX ms)
    times = []
    for line in (out or "").splitlines():
        if 'time=' in line:
            mm = re.search(r'time=([0-9.]+)\s*ms', line)
            if mm:
                try:
                    times.append(float(mm.group(1)))
                except Exception:
                    pass
    if times:
        res["times_ms"] = times
        if res["rtt_ms"] is None:
            res["rtt_ms"] = sum(times) / len(times)
        if res["jitter_ms"] is None and len(times) >= 2:
            mean = sum(times) / len(times)
            var = sum((t - mean) ** 2 for t in times) / len(times)
            res["jitter_ms"] = var ** 0.5

    return res


# ----------------------
# Main Node
# ----------------------

class NetworkQualityNode(Node):
    def __init__(self):
        super().__init__('network_quality_node')

        # ---- declare parameters ----
        self.declare_parameter('probe_host', '8.8.8.8')
        self.declare_parameter('probe_interval', 5.0)
        self.declare_parameter('ping_count', 4)
        self.declare_parameter('net_iface', 'eth0')
        self.declare_parameter('bandwidth_window', 5.0)  # currently implicit via probe_interval
        self.declare_parameter('publish_diagnostics', True)
        self.declare_parameter('required_up_mbps', 2.0)
        self.declare_parameter('required_down_mbps', 2.0)
        self.declare_parameter('enable_iperf_on_demand', False)
        self.declare_parameter('iperf3_path', 'iperf3')
        self.declare_parameter('iperf_duration', 5)

        # ---- read params ----
        self.probe_host: str = self.get_parameter('probe_host').value
        self.probe_interval: float = float(self.get_parameter('probe_interval').value)
        self.ping_count: int = int(self.get_parameter('ping_count').value)
        self.net_iface: str = self.get_parameter('net_iface').value
        self.publish_diagnostics: bool = bool(self.get_parameter('publish_diagnostics').value)
        self.required_up_mbps: float = float(self.get_parameter('required_up_mbps').value)
        self.required_down_mbps: float = float(self.get_parameter('required_down_mbps').value)
        self.enable_iperf_on_demand: bool = bool(self.get_parameter('enable_iperf_on_demand').value)
        self.iperf3_path: str = str(self.get_parameter('iperf3_path').value)
        self.iperf_duration: int = int(self.get_parameter('iperf_duration').value)

        # ---- publishers ----
        self.quality_pub = self.create_publisher(String, '/network/quality', 10)
        self.diag_pub = self.create_publisher(DiagnosticArray, '/diagnostics', 10) if self.publish_diagnostics else None

        # ---- service (create only if enabled at startup) ----
        if self.enable_iperf_on_demand:
            self.create_service(Trigger, 'run_iperf_test', self._handle_run_iperf)

        # ---- internal bandwidth state ----
        self._last_rx: Optional[int] = None
        self._last_tx: Optional[int] = None
        self._last_bytes_ts: Optional[float] = None

        # ---- create asyncio background loop (fix for Python 3.12) ----
        self._async_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._async_thread.start()

        # ---- timer to trigger probes ----
        self.get_logger().info(f"Probing {self.probe_host} every {self.probe_interval}s on iface '{self.net_iface}'")
        self.create_timer(self.probe_interval, self._trigger_probe)

    # --------------------
    # Asyncio background loop
    # --------------------
    def _run_async_loop(self):
        """Target that runs the asyncio loop in a background thread."""
        asyncio.set_event_loop(self._async_loop)
        self._async_loop.run_forever()

    def _trigger_probe(self) -> None:
        """Schedule the probe coroutine thread-safely on the background asyncio loop."""
        try:
            asyncio.run_coroutine_threadsafe(self._do_probe(), self._async_loop)
        except Exception as e:
            self.get_logger().error(f"Failed to schedule probe coroutine: {e}")

    async def _do_probe(self) -> None:
        """Performs an async ping probe, reads iface counters, computes metrics and publishes results."""
        notes = ""
        # run ping probe
        ping_res = await run_ping_probe(self.probe_host, self.ping_count, timeout_per_ping_s=1)

        # read interface counters
        rx, tx = read_iface_bytes(self.net_iface)
        now = time.time()
        up_mbps: Optional[float] = None
        down_mbps: Optional[float] = None

        if rx is None or tx is None:
            notes += f"interface '{self.net_iface}' missing or inaccessible; "
            self.get_logger().warn(f"Interface '{self.net_iface}' counters not found. Check net_iface parameter.")
        else:
            # compute rates if we have previous sample
            if self._last_rx is not None and self._last_tx is not None and self._last_bytes_ts is not None:
                dt = now - self._last_bytes_ts
                if dt > 0:
                    rx_rate = (rx - self._last_rx) / dt  # bytes/sec
                    tx_rate = (tx - self._last_tx) / dt
                    down_mbps = (rx_rate * 8.0) / 1e6
                    up_mbps = (tx_rate * 8.0) / 1e6
            # update last counters always when read succeeds
            self._last_rx = rx
            self._last_tx = tx
            self._last_bytes_ts = now

        # link status
        link_up = iface_is_up(self.net_iface)

        # compute quality
        quality = self._compute_quality_score(ping_res, up_mbps, down_mbps)
        req_check = self._check_requirements(up_mbps, down_mbps)

        payload = {
            "timestamp": iso_ts_now(),
            "probe_host": self.probe_host,
            "rtt_ms": ping_res.get("rtt_ms"),
            "jitter_ms": ping_res.get("jitter_ms"),
            "packet_loss_pct": ping_res.get("packet_loss_pct"),
            "up_mbps": up_mbps,
            "down_mbps": down_mbps,
            "iface": self.net_iface,
            "link_up": link_up,
            "quality_score": quality,
            "requirements_met": req_check["requirements_met"],
            "up_gap_mbps": req_check["up_gap_mbps"],
            "down_gap_mbps": req_check["down_gap_mbps"],
            "raw_ping": (ping_res.get("raw") or "")[:512],
            "notes": notes.strip()
        }

        # publish JSON
        try:
            self.quality_pub.publish(String(data=json.dumps(payload)))
        except Exception as e:
            self.get_logger().error(f"Failed to publish /network/quality: {e}")

        # publish diagnostics
        if self.diag_pub is not None:
            try:
                da = DiagnosticArray()
                da.header.stamp = self.get_clock().now().to_msg()
                ds = DiagnosticStatus()
                ds.name = "network/quality"
                pkt_loss = payload["packet_loss_pct"] if payload["packet_loss_pct"] is not None else 100.0
                rtt = payload["rtt_ms"] if payload["rtt_ms"] is not None else 2000.0
                if pkt_loss > 30.0 or (rtt > 2000.0):
                    ds.level = DiagnosticStatus.ERROR
                    ds.message = "Network DOWN / very poor"
                elif pkt_loss > 5.0 or (rtt > 500.0) or payload["quality_score"] < 60.0:
                    ds.level = DiagnosticStatus.WARN
                    ds.message = "Network degraded"
                else:
                    ds.level = DiagnosticStatus.OK
                    ds.message = "Network OK"

                ds.values.append(KeyValue(key="quality_score", value=str(payload["quality_score"])))
                ds.values.append(KeyValue(key="rtt_ms", value=str(payload["rtt_ms"])))
                ds.values.append(KeyValue(key="packet_loss_pct", value=str(payload["packet_loss_pct"])))
                ds.values.append(KeyValue(key="up_mbps", value=str(payload["up_mbps"])))
                ds.values.append(KeyValue(key="down_mbps", value=str(payload["down_mbps"])))
                ds.values.append(KeyValue(key="requirements_met", value=str(payload["requirements_met"])))
                ds.values.append(KeyValue(key="iface", value=str(payload["iface"])))
                ds.values.append(KeyValue(key="link_up", value=str(payload["link_up"])))
                da.status.append(ds)
                self.diag_pub.publish(da)
            except Exception as e:
                self.get_logger().error(f"Failed to publish /diagnostics: {e}")

        self.get_logger().debug(f"Published network quality: quality={quality} rtt={payload['rtt_ms']} loss={payload['packet_loss_pct']} up={payload['up_mbps']} down={payload['down_mbps']}")

    # --------------------
    # Quality heuristics & checks
    # --------------------
    def _compute_quality_score(self, ping_res: Dict[str, Any], up_mbps: Optional[float], down_mbps: Optional[float]) -> float:
        score = 100.0
        pl = ping_res.get("packet_loss_pct")
        if pl is None:
            pl = 100.0
        score -= min(pl, 100.0) * 0.9

        rtt = ping_res.get("rtt_ms")
        if rtt is None:
            rtt = 2000.0
        if rtt > 50.0:
            score -= max(0.0, (rtt - 50.0) / 10.0)

        bw = 0.0
        if down_mbps is not None:
            bw = down_mbps
        if up_mbps is not None and up_mbps > bw:
            bw = up_mbps
        if bw > 1.0:
            score += min(10.0, (bw / 5.0))

        score = max(0.0, min(100.0, score))
        return round(score, 2)

    def _check_requirements(self, up_mbps: Optional[float], down_mbps: Optional[float]) -> Dict[str, Any]:
        up_val = up_mbps or 0.0
        down_val = down_mbps or 0.0
        up_ok = up_val >= self.required_up_mbps
        down_ok = down_val >= self.required_down_mbps
        return {
            "up_ok": up_ok,
            "down_ok": down_ok,
            "up_gap_mbps": round(max(0.0, self.required_up_mbps - up_val), 3),
            "down_gap_mbps": round(max(0.0, self.required_down_mbps - down_val), 3),
            "requirements_met": (up_ok and down_ok)
        }

    # --------------------
    # iperf service
    # --------------------
    def _handle_run_iperf(self, request, response):
        """Service callback for std_srvs/Trigger. Launches iperf in a background thread."""
        # always accept the call and run iperf in background
        try:
            # prefer full path if available
            iperf_cmd = shutil.which(self.iperf3_path) or self.iperf3_path
            thread = threading.Thread(target=self._run_iperf3_test_blocking, args=(iperf_cmd, self.probe_host, self.iperf_duration), daemon=True)
            thread.start()
            response.success = True
            response.message = f"iperf3 test started to {self.probe_host}"
        except Exception as e:
            response.success = False
            response.message = str(e)
        return response

    def _run_iperf3_test_blocking(self, iperf_cmd: str, server_host: str, duration: int) -> None:
        """Run iperf3 client synchronously (in background thread)."""
        cmd = [iperf_cmd, "-c", server_host, "-t", str(duration), "-J"]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=duration + 10, text=True)
            out = proc.stdout or ""
            err = proc.stderr or ""
            # try to log a short summary
            snippet = (out[:512] + '...') if len(out) > 512 else out
            self.get_logger().info(f"iperf3 finished: {snippet}")
        except Exception as e:
            self.get_logger().warn(f"iperf3 test failed: {e}")

    # --------------------
    # Shutdown cleanup
    # --------------------
    def destroy_node(self):
        # stop asyncio loop cleanly
        try:
            if hasattr(self, "_async_loop") and self._async_loop.is_running():
                self._async_loop.call_soon_threadsafe(self._async_loop.stop)
                if hasattr(self, "_async_thread"):
                    self._async_thread.join(timeout=2.0)
        except Exception:
            pass
        super().destroy_node()


# ----------------------
# Main
# ----------------------
def main(args=None):
    rclpy.init(args=args)
    node = NetworkQualityNode()
    try:
        executor = rclpy.executors.MultiThreadedExecutor()
        executor.add_node(node)
        executor.spin()
    finally:
        executor.shutdown()
        # ensure clean destroy
        try:
            node.destroy_node()
        except Exception:
            pass
        rclpy.shutdown()


if __name__ == "__main__":
    main()
