#!/usr/bin/env python3
"""
network_quality_node.py

Monitors network quality and bandwidth for ROS2 Jazzy (Python 3.12).

Improvements included:
- Separate probe and publish timers (probe_interval, publish_rate)
- EWMA smoothing for noisy metrics
- Dynamic parameters (net_iface, probe_host, probe_interval, publish_rate, ping_count, verbose, alpha, required_* )
- Diagnostics (/diagnostics) contains full smoothed+raw metrics
- Compact /network/quality JSON topic (quality_score, up_mbps, down_mbps)
- Safe asyncio integration (background loop + thread) for ping
- Robust shutdown handling to avoid "rcl_shutdown already called" RCLError on Ctrl-C
"""

from __future__ import annotations

import rclpy
from rclpy.node import Node
from rclpy.parameter import Parameter
from rcl_interfaces.msg import SetParametersResult
from std_msgs.msg import String
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue

import asyncio
import threading
import time
import json
import datetime
import os
import re
import shutil
import subprocess
from typing import Optional, Dict, Any, Tuple


# -----------------------
# Helpers
# -----------------------

def iso_ts_now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec='milliseconds') + 'Z'


def read_iface_bytes(iface: str) -> Tuple[Optional[int], Optional[int]]:
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


async def run_ping_probe(host: str, count: int, timeout_per_ping_s: int = 1) -> Dict[str, Any]:
    """Run ping asynchronously; return dict with rtt_ms,jitter_ms,packet_loss_pct,raw,times_ms."""
    if shutil.which("ping") is None:
        return {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": None, "raw": "ping_missing", "times_ms": []}
    cmd = f"ping -c {int(count)} -W {int(timeout_per_ping_s)} {host}"
    try:
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        out = (stdout.decode(errors='ignore') or "").strip()
    except Exception as e:
        return {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": None, "raw": f"ping_failed:{e}", "times_ms": []}

    res = {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": None, "raw": out, "times_ms": []}

    # packet loss
    for line in out.splitlines():
        if "packet loss" in line:
            m = re.search(r'(\d+(?:\.\d+)?)% packet loss', line)
            if m:
                try:
                    res["packet_loss_pct"] = float(m.group(1))
                except Exception:
                    pass

    # rtt line
    m = re.search(r'rtt .* = ([0-9./]+) ms', out)
    if m:
        parts = m.group(1).split('/')
        if len(parts) >= 4:
            try:
                res["rtt_ms"] = float(parts[1])
                res["jitter_ms"] = float(parts[3])
            except Exception:
                pass

    times = []
    for line in out.splitlines():
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


# -----------------------
# Node
# -----------------------

class NetworkQualityNode(Node):
    def __init__(self):
        super().__init__('network_quality_node')

        # Default parameters
        self.declare_parameter('probe_host', '8.8.8.8')
        self.declare_parameter('probe_interval', 2.0)   # seconds between probes
        self.declare_parameter('publish_rate', 1.0)     # Hz
        self.declare_parameter('ping_count', 3)
        self.declare_parameter('net_iface', 'eth0')
        self.declare_parameter('verbose', False)
        self.declare_parameter('alpha', 0.3)            # EWMA smoothing
        self.declare_parameter('required_up_mbps', 0.0)
        self.declare_parameter('required_down_mbps', 0.0)
        self.declare_parameter('publish_diagnostics', True)
        self.declare_parameter('enable_iperf_on_demand', False)
        self.declare_parameter('iperf3_path', 'iperf3')
        self.declare_parameter('iperf_duration', 5)

        # read params
        self._read_params()

        # publishers
        self.quality_pub = self.create_publisher(String, '/network/quality', 10)
        self.diag_pub = self.create_publisher(DiagnosticArray, '/diagnostics', 10) if self.publish_diagnostics else None

        # optional iperf service
        if self.enable_iperf_on_demand:
            from std_srvs.srv import Trigger as _Trigger
            self.create_service(_Trigger, 'run_iperf_test', self._handle_run_iperf)

        # smoothing / state (EWMA)
        self._alpha = float(self.alpha)
        self._smoothed = {
            "rtt_ms": None,
            "jitter_ms": None,
            "packet_loss_pct": None,
            "up_mbps": None,
            "down_mbps": None
        }

        # latest raw metrics (protected by lock)
        self._metrics_lock = threading.Lock()
        self._latest_raw: Dict[str, Any] = {
            "rtt_ms": None, "jitter_ms": None, "packet_loss_pct": None,
            "up_mbps": None, "down_mbps": None, "raw_ping": None,
            "iface": self.net_iface, "link_up": False, "notes": ""
        }

        # network counter previous values
        self._prev_rx: Optional[int] = None
        self._prev_tx: Optional[int] = None
        self._prev_ts: Optional[float] = None

        # Asyncio loop for ping
        self._async_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._async_thread.start()

        # timers
        self._probe_timer = None
        self._publish_timer = None
        self._create_timers()

        # dynamic param callback
        self.add_on_set_parameters_callback(self._on_param_change)

        self.get_logger().info(f"NetworkQualityNode started: probe_host={self.probe_host}, probe_interval={self.probe_interval}s, publish_rate={self.publish_rate}Hz, iface={self.net_iface}")

    # read parameters
    def _read_params(self):
        self.probe_host: str = self.get_parameter('probe_host').value
        self.probe_interval: float = float(self.get_parameter('probe_interval').value)
        self.publish_rate: float = float(self.get_parameter('publish_rate').value)
        self.ping_count: int = int(self.get_parameter('ping_count').value)
        self.net_iface: str = self.get_parameter('net_iface').value
        self.verbose: bool = bool(self.get_parameter('verbose').value)
        self.alpha: float = float(self.get_parameter('alpha').value)
        self.required_up_mbps: float = float(self.get_parameter('required_up_mbps').value)
        self.required_down_mbps: float = float(self.get_parameter('required_down_mbps').value)
        self.publish_diagnostics: bool = bool(self.get_parameter('publish_diagnostics').value)
        self.enable_iperf_on_demand: bool = bool(self.get_parameter('enable_iperf_on_demand').value)
        self.iperf3_path: str = str(self.get_parameter('iperf3_path').value)
        self.iperf_duration: int = int(self.get_parameter('iperf_duration').value)

    # timers creation/destruction
    def _create_timers(self):
        # destroy existing
        if self._probe_timer is not None:
            try:
                self.destroy_timer(self._probe_timer)
            except Exception:
                pass
            self._probe_timer = None
        if self._publish_timer is not None:
            try:
                self.destroy_timer(self._publish_timer)
            except Exception:
                pass
            self._publish_timer = None

        # create new timers
        self._probe_timer = self.create_timer(self.probe_interval, self._probe_callback)
        publish_period = max(0.01, 1.0 / max(0.01, self.publish_rate))
        self._publish_timer = self.create_timer(publish_period, self._publish_callback)

    # dynamic params
    def _on_param_change(self, params):
        changed_probe_interval = False
        changed_publish_rate = False
        changed_alpha = False

        for p in params:
            if p.name == 'net_iface' and p.type_ == Parameter.Type.STRING:
                self.net_iface = p.value
                with self._metrics_lock:
                    self._latest_raw['iface'] = self.net_iface
                self.get_logger().info(f"net_iface changed -> {self.net_iface}")
            elif p.name == 'probe_host' and p.type_ == Parameter.Type.STRING:
                self.probe_host = p.value
                self.get_logger().info(f"probe_host changed -> {self.probe_host}")
            elif p.name == 'ping_count' and p.type_ == Parameter.Type.INTEGER:
                self.ping_count = int(p.value)
                self.get_logger().info(f"ping_count changed -> {self.ping_count}")
            elif p.name == 'verbose' and p.type_ == Parameter.Type.BOOL:
                self.verbose = bool(p.value)
                self.get_logger().info(f"verbose changed -> {self.verbose}")
            elif p.name == 'probe_interval' and (p.type_ in (Parameter.Type.DOUBLE, Parameter.Type.INTEGER)):
                self.probe_interval = float(p.value)
                changed_probe_interval = True
                self.get_logger().info(f"probe_interval will change -> {self.probe_interval}s")
            elif p.name == 'publish_rate' and (p.type_ in (Parameter.Type.DOUBLE, Parameter.Type.INTEGER)):
                self.publish_rate = float(p.value)
                changed_publish_rate = True
                self.get_logger().info(f"publish_rate will change -> {self.publish_rate}Hz")
            elif p.name == 'alpha' and (p.type_ in (Parameter.Type.DOUBLE, Parameter.Type.INTEGER)):
                self.alpha = float(p.value)
                changed_alpha = True
                self.get_logger().info(f"alpha changed -> {self.alpha}")
            elif p.name == 'required_up_mbps' and (p.type_ in (Parameter.Type.DOUBLE, Parameter.Type.INTEGER)):
                self.required_up_mbps = float(p.value)
            elif p.name == 'required_down_mbps' and (p.type_ in (Parameter.Type.DOUBLE, Parameter.Type.INTEGER)):
                self.required_down_mbps = float(p.value)

        if changed_alpha:
            self._alpha = float(self.alpha)
        if changed_probe_interval or changed_publish_rate:
            self._create_timers()
        return SetParametersResult(successful=True)

    # asyncio loop runner
    def _run_async_loop(self):
        asyncio.set_event_loop(self._async_loop)
        self._async_loop.run_forever()

    # probe scheduling
    def _probe_callback(self):
        try:
            asyncio.run_coroutine_threadsafe(self._do_probe(), self._async_loop)
        except Exception as e:
            self.get_logger().error(f"Failed to schedule probe: {e}")

    async def _do_probe(self):
        """Perform a probe: ping + read counters, update smoothed metrics and latest_raw."""
        link_up = iface_is_up(self.net_iface)
        raw_ping = None

        if not link_up:
            # interface down -> minimal raw metrics and conservative smoothing update
            with self._metrics_lock:
                self._latest_raw.update({
                    "rtt_ms": None, "jitter_ms": None, "packet_loss_pct": None,
                    "up_mbps": 0.0, "down_mbps": 0.0, "raw_ping": "iface_down",
                    "iface": self.net_iface, "link_up": False, "notes": f"interface {self.net_iface} down"
                })
            # apply smoothing towards zeros/None
            self._apply_ewma(None, None, None, 0.0, 0.0)
            return

        # run ping
        try:
            ping_res = await run_ping_probe(self.probe_host, self.ping_count, timeout_per_ping_s=1)
            raw_ping = ping_res.get("raw")
        except Exception as e:
            ping_res = {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": None, "raw": f"ping_err:{e}"}
            raw_ping = ping_res["raw"]

        # read counters
        rx, tx = read_iface_bytes(self.net_iface)
        now = time.time()
        if rx is None or tx is None:
            up_mbps = 0.0
            down_mbps = 0.0
            notes = f"counters missing for {self.net_iface}"
        else:
            notes = ""
            if self._prev_rx is not None and self._prev_tx is not None and self._prev_ts is not None:
                dt = max(1e-6, now - self._prev_ts)
                rx_rate = (rx - self._prev_rx) / dt
                tx_rate = (tx - self._prev_tx) / dt
                down_mbps = (rx_rate * 8.0) / 1e6
                up_mbps = (tx_rate * 8.0) / 1e6
            else:
                up_mbps = 0.0
                down_mbps = 0.0
            self._prev_rx = rx
            self._prev_tx = tx
            self._prev_ts = now

        # update raw metrics lock-protected
        with self._metrics_lock:
            self._latest_raw.update({
                "rtt_ms": ping_res.get("rtt_ms"),
                "jitter_ms": ping_res.get("jitter_ms"),
                "packet_loss_pct": ping_res.get("packet_loss_pct"),
                "up_mbps": up_mbps,
                "down_mbps": down_mbps,
                "raw_ping": raw_ping,
                "iface": self.net_iface,
                "link_up": True,
                "notes": notes
            })

        # apply EWMA smoothing
        self._apply_ewma(ping_res.get("rtt_ms"), ping_res.get("jitter_ms"), ping_res.get("packet_loss_pct"), up_mbps, down_mbps)

    def _apply_ewma(self, rtt, jitter, loss, up_mbps, down_mbps):
        """EWMA smoothing; None new values are treated conservatively."""
        a = float(self._alpha)
        def mix(old, new):
            if new is None:
                return old if old is not None else None
            if old is None:
                return new
            return (1 - a) * old + a * new

        s = self._smoothed
        s["rtt_ms"] = mix(s.get("rtt_ms"), rtt)
        s["jitter_ms"] = mix(s.get("jitter_ms"), jitter)
        s["packet_loss_pct"] = mix(s.get("packet_loss_pct"), loss)
        s["up_mbps"] = mix(s.get("up_mbps"), up_mbps)
        s["down_mbps"] = mix(s.get("down_mbps"), down_mbps)

    # publish callback
    def _publish_callback(self):
        with self._metrics_lock:
            raw = dict(self._latest_raw)
            sm = dict(self._smoothed)

        def choose(sm_key, raw_key, default=0.0):
            v = sm.get(sm_key)
            if v is None:
                v = raw.get(raw_key)
            if v is None:
                return default
            return v

        up_mbps = choose("up_mbps", "up_mbps", 0.0)
        down_mbps = choose("down_mbps", "down_mbps", 0.0)
        quality_score = self._compute_quality_from_smoothed(sm, raw)

        topic_payload = {
            "timestamp": iso_ts_now(),
            "quality_score": round(float(quality_score), 2),
            "up_mbps": round(float(up_mbps or 0.0), 3),
            "down_mbps": round(float(down_mbps or 0.0), 3)
        }
        if self.verbose:
            topic_payload.update({
                "iface": raw.get("iface"),
                "link_up": raw.get("link_up"),
            })

        try:
            self.quality_pub.publish(String(data=json.dumps(topic_payload)))
        except Exception as e:
            self.get_logger().error(f"Failed to publish /network/quality: {e}")

        # diagnostics
        if self.diag_pub:
            try:
                da = DiagnosticArray()
                da.header.stamp = self.get_clock().now().to_msg()
                ds = DiagnosticStatus()
                ds.name = "network/quality"
                q = float(quality_score or 0.0)
                if q >= 80.0:
                    ds.level = DiagnosticStatus.OK
                    ds.message = "OK"
                elif q >= 50.0:
                    ds.level = DiagnosticStatus.WARN
                    ds.message = "Degraded"
                else:
                    ds.level = DiagnosticStatus.ERROR
                    ds.message = "Poor/Down"

                ds.values.append(KeyValue(key="quality_score", value=str(round(q,2))))
                ds.values.append(KeyValue(key="smoothed_rtt_ms", value=str(sm.get("rtt_ms"))))
                ds.values.append(KeyValue(key="smoothed_jitter_ms", value=str(sm.get("jitter_ms"))))
                ds.values.append(KeyValue(key="smoothed_packet_loss_pct", value=str(sm.get("packet_loss_pct"))))
                ds.values.append(KeyValue(key="smoothed_up_mbps", value=str(sm.get("up_mbps"))))
                ds.values.append(KeyValue(key="smoothed_down_mbps", value=str(sm.get("down_mbps"))))
                ds.values.append(KeyValue(key="raw_rtt_ms", value=str(raw.get("rtt_ms"))))
                ds.values.append(KeyValue(key="raw_jitter_ms", value=str(raw.get("jitter_ms"))))
                ds.values.append(KeyValue(key="raw_packet_loss_pct", value=str(raw.get("packet_loss_pct"))))
                ds.values.append(KeyValue(key="raw_up_mbps", value=str(raw.get("up_mbps"))))
                ds.values.append(KeyValue(key="raw_down_mbps", value=str(raw.get("down_mbps"))))
                ds.values.append(KeyValue(key="iface", value=str(raw.get("iface"))))
                ds.values.append(KeyValue(key="link_up", value=str(raw.get("link_up"))))
                ds.values.append(KeyValue(key="raw_ping", value=str(raw.get("raw_ping") or "")[:200]))
                if raw.get("notes"):
                    ds.values.append(KeyValue(key="notes", value=str(raw.get("notes"))))
                if self.required_up_mbps > 0.0:
                    ds.values.append(KeyValue(key="required_up_mbps", value=str(self.required_up_mbps)))
                if self.required_down_mbps > 0.0:
                    ds.values.append(KeyValue(key="required_down_mbps", value=str(self.required_down_mbps)))
                da.status.append(ds)
                self.diag_pub.publish(da)
            except Exception as e:
                self.get_logger().error(f"Failed to publish /diagnostics: {e}")

    def _compute_quality_from_smoothed(self, smoothed: Dict[str, Any], raw: Dict[str, Any]) -> float:
        loss = smoothed.get("packet_loss_pct")
        rtt = smoothed.get("rtt_ms")
        up = smoothed.get("up_mbps") or raw.get("up_mbps") or 0.0
        down = smoothed.get("down_mbps") or raw.get("down_mbps") or 0.0

        score = 100.0

        if loss is None:
            score -= 20.0
        else:
            score -= min(100.0, loss) * 0.8

        if rtt is None:
            score -= 10.0
        else:
            if rtt > 50.0:
                score -= min(50.0, (rtt - 50.0) / 10.0)

        up_req = self.required_up_mbps
        down_req = self.required_down_mbps
        bw_penalty = 0.0
        if up_req > 0.0:
            gap = max(0.0, up_req - (up or 0.0))
            bw_penalty += (gap / max(1.0, up_req)) * 5.0
        if down_req > 0.0:
            gap = max(0.0, down_req - (down or 0.0))
            bw_penalty += (gap / max(1.0, down_req)) * 5.0
        score -= bw_penalty

        score = max(0.0, min(100.0, score))
        return round(score, 2)

    # iperf service (optional)
    def _handle_run_iperf(self, request, response):
        try:
            iperf_cmd = shutil.which(self.iperf3_path) or self.iperf3_path
            th = threading.Thread(target=self._run_iperf3_test_blocking, args=(iperf_cmd, self.probe_host, self.iperf_duration), daemon=True)
            th.start()
            response.success = True
            response.message = f"iperf3 started -> {self.probe_host}"
        except Exception as e:
            response.success = False
            response.message = str(e)
        return response

    def _run_iperf3_test_blocking(self, iperf_cmd: str, host: str, duration: int):
        cmd = [iperf_cmd, "-c", host, "-t", str(duration), "-J"]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=duration + 10, text=True)
            out = proc.stdout or ""
            snippet = out[:512] + ("..." if len(out) > 512 else "")
            self.get_logger().info(f"iperf3 result: {snippet}")
        except Exception as e:
            self.get_logger().warn(f"iperf3 failed: {e}")

    def destroy_node(self):
        # stop asyncio loop cleanly
        try:
            if hasattr(self, "_async_loop") and self._async_loop.is_running():
                self._async_loop.call_soon_threadsafe(self._async_loop.stop)
                if hasattr(self, "_async_thread"):
                    self._async_thread.join(timeout=2.0)
        except Exception:
            pass
        # destroy timers
        try:
            if self._probe_timer is not None:
                self.destroy_timer(self._probe_timer)
        except Exception:
            pass
        try:
            if self._publish_timer is not None:
                self.destroy_timer(self._publish_timer)
        except Exception:
            pass
        super().destroy_node()


# -----------------------
# main
# -----------------------

def main(args=None):
    rclpy.init(args=args)
    node = NetworkQualityNode()
    try:
        executor = rclpy.executors.MultiThreadedExecutor()
        executor.add_node(node)
        try:
            executor.spin()
        except KeyboardInterrupt:
            # user pressed Ctrl-C
            node.get_logger().info("KeyboardInterrupt received — shutting down")
    finally:
        # perform cleanup carefully; rclpy.shutdown may have been called already
        try:
            executor.shutdown()
        except Exception:
            pass
        try:
            node.destroy_node()
        except Exception:
            pass
        try:
            rclpy.shutdown()
        except Exception:
            # ignore errors like "rcl_shutdown already called"
            pass


if __name__ == "__main__":
    main()
