// Motion Dots overlay renderer.
//
// Trimmed fork of https://github.com/TheLogicMaster/OverLaid (BSD-3-Clause).
//
// What we reused is the part that actually matters and is hard to rediscover:
// gamescope will composite an ordinary Xwayland window on top of the running game
// if that window carries the GAMESCOPE_EXTERNAL_OVERLAY property. Everything else
// (ImGui, the text/image widget system, JSON widget config, stb_image) was for
// OverLaid's general-purpose widgets and is dropped -- we draw one thing, circles.
//
// Dropping ImGui is not just tidiness: it removes ~40k lines of vendored code and
// a per-frame UI rebuild from something that runs for hours behind a game (PRD 7.1).
// Circles are drawn as one instanced-free quad batch in a single draw call.
//
// Dot positions arrive over UDP from the Python bridge at ~60Hz. We never block on
// the socket: each frame we drain everything pending and keep only the newest
// packet, so if the bridge stalls we keep rendering the last known state and if it
// runs ahead we skip straight to current rather than replaying stale frames.

#include <GL/glew.h>
#include <GLFW/glfw3.h>

#include <X11/X.h>
#include <X11/Xatom.h>
#include <X11/Xlib.h>

#define GLFW_EXPOSE_NATIVE_X11
#include <GLFW/glfw3native.h>

#ifdef MOTIONDOTS_HAVE_XFIXES
#include <X11/extensions/Xfixes.h>
#include <X11/extensions/shape.h> // ShapeInput
#endif

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <csignal>
#include <string>
#include <vector>

#include "protocol.h"

using namespace motiondots;

namespace
{

const char *kGamescopeOverlayProperty = "GAMESCOPE_EXTERNAL_OVERLAY";

volatile std::sig_atomic_t g_stop = 0;
void OnSignal(int) { g_stop = 1; }

// ---------------------------------------------------------------------------
// Shaders
// ---------------------------------------------------------------------------
// Each dot is a screen-aligned quad. The fragment shader turns it into an
// antialiased disc by distance from the quad centre, which gives clean edges at
// any radius without any texture or multisampling.

const char *kVertexShader = R"GLSL(
#version 130

in vec2  aPos;    // pixel coordinates, origin top-left
in vec2  aLocal;  // -1..1 within the dot's quad
in float aAlpha;

out vec2  vLocal;
out float vAlpha;

uniform vec2 uViewport; // pixels

void main()
{
    vLocal = aLocal;
    vAlpha = aAlpha;

    // pixel space (y down) -> normalised device coords (y up)
    vec2 ndc = vec2(
        (aPos.x / uViewport.x) * 2.0 - 1.0,
        1.0 - (aPos.y / uViewport.y) * 2.0
    );
    gl_Position = vec4(ndc, 0.0, 1.0);
}
)GLSL";

const char *kFragmentShader = R"GLSL(
#version 130

in vec2  vLocal;
in float vAlpha;

out vec4 fragColor;

uniform float uFeather; // edge softness in local units (= 1 pixel)

void main()
{
    float d = length(vLocal);
    // 1 inside the disc, falling to 0 across roughly one pixel at the rim.
    float coverage = 1.0 - smoothstep(1.0 - uFeather, 1.0, d);
    if (coverage <= 0.0)
        discard;
    // Pure white dots, premultiplied by coverage and the dot's own alpha.
    fragColor = vec4(1.0, 1.0, 1.0, vAlpha * coverage);
}
)GLSL";

GLuint CompileShader(GLenum type, const char *src, const char *label)
{
    GLuint shader = glCreateShader(type);
    glShaderSource(shader, 1, &src, nullptr);
    glCompileShader(shader);

    GLint ok = GL_FALSE;
    glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
    if (!ok)
    {
        char log[1024];
        glGetShaderInfoLog(shader, sizeof(log), nullptr, log);
        std::fprintf(stderr, "[motiondots-overlay] %s shader failed to compile: %s\n", label, log);
        glDeleteShader(shader);
        return 0;
    }
    return shader;
}

GLuint BuildProgram()
{
    GLuint vs = CompileShader(GL_VERTEX_SHADER, kVertexShader, "vertex");
    if (!vs) return 0;
    GLuint fs = CompileShader(GL_FRAGMENT_SHADER, kFragmentShader, "fragment");
    if (!fs) { glDeleteShader(vs); return 0; }

    GLuint prog = glCreateProgram();
    glAttachShader(prog, vs);
    glAttachShader(prog, fs);
    // Bind before linking so we do not depend on driver-assigned locations.
    glBindAttribLocation(prog, 0, "aPos");
    glBindAttribLocation(prog, 1, "aLocal");
    glBindAttribLocation(prog, 2, "aAlpha");
    glLinkProgram(prog);

    GLint ok = GL_FALSE;
    glGetProgramiv(prog, GL_LINK_STATUS, &ok);
    if (!ok)
    {
        char log[1024];
        glGetProgramInfoLog(prog, sizeof(log), nullptr, log);
        std::fprintf(stderr, "[motiondots-overlay] program failed to link: %s\n", log);
        glDeleteProgram(prog);
        prog = 0;
    }

    glDeleteShader(vs);
    glDeleteShader(fs);
    return prog;
}

// ---------------------------------------------------------------------------
// UDP input
// ---------------------------------------------------------------------------

struct DotState
{
    std::vector<Dot> dots;
    float            radius = 4.0f;
};

int OpenSocket(int port)
{
    int fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (fd < 0)
    {
        std::perror("[motiondots-overlay] socket");
        return -1;
    }

    // Non-blocking: the render loop must never stall waiting for the bridge.
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0)
    {
        std::perror("[motiondots-overlay] fcntl");
        close(fd);
        return -1;
    }

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port   = htons(static_cast<uint16_t>(port));
    // Loopback only. The bridge is always on this machine and there is no reason
    // to accept dot positions from the network.
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

    if (bind(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0)
    {
        std::perror("[motiondots-overlay] bind");
        close(fd);
        return -1;
    }

    std::fprintf(stderr, "[motiondots-overlay] listening on 127.0.0.1:%d\n", port);
    return fd;
}

// Drain every pending datagram. Hellos are answered immediately; for dot packets
// only the last one in the queue is kept, so a backlog never causes stale frames.
void PumpSocket(int fd, DotState &state, int width, int height)
{
    // Comfortably larger than a full kMaxDots packet.
    static std::vector<uint8_t> buf(sizeof(DotsHeader) + kMaxDots * sizeof(Dot) + 64);

    for (;;)
    {
        sockaddr_in from{};
        socklen_t   fromLen = sizeof(from);
        ssize_t     n = recvfrom(fd, buf.data(), buf.size(), 0,
                                 reinterpret_cast<sockaddr *>(&from), &fromLen);
        if (n < 0)
            break; // EAGAIN: queue drained

        if (static_cast<size_t>(n) < sizeof(uint32_t))
            continue;

        uint32_t magic;
        std::memcpy(&magic, buf.data(), sizeof(magic));

        if (magic == kMagicHello)
        {
            if (static_cast<size_t>(n) < sizeof(HelloPacket))
                continue;
            HelloPacket hello;
            std::memcpy(&hello, buf.data(), sizeof(hello));
            if (hello.version != kProtocolVersion)
            {
                std::fprintf(stderr, "[motiondots-overlay] ignoring hello with version %u\n",
                             static_cast<unsigned>(hello.version));
                continue;
            }

            InfoPacket info{};
            info.magic   = kMagicInfo;
            info.version = kProtocolVersion;
            info.pad     = 0;
            info.width   = static_cast<uint32_t>(width);
            info.height  = static_cast<uint32_t>(height);
            sendto(fd, &info, sizeof(info), 0,
                   reinterpret_cast<sockaddr *>(&from), fromLen);
            continue;
        }

        if (magic != kMagicDots)
            continue;

        if (static_cast<size_t>(n) < sizeof(DotsHeader))
            continue;

        DotsHeader header;
        std::memcpy(&header, buf.data(), sizeof(header));
        if (header.version != kProtocolVersion)
            continue;
        if (header.count > kMaxDots)
            continue;
        // Reject a truncated packet rather than reading past what arrived.
        const size_t expected = sizeof(DotsHeader) + static_cast<size_t>(header.count) * sizeof(Dot);
        if (static_cast<size_t>(n) < expected)
            continue;
        if (!(header.radius > 0.0f) || !std::isfinite(header.radius))
            continue;

        state.radius = header.radius;
        state.dots.resize(header.count);
        if (header.count > 0)
            std::memcpy(state.dots.data(), buf.data() + sizeof(DotsHeader),
                        static_cast<size_t>(header.count) * sizeof(Dot));
        // Keep looping: a newer packet may still be queued behind this one.
    }
}

// A fixed ring used by --self-test, so PRD phase 2 (prove the overlay composites
// over a real game) can be verified with no sensor service and no bridge running.
void BuildSelfTestRing(DotState &state, int width, int height, int count, float radius, double t)
{
    state.radius = radius;
    state.dots.clear();
    const float margin = 10.0f;
    for (int i = 0; i < count; ++i)
    {
        // Even perimeter placement, same model the bridge uses.
        const float f = static_cast<float>(i) / static_cast<float>(count);
        const float w = static_cast<float>(width)  - 2.0f * margin;
        const float h = static_cast<float>(height) - 2.0f * margin;
        const float perim = 2.0f * (w + h);
        float d = f * perim;
        float x, y;
        if (d < w)                    { x = margin + d;             y = margin; }
        else if (d < w + h)           { x = margin + w;             y = margin + (d - w); }
        else if (d < 2.0f * w + h)    { x = margin + w - (d - w - h); y = margin + h; }
        else                          { x = margin;                 y = margin + h - (d - 2.0f * w - h); }

        Dot dot{};
        dot.x = x;
        dot.y = y;
        // Slow chase so it is obvious the overlay is live and not a frozen frame.
        dot.alpha = 0.35f + 0.5f * static_cast<float>(
            0.5 * (1.0 + std::sin(t * 2.0 + f * 6.283185307)));
        state.dots.push_back(dot);
    }
}

void PrintUsage(const char *argv0)
{
    std::printf(
        "Usage: %s [options]\n"
        "\n"
        "Renders a ring of white dots as a gamescope overlay on top of the running game.\n"
        "Dot positions are received over UDP from the Motion Dots bridge.\n"
        "\n"
        "  --port PORT    UDP port to listen on (default %d)\n"
        "  --self-test    ignore UDP and draw a built-in animated ring\n"
        "  -h, --help     this message\n",
        argv0, kDefaultOverlayPort);
}

} // namespace

int main(int argc, char *argv[])
{
    int  port     = kDefaultOverlayPort;
    bool selfTest = false;

    for (int i = 1; i < argc; ++i)
    {
        std::string arg(argv[i]);
        if (arg == "-h" || arg == "--help") { PrintUsage(argv[0]); return 0; }
        if (arg == "--self-test")           { selfTest = true; continue; }
        if (arg == "--port" && i + 1 < argc) { port = std::atoi(argv[++i]); continue; }
        std::fprintf(stderr, "[motiondots-overlay] unknown argument: %s\n", arg.c_str());
        PrintUsage(argv[0]);
        return 2;
    }

    std::signal(SIGINT,  OnSignal);
    std::signal(SIGTERM, OnSignal);

    if (!glfwInit())
    {
        std::fprintf(stderr, "[motiondots-overlay] glfwInit failed (is DISPLAY set?)\n");
        return 1;
    }

    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 0);
    glfwWindowHint(GLFW_RESIZABLE, GLFW_TRUE);
    glfwWindowHint(GLFW_TRANSPARENT_FRAMEBUFFER, GLFW_TRUE);
    glfwWindowHint(GLFW_DECORATED, GLFW_FALSE);
    glfwWindowHint(GLFW_FOCUS_ON_SHOW, GLFW_FALSE);

    GLFWwindow *window = glfwCreateWindow(1280, 800, "Motion Dots overlay", nullptr, nullptr);
    if (!window)
    {
        std::fprintf(stderr, "[motiondots-overlay] failed to create window\n");
        glfwTerminate();
        return 1;
    }

    // The one piece of OverLaid that makes any of this work: tell gamescope to
    // composite this window as an external overlay, above the game.
    Display *x11Display = glfwGetX11Display();
    Window   x11Window  = glfwGetX11Window(window);
    if (x11Display && x11Window)
    {
        Atom     overlayAtom = XInternAtom(x11Display, kGamescopeOverlayProperty, False);
        uint32_t value       = 1;
        XChangeProperty(x11Display, x11Window, overlayAtom, XA_CARDINAL, 32,
                        PropModeReplace, reinterpret_cast<unsigned char *>(&value), 1);

#ifdef MOTIONDOTS_HAVE_XFIXES
        // Empty input region: the overlay is purely decorative and must never
        // swallow a touch or a click meant for the game.
        int xfixesEventBase, xfixesErrorBase;
        if (XFixesQueryExtension(x11Display, &xfixesEventBase, &xfixesErrorBase))
        {
            XserverRegion region = XFixesCreateRegion(x11Display, nullptr, 0);
            XFixesSetWindowShapeRegion(x11Display, x11Window, ShapeInput, 0, 0, region);
            XFixesDestroyRegion(x11Display, region);
        }
#endif
        XFlush(x11Display);
    }
    else
    {
        std::fprintf(stderr,
                     "[motiondots-overlay] warning: no X11 handle, cannot set %s -- "
                     "the window will not composite over the game\n",
                     kGamescopeOverlayProperty);
    }

    glfwMakeContextCurrent(window);
    glfwSwapInterval(1); // vsync: ride the compositor's cadence, never spin

    glewExperimental = GL_TRUE;
    GLenum glewErr = glewInit();
    if (glewErr != GLEW_OK)
    {
        std::fprintf(stderr, "[motiondots-overlay] glewInit failed: %s\n",
                     glewGetErrorString(glewErr));
        glfwDestroyWindow(window);
        glfwTerminate();
        return 1;
    }
    // glewInit can leave a benign GL_INVALID_ENUM behind on core profiles.
    glGetError();

    GLuint program = BuildProgram();
    if (!program)
    {
        glfwDestroyWindow(window);
        glfwTerminate();
        return 1;
    }

    const GLint uViewport = glGetUniformLocation(program, "uViewport");
    const GLint uFeather  = glGetUniformLocation(program, "uFeather");

    GLuint vao = 0, vbo = 0;
    glGenVertexArrays(1, &vao);
    glBindVertexArray(vao);
    glGenBuffers(1, &vbo);
    glBindBuffer(GL_ARRAY_BUFFER, vbo);

    // interleaved: aPos.xy, aLocal.xy, aAlpha
    const GLsizei stride = 5 * sizeof(float);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, stride, reinterpret_cast<void *>(0));
    glEnableVertexAttribArray(1);
    glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, stride, reinterpret_cast<void *>(2 * sizeof(float)));
    glEnableVertexAttribArray(2);
    glVertexAttribPointer(2, 1, GL_FLOAT, GL_FALSE, stride, reinterpret_cast<void *>(4 * sizeof(float)));

    int sockFd = -1;
    if (!selfTest)
    {
        sockFd = OpenSocket(port);
        if (sockFd < 0)
        {
            glfwDestroyWindow(window);
            glfwTerminate();
            return 1;
        }
    }

    DotState           state;
    std::vector<float> vertices;
    const double       startTime = glfwGetTime();

    while (!glfwWindowShouldClose(window) && !g_stop)
    {
        // Track the display: gamescope's nested output can change size (docking,
        // resolution changes) and the overlay must follow it.
        int width = 0, height = 0;
        if (GLFWmonitor *monitor = glfwGetPrimaryMonitor())
        {
            int mx, my, mw, mh;
            glfwGetMonitorWorkarea(monitor, &mx, &my, &mw, &mh);
            if (mw > 0 && mh > 0)
            {
                int curW, curH;
                glfwGetWindowSize(window, &curW, &curH);
                if (curW != mw || curH != mh)
                {
                    glfwSetWindowPos(window, mx, my);
                    glfwSetWindowSize(window, mw, mh);
                }
            }
        }
        glfwGetFramebufferSize(window, &width, &height);
        if (width <= 0 || height <= 0)
        {
            glfwPollEvents();
            continue;
        }

        glfwPollEvents();

        if (selfTest)
            BuildSelfTestRing(state, width, height, 14, 4.0f, glfwGetTime() - startTime);
        else
            PumpSocket(sockFd, state, width, height);

        // Rebuild the vertex batch. 14 dots = 84 vertices; this is noise next to
        // the cost of presenting the frame at all.
        vertices.clear();
        vertices.reserve(state.dots.size() * 6 * 5);
        const float r = state.radius;
        for (const Dot &dot : state.dots)
        {
            if (!std::isfinite(dot.x) || !std::isfinite(dot.y) || !std::isfinite(dot.alpha))
                continue;
            if (dot.alpha <= 0.0f)
                continue;

            const float corners[4][2] = { {-1, -1}, {1, -1}, {1, 1}, {-1, 1} };
            const int   order[6]      = { 0, 1, 2, 0, 2, 3 };
            for (int k = 0; k < 6; ++k)
            {
                const float lx = corners[order[k]][0];
                const float ly = corners[order[k]][1];
                vertices.push_back(dot.x + lx * r);
                vertices.push_back(dot.y + ly * r);
                vertices.push_back(lx);
                vertices.push_back(ly);
                vertices.push_back(dot.alpha);
            }
        }

        glViewport(0, 0, width, height);
        // Fully transparent clear: every pixel we do not draw shows the game.
        glClearColor(0.0f, 0.0f, 0.0f, 0.0f);
        glClear(GL_COLOR_BUFFER_BIT);

        if (!vertices.empty())
        {
            glEnable(GL_BLEND);
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
            glDisable(GL_DEPTH_TEST);

            glUseProgram(program);
            glUniform2f(uViewport, static_cast<float>(width), static_cast<float>(height));
            // One pixel expressed in the quad's -1..1 local space.
            glUniform1f(uFeather, r > 0.0f ? (1.0f / r) : 0.5f);

            glBindVertexArray(vao);
            glBindBuffer(GL_ARRAY_BUFFER, vbo);
            glBufferData(GL_ARRAY_BUFFER,
                         static_cast<GLsizeiptr>(vertices.size() * sizeof(float)),
                         vertices.data(), GL_STREAM_DRAW);
            glDrawArrays(GL_TRIANGLES, 0, static_cast<GLsizei>(vertices.size() / 5));
        }

        glfwSwapBuffers(window);
    }

    if (sockFd >= 0)
        close(sockFd);
    glDeleteBuffers(1, &vbo);
    glDeleteVertexArrays(1, &vao);
    glDeleteProgram(program);
    glfwDestroyWindow(window);
    glfwTerminate();
    return 0;
}
