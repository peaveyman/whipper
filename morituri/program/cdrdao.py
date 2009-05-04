# -*- Mode: Python; test-case-name:morituri.test.test_program_cdrdao -*-
# vi:si:et:sw=4:sts=4:ts=4

# Morituri - for those about to RIP

# Copyright (C) 2009 Thomas Vander Stichele

# This file is part of morituri.
# 
# morituri is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# morituri is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with morituri.  If not, see <http://www.gnu.org/licenses/>.


import re
import os
import subprocess
import tempfile

from morituri.common import task, log
from morituri.image import toc, table
from morituri.extern import asyncsub

states = ['START', 'TRACK', 'LEADOUT', 'DONE']

_ANALYZING_RE = re.compile(r'^Analyzing track (?P<track>\d+).*')

_TRACK_RE = re.compile(r"""
    ^(?P<track>[\d\s]{2})\s+ # Track
    (?P<mode>\w+)\s+         # Mode; AUDIO
    \d\s+                    # Flags
    \d\d:\d\d:\d\d           # Start in HH:MM:FF
    \((?P<start>.+)\)\s+     # Start in frames
    \d\d:\d\d:\d\d           # Length in HH:MM:FF
    \((?P<length>.+)\)       # Length in frames
""", re.VERBOSE)

_LEADOUT_RE = re.compile(r"""
    ^Leadout\s
    \w+\s+               # Mode
    \d\s+                # Flags
    \d\d:\d\d:\d\d       # Start in HH:MM:FF
    \((?P<start>.+)\)    # Start in frames
""", re.VERBOSE)

_POSITION_RE = re.compile(r"""
    ^(?P<hh>\d\d):       # HH
    (?P<mm>\d\d):        # MM
    (?P<ss>\d\d)         # SS
""", re.VERBOSE)


class OutputParser(object, log.Loggable):
    def __init__(self, taskk):
        self._buffer = ""     # accumulate characters
        self._lines = []      # accumulate lines
        self._errors = []     # accumulate error lines
        self._state = 'START'
        self._frames = None   # number of frames
        self._track = None    # which track are we analyzing?
        self._task = taskk

        self.toc = table.IndexTable() # the index table for the TOC

    def read(self, bytes):
        self.log('received %d bytes in state %s', len(bytes), self._state)
        self._buffer += bytes

        # find counter in LEADOUT state; only when we read full toc
        self.log('state: %s, buffer bytes: %d', self._state, len(self._buffer))
        if self._buffer and self._state == 'LEADOUT':
            # split on lines that end in \r, which reset cursor to counter start
            # this misses the first one, but that's ok:
            # length 03:40:71...\n00:01:00
            times = self._buffer.split('\r')
            # counter ends in \r, so the last one would be empty
            if not times[-1]:
                del times[-1]

            position = ""
            m = None
            while times and not m:
                position = times.pop()
                m = _POSITION_RE.search(position)

            # we need both a position reported and an Analyzing line
            # to have been parsed to report progress
            if m and self._track is not None:
                track = self.toc.tracks[self._track - 1]
                frame = (track.getIndex(1).absolute or 0) \
                    + int(m.group('hh')) * 60 * 75 \
                    + int(m.group('mm')) * 75 \
                    + int(m.group('ss'))
                self.log('at frame %d of %d', frame, self._frames)
                self._task.setProgress(float(frame) / self._frames)

        # parse buffer into lines if possible, and parse them
        if "\n" in self._buffer:
            self.log('buffer has newline, splitting')
            lines = self._buffer.split('\n')
            if lines[-1] != "\n":
                # last line didn't end yet
                self.log('last line still in progress')
                self._buffer = lines[-1]
                del lines[-1]
            else:
                self.log('last line finished, resetting buffer')
                self._buffer = ""
            for line in lines:
                self.log('Parsing %s', line)
                if line.startswith('ERROR:'):
                    self._errors.append(line)

            self._parse(lines)
            self._lines.extend(lines)


    def _parse(self, lines):
        for line in lines:
            #print 'parsing', len(line), line
            methodName = "_parse_" + self._state
            getattr(self, methodName)(line)

    def _parse_START(self, line):
        if line.startswith('Track'):
            self.debug('Found possible track line')
        if line == "Track   Mode    Flags  Start                Length":
            self.debug('Found track line, moving to TRACK state')
            self._state = 'TRACK'

    def _parse_TRACK(self, line):
        if line.startswith('---'):
            return

        m = _TRACK_RE.search(line)
        if m:
            self._tracks = int(m.group('track'))
            track = table.ITTrack(self._tracks)
            track.index(1, absolute=int(m.group('start')))
            self.toc.tracks.append(track)
            self.debug('Found track %d', self._tracks)

        m = _LEADOUT_RE.search(line)
        if m:
            self.debug('Found leadout line, moving to LEADOUT state')
            self._state = 'LEADOUT'
            self._frames = int(m.group('start'))
            self.debug('Found leadout at offset %r', self._frames)
            self.toc.leadout = self._frames
            self.info('%d tracks found', self._tracks)
            return

    def _parse_LEADOUT(self, line):
        m = _ANALYZING_RE.search(line)
        if m:
            self.debug('Found analyzing line')
            track = int(m.group('track'))
            self.description = 'Analyzing track %d...' % track
            self._track = track
            #self.setProgress(float(track - 1) / self._tracks)
            #print 'analyzing', track


# FIXME: handle errors

class CDRDAOTask(task.Task):
    """
    I am a task base class that runs CDRDAO.
    """

    description = "Reading TOC..."
    options = None

    def __init__(self):
        self._errors = []

    def start(self, runner):
        task.Task.start(self, runner)

        bufsize = 1024
        self._popen = asyncsub.Popen(["cdrdao"] + self.options,
                  bufsize=bufsize,
                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, close_fds=True)

        self.runner.schedule(1.0, self._read, runner)

    def _read(self, runner):
        ret = self._popen.recv_err()

        if ret:
            self.log("read from stderr: %s", ret)
            self.readbytes(ret)

        if self._popen.poll() is None:
            # not finished yet
            self.runner.schedule(1.0, self._read, runner)
            return

        self._done()

    def _done(self):
            self.setProgress(1.0)
            if self._popen.returncode != 0:
                if self._errors:
                    print "\n".join(self._errors)
                else:
                    print 'ERROR: exit code %r' % self._popen.returncode
            else:
                self.done()

            self.stop()
            return

    def readbytes(self, bytes):
        """
        Called when bytes have been read from stderr.
        """
        raise NotImplementedError

    def done(self):
        """
        Called when cdrdao completed successfully.
        """
        raise NotImplentedError


class ReadIndexTableTask(CDRDAOTask):
    """
    I am a task that reads all indexes of a CD.

    @ivar toc: the .toc file object
    @type toc: L{toc.TOC}
    """

    description = "Scanning indexes..."

    def __init__(self):
        CDRDAOTask.__init__(self)
        self.parser = OutputParser(self)
        self.toc = None # result
        (fd, self._toc) = tempfile.mkstemp(suffix='.morituri')
        os.close(fd)
        os.unlink(self._toc)

        self.options = ['read-toc', self._toc]

    def readbytes(self, bytes):
        self.parser.read(bytes)

    def done(self):
        # FIXME: instead of reading only a TOC, output a complete IndexTable
        # by merging the TOC info.
        self.toc = toc.TOC(self._toc)
        self.toc.parse()
        os.unlink(self._toc)

class ReadTOCTask(CDRDAOTask):
    """
    I am a task that reads the TOC of a CD, without pregaps.
    """

    description = "Reading TOC..."
    table = None

    def __init__(self):
        CDRDAOTask.__init__(self)
        self.parser = OutputParser(self)

        (fd, self._toc) = tempfile.mkstemp(suffix='.morituri')
        os.close(fd)
        os.unlink(self._toc)

        self.options = ['read-toc', '--fast-toc', self._toc]

    def readbytes(self, bytes):
        self.parser.read(bytes)

    def done(self):
        os.unlink(self._toc)
        self.table = self.parser.toc