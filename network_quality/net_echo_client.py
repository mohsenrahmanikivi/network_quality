#!/usr/bin/env python3
"""
net_echo_client:
- publishes seq/timestamp JSON to /net_echo_in
- listens to /net_echo_out and matches seq to compute RTT and packet loss
- publishes summary on /network/quality (augment) or logs
"""
import rclpy
from rclpy.node import Node
from std_msgs.msg import String
import json
import time
import threading

class NetEchoClient(Node):
    def __init__(self):
        super().__init__('net_echo_client')
        self.pub = self.create_publisher(String, '/net_echo_in', 10)
        self.sub = self.create_subscription(String, '/net_echo_out', self.cb, 10)
        self.seq = 0
        self.pending = {}  # seq -> send_ts
        self.lock = threading.Lock()
        self.send_rate = 2.0  # msgs per second
        self.timeout = 2.0
        self.timer = self.create_timer(1.0/self.send_rate, self.send_msg)
        self.summary_timer = self.create_timer(5.0, self.print_summary)

        self.sent = 0
        self.recv = 0

        self.get_logger().info("NetEchoClient: sending to /net_echo_in, listening on /net_echo_out")

    def send_msg(self):
        self.seq += 1
        s = self.seq
        payload = {"seq": s, "ts": time.time()}
        msg = String()
        msg.data = json.dumps(payload)
        with self.lock:
            self.pending[s] = payload["ts"]
            self.sent += 1
        self.pub.publish(msg)

    def cb(self, msg):
        try:
            j = json.loads(msg.data)
            seq = j.get("seq")
            if seq is None:
                return
            recv_ts = time.time()
            with self.lock:
                ts = self.pending.pop(seq, None)
                if ts is not None:
                    rtt = (recv_ts - ts) * 1000.0
                    self.get_logger().info(f"echo seq={seq} rtt_ms={rtt:.1f}")
                    self.recv += 1
        except Exception:
            pass

    def print_summary(self):
        with self.lock:
            sent = self.sent
            recv = self.recv
            pending = len(self.pending)
        loss_pct = 0.0
        if sent > 0:
            loss_pct = 100.0 * (sent - recv) / sent
        self.get_logger().info(f"Echo summary: sent={sent} recv={recv} loss_pct={loss_pct:.2f} pending={pending}")

def main(args=None):
    rclpy.init(args=args)
    node = NetEchoClient()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
