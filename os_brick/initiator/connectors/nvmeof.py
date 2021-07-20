#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import errno
import glob
import json
import os.path
import re
import time

from oslo_concurrency import lockutils
from oslo_concurrency import processutils as putils
from oslo_log import log as logging

from os_brick import exception
from os_brick.i18n import _
from os_brick.initiator.connectors import base
from os_brick.privileged import nvmeof as priv_nvme
from os_brick.privileged import rootwrap as priv_rootwrap
from os_brick import utils

DEV_SEARCH_PATH = '/dev/'

DEVICE_SCAN_ATTEMPTS_DEFAULT = 5

synchronized = lockutils.synchronized_with_prefix('os-brick-')

LOG = logging.getLogger(__name__)


class NVMeOFConnector(base.BaseLinuxConnector):
    """Connector class to attach/detach NVMe-oF volumes."""

    def __init__(self, root_helper, driver=None, use_multipath=False,
                 device_scan_attempts=DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 *args, **kwargs):
        super(NVMeOFConnector, self).__init__(
            root_helper,
            driver=driver,
            device_scan_attempts=device_scan_attempts,
            *args, **kwargs)
        self.use_multipath = use_multipath

    @staticmethod
    def get_search_path():
        return DEV_SEARCH_PATH

    def get_volume_paths(self, connection_properties):
        device_path = connection_properties.get('device_path')
        if device_path:
            return [device_path]
        volume_replicas = connection_properties.get('volume_replicas')
        if not volume_replicas:  # compatibility
            return []

        try:
            if volume_replicas and len(volume_replicas) > 1:
                return ['/dev/md/' + connection_properties.get('alias')]
            if volume_replicas and len(volume_replicas) == 1:
                return [NVMeOFConnector.get_nvme_device_path(
                    volume_replicas[0]['target_nqn'],
                    volume_replicas[0]['vol_uuid'])]
            else:
                return [NVMeOFConnector.get_nvme_device_path(
                    connection_properties.get('target_nqn'),
                    connection_properties.get('vol_uuid'))]
        except exception.VolumeDeviceNotFound:
            return []

    @classmethod
    def nvme_present(cls):
        try:
            priv_rootwrap.custom_execute('nvme', 'version')
            return True
        except Exception as exc:
            if isinstance(exc, OSError) and exc.errno == errno.ENOENT:
                LOG.debug('nvme not present on system')
            else:
                LOG.warning('Unknown error when checking presence of nvme: %s',
                            exc)
        return False

    @classmethod
    def get_connector_properties(cls, root_helper, *args, **kwargs):
        """The NVMe-oF connector properties (initiator uuid and nqn.)"""
        execute = kwargs.get('execute') or priv_rootwrap.execute
        nvmf = NVMeOFConnector(root_helper=root_helper, execute=execute)
        ret = {}

        nqn = None
        uuid = priv_nvme.get_host_uuid()
        suuid = nvmf._get_system_uuid()
        if cls.nvme_present():
            nqn = nvmf._get_host_nqn()
        if uuid:
            ret['uuid'] = uuid
        if suuid:
            ret['system uuid'] = suuid  # compatibility
        if nqn:
            ret['nqn'] = nqn
        return ret

    def _get_host_nqn(self):
        try:
            with open('/etc/nvme/hostnqn', 'r') as f:
                host_nqn = f.read().strip()
        except IOError:
            host_nqn = priv_nvme.create_hostnqn()
        except Exception:
            host_nqn = None
        return host_nqn

    def _get_system_uuid(self):
        # RSD requires system_uuid to let Cinder RSD Driver identify
        # Nova node for later RSD volume attachment.
        try:
            out, err = self._execute('cat', '/sys/class/dmi/id/product_uuid',
                                     root_helper=self._root_helper,
                                     run_as_root=True)
        except putils.ProcessExecutionError:
            try:
                out, err = self._execute('dmidecode', '-ssystem-uuid',
                                         root_helper=self._root_helper,
                                         run_as_root=True)
                if not out:
                    LOG.warning('dmidecode returned empty system-uuid')
            except putils.ProcessExecutionError as e:
                LOG.debug("Unable to locate dmidecode. For Cinder RSD Backend,"
                          " please make sure it is installed: %s", e)
                out = ""
        return out.strip()

    def _get_nvme_devices(self):
        nvme_devices = []
        # match nvme devices like /dev/nvme10n10
        pattern = r'/dev/nvme[0-9]+n[0-9]+'
        cmd = ['nvme', 'list']
        for retry in range(1, self.device_scan_attempts + 1):
            try:
                (out, err) = self._execute(*cmd,
                                           root_helper=self._root_helper,
                                           run_as_root=True)
                for line in out.split('\n'):
                    result = re.match(pattern, line)
                    if result:
                        nvme_devices.append(result.group(0))
                LOG.debug("_get_nvme_devices returned %(nvme_devices)s",
                          {'nvme_devices': nvme_devices})
                return nvme_devices

            except putils.ProcessExecutionError:
                LOG.warning(
                    "Failed to list available NVMe connected controllers, "
                    "retrying.")
                time.sleep(retry ** 2)
        else:
            msg = _("Failed to retrieve available connected NVMe controllers "
                    "when running nvme list.")
            raise exception.CommandExecutionFailed(message=msg)

    @utils.retry(exceptions=exception.VolumePathsNotFound)
    def _get_device_path(self, current_nvme_devices):
        all_nvme_devices = self._get_nvme_devices()
        LOG.debug("all_nvme_devices are %(all_nvme_devices)s",
                  {'all_nvme_devices': all_nvme_devices})
        path = set(all_nvme_devices) - set(current_nvme_devices)
        if not path:
            raise exception.VolumePathsNotFound()
        return list(path)

    @utils.retry(exceptions=putils.ProcessExecutionError)
    def _try_connect_nvme(self, cmd):
        self._execute(*cmd, root_helper=self._root_helper,
                      run_as_root=True)

    def _get_nvme_subsys(self):
        # Example output:
        # {
        #   'Subsystems' : [
        #     {
        #       'Name' : 'nvme-subsys0',
        #       'NQN' : 'nqn.2016-06.io.spdk:cnode1'
        #     },
        #     {
        #       'Paths' : [
        #         {
        #           'Name' : 'nvme0',
        #           'Transport' : 'rdma',
        #           'Address' : 'traddr=10.0.2.15 trsvcid=4420'
        #         }
        #       ]
        #     }
        #   ]
        # }
        #

        cmd = ['nvme', 'list-subsys', '-o', 'json']
        ret_val = self._execute(*cmd, root_helper=self._root_helper,
                                run_as_root=True)

        return ret_val

    @utils.retry(exceptions=exception.NotFound, retries=5)
    def _is_nvme_available(self, nvme_name):
        nvme_name_pattern = "/dev/%sn[0-9]+" % nvme_name
        for nvme_dev_name in self._get_nvme_devices():
            if re.match(nvme_name_pattern, nvme_dev_name):
                return True
        else:
            LOG.error("Failed to find nvme device")
            raise exception.NotFound()

    def _wait_for_blk(self, nvme_transport_type, conn_nqn,
                      target_portal, port):
        # Find nvme name in subsystem list and wait max 15 seconds
        # until new volume will be available in kernel
        nvme_name = ""
        nvme_address = "traddr=%s trsvcid=%s" % (target_portal, port)

        # Get nvme subsystems in order to find
        # nvme name for connected nvme
        try:
            (out, err) = self._get_nvme_subsys()
        except putils.ProcessExecutionError:
            LOG.error("Failed to get nvme subsystems")
            raise

        # Get subsystem list. Throw exception if out is currupt or empty
        try:
            subsystems = json.loads(out)['Subsystems']
        except Exception:
            return False

        # Find nvme name among subsystems
        for i in range(0, int(len(subsystems) / 2)):
            subsystem = subsystems[i * 2]
            if 'NQN' in subsystem and subsystem['NQN'] == conn_nqn:
                for path in subsystems[i * 2 + 1]['Paths']:
                    if (path['Transport'] == nvme_transport_type
                            and path['Address'] == nvme_address):
                        nvme_name = path['Name']
                        break

        if not nvme_name:
            return False

        # Wait until nvme will be available in kernel
        return self._is_nvme_available(nvme_name)

    def _try_disconnect_volume(self, conn_nqn, ignore_errors=False):
        cmd = [
            'nvme',
            'disconnect',
            '-n',
            conn_nqn]
        try:
            self._execute(
                *cmd,
                root_helper=self._root_helper,
                run_as_root=True)

        except putils.ProcessExecutionError:
            LOG.error(
                "Failed to disconnect from NVMe nqn "
                "%(conn_nqn)s", {'conn_nqn': conn_nqn})
            if not ignore_errors:
                raise

    @utils.trace
    @synchronized('connect_volume')
    def connect_volume(self, connection_properties):
        """Discover and attach the volume.

        :param connection_properties: The dictionary that describes all
                                      of the target volume attributes.
               connection_properties must include:
               nqn - NVMe subsystem name to the volume to be connected
               target_port - NVMe target port that hosts the nqn sybsystem
               target_portal - NVMe target ip that hosts the nqn sybsystem
        :type connection_properties: dict
        :returns: dict
        """
        if connection_properties.get('vol_uuid'):  # compatibility
            return self._connect_volume_replicated(connection_properties)

        current_nvme_devices = self._get_nvme_devices()

        device_info = {'type': 'block'}
        conn_nqn = connection_properties['nqn']
        target_portal = connection_properties['target_portal']
        port = connection_properties['target_port']
        nvme_transport_type = connection_properties['transport_type']
        host_nqn = connection_properties.get('host_nqn')
        cmd = [
            'nvme', 'connect',
            '-t', nvme_transport_type,
            '-n', conn_nqn,
            '-a', target_portal,
            '-s', port]
        if host_nqn:
            cmd.extend(['-q', host_nqn])

        self._try_connect_nvme(cmd)
        try:
            self._wait_for_blk(nvme_transport_type, conn_nqn,
                               target_portal, port)
        except exception.NotFound:
            LOG.error("Waiting for nvme failed")
            self._try_disconnect_volume(conn_nqn, True)
            raise exception.NotFound(message="nvme connect: NVMe device "
                                             "not found")
        path = self._get_device_path(current_nvme_devices)
        device_info['path'] = path[0]
        LOG.debug("NVMe device to be connected to is %(path)s",
                  {'path': path[0]})
        return device_info

    @utils.trace
    @synchronized('connect_volume')
    def disconnect_volume(self, connection_properties, device_info,
                          force=False, ignore_errors=False):
        """Detach and flush the volume.

        :param connection_properties: The dictionary that describes all
                                      of the target volume attributes.
               connection_properties must include:
               device_path - path to the volume to be connected
        :type connection_properties: dict

        :param device_info: historical difference, but same as connection_props
        :type device_info: dict

        """
        if connection_properties.get('vol_uuid'):  # compatibility
            return self._disconnect_volume_replicated(
                connection_properties, device_info,
                force=force, ignore_errors=ignore_errors)

        conn_nqn = connection_properties['nqn']
        if device_info and device_info.get('path'):
            device_path = device_info.get('path')
        else:
            device_path = connection_properties['device_path'] or ''
        current_nvme_devices = self._get_nvme_devices()
        if device_path not in current_nvme_devices:
            LOG.warning("Trying to disconnect device %(device_path)s with "
                        "subnqn %(conn_nqn)s that is not connected.",
                        {'device_path': device_path, 'conn_nqn': conn_nqn})
            return

        exc = exception.ExceptionChainer()
        with exc.context(force, 'Flushing %s failed', device_path):
            self._linuxscsi.flush_device_io(device_path)

        LOG.debug(
            "Trying to disconnect from device %(device_path)s with "
            "subnqn %(conn_nqn)s",
            {'device_path': device_path, 'conn_nqn': conn_nqn})
        cmd = [
            'nvme',
            'disconnect',
            '-n',
            conn_nqn]
        with exc.context(force, "Failed to disconnect from NVMe nqn "
                         "%(conn_nqn)s with device_path %(device_path)s",
                         {'conn_nqn': conn_nqn, 'device_path': device_path}):
            self._execute(
                *cmd,
                root_helper=self._root_helper,
                run_as_root=True)

        if exc:
            LOG.warning('There were errors removing %s, leftovers may remain '
                        'in the system', device_path)
            if not ignore_errors:
                raise exc

    @utils.trace
    @synchronized('extend_volume')
    def extend_volume(self, connection_properties):
        """Update the local kernel's size information.

        Try and update the local kernel's size information
        for an LVM volume.
        """
        if connection_properties.get('vol_uuid'):  # compatibility
            return self._extend_volume_replicated(connection_properties)

        volume_paths = self.get_volume_paths(connection_properties)
        if volume_paths:
            return self._linuxscsi.extend_volume(
                volume_paths, use_multipath=self.use_multipath)
        else:
            LOG.warning("Couldn't find any volume paths on the host to "
                        "extend volume for %(props)s",
                        {'props': connection_properties})
            raise exception.VolumePathsNotFound()

    @utils.trace
    def _connect_volume_replicated(self, connection_properties):
        """connect to volume on host

        connection_properties for NVMe-oF must include:
        target_portals - list of ip,port,transport for each portal
        target_nqn - NVMe-oF Qualified Name
        vol_uuid - UUID for volume/replica
        """

        volume_replicas = connection_properties.get('volume_replicas')
        replica_count = connection_properties.get('replica_count')
        volume_alias = connection_properties.get('alias')

        if volume_replicas:
            host_device_paths = []

            for replica in volume_replicas:
                try:
                    rep_host_device_path = self._connect_target_volume(
                        replica['target_nqn'], replica['vol_uuid'],
                        replica['portals'])
                    if rep_host_device_path:
                        host_device_paths.append(rep_host_device_path)
                except Exception as ex:
                    LOG.error("_connect_target_volume: %s", ex)
            if not host_device_paths:
                raise exception.VolumeDeviceNotFound(
                    device=volume_replicas)

            if replica_count > 1:
                device_path = self._handle_replicated_volume(
                    host_device_paths, volume_alias, replica_count)
            else:
                device_path = self._handle_single_replica(
                    host_device_paths, volume_alias)
        else:
            device_path = self._connect_target_volume(
                connection_properties['target_nqn'],
                connection_properties['vol_uuid'],
                connection_properties['portals'])

        return {'type': 'block', 'path': device_path}

    @utils.trace
    def _disconnect_volume_replicated(self, connection_properties, device_info,
                                      force=False, ignore_errors=False):
        device_path = None
        volume_replicas = connection_properties.get('volume_replicas')
        if device_info and device_info.get('path'):
            device_path = device_info['path']
        elif connection_properties.get('device_path'):
            device_path = connection_properties['device_path']
        elif volume_replicas and len(volume_replicas) > 1:
            device_path = '/dev/md/' + connection_properties['alias']

        if volume_replicas and len(volume_replicas) > 1:
            NVMeOFConnector.end_raid(device_path)
        else:
            if (NVMeOFConnector._get_fs_type(device_path)
                    == 'linux_raid_member'):
                NVMeOFConnector.end_raid(device_path)

    def _extend_volume_replicated(self, connection_properties):
        volume_replicas = connection_properties.get('volume_replicas')

        if volume_replicas and len(volume_replicas) > 1:
            device_path = '/dev/md/' + connection_properties['alias']
            NVMeOFConnector.run_mdadm(
                ['mdadm', '--grow', '--size', 'max', device_path])
        else:
            if not volume_replicas:
                target_nqn = connection_properties['target_nqn']
                vol_uuid = connection_properties['vol_uuid']
            elif len(volume_replicas) == 1:
                target_nqn = volume_replicas[0]['target_nqn']
                vol_uuid = volume_replicas[0]['vol_uuid']
            device_path = NVMeOFConnector.get_nvme_device_path(
                target_nqn, vol_uuid)

        return self._linuxscsi.get_device_size(device_path)

    def _connect_target_volume(self, target_nqn, vol_uuid, portals):
        try:
            host_device_path = NVMeOFConnector.get_nvme_device_path(
                target_nqn, vol_uuid)
        except exception.VolumeDeviceNotFound:
            host_device_path = None

        if not host_device_path:
            any_connect = NVMeOFConnector.connect_to_portals(
                target_nqn, portals)
            if not any_connect:
                LOG.error(
                    "No successful connections: %(host_devices)s",
                    {'host_devices': target_nqn})
                raise exception.VolumeDeviceNotFound(device=target_nqn)

            host_device_path = NVMeOFConnector.get_nvme_device_path(
                target_nqn, vol_uuid)
            if not host_device_path:
                LOG.error(
                    "No accessible volume device: %(host_devices)s",
                    {'host_devices': target_nqn})
                raise exception.VolumeDeviceNotFound(device=target_nqn)
        else:
            NVMeOFConnector.rescan(target_nqn, vol_uuid)
            host_device_path = NVMeOFConnector.get_nvme_device_path(
                target_nqn, vol_uuid)

        return host_device_path

    @staticmethod
    def connect_to_portals(target_nqn, target_portals):
        """connect to any of NVMe-oF target portals"""
        any_connect = False
        for portal in target_portals:
            portal_address = portal[0]
            portal_port = portal[1]
            if portal[2] == 'RoCEv2':
                portal_transport = 'rdma'
            else:
                portal_transport = 'tcp'
            nvme_command = (
                'connect', '-a', portal_address, '-s', portal_port, '-t',
                portal_transport, '-n', target_nqn, '-Q', '128', '-l', '-1')
            try:
                NVMeOFConnector.run_nvme_cli(nvme_command)
                any_connect = True
                break
            except Exception:
                LOG.exception("Could not connect to portal %s", portal)
        return any_connect

    @staticmethod
    def get_nvme_controller(target_nqn):
        ctrls = glob.glob('/sys/class/nvme-fabrics/ctl/nvme*')
        for ctrl in ctrls:
            try:
                lines, _err = priv_rootwrap.custom_execute(
                    'cat', ctrl + '/subsysnqn')
                for line in lines.split('\n'):
                    if line == target_nqn:
                        state, _err = priv_rootwrap.custom_execute(
                            'cat', ctrl + '/state')
                        if 'live' not in state:
                            LOG.debug("nvmeof ctrl device not live: %s", ctrl)
                            raise exception.VolumeDeviceNotFound(device=ctrl)
                        return ctrl[ctrl.rfind('/') + 1:]
            except putils.ProcessExecutionError as e:
                LOG.exception(e)

        raise exception.VolumeDeviceNotFound(device=target_nqn)

    @staticmethod
    @utils.retry(exceptions=exception.VolumeDeviceNotFound)
    def get_nvme_device_path(target_nqn, vol_uuid):
        nvme_ctrl = NVMeOFConnector.get_nvme_controller(target_nqn)
        blocks = glob.glob(
            '/sys/class/block/' + nvme_ctrl + 'n*')
        for block in blocks:
            uuid_path = block + '/uuid'
            try:
                exists = os.path.exists(uuid_path)
                if exists:
                    uuid_lines, _err = priv_rootwrap.custom_execute(
                        'cat', uuid_path)
                    if uuid_lines.split('\n')[0] == vol_uuid:
                        return '/dev/' + block[block.rfind('/') + 1:]
            except putils.ProcessExecutionError as e:
                LOG.exception(e)
        raise exception.VolumeDeviceNotFound(device=vol_uuid)

    def _handle_replicated_volume(self, host_device_paths,
                                  volume_alias, num_of_replicas):
        path_in_raid = False
        for dev_path in host_device_paths:
            path_in_raid = NVMeOFConnector._is_device_in_raid(dev_path)
            if path_in_raid:
                break
        device_path = '/dev/md/' + volume_alias
        if path_in_raid:
            NVMeOFConnector.stop_and_assemble_raid(
                host_device_paths, device_path, False)
        else:
            paths_found = len(host_device_paths)
            if num_of_replicas > paths_found:
                LOG.error(
                    'Cannot create MD as %s out of %s legs were found.',
                    paths_found, num_of_replicas)
                raise exception.VolumeDeviceNotFound(device=volume_alias)
            NVMeOFConnector.create_raid(host_device_paths, '1',
                                        volume_alias, volume_alias, False)

        return device_path

    def _handle_single_replica(self, host_device_paths, volume_alias):
        if (NVMeOFConnector._get_fs_type(host_device_paths[0])
                == 'linux_raid_member'):
            md_path = '/dev/md/' + volume_alias
            NVMeOFConnector.stop_and_assemble_raid(
                host_device_paths, md_path, False)
            return md_path
        return host_device_paths[0]

    @staticmethod
    def run_mdadm(cmd, raise_exception=False):
        cmd_output = None
        try:
            lines, err = priv_rootwrap.custom_execute(*cmd)
            for line in lines.split('\n'):
                cmd_output = line
                break
        except putils.ProcessExecutionError as ex:
            LOG.warning("[!] Could not run mdadm: %s", str(ex))
            if raise_exception:
                raise ex
        return cmd_output

    @staticmethod
    def _is_device_in_raid(device_path):
        cmd = ['mdadm', '--examine', device_path]
        raid_expected = device_path + ':'
        try:
            lines, err = priv_rootwrap.custom_execute(*cmd)
            for line in lines.split('\n'):
                if line == raid_expected:
                    return True
                else:
                    return False
        except putils.ProcessExecutionError:
            return False

    @staticmethod
    def ks_readlink(dest):
        try:
            return os.readlink(dest)
        except Exception:
            return ''

    @staticmethod
    def get_md_name(device_name):
        get_md_cmd = (
            'cat /proc/mdstat | grep ' + device_name +
            ' | awk \'{print $1;}\'')
        cmd = ['bash', '-c', get_md_cmd]
        LOG.debug("[!] cmd = " + str(cmd))
        cmd_output = None

        try:
            lines, err = priv_rootwrap.custom_execute(*cmd)

            for line in lines.split('\n'):
                cmd_output = line
                break

            LOG.debug("[!] cmd_output = " + cmd_output)
            if err:
                return None

            return cmd_output
        except putils.ProcessExecutionError as ex:
            LOG.warning("[!] Could not run cmd: %s", str(ex))
        return None

    @staticmethod
    def stop_and_assemble_raid(drives, md_path, read_only):
        md_name = None
        i = 0
        assembled = False
        link = ''
        while i < 5 and not assembled:
            for drive in drives:
                device_name = drive[5:]
                md_name = NVMeOFConnector.get_md_name(device_name)
                link = NVMeOFConnector.ks_readlink(md_path)
                if link != '':
                    link = os.path.basename(link)
                if md_name and md_name == link:
                    return
                LOG.debug(
                    "sleeping 1 sec -allow auto assemble link = " +
                    link + " md path = " + md_path)
                time.sleep(1)

            if md_name and md_name != link:
                NVMeOFConnector.stop_raid(md_name)

            try:
                assembled = NVMeOFConnector.assemble_raid(
                    drives, md_path, read_only)
            except Exception:
                i += 1

    @staticmethod
    def assemble_raid(drives, md_path, read_only):
        cmd = ['mdadm', '--assemble', '--run', md_path]

        if read_only:
            cmd.append('-o')

        for i in range(len(drives)):
            cmd.append(drives[i])

        try:
            NVMeOFConnector.run_mdadm(cmd, True)
        except putils.ProcessExecutionError as ex:
            LOG.warning("[!] Could not _assemble_raid: %s", str(ex))
            raise ex

        return True

    @staticmethod
    def create_raid(drives, raid_type, device_name, name, read_only):
        cmd = ['mdadm']
        num_drives = len(drives)
        cmd.append('-C')

        if read_only:
            cmd.append('-o')

        cmd.append(device_name)
        cmd.append('-R')

        if name:
            cmd.append('-N')
            cmd.append(name)

        cmd.append('--level')
        cmd.append(raid_type)
        cmd.append('--raid-devices=' + str(num_drives))
        cmd.append('--bitmap=internal')
        cmd.append('--homehost=any')
        cmd.append('--failfast')
        cmd.append('--assume-clean')

        for i in range(len(drives)):
            cmd.append(drives[i])

        LOG.debug('[!] cmd = ' + str(cmd))
        NVMeOFConnector.run_mdadm(cmd)

    @staticmethod
    def end_raid(device_path):
        raid_exists = NVMeOFConnector.is_raid_exists(device_path)
        if raid_exists:
            for i in range(10):
                try:
                    cmd_out = NVMeOFConnector.stop_raid(
                        device_path)
                    if not cmd_out:
                        break
                except Exception:
                    break
                time.sleep(1)
            try:
                is_exist = os.path.exists(device_path)
                LOG.debug("[!] is_exist = %s", is_exist)
                if is_exist:
                    NVMeOFConnector.remove_raid(device_path)
                    os.remove(device_path)
            except Exception:
                LOG.debug('[!] Exception_stop_raid!')

    @staticmethod
    def stop_raid(md_path):
        cmd = ['mdadm', '--stop', md_path]
        LOG.debug("[!] cmd = " + str(cmd))
        cmd_out = NVMeOFConnector.run_mdadm(cmd)
        return cmd_out

    @staticmethod
    def is_raid_exists(device_path):
        cmd = ['mdadm', '--detail', device_path]
        LOG.debug("[!] cmd = " + str(cmd))
        raid_expected = device_path + ':'
        try:
            lines, err = priv_rootwrap.custom_execute(*cmd)

            for line in lines.split('\n'):
                LOG.debug("[!] line = " + line)
                if line == raid_expected:
                    return True
                else:
                    return False
        except putils.ProcessExecutionError:
            return False

    @staticmethod
    def remove_raid(device_path):
        cmd = ['mdadm', '--remove', device_path]
        LOG.debug("[!] cmd = " + str(cmd))
        NVMeOFConnector.run_mdadm(cmd)

    @staticmethod
    def run_nvme_cli(nvme_command, **kwargs):
        (out, err) = priv_rootwrap.custom_execute(
            'nvme', *nvme_command, check_exit_code=True)
        msg = ("nvme %(nvme_command)s: stdout=%(out)s stderr=%(err)s" %
               {'nvme_command': nvme_command, 'out': out, 'err': err})
        LOG.debug("[!] " + msg)

        return out, err

    @staticmethod
    def rescan(target_nqn, vol_uuid):
        ctr_device = (
            NVMeOFConnector.get_search_path() +
            NVMeOFConnector.get_nvme_controller(target_nqn))
        nvme_command = ('ns-rescan', ctr_device)
        try:
            NVMeOFConnector.run_nvme_cli(nvme_command)
        except Exception as e:
            raise exception.CommandExecutionFailed(e, cmd=nvme_command)

    @staticmethod
    def _get_fs_type(device_path):
        cmd = ['blkid', device_path, '-s', 'TYPE', '-o', 'value']
        LOG.debug("[!] cmd = " + str(cmd))
        fs_type = None

        try:
            lines, err = priv_rootwrap.custom_execute(*cmd)

            fs_type = lines.split('\n')[0]
        except putils.ProcessExecutionError:
            return None

        return fs_type
