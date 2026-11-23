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

#include <kinng.h>
#include <nng/nng.h>
#include <nng/protocol/reqrep0/rep.h>
#include <wx/log.h>
#include <memory>
#include <utility>


/**
 * Trace nng server debug output
 * @ingroup trace_env_vars
 */
static const wxChar TraceNng[] = wxT( "KINNG" );


KINNG_REQUEST_SERVER::KINNG_REQUEST_SERVER( const std::string& aSocketUrl ) :
        m_socketUrl( aSocketUrl )
{
}


KINNG_REQUEST_SERVER::~KINNG_REQUEST_SERVER()
{
    Stop();
}


bool KINNG_REQUEST_SERVER::Running() const
{
    return m_thread.joinable();
}


bool KINNG_REQUEST_SERVER::Start( std::function<void( std::string_view )> aCallback )
{
    if( Running() || !aCallback )
        return false;

    int error = nng_rep0_open( &m_socket );

    if( error == 0 )
    {
        error = nng_socket_set_ms( m_socket, NNG_OPT_SENDTIMEO, 500 );

        if( error == 0 )
            error = nng_listen( m_socket, m_socketUrl.c_str(), nullptr, 0 );

        if( error )
            nng_close( m_socket );
    }

    if( error )
    {
        wxLogTrace( TraceNng, "Could not start listener: %s", nng_strerror( error ) );
        return false;
    }

    m_shutdown.store( false );
    m_pendingReply.reset();

    try
    {
        m_thread = std::thread( &KINNG_REQUEST_SERVER::listenThread, this, std::move( aCallback ) );
    }
    catch( ... )
    {
        nng_close( m_socket );
        throw;
    }

    return true;
}


void KINNG_REQUEST_SERVER::Stop()
{
    if( !m_thread.joinable() )
        return;

    // The flag under the lock, then the wake-up: the listener waiting for a reply tests m_shutdown under the same lock, so it either
    // sees it or is woken after it is set. Set after the notify (as before), a request received during the exit left the listener
    // waiting for a reply the main thread, here in join(), never gives: the process never ended (Windows, 2 in 30 closes, bp.13.3)
    {
        std::lock_guard<std::mutex> lock( m_mutex );
        m_shutdown.store( true );
    }

    if( m_stopHookForTest )
        m_stopHookForTest();

    m_replyReady.notify_one();
    nng_close( m_socket );   // Wake a blocked receive or send.
    m_thread.join();
}


void KINNG_REQUEST_SERVER::Reply( std::string aReply )
{
    std::lock_guard<std::mutex> lock( m_mutex );
    m_pendingReply = std::move( aReply );
    m_replyReady.notify_one();
}


void KINNG_REQUEST_SERVER::listenThread( std::function<void( std::string_view )> aCallback )
{
    while( !m_shutdown.load() )
    {
        nng_msg* raw = nullptr;
        int error = nng_recvmsg( m_socket, &raw, 0 );

        if( error )
        {
            if( !m_shutdown.load() )
                wxLogTrace( TraceNng, "Receive failed: %s", nng_strerror( error ) );

            break;
        }

        {
            std::unique_ptr<nng_msg, decltype( &nng_msg_free )> message( raw, nng_msg_free );
            aCallback( { static_cast<const char*>( nng_msg_body( message.get() ) ), nng_msg_len( message.get() ) } );
        }

        std::string reply;

        {
            std::unique_lock<std::mutex> lock( m_mutex );
            m_replyReady.wait( lock, [&]() { return m_shutdown.load() || m_pendingReply.has_value(); } );

            if( m_shutdown.load() )
                break;

            reply = std::move( *m_pendingReply );
            m_pendingReply.reset();
        }

        error = nng_send( m_socket, reply.data(), reply.size(), 0 );

        if( error && !m_shutdown.load() )
            wxLogTrace( TraceNng, "Send failed: %s", nng_strerror( error ) );
    }
}
