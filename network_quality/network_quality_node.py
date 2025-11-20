#!/usr/bin/env python3
"""
network_quality_node.py
- Publishes JSON on /network/quality (std_msgs/String)
- Publishes diagnostics on /diagnostics (diagnostic_msgs/DiagnosticArray)
- Provides an optional iperf3 on-demand service (std_srvs/Trigger) when enabled
"""

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

def iso_ts_now():
    return datetime.datetime.utcnow().isoformat(timespec='milliseconds') + 'Z'

def read_iface_bytes(iface):
    try:
        base = f"/sys/class/net/{iface}/statistics"
        rx_path = os.path.join(base, "rx_bytes")
        tx_path = os.path.join(base, "tx_bytes")
        with open(rx_path, "r") as f:
            rx = int(f.read().strip())
        with open(tx_path, "r") as f:
            tx = int(f.read().strip())
        return rx, tx
    except Exception:
        return None, None

def iface_is_up(iface):
    try:
        path = f"/sys/class/net/{iface}/operstate"
        with open(path, "r") as f:
            state = f.read().strip()
        return state == "up"
    except Exception:
        return False

async def run_ping_probe(host: str, count: int, timeout_per_ping_s=1):
    if shutil.which("ping") is None:
        return {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": 100.0, "raw": "ping missing", "times_ms": []}
    cmd = f"ping -c {count} -W {timeout_per_ping_s} {host}"
    try:
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        out = stdout.decode(errors='ignore')
        err = stderr.decode(errors='ignore')
    except Exception as e:
        return {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": 100.0, "raw": f"ping failed: {e}", "times_ms": []}

    res = {"rtt_ms": None, "jitter_ms": None, "packet_loss_pct": 100.0, "raw": out or err, "times_ms": []}

    for line in out.splitlines():
        if "packet loss" in line:
            m = re.search(r'(\d+(\.\d+)?)% packet loss', line)
            if m:
                try:
                    res["packet_loss_pct"] = float(m.group(1))
                except Exception:
                    pass

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

class NetworkQualityNode(Node):
    def __init__(self):
        super().__init__('network_quality_node')

        # params
        self.declare_parameter('probe_host', '8.8.8.8')
        self.declare_parameter('probe_interval', 5.0)
        self.declare_parameter('ping_count', 4)
        self.declare_parameter('net_iface', 'eth0')
        self.declare_parameter('bandwidth_window', 5.0)
        self.declare_parameter('publish_diagnostics', True)
        self.declare_parameter('required_up_mbps', 2.0)
        self.declare_parameter('required_down_mbps', 2.0)
        self.declare_parameter('enable_iperf_on_demand', False)
        self.declare_parameter('iperf3_path', 'iperf3')
        self.declare_parameter('iperf_duration', 5)

        self.probe_host = self.get_parameter('probe_host').value
        self.probe_interval = float(self.get_parameter('probe_interval').value)
        self.ping_count = int(self.get_parameter('ping_count').value)
        self.net_iface = self.get_parameter('net_iface').value
        self.bandwidth_window = float(self.get_parameter('bandwidth_window').value)
        self.publish_diagnostics = bool(self.get_parameter('publish_diagnostics').value)
        self.required_up_mbps = float(self.get_parameter('required_up_mbps').value)
        self.required_down_mbps = float(self.get_parameter('required_down_mbps').value)
        self.enable_iperf_on_demand = bool(self.get_parameter('enable_iperf_on_demand').value)
        self.iperf3_path = str(self.get_parameter('iperf3_path').value)
        self.iperf_duration = int(self.get_parameter('iperf_duration').value)

        # publishers & service
        self.quality_pub = self.create_publisher(String, '/network/quality', 10)
        self.diag_pub = self.create_publisher(DiagnosticArray, '/diagnostics', 10) if self.publish_diagnostics else None
        if self.enable_iperf_on_demand:
            self.create_service(Trigger, 'run_iperf_test', self._handle_run_iperf)

        # internal counters for bandwidth calc
        self._last_rx = None
        self._last_tx = None
        self._last_bytes_ts = None

        self.get_logger().info(f"Probing {self.probe_host} every {self.probe_interval}s on iface {self.net_iface}")
        self.create_timer(self.probe_interval, self._trigger_probe)

    def _trigger_probe(self):
        asyncio.ensure_future(self._do_probe())

    async def _do_probe(self):
        ping_res = await run_ping_probe(self.probe_host, self.ping_count)

        # bandwidth
        rx, tx = read_iface_bytes(self.net_iface)
        now = time.time()
        up_mbps = None
        down_mbps = None
        if rx is not None and tx is not None:
            if self._last_rx is not None and self._last_tx is not None and self._last_bytes_ts is not None:
                dt = now - self._last_bytes_ts
                if dt > 0:
                    rx_rate = (rx - self._last_rx) / dt
                    tx_rate = (tx - self._last_tx) / dt
                    down_mbps = (rx_rate * 8.0) / 1e6
                    up_mbps = (tx_rate * 8.0) / 1e6
            self._last_rx = rx
            self._last_tx = tx
            self._last_bytes_ts = now

        link_up = iface_is_up(self.net_iface)

        quality = self._compute_quality_score(
            packet_loss=ping_res.get('packet_loss_pct', 100.0),
            rtt_ms=ping_res.get('rtt_ms'),
            down_mbps=down_mbps,
            up_mbps=up_mbps
        )

        req_check = self._check_requirements(up_mbps, down_mbps)

        payload = {
            "timestamp": iso_ts_now(),
            "probe_host": self.probe_host,
            "rtt_ms": ping_res.get('rtt_ms'),
            "jitter_ms": ping_res.get('jitter_ms'),
            "packet_loss_pct": ping_res.get('packet_loss_pct'),
            "up_mbps": up_mbps,
            "down_mbps": down_mbps,
            "iface": self.net_iface,
            "link_up": link_up,
            "quality_score": quality,
            "requirements_met": req_check["requirements_met"],
            "up_gap_mbps": req_check["up_gap_mbps"],
            "down_gap_mbps": req_check["down_gap_mbps"],
            "raw_ping": (ping_res.get('raw') or '')[:512],
            "notes": ""
        }

        # publish JSON
        msg = String()
        msg.data = json.dumps(payload)
        self.quality_pub.publish(msg)

        # diagnostics
        if self.diag_pub is not None:
            da = DiagnosticArray()
            da.header.stamp = self.get_clock().now().to_msg()
            ds = DiagnosticStatus()
            ds.name = "network/quality"
            pkt_loss = payload["packet_loss_pct"] if payload["packet_loss_pct"] is not None else 100.0
            rtt = payload["rtt_ms"]
            if pkt_loss > 30.0 or (rtt is not None and rtt > 2000):
                ds.level = DiagnosticStatus.ERROR
                ds.message = "Network DOWN / very poor"
            elif pkt_loss > 5.0 or (rtt is not None and rtt > 500) or payload["quality_score"] < 60:
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

        self.get_logger().debug("Published network quality JSON")

    def _compute_quality_score(self, packet_loss, rtt_ms, down_mbps, up_mbps):
        score = 100.0
        if packet_loss is None:
            packet_loss = 100.0
        score -= min(packet_loss, 100.0) * 0.9
        if rtt_ms is None:
            rtt_ms = 2000.0
        if rtt_ms > 50:
            score -= max(0.0, (rtt_ms - 50.0) / 10.0)
        bw = 0.0
        if down_mbps is not None:
            bw = down_mbps
        if up_mbps is not None and up_mbps > bw:
            bw = up_mbps
        if bw > 1.0:
            score += min(10.0, (bw / 5.0))
        score = max(0.0, min(100.0, score))
        return round(score, 2)

    def _check_requirements(self, up_mbps, down_mbps):
        up_ok = None
        down_ok = None
        up_gap = 0.0
        down_gap = 0.0
        if up_mbps is None:
            up_ok = False
            up_gap = self.required_up_mbps
        else:
            up_ok = (up_mbps >= self.required_up_mbps)
            up_gap = max(0.0, self.required_up_mbps - up_mbps)
        if down_mbps is None:
            down_ok = False
            down_gap = self.required_down_mbps
        else:
            down_ok = (down_mbps >= self.required_down_mbps)
            down_gap = max(0.0, self.required_down_mbps - down_mbps)
        return {"up_ok": up_ok, "down_ok": down_ok, "up_gap_mbps": round(up_gap,3), "down_gap_mbps": round(down_gap,3), "requirements_met": (up_ok and down_ok)}

    # --- iperf runner (blocking)
    def _run_iperf3_test_blocking(self, server_host, duration=5):
        ipp = shutil.which(self.iperf3_path) or self.iperf3_path
        cmd = f"{ipp} -c {server_host} -t {duration} -J"
        try:
            proc = subprocess.run(cmd.split(), capture_output=True, timeout=duration+10, text=True)
        except Exception as e:
            return {"success": False, "error": f"iperf failed: {e}"}
        out = proc.stdout
        if not out:
            return {"success": False, "error": proc.stderr or "no output"}
        try:
            j = json.loads(out)
            # try to find bits_per_second in JSON
            def find_bps(obj):
                if isinstance(obj, dict):
                    for k,v in obj.items():
                        if k == 'bits_per_second':
                            return v
                        res = find_bps(v)
                        if res is not None:
                            return res
                elif isinstance(obj, list):
                    for it in obj:
                        res = find_bps(it)
                        if res is not None:
                            return res
                return None
            bits_per_second = find_bps(j)
            if bits_per_second is None:
                return {"success": False, "error": "Could not parse iperf JSON", "raw": out}
            mbps = float(bits_per_second)/1e6
            return {"success": True, "mbps": round(mbps,3), "raw_json": j}
        except Exception as e:
            return {"success": False, "error": f"parse error: {e}", "raw": out}

    def run_iperf3_test_async(self, server_host, duration=None):
        if duration is None:
            duration = self.iperf_duration
        def target():
            self.get_logger().info(f"Starting iperf3 test to {server_host} for {duration}s")
            res = self._run_iperf3_test_blocking(server_host, duration)
            if res.get("success"):
                self.get_logger().info(f"iperf result: {res['mbps']} Mbps")
            else:
                self.get_logger().warn(f"iperf failed: {res.get('error')}")
        t = threading.Thread(target=target, daemon=True)
        t.start()
        return True

    def _handle_run_iperf(self, req, resp):
        try:
            self.run_iperf3_test_async(self.probe_host, duration=self.iperf_duration)
            resp.success = True
            resp.message = f"iperf test started to {self.probe_host}"
        except Exception as e:
            resp.success = False
            resp.message = str(e)
        return resp

def main(args=None):
    rclpy.init(args=args)
    node = NetworkQualityNode()
    try:
        executor = rclpy.executors.MultiThreadedExecutor()
        executor.add_node(node)
        executor.spin()
    finally:
        node.destroy_node()
        rclpy.shutdown()
