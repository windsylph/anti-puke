// Motion Dots overlay wire protocol.
//
// The bridge (py_modules/sensor_bridge.py) owns ALL geometry and smoothing; the
// overlay is a dumb renderer that draws whatever dots it is told to draw. Keeping
// the split here means tuning the look never requires rebuilding the C++ binary.
//
// The Python side mirrors these layouts in py_modules/protocol.py. If you change
// anything here, change it there too -- tests/test_protocol.py asserts the two
// agree on struct sizes and the magic values.
//
// All integers little-endian (the Deck is x86-64; the two peers are always on the
// same machine, so no byte-order negotiation is needed).

#ifndef MOTIONDOTS_PROTOCOL_H
#define MOTIONDOTS_PROTOCOL_H

#include <cstdint>

namespace motiondots
{
    constexpr uint16_t kProtocolVersion = 1;

    // 'MDHL', 'MDIN', 'MDOT' as little-endian u32 of the ASCII bytes.
    constexpr uint32_t kMagicHello = 0x4C48444DU; // "MDHL"
    constexpr uint32_t kMagicInfo  = 0x4E49444DU; // "MDIN"
    constexpr uint32_t kMagicDots  = 0x544F444DU; // "MDOT"

    constexpr int kDefaultOverlayPort = 27761;

    // Hard cap so a malformed/hostile datagram can never make us allocate or draw
    // an unbounded number of dots. The ring is ~12-14 dots; 256 is generous.
    constexpr uint16_t kMaxDots = 256;

    // Bridge -> overlay. "I am here, tell me your resolution."
    struct __attribute__((packed)) HelloPacket
    {
        uint32_t magic;   // kMagicHello
        uint16_t version; // kProtocolVersion
        uint16_t pad;
    };

    // Overlay -> bridge, sent in reply to every Hello. Lets the bridge lay the ring
    // out in real pixels without having to assume 1280x800.
    struct __attribute__((packed)) InfoPacket
    {
        uint32_t magic;   // kMagicInfo
        uint16_t version; // kProtocolVersion
        uint16_t pad;
        uint32_t width;   // overlay window size in pixels
        uint32_t height;
    };

    // Bridge -> overlay, ~60Hz. Followed immediately by `count` Dot records.
    struct __attribute__((packed)) DotsHeader
    {
        uint32_t magic;   // kMagicDots
        uint16_t version; // kProtocolVersion
        uint16_t count;   // number of Dot records that follow
        float    radius;  // dot radius in pixels, shared by every dot
    };

    struct __attribute__((packed)) Dot
    {
        float x;     // pixels, origin top-left
        float y;     // pixels, origin top-left
        float alpha; // 0..1
    };

    static_assert(sizeof(HelloPacket) == 8,  "HelloPacket layout changed");
    static_assert(sizeof(InfoPacket)  == 16, "InfoPacket layout changed");
    static_assert(sizeof(DotsHeader)  == 12, "DotsHeader layout changed");
    static_assert(sizeof(Dot)         == 12, "Dot layout changed");
}

#endif
