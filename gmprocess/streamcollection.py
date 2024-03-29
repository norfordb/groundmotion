"""
Module for StreamCollection class.

This class functions as a list of StationStream objects, and enforces
various rules, such as all traces within a stream are from the same station.
"""

import re
import copy
import logging
import fnmatch

from obspy import UTCDateTime
from obspy.core.event import Origin
from obspy.geodetics import gps2dist_azimuth
import pandas as pd

from gmprocess.exception import GMProcessException
from gmprocess.metrics.station_summary import StationSummary
from gmprocess.stationtrace import REV_PROCESS_LEVELS
from gmprocess.stationstream import StationStream
from gmprocess.io.read_directory import directory_to_streams
from gmprocess.config import get_config


INDENT = 2

DEFAULT_IMTS = ['PGA', 'PGV', 'SA(0.3)', 'SA(1.0)', 'SA(3.0)']
DEFAULT_IMCS = ['GREATER_OF_TWO_HORIZONTALS', 'CHANNELS']

NETWORKS_USING_LOCATION = ['RE']


class StreamCollection(object):
    """
    A collection/list of StationStream objects.

    This is a list of StationStream objectss, where the constituent
    StationTraces are grouped such that:

        - All traces are from the same network/station.
        - Sample rates must match.
        - Units much match.

    TODO:
        - Check for and handle misaligned start times and end times.
        - Check units

    """

    def __init__(self, streams=None, drop_non_free=True,
                 handle_duplicates=True, max_dist_tolerance=None,
                 process_level_preference=None, format_preference=None):
        """
        Args:
            streams (list):
                List of StationStream objects.
            drop_non_free (bool):
                If True, drop non-free-field Streams from the collection.
            hande_duplicates (bool):
                If True, remove duplicate data from the collection.
            max_dist_tolerance (float):
                Maximum distance tolerance for determining whether two streams
                are at the same location (in meters).
            process_level_preference (list):
                A list containing 'V0', 'V1', 'V2', with the order determining
                which process level is the most preferred (most preferred goes
                first in the list).
            format_preference (list):
                A list continaing strings of the file source formats (found
                in gmprocess.io). Does not need to list all of the formats.
                Example: ['cosmos', 'dmg'] indicates that cosmos files are
                preferred over dmg files.
        """

        # Some initial checks of input streams
        if not isinstance(streams, list):
            raise TypeError(
                'streams must be a list of StationStream objects.')
        newstreams = []
        for s in streams:
            if not isinstance(s, StationStream):
                raise TypeError(
                    'streams must be a list of StationStream objects.')

            logging.debug(s.get_id())

            if drop_non_free:
                if s[0].free_field:
                    newstreams.append(s)
            else:
                newstreams.append(s)

        self.streams = newstreams
        if handle_duplicates:
            if len(self.streams):
                self.__handle_duplicates(
                    max_dist_tolerance,
                    process_level_preference,
                    format_preference)
        self.__group_by_net_sta_inst()
        self.validate()

    @property
    def n_passed(self):
        n_passed = 0
        for stream in self:
            if stream.passed:
                n_passed += 1
        return n_passed

    @property
    def n_failed(self):
        n = len(self.streams)
        return n - self.n_passed

    def validate(self):
        """Some validation checks across streams.

        """
        # If tag exists, it should be consistent across StationStreams
        all_labels = []
        for stream in self:
            if hasattr(stream, 'tag'):
                eventid, station, label = stream.tag.split('_')
                all_labels.append(label)
            else:
                all_labels.append("")
        if len(set(all_labels)) > 1:
            raise GMProcessException(
                'Only one label allowed within a StreamCollection.')

    def select_colocated(self, preference=["HN?", "BN?", "HH?", "BH?"]):
        """
        Detect colocated instruments and select the preferred instrument type.

        This uses the a list of the first two channel characters, given as
        'preference' in the 'colocated' section of the config. The algorithm
        is:

            1) Generate list of StationStreams that have the same station code.
            2) For each colocated group, loop over the list of preferred
               instrument codes, select the first one that is encountered by
               labeling all others a failed.

                * If the preferred instrument type matches more than one
                  StationStream, pick the first (hopefully this never happens).
                * If no StationStream matches any of the codes in the preferred
                  list then label all as failed.

        Args:
            preference (list):
                List of strings indicating preferred instrument types.
        """

        # Create a list of streams with matching id (combo of net and station).
        all_matches = []
        match_list = []
        for idx1, stream1 in enumerate(self):
            if idx1 in all_matches:
                continue
            matches = [idx1]
            net_sta = stream1.get_net_sta()
            for idx2, stream2 in enumerate(self):
                if idx1 != idx2 and idx1 not in all_matches:
                    if (net_sta == stream2.get_net_sta()):
                        matches.append(idx2)
            if len(matches) > 1:
                match_list.append(matches)
                all_matches.extend(matches)
            else:
                if matches[0] not in all_matches:
                    match_list.append(matches)
                    all_matches.extend(matches)

        for group in match_list:
            # Are there colocated instruments for this group?
            if len(group) > 1:
                # If so, loop over list of preferred instruments
                group_insts = [self[g].get_inst() for g in group]

                # Loop over preferred instruments
                no_match = True
                for pref in preference:
                    # Is this instrument available in the group?
                    r = re.compile(pref[0:2])
                    inst_match = list(filter(r.match, group_insts))
                    if len(inst_match):
                        no_match = False
                        # Select index; if more than one, we just take the
                        # first one because we don't know any better
                        keep = inst_match[0]

                        # Label all non-selected streams in the group as failed
                        to_fail = group_insts
                        to_fail.remove(keep)
                        for tf in to_fail:
                            for st in self.select(instrument=tf):
                                for tr in st:
                                    tr.fail(
                                        'Colocated with %s instrument.' % keep
                                    )

                        break
                if no_match:
                    # Fail all Streams in group
                    for g in group:
                        for tr in self[g]:
                            tr.fail(
                                'No instruments match entries in the '
                                'colocated instrument preference list for '
                                'this station.'
                            )

    @classmethod
    def from_directory(cls, directory):
        """
        Create a StreamCollection instance from a directory of data.

        Args:
            directory (str):
                Directory of ground motion files (streams) to be read.

        Returns:
            StreamCollection instance.
        """
        streams, missed_files, errors = directory_to_streams(directory)

        # Might eventually want to include some of the missed files and
        # error info but don't have a sensible place to put it currently.
        return cls(streams)

    @classmethod
    def from_traces(cls, traces):
        """
        Create a StreamCollection instance from a list of traces.

        Args:
            traces (list):
                List of StationTrace objects.

        Returns:
            StreamCollection instance.
        """

        streams = [StationStream([tr]) for tr in traces]
        return cls(streams)

    def to_dataframe(self, origin, imcs=None, imts=None):
        """Get a summary dataframe of streams.

        Note: The PGM columns underneath each channel will be variable
        depending on the units of the Stream being passed in (velocity
        sensors can only generate PGV) and on the imtlist passed in by
        user. Spectral acceleration columns will be formatted as SA(0.3)
        for 0.3 second spectral acceleration, for example.

        Args:
            directory (str):
                Directory of ground motion files (streams).
            origin_dict (obspy):
                Dictionary with the following keys:
                   - id
                   - magnitude
                   - time (UTCDateTime object)
                   - lon
                   - lat
                   - depth
            imcs (list):
                Strings designating desired components to create in table.
            imts (list):
                Strings designating desired PGMs to create in table.

        Returns:
            DataFrame: Pandas dataframe containing columns:
                - STATION Station code.
                - NAME Text description of station.
                - LOCATION Two character location code.
                - SOURCE Long form string containing source network.
                - NETWORK Short network code.
                - LAT Station latitude
                - LON Station longitude
                - DISTANCE Epicentral distance (km) (if epicentral
                  lat/lon provided)
                - HN1 East-west channel (or H1) (multi-index with pgm columns):
                    - PGA Peak ground acceleration (%g).
                    - PGV Peak ground velocity (cm/s).
                    - SA(0.3) Pseudo-spectral acceleration at 0.3 seconds (%g).
                    - SA(1.0) Pseudo-spectral acceleration at 1.0 seconds (%g).
                    - SA(3.0) Pseudo-spectral acceleration at 3.0 seconds (%g).
                - HN2 North-south channel (or H2) (multi-index with pgm
                  columns):
                    - PGA Peak ground acceleration (%g).
                    - PGV Peak ground velocity (cm/s).
                    - SA(0.3) Pseudo-spectral acceleration at 0.3 seconds (%g).
                    - SA(1.0) Pseudo-spectral acceleration at 1.0 seconds (%g).
                    - SA(3.0) Pseudo-spectral acceleration at 3.0 seconds (%g).
                - HNZ Vertical channel (or HZ) (multi-index with pgm columns):
                    - PGA Peak ground acceleration (%g).
                    - PGV Peak ground velocity (cm/s).
                    - SA(0.3) Pseudo-spectral acceleration at 0.3 seconds (%g).
                    - SA(1.0) Pseudo-spectral acceleration at 1.0 seconds (%g).
                    - SA(3.0) Pseudo-spectral acceleration at 3.0 seconds (%g).
                - GREATER_OF_TWO_HORIZONTALS (multi-index with pgm columns):
                    - PGA Peak ground acceleration (%g).
                    - PGV Peak ground velocity (cm/s).
                    - SA(0.3) Pseudo-spectral acceleration at 0.3 seconds (%g).
                    - SA(1.0) Pseudo-spectral acceleration at 1.0 seconds (%g).
                    - SA(3.0) Pseudo-spectral acceleration at 3.0 seconds (%g).
        """
        streams = self.streams
        # dept for an origin object should be stored in meters
        origin = Origin(resource_id=origin['id'], latitude=origin['lat'],
                        longitude=origin['lon'], time=origin['time'],
                        depth=origin['depth'] * 1000)

        if imcs is None:
            station_summary_imcs = DEFAULT_IMCS
        else:
            station_summary_imcs = imcs
        if imts is None:
            station_summary_imts = DEFAULT_IMTS
        else:
            station_summary_imts = imts

        if imcs is None:
            station_summary_imcs = DEFAULT_IMCS
        else:
            station_summary_imcs = imcs
        if imts is None:
            station_summary_imts = DEFAULT_IMTS
        else:
            station_summary_imts = imts

        subdfs = []
        for stream in streams:
            if not stream.passed:
                continue
            if len(stream) < 3:
                continue
            stream_summary = StationSummary.from_stream(
                stream, station_summary_imcs, station_summary_imts, origin)
            summary = stream_summary.summary
            subdfs += [summary]
        dataframe = pd.concat(subdfs, axis=0).reset_index(drop=True)

        return dataframe

    def __str__(self):
        """
        String summary of the StreamCollection.
        """
        summary = ''
        n = len(self.streams)
        summary += '%s StationStreams(s) in StreamCollection:\n' % n
        summary += '    %s StationStreams(s) passed checks.\n' % self.n_passed
        summary += '    %s StationStreams(s) failed checks.\n' % self.n_failed
        return summary

    def describe(self):
        """
        More verbose description of StreamCollection.
        """
        summary = ''
        summary += str(len(self.streams)) + \
            ' StationStreams(s) in StreamCollection:\n'
        for stream in self:
            summary += stream.__str__(indent=INDENT) + '\n'
        print(summary)

    def __len__(self):
        """
        Length of StreamCollection is the number of constituent StationStreams.
        """
        return len(self.streams)

    def __nonzero__(self):
        """
        Nonzero if there are no StationStreams.
        """
        return bool(len(self.traces))

    def __add__(self, other):
        """
        Add two streams together means appending to list of streams.
        """
        if not isinstance(other, StreamCollection):
            raise TypeError
        streams = self.streams + other.streams
        return self.__class__(streams)

    def __iter__(self):
        """
        Iterator for StreamCollection iterates over constituent StationStreams.
        """
        return list(self.streams).__iter__()

    def __setitem__(self, index, stream):
        """
        __setitem__ method.
        """
        self.streams.__setitem__(index, stream)

    def __getitem__(self, index):
        """
        __getitem__ method.
        """
        if isinstance(index, slice):
            return self.__class__(stream=self.streams.__getitem__(index))
        else:
            return self.streams.__getitem__(index)

    def __delitem__(self, index):
        """
        __delitem__ method.
        """
        return self.streams.__delitem__(index)

    def __getslice__(self, i, j, k=1):
        """
        Getslice method.
        """
        return self.__class__(streams=self.streams[max(0, i):max(0, j):k])

    def append(self, stream):
        """
        Append a single StationStream object.

        Args:
            stream:
                A StationStream object.
        """
        if isinstance(stream, StationStream):
            streams = self.streams + [stream]
            return self.__class__(streams)
        else:
            raise TypeError(
                'Append only uspports adding a single StationStream.')

    def pop(self, index=(-1)):
        """
        Remove and return the StationStream object specified by index from
        the StreamCollection.
        """
        return self.streams.pop(index)

    def copy(self):
        """
        Copy method.
        """
        return copy.deepcopy(self)

    def select(self, network=None, station=None, instrument=None):
        """
        Return a new StreamCollection with only those StationStreams
        that match the selection criteria.

        Based on obspy's `select` method for traces.

        Args:
            network (str):
                Network code.
            station (str):
                Station code.
            instrument (str):
                Instrument code; i.e., the first two characters of the
                channel.
        """
        sel = []
        for st in self:
            inst = st.get_inst()
            net_sta = st.get_net_sta()
            net = net_sta.split('.')[0]
            sta = net_sta.split('.')[1]
            if network is not None:
                if not fnmatch.fnmatch(net.upper(), network.upper()):
                    continue
            if station is not None:
                if not fnmatch.fnmatch(sta.upper(), station.upper()):
                    continue
            if instrument is not None:
                if not fnmatch.fnmatch(inst.upper(), instrument.upper()):
                    continue
            sel.append(st)
        return self.__class__(sel)

    def __group_by_net_sta_inst(self):

        trace_list = []
        stream_params = gather_stream_parameters(self.streams)
        for st in self.streams:
            for tr in st:
                trace_list.append(tr)

        # Create a list of traces with matching net, sta.
        all_matches = []
        match_list = []
        for idx1, trace1 in enumerate(trace_list):
            if idx1 in all_matches:
                continue
            matches = [idx1]
            network = trace1.stats['network']
            station = trace1.stats['station']
            free_field = trace1.free_field
            # For instrument, use first two characters of the channel
            inst = trace1.stats['channel'][0:2]
            for idx2, trace2 in enumerate(trace_list):
                if idx1 != idx2 and idx1 not in all_matches:
                    if (
                        network == trace2.stats['network']
                        and station == trace2.stats['station']
                        and inst == trace2.stats['channel'][0:2]
                        and free_field == trace2.free_field
                    ):
                        matches.append(idx2)
            if len(matches) > 1:
                match_list.append(matches)
                all_matches.extend(matches)
            else:
                if matches[0] not in all_matches:
                    match_list.append(matches)
                    all_matches.extend(matches)

        grouped_streams = []
        for groups in match_list:
            grouped_trace_list = []
            for i in groups:
                grouped_trace_list.append(
                    trace_list[i]
                )
            # some networks (e.g., Bureau of Reclamation, at the time of this
            # writing) use the location field to indicate different sensors at
            # (roughly) the same location. If we know this (as in the case of
            # BOR), we can use this to trim the stations into 3-channel
            # streams.
            streams = split_station(grouped_trace_list)
            streams = insert_stream_parameters(streams, stream_params)

            for st in streams:
                grouped_streams.append(st)

        self.streams = grouped_streams

    def __handle_duplicates(self, max_dist_tolerance,
                            process_level_preference, format_preference):
        """
        Removes duplicate data from the StreamCollection, based on the
        process level and format preferences.

        Args:
            max_dist_tolerance (float):
                Maximum distance tolerance for determining whether two streams
                are at the same location (in meters).
            process_level_preference (list):
                A list containing 'V0', 'V1', 'V2', with the order determining
                which process level is the most preferred (most preferred goes
                first in the list).
            format_preference (list):
                A list continaing strings of the file source formats (found
                in gmprocess.io). Does not need to list all of the formats.
                Example: ['cosmos', 'dmg'] indicates that cosmos files are
                preferred over dmg files.
        """

        # If arguments are None, check the config
        # If not in the config, use the default values at top of the file
        if max_dist_tolerance is None:
            max_dist_tolerance = get_config('duplicate')['max_dist_tolerance']

        if process_level_preference is None:
            process_level_preference = \
                get_config('duplicate')['process_level_preference']

        if format_preference is None:
            format_preference = get_config('duplicate')['format_preference']

        stream_params = gather_stream_parameters(self.streams)

        traces = []
        for st in self.streams:
            for tr in st:
                traces.append(tr)
        preferred_traces = []

        for tr_to_add in traces:
            is_duplicate = False
            for tr_pref in preferred_traces:
                if are_duplicates(tr_to_add, tr_pref, max_dist_tolerance):
                    is_duplicate = True
                    break

            if is_duplicate:
                if choose_preferred(
                 tr_to_add, tr_pref,
                 process_level_preference, format_preference) == tr_to_add:
                    preferred_traces.remove(tr_pref)
                    logging.info('Trace %s (%s) is a duplicate and '
                                 'has been removed from the StreamCollection.'
                                 % (tr_pref.id,
                                    tr_pref.stats.standard.source_file))
                    preferred_traces.append(tr_to_add)
                else:
                    logging.info('Trace %s (%s) is a duplicate and '
                                 'has been removed from the StreamCollection.'
                                 % (tr_to_add.id,
                                    tr_to_add.stats.standard.source_file))

            else:
                preferred_traces.append(tr_to_add)

        streams = [StationStream([tr]) for tr in preferred_traces]
        streams = insert_stream_parameters(streams, stream_params)
        self.streams = streams


def gather_stream_parameters(streams):
    """
    Helper function for gathering the stream parameters into a datastructure
    and sticking the stream tag into the trace stats dictionaries.

    Args:
        streams (list): list of StationStream objects.

    Returns:
        dict. Dictionary of the stream parameters.
    """
    stream_params = {}

    # Need to make sure that tag will be preserved; tag only really should
    # be created once a StreamCollection has been written to an ASDF file
    # and then read back in.
    for stream in streams:
        # we have stream-based metadata that we need to preserve
        if len(stream.parameters):
            stream_params[stream.get_id()] = stream.parameters

        # Tag is a StationStream attribute; If it does not exist, make it
        # an empty string
        if hasattr(stream, 'tag'):
            tag = stream.tag
        else:
            tag = ""
        # Since we have to deconstruct the stream groupings each time, we
        # need to stick the tag into the trace stats dictionary temporarily
        for trace in stream:
            tr = trace
            tr.stats.tag = tag

    return stream_params


def insert_stream_parameters(streams, stream_params):
    """
    Helper function for inserting the stream parameters back to the streams.

    Args:
        streams (list): list of StationStream objects.
        stream_params (dict): Dictionary of stream parameters.

    Returns:
        list of StationStream objects with stream parameters.
    """
    for st in streams:
        if len(st):
            sid = st.get_id()
            # put stream parameters back in
            if sid in stream_params:
                st.parameters = stream_params[sid].copy()

            # Put tag back as a stream attribute, assuming that the
            # tag has stayed the same through the grouping process
            if st[0].stats.tag:
                st.tag = st[0].stats.tag

    return streams


def split_station(grouped_trace_list):
    if grouped_trace_list[0].stats.network in NETWORKS_USING_LOCATION:
        streams_dict = {}
        for trace in grouped_trace_list:
            if trace.stats.location in streams_dict:
                streams_dict[trace.stats.location] += trace
            else:
                streams_dict[trace.stats.location] = \
                    StationStream(traces=[trace])
        streams = list(streams_dict.values())
    else:
        streams = [StationStream(traces=grouped_trace_list)]
    return streams


def are_duplicates(tr1, tr2, max_dist_tolerance):
    """
    Determines whether two StationTraces are duplicates by checking the
    station, channel codes, and the distance between them.

    Args:
        tr1 (StationTrace):
            1st trace.
        tr2 (StationTrace):
            2nd trace.
        max_dist_tolerance (float):
            Maximum distance tolerance for determining whether two streams
            are at the same location (in meters).

    Returns:
        bool. True if traces are duplicates, False otherwise.
    """

    # First, check if the ids match (net.sta.loc.cha)
    if tr1.id == tr2.id:
        return True
    # If not matching IDs, check the station, instrument code, and distance
    else:
        distance = gps2dist_azimuth(
            tr1.stats.coordinates.latitude, tr1.stats.coordinates.longitude,
            tr2.stats.coordinates.latitude, tr2.stats.coordinates.longitude)[0]
        if (tr1.stats.station == tr2.stats.station and
            tr1.stats.location == tr2.stats.location and
            tr1.stats.channel == tr2.stats.channel and
           distance < max_dist_tolerance):
            return True
        else:
            return False


def choose_preferred(tr1, tr2, process_level_preference, format_preference):
    """
    Determines which trace is preferred. Returns the preferred the trace.

    Args:
        tr1 (StationTrace):
            1st trace.
        tr2 (StationTrace):
            2nd trace.
        process_level_preference (list):
            A list containing 'V0', 'V1', 'V2', with the order determining
            which process level is the most preferred (most preferred goes
            first in the list).
        format_preference (list):
            A list continaing strings of the file source formats (found
            in gmprocess.io). Does not need to list all of the formats.
            Example: ['cosmos', 'dmg'] indicates that cosmos files are
            preferred over dmg files.

    Returns:
        The preferred trace (StationTrace).
    """

    tr1_pref = process_level_preference.index(
        REV_PROCESS_LEVELS[tr1.stats.standard.process_level])
    tr2_pref = process_level_preference.index(
        REV_PROCESS_LEVELS[tr2.stats.standard.process_level])

    if tr1_pref < tr2_pref:
        return tr1
    elif tr1_pref > tr2_pref:
        return tr2
    else:
        if (tr1.stats.standard.source_format in format_preference and
           tr2.stats.standard.source_format in format_preference):
            # Determine preferred format
            tr1_form_pref = format_preference.index(
                tr1.stats.standard.source_format)
            tr2_form_pref = format_preference.index(
                tr2.stats.standard.source_format)
            if tr1_form_pref < tr2_form_pref:
                return tr1
            elif tr1_form_pref > tr2_form_pref:
                return tr2
            else:
                if (tr1.stats.starttime == UTCDateTime(0) and
                   tr2.stats.starttime != UTCDateTime(0)):
                    return tr2
                elif (tr1.stats.starttime != UTCDateTime(0) and
                      tr2.stats.starttime == UTCDateTime(0)):
                    return tr1
                else:
                    if tr1.stats.npts > tr2.stats.npts:
                        return tr1
                    elif tr2.stats.npts > tr1.stats.npts:
                        return tr2
                    else:
                        if tr2.stats.sampling_rate > tr1.stats.sampling_rate:
                            return tr2
                        else:
                            return tr1
