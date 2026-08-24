#ifndef MOTIONDOTS_FATAL_H
#define MOTIONDOTS_FATAL_H

// A pipeline thread that cannot continue has no way to return an error: throwing
// out of the thread body just calls std::terminate, which aborts the process with
// SIGABRT and no useful context. FatalError() instead records a reason and asks
// the process to shut down through its normal signal path, so the service exits
// cleanly with a non-zero status and a message a user can act on.

#include <string>

namespace motiondots
{
    // Exit status used for "the service could not do its job", as distinct from
    // a normal shutdown (0) or a bad command line (2).
    constexpr int kFatalExitCode = 3;

    // Records the reason and raises SIGTERM. Safe to call from any thread; only
    // the first call is recorded.
    void FatalError(std::string const& reason);

    bool HadFatalError();
    std::string FatalErrorReason();
}

#endif
