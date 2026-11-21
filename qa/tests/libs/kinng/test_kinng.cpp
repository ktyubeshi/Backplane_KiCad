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

#include <chrono>
#include <future>
#include <qa_utils/wx_utils/unit_test_utils.h>
#include <kinng.h>
#include <nng/protocol/reqrep0/req.h>

BOOST_AUTO_TEST_SUITE( KiNNG )

BOOST_AUTO_TEST_CASE( ReplyAndStopWithAnOutstandingRequest )
{
    const std::string url = "inproc://qa-kinng";
    std::promise<void> received;
    auto pending = received.get_future();
    KINNG_REQUEST_SERVER server( url );
    BOOST_REQUIRE( server.Start( [&]( std::string_view request )
    {
        if( request == "hold" )
            received.set_value();
        else
            server.Reply( std::string( request ) );
    } ) );

    struct CLIENT
    {
        nng_socket socket{};
        ~CLIENT() { nng_close( socket ); }
    } client;

    BOOST_REQUIRE_EQUAL( nng_req0_open( &client.socket ), 0 );
    BOOST_REQUIRE_EQUAL( nng_socket_set_ms( client.socket, NNG_OPT_SENDTIMEO, 2000 ), 0 );
    BOOST_REQUIRE_EQUAL( nng_socket_set_ms( client.socket, NNG_OPT_RECVTIMEO, 2000 ), 0 );
    BOOST_REQUIRE_EQUAL( nng_dial( client.socket, url.c_str(), nullptr, 0 ), 0 );

    for( std::string request : { std::string( "a\0b", 3 ), std::string() } )
    {
        BOOST_REQUIRE_EQUAL( nng_send( client.socket, request.data(), request.size(), 0 ), 0 );
        char reply[4];
        size_t size = sizeof( reply );
        BOOST_REQUIRE_EQUAL( nng_recv( client.socket, reply, &size, 0 ), 0 );
        BOOST_CHECK_EQUAL( std::string( reply, size ), request );
    }

    char hold[] = "hold";
    BOOST_REQUIRE_EQUAL( nng_send( client.socket, hold, sizeof( hold ) - 1, 0 ), 0 );
    BOOST_REQUIRE( pending.wait_for( std::chrono::seconds( 2 ) ) == std::future_status::ready );
    server.Stop();
    BOOST_CHECK( !server.Running() );
    BOOST_CHECK( server.Start( [&]( std::string_view ) { server.Reply( "" ); } ) );
}

BOOST_AUTO_TEST_SUITE_END()
