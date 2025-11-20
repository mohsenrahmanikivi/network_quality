#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from std_msgs.msg import String

class NetEchoServer(Node):
    def __init__(self):
        super().__init__('net_echo_server')
        self.pub = self.create_publisher(String, '/net_echo_out', 10)
        self.sub = self.create_subscription(String, '/net_echo_in', self.cb, 10)
        self.get_logger().info("NetEchoServer: listening on /net_echo_in, echoing to /net_echo_out")

    def cb(self, msg):
        # echo immediately
        self.pub.publish(msg)

def main(args=None):
    rclpy.init(args=args)
    node = NetEchoServer()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
