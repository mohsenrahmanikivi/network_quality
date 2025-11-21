import rclpy
from rclpy.node import Node
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
import asyncio
import websockets
import json
from datetime import datetime
import threading

# Connect to Foxglove bridge on localhost
ROBOT_WS_URL = "ws://localhost:8765"

class FoxgloveStatusDiagnostics(Node):
    def __init__(self):
        super().__init__('foxglove_status_diagnostics')
        self.pub = self.create_publisher(DiagnosticArray, '/network_quality', 1)
        self.loop = asyncio.new_event_loop()
        self.status_data = {}
        self.total_dropped_events = 0
        self.get_logger().info(f"Starting Foxglove WebSocket network quality monitor at {ROBOT_WS_URL}")

        # Background thread for WebSocket
        t = threading.Thread(target=self._start_ws_loop, daemon=True)
        t.start()

        # Publish Diagnostics every second
        self.create_timer(1.0, self._publish_diagnostics)

    def _start_ws_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._ws_monitor())

    async def _ws_monitor(self):
        while True:
            try:
                async with websockets.connect(ROBOT_WS_URL) as ws:
                    self.get_logger().info(f"Connected to Foxglove bridge at {ROBOT_WS_URL}")

                    # Subscribe to status messages
                    subscribe_msg = {
                        "op": "subscribe",
                        "id": "status_sub",
                        "topic": "status"
                    }
                    await ws.send(json.dumps(subscribe_msg))

                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)

                        # Status messages
                        if data.get("type") == "status" or "status" in data:
                            status = data.get("status") or data["status"]
                            name = status.get("name", "unknown")
                            self.status_data[name] = status

                            # Update total dropped events if send_buffer
                            if name == "send_buffer":
                                values = status.get("values", [])
                                for v in values:
                                    if v.get("key") == "total_events":
                                        try:
                                            self.total_dropped_events = int(v.get("value", 0))
                                        except ValueError:
                                            self.total_dropped_events = 0

            except Exception as e:
                self.get_logger().warn(f"WebSocket connection failed: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    def _publish_diagnostics(self):
        diag_array = DiagnosticArray()
        diag_array.header.stamp = self.get_clock().now().to_msg()

        for name, status in self.status_data.items():
            diag = DiagnosticStatus()
            diag.name = f"network_quality/{name}"
            level = status.get("level", "ok").lower()
            diag.level = {
                "ok": DiagnosticStatus.OK,
                "warn": DiagnosticStatus.WARN,
                "error": DiagnosticStatus.ERROR
            }.get(level, DiagnosticStatus.UNKNOWN)
            diag.message = status.get("message", "")

            # Copy all values
            values = status.get("values", [])
            for v in values:
                key = v.get("key", "")
                value = str(v.get("value", ""))
                diag.values.append(KeyValue(key=key, value=value))

            # Add a computed network health metric (packet loss percent approximation)
            if name == "send_buffer":
                packet_loss_percent = min(self.total_dropped_events, 100)
                diag.values.append(KeyValue(key="packet_loss_percent", value=str(packet_loss_percent)))

            diag_array.status.append(diag)

        self.pub.publish(diag_array)

def main(args=None):
    rclpy.init(args=args)
    node = FoxgloveStatusDiagnostics()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.shutdown()
