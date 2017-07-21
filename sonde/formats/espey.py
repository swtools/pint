"""
    sonde.formats.espey
    ~~~~~~~~~~~~~~~~~

    This module implements the weird espey YSI format

"""


from datetime import datetime
import pkg_resources
import re
from io import StringIO
import struct
import time
import warnings

import numpy as np
import quantities as pq

from .. import sonde
from .. import quantities as sq
from ..timezones import cdt, cst


DEFAULT_ESPEY_PARAM_DEF = 'data/ysi_param.def'


class EspeyDataset(sonde.BaseSondeDataset):
    """
    Dataset object that represents the data contained in a ESPEY binary
    file. It accepts two optional parameters, `param_file` is a
    ysi_param.def definition file and `tzinfo` is a datetime.tzinfo
    object that represents the timezone of the timestamps in the
    binary file.
    """
    def __init__(self, data_file, tzinfo=None, param_file=None, format=None):
        self.file_format = sonde.autodetect(data_file) or 'espey'
        self.manufacturer = 'espey'
        self.data_file = data_file
        self.param_file = param_file
        self.default_tzinfo = tzinfo
        super(EspeyDataset, self).__init__(data_file)

    def _read_data(self):
        """
        Read the ESPEY binary data file
        """
        param_map = {'Temperature': 'water_temperature',
                     'Temp': 'water_temperature',
                     'Conductivity': 'water_electrical_conductivity',
                     'Cond': 'water_electrical_conductivity',
                     'Specific Cond': 'water_specific_conductance',
                     'SpCond': 'water_specific_conductance',
                     'Salinity': 'seawater_salinity',
                     'Sal': 'seawater_salinity',
                     'DO+': 'water_dissolved_oxygen_percent_saturation',
                     'ODOSat': 'water_dissolved_oxygen_percent_saturation',
                     'ODO%': 'water_dissolved_oxygen_percent_saturation',
                     'ODO': 'water_dissolved_oxygen_concentration',
                     'ODO Conc': 'water_dissolved_oxygen_concentration',
                     'pH': 'water_ph',
                     # 'Depth': 'water_depth_non_vented',
                     'Battery': 'instrument_battery_voltage',
                     'Press': 'water_pressure',
                     'Atmospheric Pressure at Time(i)': 'air_pressure',
                     # 'Final Depth (ft)': 'water_depth_vented',
                     'WSE Elevation': 'water_surface_elevation',
                     }

        unit_map = {'%': pq.percent,
                    'C': pq.degC,
                    'F': pq.degF,
                    'K': pq.degK,
                    'feet': sq.ftH2O,
                    'ft': sq.ftH2O,
                    'ft above NAVD 88': pq.ft,
                    'inches Hg': pq.inHg,
                    'm': sq.mH2O,
                    'meters': sq.mH2O,
                    'mg/L': sq.mgl,
                    'mS/cm': sq.mScm,
                    'pH': pq.dimensionless,
                    'psig': pq.psi,
                    'ppt': sq.psu,
                    'volts': pq.volt,
                    'V': pq.volt,
                    'uS/cm': sq.uScm,
                    }

        espey_data = ESPEYReaderTxt(self.data_file, self.default_tzinfo,
                                    self.param_file)
        self.format_parameters = {}

        # determine parameters provided and in what units
        self.parameters = dict()
        self.data = dict()
        for parameter in espey_data.parameters:
            try:
                pcode = param_map[(parameter.name).strip()]
                punit = unit_map[(parameter.unit).strip()]
                self.parameters[pcode] = sonde.master_parameter_list[pcode]
                self.data[param_map[parameter.name]] = parameter.data * punit
            except KeyError:
                warnings.warn('Un-mapped Parameter/Unit Type:\n'
                              '%s parameter name: "%s"\n'
                              '%s unit name: "%s"' %
                              (self.file_format, parameter.name,
                               self.file_format, parameter.unit),
                              Warning)

        self.dates = espey_data.dates


class ChannelRec:
    """
    Class that implements the channel record data structure used by
    the ESPEY binary file format
    """
    def __init__(self, rec, param_def):
        self.sonde_channel_num = rec[0]
        self.sensor_type = rec[1]
        self.probe_type = rec[2]
        self.zero_scale = rec[3]
        self.full_scale = rec[4]
        self.name = param_def[rec[1]][1]
        self.unit = param_def[rec[1]][2]
        self.unitcode = param_def[rec[1]][3]
        self.ndecimals = param_def[rec[1]][4]
        self.data = []


class ESPEYReaderTxt:
    """
    A reader object that opens and reads a ESPEY txt/cdf file.

    `data_file` should be either a file path string or a file-like
    object. It one optional parameters, `tzinfo` is a datetime.tzinfo
    object that represents the timezone of the timestamps in the
    text file.
    """
    def __init__(self, data_file, tzinfo=None, param_file=None):
        self.default_tzinfo = tzinfo
        self.num_params = 0
        self.parameters = []
        self.read_espey(data_file)
        if tzinfo:
            self.dates = [i.replace(tzinfo=tzinfo) for i in self.dates]

    def read_espey(self, data_file):
        """
        Open and read a ESPEY text file.
        """
        if isinstance(data_file, str):
            fid = open(data_file, 'r')
        else:
            fid = data_file

        #read header
        fid_initial_location = fid.tell()
        fid.seek(0)

        # skip initial 'espey' header line
        buf = fid.readline().strip('\r\n').lstrip('&,')
        dlm = ','

        while buf:
            if buf.split(',')[1].strip(' "').lower() == 'date' or \
                   buf.split(',')[1].strip(' "').lower() == 'datetime':
                param_fields = buf.lstrip('&,').split(',')
                param_units = fid.readline().strip('&,\r\n').split(',')

            if len(buf.split(',')[0].strip(' "').split('/')) == 3:
                line1 = buf.split(',')
                break

            buf = fid.readline().strip('\r\n')

        #clean up names & units
        fields = []
        params = []
        units = []
        for param, unit in zip(param_fields, param_units):
            fields.append(param.strip(' "'))
            units.append(unit.strip(' "'))

        datestr = line1[1]
        timestr = line1[2]
        start = 2

        fmt = '%m/%d/%Y %H:%M:%S'

        params = fields[start:]
        units = units[start:]
        fid.seek(-len(buf) - 2, 1)  # move back to above first line of data

        null_handler = lambda v: float(v) if v != '#VALUE!' and v != '' else None
        converter_dict = dict([(i, null_handler) for i in range(10, 19)])

        data = np.genfromtxt(fid, usecols=list(range(1, 18)), dtype=None,
                             names=fields, delimiter=',',
                             missing_values=['#VALUE!', ''],
                             converters=converter_dict,
                             filling_values=None, comments='%')

        fid.seek(fid_initial_location)

        self.dates = np.array(
            [datetime.strptime(d.strip('"') + ' ' + \
                               t.strip('"'), fmt)
             for d, t in zip(data['Date'], data['Time'])]
            )

        #assign param & unit names
        for param, unit in zip(params, units):
            self.num_params += 1
            self.parameters.append(Parameter(param.strip(), unit.strip()))

        for ii in range(self.num_params):
            # from nose.tools import set_trace; set_trace()
            param = re.sub('[?.:%()]', '',
                           self.parameters[ii].name).replace(' ', '_')
            self.parameters[ii].data = data[param]


class Parameter:
    """
    Class that implements the a structure to return a parameters
    name, unit and data
    """
    def __init__(self, param_name, param_unit):

        self.name = param_name
        self.unit = param_unit
        self.data = []


class ESPEYReaderBin:
    """
    A reader object that opens and reads a ESPEY binary file.

    `data_file` should be either a file path string or a file-like
    object. It accepts two optional parameters, `param_file` is a
    ysi_param.def definition file and `tzinfo` is a datetime.tzinfo
    object that represents the timezone of the timestamps in the
    binary file.
    """
    def __init__(self, data_file, tzinfo=None, param_file=None):
        self.default_tzinfo = tzinfo
        self.num_params = 0
        self.parameters = []
        self.julian_time = []
        self.read_param_def(param_file)
        self.read_espey(data_file)

        espey_epoch = datetime(year=1984, month=3, day=1,
                                      tzinfo=tzinfo)

        espey_epoch_in_seconds = time.mktime(espey_epoch.timetuple())

        for param in self.parameters:
            param.data = (np.array(param.data)).round(decimals=param.ndecimals)

        self.dates = np.array([datetime.fromtimestamp(t + espey_epoch_in_seconds,
                                                      tzinfo)
                               for t in self.julian_time])

        if tzinfo:
            self.dates = [i.replace(tzinfo=tzinfo) for i in self.dates]

        self.julian_time = np.array(self.julian_time)
        self.begin_log_time = datetime.fromtimestamp(
            self.begin_log_time + espey_epoch_in_seconds)

        self.first_sample_time = datetime.fromtimestamp(
            self.first_sample_time + espey_epoch_in_seconds)

    def read_param_def(self, param_file):
        """
        Open and read a ESPEY param definition file.
        """
        if param_file == None:
            file_string = pkg_resources.resource_string('sonde',
                                                        DEFAULT_ESPEY_PARAM_DEF)
        elif isinstance(param_file, str):
            with open(param_file, 'rb') as fid:
                file_string = fid.read()

        elif isinstance(param_file, file):
            file_string = param_file.read()

        file_string = re.sub("\n\s*\n*", "\n", file_string)
        file_string = re.sub(";.*\n*", "", file_string)
        file_string = re.sub("\t", "", file_string)
        file_string = re.sub("\"", "", file_string)
        self.espey_file_version = int(file_string.splitlines()[0].split('=')[-1])
        self.espey_num_param_in_def = int(
            file_string.splitlines()[1].split('=')[-1])
        self.espey_ecowatch_version = int(
            file_string.splitlines()[2].split('=')[-1])
        dtype = np.dtype([('espey_id', '<i8'),
                          ('name', '|S20'),
                          ('unit', '|S11'),
                          ('shortname', '|S9'),
                          ('num_dec_places', '<i8')])
        self.espey_param_def = np.genfromtxt(StringIO(file_string),
                                           delimiter=',',
                                           usecols=(0, 1, 3, 5, 7),
                                           skip_header=3, dtype=dtype)

    def read_espey(self, espey_file):
        """
        Open and read a ESPEY binary file.
        """
        if isinstance(espey_file, str):
            fid = open(espey_file, 'rb')

        else:
            fid = espey_file
            fid.seek(0)

        record_type = []
        self.num_params = 0

        record_type = fid.read(1)
        while record_type:
            if record_type == 'A':
                fmt = '<HLH16s32s6sLll36s'
                fmt_size = struct.calcsize(fmt)
                self.instr_type, self.system_sig, self.prog_ver, \
                                 self.serial_number, self.site_name, \
                                 self.pad1, self.logging_interval, \
                                 self.begin_log_time, \
                                 self.first_sample_time, self.pad2 \
                                 = struct.unpack(fmt, fid.read(fmt_size))
                self.site_name = self.site_name.strip('\x00')
                self.serial_number = self.serial_number.strip('\x00')
                self.log_file_name = self.site_name

            elif record_type == 'B':
                self.num_params = self.num_params + 1
                fmt = '<hhHff'
                fmt_size = struct.calcsize(fmt)
                self.parameters.append(
                    ChannelRec(struct.unpack(fmt, fid.read(fmt_size)),
                               self.espey_param_def))

            elif record_type == 'D':
                fmt = '<l' + str(self.num_params) + 'f'
                fmt_size = struct.calcsize(fmt)
                recs = struct.unpack(fmt, fid.read(fmt_size))
                self.julian_time.append(recs[0])
                for ii in range(self.num_params):
                    self.parameters[ii].data.append(recs[ii + 1])

            else:
                warnings.warn('Type not implemented yet: %s' % record_type,
                              Warning)
                break

            record_type = fid.read(1)

        if isinstance(espey_file, str):
            fid.close()
