#include "fatal.h"
#include "log/log.h"

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <thread>

namespace motiondots
{
    namespace
    {
        std::atomic<bool> g_fatal{false};
        std::mutex        g_reasonMutex;
        std::string       g_reason;
    }

    void FatalError(std::string const& reason)
    {
        bool expected = false;
        if(!g_fatal.compare_exchange_strong(expected, true))
            return; // first failure wins; later ones are usually consequences

        {
            std::lock_guard lock(g_reasonMutex);
            g_reason = reason;
        }

        { kmicki::log::LogF() << "FATAL: " << reason; }

        // Prefer the graceful path: let main's signal handler run the normal
        // teardown.
        std::raise(SIGTERM);

        // ...but do not depend on it. The reader pipeline's shutdown can block
        // when a stage died before it ever produced a frame, and a sensor
        // service that hangs instead of exiting is worse than one that crashes:
        // the plugin's watchdog would never notice it had failed, and the QAM
        // panel would keep claiming the overlay was running. Give the clean
        // shutdown a moment, then leave regardless.
        std::thread([]{
            std::this_thread::sleep_for(std::chrono::seconds(2));
            std::cout.flush();
            std::cerr.flush();
            std::_Exit(kFatalExitCode);
        }).detach();
    }

    bool HadFatalError()
    {
        return g_fatal.load();
    }

    std::string FatalErrorReason()
    {
        std::lock_guard lock(g_reasonMutex);
        return g_reason;
    }
}
