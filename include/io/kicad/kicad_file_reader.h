/*
 * This program source code file is part of KiCad, a free EDA CAD application.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#pragma once

#include <array>
#include <chrono>
#include <richio.h>
#include <wx/ffile.h>

class PROGRESS_REPORTER;

/** Block-buffered input for native S-expression files. Owns the stream it reads ahead on. */
class KICOMMON_API KICAD_FILE_READER final : public LINE_READER
{
public:
    explicit KICAD_FILE_READER( const wxString& aFileName, PROGRESS_REPORTER* aProgressReporter = nullptr );

    char* ReadLine() override;

private:
    bool refill();

    wxFFile                               m_file;
    std::array<char, 32 * 1024>           m_buffer;
    size_t                                m_pos = 0;
    size_t                                m_end = 0;
    PROGRESS_REPORTER*                    m_progressReporter;
    wxFileOffset                          m_fileSize = wxInvalidOffset;
    std::chrono::steady_clock::time_point m_lastRefresh;
};
