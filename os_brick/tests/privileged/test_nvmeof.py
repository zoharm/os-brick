# Copyright (c) 2021, Red Hat, Inc.
# All Rights Reserved.
#
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
import builtins
import errno
from unittest import mock

import ddt
from oslo_concurrency import processutils as putils

import os_brick.privileged as privsep_brick
import os_brick.privileged.nvmeof as privsep_nvme
from os_brick.privileged import rootwrap
from os_brick.tests import base


@ddt.ddt
class PrivNVMeTestCase(base.TestCase):
    def setUp(self):
        super(PrivNVMeTestCase, self).setUp()

        # Disable privsep server/client mode
        privsep_brick.default.set_client_mode(False)
        self.addCleanup(privsep_brick.default.set_client_mode, True)

    @mock.patch('os.chmod')
    @mock.patch.object(builtins, 'open', new_callable=mock.mock_open)
    @mock.patch('os.makedirs')
    @mock.patch.object(rootwrap, 'custom_execute')
    def test_create_hostnqn(self, mock_exec, mock_mkdirs, mock_open,
                            mock_chmod):
        hostnqn = mock.Mock()
        mock_exec.return_value = (hostnqn, mock.sentinel.err)

        res = privsep_nvme.create_hostnqn()

        mock_mkdirs.assert_called_once_with('/etc/nvme',
                                            mode=0o755,
                                            exist_ok=True)
        mock_exec.assert_called_once_with('nvme', 'show-hostnqn')
        mock_open.assert_called_once_with('/etc/nvme/hostnqn', 'w')
        stripped_hostnqn = hostnqn.strip.return_value
        mock_open().write.assert_called_once_with(stripped_hostnqn)
        mock_chmod.assert_called_once_with('/etc/nvme/hostnqn', 0o644)
        self.assertEqual(stripped_hostnqn, res)

    @mock.patch('os.chmod')
    @mock.patch.object(builtins, 'open', new_callable=mock.mock_open)
    @mock.patch('os.makedirs')
    @mock.patch.object(rootwrap, 'custom_execute')
    def test_create_hostnqn_generate(self, mock_exec, mock_mkdirs, mock_open,
                                     mock_chmod):
        hostnqn = mock.Mock()
        mock_exec.side_effect = [
            putils.ProcessExecutionError(exit_code=errno.ENOENT),
            (hostnqn, mock.sentinel.err)
        ]

        res = privsep_nvme.create_hostnqn()

        mock_mkdirs.assert_called_once_with('/etc/nvme',
                                            mode=0o755,
                                            exist_ok=True)
        self.assertEqual(2, mock_exec.call_count)
        mock_exec.assert_has_calls([mock.call('nvme', 'show-hostnqn'),
                                    mock.call('nvme', 'gen-hostnqn')])

        mock_open.assert_called_once_with('/etc/nvme/hostnqn', 'w')
        stripped_hostnqn = hostnqn.strip.return_value
        mock_open().write.assert_called_once_with(stripped_hostnqn)
        mock_chmod.assert_called_once_with('/etc/nvme/hostnqn', 0o644)
        self.assertEqual(stripped_hostnqn, res)

    @ddt.data(OSError(errno.ENOENT),  # nvme not present in system
              putils.ProcessExecutionError(exit_code=123))  # nvme error
    @mock.patch('os.makedirs')
    @mock.patch.object(rootwrap, 'custom_execute')
    def test_create_hostnqn_nvme_not_present(self, exception,
                                             mock_exec, mock_mkdirs):
        mock_exec.side_effect = exception
        res = privsep_nvme.create_hostnqn()
        mock_mkdirs.assert_called_once_with('/etc/nvme',
                                            mode=0o755,
                                            exist_ok=True)
        mock_exec.assert_called_once_with('nvme', 'show-hostnqn')
        self.assertEqual('', res)

    @mock.patch.object(rootwrap, 'custom_execute')
    def test_get_host_uuid(self, mock_execute):
        mock_execute.return_value = 'fakeuuid', ''
        self.assertEqual('fakeuuid', privsep_nvme.get_host_uuid())

    @mock.patch.object(rootwrap, 'custom_execute')
    def test_get_host_uuid_err(self, mock_execute):
        mock_execute.side_effect = putils.ProcessExecutionError()
        self.assertIsNone(privsep_nvme.get_host_uuid())

    @mock.patch.object(rootwrap, 'custom_execute')
    def test_run_nvme_cli(self, mock_execute):
        mock_execute.return_value = ("\n", "")
        cmd = 'dummy command'
        result = privsep_nvme.run_nvme_cli(cmd)
        self.assertEqual(("\n", ""), result)

    @mock.patch.object(rootwrap, 'custom_execute')
    def test_run_mdadm(self, mock_execute):
        mock_execute.return_value = "fakeuuid"
        cmd = ['mdadm', '--examine', '/dev/nvme1']
        result = privsep_nvme.run_mdadm(cmd)
        self.assertEqual('fakeuuid', result)
        args, kwargs = mock_execute.call_args
        self.assertEqual(args[0], cmd[0])
        self.assertEqual(args[1], cmd[1])
        self.assertEqual(args[2], cmd[2])

    @mock.patch.object(rootwrap, 'custom_execute')
    def test_run_mdadm_err(self, mock_execute):
        mock_execute.side_effect = putils.ProcessExecutionError()
        cmd = ['mdadm', '--examine', '/dev/nvme1']
        result = privsep_nvme.run_mdadm(cmd)
        self.assertIsNone(result)
        args, kwargs = mock_execute.call_args
        self.assertEqual(args[0], cmd[0])
        self.assertEqual(args[1], cmd[1])
        self.assertEqual(args[2], cmd[2])
