// Motion Dots sensor service.
//
// Trimmed fork of https://github.com/itsOwen/SteamDeckMotion (itself derived from
// kmicki/SteamDeckGyroDSU, MIT). All this binary does is read the Steam Deck's
// built-in IMU over hidraw and broadcast the readings as JSON over local UDP.
//
// Relative to upstream we dropped:
//   * the ncurses live-view presenter (and the ncurses dependency)
//   * the hiddev device-discovery helper (and the libsystemd dependency)
//   * the systemd user unit -- Motion Dots spawns this binary directly so that
//     toggling the plugin off actually kills it (see PRD 7.4).
// The motion-sickness detection/haptics that motioncues-decky layered on top
// lived in its Python plugin, never in this binary, so there was nothing to
// strip here for that.

#include "hiddev/hiddevreader.h"
#include "sdgyrodsu/sdhidframe.h"
#include "sdgyrodsu/motionadapter.h"
#include "motion/jsonserver.h"
#include "log/log.h"
#include "fatal.h"

#include <condition_variable>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <mutex>
#include <string>

using namespace kmicki::sdgyrodsu;
using namespace kmicki::hiddev;
using namespace kmicki::log;
using namespace kmicki::motion;

namespace
{
    // Steam Deck built-in controller HID interface. These are properties of the
    // hardware, not tunables.
    constexpr int      cFrameLen        = 64;     // custom HID report length, bytes
    constexpr int      cScanTimeUs      = 4000;   // period between reports (250Hz)
    constexpr uint16_t cVID             = 0x28de; // Valve
    constexpr uint16_t cPID             = 0x1205; // Steam Deck controls
    constexpr int      cInterfaceNumber = 2;

    const std::string cVersion = "motion-dots-1.0";

    bool                    stop = false;
    std::mutex              stopMutex;
    std::condition_variable stopCV;

    void SignalHandler(int sig)
    {
        if(sig != SIGINT && sig != SIGTERM)
            return; // unhandled, ignore
        {
            std::lock_guard lock(stopMutex);
            stop = true;
        }
        stopCV.notify_all();
    }

    void PrintUsage(const char *argv0)
    {
        std::cout
            << "Usage: " << argv0 << " [--log-level LEVEL]\n"
            << "\n"
            << "Reads the Steam Deck IMU and broadcasts JSON motion data over UDP.\n"
            << "A client registers by sending any UDP datagram to the service port;\n"
            << "it is then streamed data at 60Hz until it stops sending keepalives.\n"
            << "\n"
            << "  --log-level LEVEL   one of: none, default, debug, trace (default: default)\n"
            << "  -h, --help          this message\n"
            << "\n"
            << "Environment:\n"
            << "  SDMOTION_SERVER_PORT   UDP port to listen on (default 27760)\n";
    }
}

int main(int argc, char *argv[])
{
    LogLevel logLevel = LogLevelDefault;

    for(int i = 1; i < argc; ++i)
    {
        std::string arg(argv[i]);
        if(arg == "-h" || arg == "--help")
        {
            PrintUsage(argv[0]);
            return 0;
        }
        if(arg == "--log-level" && i + 1 < argc)
        {
            std::string lvl(argv[++i]);
            if(lvl == "none")          logLevel = LogLevelNone;
            else if(lvl == "default")  logLevel = LogLevelDefault;
            else if(lvl == "debug")    logLevel = LogLevelDebug;
            else if(lvl == "trace")    logLevel = LogLevelTrace;
            else
            {
                std::cerr << "Unknown log level: " << lvl << std::endl;
                return 2;
            }
            continue;
        }
        std::cerr << "Unknown argument: " << arg << std::endl;
        PrintUsage(argv[0]);
        return 2;
    }

    signal(SIGINT, SignalHandler);
    signal(SIGTERM, SignalHandler);

    SetLogLevel(logLevel);

    { LogF() << "Motion Dots sensor service version: " << cVersion; }

    std::unique_ptr<HidDevReader> readerPtr;
    try
    {
        // hidraw via hidapi. Upstream also supported /dev/usb/hiddevX, but that
        // path needed libsystemd purely for device discovery and is not the path
        // the Deck actually uses.
        readerPtr.reset(new HidDevReader(cVID, cPID, cInterfaceNumber, cFrameLen, cScanTimeUs));
    }
    catch(std::exception const& e)
    {
        { LogF() << "Failed to open Steam Deck HID device: " << e.what(); }
        return 1;
    }

    HidDevReader &reader = *readerPtr;

    // Start-of-frame marker for Steam Deck HID reports.
    reader.SetStartMarker({ 0x01, 0x00, 0x09, 0x40 });

    MotionAdapter adapter(reader);
    reader.SetNoGyro(adapter.NoGyro);

    try
    {
        JsonServer server(adapter);

        Log("Sensor service running. SIGINT/SIGTERM to stop.");

        std::unique_lock lock(stopMutex);
        stopCV.wait(lock, []{ return stop; });
    }
    catch(std::exception const& e)
    {
        { LogF() << "Sensor service failed: " << e.what(); }
        return 1;
    }

    if(motiondots::HadFatalError())
    {
        { LogF() << "Sensor service exiting after fatal error: "
                 << motiondots::FatalErrorReason(); }
        return motiondots::kFatalExitCode;
    }

    Log("Sensor service exiting.");
    return 0;
}
