# stdlib imports
import json
import logging
from datetime import datetime
import getpass
import re
import inspect

# third party imports
import numpy as np
from obspy.core.trace import Trace
import prov
import prov.model
from obspy.core.utcdatetime import UTCDateTime
import pandas as pd

# local imports
from gmprocess._version import get_versions
from gmprocess.config import get_config

UNITS = {'acc': 'cm/s/s',
         'vel': 'cm/s'}
REVERSE_UNITS = {'cm/s/s': 'acc',
                 'cm/s': 'vel'}

PROCESS_LEVELS = {'V0': 'raw counts',
                  'V1': 'uncorrected physical units',
                  'V2': 'corrected physical units',
                  'V3': 'derived time series'}

REV_PROCESS_LEVELS = {'raw counts': 'V0',
                      'uncorrected physical units': 'V1',
                      'corrected physical units': 'V2',
                      'derived time series': 'V3'}


# NOTE: if requird is True then this means that the value must be
# filled in with a value that does NOT match the default.
STANDARD_KEYS = {
    'source_file': {
        'type': str,
        'required': False,
        'default': ''
    },
    'source': {
        'type': str,
        'required': True,
        'default': ''
    },
    'horizontal_orientation': {
        'type': float,
        'required': False,
        'default': np.nan
    },
    'station_name': {
        'type': str,
        'required': False,
        'default': ''
    },
    'instrument_period': {
        'type': float,
        'required': False,
        'default': np.nan
    },
    'instrument_damping': {
        'type': float,
        'required': False,
        'default': np.nan
    },
    'process_time': {
        'type': str,
        'required': False,
        'default': ''
    },
    'process_level': {
        'type': str,
        'required': True,
        'default': list(PROCESS_LEVELS.values())
    },
    'sensor_serial_number': {
        'type': str,
        'required': False,
        'default': ''
    },
    'instrument': {
        'type': str,
        'required': False,
        'default': ''
    },
    'structure_type': {
        'type': str,
        'required': False,
        'default': ''
    },
    'corner_frequency': {
        'type': float,
        'required': False,
        'default': np.nan
    },
    'units': {
        'type': str,
        'required': True,
        'default': ''
    },
    'source_format': {
        'type': str,
        'required': True,
        'default': ''
    },
    'instrument_sensitivity': {
        'type': float,
        'required': False,
        'default': np.nan,
    },
    'comments': {
        'type': str,
        'required': False,
        'default': ''
    },
}

INT_TYPES = [np.dtype('int8'),
             np.dtype('int16'),
             np.dtype('int32'),
             np.dtype('int64'),
             np.dtype('uint8'),
             np.dtype('uint16'),
             np.dtype('uint32'),
             np.dtype('uint64')]

FLOAT_TYPES = [np.dtype('float32'),
               np.dtype('float64')]

TIMEFMT = '%Y-%m-%dT%H:%M:%SZ'
TIMEFMT_MS = '%Y-%m-%dT%H:%M:%S.%fZ'

NS_PREFIX = "seis_prov"
NS_SEIS = (NS_PREFIX, "http://seisprov.org/seis_prov/0.1/#")

MAX_ID_LEN = 12

PROV_TIME_FMT = '%Y-%m-%dT%H:%M:%S.%fZ'

ACTIVITIES = {'waveform_simulation': {'code': 'ws',
                                      'label': 'Waveform Simulation'},
              'taper': {'code': 'tp', 'label': 'Taper'},
              'stack_cross_correlations': {
                  'code': 'sc', 'label': 'Stack Cross Correlations'},
              'simulate_response': {
                  'code': 'sr', 'label': 'Simulate Response'},
              'rotate': {'code': 'rt', 'label': 'Rotate'},
              'resample': {'code': 'rs', 'label': 'Resample'},
              'remove_response': {'code': 'rr', 'label': 'Remove Response'},
              'pad': {'code': 'pd', 'label': 'Pad'},
              'normalize': {'code': 'nm', 'label': 'Normalize'},
              'multiply': {'code': 'nm', 'label': 'Multiply'},
              'merge': {'code': 'mg', 'label': 'Merge'},
              'lowpass_filter': {'code': 'lp', 'label': 'Lowpass Filter'},
              'interpolate': {'code': 'ip', 'label': 'Interpolate'},
              'integrate': {'code': 'ig', 'label': 'Integrate'},
              'highpass_filter': {'code': 'hp', 'label': 'Highpass Filter'},
              'divide': {'code': 'dv', 'label': 'Divide'},
              'differentiate': {'code': 'df', 'label': 'Differentiate'},
              'detrend': {'code': 'dt', 'label': 'Detrend'},
              'decimate': {'code': 'dc', 'label': 'Decimate'},
              'cut': {'code': 'ct', 'label': 'Cut'},
              'cross_correlate': {'code': 'co', 'label': 'Cross Correlate'},
              'calculate_adjoint_source': {
                  'code': 'ca', 'label': 'Calculate Adjoint Source'},
              'bandstop_filter': {'code': 'bs', 'label': 'Bandstop Filter'},
              'bandpass_filter': {'code': 'bp', 'label': 'Bandpass Filter'}
              }


class StationTrace(Trace):
    """Subclass of Obspy Trace object which holds more metadata.

    """

    def __init__(self, data=np.array([]), header=None, inventory=None):
        """Construct StationTrace.

        Args:
            data (ndarray):
                numpy array of points.
            header (dict-like):
                Dictionary of metadata (see trace.stats docs).
            inventory (Inventory):
                Obspy Inventory object.
        """
        if inventory is None and header is None:
            raise Exception(
                'Cannot create StationTrace without header info or Inventory')
        if inventory is not None and header is not None:
            channelid = header['channel']
            (response, standard,
             coords, format_specific) = _stats_from_inventory(data, inventory,
                                                              channelid)
            header['response'] = response
            header['coordinates'] = coords
            header['standard'] = standard
            header['format_specific'] = format_specific
        super(StationTrace, self).__init__(data=data, header=header)
        self.provenance = []
        self.parameters = {}
        self.validate()

    @property
    def free_field(self):
        """Is this station a free-field station?

        Returns:
            bool: True if a free-field sensor, False if not.
        """
        stype = self.stats.standard['structure_type']
        non_free = ['building',
                    'bridge',
                    'dam',
                    'borehole',
                    'hole',
                    'crest',
                    'toe',
                    'foundation',
                    'body',
                    'roof',
                    'floor']
        for ftype in non_free:
            if re.search(ftype, stype.lower()) is not None:
                return False

        return True

    def fail(self, reason):
        """Note that a check on this StationTrace failed for a given reason.

        This method will set the parameter "failure", and store the reason
        provided, plus the name of the calling function.

        Args:
            reason (str):
                Reason given for failure.

        """
        istack = inspect.stack()
        calling_module = istack[1][3]
        self.setParameter('failure', {
            'module': calling_module,
            'reason': reason
        })
        logging.info(calling_module)
        logging.info(reason)

    def validate(self):
        """Ensure that all required metadata fields have been set.

        Raises:
            KeyError:
                - When standard dictionary is missing required fields
                - When standard values are of the wrong type
                - When required values are set to a default.
            ValueError:
                - When number of points in header does not match data length.
        """
        # here's something we thought obspy would do...
        # verify that npts matches length of data
        if self.stats.standard['process_level'] != PROCESS_LEVELS['V3']:
            if self.stats.npts != len(self.data):
                raise ValueError(
                    'Number of points in header does not match the number of '
                    'points in the data.'
                )

        # are all of the defined standard keys in the standard dictionary?
        if self.stats.standard['process_level'] == PROCESS_LEVELS['V3']:
            STANDARD_KEYS.pop('units', None)
        req_keys = set(STANDARD_KEYS.keys())
        std_keys = set(list(self.stats.standard.keys()))
        if not req_keys <= std_keys:
            missing = str(req_keys - std_keys)
            raise KeyError(
                'Missing standard values in StationTrace header: "%s"'
                % missing)
        type_errors = []
        required_errors = []
        for key in req_keys:
            keydict = STANDARD_KEYS[key]
            value = self.stats.standard[key]
            required = keydict['required']
            vtype = keydict['type']
            default = keydict['default']
            if not isinstance(value, vtype):
                type_errors.append(key)
            if required:
                if isinstance(default, list):
                    if value not in default:
                        required_errors.append(key)
                if value == default:
                    required_errors.append(key)

        type_error_msg = ''
        if len(type_errors):
            fmt = 'The following standard keys have the wrong type: "%s"'
            tpl = ','.join(type_errors)
            type_error_msg = fmt % tpl

        required_error_msg = ''
        if len(required_errors):
            fmt = 'The following standard keys are required: "%s"'
            tpl = ','.join(required_errors)
            required_error_msg = fmt % tpl

        error_msg = type_error_msg + '\n' + required_error_msg
        if len(error_msg.strip()):
            raise KeyError(error_msg)

    def getProvenanceKeys(self):
        """Get a list of all available provenance keys.

        Returns:
            list: List of available provenance keys.
        """
        if not len(self.provenance):
            return []
        pkeys = []
        for provdict in self.provenance:
            pkeys.append(provdict['prov_id'])
        return pkeys

    def getProvenance(self, prov_id):
        """Get list of seis-prov compatible attributes whose id matches prov_id.

        # activities.
        See http://seismicdata.github.io/SEIS-PROV/_generated_details.html

        Args:
            prov_id (str):
                Provenance ID (see URL above).

        Returns:
            list: Sequence of prov_attribute dictionaries (see URL above).
        """
        matching_prov = []
        if not len(self.provenance):
            return matching_prov
        for provdict in self.provenance:
            if provdict['prov_id'] == prov_id:
                matching_prov.append(provdict['prov_attributes'])
        return matching_prov

    def setProvenance(self, prov_id, prov_attributes):
        """Update a trace's provenance information.

        Args:
            trace (obspy.core.trace.Trace):
                Trace of strong motion dataself.
            prov_id (str):
                Activity prov:id (see URL above).
            prov_attributes (dict or list):
                Activity attributes for the given key.
        """
        provdict = {'prov_id': prov_id,
                    'prov_attributes': prov_attributes}
        self.provenance.append(provdict)

    def getAllProvenance(self):
        """Get internal list of processing history.

        Returns:
            list:
                Sequence of dictionaries containing fields:
                - prov_id Activity prov:id (see URL above).
                - prov_attributes Activity attributes for the given key.
        """
        return self.provenance

    def getProvenanceDocument(self):
        pr = prov.model.ProvDocument()
        pr.add_namespace(*NS_SEIS)
        pr = _get_person_agent(pr)
        pr = _get_software_agent(pr)
        pr = _get_waveform_entity(self, pr)
        sequence = 1
        for provdict in self.getAllProvenance():
            provid = provdict['prov_id']
            prov_attributes = provdict['prov_attributes']
            if provid not in ACTIVITIES:
                fmt = 'Unknown or invalid processing parameter %s'
                logging.debug(fmt % provid)
                continue
            pr = _get_activity(pr, provid, prov_attributes, sequence)
            sequence += 1
        return pr

    def setProvenanceDocument(self, provdoc):
        software = {}
        person = {}
        for record in provdoc.get_records():
            ident = record.identifier.localpart
            parts = ident.split('_')
            sptype = parts[1]
            # hashid = '_'.join(parts[2:])
            # sp, sptype, hashid = ident.split('_')
            if sptype == 'sa':
                for attr_key, attr_val in record.attributes:
                    key = attr_key.localpart
                    if isinstance(attr_val, prov.identifier.Identifier):
                        attr_val = attr_val.uri
                    software[key] = attr_val
            elif sptype == 'pp':
                for attr_key, attr_val in record.attributes:
                    key = attr_key.localpart
                    if isinstance(attr_val, prov.identifier.Identifier):
                        attr_val = attr_val.uri
                    person[key] = attr_val
            elif sptype == 'wf':  # waveform tag
                continue
            else:  # these are processing steps
                params = {}
                sptype = ''
                for attr_key, attr_val in record.attributes:
                    key = attr_key.localpart
                    if key == 'label':
                        continue
                    elif key == 'type':
                        _, sptype = attr_val.split(':')
                        continue
                    if isinstance(attr_val, datetime):
                        attr_val = UTCDateTime(attr_val)
                    params[key] = attr_val
                self.setProvenance(sptype, params)
            self.setParameter('software', software)
            self.setParameter('user', person)

    def hasParameter(self, param_id):
        """Check to see if Trace contains a given parameter.

        Args:
            param_id (str): Name of parameter to check.

        Returns:
            bool: True if parameter is set, False if not.
        """
        return param_id in self.parameters

    def setParameter(self, param_id, param_attributes):
        """Add to the StationTrace's set of arbitrary metadata.

        Args:
            param_id (str):
                Key for parameters dictionary.
            param_attributes (dict or list):
                Parameters for the given key.
        """
        self.parameters[param_id] = param_attributes

    def getParameterKeys(self):
        """Get a list of all available parameter keys.

        Returns:
            list: List of available parameter keys.
        """
        return list(self.parameters.keys())

    def getParameter(self, param_id):
        """Retrieve some arbitrary metadata.

        Args:
            param_id (str):
                Key for parameters dictionary.

        Returns:
            dict or list:
                Parameters for the given key.
        """
        if param_id not in self.parameters:
            raise KeyError(
                'Parameter %s not found in StationTrace' % param_id)
        return self.parameters[param_id]

    def getProvDataFrame(self):
        columns = ['Process Step', 'Process Attribute', 'Process Value']
        df = pd.DataFrame(columns=columns)
        values = []
        attributes = []
        steps = []
        indices = []
        index = 0
        for activity in self.getAllProvenance():
            provid = activity['prov_id']
            provstep = ACTIVITIES[provid]['label']
            prov_attrs = activity['prov_attributes']
            steps += [provstep] * len(prov_attrs)
            indices += [index] * len(prov_attrs)
            for key, value in prov_attrs.items():
                attributes.append(key)
                if isinstance(value, UTCDateTime):
                    value = value.datetime.strftime('%Y-%m-%d %H:%M:%S')
                values.append(str(value))
            index += 1

        mdict = {'Index': indices,
                 'Process Step': steps,
                 'Process Attribute': attributes,
                 'Process Value': values}
        df = pd.DataFrame(mdict)
        return df

    def getProvSeries(self):
        """Return a pandas Series containing the processing history for the trace.

        BO.NGNH31.HN2  Remove Response  input_units     counts
                                        output_units    cm/s^2
                       Taper            side            both
                                        window_type     Hann
                                        taper_width     0.05

        Returns:
            Series:
                Pandas Series (see above).

        """
        tpl = (self.stats.network, self.stats.station, self.stats.channel)
        recstr = '%s.%s.%s' % tpl
        values = []
        attributes = []
        steps = []
        for activity in self.getAllProvenance():
            provid = activity['prov_id']
            provstep = ACTIVITIES[provid]['label']
            prov_attrs = activity['prov_attributes']
            steps += [provstep] * len(prov_attrs)
            for key, value in prov_attrs.items():
                attributes.append(key)
                values.append(str(value))
        records = [recstr] * len(attributes)
        index = [records, steps, attributes]
        row = pd.Series(values, index=index)
        return row

    def __str__(self, id_length=None, indent=0):
        """
        Extends Trace __str__.
        """
        # set fixed id width

        if id_length:
            out = "%%-%ds" % (id_length)
            trace_id = out % self.id
        else:
            trace_id = "%s" % self.id
        out = ''
        # output depending on delta or sampling rate bigger than one
        if self.stats.sampling_rate < 0.1:
            if hasattr(self.stats, 'preview') and self.stats.preview:
                out = out + ' | '\
                    "%(starttime)s - %(endtime)s | " + \
                    "%(delta).1f s, %(npts)d samples [preview]"
            else:
                out = out + ' | '\
                    "%(starttime)s - %(endtime)s | " + \
                    "%(delta).1f s, %(npts)d samples"
        else:
            if hasattr(self.stats, 'preview') and self.stats.preview:
                out = out + ' | '\
                    "%(starttime)s - %(endtime)s | " + \
                    "%(sampling_rate).1f Hz, %(npts)d samples [preview]"
            else:
                out = out + ' | '\
                    "%(starttime)s - %(endtime)s | " + \
                    "%(sampling_rate).1f Hz, %(npts)d samples"
        # check for masked array
        if np.ma.count_masked(self.data):
            out += ' (masked)'
        if self.hasParameter('failure'):
            out += ' (failed)'
        else:
            out += ' (passed)'
        ind_str = ' ' * indent
        return ind_str + trace_id + out % (self.stats)


def _stats_from_inventory(data, inventory, channelid):
    if len(inventory.source):
        source = inventory.source
    station = inventory.networks[0].stations[0]
    coords = {'latitude': station.latitude,
              'longitude': station.longitude,
              'elevation': station.elevation}
    channel_names = [ch.code for ch in station.channels]
    channelidx = channel_names.index(channelid)
    channel = station.channels[channelidx]

    standard = {}

    # things we'll never get from an inventory object
    standard['corner_frequency'] = np.nan
    standard['instrument_damping'] = np.nan
    standard['instrument_period'] = np.nan
    standard['structure_type'] = ''
    standard['process_time'] = ''

    if data.dtype in INT_TYPES:
        standard['process_level'] = 'raw counts'
    else:
        standard['process_level'] = 'uncorrected physical units'

    standard['source'] = source
    standard['source_file'] = ''
    standard['instrument'] = ''
    standard['sensor_serial_number'] = ''
    if channel.sensor is not None:
        standard['instrument'] = ('%s %s %s %s'
                                  % (channel.sensor.type,
                                     channel.sensor.manufacturer,
                                     channel.sensor.model,
                                     channel.sensor.description))
        if channel.sensor.serial_number is not None:
            standard['sensor_serial_number'] = channel.sensor.serial_number
        else:
            standard['sensor_serial_number'] = ''

    if channel.azimuth is not None:
        standard['horizontal_orientation'] = channel.azimuth

    standard['source_format'] = channel.storage_format
    if standard['source_format'] is None:
        standard['source_format'] = 'fdsn'

    standard['units'] = ''
    if channelid[1] == 'N':
        standard['units'] = 'acc'
    else:
        standard['units'] = 'vel'

    if len(channel.comments):
        comments = ' '.join(
            channel.comments[i].value for i in range(len(channel.comments)))
        standard['comments'] = comments
    else:
        standard['comments'] = ''
    standard['station_name'] = ''
    if station.site.name != 'None':
        standard['station_name'] = station.site.name
    # extract the remaining standard info and format_specific info
    # from a JSON string in the station description.

    format_specific = {}
    if station.description is not None and station.description != 'None':
        jsonstr = station.description
        try:
            big_dict = json.loads(jsonstr)
            standard.update(big_dict['standard'])
            format_specific = big_dict['format_specific']
        except json.decoder.JSONDecodeError:
            format_specific['description'] = jsonstr

    standard['instrument_sensitivity'] = np.nan
    response = None
    if channel.response is not None:
        response = channel.response
        if hasattr(response, 'sensitivity'):
            standard['instrument_sensitivity'] = response.sensitivity.value

    return (response, standard, coords, format_specific)


def _get_software_agent(pr):
    '''Get the seis-prov entity for the gmprocess software.

    Args:
        pr (prov.model.ProvDocument):
            Existing ProvDocument.

    Returns:
        prov.model.ProvDocument:
            Provenance document updated with gmprocess software name/version.
    '''
    software = 'gmprocess'
    version = get_versions()['version']
    hashstr = '0000001'
    agent_id = "seis_prov:sp001_sa_%s" % hashstr
    giturl = 'https://github.com/usgs/groundmotion-processing'
    pr.agent(agent_id, other_attributes=((
        ("prov:label", software),
        ("prov:type", prov.identifier.QualifiedName(
            prov.constants.PROV, "SoftwareAgent")),
        ("seis_prov:software_name", software),
        ("seis_prov:software_version", version),
        ("seis_prov:website", prov.model.Literal(
            giturl,
            prov.constants.XSD_ANYURI)),
    )))
    return pr


def _get_person_agent(pr):
    '''Get the seis-prov entity for the user software.

    Args:
        pr (prov.model.ProvDocument):
            Existing ProvDocument.

    Returns:
        prov.model.ProvDocument:
            Provenance document updated with gmprocess software name/version.
    '''
    username = getpass.getuser()
    config = get_config()
    fullname = ''
    email = ''
    if 'user' in config:
        if 'name' in config['user']:
            fullname = config['user']['name']
        if 'email' in config['user']:
            email = config['user']['email']
    hashstr = '0000001'
    person_id = "seis_prov:sp001_pp_%s" % hashstr
    pr.agent(person_id, other_attributes=((
        ("prov:label", username),
        ("prov:type", prov.identifier.QualifiedName(
            prov.constants.PROV, "Person")),
        ("seis_prov:name", fullname),
        ("seis_prov:email", email)
    )))
    return pr


def _get_waveform_entity(trace, pr):
    '''Get the seis-prov entity for an input Trace.

    Args:
        trace (Trace):
            Input Obspy Trace object.
        pr (Prov):
            prov.model.ProvDocument

    Returns:
        prov.model.ProvDocument:
            Provenance document updated with waveform entity information.
    '''
    tpl = (trace.stats.network.lower(),
           trace.stats.station.lower(),
           trace.stats.channel.lower())
    waveform_hash = '%s_%s_%s' % tpl
    waveform_id = "seis_prov:sp001_wf_%s" % waveform_hash
    pr.entity(waveform_id, other_attributes=((
        ("prov:label", "Waveform Trace"),
        ("prov:type", "seis_prov:waveform_trace"),

    )))
    return pr


def _get_activity(pr, activity, attributes, sequence):
    '''Get the seis-prov entity for an input processing "activity".

    See
    http://seismicdata.github.io/SEIS-PROV/_generated_details.html#activities

    for details on the types of activities that are possible to capture.


    Args:
        pr (prov.model.ProvDocument):
            Existing ProvDocument.
        activity (str):
            The prov:id for the input activity.
        attributes (dict):
            The attributes associated with the activity.
        sequence (int):
            Integer used to identify the order in which the activities were
            performed.
    Returns:
        prov.model.ProvDocument:
            Provenance document updated with input activity.
    '''
    activity_dict = ACTIVITIES[activity]
    hashid = '%07i' % sequence
    code = activity_dict['code']
    label = activity_dict['label']
    activity_id = 'sp%03i_%s_%s' % (sequence, code, hashid)
    pr_attributes = [('prov:label', label),
                     ('prov:type', 'seis_prov:%s' % activity)]
    for key, value in attributes.items():
        if isinstance(value, float):
            value = prov.model.Literal(value, prov.constants.XSD_DOUBLE)
        elif isinstance(value, int):
            value = prov.model.Literal(value,
                                       prov.constants.XSD_INT)
        elif isinstance(value, UTCDateTime):
            value = prov.model.Literal(value.strftime(TIMEFMT),
                                       prov.constants.XSD_DATETIME)

        att_tuple = ('seis_prov:%s' % key, value)
        pr_attributes.append(att_tuple)
    pr.activity('seis_prov:%s' % activity_id,
                other_attributes=pr_attributes)
    return pr
