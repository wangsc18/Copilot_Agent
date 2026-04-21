import struct
import unittest

from xpc import XPlaneConnect


class _FakeSocket:
    def __init__(self):
        self.packet = None
        self.addr = None

    def sendto(self, buffer, _flags, addr):
        self.packet = buffer
        self.addr = addr

    def close(self):
        return None


class XpcTextEncodingTests(unittest.TestCase):
    def test_send_text_uses_utf8_byte_length(self):
        client = XPlaneConnect.__new__(XPlaneConnect)
        fake_socket = _FakeSocket()
        client.socket = fake_socket
        client.xpDst = ("127.0.0.1", 49009)

        msg = "中文A"
        client.sendTEXT(msg)

        packet = fake_socket.packet
        self.assertIsNotNone(packet)
        self.assertEqual(packet[:4], b"TEXT")

        x, y, msg_len = struct.unpack_from("<iiB", packet, 5)
        self.assertEqual(x, -1)
        self.assertEqual(y, -1)

        payload = packet[14:]
        self.assertEqual(msg_len, len(msg.encode("utf-8")))
        self.assertEqual(payload, msg.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
