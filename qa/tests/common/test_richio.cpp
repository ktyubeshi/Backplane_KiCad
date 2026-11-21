/*
 * This program source code file is part of KiCad, a free EDA CAD application.
 *
 * Copyright The KiCad Developers, see AUTHORS.TXT for contributors.
 *
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of the GNU General Public License
 * as published by the Free Software Foundation; either version 2
 * of the License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

/**
 * @file
 * Test suite for RICHIO and related formatting utilities
 */

#include <qa_utils/wx_utils/unit_test_utils.h>

#include <richio.h>
#include <cli_progress_reporter.h>
#include <dsnlexer.h>
#include <io/kicad/kicad_file_reader.h>
#include <io/kicad/kicad_io_utils.h>
#include <wx/filename.h>
#include <chrono>
#include <cstring>
#include <thread>

#define wxUSE_BASE64 1
#include <wx/base64.h>
#include <wx/mstream.h>


BOOST_AUTO_TEST_SUITE( RichIO )


BOOST_AUTO_TEST_CASE( BufferedNativeFileLines )
{
    wxFFile file;
    const wxString path = wxFileName::CreateTempFileName( wxS( "kicad-lines" ), &file );
    BOOST_REQUIRE( file.IsOpened() );

    // End on a block boundary, cross two more, and preserve embedded NUL and an unterminated tail.
    std::string contents( 32767, 'a' );
    contents += "\n\n";
    contents.append( 70000, 'b' );
    contents.append( "\0c\r\nlast", 8 );
    BOOST_REQUIRE_EQUAL( file.Write( contents.data(), contents.size() ), contents.size() );
    BOOST_REQUIRE( file.Close() );

    {
        FILE_LINE_READER expected( path );
        KICAD_FILE_READER reader( path );

        while( expected.ReadLine() )
        {
            BOOST_REQUIRE( reader.ReadLine() );
            BOOST_CHECK_EQUAL( reader.LineNumber(), expected.LineNumber() );
            BOOST_REQUIRE_EQUAL( reader.Length(), expected.Length() );
            BOOST_CHECK_EQUAL( std::memcmp( reader.Line(), expected.Line(), reader.Length() + 1 ), 0 );
        }

        BOOST_CHECK( !reader.ReadLine() );
        BOOST_CHECK_EQUAL( reader.LineNumber(), expected.LineNumber() );
        BOOST_CHECK_EQUAL( reader.Length(), 0U );
    }

    BOOST_CHECK( wxRemoveFile( path ) );
}


BOOST_AUTO_TEST_CASE( BufferedNativeLineLimit )
{
    wxFFile file;
    const wxString path = wxFileName::CreateTempFileName( wxS( "kicad-line-limit" ), &file );
    BOOST_REQUIRE( file.IsOpened() );
    std::string line( LINE_READER_LINE_DEFAULT_MAX, 'x' );
    line.back() = '\n';
    BOOST_REQUIRE_EQUAL( file.Write( line.data(), line.size() ), line.size() );
    line.back() = 'x';
    BOOST_REQUIRE_EQUAL( file.Write( line.data(), line.size() ), line.size() );
    BOOST_REQUIRE( file.Close() );

    {
        KICAD_FILE_READER reader( path );
        BOOST_REQUIRE( reader.ReadLine() );
        BOOST_CHECK_EQUAL( reader.Length(), LINE_READER_LINE_DEFAULT_MAX );
        BOOST_CHECK_THROW( reader.ReadLine(), IO_ERROR );
    }

    BOOST_CHECK( wxRemoveFile( path ) );
}


BOOST_AUTO_TEST_CASE( BufferedNativeReadProgressAndCancel )
{
    struct REPORTER : CLI_PROGRESS_REPORTER
    {
        void SetCurrentProgress( double aProgress ) override { progress = aProgress; }
        bool KeepRefreshing( bool ) override { return !cancelled; }
        double progress = 0;
        bool   cancelled = false;
    } reporter;

    wxFFile file;
    const wxString path = wxFileName::CreateTempFileName( wxS( "kicad-progress" ), &file );
    BOOST_REQUIRE( file.IsOpened() );
    std::string block( 32767, 'a' );
    block += '\n';
    BOOST_REQUIRE_EQUAL( file.Write( block.data(), block.size() ), block.size() );
    BOOST_REQUIRE_EQUAL( file.Write( block.data(), block.size() ), block.size() );
    BOOST_REQUIRE( file.Close() );

    {
        KICAD_FILE_READER reader( path, &reporter );
        BOOST_REQUIRE( reader.ReadLine() );
        reporter.cancelled = true;
        std::this_thread::sleep_for( std::chrono::milliseconds( 260 ) );
        BOOST_CHECK_THROW( reader.ReadLine(), IO_ERROR );
        BOOST_CHECK_GT( reporter.progress, 0.0 );
        BOOST_CHECK_LT( reporter.progress, 1.0 );
    }

    BOOST_CHECK( wxRemoveFile( path ) );
}


BOOST_AUTO_TEST_CASE( LexerTextAndSeparators )
{
    DSNLEXER lexer( "  (name\n\t \"abc\\n\\x41\\101\\\\\\\"z\"  -1.5e+2)" );
    BOOST_CHECK_EQUAL( lexer.NextTok(), DSN_LEFT );
    BOOST_CHECK_EQUAL( lexer.CurSeparator(), "  " );
    BOOST_CHECK_EQUAL( lexer.NextTok(), DSN_SYMBOL );
    BOOST_CHECK_EQUAL( lexer.CurStr(), "name" );
    BOOST_CHECK_EQUAL( lexer.NextTok(), DSN_STRING );
    BOOST_CHECK_EQUAL( lexer.CurSeparator(), "\n\t " );
    BOOST_CHECK_EQUAL( lexer.CurStr(), "abc\nAA\\\"z" );
    BOOST_CHECK_EQUAL( lexer.NextTok(), DSN_NUMBER );
    BOOST_CHECK_EQUAL( lexer.CurStr(), "-1.5e+2" );
    BOOST_CHECK_EQUAL( lexer.NextTok(), DSN_RIGHT );
    BOOST_CHECK_EQUAL( lexer.NextTok(), DSN_EOF );
}


/**
 * Verify that Prettify produces well-formed output for large (data ...) blocks such as
 * base64-encoded images, and that STRING_LINE_READER can read the result.
 *
 * Regression test for https://gitlab.com/kicad/code/kicad/-/issues/23162
 */
BOOST_AUTO_TEST_CASE( PrettifyLargeImageData )
{
    STRING_FORMATTER fmt;

    fmt.Print( "(kicad_sch (version 20231120) (generator \"eeschema\")" );
    fmt.Print( "(image (at 0 0)" );

    const size_t imageSize = 2 * 1024 * 1024;
    std::vector<uint8_t> fakeImage( imageSize, 0x42 );

    wxMemoryOutputStream stream;
    stream.Write( fakeImage.data(), fakeImage.size() );

    KICAD_FORMAT::FormatStreamData( fmt, *stream.GetOutputStreamBuffer() );

    fmt.Print( ")" );  // close image
    fmt.Print( ")" );  // close kicad_sch

    std::string buf = fmt.GetString();

    KICAD_FORMAT::Prettify( buf );

    BOOST_CHECK_NO_THROW(
    {
        STRING_LINE_READER reader( buf, "test" );

        while( reader.ReadLine() )
        {
            // just consume
        }
    } );
}


/**
 * Verify that STRING_LINE_READER can handle prettified output containing a very long
 * quoted string (e.g. a property value that exceeds the old 1 MB limit).
 *
 * Regression test for https://gitlab.com/kicad/code/kicad/-/issues/23162
 */
BOOST_AUTO_TEST_CASE( PrettifyLongQuotedString )
{
    const size_t longLen = 1100000;
    std::string longValue( longLen, 'A' );

    STRING_FORMATTER fmt;

    fmt.Print( "(kicad_sch (version 20231120)" );
    fmt.Print( "(property \"Description\" %s (at 0 0 0))",
               fmt.Quotes( longValue ).c_str() );
    fmt.Print( ")" );

    std::string buf = fmt.GetString();

    KICAD_FORMAT::Prettify( buf );

    BOOST_CHECK_NO_THROW(
    {
        STRING_LINE_READER reader( buf, "test" );

        while( reader.ReadLine() )
        {
            // just consume
        }
    } );
}


/**
 * Indent writes two spaces per nesting level and nothing else.  Callers that want only the
 * indentation, such as DSN::WIRE::Format re-indenting a closing paren, depend on the exact width.
 */
BOOST_AUTO_TEST_CASE( IndentWritesTwoSpacesPerLevel )
{
    for( int nestLevel : { 0, 1, 2, 7 } )
    {
        STRING_FORMATTER fmt;

        const int written = fmt.Indent( nestLevel );

        BOOST_CHECK_EQUAL( fmt.GetString(), std::string( 2 * nestLevel, ' ' ) );
        BOOST_CHECK_EQUAL( written, 2 * nestLevel );
    }
}


/**
 * The nesting Print() overload prepends that same indentation to its formatted output.
 */
BOOST_AUTO_TEST_CASE( NestedPrintIndentsItsOutput )
{
    STRING_FORMATTER fmt;

    const int written = fmt.Print( 3, "(net %d)", 42 );

    BOOST_CHECK_EQUAL( fmt.GetString(), "      (net 42)" );
    BOOST_CHECK_EQUAL( written, 14 );
}


BOOST_AUTO_TEST_SUITE_END()
