# -*- coding: utf-8 -*-
#
# Copyright(C) 2016 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
import difflib
import logging
import os
import re
import socket
import time

from collections import namedtuple

from six.moves import configparser

from convert2rhel import logger, utils
from convert2rhel.toolopts import tool_opts
from convert2rhel.utils import run_subprocess


# Number of times to retry checking the status of dbus
CHECK_DBUS_STATUS_RETRIES = 3

# Allowed conversion paths to RHEL. We want to prevent a conversion and minor
# version update at the same time.
RELEASE_VER_MAPPING = {
    "8.10": "8.10",
    "8.9": "8.9",
    "8.8": "8.8",
    "8.7": "8.7",
    "8.6": "8.6",
    "8.5": "8.5",
    "8.4": "8.4",
    "7.9": "7Server",
    "6.10": "6Server",
}

# For a list of modified rpm files before the conversion starts
PRE_RPM_VA_LOG_FILENAME = "rpm_va.log"

# For a list of modified rpm files after the conversion finishes for comparison purposes
POST_RPM_VA_LOG_FILENAME = "rpm_va_after_conversion.log"

# List of EUS minor versions supported
EUS_MINOR_VERSIONS = ["8.4"]

Version = namedtuple("Version", ["major", "minor"])


class SystemInfo(object):
    def __init__(self):
        # Operating system name (e.g. Oracle Linux)
        self.name = None
        # Single-word lowercase identificator of the system (e.g. oracle)
        self.id = None  # pylint: disable=C0103
        # Major and minor version of the operating system (e.g. version.major == 6, version.minor == 10)
        self.version = None
        # Platform architecture
        self.arch = None
        # Fingerprints of the original operating system GPG keys
        self.fingerprints_orig_os = None
        # Fingerprints of RHEL GPG keys available at:
        #  https://access.redhat.com/security/team/key/
        self.fingerprints_rhel = [
            # RHEL 6/7: RPM-GPG-KEY-redhat-release
            "199e2f91fd431d51",
            # RHEL 6/7: RPM-GPG-KEY-redhat-legacy-release
            "5326810137017186",
            # RHEL 6/7: RPM-GPG-KEY-redhat-legacy-former
            "219180cddb42a60e",
        ]
        # Packages to be removed before the system conversion
        self.excluded_pkgs = []
        # Release packages to be removed before the system conversion
        self.repofile_pkgs = []
        self.cfg_filename = None
        self.cfg_content = None
        self.system_release_file_content = None
        self.logger = None
        # IDs of the default Red Hat CDN repositories that correspond to the current system
        self.default_rhsm_repoids = None
        # IDs of the Extended Update Support (EUS) Red Hat CDN repositories that correspond to the current system
        self.eus_rhsm_repoids = None
        # List of repositories enabled through subscription-manager
        self.submgr_enabled_repos = []
        # Value to use for substituting the $releasever variable in the url of RHEL repositories
        self.releasever = None
        # List of kmods to not inhbit the conversion upon when detected as not available in RHEL
        self.kmods_to_ignore = []
        # Booted kernel VRA (version, release, architecture), e.g. "4.18.0-240.22.1.el8_3.x86_64"
        self.booted_kernel = ""

    def resolve_system_info(self):
        self.logger = logging.getLogger(__name__)
        self.system_release_file_content = self._get_system_release_file_content()
        self.name = self._get_system_name()
        self.id = self.name.split()[0].lower()
        self.version = self._get_system_version()
        self.arch = self._get_architecture()

        self.cfg_filename = self._get_cfg_filename()
        self.cfg_content = self._get_cfg_content()
        self.excluded_pkgs = self._get_excluded_pkgs()
        self.repofile_pkgs = self._get_repofile_pkgs()
        self.default_rhsm_repoids = self._get_default_rhsm_repoids()
        self.eus_rhsm_repoids = self._get_eus_rhsm_repoids()
        self.fingerprints_orig_os = self._get_gpg_key_fingerprints()
        self.generate_rpm_va()
        self.releasever = self._get_releasever()
        self.kmods_to_ignore = self._get_kmods_to_ignore()
        self.booted_kernel = self._get_booted_kernel()
        self.has_internet_access = self._check_internet_access()
        self.dbus_running = self._is_dbus_running()

    @staticmethod
    def _get_system_release_file_content():
        from convert2rhel import redhatrelease

        return redhatrelease.get_system_release_content()

    def _get_system_name(self):
        name = re.search(r"(.+?)\s?(?:release\s?)?\d", self.system_release_file_content).group(1)
        self.logger.info("%-20s %s" % ("Name:", name))
        return name

    def _get_system_version(self):
        """Return a namedtuple with major and minor elements, both of an int type.

        Examples:
        Oracle Linux Server release 6.10
        Oracle Linux Server release 7.8
        CentOS release 6.10 (Final)
        CentOS Linux release 7.6.1810 (Core)
        CentOS Linux release 8.1.1911 (Core)
        """
        match = re.search(r".+?(\d+)\.(\d+)\D?", self.system_release_file_content)
        if not match:
            from convert2rhel import redhatrelease

            self.logger.critical("Couldn't get system version from %s" % redhatrelease.get_system_release_filepath())
        version = Version(int(match.group(1)), int(match.group(2)))

        self.logger.info("%-20s %d.%d" % ("OS version:", version.major, version.minor))
        return version

    def _get_architecture(self):
        arch, _ = utils.run_subprocess(["uname", "-i"], print_output=False)
        arch = arch.strip()  # Remove newline
        self.logger.info("%-20s %s" % ("Architecture:", arch))
        return arch

    def _get_cfg_filename(self):
        cfg_filename = "%s-%d-%s.cfg" % (
            self.id,
            self.version.major,
            self.arch,
        )
        self.logger.info("%-20s %s" % ("Config filename:", cfg_filename))
        return cfg_filename

    def _get_cfg_content(self):
        return self._get_cfg_section("system_info")

    def _get_cfg_section(self, section_name):
        """Read out options from within a specific section in a configuration
        file.
        """
        cfg_parser = configparser.ConfigParser()
        cfg_filepath = os.path.join(utils.DATA_DIR, "configs", self.cfg_filename)
        if not cfg_parser.read(cfg_filepath):
            self.logger.critical(
                "Current combination of system distribution"
                " and architecture is not supported for the"
                " conversion to RHEL."
            )

        options_list = cfg_parser.options(section_name)
        return dict(
            zip(
                options_list,
                [cfg_parser.get(section_name, opt) for opt in options_list],
            )
        )

    def _get_default_rhsm_repoids(self):
        return self._get_cfg_opt("default_rhsm_repoids").split()

    def _get_eus_rhsm_repoids(self):
        return self._get_cfg_opt("eus_rhsm_repoids").split()

    def _get_cfg_opt(self, option_name):
        """Return value of a specific configuration file option."""
        if option_name in self.cfg_content:
            return self.cfg_content[option_name]
        else:
            self.logger.error(
                "Internal error: %s option not found in %s config file." % (option_name, self.cfg_filename)
            )

    def _get_gpg_key_fingerprints(self):
        return self._get_cfg_opt("gpg_fingerprints").split()

    def _get_excluded_pkgs(self):
        return self._get_cfg_opt("excluded_pkgs").split()

    def _get_repofile_pkgs(self):
        return self._get_cfg_opt("repofile_pkgs").split()

    def _get_releasever(self):
        """
        Get the release version to be passed to yum through --releasever.

        Releasever is used to figure out the version of RHEL that is to be used
        for the conversion, passing the releasever var to yum to it’s --releasever
        option when accessing RHEL repos in the conversion. By default, the value is found by mapping
        from the current distro's version to a compatible version of RHEL via the RELEASE_VER_MAPPING.
        This can be overridden by the user by specifying it in the config file. The version specific
        config files are located in convert2rhel/convert2rhel/data.
        """
        releasever_cfg = self._get_cfg_opt("releasever")
        try:
            # return config value or corresponding releasever from the RELEASE_VER_MAPPING
            return releasever_cfg or RELEASE_VER_MAPPING[".".join(map(str, self.version))]
        except KeyError:
            self.logger.critical(
                "%s of version %d.%d is not allowed for conversion.\n"
                "Allowed versions are: %s"
                % (
                    self.name,
                    self.version.major,
                    self.version.minor,
                    list(RELEASE_VER_MAPPING.keys()),
                )
            )

    def _get_kmods_to_ignore(self):
        return self._get_cfg_opt("kmods_to_ignore").split()

    def _get_booted_kernel(self):
        kernel_vra = run_subprocess(["uname", "-r"], print_output=False)[0].rstrip()
        self.logger.debug("Booted kernel VRA (version, release, architecture): {0}".format(kernel_vra))
        return kernel_vra

    def generate_rpm_va(self, log_filename=PRE_RPM_VA_LOG_FILENAME):
        """RPM is able to detect if any file installed as part of a package has been changed in any way after the
        package installation.

        Here we are getting a list of changed package files of all the installed packages. Such a list is useful for
        debug and support purposes. It's being saved to the default log folder as log_filename."""
        if tool_opts.no_rpm_va:
            self.logger.info("Skipping the execution of 'rpm -Va'.")
            return

        self.logger.info(
            "Running the 'rpm -Va' command which can take several"
            " minutes. It can be disabled by using the"
            " --no-rpm-va option."
        )
        rpm_va, _ = utils.run_subprocess(["rpm", "-Va"], print_output=False)
        output_file = os.path.join(logger.LOG_DIR, log_filename)
        utils.store_content_to_file(output_file, rpm_va)
        self.logger.info("The 'rpm -Va' output has been stored in the %s file" % output_file)

    def modified_rpm_files_diff(self):
        """Get a list of modified rpm files after the conversion and compare it to the one from before the conversion."""
        self.generate_rpm_va(log_filename=POST_RPM_VA_LOG_FILENAME)

        pre_rpm_va_log_path = os.path.join(logger.LOG_DIR, PRE_RPM_VA_LOG_FILENAME)
        if not os.path.exists(pre_rpm_va_log_path):
            self.logger.info("Skipping comparison of the 'rpm -Va' output from before and after the conversion.")
            return
        pre_rpm_va = utils.get_file_content(pre_rpm_va_log_path, True)
        post_rpm_va_log_path = os.path.join(logger.LOG_DIR, POST_RPM_VA_LOG_FILENAME)
        post_rpm_va = utils.get_file_content(post_rpm_va_log_path, True)
        modified_rpm_files_diff = "\n".join(
            difflib.unified_diff(
                pre_rpm_va,
                post_rpm_va,
                fromfile=pre_rpm_va_log_path,
                tofile=post_rpm_va_log_path,
                n=0,
                lineterm="",
            )
        )

        if modified_rpm_files_diff:
            self.logger.info(
                "Comparison of modified rpm files from before and after the conversion:\n%s" % modified_rpm_files_diff
            )

    @staticmethod
    def is_rpm_installed(name):
        _, return_code = run_subprocess(["rpm", "-q", name], print_cmd=False, print_output=False)
        return return_code == 0

    # TODO write unit tests
    def get_enabled_rhel_repos(self):
        """Return a tuple of repoids containing RHEL packages.

        These are either the repos enabled through RHSM or the custom
        repositories passed though CLI.
        """
        # TODO:
        # if not self.submgr_enabled_repos:
        #     raise ValueError(
        #         "system_info.get_enabled_rhel_repos is not "
        #          "to be consumed before registering the system with RHSM."
        #     )
        return self.submgr_enabled_repos if not tool_opts.no_rhsm else tool_opts.enablerepo

    def _check_internet_access(self, host="8.8.8.8", port=53, timeout=3):
        """Check whether or not the machine is connected to the internet.

        This method will try to estabilish a socket connection through the
        default host in the method signature (8.8.8.8). If it's connected
        successfully, then we know that internet is accessible from the host.

        .. warnings::
            We might have some problems with this if the host machine is using
            a NAT gateway to route the outbound requests

        .. seealso::
            * Comparison of different methods to check internet connections: https://stackoverflow.com/a/33117579
            * Redirecting IP addresses: https://superuser.com/questions/954665/how-to-redirect-route-ip-address-to-another-ip-address

        :param host: The host to establish a connection to. Defaults to "8.8.8.8".
        :type host: str
        :param port: The port for the connection. Defaults to 53.
        :type port: int
        :param timeout: The timeout in seconds for the connection. Defaults to 3.
        :type port: int
        :return: Return boolean indicating whether or not we have internet access.
        :rtype: bool
        """
        try:
            self.logger.info("Checking internet connectivity using host '%s' and port '%s'." % (host, port))
            socket.setdefaulttimeout(timeout)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
            return True
        except (socket.timeout, socket.error):
            self.logger.warning("Couldn't connect to the host '%s'." % host)
            return False

    def corresponds_to_rhel_eus_release(self):
        """Return whether the current minor version corresponds to a RHEL Extended Update Support (EUS) release.

        For example if we detect CentOS Linux 8.4, the corresponding RHEL 8.4 is an EUS release, but if we detect
        CentOS Linux 8.5, the corresponding RHEL 8.5 is not an EUS release.

        :return: Whether or not the current system has a EUS correspondent in RHEL.
        :rtype: bool
        """
        return self.releasever in EUS_MINOR_VERSIONS

    def _is_dbus_running(self):
        """
        Check whether dbus is running.

        :returns: True if dbus is running.  Otherwise False
        """
        retries = 0
        status = False

        while retries < CHECK_DBUS_STATUS_RETRIES:
            if self.version.major <= 6:
                status = _is_sysv_managed_dbus_running()
            else:
                status = _is_systemd_managed_dbus_running()

            if status is not None:
                # We know that DBus is definitely running or stopped
                break

            # Wait for 1 second, 2 seconds, and then 4 seconds for dbus to be running
            # (In case it was started before convert2rhel but it is slow to start)
            time.sleep(2**retries)
            retries += 1

        else:  # while-else
            # If we haven't gotten a definite yes or no but we've exceeded or retries,
            # report that DBus is not running
            status = False

        return status


def _is_sysv_managed_dbus_running():
    """Get DBus status from SysVinit compatible systems."""
    # None means the status should be retried because we weren't sure if it is turned off.
    running = None
    output, _ret_code = utils.run_subprocess(["/sbin/service", "messagebus", "status"])
    for line in output.splitlines():
        if line.startswith("messagebus"):
            if "running" in line:
                running = True
                break

            # Note: SysV has a stopped status but I don't think that toggles until after
            # the service is running so we could be caught in the case where the service
            # is starting if we don't retry.

    return running


def _is_systemd_managed_dbus_running():
    """Get DBus status from systemd."""
    # Reloading, activating, etc will return None which means to retry
    running = None

    output, ret_code = utils.run_subprocess(["/usr/bin/systemctl", "show", "-p", "ActiveState", "dbus"])
    for line in output.splitlines():
        # Note: systemctl seems to always emit an ActiveState line (ActiveState=inactive if
        # the service doesn't exist).  So this check is just defensive coding.
        if line.startswith("ActiveState="):
            state = line.split("=", 1)[1]

            if state == "active":
                # DBus is definitely running
                running = True
                break

            if state in ("inactive", "deactivating", "failed"):
                # DBus is definitely not running
                running = False
                break

    return running


# Code to be executed upon module import
system_info = SystemInfo()  # pylint: disable=C0103
