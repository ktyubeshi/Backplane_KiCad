/*
 * This program source code file is part of KiCad, a free EDA CAD application.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <io/kicad/kicad_file_reader.h>
#include <kiplatform/io.h>
#include <progress_reporter.h>

#include <algorithm>
#include <cstring>

KICAD_FILE_READER::KICAD_FILE_READER( const wxString& aFileName, PROGRESS_REPORTER* aProgressReporter ) :
        m_file( KIPLATFORM::IO::SeqFOpen( aFileName, wxT( "rt" ) ) ),
        m_progressReporter( aProgressReporter )
{
    if( !m_file.IsOpened() )
        THROW_IO_ERROR( wxString::Format( _( "Unable to open %s for reading." ), aFileName ) );

    m_source = aFileName;

    if( m_progressReporter )
    {
        m_fileSize = m_file.Length();
        m_lastRefresh = std::chrono::steady_clock::now();
    }
}


bool KICAD_FILE_READER::refill()
{
    if( m_progressReporter )
    {
        const auto now = std::chrono::steady_clock::now();

        if( now - m_lastRefresh >= std::chrono::milliseconds( 250 ) )
        {
            // All the preceding block has been consumed. Tell() gives the on-disk
            // offset even when the Windows CRT translates CRLF in text mode.
            if( m_fileSize > 0 )
                m_progressReporter->SetCurrentProgress( std::clamp( double( m_file.Tell() ) / m_fileSize,
                                                                    0.0, 1.0 ) );

            if( !m_progressReporter->KeepRefreshing() )
                THROW_IO_CANCELLED();

            m_lastRefresh = now;
        }
    }

    m_pos = 0;
    m_end = m_file.Read( m_buffer.data(), m_buffer.size() );

    if( m_file.Error() )
        THROW_IO_ERROR( wxString::Format( _( "Unable to read file '%s'." ), m_source ) );

    return m_end != 0;
}


char* KICAD_FILE_READER::ReadLine()
{
    m_length = 0;

    while( m_pos < m_end || refill() )
    {
        const char* start = m_buffer.data() + m_pos;
        const size_t available = m_end - m_pos;
        const char* newline = static_cast<const char*>( std::memchr( start, '\n', available ) );
        const size_t count = newline ? size_t( newline - start ) + 1 : available;
        const size_t length = m_length + count;

        if( length > m_maxLineLength || ( length == m_maxLineLength && !newline ) )
            THROW_IO_ERROR( _( "Maximum line length exceeded" ) );

        if( length + 1 > m_capacity )
            expandCapacity( std::max( m_capacity * 2, static_cast<unsigned>( length + 1 ) ) );

        std::memcpy( m_line + m_length, start, count );
        m_length = static_cast<unsigned>( length );
        m_pos += count;

        if( newline )
            break;
    }

    ++m_lineNum;
    m_line[m_length] = 0;
    return m_length ? m_line : nullptr;
}
