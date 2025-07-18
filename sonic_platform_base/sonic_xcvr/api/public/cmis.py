
"""
    cmis.py

    Implementation of XcvrApi that corresponds to the CMIS specification.
"""

from enum import Enum
from ...fields import consts
from ..xcvr_api import XcvrApi

import logging
from ...codes.public.cmis import CmisCodes
from ...codes.public.sff8024 import Sff8024
from ...fields import consts
from ..xcvr_api import XcvrApi
from .cmisCDB import CmisCdbApi
from .cmisVDM import CmisVdmApi
import time
import copy
from collections import defaultdict
from ...utils.cache import read_only_cached_api_return

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

VDM_FREEZE = 128
VDM_UNFREEZE = 0

DATAPATH_INIT_DURATION_MULTIPLIER = 10
DATAPATH_INIT_DURATION_OVERRIDE_THRESHOLD = 1000

class VdmSubtypeIndex(Enum):
    VDM_SUBTYPE_REAL_VALUE = 0
    VDM_SUBTYPE_HALARM_THRESHOLD = 1
    VDM_SUBTYPE_LALARM_THRESHOLD = 2
    VDM_SUBTYPE_HWARN_THRESHOLD = 3
    VDM_SUBTYPE_LWARN_THRESHOLD = 4
    VDM_SUBTYPE_HALARM_FLAG = 5
    VDM_SUBTYPE_LALARM_FLAG = 6
    VDM_SUBTYPE_HWARN_FLAG = 7
    VDM_SUBTYPE_LWARN_FLAG = 8

THRESHOLD_TYPE_STR_MAP = {
    VdmSubtypeIndex.VDM_SUBTYPE_HALARM_THRESHOLD: "halarm",
    VdmSubtypeIndex.VDM_SUBTYPE_LALARM_THRESHOLD: "lalarm",
    VdmSubtypeIndex.VDM_SUBTYPE_HWARN_THRESHOLD: "hwarn",
    VdmSubtypeIndex.VDM_SUBTYPE_LWARN_THRESHOLD: "lwarn"
}

FLAG_TYPE_STR_MAP = {
    VdmSubtypeIndex.VDM_SUBTYPE_HALARM_FLAG: "halarm",
    VdmSubtypeIndex.VDM_SUBTYPE_LALARM_FLAG: "lalarm",
    VdmSubtypeIndex.VDM_SUBTYPE_HWARN_FLAG: "hwarn",
    VdmSubtypeIndex.VDM_SUBTYPE_LWARN_FLAG: "lwarn"
}

CMIS_VDM_KEY_TO_DB_PREFIX_KEY_MAP = {
    "Laser Temperature [C]" : "laser_temperature_media",
    "eSNR Media Input [dB]" : "esnr_media_input",
    "PAM4 Level Transition Parameter Media Input [dB]" : "pam4_level_transition_media_input",
    "Pre-FEC BER Minimum Media Input" : "prefec_ber_min_media_input",
    "Pre-FEC BER Maximum Media Input" : "prefec_ber_max_media_input",
    "Pre-FEC BER Average Media Input" : "prefec_ber_avg_media_input",
    "Pre-FEC BER Current Value Media Input" : "prefec_ber_curr_media_input",
    "Errored Frames Minimum Media Input" : "errored_frames_min_media_input",
    "Errored Frames Maximum Media Input" : "errored_frames_max_media_input",
    "Errored Frames Average Media Input" : "errored_frames_avg_media_input",
    "Errored Frames Current Value Media Input" : "errored_frames_curr_media_input",
    "eSNR Host Input [dB]" : "esnr_host_input",
    "PAM4 Level Transition Parameter Host Input [dB]" : "pam4_level_transition_host_input",
    "Pre-FEC BER Minimum Host Input" : "prefec_ber_min_host_input",
    "Pre-FEC BER Maximum Host Input" : "prefec_ber_max_host_input",
    "Pre-FEC BER Average Host Input" : "prefec_ber_avg_host_input",
    "Pre-FEC BER Current Value Host Input" : "prefec_ber_curr_host_input",
    "Errored Frames Minimum Host Input" : "errored_frames_min_host_input",
    "Errored Frames Maximum Host Input" : "errored_frames_max_host_input",
    "Errored Frames Average Host Input" : "errored_frames_avg_host_input",
    "Errored Frames Current Value Host Input" : "errored_frames_curr_host_input"
}

CMIS_XCVR_INFO_DEFAULT_DICT = {
        "type": "N/A",
        "type_abbrv_name": "N/A",
        "hardware_rev": "N/A",
        "serial": "N/A",
        "manufacturer": "N/A",
        "model": "N/A",
        "connector": "N/A",
        "encoding": "N/A",
        "ext_identifier": "N/A",
        "ext_rateselect_compliance": "N/A",
        "cable_length": "N/A",
        "nominal_bit_rate": "N/A",
        "vendor_date": "N/A",
        "vendor_oui": "N/A",
        **{f"active_apsel_hostlane{i}": "N/A" for i in range(1, 9)},
        "application_advertisement": "N/A",
        "host_electrical_interface": "N/A",
        "media_interface_code": "N/A",
        "host_lane_count": "N/A",
        "media_lane_count": "N/A",
        "host_lane_assignment_option": "N/A",
        "media_lane_assignment_option": "N/A",
        "cable_type": "N/A",
        "media_interface_technology": "N/A",
        "vendor_rev": "N/A",
        "cmis_rev": "N/A",
        "specification_compliance": "N/A",
        "vdm_supported": "N/A"
        }

class CmisApi(XcvrApi):
    NUM_CHANNELS = 8
    LowPwrRequestSW = 4
    LowPwrAllowRequestHW = 6

    # Default caching enabled; control via classmethod
    cache_enabled = True

    @classmethod
    def set_cache_enabled(cls, enabled: bool):
        """
        Set the cache_enabled flag to control read_only_cached_api_return behavior.
        """
        cls.cache_enabled = bool(enabled)

    def __init__(self, xcvr_eeprom):
        super(CmisApi, self).__init__(xcvr_eeprom)
        self.vdm = CmisVdmApi(xcvr_eeprom) if not self.is_flat_memory() else None
        self.cdb = CmisCdbApi(xcvr_eeprom) if not self.is_flat_memory() else None

    def _get_vdm_key_to_db_prefix_map(self):
        return CMIS_VDM_KEY_TO_DB_PREFIX_KEY_MAP

    @staticmethod
    def _strip_str(val):
        return val.rstrip() if isinstance(val, str) else val

    def _update_vdm_dict(self, dict_to_update, new_key, vdm_raw_dict, vdm_observable_type, vdm_subtype_index, lane):
        """
        Updates the dictionary with the VDM value if the vdm_observable_type exists.
        If the key does not exist, it will update the dictionary with 'N/A'.

        Args:
            dict_to_update (dict): The dictionary to be updated.
            new_key (str): The key to be added in dict_to_update.
            vdm_raw_dict (dict): The raw VDM dictionary to be parsed.
            vdm_observable_type (str): Lookup key in the VDM dictionary.
            vdm_subtype_index (VdmSubtypeIndex): The index of the VDM subtype in the VDM page.
            lane (int): The lane number to be looked up in the VDM dictionary.

        Returns:
            bool: True if the key exists in the VDM dictionary, False if not.
        """
        try:
            dict_to_update[new_key] = vdm_raw_dict[vdm_observable_type][lane][vdm_subtype_index.value]
        except (KeyError, TypeError):
            dict_to_update[new_key] = 'N/A'
            logger.debug('key {} not present in VDM'.format(new_key))
            return False

        return True

    def freeze_vdm_stats(self):
        '''
        This function freeze all the vdm statistics reporting registers.
        When raised by the host, causes the module to freeze and hold all 
        reported statistics reporting registers (minimum, maximum and 
        average values)in Pages 24h-27h.

        Returns True if the provision succeeds and False incase of failure.
        '''
        return self.xcvr_eeprom.write(consts.VDM_CONTROL, VDM_FREEZE)

    def get_vdm_freeze_status(self):
        '''
        This function reads and returns the vdm Freeze done status.

        Returns True if the vdm stats freeze is successful and False if not freeze.
        '''
        return self.xcvr_eeprom.read(consts.VDM_FREEZE_DONE)

    def unfreeze_vdm_stats(self):
        '''
        This function unfreeze all the vdm statistics reporting registers.
        When freeze is ceased by the host, releases the freeze request, allowing the 
        reported minimum, maximum and average values to update again.
        
        Returns True if the provision succeeds and False incase of failure.
        '''
        return self.xcvr_eeprom.write(consts.VDM_CONTROL, VDM_UNFREEZE)

    def get_vdm_unfreeze_status(self):
        '''
        This function reads and returns the vdm unfreeze status.

        Returns True if the vdm stats unfreeze is successful and False if not unfreeze.
        '''
        return self.xcvr_eeprom.read(consts.VDM_UNFREEZE_DONE)

    @read_only_cached_api_return
    def get_manufacturer(self):
        '''
        This function returns the manufacturer of the module
        '''
        return self._strip_str(self.xcvr_eeprom.read(consts.VENDOR_NAME_FIELD))

    @read_only_cached_api_return
    def get_model(self):
        '''
        This function returns the part number of the module
        '''
        return self._strip_str(self.xcvr_eeprom.read(consts.VENDOR_PART_NO_FIELD))

    def get_cable_length_type(self):
        '''
        This function returns the cable type of the module
        '''
        return "Length Cable Assembly(m)"

    @read_only_cached_api_return
    def get_cable_length(self):
        '''
        This function returns the cable length of the module
        '''
        return self.xcvr_eeprom.read(consts.LENGTH_ASSEMBLY_FIELD)

    @read_only_cached_api_return
    def get_vendor_rev(self):
        '''
        This function returns the revision level for part number provided by vendor
        '''
        return self._strip_str(self.xcvr_eeprom.read(consts.VENDOR_REV_FIELD))

    @read_only_cached_api_return
    def get_serial(self):
        '''
        This function returns the serial number of the module
        '''
        return self._strip_str(self.xcvr_eeprom.read(consts.VENDOR_SERIAL_NO_FIELD))

    @read_only_cached_api_return
    def get_module_type(self):
        '''
        This function returns the SFF8024Identifier (module type / form-factor). Table 4-1 in SFF-8024 Rev4.6
        '''
        return self.xcvr_eeprom.read(consts.ID_FIELD)

    @read_only_cached_api_return
    def get_module_type_abbreviation(self):
        '''
        This function returns the SFF8024Identifier (module type / form-factor). Table 4-1 in SFF-8024 Rev4.6
        '''
        return self.xcvr_eeprom.read(consts.ID_ABBRV_FIELD)

    @read_only_cached_api_return
    def get_connector_type(self):
        '''
        This function returns module connector. Table 4-3 in SFF-8024 Rev4.6
        '''
        return self.xcvr_eeprom.read(consts.CONNECTOR_FIELD)

    @read_only_cached_api_return
    def get_module_hardware_revision(self):
        '''
        This function returns the module hardware revision
        '''
        if self.is_flat_memory():
            return '0.0'
        hw_major_rev = self.xcvr_eeprom.read(consts.HW_MAJOR_REV)
        hw_minor_rev = self.xcvr_eeprom.read(consts.HW_MINOR_REV)
        hw_rev = [str(num) for num in [hw_major_rev, hw_minor_rev]]
        return '.'.join(hw_rev)

    @read_only_cached_api_return
    def get_cmis_rev(self):
        '''
        This function returns the CMIS version the module complies to
        '''
        cmis_major = self.xcvr_eeprom.read(consts.CMIS_MAJOR_REVISION)
        cmis_minor = self.xcvr_eeprom.read(consts.CMIS_MINOR_REVISION)
        cmis_rev = [str(num) for num in [cmis_major, cmis_minor]]
        return '.'.join(cmis_rev)

    # Transceiver status
    def get_module_state(self):
        '''
        This function returns the module state
        '''
        return self.xcvr_eeprom.read(consts.MODULE_STATE)

    def get_module_fault_cause(self):
        '''
        This function returns the module fault cause
        '''
        return self.xcvr_eeprom.read(consts.MODULE_FAULT_CAUSE)

    def get_module_active_firmware(self):
        '''
        This function returns the active firmware version
        '''
        active_fw_major = self.xcvr_eeprom.read(consts.ACTIVE_FW_MAJOR_REV)
        active_fw_minor = self.xcvr_eeprom.read(consts.ACTIVE_FW_MINOR_REV)
        active_fw = [str(num) for num in [active_fw_major, active_fw_minor]]
        return '.'.join(active_fw)

    def get_module_inactive_firmware(self):
        '''
        This function returns the inactive firmware version
        '''
        if self.is_flat_memory():
            return 'N/A'
        inactive_fw_major = self.xcvr_eeprom.read(consts.INACTIVE_FW_MAJOR_REV)
        inactive_fw_minor = self.xcvr_eeprom.read(consts.INACTIVE_FW_MINOR_REV)
        inactive_fw = [str(num) for num in [inactive_fw_major, inactive_fw_minor]]
        return '.'.join(inactive_fw)

    def _get_xcvr_info_default_dict(self):
        return CMIS_XCVR_INFO_DEFAULT_DICT

    def get_transceiver_info(self):
        admin_info = self.xcvr_eeprom.read(consts.ADMIN_INFO_FIELD)
        if admin_info is None:
            return None

        ext_id = admin_info[consts.EXT_ID_FIELD]
        power_class = ext_id[consts.POWER_CLASS_FIELD]
        max_power = ext_id[consts.MAX_POWER_FIELD]
        xcvr_info = copy.deepcopy(self._get_xcvr_info_default_dict())
        xcvr_info.update({
            "type": admin_info[consts.ID_FIELD],
            "type_abbrv_name": admin_info[consts.ID_ABBRV_FIELD],
            "hardware_rev": self.get_module_hardware_revision(),
            "serial": self._strip_str(admin_info[consts.VENDOR_SERIAL_NO_FIELD]),
            "manufacturer": self._strip_str(admin_info[consts.VENDOR_NAME_FIELD]),
            "model": self._strip_str(admin_info[consts.VENDOR_PART_NO_FIELD]),
            "connector": admin_info[consts.CONNECTOR_FIELD],
            "ext_identifier": "%s (%sW Max)" % (power_class, max_power),
            "cable_length": float(admin_info[consts.LENGTH_ASSEMBLY_FIELD]),
            "vendor_date": self._strip_str(admin_info[consts.VENDOR_DATE_FIELD]),
            "vendor_oui": admin_info[consts.VENDOR_OUI_FIELD],
            "application_advertisement": str(self.get_application_advertisement()) if len(self.get_application_advertisement()) > 0 else 'N/A',
            "host_electrical_interface": self.get_host_electrical_interface(),
            "media_interface_code": self.get_module_media_interface(),
            "host_lane_count": self.get_host_lane_count(),
            "media_lane_count": self.get_media_lane_count(),
            "host_lane_assignment_option": self.get_host_lane_assignment_option(),
            "media_lane_assignment_option": self.get_media_lane_assignment_option(),
            "cable_type": self.get_cable_length_type(),
            "media_interface_technology": self.get_media_interface_technology(),
            "vendor_rev": self._strip_str(self.get_vendor_rev()),
            "cmis_rev": self.get_cmis_rev(),
            "specification_compliance": self.get_module_media_type(),
            "vdm_supported": self.is_transceiver_vdm_supported()
        })
        apsel_dict = self.get_active_apsel_hostlane()
        for lane in range(1, self.NUM_CHANNELS + 1):
            xcvr_info["%s%d" % ("active_apsel_hostlane", lane)] = \
            apsel_dict["%s%d" % (consts.ACTIVE_APSEL_HOSTLANE, lane)]

        # In normal case will get a valid value for each of the fields. If get a 'None' value
        # means there was a failure while reading the EEPROM, either because the EEPROM was
        # not ready yet or experiencing some other issues. It shouldn't return a dict with a
        # wrong field value, instead should return a 'None' to indicate to XCVRD that retry is
        # needed.
        if None in xcvr_info.values():
            return None
        else:
            return xcvr_info

    def get_transceiver_info_firmware_versions(self):
        return_dict = {"active_firmware" : "N/A", "inactive_firmware" : "N/A"}
        result = self.get_module_fw_info()
        if result is None:
            return return_dict
        try:
            ( _, _, _, _, _, _, _, _, ActiveFirmware, InactiveFirmware) = result['result']
        except (ValueError, TypeError):
            return return_dict
        
        return_dict["active_firmware"] = ActiveFirmware
        return_dict["inactive_firmware"] = InactiveFirmware
        return return_dict

    def get_transceiver_dom_real_value(self):
        """
        Retrieves DOM sensor values for this transceiver

        The returned dictionary contains floating-point values corresponding to various
        DOM sensor readings, as defined in the TRANSCEIVER_DOM_SENSOR table in STATE_DB.

        Returns:
            Dictionary
        """
        temp = self.get_module_temperature()
        voltage = self.get_voltage()
        tx_bias = self.get_tx_bias()
        rx_power = self.get_rx_power()
        tx_power = self.get_tx_power()
        read_failed = temp is None or \
                      voltage is None or \
                      tx_bias is None or \
                      rx_power is None or \
                      tx_power is None
        if read_failed:
            return None

        bulk_status = {
            "temperature": temp,
            "voltage": voltage
        }

        for i in range(1, self.NUM_CHANNELS + 1):
            bulk_status["tx%dbias" % i] = tx_bias[i - 1]
            bulk_status["rx%dpower" % i] = float("{:.3f}".format(self.mw_to_dbm(rx_power[i - 1]))) if rx_power[i - 1] != 'N/A' else 'N/A'
            bulk_status["tx%dpower" % i] = float("{:.3f}".format(self.mw_to_dbm(tx_power[i - 1]))) if tx_power[i - 1] != 'N/A' else 'N/A'

        laser_temp_dict = self.get_laser_temperature()
        try:
            bulk_status['laser_temperature'] = laser_temp_dict['monitor value']
        except (KeyError, TypeError):
            pass

        return bulk_status

    def get_transceiver_dom_flags(self):
        """
        Retrieves the DOM flags for this xcvr

        The returned dictionary contains boolean values representing various DOM flags.
        All registers accessed by this function are latched and correspond to the
        TRANSCEIVER_DOM_FLAG table in the STATE_DB.

        Returns:
            Dictionary
        """
        dom_flag_dict = dict()
        module_flag = self.get_module_level_flag()

        try:
            case_temp_flags = module_flag['case_temp_flags']
            voltage_flags = module_flag['voltage_flags']
            dom_flag_dict.update({
                'tempHAlarm': case_temp_flags['case_temp_high_alarm_flag'],
                'tempLAlarm': case_temp_flags['case_temp_low_alarm_flag'],
                'tempHWarn': case_temp_flags['case_temp_high_warn_flag'],
                'tempLWarn': case_temp_flags['case_temp_low_warn_flag'],
                'vccHAlarm': voltage_flags['voltage_high_alarm_flag'],
                'vccLAlarm': voltage_flags['voltage_low_alarm_flag'],
                'vccHWarn': voltage_flags['voltage_high_warn_flag'],
                'vccLWarn': voltage_flags['voltage_low_warn_flag']
            })
        except TypeError:
            pass
        try:
            _, aux2_mon_type, aux3_mon_type = self.get_aux_mon_type()
            if aux2_mon_type == 0:
                dom_flag_dict['lasertempHAlarm'] = module_flag['aux2_flags']['aux2_high_alarm_flag']
                dom_flag_dict['lasertempLAlarm'] = module_flag['aux2_flags']['aux2_low_alarm_flag']
                dom_flag_dict['lasertempHWarn'] = module_flag['aux2_flags']['aux2_high_warn_flag']
                dom_flag_dict['lasertempLWarn'] = module_flag['aux2_flags']['aux2_low_warn_flag']
            elif aux2_mon_type == 1 and aux3_mon_type == 0:
                dom_flag_dict['lasertempHAlarm'] = module_flag['aux3_flags']['aux3_high_alarm_flag']
                dom_flag_dict['lasertempLAlarm'] = module_flag['aux3_flags']['aux3_low_alarm_flag']
                dom_flag_dict['lasertempHWarn'] = module_flag['aux3_flags']['aux3_high_warn_flag']
                dom_flag_dict['lasertempLWarn'] = module_flag['aux3_flags']['aux3_low_warn_flag']
        except TypeError:
            pass

        if not self.is_flat_memory():
            tx_power_flag_dict = self.get_tx_power_flag()
            if tx_power_flag_dict:
                for lane in range(1, self.NUM_CHANNELS+1):
                    dom_flag_dict['tx%dpowerHAlarm' % lane] = tx_power_flag_dict['tx_power_high_alarm']['TxPowerHighAlarmFlag%d' % lane]
                    dom_flag_dict['tx%dpowerLAlarm' % lane] = tx_power_flag_dict['tx_power_low_alarm']['TxPowerLowAlarmFlag%d' % lane]
                    dom_flag_dict['tx%dpowerHWarn' % lane] = tx_power_flag_dict['tx_power_high_warn']['TxPowerHighWarnFlag%d' % lane]
                    dom_flag_dict['tx%dpowerLWarn' % lane] = tx_power_flag_dict['tx_power_low_warn']['TxPowerLowWarnFlag%d' % lane]
            rx_power_flag_dict = self.get_rx_power_flag()
            if rx_power_flag_dict:
                for lane in range(1, self.NUM_CHANNELS+1):
                    dom_flag_dict['rx%dpowerHAlarm' % lane] = rx_power_flag_dict['rx_power_high_alarm']['RxPowerHighAlarmFlag%d' % lane]
                    dom_flag_dict['rx%dpowerLAlarm' % lane] = rx_power_flag_dict['rx_power_low_alarm']['RxPowerLowAlarmFlag%d' % lane]
                    dom_flag_dict['rx%dpowerHWarn' % lane] = rx_power_flag_dict['rx_power_high_warn']['RxPowerHighWarnFlag%d' % lane]
                    dom_flag_dict['rx%dpowerLWarn' % lane] = rx_power_flag_dict['rx_power_low_warn']['RxPowerLowWarnFlag%d' % lane]
            tx_bias_flag_dict = self.get_tx_bias_flag()
            if tx_bias_flag_dict:
                for lane in range(1, self.NUM_CHANNELS+1):
                    dom_flag_dict['tx%dbiasHAlarm' % lane] = tx_bias_flag_dict['tx_bias_high_alarm']['TxBiasHighAlarmFlag%d' % lane]
                    dom_flag_dict['tx%dbiasLAlarm' % lane] = tx_bias_flag_dict['tx_bias_low_alarm']['TxBiasLowAlarmFlag%d' % lane]
                    dom_flag_dict['tx%dbiasHWarn' % lane] = tx_bias_flag_dict['tx_bias_high_warn']['TxBiasHighWarnFlag%d' % lane]
                    dom_flag_dict['tx%dbiasLWarn' % lane] = tx_bias_flag_dict['tx_bias_low_warn']['TxBiasLowWarnFlag%d' % lane]

        return dom_flag_dict

    def get_transceiver_threshold_info(self):
        """
        Retrieves threshold info for this xcvr

        The returned dictionary contains floating-point values corresponding to various
        DOM sensor threshold readings, as defined in the TRANSCEIVER_DOM_THRESHOLD table in STATE_DB.

        Returns:
            Dictionary
        """
        threshold_info_keys = ['temphighalarm',    'temphighwarning',
                               'templowalarm',     'templowwarning',
                               'vcchighalarm',     'vcchighwarning',
                               'vcclowalarm',      'vcclowwarning',
                               'rxpowerhighalarm', 'rxpowerhighwarning',
                               'rxpowerlowalarm',  'rxpowerlowwarning',
                               'txpowerhighalarm', 'txpowerhighwarning',
                               'txpowerlowalarm',  'txpowerlowwarning',
                               'txbiashighalarm',  'txbiashighwarning',
                               'txbiaslowalarm',   'txbiaslowwarning'
                              ]
        threshold_info_dict = dict.fromkeys(threshold_info_keys, 'N/A')

        thresh_support = self.get_transceiver_thresholds_support()
        if thresh_support is None:
            return None
        if not thresh_support:
            return threshold_info_dict
        thresh = self.xcvr_eeprom.read(consts.THRESHOLDS_FIELD)
        if thresh is None:
            return None
        tx_bias_scale_raw = self.xcvr_eeprom.read(consts.TX_BIAS_SCALE)
        if tx_bias_scale_raw is not None:
            tx_bias_scale = 2**tx_bias_scale_raw if tx_bias_scale_raw < 3 else 1
        else:
            tx_bias_scale = None
        threshold_info_dict =  {
            "temphighalarm": float("{:.3f}".format(thresh[consts.TEMP_HIGH_ALARM_FIELD])),
            "templowalarm": float("{:.3f}".format(thresh[consts.TEMP_LOW_ALARM_FIELD])),
            "temphighwarning": float("{:.3f}".format(thresh[consts.TEMP_HIGH_WARNING_FIELD])),
            "templowwarning": float("{:.3f}".format(thresh[consts.TEMP_LOW_WARNING_FIELD])),
            "vcchighalarm": float("{:.3f}".format(thresh[consts.VOLTAGE_HIGH_ALARM_FIELD])),
            "vcclowalarm": float("{:.3f}".format(thresh[consts.VOLTAGE_LOW_ALARM_FIELD])),
            "vcchighwarning": float("{:.3f}".format(thresh[consts.VOLTAGE_HIGH_WARNING_FIELD])),
            "vcclowwarning": float("{:.3f}".format(thresh[consts.VOLTAGE_LOW_WARNING_FIELD])),
            "rxpowerhighalarm": float("{:.3f}".format(self.mw_to_dbm(thresh[consts.RX_POWER_HIGH_ALARM_FIELD]))),
            "rxpowerlowalarm": float("{:.3f}".format(self.mw_to_dbm(thresh[consts.RX_POWER_LOW_ALARM_FIELD]))),
            "rxpowerhighwarning": float("{:.3f}".format(self.mw_to_dbm(thresh[consts.RX_POWER_HIGH_WARNING_FIELD]))),
            "rxpowerlowwarning": float("{:.3f}".format(self.mw_to_dbm(thresh[consts.RX_POWER_LOW_WARNING_FIELD]))),
            "txpowerhighalarm": float("{:.3f}".format(self.mw_to_dbm(thresh[consts.TX_POWER_HIGH_ALARM_FIELD]))),
            "txpowerlowalarm": float("{:.3f}".format(self.mw_to_dbm(thresh[consts.TX_POWER_LOW_ALARM_FIELD]))),
            "txpowerhighwarning": float("{:.3f}".format(self.mw_to_dbm(thresh[consts.TX_POWER_HIGH_WARNING_FIELD]))),
            "txpowerlowwarning": float("{:.3f}".format(self.mw_to_dbm(thresh[consts.TX_POWER_LOW_WARNING_FIELD]))),
            "txbiashighalarm": float("{:.3f}".format(thresh[consts.TX_BIAS_HIGH_ALARM_FIELD]*tx_bias_scale))
            if tx_bias_scale is not None else 'N/A',
            "txbiaslowalarm": float("{:.3f}".format(thresh[consts.TX_BIAS_LOW_ALARM_FIELD]*tx_bias_scale))
            if tx_bias_scale is not None else 'N/A',
            "txbiashighwarning": float("{:.3f}".format(thresh[consts.TX_BIAS_HIGH_WARNING_FIELD]*tx_bias_scale))
            if tx_bias_scale is not None else 'N/A',
            "txbiaslowwarning": float("{:.3f}".format(thresh[consts.TX_BIAS_LOW_WARNING_FIELD]*tx_bias_scale))
            if tx_bias_scale is not None else 'N/A'
        }
        laser_temp_dict = self.get_laser_temperature()
        try:
            threshold_info_dict['lasertemphighalarm'] = laser_temp_dict['high alarm']
            threshold_info_dict['lasertemplowalarm'] = laser_temp_dict['low alarm']
            threshold_info_dict['lasertemphighwarning'] = laser_temp_dict['high warn']
            threshold_info_dict['lasertemplowwarning'] = laser_temp_dict['low warn']
        except (KeyError, TypeError):
            pass

        return threshold_info_dict

    def get_module_temperature(self):
        '''
        This function returns the module case temperature and its thresholds. Unit in deg C
        '''
        if not self.get_temperature_support():
            return 'N/A'
        temp = self.xcvr_eeprom.read(consts.TEMPERATURE_FIELD)
        if temp is None:
            return None
        return float("{:.3f}".format(temp))

    def get_voltage(self):
        '''
        This function returns the monitored value of the 3.3-V supply voltage and its thresholds.
        Unit in V
        '''
        if not self.get_voltage_support():
            return 'N/A'
        voltage = self.xcvr_eeprom.read(consts.VOLTAGE_FIELD)
        if voltage is None:
            return None
        return float("{:.3f}".format(voltage))

    def is_copper(self):
        '''
        Returns True if the module is copper, False otherwise
        '''
        media_intf = self.get_module_media_type()
        return media_intf == "passive_copper_media_interface" if media_intf else None

    @read_only_cached_api_return
    def is_flat_memory(self):
        return self.xcvr_eeprom.read(consts.FLAT_MEM_FIELD) is not False

    def get_temperature_support(self):
        return not self.is_flat_memory()

    def get_voltage_support(self):
        return not self.is_flat_memory()

    def get_rx_los_support(self):
        return not self.is_flat_memory() and self.xcvr_eeprom.read(consts.RX_LOS_SUPPORT)

    def get_tx_cdr_lol_support(self):
        return not self.is_flat_memory() and self.xcvr_eeprom.read(consts.TX_CDR_LOL_SUPPORT_FIELD)

    def get_tx_cdr_lol(self):
        '''
        This function returns TX CDR LOL flag on TX host lane
        '''
        tx_cdr_lol_support = self.get_tx_cdr_lol_support()
        if tx_cdr_lol_support is None:
            return None
        if not tx_cdr_lol_support:
            return ["N/A" for _ in range(self.NUM_CHANNELS)]
        tx_cdr_lol = self.xcvr_eeprom.read(consts.TX_CDR_LOL)
        if tx_cdr_lol is None:
            return None
        keys = sorted(tx_cdr_lol.keys())
        tx_cdr_lol_final = []
        for key in keys:
            tx_cdr_lol_final.append(bool(tx_cdr_lol[key]))
        return tx_cdr_lol_final

    def get_rx_los(self):
        '''
        This function returns RX LOS flag on RX media lane
        '''
        rx_los_support = self.get_rx_los_support()
        if rx_los_support is None:
            return None
        if not rx_los_support:
            return ["N/A" for _ in range(self.NUM_CHANNELS)]
        rx_los = self.xcvr_eeprom.read(consts.RX_LOS_FIELD)
        if rx_los is None:
            return None
        keys = sorted(rx_los.keys())
        rx_los_final = []
        for key in keys:
            rx_los_final.append(bool(rx_los[key]))
        return rx_los_final

    def get_rx_cdr_lol_support(self):
        return not self.is_flat_memory() and self.xcvr_eeprom.read(consts.RX_CDR_LOL_SUPPORT_FIELD)

    def get_rx_cdr_lol(self):
        '''
        This function returns RX CDR LOL flag on RX media lane
        '''
        rx_cdr_lol_support = self.get_rx_cdr_lol_support()
        if rx_cdr_lol_support is None:
            return None
        if not rx_cdr_lol_support:
            return ["N/A" for _ in range(self.NUM_CHANNELS)]
        rx_cdr_lol = self.xcvr_eeprom.read(consts.RX_CDR_LOL)
        if rx_cdr_lol is None:
            return None
        keys = sorted(rx_cdr_lol.keys())
        rx_cdr_lol_final = []
        for key in keys:
            rx_cdr_lol_final.append(bool(rx_cdr_lol[key]))
        return rx_cdr_lol_final

    def get_alarm_flags(self, alarm_flag):
        '''Generic helper to return alarm and warning flags for given type: TX_POWER, TX_BIAS, RX_POWER.'''
        flags = self.xcvr_eeprom.read(getattr(consts, f"{alarm_flag}_ALARM_FLAGS_FIELD"))
        if flags is None:
            return None
        high_alarm = flags.get(getattr(consts, f"{alarm_flag}_HIGH_ALARM_FLAG"))
        low_alarm = flags.get(getattr(consts, f"{alarm_flag}_LOW_ALARM_FLAG"))
        high_warn = flags.get(getattr(consts, f"{alarm_flag}_HIGH_WARN_FLAG"))
        low_warn = flags.get(getattr(consts, f"{alarm_flag}_LOW_WARN_FLAG"))
        if high_alarm is None or low_alarm is None or high_warn is None or low_warn is None:
            return None
        for d in (high_alarm, low_alarm, high_warn, low_warn):
            for key, value in d.items():
                d[key] = bool(value)
        prefix = alarm_flag.lower()
        return {
            f"{prefix}_high_alarm": high_alarm,
            f"{prefix}_low_alarm": low_alarm,
            f"{prefix}_high_warn": high_warn,
            f"{prefix}_low_warn": low_warn,
        }

    def get_tx_power_flag(self):
        '''
        This function returns TX power out of range flag on TX media lane
        '''
        return self.get_alarm_flags("TX_POWER")

    def get_tx_bias_flag(self):
        '''
        This function returns TX bias out of range flag on TX media lane
        '''
        return self.get_alarm_flags("TX_BIAS")

    def get_rx_power_flag(self):
        '''
        This function returns RX power out of range flag on RX media lane
        '''
        return self.get_alarm_flags("RX_POWER")

    def get_tx_output_status(self):
        '''
        This function returns whether TX output signals are valid on TX media lane
        '''
        tx_output_status_dict = self.xcvr_eeprom.read(consts.TX_OUTPUT_STATUS)
        if tx_output_status_dict is None:
            return None
        for key, value in tx_output_status_dict.items():
            tx_output_status_dict[key] = bool(value)
        return tx_output_status_dict

    def get_rx_output_status(self):
        '''
        This function returns whether RX output signals are valid on RX host lane
        '''
        rx_output_status_dict = self.xcvr_eeprom.read(consts.RX_OUTPUT_STATUS)
        if rx_output_status_dict is None:
            return None
        for key, value in rx_output_status_dict.items():
            rx_output_status_dict[key] = bool(value)
        return rx_output_status_dict

    def get_tx_bias_support(self):
        return not self.is_flat_memory() and self.xcvr_eeprom.read(consts.TX_BIAS_SUPPORT_FIELD)

    def get_tx_bias(self):
        '''
        This function returns TX bias current on each media lane
        '''
        tx_bias_support = self.get_tx_bias_support()
        if tx_bias_support is None:
            return None
        if not tx_bias_support:
            return ["N/A" for _ in range(self.NUM_CHANNELS)]
        scale_raw = self.xcvr_eeprom.read(consts.TX_BIAS_SCALE)
        if scale_raw is None:
            return ["N/A" for _ in range(self.NUM_CHANNELS)]
        scale = 2**scale_raw if scale_raw < 3 else 1
        tx_bias = self.xcvr_eeprom.read(consts.TX_BIAS_FIELD)
        if tx_bias is None:
            return ["N/A" for _ in range(self.NUM_CHANNELS)]
        for key, value in tx_bias.items():
            tx_bias[key] *= scale
        return [tx_bias['LaserBiasTx%dField' % i] for i in range(1, self.NUM_CHANNELS + 1)]

    def get_tx_power(self):
        '''
        This function returns TX output power in mW on each media lane
        '''
        tx_power = ["N/A" for _ in range(self.NUM_CHANNELS)]

        tx_power_support = self.get_tx_power_support()
        if not tx_power_support:
            return tx_power

        if tx_power_support:
            tx_power = self.xcvr_eeprom.read(consts.TX_POWER_FIELD)
            if tx_power is not None:
                tx_power =  [tx_power['OpticalPowerTx%dField' %i] for i in range(1, self.NUM_CHANNELS+1)]

        return tx_power

    def get_tx_power_support(self):
        return not self.is_flat_memory() and self.xcvr_eeprom.read(consts.TX_POWER_SUPPORT_FIELD)

    def get_rx_power(self):
        '''
        This function returns RX input power in mW on each media lane
        '''
        rx_power = ["N/A" for _ in range(self.NUM_CHANNELS)]

        rx_power_support = self.get_rx_power_support()
        if not rx_power_support:
            return rx_power

        if rx_power_support:
            rx_power = self.xcvr_eeprom.read(consts.RX_POWER_FIELD)
            if rx_power is not None:
                rx_power = [rx_power['OpticalPowerRx%dField' %i] for i in range(1, self.NUM_CHANNELS+1)]

        return rx_power

    def get_rx_power_support(self):
        return not self.is_flat_memory() and self.xcvr_eeprom.read(consts.RX_POWER_SUPPORT_FIELD)

    def get_tx_fault_support(self):
        return not self.is_flat_memory() and self.xcvr_eeprom.read(consts.TX_FAULT_SUPPORT_FIELD)

    def get_tx_fault(self):
        '''
        This function returns TX fault flag on TX media lane
        '''
        tx_fault_support = self.get_tx_fault_support()
        if tx_fault_support is None:
            return None
        if not tx_fault_support:
            return ["N/A" for _ in range(self.NUM_CHANNELS)]
        tx_fault = self.xcvr_eeprom.read(consts.TX_FAULT_FIELD)
        if tx_fault is None:
            return None
        keys = sorted(tx_fault.keys())
        tx_fault_final = []
        for key in keys:
            tx_fault_final.append(bool(tx_fault[key]))
        return tx_fault_final

    def get_tx_los_support(self):
        return not self.is_flat_memory() and self.xcvr_eeprom.read(consts.TX_LOS_SUPPORT_FIELD)

    def get_tx_los(self):
        '''
        This function returns TX LOS flag on TX host lane
        '''
        tx_los_support = self.get_tx_los_support()
        if tx_los_support is None:
            return None
        if not tx_los_support:
            return ["N/A" for _ in range(self.NUM_CHANNELS)]
        tx_los = self.xcvr_eeprom.read(consts.TX_LOS_FIELD)
        if tx_los is None:
            return None
        keys = sorted(tx_los.keys())
        tx_los_final = []
        for key in keys:
            tx_los_final.append(bool(tx_los[key]))
        return tx_los_final

    @read_only_cached_api_return
    def get_tx_disable_support(self):
        return not self.is_flat_memory() and self.xcvr_eeprom.read(consts.TX_DISABLE_SUPPORT_FIELD)

    def get_tx_disable(self):
        tx_disable_support = self.get_tx_disable_support()
        if tx_disable_support is None:
            return None
        if not tx_disable_support:
            return ["N/A" for _ in range(self.NUM_CHANNELS)]
        tx_disable = self.xcvr_eeprom.read(consts.TX_DISABLE_FIELD)
        if tx_disable is None:
            return None
        return [bool(tx_disable & (1 << i)) for i in range(self.NUM_CHANNELS)]

    def tx_disable(self, tx_disable):
        val = 0xFF if tx_disable else 0x0
        return self.xcvr_eeprom.write(consts.TX_DISABLE_FIELD, val)

    def get_tx_disable_channel(self):
        tx_disable_support = self.get_tx_disable_support()
        if tx_disable_support is None:
            return None
        if not tx_disable_support:
            return 'N/A'
        return self.xcvr_eeprom.read(consts.TX_DISABLE_FIELD)

    def tx_disable_channel(self, channel, disable):
        channel_state = self.get_tx_disable_channel()
        if channel_state is None or channel_state == 'N/A':
            return False

        for i in range(self.NUM_CHANNELS):
            mask = (1 << i)
            if not (channel & mask):
                continue
            if disable:
                channel_state |= mask
            else:
                channel_state &= ~mask

        return self.xcvr_eeprom.write(consts.TX_DISABLE_FIELD, channel_state)

    def get_rx_disable_support(self):
        return not self.is_flat_memory() and self.xcvr_eeprom.read(consts.RX_DISABLE_SUPPORT_FIELD)

    def get_rx_disable(self):
        rx_disable_support = self.get_rx_disable_support()
        if rx_disable_support is None:
            return None
        if not rx_disable_support:
            return ["N/A" for _ in range(self.NUM_CHANNELS)]
        rx_disable = self.xcvr_eeprom.read(consts.RX_DISABLE_FIELD)
        if rx_disable is None:
            return None
        return [bool(rx_disable & (1 << i)) for i in range(self.NUM_CHANNELS)]

    def rx_disable(self, rx_disable):
        val = 0xFF if rx_disable else 0x0
        return self.xcvr_eeprom.write(consts.RX_DISABLE_FIELD, val)

    def get_rx_disable_channel(self):
        rx_disable_support = self.get_rx_disable_support()
        if rx_disable_support is None:
            return None
        if not rx_disable_support:
            return 'N/A'
        return self.xcvr_eeprom.read(consts.RX_DISABLE_FIELD)

    def rx_disable_channel(self, channel, disable):
        channel_state = self.get_rx_disable_channel()
        if channel_state is None or channel_state == 'N/A':
            return False

        for i in range(self.NUM_CHANNELS):
            mask = (1 << i)
            if not (channel & mask):
                continue
            if disable:
                channel_state |= mask
            else:
                channel_state &= ~mask

        return self.xcvr_eeprom.write(consts.RX_DISABLE_FIELD, channel_state)

    def get_laser_tuning_summary(self):
        '''
        This function returns laser tuning status summary on media lane
        '''
        result = self.xcvr_eeprom.read(consts.LASER_TUNING_DETAIL)
        laser_tuning_summary = []
        if (result >> 5) & 0x1:
            laser_tuning_summary.append("TargetOutputPowerOOR")
        if (result >> 4) & 0x1:
            laser_tuning_summary.append("FineTuningOutOfRange")
        if (result >> 3) & 0x1:
            laser_tuning_summary.append("TuningNotAccepted")
        if (result >> 2) & 0x1:
            laser_tuning_summary.append("InvalidChannel")
        if (result >> 1) & 0x1:
            laser_tuning_summary.append("WavelengthUnlocked")
        if (result >> 0) & 0x1:
            laser_tuning_summary.append("TuningComplete")
        return laser_tuning_summary

    def get_power_override(self):
        return None

    def set_power_override(self, power_override, power_set):
        return True

    def get_transceiver_thresholds_support(self):
        return not self.is_flat_memory()

    @read_only_cached_api_return
    def get_lpmode_support(self):
        power_class = self.xcvr_eeprom.read(consts.POWER_CLASS_FIELD)
        if power_class is None:
            return False
        return "Power Class 1" not in power_class

    def get_power_override_support(self):
        return False

    def get_module_media_type(self):
        '''
        This function returns module media type: MMF, SMF, Passive Copper Cable, Active Cable Assembly or Base-T.
        '''
        return self.xcvr_eeprom.read(consts.MEDIA_TYPE_FIELD)

    def get_host_electrical_interface(self):
        '''
        This function returns module host electrical interface. Table 4-5 in SFF-8024 Rev4.6
        '''
        if self.is_flat_memory():
            return 'N/A'
        return self.xcvr_eeprom.read(consts.HOST_ELECTRICAL_INTERFACE)

    def get_module_media_interface(self):
        '''
        This function returns module media electrical interface. Table 4-6 ~ 4-10 in SFF-8024 Rev4.6
        '''
        media_type = self.get_module_media_type()
        if media_type == 'nm_850_media_interface':
            return self.xcvr_eeprom.read(consts.MODULE_MEDIA_INTERFACE_850NM)
        elif media_type == 'sm_media_interface':
            return self.xcvr_eeprom.read(consts.MODULE_MEDIA_INTERFACE_SM)
        elif media_type == 'passive_copper_media_interface':
            return self.xcvr_eeprom.read(consts.MODULE_MEDIA_INTERFACE_PASSIVE_COPPER)
        elif media_type == 'active_cable_media_interface':
            return self.xcvr_eeprom.read(consts.MODULE_MEDIA_INTERFACE_ACTIVE_CABLE)
        elif media_type == 'base_t_media_interface':
            return self.xcvr_eeprom.read(consts.MODULE_MEDIA_INTERFACE_BASE_T)
        else:
            return 'Unknown media interface'

    @read_only_cached_api_return
    def is_coherent_module(self):
        '''
        Returns True if the module follow C-CMIS spec, False otherwise
        '''
        mintf = self.get_module_media_interface()
        return False if 'ZR' not in mintf else True

    @read_only_cached_api_return
    def get_datapath_init_duration(self):
        '''
        This function returns the duration of datapath init
        '''
        if self.is_flat_memory():
            return 0
        duration = self.xcvr_eeprom.read(consts.DP_PATH_INIT_DURATION)
        if duration is None:
            return 0
        value = float(duration)
        return value * DATAPATH_INIT_DURATION_MULTIPLIER if value <= DATAPATH_INIT_DURATION_OVERRIDE_THRESHOLD else value

    @read_only_cached_api_return
    def get_datapath_deinit_duration(self):
        '''
        This function returns the duration of datapath deinit
        '''
        if self.is_flat_memory():
            return 0
        duration = self.xcvr_eeprom.read(consts.DP_PATH_DEINIT_DURATION)
        return float(duration) if duration is not None else 0

    @read_only_cached_api_return
    def get_datapath_tx_turnon_duration(self):
        '''
        This function returns the duration of datapath tx turnon
        '''
        if self.is_flat_memory():
            return 0
        duration = self.xcvr_eeprom.read(consts.DP_TX_TURNON_DURATION)
        return float(duration) if duration is not None else 0

    @read_only_cached_api_return
    def get_datapath_tx_turnoff_duration(self):
        '''
        This function returns the duration of datapath tx turnoff
        '''
        if self.is_flat_memory():
            return 0
        duration = self.xcvr_eeprom.read(consts.DP_TX_TURNOFF_DURATION)
        return float(duration) if duration is not None else 0

    @read_only_cached_api_return
    def get_module_pwr_up_duration(self):
        '''
        This function returns the duration of module power up
        '''
        if self.is_flat_memory():
            return 0
        duration = self.xcvr_eeprom.read(consts.MODULE_PWRUP_DURATION)
        return float(duration) if duration is not None else 0

    @read_only_cached_api_return
    def get_module_pwr_down_duration(self):
        '''
        This function returns the duration of module power down
        '''
        if self.is_flat_memory():
            return 0
        duration = self.xcvr_eeprom.read(consts.MODULE_PWRDN_DURATION)
        return float(duration) if duration is not None else 0

    def get_host_lane_count(self):
        '''
        This function returns number of host lanes for default application
        '''
        return self.xcvr_eeprom.read(consts.HOST_LANE_COUNT)

    def get_media_lane_count(self, appl=1):
        '''
        This function returns number of media lanes for default application
        '''
        if self.is_flat_memory():
            return 0
        
        if (appl <= 0):
            return 0
        
        appl_advt = self.get_application_advertisement()
        return appl_advt[appl]['media_lane_count'] if len(appl_advt) >= appl else 0

    def get_media_interface_technology(self):
        '''
        This function returns the media lane technology
        '''
        return self.xcvr_eeprom.read(consts.MEDIA_INTERFACE_TECH)

    def get_host_lane_assignment_option(self, appl=1):
        '''
        This function returns the host lane that the application begins on
        Args:
            app:
                Integer, desired application for which host_lane_assignment_options are requested
        '''
        if (appl <= 0):
            return 0

        appl_advt = self.get_application_advertisement()
        if appl not in appl_advt:
            logger.error('Application {} not found in application advertisement'.format(appl))
            return 0

        return appl_advt[appl].get('host_lane_assignment_options', 0)

    def get_media_lane_assignment_option(self, appl=1):
        '''
        This function returns the media lane that the application is allowed to begin on
        '''
        if self.is_flat_memory():
            return 'N/A'
        
        if (appl <= 0):
            return 0
        
        appl_advt = self.get_application_advertisement()
        return appl_advt[appl]['media_lane_assignment_options'] if len(appl_advt) >= appl else 0

    def get_active_apsel_hostlane(self):
        '''
        This function returns the application select code that each host lane has
        '''
        if (self.is_flat_memory()):
            return defaultdict(lambda: 'N/A')

        active_apsel_code = self.xcvr_eeprom.read(consts.ACTIVE_APSEL_CODE)
        return defaultdict(lambda: 'N/A') if not active_apsel_code else active_apsel_code

    def get_tx_config_power(self):
        '''
        This function returns the configured TX output power. Unit in dBm
        '''
        return self.xcvr_eeprom.read(consts.TX_CONFIG_POWER)

    def get_media_output_loopback(self):
        '''
        This function returns the media output loopback status
        '''
        result = self.xcvr_eeprom.read(consts.MEDIA_OUTPUT_LOOPBACK)
        if result is None:
            return None
        return result == 1

    def get_media_input_loopback(self):
        '''
        This function returns the media input loopback status
        '''
        result = self.xcvr_eeprom.read(consts.MEDIA_INPUT_LOOPBACK)
        if result is None:
            return None
        return result == 1

    def get_host_output_loopback(self):
        '''
        This function returns the host output loopback status
        '''
        result = self.xcvr_eeprom.read(consts.HOST_OUTPUT_LOOPBACK)
        if result is None:
            return None
        loopback_status = []
        for bitpos in range(self.NUM_CHANNELS):
            loopback_status.append(bool((result >> bitpos) & 0x1))
        return loopback_status

    def get_host_input_loopback(self):
        '''
        This function returns the host input loopback status
        '''
        result = self.xcvr_eeprom.read(consts.HOST_INPUT_LOOPBACK)
        if result is None:
            return None
        loopback_status = []
        for bitpos in range(self.NUM_CHANNELS):
            loopback_status.append(bool((result >> bitpos) & 0x1))
        return loopback_status

    def get_aux_mon_type(self):
        '''
        This function returns the aux monitor types
        '''
        result = self.xcvr_eeprom.read(consts.AUX_MON_TYPE) if not self.is_flat_memory() else None
        if result is None:
            return None
        aux1_mon_type = result & 0x1
        aux2_mon_type = (result >> 1) & 0x1
        aux3_mon_type = (result >> 2) & 0x1
        return aux1_mon_type, aux2_mon_type, aux3_mon_type

    def get_laser_temperature(self):
        '''
        This function returns the laser temperature monitor value
        '''
        laser_temp_dict = {
            'monitor value' : 'N/A',
            'high alarm' : 'N/A',
            'low alarm' : 'N/A',
            'high warn' : 'N/A',
            'low warn' : 'N/A'
        }

        if self.is_flat_memory():
            return laser_temp_dict

        try:
            aux1_mon_type, aux2_mon_type, aux3_mon_type = self.get_aux_mon_type()
        except TypeError:
            return laser_temp_dict
        if aux2_mon_type == 0:
            laser_temp = self._get_laser_temp_threshold(consts.AUX2_MON)
            laser_temp_high_alarm = self._get_laser_temp_threshold(consts.AUX2_HIGH_ALARM)
            laser_temp_low_alarm = self._get_laser_temp_threshold(consts.AUX2_LOW_ALARM)
            laser_temp_high_warn = self._get_laser_temp_threshold(consts.AUX2_HIGH_WARN)
            laser_temp_low_warn = self._get_laser_temp_threshold(consts.AUX2_LOW_WARN)
        elif aux2_mon_type == 1 and aux3_mon_type == 0:
            laser_temp = self._get_laser_temp_threshold(consts.AUX3_MON)
            laser_temp_high_alarm = self._get_laser_temp_threshold(consts.AUX3_HIGH_ALARM)
            laser_temp_low_alarm = self._get_laser_temp_threshold(consts.AUX3_LOW_ALARM)
            laser_temp_high_warn = self._get_laser_temp_threshold(consts.AUX3_HIGH_WARN)
            laser_temp_low_warn = self._get_laser_temp_threshold(consts.AUX3_LOW_WARN)
        else:
            return laser_temp_dict
        laser_temp_dict = {'monitor value': laser_temp,
                           'high alarm': laser_temp_high_alarm,
                           'low alarm': laser_temp_low_alarm,
                           'high warn': laser_temp_high_warn,
                           'low warn': laser_temp_low_warn}
        return laser_temp_dict

    def _get_laser_temp_threshold(self, field):
        LASER_TEMP_SCALE = 256.0
        value = self.xcvr_eeprom.read(field)
        return value/LASER_TEMP_SCALE if value is not None else 'N/A'

    def get_laser_TEC_current(self):
        '''
        This function returns the laser TEC current monitor value
        '''
        try:
            aux1_mon_type, aux2_mon_type, aux3_mon_type = self.get_aux_mon_type()
        except TypeError:
            return None
        LASER_TEC_CURRENT_SCALE = 32767.0
        if aux1_mon_type == 1:
            laser_tec_current = self.xcvr_eeprom.read(consts.AUX1_MON)/LASER_TEC_CURRENT_SCALE
            laser_tec_current_high_alarm = self.xcvr_eeprom.read(consts.AUX1_HIGH_ALARM)/LASER_TEC_CURRENT_SCALE
            laser_tec_current_low_alarm = self.xcvr_eeprom.read(consts.AUX1_LOW_ALARM)/LASER_TEC_CURRENT_SCALE
            laser_tec_current_high_warn = self.xcvr_eeprom.read(consts.AUX1_HIGH_WARN)/LASER_TEC_CURRENT_SCALE
            laser_tec_current_low_warn = self.xcvr_eeprom.read(consts.AUX1_LOW_WARN)/LASER_TEC_CURRENT_SCALE
        elif aux1_mon_type == 0 and aux2_mon_type == 1:
            laser_tec_current = self.xcvr_eeprom.read(consts.AUX2_MON)/LASER_TEC_CURRENT_SCALE
            laser_tec_current_high_alarm = self.xcvr_eeprom.read(consts.AUX2_HIGH_ALARM)/LASER_TEC_CURRENT_SCALE
            laser_tec_current_low_alarm = self.xcvr_eeprom.read(consts.AUX2_LOW_ALARM)/LASER_TEC_CURRENT_SCALE
            laser_tec_current_high_warn = self.xcvr_eeprom.read(consts.AUX2_HIGH_WARN)/LASER_TEC_CURRENT_SCALE
            laser_tec_current_low_warn = self.xcvr_eeprom.read(consts.AUX2_LOW_WARN)/LASER_TEC_CURRENT_SCALE
        else:
            return None
        laser_tec_current_dict = {'monitor value': laser_tec_current,
                                  'high alarm': laser_tec_current_high_alarm,
                                  'low alarm': laser_tec_current_low_alarm,
                                  'high warn': laser_tec_current_high_warn,
                                  'low warn': laser_tec_current_low_warn}
        return laser_tec_current_dict

    def get_config_datapath_hostlane_status(self):
        '''
        This function returns configuration command execution
        / result status for the datapath of each host lane
        '''
        return self.xcvr_eeprom.read(consts.CONFIG_LANE_STATUS)

    def get_datapath_state(self):
        '''
        This function returns the eight datapath states
        '''
        return self.xcvr_eeprom.read(consts.DATA_PATH_STATE)

    def get_dpinit_pending(self):
        '''
        This function returns datapath init pending status.
        0 means datapath init not pending.
        1 means datapath init pending. DPInit not yet executed after successful ApplyDPInit.
        Hence the active control set content may deviate from the actual hardware config
        '''
        dpinit_pending_dict = self.xcvr_eeprom.read(consts.DPINIT_PENDING)
        if dpinit_pending_dict is None:
            return None
        for key, value in dpinit_pending_dict.items():
            dpinit_pending_dict[key] = bool(value)
        return dpinit_pending_dict

    def get_supported_power_config(self):
        '''
        This function returns the supported TX power range
        '''
        min_prog_tx_output_power = self.xcvr_eeprom.read(consts.MIN_PROG_OUTPUT_POWER)
        max_prog_tx_output_power = self.xcvr_eeprom.read(consts.MAX_PROG_OUTPUT_POWER)
        return min_prog_tx_output_power, max_prog_tx_output_power

    def reset_module(self, reset = False):
        '''
        This function resets the module
        Return True if the provision succeeds, False if it fails
        Return True if no action.
        '''
        if reset:
            reset_control = reset << 3
            return self.xcvr_eeprom.write(consts.MODULE_LEVEL_CONTROL, reset_control)
        else:
            return True

    def reset(self):
        """
        Reset SFP and return all user module settings to their default state.

        Returns:
            A boolean, True if successful, False if not
        """
        if self.reset_module(True):
            # minimum waiting time for the TWI to be functional again
            time.sleep(2)
            # buffer time
            for retries in range(5):
                state = self.get_module_state()
                if state in ['ModuleReady', 'ModuleLowPwr']:
                    return True
                time.sleep(1)
        return False

    def get_lpmode(self):
        '''
        Retrieves Low power module status
        Returns True if module in low power else returns False.
        '''
        if self.is_flat_memory() or not self.get_lpmode_support():
            return False

        lpmode = self.xcvr_eeprom.read(consts.TRANS_MODULE_STATUS_FIELD)
        if lpmode is not None:
            if lpmode.get('ModuleState') == 'ModuleLowPwr':
                return True
        return False

    def wait_time_condition(self, condition_func, expected_state, duration_ms, delay_retry):
        '''
        This function will wait and retry based on
        condition function state and delay provided
        '''
        start_time = time.time()
        duration = duration_ms / 1000
        # Loop until the duration has elapsed
        while (time.time() - start_time) < duration:
            if condition_func() == expected_state:
                return True
            # Sleep for a delay_retry interval before the next check
            time.sleep(delay_retry)
        return condition_func() == expected_state

    def set_lpmode(self, lpmode, wait_state_change = True):
        '''
        This function sets the module to low power state.
        lpmode being False means "set to high power"
        lpmode being True means "set to low power"
        Return True if the provision succeeds, False if it fails
        '''

        if self.is_flat_memory() or not self.get_lpmode_support():
            return False

        DELAY_RETRY = 0.1
        lpmode_val = self.xcvr_eeprom.read(consts.MODULE_LEVEL_CONTROL)
        if lpmode_val is not None:
            if lpmode is True:
                # Force module transition to LowPwr under SW control
                lpmode_val = lpmode_val | (1 << CmisApi.LowPwrRequestSW)
                self.xcvr_eeprom.write(consts.MODULE_LEVEL_CONTROL, lpmode_val)
                if wait_state_change:
                    return self.wait_time_condition(self.get_lpmode, True, self.get_module_pwr_down_duration(), DELAY_RETRY)
                else:
                    return True
            else:
                # Force transition from LowPwr to HighPower state under SW control.
                # This will transition LowPwrS signal to False. (see Table 6-12 CMIS v5.0)
                lpmode_val = lpmode_val & ~(1 << CmisApi.LowPwrRequestSW)
                lpmode_val = lpmode_val & ~(1 << CmisApi.LowPwrAllowRequestHW)
                self.xcvr_eeprom.write(consts.MODULE_LEVEL_CONTROL, lpmode_val)
                if wait_state_change:
                    return self.wait_time_condition(self.get_module_state, 'ModuleReady', self.get_module_pwr_up_duration(), DELAY_RETRY)
                else:
                    return True
        return False

    def get_loopback_capability(self):
        '''
        This function returns the module loopback capability as advertised
        '''
        if self.is_flat_memory():
            return None
        allowed_loopback_result = self.xcvr_eeprom.read(consts.LOOPBACK_CAPABILITY)
        if allowed_loopback_result is None:
            return None
        loopback_capability = dict()
        loopback_capability['simultaneous_host_media_loopback_supported'] = bool((allowed_loopback_result >> 6) & 0x1)
        loopback_capability['per_lane_media_loopback_supported'] = bool((allowed_loopback_result >> 5) & 0x1)
        loopback_capability['per_lane_host_loopback_supported'] = bool((allowed_loopback_result >> 4) & 0x1)
        loopback_capability['host_side_input_loopback_supported'] = bool((allowed_loopback_result >> 3) & 0x1)
        loopback_capability['host_side_output_loopback_supported'] = bool((allowed_loopback_result >> 2) & 0x1)
        loopback_capability['media_side_input_loopback_supported'] = bool((allowed_loopback_result >> 1) & 0x1)
        loopback_capability['media_side_output_loopback_supported'] = bool((allowed_loopback_result >> 0) & 0x1)
        return loopback_capability

    def set_host_input_loopback(self, lane_mask, enable):
        '''
        Sets the host-side input loopback mode for specified lanes.

        Args:
            lane_mask (int): A bitmask indicating which lanes to enable/disable loopback.
                - 0xFF: Enable loopback on all lanes.
                - Individual bits represent corresponding lanes.
            enable (bool): True to enable loopback, False to disable.

        Returns:
            bool: True if the operation succeeds, False otherwise.
        '''
        loopback_capability = self.get_loopback_capability()
        if loopback_capability is None:
            logger.info('Failed to get loopback capabilities')
            return False

        if loopback_capability['host_side_input_loopback_supported'] is False:
            logger.error('Host input loopback is not supported')
            return False

        if loopback_capability['per_lane_host_loopback_supported'] is False and lane_mask != 0xff:
            logger.error('Per-lane host input loopback is not supported, lane_mask:%#x', lane_mask)
            return False

        if loopback_capability['simultaneous_host_media_loopback_supported'] is False:
            media_input_val = self.xcvr_eeprom.read(consts.MEDIA_INPUT_LOOPBACK)
            media_output_val = self.xcvr_eeprom.read(consts.MEDIA_OUTPUT_LOOPBACK)
            if media_input_val or media_output_val:
                txt = 'Simultaneous host media loopback is not supported\n'
                txt += f'media_input_val:{media_input_val:#x}, media_output_val:{media_output_val:#x}'
                logger.error(txt)
                return False

        host_input_val = self.xcvr_eeprom.read(consts.HOST_INPUT_LOOPBACK)
        if enable:
            return self.xcvr_eeprom.write(consts.HOST_INPUT_LOOPBACK, host_input_val | lane_mask)
        else:
            return self.xcvr_eeprom.write(consts.HOST_INPUT_LOOPBACK, host_input_val & ~lane_mask)

    def set_host_output_loopback(self, lane_mask, enable):
        '''
        Sets the host-side output loopback mode for specified lanes.

        Args:
            lane_mask (int): A bitmask indicating which lanes to enable/disable loopback.
                - 0xFF: Enable loopback on all lanes.
                - Individual bits represent corresponding lanes.
            enable (bool): True to enable loopback, False to disable.

        Returns:
            bool: True if the operation succeeds, False otherwise.
        '''
        loopback_capability = self.get_loopback_capability()
        if loopback_capability is None:
            logger.info('Failed to get loopback capabilities')
            return False

        if loopback_capability['host_side_output_loopback_supported'] is False:
            logger.error('Host output loopback is not supported')
            return False

        if loopback_capability['per_lane_host_loopback_supported'] is False and lane_mask != 0xff:
            logger.error('Per-lane host output loopback is not supported, lane_mask:%#x', lane_mask)
            return False

        if loopback_capability['simultaneous_host_media_loopback_supported'] is False:
            media_input_val = self.xcvr_eeprom.read(consts.MEDIA_INPUT_LOOPBACK)
            media_output_val = self.xcvr_eeprom.read(consts.MEDIA_OUTPUT_LOOPBACK)
            if media_input_val or media_output_val:
                txt = 'Simultaneous host media loopback is not supported\n'
                txt += f'media_input_val:{media_input_val:x}, media_output_val:{media_output_val:#x}'
                logger.error(txt)
                return False

        host_output_val = self.xcvr_eeprom.read(consts.HOST_OUTPUT_LOOPBACK)
        if enable:
            return self.xcvr_eeprom.write(consts.HOST_OUTPUT_LOOPBACK, host_output_val | lane_mask)
        else:
            return self.xcvr_eeprom.write(consts.HOST_OUTPUT_LOOPBACK, host_output_val & ~lane_mask)

    def set_media_input_loopback(self, lane_mask, enable):
        '''
        Sets the media-side input loopback mode for specified lanes.

        Args:
            lane_mask (int): A bitmask indicating which lanes to enable/disable loopback.
                - 0xFF: Enable loopback on all lanes.
                - Individual bits represent corresponding lanes.
            enable (bool): True to enable loopback, False to disable.

        Returns:
            bool: True if the operation succeeds, False otherwise.
        '''
        loopback_capability = self.get_loopback_capability()
        if loopback_capability is None:
            logger.info('Failed to get loopback capabilities')
            return False

        if loopback_capability['media_side_input_loopback_supported'] is False:
            logger.error('Media input loopback is not supported')
            return False

        if loopback_capability['per_lane_media_loopback_supported'] is False and lane_mask != 0xff:
            logger.error('Per-lane media input loopback is not supported, lane_mask:%#x', lane_mask)
            return False

        if loopback_capability['simultaneous_host_media_loopback_supported'] is False:
            host_input_val = self.xcvr_eeprom.read(consts.HOST_INPUT_LOOPBACK)
            host_output_val = self.xcvr_eeprom.read(consts.HOST_OUTPUT_LOOPBACK)
            if host_input_val or host_output_val:
                txt = 'Simultaneous host media loopback is not supported\n'
                txt += f'host_input_val:{host_input_val:#x}, host_output_val:{host_output_val:#x}'
                logger.error(txt)
                return False

        media_input_val = self.xcvr_eeprom.read(consts.MEDIA_INPUT_LOOPBACK)
        if enable:
            return self.xcvr_eeprom.write(consts.MEDIA_INPUT_LOOPBACK, media_input_val | lane_mask)
        else:
            return self.xcvr_eeprom.write(consts.MEDIA_INPUT_LOOPBACK, media_input_val & ~lane_mask)

    def set_media_output_loopback(self, lane_mask, enable):
        '''
        Sets the media-side output loopback mode for specified lanes.

        Args:
            lane_mask (int): A bitmask indicating which lanes to enable/disable loopback.
                - 0xFF: Enable loopback on all lanes.
                - Individual bits represent corresponding lanes.
            enable (bool): True to enable loopback, False to disable.

        Returns:
            bool: True if the operation succeeds, False otherwise.
        '''
        loopback_capability = self.get_loopback_capability()
        if loopback_capability is None:
            logger.info('Failed to get loopback capabilities')
            return False

        if loopback_capability['media_side_output_loopback_supported'] is False:
            logger.error('Media output loopback is not supported')
            return False

        if loopback_capability['per_lane_media_loopback_supported'] is False and lane_mask != 0xff:
            logger.error('Per-lane media output loopback is not supported, lane_mask:%#x', lane_mask)
            return False

        if loopback_capability['simultaneous_host_media_loopback_supported'] is False:
            host_input_val = self.xcvr_eeprom.read(consts.HOST_INPUT_LOOPBACK)
            host_output_val = self.xcvr_eeprom.read(consts.HOST_OUTPUT_LOOPBACK)
            if host_input_val or host_output_val:
                txt = 'Simultaneous host media loopback is not supported\n'
                txt += f'host_input_val:{host_input_val:#x}, host_output_val:{host_output_val:#x}'
                logger.error(txt)
                return False

        media_output_val = self.xcvr_eeprom.read(consts.MEDIA_OUTPUT_LOOPBACK)
        if enable:
            return self.xcvr_eeprom.write(consts.MEDIA_OUTPUT_LOOPBACK, media_output_val | lane_mask)
        else:
            return self.xcvr_eeprom.write(consts.MEDIA_OUTPUT_LOOPBACK, media_output_val & ~lane_mask)

    def set_loopback_mode(self, loopback_mode, lane_mask = 0xff, enable = False):
        '''
        This function sets the module loopback mode.

        Args:
        - loopback_mode (str): Specifies the loopback mode. It must be one of the following:
            1. "none"
            2. "host-side-input"
            3. "host-side-output"
            4. "media-side-input"
            5. "media-side-output"
        - lane_mask (int): A bitmask representing the lanes to which the loopback mode should
                           be applied. Default 0xFF applies to all lanes.
        - enable (bool): Whether to enable or disable the loopback mode. Default False.
        Returns:
        - bool: True if the operation succeeds, False otherwise.
        '''
        loopback_functions = {
            'host-side-input': self.set_host_input_loopback,
            'host-side-output': self.set_host_output_loopback,
            'media-side-input': self.set_media_input_loopback,
            'media-side-output': self.set_media_output_loopback,
        }

        if loopback_mode == 'none':
            return all([
                self.set_host_input_loopback(0xff, False),
                self.set_host_output_loopback(0xff, False),
                self.set_media_input_loopback(0xff, False),
                self.set_media_output_loopback(0xff, False)
            ])

        func = loopback_functions.get(loopback_mode)
        if func:
            return func(lane_mask, enable)

        logger.error('Invalid loopback mode:%s, lane_mask:%#x', loopback_mode, lane_mask)
        return False

    def is_transceiver_vdm_supported(self):
        '''
        This function returns whether VDM is supported
        '''
        return self.vdm is not None and self.xcvr_eeprom.read(consts.VDM_SUPPORTED)

    def get_vdm(self, field_option=None):
        '''
        This function returns all the VDM items, including real time monitor value, threholds and flags
        '''
        if field_option is None:
            field_option = self.vdm.ALL_FIELD
        vdm = self.vdm.get_vdm_allpage(field_option) if self.vdm is not None else {}
        return vdm

    def get_module_firmware_fault_state_changed(self):
        '''
        This function returns datapath firmware fault state, module firmware fault state
        and whether module state changed
        '''
        result = self.xcvr_eeprom.read(consts.MODULE_FIRMWARE_FAULT_INFO)
        if result is None:
            return None
        datapath_firmware_fault = bool((result >> 2) & 0x1)
        module_firmware_fault = bool((result >> 1) & 0x1)
        module_state_changed = bool(result & 0x1)
        return datapath_firmware_fault, module_firmware_fault, module_state_changed

    def get_module_level_flag(self):
        '''
        This function returns teh module level flags, including
        - 3.3 V voltage supply flags
        - Case temperature flags
        - Aux 1 flags
        - Aux 2 flags
        - Aux 3 flags
        - Custom field flags
        '''
        module_flag_byte1 = self.xcvr_eeprom.read(consts.MODULE_FLAG_BYTE1)
        module_flag_byte2 = self.xcvr_eeprom.read(consts.MODULE_FLAG_BYTE2)
        module_flag_byte3 = self.xcvr_eeprom.read(consts.MODULE_FLAG_BYTE3)
        if module_flag_byte1 is None or module_flag_byte2 is None or module_flag_byte3 is None:
            return None
        voltage_high_alarm_flag = bool((module_flag_byte1 >> 4) & 0x1)
        voltage_low_alarm_flag = bool((module_flag_byte1 >> 5) & 0x1)
        voltage_high_warn_flag = bool((module_flag_byte1 >> 6) & 0x1)
        voltage_low_warn_flag = bool((module_flag_byte1 >> 7) & 0x1)
        voltage_flags = {'voltage_high_alarm_flag': voltage_high_alarm_flag,
                         'voltage_low_alarm_flag': voltage_low_alarm_flag,
                         'voltage_high_warn_flag': voltage_high_warn_flag,
                         'voltage_low_warn_flag': voltage_low_warn_flag}

        case_temp_high_alarm_flag = bool((module_flag_byte1 >> 0) & 0x1)
        case_temp_low_alarm_flag = bool((module_flag_byte1 >> 1) & 0x1)
        case_temp_high_warn_flag = bool((module_flag_byte1 >> 2) & 0x1)
        case_temp_low_warn_flag = bool((module_flag_byte1 >> 3) & 0x1)
        case_temp_flags = {'case_temp_high_alarm_flag': case_temp_high_alarm_flag,
                           'case_temp_low_alarm_flag': case_temp_low_alarm_flag,
                           'case_temp_high_warn_flag': case_temp_high_warn_flag,
                           'case_temp_low_warn_flag': case_temp_low_warn_flag}

        aux2_high_alarm_flag = bool((module_flag_byte2 >> 4) & 0x1)
        aux2_low_alarm_flag = bool((module_flag_byte2 >> 5) & 0x1)
        aux2_high_warn_flag = bool((module_flag_byte2 >> 6) & 0x1)
        aux2_low_warn_flag = bool((module_flag_byte2 >> 7) & 0x1)
        aux2_flags = {'aux2_high_alarm_flag': aux2_high_alarm_flag,
                      'aux2_low_alarm_flag': aux2_low_alarm_flag,
                      'aux2_high_warn_flag': aux2_high_warn_flag,
                      'aux2_low_warn_flag': aux2_low_warn_flag}
        aux1_high_alarm_flag = bool((module_flag_byte2 >> 0) & 0x1)
        aux1_low_alarm_flag = bool((module_flag_byte2 >> 1) & 0x1)
        aux1_high_warn_flag = bool((module_flag_byte2 >> 2) & 0x1)
        aux1_low_warn_flag = bool((module_flag_byte2 >> 3) & 0x1)
        aux1_flags = {'aux1_high_alarm_flag': aux1_high_alarm_flag,
                      'aux1_low_alarm_flag': aux1_low_alarm_flag,
                      'aux1_high_warn_flag': aux1_high_warn_flag,
                      'aux1_low_warn_flag': aux1_low_warn_flag}

        custom_mon_high_alarm_flag = bool((module_flag_byte3 >> 4) & 0x1)
        custom_mon_low_alarm_flag = bool((module_flag_byte3 >> 5) & 0x1)
        custom_mon_high_warn_flag = bool((module_flag_byte3 >> 6) & 0x1)
        custom_mon_low_warn_flag = bool((module_flag_byte3 >> 7) & 0x1)
        custom_mon_flags = {'custom_mon_high_alarm_flag': custom_mon_high_alarm_flag,
                            'custom_mon_low_alarm_flag': custom_mon_low_alarm_flag,
                            'custom_mon_high_warn_flag': custom_mon_high_warn_flag,
                            'custom_mon_low_warn_flag': custom_mon_low_warn_flag}

        aux3_high_alarm_flag = bool((module_flag_byte3 >> 0) & 0x1)
        aux3_low_alarm_flag = bool((module_flag_byte3 >> 1) & 0x1)
        aux3_high_warn_flag = bool((module_flag_byte3 >> 2) & 0x1)
        aux3_low_warn_flag = bool((module_flag_byte3 >> 3) & 0x1)
        aux3_flags = {'aux3_high_alarm_flag': aux3_high_alarm_flag,
                      'aux3_low_alarm_flag': aux3_low_alarm_flag,
                      'aux3_high_warn_flag': aux3_high_warn_flag,
                      'aux3_low_warn_flag': aux3_low_warn_flag}

        module_flag = {'voltage_flags': voltage_flags,
                       'case_temp_flags': case_temp_flags,
                       'aux1_flags': aux1_flags,
                       'aux2_flags': aux2_flags,
                       'aux3_flags': aux3_flags,
                       'custom_mon_flags': custom_mon_flags}
        return module_flag

    def get_module_fw_mgmt_feature(self, verbose = False):
        """
        This function obtains CDB features supported by the module from CDB command 0041h,
        such as start header size, maximum block size, whether extended payload messaging
        (page 0xA0 - 0xAF) or only local payload is supported. These features are important because
        the following upgrade with depend on these parameters.
        """
        txt = ''
        if self.cdb is None:
            return {'status': False, 'info': "CDB Not supported", 'feature': None}

        # get fw upgrade features (CMD 0041h)
        starttime = time.time()
        autopaging = self.xcvr_eeprom.read(consts.AUTO_PAGING_SUPPORT)
        autopaging_flag = bool(autopaging)
        writelength_raw = self.xcvr_eeprom.read(consts.CDB_SEQ_WRITE_LENGTH_EXT)
        if writelength_raw is None:
            return None
        writelength = (writelength_raw + 1) * 8
        txt += 'Auto page support: %s\n' %autopaging_flag
        txt += 'Max write length: %d\n' %writelength
        result = self.cdb.get_fw_management_features()
        status = result['status']
        _, rpl_chkcode, rpl = result['rpl']

        if status == 1 and self.cdb.cdb_chkcode(rpl) == rpl_chkcode:
            startLPLsize = rpl[2]
            txt += 'Start payload size %d\n' % startLPLsize
            maxblocksize = (rpl[4] + 1) * 8
            txt += 'Max block size %d\n' % maxblocksize
            lplEplSupport = {0x00 : 'No write to LPL/EPL supported',
                            0x01 : 'Write to LPL supported',
                            0x10 : 'Write to EPL supported',
                            0x11 : 'Write to LPL/EPL supported'}
            txt += '{}\n'.format(lplEplSupport[rpl[5]])
            if rpl[5] == 1:
                lplonly_flag = True
            else:
                lplonly_flag = False
            txt += 'Abort CMD102h supported %s\n' %bool(rpl[1] & 0x01)
            if verbose:
                txt += 'Copy CMD108h supported %s\n' %bool((rpl[1] >> 1) & 0x01)
                txt += 'Skipping erased blocks supported %s\n' %bool((rpl[1] >> 2) & 0x01)
                txt += 'Full image readback supported %s\n' %bool((rpl[1] >> 7) & 0x01)
                txt += 'Default erase byte {:#x}\n'.format(rpl[3])
                txt += 'Read to LPL/EPL {:#x}\n'.format(rpl[6])

        else:
            txt += 'Status or reply payload check code error\n'
            logger.error(txt)
            logger.error('Fail to get fw mgmt feature, cdb status: {:#x}, cdb_chkcode: {:#x}, rpl_chkcode: {:#x}\n'.format(status, self.cdb.cdb_chkcode(rpl), rpl_chkcode))
            return {'status': False, 'info': txt, 'feature': None}
        elapsedtime = time.time()-starttime
        logger.info('Get module FW upgrade features time: %.2f s\n' %elapsedtime)
        logger.info(txt)
        return {'status': True, 'info': txt, 'feature': (startLPLsize, maxblocksize, lplonly_flag, autopaging_flag, writelength)}

    def get_module_fw_info(self):
        """
        This function returns firmware Image A and B version, running version, committed version
        and whether both firmware images are valid.
        Operational Status: 1 = running, 0 = not running
        Administrative Status: 1=committed, 0=uncommitted
        Validity Status: 1 = invalid, 0 = valid
        """
        txt = ''

        if self.cdb is None:
            return {'status': False, 'info': "CDB Not supported", 'result': None}

        # get fw info (CMD 0100h)
        result = self.cdb.get_fw_info()
        status = result['status']
        rpllen, rpl_chkcode, rpl = result['rpl']

        # Interface NACK or timeout
        if (rpllen is None) or (rpl_chkcode is None):
            return {'status': False, 'info': "Interface fail", 'result': 0} # Return result 0 for distinguishing CDB is maybe in busy or failure.

        # password issue
        if status == 0x46:
            string = 'Get module FW info: Need to enter password\n'
            logger.info(string)
            # Reset password for module using CMIS 4.0
            self.cdb.module_enter_password(0)
            result = self.cdb.get_fw_info()
            status = result['status']
            rpllen, rpl_chkcode, rpl = result['rpl']

        if status == 1 and self.cdb.cdb_chkcode(rpl) == rpl_chkcode:
            # Regiter 9Fh:136
            fwStatus = rpl[0]
            ImageARunning = (fwStatus & 0x01) # bit 0 - image A is running
            ImageACommitted = ((fwStatus >> 1) & 0x01) # bit 1 - image A is committed
            ImageAValid = ((fwStatus >> 2) & 0x01) # bit 2 - image A is valid
            ImageBRunning = ((fwStatus >> 4) & 0x01) # bit 4 - image B is running
            ImageBCommitted = ((fwStatus >> 5) & 0x01)  # bit 5 - image B is committed
            ImageBValid = ((fwStatus >> 6) & 0x01) # bit 6 - image B is valid

            if ImageAValid == 0:
                # Registers 9Fh:138,139; 140,141
                ImageA = '%d.%d.%d' %(rpl[2], rpl[3], ((rpl[4]<< 8) | rpl[5]))
            else:
                ImageA = "N/A"
            txt += 'Image A Version: %s\n' %ImageA

            if ImageBValid == 0:
                # Registers 9Fh:174,175; 176.177
                ImageB = '%d.%d.%d' %(rpl[38], rpl[39], ((rpl[40]<< 8) | rpl[41]))
            else:
                ImageB = "N/A"
            txt += 'Image B Version: %s\n' %ImageB

            if rpllen > 77:
                factory_image = '%d.%d.%d' % (rpl[74], rpl[75], ((rpl[76] << 8) | rpl[77]))
                txt += 'Factory Image Version: %s\n' %factory_image

            ActiveFirmware = 'N/A'
            InactiveFirmware = 'N/A'
            if ImageARunning == 1:
                RunningImage = 'A'
                ActiveFirmware = ImageA
                if ImageBValid == 0:
                    InactiveFirmware = ImageB
                else:
                    #In case of single bank module, inactive firmware version can be read from EEPROM
                    InactiveFirmware = self.get_module_inactive_firmware() + ".0"
            elif ImageBRunning == 1:
                RunningImage = 'B'
                ActiveFirmware = ImageB
                if ImageAValid == 0:
                    InactiveFirmware = ImageA
                else:
                    #In case of single bank module, inactive firmware version can be read from EEPROM
                    InactiveFirmware = self.get_module_inactive_firmware() + ".0"
            else:
                RunningImage = 'N/A'
            if ImageACommitted == 1:
                CommittedImage = 'A'
            elif ImageBCommitted == 1:
                CommittedImage = 'B'
            else:
                CommittedImage = 'N/A'
            txt += 'Running Image: %s\n' % (RunningImage)
            txt += 'Committed Image: %s\n' % (CommittedImage)
            txt += 'Active Firmware: {}\n'.format(ActiveFirmware)
            txt += 'Inactive Firmware: {}\n'.format(InactiveFirmware)
        else:
            txt += 'Reply payload check code error\n'
            return {'status': False, 'info': txt, 'result': None}
        return {'status': True, 'info': txt, 'result': (ImageA, ImageARunning, ImageACommitted, ImageAValid, ImageB, ImageBRunning, ImageBCommitted, ImageBValid, ActiveFirmware, InactiveFirmware)}

    def cdb_run_firmware(self, mode = 0x01):
        # run module FW (CMD 0109h)
        return self.cdb.run_fw_image(mode)

    def cdb_commit_firmware(self):
        return self.cdb.commit_fw_image()

    def module_fw_run(self, mode = 0x01):
        """
        This command is used to start and run a selected image.
        This command transfers control from the currently
        running firmware to a selected firmware that is started. It
        can be used to switch between firmware versions, or to
        perform a restart of the currently running firmware.
        mode:
        00h = Traffic affecting Reset to Inactive Image.
        01h = Attempt Hitless Reset to Inactive Image
        02h = Traffic affecting Reset to Running Image.
        03h = Attempt Hitless Reset to Running Image

        This function returns True if firmware run successfully completes.
        Otherwise it will return False.
        """
        # run module FW (CMD 0109h)
        txt = ''
        if self.cdb is None:
            return False, "CDB NOT supported on this module"
        starttime = time.time()
        fw_run_status = self.cdb.run_fw_image(mode)
        if fw_run_status == 1:
            txt += 'Module FW run: Success\n'
        # password issue
        elif fw_run_status == 70:
            string = 'Module FW run: Need to enter password\n'
            logger.info(string)
            self.cdb.module_enter_password()
            fw_run_status = self.cdb.run_fw_image(mode)
            txt += 'FW_run_status %d\n' %fw_run_status
        else:
            # self.cdb.abort_fw_download()
            txt += 'Module FW run: Fail\n'
            txt += 'FW_run_status %d\n' %fw_run_status
            return False, txt
        elapsedtime = time.time()-starttime
        logger.info('Module FW run time: %.2f s\n' %elapsedtime)
        logger.info(txt)
        return True, txt

    def module_fw_commit(self):
        """
        The host uses this command to commit the running image
        so that the module will boot from it on future boots.

        This function returns True if firmware commit successfully completes.
        Otherwise it will return False.
        """
        txt = ''
        if self.cdb is None:
            return False, "CDB NOT supported on this module"
        # commit module FW (CMD 010Ah)
        starttime = time.time()
        fw_commit_status= self.cdb.commit_fw_image()
        if fw_commit_status == 1:
            txt += 'Module FW commit: Success\n'
        # password issue
        elif fw_commit_status == 70:
            string = 'Module FW commit: Need to enter password\n'
            logger.info(string)
            self.cdb.module_enter_password()
            fw_commit_status = self.cdb.commit_fw_image()
            txt += 'FW_commit_status %d\n' %fw_commit_status
        else:
            # self.cdb.abort_fw_download()
            txt += 'Module FW commit: Fail\n'
            txt += 'FW_commit_status %d\n' %fw_commit_status
            return False, txt
        elapsedtime = time.time()-starttime
        logger.info('Module FW commit time: %.2f s\n' %elapsedtime)
        logger.info(txt)
        return True, txt

    def cdb_firmware_download_complete(self):
        # complete FW download (CMD 0107h)
        return self.cdb.validate_fw_image()

    def cdb_start_firmware_download(self, startLPLsize, startdata, imagesize):
        return self.cdb.start_fw_download(startLPLsize, bytearray(startdata), imagesize)

    def cdb_lpl_block_write(self, address, data):
        return self.cdb.block_write_lpl(address, data)

    def cdb_epl_block_write(self, address, data, autopaging_flag, writelength):
        return self.cdb.block_write_epl(address, data, autopaging_flag, writelength)

    def cdb_enter_host_password(self, password):
        return self.cdb.module_enter_password(password)

    def module_fw_download(self, startLPLsize, maxblocksize, lplonly_flag, autopaging_flag, writelength, imagepath):
        """
        This function performs the download of a firmware image to module eeprom
        It starts CDB download by writing the header of start header size
        from the designated firmware file to the local payload page 0x9F, with CDB command 0101h.

        Then it repeatedly reads from the given firmware file and write to the payload
        space advertised from the first step. We use CDB command 0103h to write to the local payload;
        we use CDB command 0104h to write to the extended paylaod. This step repeats until it reaches
        end of the firmware file, or the CDB status failed.

        The last step is to complete the firmware upgrade with CDB command 0107h.

        Note that if the download process fails anywhere in the middle, we need to run CDB command 0102h
        to abort the upgrade before we restart another upgrade process.

        This function returns True if download successfully completes. Otherwise it will return False where it fails.
        """
        txt = ''
        if self.cdb is None:
            return False, "CDB NOT supported on this module"

        # start fw download (CMD 0101h)
        starttime = time.time()
        try:
            f = open(imagepath, 'rb')
        except FileNotFoundError:
            txt += 'Image path  %s is incorrect.\n' % imagepath
            logger.info(txt)
            return False, txt

        f.seek(0, 2)
        imagesize = f.tell()
        f.seek(0, 0)
        startdata = f.read(startLPLsize)
        logger.info('\nStart FW downloading')
        logger.info("startLPLsize is %d" %startLPLsize)
        fw_start_status = self.cdb.start_fw_download(startLPLsize, bytearray(startdata), imagesize)
        if fw_start_status == 1:
            string = 'Start module FW download: Success\n'
            logger.info(string)
        # password error
        elif fw_start_status == 70:
            string = 'Start module FW download: Need to enter password\n'
            logger.info(string)
            self.cdb.module_enter_password()
            self.cdb.start_fw_download(startLPLsize, bytearray(startdata), imagesize)
        else:
            string = 'Start module FW download: Fail\n'
            txt += string
            self.cdb.abort_fw_download()
            txt += 'FW_start_status %d\n' %fw_start_status
            logger.info(txt)
            return False, txt
        elapsedtime = time.time()-starttime
        logger.info('Start module FW download time: %.2f s' %elapsedtime)

        # start periodically writing (CMD 0103h or 0104h)
        # assert maxblocksize == 2048 or lplonly_flag
        if lplonly_flag:
            BLOCK_SIZE = 116
        else:
            BLOCK_SIZE = maxblocksize
        address = 0
        remaining = imagesize - startLPLsize
        logger.info("\nTotal size: {} start bytes: {} remaining: {}".format(imagesize, startLPLsize, remaining))
        while remaining > 0:
            if remaining < BLOCK_SIZE:
                count = remaining
            else:
                count = BLOCK_SIZE
            data = f.read(count)
            if lplonly_flag:
                fw_download_status = self.cdb.block_write_lpl(address, data)
            else:
                fw_download_status = self.cdb.block_write_epl(address, data, autopaging_flag, writelength)
            if fw_download_status != 1:
                self.cdb.abort_fw_download()
                txt += 'CDB download failed. CDB Status: %d\n' %fw_download_status
                txt += 'FW_download_status %d\n' %fw_download_status
                logger.info(txt)
                return False, txt
            elapsedtime = time.time()-starttime
            address += count
            remaining -= count
            progress = (imagesize - remaining) * 100.0 / imagesize
            logger.info('Address: {:#08x}; Count: {}; Remain: {:#08x}; Progress: {:.2f}%; Time: {:.2f}s'.format(address, count, remaining, progress, elapsedtime))

        elapsedtime = time.time()-starttime
        logger.info('Total module FW download time: %.2f s' %elapsedtime)

        time.sleep(2)
        # complete FW download (CMD 0107h)
        fw_complete_status = self.cdb.validate_fw_image()
        if fw_complete_status == 1:
            string = 'Module FW download complete: Success'
            logger.info(string)
            txt += string
        else:
            txt += 'Module FW download complete: Fail\n'
            txt += 'FW_complete_status %d\n' %fw_complete_status
            logger.info(txt)
            return False, txt
        elapsedtime = time.time()-elapsedtime-starttime
        string = 'Complete module FW download time: %.2f s\n' %elapsedtime
        logger.info(string)
        txt += string
        return True, txt

    def module_fw_upgrade(self, imagepath):
        """
        This function performs firmware upgrade.
        1.  show FW version in the beginning
        2.  check module advertised FW download capability
        3.  configure download
        4.  show download progress
        5.  configure run downloaded firmware
        6.  configure commit downloaded firmware
        7.  show FW version in the end

        imagepath specifies where firmware image file is located.
        target_firmware is a string that specifies the firmware version to upgrade to

        This function returns True if download successfully completes.
        Otherwise it will return False.
        """
        result = self.get_module_fw_info()
        try:
            _, _, _, _, _, _, _, _, _, _ = result['result']
        except (ValueError, TypeError):
            return result['status'], result['info']
        result = self.get_module_fw_mgmt_feature()
        try:
            startLPLsize, maxblocksize, lplonly_flag, autopaging_flag, writelength = result['feature']
        except (ValueError, TypeError):
            return result['status'], result['info']
        download_status, txt = self.module_fw_download(startLPLsize, maxblocksize, lplonly_flag, autopaging_flag, writelength, imagepath)
        if not download_status:
            return False, txt
        switch_status, switch_txt = self.module_fw_switch()
        status = download_status and switch_status
        txt += switch_txt
        return status, txt

    def module_fw_switch(self):
        """
        This function switch the active/inactive module firmware in the current module memory
        This function returns True if firmware switch successfully completes.
        Otherwise it will return False.
        If not both images are valid, it will stop firmware switch and return False
        """
        txt = ''
        result = self.get_module_fw_info()
        try:
            (ImageA_init, ImageARunning_init, ImageACommitted_init, ImageAValid_init,
             ImageB_init, ImageBRunning_init, ImageBCommitted_init, ImageBValid_init, _, _) = result['result']
        except (ValueError, TypeError):
            return result['status'], result['info']
        if ImageAValid_init == 0 and ImageBValid_init == 0:
            self.module_fw_run(mode = 0x01)
            time.sleep(60)
            self.module_fw_commit()
            (ImageA, ImageARunning, ImageACommitted, ImageAValid,
             ImageB, ImageBRunning, ImageBCommitted, ImageBValid, _, _) = self.get_module_fw_info()['result']
            # detect if image switch happened
            txt += 'Before switch Image A: %s; Run: %d Commit: %d, Valid: %d\n' %(
                ImageA_init, ImageARunning_init, ImageACommitted_init, ImageAValid_init
            )
            txt += 'Before switch Image B: %s; Run: %d Commit: %d, Valid: %d\n' %(
                ImageB_init, ImageBRunning_init, ImageBCommitted_init, ImageBValid_init
            )
            txt += 'After switch Image A: %s; Run: %d Commit: %d, Valid: %d\n' %(ImageA, ImageARunning, ImageACommitted, ImageAValid)
            txt += 'After switch Image B: %s; Run: %d Commit: %d, Valid: %d\n' %(ImageB, ImageBRunning, ImageBCommitted, ImageBValid)
            if (ImageARunning_init == 1 and ImageARunning == 1) or (ImageBRunning_init == 1 and ImageBRunning == 1):
                txt += 'Switch did not happen.\n'
                logger.info(txt)
                return False, txt
            else:
                logger.info(txt)
                return True, txt
        else:
            txt += 'Not both images are valid.'
            logger.info(txt)
            return False, txt

    def get_transceiver_status(self):
        """
        Retrieves the current status of the transceiver module.

        Accesses non-latched registers to gather information about the module's state,
        fault causes, and datapath-level statuses, including TX and RX statuses.

        Returns:
            dict: A dictionary containing boolean values for various status fields, as defined in
                the TRANSCEIVER_STATUS table in STATE_DB.
        """
        trans_status = dict()
        trans_status['module_state'] = self.get_module_state()
        trans_status['module_fault_cause'] = self.get_module_fault_cause()
        if not self.is_flat_memory():
            dp_state_dict = self.get_datapath_state()
            if dp_state_dict:
                for lane in range(1, self.NUM_CHANNELS+1):
                    trans_status['DP%dState' % lane] = dp_state_dict.get('DP%dState' % lane)
            tx_output_status_dict = self.get_tx_output_status()
            if tx_output_status_dict:
                for lane in range(1, self.NUM_CHANNELS+1):
                    trans_status['tx%dOutputStatus' % lane] = tx_output_status_dict.get('TxOutputStatus%d' % lane)
            rx_output_status_dict = self.get_rx_output_status()
            if rx_output_status_dict:
                for lane in range(1, self.NUM_CHANNELS+1):
                    trans_status['rx%dOutputStatusHostlane' % lane] = rx_output_status_dict.get('RxOutputStatus%d' % lane)
            tx_disabled_channel = self.get_tx_disable_channel()
            if tx_disabled_channel is not None:
                trans_status['tx_disabled_channel'] = tx_disabled_channel
            tx_disable = self.get_tx_disable()
            if tx_disable is not None:
                for lane in range(1, self.NUM_CHANNELS+1):
                    trans_status['tx%ddisable' % lane] = tx_disable[lane - 1]
            config_status_dict = self.get_config_datapath_hostlane_status()
            if config_status_dict:
                for lane in range(1, self.NUM_CHANNELS+1):
                    trans_status['config_state_hostlane%d' % lane] = config_status_dict.get('ConfigStatusLane%d' % lane)
            dedeint_hostlane = self.get_datapath_deinit()
            if dedeint_hostlane is not None:
                for lane in range(1, self.NUM_CHANNELS+1):
                    trans_status['dpdeinit_hostlane%d' % lane] = dedeint_hostlane[lane - 1]
            dpinit_pending_dict = self.get_dpinit_pending()
            if dpinit_pending_dict:
                for lane in range(1, self.NUM_CHANNELS+1):
                    trans_status['dpinit_pending_hostlane%d' % lane] = dpinit_pending_dict.get('DPInitPending%d' % lane)
        return trans_status

    def get_transceiver_status_flags(self):
        """
        Retrieves the current flag status of the transceiver module.

        Accesses latched registers to gather information about both
        module-level and datapath-level states (including TX/RX related flags).

        Returns:
            dict: A dictionary containing boolean values for various flags, as defined in
                the TRANSCEIVER_STATUS_FLAGS table in STATE_DB.
        """
        status_flags_dict = dict()
        try:
            dp_fw_fault, module_fw_fault, module_state_changed = self.get_module_firmware_fault_state_changed()
            status_flags_dict.update({
                'datapath_firmware_fault': dp_fw_fault,
                'module_firmware_fault': module_fw_fault,
                'module_state_changed': module_state_changed
            })
        except TypeError:
            pass

        if not self.is_flat_memory():
            fault_types = {
                'tx{lane_num}fault': self.get_tx_fault(),
                'rx{lane_num}los': self.get_rx_los(),
                'tx{lane_num}los_hostlane': self.get_tx_los(),
                'tx{lane_num}cdrlol_hostlane': self.get_tx_cdr_lol(),
                'tx{lane_num}_eq_fault': self.get_tx_adaptive_eq_fail_flag(),
                'rx{lane_num}cdrlol': self.get_rx_cdr_lol()
            }

            for fault_type_template, fault_values in fault_types.items():
                for lane in range(1, self.NUM_CHANNELS + 1):
                    key = fault_type_template.format(lane_num=lane)
                    status_flags_dict[key] = fault_values[lane - 1] if fault_values else "N/A"

        return status_flags_dict

    def get_transceiver_loopback(self):
        """
        Retrieves loopback mode for this xcvr

        Returns:
            A dict containing the following keys/values :
        ========================================================================
        key                          = TRANSCEIVER_PM|ifname            ; information of loopback on port
        ; field                      = value
        media_output_loopback        = BOOLEAN                          ; media side output loopback enable
        media_input_loopback         = BOOLEAN                          ; media side input loopback enable
        host_output_loopback_lane1   = BOOLEAN                          ; host side output loopback enable lane1
        host_output_loopback_lane2   = BOOLEAN                          ; host side output loopback enable lane2
        host_output_loopback_lane3   = BOOLEAN                          ; host side output loopback enable lane3
        host_output_loopback_lane4   = BOOLEAN                          ; host side output loopback enable lane4
        host_output_loopback_lane5   = BOOLEAN                          ; host side output loopback enable lane5
        host_output_loopback_lane6   = BOOLEAN                          ; host side output loopback enable lane6
        host_output_loopback_lane7   = BOOLEAN                          ; host side output loopback enable lane7
        host_output_loopback_lane8   = BOOLEAN                          ; host side output loopback enable lane8
        host_input_loopback_lane1   = BOOLEAN                          ; host side input loopback enable lane1
        host_input_loopback_lane2   = BOOLEAN                          ; host side input loopback enable lane2
        host_input_loopback_lane3   = BOOLEAN                          ; host side input loopback enable lane3
        host_input_loopback_lane4   = BOOLEAN                          ; host side input loopback enable lane4
        host_input_loopback_lane5   = BOOLEAN                          ; host side input loopback enable lane5
        host_input_loopback_lane6   = BOOLEAN                          ; host side input loopback enable lane6
        host_input_loopback_lane7   = BOOLEAN                          ; host side input loopback enable lane7
        host_input_loopback_lane8   = BOOLEAN                          ; host side input loopback enable lane8
        ========================================================================
        """
        trans_loopback = dict()
        loopback_capability = self.get_loopback_capability()
        if loopback_capability is None:
            trans_loopback['simultaneous_host_media_loopback_supported'] = 'N/A'
            trans_loopback['per_lane_media_loopback_supported'] = 'N/A'
            trans_loopback['per_lane_host_loopback_supported'] = 'N/A'
            trans_loopback['host_side_input_loopback_supported'] = 'N/A'
            trans_loopback['host_side_output_loopback_supported'] = 'N/A'
            trans_loopback['media_side_input_loopback_supported'] = 'N/A'
            trans_loopback['media_side_output_loopback_supported'] = 'N/A'
            trans_loopback['media_output_loopback'] = 'N/A'
            trans_loopback['media_input_loopback'] = 'N/A'
            for lane in range(1, self.NUM_CHANNELS+1):
                trans_loopback['host_output_loopback_lane%d' % lane] = 'N/A'
                trans_loopback['host_input_loopback_lane%d' % lane] = 'N/A'
            return trans_loopback
        else:
            trans_loopback['simultaneous_host_media_loopback_supported'] = loopback_capability['simultaneous_host_media_loopback_supported']
            trans_loopback['per_lane_media_loopback_supported'] = loopback_capability['per_lane_media_loopback_supported']
            trans_loopback['per_lane_host_loopback_supported'] = loopback_capability['per_lane_host_loopback_supported']
            trans_loopback['host_side_input_loopback_supported'] = loopback_capability['host_side_input_loopback_supported']
            trans_loopback['host_side_output_loopback_supported'] = loopback_capability['host_side_output_loopback_supported']
            trans_loopback['media_side_input_loopback_supported'] = loopback_capability['media_side_input_loopback_supported']
            trans_loopback['media_side_output_loopback_supported'] = loopback_capability['media_side_output_loopback_supported']
        if loopback_capability['media_side_output_loopback_supported']:
            trans_loopback['media_output_loopback'] = self.get_media_output_loopback()
        else:
            trans_loopback['media_output_loopback'] = 'N/A'
        if loopback_capability['media_side_input_loopback_supported']:
            trans_loopback['media_input_loopback'] = self.get_media_input_loopback()
        else:
            trans_loopback['media_input_loopback'] = 'N/A'
        if loopback_capability['host_side_output_loopback_supported']:
            host_output_loopback_status = self.get_host_output_loopback()
            for lane in range(1, self.NUM_CHANNELS+1):
                trans_loopback['host_output_loopback_lane%d' % lane] = host_output_loopback_status[lane - 1]
        else:
            for lane in range(1, self.NUM_CHANNELS+1):
                trans_loopback['host_output_loopback_lane%d' % lane] = 'N/A'
        if loopback_capability['host_side_input_loopback_supported']:
            host_input_loopback_status = self.get_host_input_loopback()
            for lane in range(1, self.NUM_CHANNELS+1):
                trans_loopback['host_input_loopback_lane%d' % lane] = host_input_loopback_status[lane - 1]
        else:
            for lane in range(1, self.NUM_CHANNELS+1):
                trans_loopback['host_input_loopback_lane%d' % lane] = 'N/A'
        return trans_loopback

    def get_transceiver_vdm_real_value(self):
        """
        Retrieves VDM real value for this xcvr

        Returns:
            A dict containing the following keys/values :
        ========================================================================
        key                                            = TRANSCEIVER_VDM_REAL_VALUE|ifname    ; information module VDM sample on port
        ; field                                        = value
        laser_temperature_media{lane_num}              = FLOAT                  ; laser temperature value in Celsius for media input
        esnr_media_input{lane_num}                     = FLOAT                  ; eSNR value in dB for media input
        esnr_host_input{lane_num}                      = FLOAT                  ; eSNR value in dB for host input
        pam4_level_transition_media_input{lane_num}    = FLOAT                  ; PAM4 level transition parameter in dB for media input
        pam4_level_transition_host_input{lane_num}     = FLOAT                  ; PAM4 level transition parameter in dB for host input
        prefec_ber_min_media_input{lane_num}           = FLOAT                  ; Pre-FEC BER minimum value for media input
        prefec_ber_max_media_input{lane_num}           = FLOAT                  ; Pre-FEC BER maximum value for media input
        prefec_ber_avg_media_input{lane_num}           = FLOAT                  ; Pre-FEC BER average value for media input
        prefec_ber_curr_media_input{lane_num}          = FLOAT                  ; Pre-FEC BER current value for media input
        prefec_ber_min_host_input{lane_num}            = FLOAT                  ; Pre-FEC BER minimum value for host input
        prefec_ber_max_host_input{lane_num}            = FLOAT                  ; Pre-FEC BER maximum value for host input
        prefec_ber_avg_host_input{lane_num}            = FLOAT                  ; Pre-FEC BER average value for host input
        prefec_ber_curr_host_input{lane_num}           = FLOAT                  ; Pre-FEC BER current value for host input
        errored_frames_min_media_input{lane_num}       = FLOAT                  ; Errored frames minimum value for media input
        errored_frames_max_media_input{lane_num}       = FLOAT                  ; Errored frames maximum value for media input
        errored_frames_avg_media_input{lane_num}       = FLOAT                  ; Errored frames average value for media input
        errored_frames_curr_media_input{lane_num}      = FLOAT                  ; Errored frames current value for media input
        errored_frames_min_host_input{lane_num}        = FLOAT                  ; Errored frames minimum value for host input
        errored_frames_max_host_input{lane_num}        = FLOAT                  ; Errored frames maximum value for host input
        errored_frames_avg_host_input{lane_num}        = FLOAT                  ; Errored frames average value for host input
        errored_frames_curr_host_input{lane_num}       = FLOAT                  ; Errored frames current value for host input

        ;C-CMIS specific fields
        biasxi{lane_num}                               = FLOAT                  ; modulator bias xi in percentage
        biasxq{lane_num}                               = FLOAT                  ; modulator bias xq in percentage
        biasxp{lane_num}                               = FLOAT                  ; modulator bias xp in percentage
        biasyi{lane_num}                               = FLOAT                  ; modulator bias yi in percentage
        biasyq{lane_num}                               = FLOAT                  ; modulator bias yq in percentage
        biasyp{lane_num}                               = FLOAT                  ; modulator bias yq in percentage
        cdshort{lane_num}                              = FLOAT                  ; chromatic dispersion, high granularity, short link in ps/nm
        cdlong{lane_num}                               = FLOAT                  ; chromatic dispersion, high granularity, long link in ps/nm  
        dgd{lane_num}                                  = FLOAT                  ; differential group delay in ps
        sopmd{lane_num}                                = FLOAT                  ; second order polarization mode dispersion in ps^2
        soproc{lane_num}                               = FLOAT                  ; state of polarization rate of change in krad/s
        pdl{lane_num}                                  = FLOAT                  ; polarization dependent loss in db
        osnr{lane_num}                                 = FLOAT                  ; optical signal to noise ratio in db
        esnr{lane_num}                                 = FLOAT                  ; electrical signal to noise ratio in db
        cfo{lane_num}                                  = FLOAT                  ; carrier frequency offset in Hz
        txcurrpower{lane_num}                          = FLOAT                  ; tx current output power in dbm
        rxtotpower{lane_num}                           = FLOAT                  ; rx total power in  dbm
        rxsigpower{lane_num}                           = FLOAT                  ; rx signal power in dbm
        ========================================================================
        """
        vdm_real_value_dict = dict()
        vdm_raw_dict = self.get_vdm(self.vdm.VDM_REAL_VALUE)
        for vdm_observable_type, db_key_name_prefix in self._get_vdm_key_to_db_prefix_map().items():
            for lane in range(1, self.NUM_CHANNELS + 1):
                db_key_name = f"{db_key_name_prefix}{lane}"
                self._update_vdm_dict(vdm_real_value_dict, db_key_name, vdm_raw_dict, vdm_observable_type,
                                                    VdmSubtypeIndex.VDM_SUBTYPE_REAL_VALUE, lane)
        return vdm_real_value_dict

    def get_transceiver_vdm_thresholds(self):
        """
        Retrieves VDM thresholds for this xcvr

        Returns:
            A dict containing the following keys/values :
        ========================================================================
        xxx refers to HALARM/LALARM/HWARN/LWARN threshold 
        ;Defines Transceiver VDM high/low alarm/warning threshold for a port
        key                                            = TRANSCEIVER_VDM_XXX_THRESHOLD|ifname    ; information module VDM high/low alarm/warning threshold on port
        ; field                                        = value
        laser_temperature_media_xxx{lane_num}             = FLOAT          ; laser temperature high/low alarm/warning value in Celsius for media input
        esnr_media_input_xxx{lane_num}                    = FLOAT          ; eSNR high/low alarm/warning value in dB for media input
        esnr_host_input_xxx{lane_num}                     = FLOAT          ; eSNR high/low alarm/warning value in dB for host input
        pam4_level_transition_media_input_xxx{lane_num}   = FLOAT          ; PAM4 level transition high/low alarm/warning value in dB for media input
        pam4_level_transition_host_input_xxx{lane_num}    = FLOAT          ; PAM4 level transition high/low alarm/warning value in dB for host input
        prefec_ber_min_media_input_xxx{lane_num}          = FLOAT          ; Pre-FEC BER minimum high/low alarm/warning value for media input
        prefec_ber_max_media_input_xxx{lane_num}          = FLOAT          ; Pre-FEC BER maximum high/low alarm/warning value for media input
        prefec_ber_avg_media_input_xxx{lane_num}          = FLOAT          ; Pre-FEC BER average high/low alarm/warning value for media input
        prefec_ber_curr_media_input_xxx{lane_num}         = FLOAT          ; Pre-FEC BER current high/low alarm/warning value for media input
        prefec_ber_min_host_input_xxx{lane_num}           = FLOAT          ; Pre-FEC BER minimum high/low alarm/warning value for host input
        prefec_ber_max_host_input_xxx{lane_num}           = FLOAT          ; Pre-FEC BER maximum high/low alarm/warning value for host input
        prefec_ber_avg_host_input_xxx{lane_num}           = FLOAT          ; Pre-FEC BER average high/low alarm/warning value for host input
        prefec_ber_curr_host_input_xxx{lane_num}          = FLOAT          ; Pre-FEC BER current high/low alarm/warning value for host input
        errored_frames_min_media_input_xxx{lane_num}      = FLOAT          ; Errored frames minimum high/low alarm/warning value for media input
        errored_frames_max_media_input_xxx{lane_num}      = FLOAT          ; Errored frames maximum high/low alarm/warning value for media input
        errored_frames_avg_media_input_xxx{lane_num}      = FLOAT          ; Errored frames average high/low alarm/warning value for media input
        errored_frames_curr_media_input_xxx{lane_num}     = FLOAT          ; Errored frames current high/low alarm/warning value for media input
        errored_frames_min_host_input_xxx{lane_num}       = FLOAT          ; Errored frames minimum high/low alarm/warning value for host input
        errored_frames_max_host_input_xxx{lane_num}       = FLOAT          ; Errored frames maximum high/low alarm/warning value for host input
        errored_frames_avg_host_input_xxx{lane_num}       = FLOAT          ; Errored frames average high/low alarm/warning value for host input
        errored_frames_curr_host_input_xxx{lane_num}      = FLOAT          ; Errored frames current high/low alarm/warning value for host input

        ;C-CMIS specific fields
        biasxi_xxx{lane_num}                             = FLOAT         ; modulator bias xi in percentage (high/low alarm/warning)
        biasxq_xxx{lane_num}                             = FLOAT         ; modulator bias xq in percentage (high/low alarm/warning)
        biasxp_xxx{lane_num}                             = FLOAT         ; modulator bias xp in percentage (high/low alarm/warning)
        biasyi_xxx{lane_num}                             = FLOAT         ; modulator bias yi in percentage (high/low alarm/warning)
        biasyq_xxx{lane_num}                             = FLOAT         ; modulator bias yq in percentage (high/low alarm/warning)
        biasyp_xxx{lane_num}                             = FLOAT         ; modulator bias yq in percentage (high/low alarm/warning)
        cdshort_xxx{lane_num}                            = FLOAT         ; chromatic dispersion, high granularity, short link in ps/nm (high/low alarm/warning)
        cdlong_xxx{lane_num}                             = FLOAT         ; chromatic dispersion, high granularity, long link in ps/nm (high/low alarm/warning)
        dgd_xxx{lane_num}                                = FLOAT         ; differential group delay in ps (high/low alarm/warning)
        sopmd_xxx{lane_num}                              = FLOAT         ; second order polarization mode dispersion in ps^2 (high/low alarm/warning)
        soproc_xxx{lane_num}                             = FLOAT         ; state of polarization rate of change in krad/s (high/low alarm/warning)
        pdl_xxx{lane_num}                                = FLOAT         ; polarization dependent loss in db (high/low alarm/warning)
        osnr_xxx{lane_num}                               = FLOAT         ; optical signal to noise ratio in db (high/low alarm/warning)
        esnr_xxx{lane_num}                               = FLOAT         ; electrical signal to noise ratio in db (high/low alarm/warning)
        cfo_xxx{lane_num}                                = FLOAT         ; carrier frequency offset in Hz (high/low alarm/warning)
        txcurrpower_xxx{lane_num}                        = FLOAT         ; tx current output power in dbm (high/low alarm/warning)
        rxtotpower_xxx{lane_num}                         = FLOAT         ; rx total power in  dbm (high/low alarm/warning)
        rxsigpower_xxx{lane_num}                         = FLOAT         ; rx signal power in dbm (high/low alarm/warning)        ========================================================================
        """
        vdm_thresholds_dict = dict()
        vdm_raw_dict = self.get_vdm(self.vdm.VDM_THRESHOLD)
        for vdm_observable_type, db_key_name_prefix in self._get_vdm_key_to_db_prefix_map().items():
            for lane in range(1, self.NUM_CHANNELS + 1):
                for vdm_threshold_type in range(VdmSubtypeIndex.VDM_SUBTYPE_HALARM_THRESHOLD.value, VdmSubtypeIndex.VDM_SUBTYPE_LWARN_THRESHOLD.value + 1):
                    vdm_threshold_enum = VdmSubtypeIndex(vdm_threshold_type)
                    threshold_type_str = THRESHOLD_TYPE_STR_MAP.get(vdm_threshold_enum)
                    if threshold_type_str:
                        db_key_name = f"{db_key_name_prefix}_{threshold_type_str}{lane}"
                        self._update_vdm_dict(vdm_thresholds_dict, db_key_name, vdm_raw_dict,
                                                            vdm_observable_type, vdm_threshold_enum, lane)

        return vdm_thresholds_dict

    def get_transceiver_vdm_flags(self):
        """
        Retrieves VDM flags for this xcvr

        Returns:
            A dict containing the following keys/values :
        ========================================================================
        xxx refers to HALARM/LALARM/HWARN/LWARN
        ;Defines Transceiver VDM high/low alarm/warning flag for a port
        key                                            = TRANSCEIVER_VDM_XXX_FLAG|ifname    ; information module VDM high/low alarm/warning flag on port
        ; field                                        = value
        laser_temperature_media_xxx{lane_num}             = FLOAT          ; laser temperature high/low alarm/warning flag in Celsius for media input
        esnr_media_input_xxx{lane_num}                    = FLOAT          ; eSNR high/low alarm/warning flag in dB for media input
        esnr_host_input_xxx{lane_num}                     = FLOAT          ; eSNR high/low alarm/warning flag in dB for host input
        pam4_level_transition_media_input_xxx{lane_num}   = FLOAT          ; PAM4 level transition high/low alarm/warning flag in dB for media input
        pam4_level_transition_host_input_xxx{lane_num}    = FLOAT          ; PAM4 level transition high/low alarm/warning flag in dB for host input
        prefec_ber_min_media_input_xxx{lane_num}          = FLOAT          ; Pre-FEC BER minimum high/low alarm/warning flag for media input
        prefec_ber_max_media_input_xxx{lane_num}          = FLOAT          ; Pre-FEC BER maximum high/low alarm/warning flag for media input
        prefec_ber_avg_media_input_xxx{lane_num}          = FLOAT          ; Pre-FEC BER average high/low alarm/warning flag for media input
        prefec_ber_curr_media_input_xxx{lane_num}         = FLOAT          ; Pre-FEC BER current high/low alarm/warning flag for media input
        prefec_ber_min_host_input_xxx{lane_num}           = FLOAT          ; Pre-FEC BER minimum high/low alarm/warning flag for host input
        prefec_ber_max_host_input_xxx{lane_num}           = FLOAT          ; Pre-FEC BER maximum high/low alarm/warning flag for host input
        prefec_ber_avg_host_input_xxx{lane_num}           = FLOAT          ; Pre-FEC BER average high/low alarm/warning flag for host input
        prefec_ber_curr_host_input_xxx{lane_num}          = FLOAT          ; Pre-FEC BER current high/low alarm/warning flag for host input
        errored_frames_min_media_input_xxx{lane_num}      = FLOAT          ; Errored frames minimum high/low alarm/warning flag for media input
        errored_frames_max_media_input_xxx{lane_num}      = FLOAT          ; Errored frames maximum high/low alarm/warning flag for media input
        errored_frames_avg_media_input_xxx{lane_num}      = FLOAT          ; Errored frames average high/low alarm/warning flag for media input
        errored_frames_curr_media_input_xxx{lane_num}     = FLOAT          ; Errored frames current high/low alarm/warning flag for media input
        errored_frames_min_host_input_xxx{lane_num}       = FLOAT          ; Errored frames minimum high/low alarm/warning flag for host input
        errored_frames_max_host_input_xxx{lane_num}       = FLOAT          ; Errored frames maximum high/low alarm/warning flag for host input
        errored_frames_avg_host_input_xxx{lane_num}       = FLOAT          ; Errored frames average high/low alarm/warning flag for host input
        errored_frames_curr_host_input_xxx{lane_num}      = FLOAT          ; Errored frames current high/low alarm/warning flag for host input

        ;C-CMIS specific fields
        biasxi_xxx{lane_num}                             = FLOAT         ; modulator bias xi in percentage (high/low alarm/warning flag)
        biasxq_xxx{lane_num}                             = FLOAT         ; modulator bias xq in percentage (high/low alarm/warning flag)
        biasxp_xxx{lane_num}                             = FLOAT         ; modulator bias xp in percentage (high/low alarm/warning flag)
        biasyi_xxx{lane_num}                             = FLOAT         ; modulator bias yi in percentage (high/low alarm/warning flag)
        biasyq_xxx{lane_num}                             = FLOAT         ; modulator bias yq in percentage (high/low alarm/warning flag)
        biasyp_xxx{lane_num}                             = FLOAT         ; modulator bias yq in percentage (high/low alarm/warning flag)
        cdshort_xxx{lane_num}                            = FLOAT         ; chromatic dispersion, high granularity, short link in ps/nm (high/low alarm/warning flag)
        cdlong_xxx{lane_num}                             = FLOAT         ; chromatic dispersion, high granularity, long link in ps/nm (high/low alarm/warning flag)
        dgd_xxx{lane_num}                                = FLOAT         ; differential group delay in ps (high/low alarm/warning flag)
        sopmd_xxx{lane_num}                              = FLOAT         ; second order polarization mode dispersion in ps^2 (high/low alarm/warning flag)
        soproc_xxx{lane_num}                             = FLOAT         ; state of polarization rate of change in krad/s (high/low alarm/warning flag)
        pdl_xxx{lane_num}                                = FLOAT         ; polarization dependent loss in db (high/low alarm/warning flag)
        osnr_xxx{lane_num}                               = FLOAT         ; optical signal to noise ratio in db (high/low alarm/warning flag)
        esnr_xxx{lane_num}                               = FLOAT         ; electrical signal to noise ratio in db (high/low alarm/warning flag)
        cfo_xxx{lane_num}                                = FLOAT         ; carrier frequency offset in Hz (high/low alarm/warning flag)
        txcurrpower_xxx{lane_num}                        = FLOAT         ; tx current output power in dbm (high/low alarm/warning flag)
        rxtotpower_xxx{lane_num}                         = FLOAT         ; rx total power in  dbm (high/low alarm/warning flag)
        rxsigpower_xxx{lane_num}                         = FLOAT         ; rx signal power in dbm (high/low alarm/warning flag)
        """
        vdm_flags_dict = dict()
        vdm_raw_dict = self.get_vdm(self.vdm.VDM_FLAG)
        for vdm_observable_type, db_key_name_prefix in self._get_vdm_key_to_db_prefix_map().items():
            for lane in range(1, self.NUM_CHANNELS + 1):
                for vdm_flag_type in range(VdmSubtypeIndex.VDM_SUBTYPE_HALARM_FLAG.value, VdmSubtypeIndex.VDM_SUBTYPE_LWARN_FLAG.value + 1):
                    vdm_flag_enum = VdmSubtypeIndex(vdm_flag_type)
                    flag_type_str = FLAG_TYPE_STR_MAP.get(vdm_flag_enum)
                    if flag_type_str:
                        db_key_name = f"{db_key_name_prefix}_{flag_type_str}{lane}"
                        self._update_vdm_dict(vdm_flags_dict, db_key_name, vdm_raw_dict,
                                                            vdm_observable_type, vdm_flag_enum, lane)

        return vdm_flags_dict

    def set_datapath_init(self, channel):
        """
        Put the CMIS datapath into the initialized state

        Args:
            channel:
                Integer, a bitmask of the lanes on the host side
                e.g. 0x5 for lane 0 and lane 2.

        Returns:
            Boolean, true if success otherwise false
        """
        cmis_major = self.xcvr_eeprom.read(consts.CMIS_MAJOR_REVISION)
        data = self.xcvr_eeprom.read(consts.DATAPATH_DEINIT_FIELD)
        for lane in range(self.NUM_CHANNELS):
            if ((1 << lane) & channel) == 0:
                continue
            if cmis_major >= 4: # CMIS v4 onwards
                data &= ~(1 << lane)
            else:               # CMIS v3
                data |= (1 << lane)
        self.xcvr_eeprom.write(consts.DATAPATH_DEINIT_FIELD, data)

    def set_datapath_deinit(self, channel):
        """
        Put the CMIS datapath into the de-initialized state

        Args:
            channel:
                Integer, a bitmask of the lanes on the host side
                e.g. 0x5 for lane 0 and lane 2.

        Returns:
            Boolean, true if success otherwise false
        """
        cmis_major = self.xcvr_eeprom.read(consts.CMIS_MAJOR_REVISION)
        data = self.xcvr_eeprom.read(consts.DATAPATH_DEINIT_FIELD)
        for lane in range(self.NUM_CHANNELS):
            if ((1 << lane) & channel) == 0:
                continue
            if cmis_major >= 4: # CMIS v4 onwards
                data |= (1 << lane)
            else:               # CMIS v3
                data &= ~(1 << lane)
        self.xcvr_eeprom.write(consts.DATAPATH_DEINIT_FIELD, data)

    def get_datapath_deinit(self):
        datapath_deinit = self.xcvr_eeprom.read(consts.DATAPATH_DEINIT_FIELD)
        if datapath_deinit is None:
            return None
        return [bool(datapath_deinit & (1 << lane)) for lane in range(self.NUM_CHANNELS)]

    @read_only_cached_api_return
    def get_application_advertisement(self):
        """
        Get the application advertisement of the CMIS transceiver

        Returns:
            Dictionary, the application advertisement
        """
        map = {
            Sff8024.MODULE_MEDIA_TYPE[1]: consts.MODULE_MEDIA_INTERFACE_850NM,
            Sff8024.MODULE_MEDIA_TYPE[2]: consts.MODULE_MEDIA_INTERFACE_SM,
            Sff8024.MODULE_MEDIA_TYPE[3]: consts.MODULE_MEDIA_INTERFACE_PASSIVE_COPPER,
            Sff8024.MODULE_MEDIA_TYPE[4]: consts.MODULE_MEDIA_INTERFACE_ACTIVE_CABLE,
            Sff8024.MODULE_MEDIA_TYPE[5]: consts.MODULE_MEDIA_INTERFACE_BASE_T
        }

        ret = {}
        # Read the application advertisment in lower memory
        dic = self.xcvr_eeprom.read(consts.APPLS_ADVT_FIELD)
        if not dic:
            return ret

        if not self.is_flat_memory():
            # Read the application advertisement in page01
            try:
                dic.update(self.xcvr_eeprom.read(consts.APPLS_ADVT_FIELD_PAGE01))
            except (TypeError, AttributeError) as e:
                logger.error('Failed to read APPLS_ADVT_FIELD_PAGE01: ' + str(e))
                return ret

        media_type = self.xcvr_eeprom.read(consts.MEDIA_TYPE_FIELD)
        prefix = map.get(media_type)
        for app in range(1, 16):
            buf = {}

            key = "{}_{}".format(consts.HOST_ELECTRICAL_INTERFACE, app)
            val = dic.get(key)
            if val in [None, 'Unknown', 'Undefined']:
                continue
            buf['host_electrical_interface_id'] = val

            if prefix is None:
                continue
            key = "{}_{}".format(prefix, app)
            val = dic.get(key)
            if val in [None, 'Unknown']:
                continue
            buf['module_media_interface_id'] = val

            key = "{}_{}".format(consts.MEDIA_LANE_COUNT, app)
            val = dic.get(key)
            if val is None:
                continue
            buf['media_lane_count'] = val

            key = "{}_{}".format(consts.HOST_LANE_COUNT, app)
            val = dic.get(key)
            if val is None:
                continue
            buf['host_lane_count'] = val

            key = "{}_{}".format(consts.HOST_LANE_ASSIGNMENT_OPTION, app)
            val = dic.get(key)
            if val is None:
                continue
            buf['host_lane_assignment_options'] = val

            key = "{}_{}".format(consts.MEDIA_LANE_ASSIGNMENT_OPTION, app)
            val = dic.get(key)
            if val is not None:
                buf['media_lane_assignment_options'] = val

            ret[app] = buf
        return ret

    def get_application(self, lane):
        """
        Get the CMIS selected application code of a host lane

        Args:
            lane:
                Integer, the zero-based lane id on the host side

        Returns:
            Integer, the transceiver-specific application code
        """
        appl = 0
        if lane in range(self.NUM_CHANNELS) and not self.is_flat_memory():
            name = "{}_{}_{}".format(consts.STAGED_CTRL_APSEL_FIELD, 0, lane + 1)
            appl = self.xcvr_eeprom.read(name) >> 4

        return (appl & 0xf)

    def set_application(self, channel, appl_code, ec=0):
        """
        Update the selected application code to the specified lanes on the host side

        Args:
            channel:
                Integer, a bitmask of the lanes on the host side
                e.g. 0x5 for lane 0 and lane 2.
            appl_code:
                Integer, the desired application code

        Returns:
            Boolean, true if success otherwise false
        """
        # Update the application selection
        lane_first = -1
        for lane in range(self.NUM_CHANNELS):
            if ((1 << lane) & channel) == 0:
                continue
            if lane_first < 0:
                lane_first = lane
            addr = "{}_{}_{}".format(consts.STAGED_CTRL_APSEL_FIELD, 0, lane + 1)
            data = (appl_code << 4) | (lane_first << 1)
            #set EC bit
            data|= ec
            self.xcvr_eeprom.write(addr, data)

    def scs_apply_datapath_init(self, channel):
        '''
        This function applies DataPathInit
        '''
        return self.xcvr_eeprom.write("%s_%d" % (consts.STAGED_CTRL_APPLY_DPINIT_FIELD, 0), channel)

    def decommission_all_datapaths(self):
        '''
            Return True if all datapaths are successfully de-commissioned, False otherwise
        '''
        # De-init all datpaths
        self.set_datapath_deinit((1 << self.NUM_CHANNELS) - 1)
        # Decommision all lanes by apply AppSel=0
        self.set_application(((1 << self.NUM_CHANNELS) - 1), 0, 0)
        # Start with AppSel=0 i.e undo any default AppSel
        self.scs_apply_datapath_init((1 << self.NUM_CHANNELS) - 1)

        dp_state = self.get_datapath_state()
        config_state = self.get_config_datapath_hostlane_status()

        for lane in range(self.NUM_CHANNELS):
            name = "DP{}State".format(lane + 1)
            if dp_state[name] != 'DataPathDeactivated':
                return False
            
            name = "ConfigStatusLane{}".format(lane + 1)
            if config_state[name] != 'ConfigSuccess':
                return False

        return True

    def get_rx_output_amp_max_val(self):
        '''
        This function returns the supported RX output amp val
        '''
        rx_amp_max_val = self.xcvr_eeprom.read(consts.RX_OUTPUT_LEVEL_SUPPORT)
        if rx_amp_max_val is None:
            return None
        return rx_amp_max_val

    def get_rx_output_eq_pre_max_val(self):
        '''
        This function returns the supported RX output eq pre cursor val
        '''
        rx_pre_max_val = self.xcvr_eeprom.read(consts.RX_OUTPUT_EQ_PRE_CURSOR_MAX)
        if rx_pre_max_val is None:
            return None
        return rx_pre_max_val

    def get_rx_output_eq_post_max_val(self):
        '''
        This function returns the supported RX output eq post cursor val
        '''
        rx_post_max_val = self.xcvr_eeprom.read(consts.RX_OUTPUT_EQ_POST_CURSOR_MAX)
        if rx_post_max_val is None:
            return None
        return rx_post_max_val

    def get_tx_input_eq_max_val(self):
        '''
        This function returns the supported TX input eq val
        '''
        tx_input_max_val = self.xcvr_eeprom.read(consts.TX_INPUT_EQ_MAX)
        if tx_input_max_val is None:
            return None
        return tx_input_max_val

    def get_tx_adaptive_eq_fail_flag_supported(self):
        """
        Returns whether the TX Adaptive Input EQ Fail Flag field is supported.
        """
        return not self.is_flat_memory() and self.xcvr_eeprom.read(consts.TX_ADAPTIVE_INPUT_EQ_FAIL_FLAG_SUPPORTED)

    def get_tx_adaptive_eq_fail_flag(self):
        """
        Returns the TX Adaptive Input EQ Fail Flag field on all lanes.
        """
        tx_adaptive_eq_fail_flag_supported = self.get_tx_adaptive_eq_fail_flag_supported()
        if tx_adaptive_eq_fail_flag_supported is None:
            return None
        if not tx_adaptive_eq_fail_flag_supported:
            return ["N/A" for _ in range(self.NUM_CHANNELS)]
        tx_adaptive_eq_fail_flag_val = self.xcvr_eeprom.read(consts.TX_ADAPTIVE_INPUT_EQ_FAIL_FLAG)
        if tx_adaptive_eq_fail_flag_val is None:
            return None
        keys = sorted(tx_adaptive_eq_fail_flag_val.keys())
        tx_adaptive_eq_fail_flag_val_final = []
        for key in keys:
            tx_adaptive_eq_fail_flag_val_final.append(bool(tx_adaptive_eq_fail_flag_val[key]))
        return tx_adaptive_eq_fail_flag_val_final

    def get_tx_cdr_supported(self):
        '''
        This function returns the supported TX CDR field
        '''
        tx_cdr_support = self.xcvr_eeprom.read(consts.TX_CDR_SUPPORT_FIELD)
        if not tx_cdr_support or tx_cdr_support is None:
            return False
        return tx_cdr_support

    def get_rx_cdr_supported(self):
        '''
        This function returns the supported RX CDR field
        '''
        rx_cdr_support = self.xcvr_eeprom.read(consts.RX_CDR_SUPPORT_FIELD)
        if not rx_cdr_support or rx_cdr_support is None:
            return False
        return rx_cdr_support

    def get_tx_input_eq_fixed_supported(self):
        '''
        This function returns the supported TX input eq field
        '''
        tx_fixed_support = self.xcvr_eeprom.read(consts.TX_INPUT_EQ_FIXED_MANUAL_CTRL_SUPPORT_FIELD)
        if not tx_fixed_support or tx_fixed_support is None:
            return False
        return tx_fixed_support

    def get_tx_input_adaptive_eq_supported(self):
        '''
        This function returns the supported TX input adaptive eq field
        '''
        tx_adaptive_support = self.xcvr_eeprom.read(consts.TX_INPUT_ADAPTIVE_EQ_SUPPORT_FIELD)
        if not tx_adaptive_support or tx_adaptive_support is None:
            return False
        return tx_adaptive_support

    def get_tx_input_recall_buf1_supported(self):
        '''
        This function returns the supported TX input recall buf1 field
        '''
        tx_recall_buf1_support = self.xcvr_eeprom.read(consts.TX_INPUT_EQ_RECALL_BUF1_SUPPORT_FIELD)
        if not tx_recall_buf1_support or tx_recall_buf1_support is None:
            return False
        return tx_recall_buf1_support

    def get_tx_input_recall_buf2_supported(self):
        '''
        This function returns the supported TX input recall buf2 field
        '''
        tx_recall_buf2_support = self.xcvr_eeprom.read(consts.TX_INPUT_EQ_RECALL_BUF2_SUPPORT_FIELD)
        if not tx_recall_buf2_support or tx_recall_buf2_support is None:
            return False
        return tx_recall_buf2_support

    def get_rx_ouput_amp_ctrl_supported(self):
        '''
        This function returns the supported RX output amp control field
        '''
        rx_amp_support = self.xcvr_eeprom.read(consts.RX_OUTPUT_AMP_CTRL_SUPPORT_FIELD)
        if not rx_amp_support or rx_amp_support is None:
            return False
        return rx_amp_support

    def get_rx_output_eq_pre_ctrl_supported(self):
        '''
        This function returns the supported RX output eq pre control field
        '''
        rx_pre_support = self.xcvr_eeprom.read(consts.RX_OUTPUT_EQ_PRE_CTRL_SUPPORT_FIELD)
        if not rx_pre_support or rx_pre_support is None:
            return False
        return rx_pre_support

    def get_rx_output_eq_post_ctrl_supported(self):
        '''
        This function returns the supported RX output eq post control field
        '''
        rx_post_support = self.xcvr_eeprom.read(consts.RX_OUTPUT_EQ_POST_CTRL_SUPPORT_FIELD)
        if not rx_post_support or rx_post_support is None:
            return False
        return rx_post_support

    def scs_lane_write(self, si_param, host_lanes_mask, si_settings_dict):
        '''
        This function sets each lane val based on SI param
        '''
        for lane in range(self.NUM_CHANNELS):
            if ((1 << lane) & host_lanes_mask) == 0:
                continue
            lane = lane+1
            si_param_lane = "{}{}".format(si_param, lane)
            si_param_lane_val = si_settings_dict[si_param_lane]
            if si_param_lane_val is None:
                return False
            if not self.xcvr_eeprom.write(si_param_lane, si_param_lane_val):
                return False
        return True

    def stage_output_eq_pre_cursor_target_rx(self, host_lanes_mask, si_settings_dict):
        '''
        This function applies RX output eq pre cursor settings
        '''
        rx_pre_max_val = self.get_rx_output_eq_pre_max_val()
        if rx_pre_max_val is None:
            return False
        for lane in range(self.NUM_CHANNELS):
            if ((1 << lane) & host_lanes_mask) == 0:
                continue
            lane = lane+1
            si_param_lane = "{}{}".format(consts.OUTPUT_EQ_PRE_CURSOR_TARGET_RX, lane)
            si_param_lane_val = si_settings_dict[si_param_lane]
            if si_param_lane_val is None or si_param_lane_val > rx_pre_max_val:
                return False
            if not self.xcvr_eeprom.write(si_param_lane, si_param_lane_val):
                return False
        return True

    def stage_output_eq_post_cursor_target_rx(self, host_lanes_mask, si_settings_dict):
        '''
        This function applies RX output eq post cursor settings
        '''
        rx_post_max_val = self.get_rx_output_eq_post_max_val()
        if rx_post_max_val is None:
            return False
        for lane in range(self.NUM_CHANNELS):
            if ((1 << lane) & host_lanes_mask) == 0:
                continue
            lane = lane+1
            si_param_lane = "{}{}".format(consts.OUTPUT_EQ_POST_CURSOR_TARGET_RX, lane)
            si_param_lane_val = si_settings_dict[si_param_lane]
            if si_param_lane_val is None or si_param_lane_val > rx_post_max_val:
                return False
            if not self.xcvr_eeprom.write(si_param_lane, si_param_lane_val):
                return False
        return True

    def stage_output_amp_target_rx(self, host_lanes_mask, si_settings_dict):
        '''
        This function applies RX output amp settings
        '''
        rx_amp_max_val = self.get_rx_output_amp_max_val()
        if rx_amp_max_val is None:
            return False
        for lane in range(self.NUM_CHANNELS):
            if ((1 << lane) & host_lanes_mask) == 0:
                continue
            lane = lane+1
            si_param_lane = "{}{}".format(consts.OUTPUT_AMPLITUDE_TARGET_RX, lane)
            si_param_lane_val = si_settings_dict[si_param_lane]
            if si_param_lane_val is None or si_param_lane_val > rx_amp_max_val:
                return False
            if not self.xcvr_eeprom.write(si_param_lane, si_param_lane_val):
                return False
        return True

    def stage_fixed_input_target_tx(self, host_lanes_mask, si_settings_dict):
        '''
        This function applies fixed TX input si settings
        '''
        tx_fixed_input = self.get_tx_input_eq_max_val()
        if tx_fixed_input is None:
            return False
        for lane in range(self.NUM_CHANNELS):
            if ((1 << lane) & host_lanes_mask) == 0:
                continue
            lane = lane+1
            si_param_lane = "{}{}".format(consts.FIXED_INPUT_EQ_TARGET_TX, lane)
            si_param_lane_val = si_settings_dict[si_param_lane]
            if si_param_lane_val is None or si_param_lane_val > tx_fixed_input:
                return False
            if not self.xcvr_eeprom.write(si_param_lane, si_param_lane_val):
                return False
        return True

    def stage_adaptive_input_eq_recall_tx(self, host_lanes_mask, si_settings_dict):
        '''
        This function applies adaptive TX input recall si settings.
        '''
        if si_settings_dict is None:
            return False
        return self.scs_lane_write(consts.ADAPTIVE_INPUT_EQ_RECALLED_TX, host_lanes_mask, si_settings_dict)

    def stage_adaptive_input_eq_enable_tx(self, host_lanes_mask, si_settings_dict):
        '''
        This function applies adaptive TX input enable si settings
        '''
        if si_settings_dict is None:
            return False
        return self.scs_lane_write(consts.ADAPTIVE_INPUT_EQ_ENABLE_TX, host_lanes_mask, si_settings_dict)

    def stage_cdr_tx(self, host_lanes_mask, si_settings_dict):
        '''
        This function applies TX CDR si settings
        '''
        if si_settings_dict is None:
            return False
        return self.scs_lane_write(consts.CDR_ENABLE_TX, host_lanes_mask, si_settings_dict)

    def stage_cdr_rx(self, host_lanes_mask, si_settings_dict):
        '''
        This function applies RX CDR si settings
        '''
        if si_settings_dict is None:
            return False
        return self.scs_lane_write(consts.CDR_ENABLE_RX, host_lanes_mask, si_settings_dict)

    def stage_rx_si_settings(self, host_lanes_mask, si_settings_dict):
        for si_param in si_settings_dict:
            if si_param == consts.OUTPUT_EQ_PRE_CURSOR_TARGET_RX:
                if self.get_rx_output_eq_pre_ctrl_supported():
                    if not self.stage_output_eq_pre_cursor_target_rx(host_lanes_mask, si_settings_dict[si_param]):
                        return False
            elif si_param == consts.OUTPUT_EQ_POST_CURSOR_TARGET_RX:
                if self.get_rx_output_eq_post_ctrl_supported():
                    if not self.stage_output_eq_post_cursor_target_rx(host_lanes_mask, si_settings_dict[si_param]):
                        return False
            elif si_param == consts.OUTPUT_AMPLITUDE_TARGET_RX:
                if self.get_rx_ouput_amp_ctrl_supported():
                    if not self.stage_output_amp_target_rx(host_lanes_mask, si_settings_dict[si_param]):
                        return False
            elif si_param == consts.CDR_ENABLE_RX:
                if self.get_rx_cdr_supported():
                    if not self.stage_cdr_rx(host_lanes_mask, si_settings_dict[si_param]):
                        return False
            else:
                return False

        return True

    def stage_tx_si_settings(self, host_lanes_mask, si_settings_dict):
        for si_param in si_settings_dict:
            if si_param == consts.FIXED_INPUT_EQ_TARGET_TX:
                if self.get_tx_input_eq_fixed_supported():
                    if not self.stage_fixed_input_target_tx(host_lanes_mask, si_settings_dict[si_param]):
                        return False
            elif si_param == consts.ADAPTIVE_INPUT_EQ_RECALLED_TX:
                if self.get_tx_input_recall_buf1_supported() or self.get_tx_input_recall_buf2_supported():
                    if not self.stage_adaptive_input_eq_recall_tx(host_lanes_mask, si_settings_dict[si_param]):
                        return False
            elif si_param == consts.ADAPTIVE_INPUT_EQ_ENABLE_TX:
                if self.get_tx_input_adaptive_eq_supported():
                    if not self.stage_adaptive_input_eq_enable_tx(host_lanes_mask, si_settings_dict[si_param]):
                        return False
            elif si_param == consts.CDR_ENABLE_TX:
                if self.get_tx_cdr_supported():
                    if not self.stage_cdr_tx(host_lanes_mask, si_settings_dict[si_param]):
                        return False
            else:
                return False

        return True

    def stage_custom_si_settings(self, host_lanes_mask, optics_si_dict):
        # Create TX/RX specific si_dict
        rx_si_settings = {}
        tx_si_settings = {}
        for si_param in optics_si_dict:
            if si_param.endswith("Tx"):
                tx_si_settings[si_param] = optics_si_dict[si_param]
            elif si_param.endswith("Rx"):
                rx_si_settings[si_param] = optics_si_dict[si_param]

        # stage RX si settings
        if not self.stage_rx_si_settings(host_lanes_mask, rx_si_settings):
            return False

        # stage TX si settings
        if not self.stage_tx_si_settings(host_lanes_mask, tx_si_settings):
            return False

        return True

    def get_error_description(self):
        if not self.is_flat_memory():
            dp_state = self.get_datapath_state()
            conf_state = self.get_config_datapath_hostlane_status()
            for lane in range(self.NUM_CHANNELS):
                name = "{}_{}_{}".format(consts.STAGED_CTRL_APSEL_FIELD, 0, lane + 1)
                appl = self.xcvr_eeprom.read(name)
                if (appl is None) or ((appl >> 4) == 0):
                    continue

                name = "DP{}State".format(lane + 1)
                if dp_state[name] != CmisCodes.DATAPATH_STATE[4]:
                    return dp_state[name]

                name = "ConfigStatusLane{}".format(lane + 1)
                if conf_state[name] != CmisCodes.CONFIG_STATUS[1]:
                    return conf_state[name]

        state = self.get_module_state()
        if state != CmisCodes.MODULE_STATE[3]:
            return state

        return 'OK'

    # TODO: other XcvrApi methods

