/*
 * This program source code file is part of KiCad, a free EDA CAD application.
 *
 * Copyright (C) 2023 Jon Evans <jon@craftyjon.com>
 * Copyright The KiCad Developers, see AUTHORS.txt for contributors.
 *
 * This program is free software: you can redistribute it and/or modify it
 * under the terms of the GNU General Public License as published by the
 * Free Software Foundation, either version 3 of the License, or (at your
 * option) any later version.
 *
 * This program is distributed in the hope that it will be useful, but
 * WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License along
 * with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

#ifndef KICAD_KINNG_H
#define KICAD_KINNG_H

#include <atomic>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <nng/nng.h>


class KINNG_REQUEST_SERVER
{
public:
    KINNG_REQUEST_SERVER( const std::string& aSocketUrl );

    ~KINNG_REQUEST_SERVER();

    // The request bytes are valid for the duration of the callback.
    bool Start( std::function<void( std::string_view )> aCallback );

    void Stop();

    bool Running() const;

    void Reply( std::string aReply );

    /// Tests only: called inside Stop() after the shutdown flag is set and before the listener is woken (a test holds Stop there to
    /// show the listener's wait cannot miss the flag)
    void SetStopHookForTest( std::function<void()> aHook ) { m_stopHookForTest = std::move( aHook ); }

    const std::string& SocketPath() const { return m_socketUrl; }

private:
    void listenThread( std::function<void( std::string_view )> aCallback );

    std::thread m_thread;

    std::atomic<bool> m_shutdown{ false };

    std::string m_socketUrl;

    nng_socket m_socket{};
    std::optional<std::string> m_pendingReply;

    std::function<void()> m_stopHookForTest;

    std::condition_variable m_replyReady;

    std::mutex m_mutex;
};

#endif //KICAD_KINNG_H
