"""Manual end-to-end check: fake sensor service -> real bridge -> fake overlay.

Not part of the unit suite; this is the harness used to eyeball the full data
path without a Steam Deck. Run: python3 tests/loopback_check.py
"""
import json, math, os, socket, sys, threading, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py_modules"))
import config, protocol
from sensor_bridge import SensorBridge

SENSOR_PORT, OVERLAY_PORT = 37760, 37761
W, H = 1280, 800

def fake_sensor(stop):
    """Mimics the real service: wait for a client datagram, then stream 60Hz JSON."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", SENSOR_PORT))
    # Non-blocking: the real service registers clients on one thread and streams
    # on another, so it must not stall the 60Hz send cadence waiting for a
    # registration datagram.
    s.setblocking(False)
    client, t0, n = None, time.monotonic(), 0
    while not stop.is_set():
        try:
            _, client = s.recvfrom(1024)
        except BlockingIOError:
            pass
        if client:
            t = time.monotonic() - t0
            # Rest at a 30-degree hold, then tilt right starting at t=1.5s.
            tilt = 0.0 if t < 1.5 else 0.30
            s.sendto(json.dumps({
                "timestamp": int(time.time()*1e6),
                # accel.x = right, accel.z = down; plus a little noise so the
                # smoothing has something real to do.
                "accel": {"x": tilt + 0.01*math.sin(t*97), "y": -0.5, "z": 0.5 + 0.01*math.cos(t*89)},
                "gyro":  {"pitch": 0.4*math.sin(t*53), "yaw": 0.0, "roll": 0.4*math.cos(t*61)},
                "frameId": n, "magnitude": {"accel": 1.0, "gyro": 0.4},
            }).encode(), client)
            n += 1
        time.sleep(1/60)
    s.close()

def fake_overlay(stop, received):
    """Mimics the renderer: answer HELLO with INFO, collect DOTS frames."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", OVERLAY_PORT)); s.settimeout(0.2)
    while not stop.is_set():
        try:
            payload, addr = s.recvfrom(8192)
        except socket.timeout:
            continue
        magic = int.from_bytes(payload[:4], "little")
        if magic == protocol.MAGIC_HELLO:
            import struct
            s.sendto(struct.pack("<IHHII", protocol.MAGIC_INFO, protocol.PROTOCOL_VERSION, 0, W, H), addr)
        elif magic == protocol.MAGIC_DOTS:
            decoded = protocol.decode_dots(payload)
            if decoded:
                received.append((time.monotonic(), decoded))
    s.close()

def main():
    stop = threading.Event(); received = []
    threads = [threading.Thread(target=fake_sensor, args=(stop,), daemon=True),
               threading.Thread(target=fake_overlay, args=(stop, received), daemon=True)]
    for t in threads: t.start()
    time.sleep(0.3)

    settings = config.Settings()
    settings.fallback_width, settings.fallback_height = 640, 480  # deliberately wrong
    bridge = SensorBridge(settings, SENSOR_PORT, OVERLAY_PORT)
    bridge.open()
    try:
        bridge.run(max_seconds=3.0)
    finally:
        bridge.close()
    stop.set()
    for t in threads: t.join(timeout=1)

    print(f"samples in: {bridge._samples_seen}   frames out: {bridge._frames_sent}")
    print(f"overlay received: {len(received)} frames")
    assert received, "overlay received nothing"

    print(f"resolution learned from overlay: {bridge.width}x{bridge.height} (fallback was 640x480)")
    assert (bridge.width, bridge.height) == (W, H), "handshake did not update resolution"

    rate = len(received) / 3.0
    print(f"effective output rate: {rate:.1f} Hz")

    radius, first = received[0][1]
    _, last = received[-1][1]
    print(f"dot count: {len(first)}  radius: {radius}")

    # Where did the ring end up before vs after the tilt?
    def centroid(dots): return (sum(d.x for d in dots)/len(dots), sum(d.y for d in dots)/len(dots))
    early = [f for t, f in received if t - received[0][0] < 1.0]
    late = [f for t, f in received if t - received[0][0] > 2.5]
    cx0, cy0 = centroid(early[len(early)//2][1])
    cx1, cy1 = centroid(late[len(late)//2][1])
    print(f"ring centroid before tilt: ({cx0:.2f}, {cy0:.2f})")
    print(f"ring centroid after  tilt: ({cx1:.2f}, {cy1:.2f})")
    print(f"shift: dx={cx1-cx0:+.2f}px dy={cy1-cy0:+.2f}px  (tilt was +x / to the right)")

    assert all(0 <= d.x <= W and 0 <= d.y <= H for _, f in received for d in f[1]), "dot left the screen"
    print("all dots stayed on screen")

    # Idle drift: during the pre-tilt window the dots must still be moving, but
    # only by a sub-pixel amount (PRD 6.1: near-static, never a hard freeze).
    idle = [f for t, f in received if 0.3 < t - received[0][0] < 1.2]
    a, b = idle[0][1], idle[3][1]
    moved = max(abs(p.x-q.x) + abs(p.y-q.y) for p, q in zip(a, b))
    print(f"max per-dot movement across 3 idle frames: {moved:.4f}px "
          f"(must be > 0 and sub-pixel)")
    assert moved > 0.0, "dots are frozen"
    assert moved < 1.0, f"idle drift is not sub-pixel: {moved}px"
    print("\nLOOPBACK OK")

if __name__ == "__main__":
    main()
