"""Panda package management for Windows.

Command configuration object definitions.
"""
import abc
from pathlib import Path
import posixpath
import re
import tempfile as tf

from common import constants as cm_const
from common import logger
from common.cli import CLI_COMMAND, CLI_CMDOPT, CLI_CMDTARGET
from common.storage import InstallationStorage
from core import exceptions as cr_exc
from core import utils as cr_utl

from common import utils as cm_utl


LOG = logger.get_logger(__name__)

CMDCONF_TYPES = {}


def create(**cmd_opts):
    """Create configuration for a command.

    :param cmd_opts: dict, command options:
                     {
                         'command_name': <str>,

                     }
    """
    command_name = cmd_opts.get(CLI_CMDOPT.CMD_NAME, '')

    return CMDCONF_TYPES[command_name](**cmd_opts)


def cmdconf_type(command_name):
    """Register a command configuration class in the config types registry.

    :param command_name: str, name of a command
    """
    def decorator(cls):
        """"""
        CMDCONF_TYPES[command_name] = cls
        return cls

    return decorator


class CommandConfig(metaclass=abc.ABCMeta):
    """Abstract base class for command configuration types.
    """
    def __init__(self, **cmd_opts):
        """Constructor."""
        self.cmd_opts = cmd_opts

    def __repr__(self):
        return (
            '<%s(cmd_opts="%s")>' % (self.__class__.__name__, self.cmd_opts)
        )

    def __str__(self):
        return self.__repr__()


@cmdconf_type(CLI_COMMAND.SETUP)
class CmdConfigSetup(CommandConfig):
    """Configuration for the 'setup' command."""
    def __init__(self, **cmd_opts):
        """"""
        self.msg_src = self.__class__.__name__
        super(CmdConfigSetup, self).__init__(**cmd_opts)
        # DC/OS installation storage manager
        self.inst_storage = InstallationStorage(
            root_dpath=cmd_opts.get(CLI_CMDOPT.INST_ROOT),
            cfg_dpath=cmd_opts.get(CLI_CMDOPT.INST_CONF),
            pkgrepo_dpath=cmd_opts.get(CLI_CMDOPT.INST_PKGREPO),
            state_dpath=cmd_opts.get(CLI_CMDOPT.INST_STATE),
            var_dpath=cmd_opts.get(CLI_CMDOPT.INST_VAR),
        )
        LOG.debug(f'{self.msg_src}: istor_nodes:'
                  f' {self.inst_storage.istor_nodes}')
        if cmd_opts.get(CLI_CMDOPT.CMD_TARGET) == CLI_CMDTARGET.PKGALL:
            # Make sure that the installation storage is in consistent state
            self.inst_storage.construct()

        # DC/OS cluster setup parameters
        self.cluster_conf_nop = False
        self.cluster_conf = self.get_cluster_conf()
        LOG.debug(f'{self.msg_src}: cluster_conf: {self.cluster_conf}')

        # Reference list of DC/OS packages
        self.ref_pkg_list = self.get_ref_pkg_list()
        LOG.debug(f'{self.msg_src}: ref_pkg_list: {self.ref_pkg_list}')

        # DC/OS aggregated configuration object
        self.dcos_conf = self.get_dcos_conf()
        LOG.debug(f'{self.msg_src}: dcos_conf: {self.dcos_conf}')

    def get_cluster_conf(self):
        """"Get a collection of DC/OS cluster configuration options.

        :return: dict, configparser.ConfigParser.read_dict() compatible data
        """
        # Load cluster configuration file
        fpath = Path(self.cmd_opts.get(CLI_CMDOPT.DCOS_CLUSTERCFGPATH))

        # Unblock irrelevant local operations
        if str(fpath) == 'NOP':
            self.cluster_conf_nop = True
            LOG.info(f'{self.msg_src}: cluster_conf: NOP')
            return {}

        if not fpath.is_absolute():
            if self.inst_storage.cfg_dpath.exists():
                fpath = self.inst_storage.cfg_dpath.joinpath(fpath)
            else:
                fpath = Path('.').resolve().joinpath(fpath)

        cluster_conf = cr_utl.rc_load_ini(
            fpath, emheading='Cluster setup descriptor'
        )

        # CLI options take precedence, if any.
        # list(tuple('ipaddr', 'port'))
        cli_master_priv_ipaddrs = [
            ipaddr.partition(':')[::2] for ipaddr in
            self.cmd_opts.get(CLI_CMDOPT.MASTER_PRIVIPADDR, '').split(' ') if
            ipaddr != ''
        ]
        mnode_sects = [
            sect for sect in cluster_conf if sect.startswith('master-node')
        ]
        # iterator(tuple('ipaddr', 'port'), str)
        change_map = zip(cli_master_priv_ipaddrs, mnode_sects)
        for item in change_map:
            if item[0][0]:
                cluster_conf[item[1]]['privateipaddr'] = item[0][0]
                if item[0][1]:
                    try:
                        port = int(item[0][1])
                    except (ValueError, TypeError):
                        port = cm_const.ZK_CLIENTPORT_DFT
                    port = (port if 0 < port < 65536 else
                            cm_const.ZK_CLIENTPORT_DFT)
                    cluster_conf[item[1]]['zookeeperclientport'] = port

        # Add extra 'master-node' sections, if CLI provides extra arguments
        extra_cli_items = cli_master_priv_ipaddrs[len(mnode_sects):]
        for n, item in enumerate(extra_cli_items):
            if item[0]:
                # TODO: Implement collision tolerance for section names.
                cluster_conf[f'master-node-extra{n}'] = {}
                cluster_conf[f'master-node-extra{n}']['privateipaddr'] = (
                    item[0]
                )
                if item[1]:
                    try:
                        port = int(item[1])
                    except (ValueError, TypeError):
                        port = cm_const.ZK_CLIENTPORT_DFT
                    port = (port if 0 < port < 65536 else
                            cm_const.ZK_CLIENTPORT_DFT)
                    cluster_conf[f'master-node-extra{n}'][
                        'zookeeperclientport'
                    ] = port
        # DC/OS storage distribution parameters
        cli_dstor_url = self.cmd_opts.get(CLI_CMDOPT.DSTOR_URL)
        cli_dstor_pkgrepo_path = self.cmd_opts.get(
            CLI_CMDOPT.DSTOR_PKGREPOPATH
        )
        cli_dstor_pkglist_path = self.cmd_opts.get(
            CLI_CMDOPT.DSTOR_PKGLISTPATH
        )
        cli_dstor_dcoscfg_path = self.cmd_opts.get(
            CLI_CMDOPT.DSTOR_DCOSCFGPATH
        )
        if not cluster_conf.get('distribution-storage'):
            cluster_conf['distribution-storage'] = {}

        if cli_dstor_url:
            cluster_conf['distribution-storage']['rooturl'] = cli_dstor_url
        if cli_dstor_pkgrepo_path:
            cluster_conf['distribution-storage']['pkgrepopath'] = (
                cli_dstor_pkgrepo_path
            )
        if cli_dstor_pkglist_path:
            cluster_conf['distribution-storage']['pkglistpath'] = (
                cli_dstor_pkglist_path
            )
        if cli_dstor_dcoscfg_path:
            cluster_conf['distribution-storage']['dcoscfgpath'] = (
                cli_dstor_dcoscfg_path
            )

        # Local parameters of DC/OS node
        cli_local_priv_ipaddr = self.cmd_opts.get(CLI_CMDOPT.LOCAL_PRIVIPADDR)
        if not cluster_conf.get('local'):
            cluster_conf['local'] = {}

        if cli_local_priv_ipaddr:
            cluster_conf['local']['privateipaddr'] = cli_local_priv_ipaddr

        return cluster_conf

    def get_ref_pkg_list(self):
        """Get the current reference package list.

        :return: list, JSON-formatted data
        """
        dstor_root_url = (
            self.cluster_conf.get('distribution-storage', {}).get(
                'rooturl', ''
            )
        )
        dstor_pkglist_path = (
            self.cluster_conf.get('distribution-storage', {}).get(
                'pkglistpath', ''
            )
        )
        # Unblock irrelevant local operations
        if self.cluster_conf_nop or dstor_pkglist_path == 'NOP':
            LOG.info(f'{self.msg_src}: ref_pkg_list: NOP')
            return []

        rpl_url = posixpath.join(dstor_root_url, dstor_pkglist_path)
        rpl_fname = Path(dstor_pkglist_path).name

        try:
            cm_utl.download(rpl_url, str(self.inst_storage.tmp_dpath))
            LOG.debug(f'{self.msg_src}: Reference package list: Download:'
                      f' {rpl_fname}: {rpl_url}')
        except Exception as e:
            raise cr_exc.RCDownloadError(
                f'Reference package list: Download: {rpl_fname}: {rpl_url}:'
                f' {type(e).__name__}: {e}'
            ) from e

        rpl_fpath = self.inst_storage.tmp_dpath.joinpath(rpl_fname)
        try:
            return cr_utl.rc_load_json(
                rpl_fpath, emheading=f'Reference package list: {rpl_fname}'
            )
        except cr_exc.RCError as e:
            raise e
        finally:
            rpl_fpath.unlink()

    def get_dcos_conf(self):
        """Get the DC/OS aggregated configuration object.

        :return: dict, set of DC/OS shared and package specific configuration
                 templates coupled with 'key=value' substitution data
                 container:
                 {
                    'template': {
                        'package': [
                            {'path': <str>, 'content': <str>},
                             ...
                        ]
                    },
                    'values': {
                        key: value,
                        ...
                    }
                 }
        """

        dstor_root_url = (
            self.cluster_conf.get('distribution-storage', {}).get(
                'rooturl', ''
            )
        )
        dstor_linux_pkg_index_path = (
            self.cluster_conf.get('distribution-storage', {}).get(
                'dcosclusterpkginfopath', ''
            )
        )
        dcos_conf_pkg_name = 'dcos-config-win'
        template_fname = 'dcos-config-windows.yaml'
        values_fname = 'expanded.config.full.json'

        # Unblock irrelevant local operations
        if self.cluster_conf_nop or dstor_linux_pkg_index_path == 'NOP':
            LOG.info(f'{self.msg_src}: dcos_conf: NOP')
            return {}

        # Linux package index direct URL
        lpi_url = posixpath.join(dstor_root_url, dstor_linux_pkg_index_path)
        lpi_fname = Path(dstor_linux_pkg_index_path).name

        try:
            cm_utl.download(lpi_url, str(self.inst_storage.tmp_dpath))
            LOG.debug(f'{self.msg_src}: DC/OS Linux package index: Download:'
                      f' {lpi_fname}: {lpi_url}')
        except Exception as e:
            raise cr_exc.RCDownloadError(
                f'DC/OS Linux package index: Download: {lpi_fname}:'
                f' {lpi_url}: {type(e).__name__}: {e}'
            ) from e

        lpi_fpath = self.inst_storage.tmp_dpath.joinpath(lpi_fname)

        try:
            lpi = cr_utl.rc_load_json(
                lpi_fpath,
                emheading=f'DC/OS Linux package index: {lpi_fname}'
            )

            if (not isinstance(lpi, dict) or not
                    isinstance(lpi.get(dcos_conf_pkg_name), dict)):
                raise cr_exc.RCInvalidError(
                    f'DC/OS Linux package index: {lpi}'
                )

            dstor_dcoscfg_pkg_path = lpi.get(dcos_conf_pkg_name).get(
                'filename'
            )
            if not isinstance(dstor_dcoscfg_pkg_path, str):
                raise cr_exc.RCElementError(
                    f'DC/OS Linux package index: DC/OS config package'
                    f' distribution storage path: {dstor_dcoscfg_pkg_path}'
                )
        except cr_exc.RCError as e:
            raise e
        finally:
            lpi_fpath.unlink()

        dcoscfg_pkg_url = posixpath.join(
            dstor_root_url, dstor_dcoscfg_pkg_path
        )
        dcoscfg_pkg_fname = Path(dstor_dcoscfg_pkg_path).name

        # Download DC/OS aggregated configuration package ...
        try:
            cm_utl.download(dcoscfg_pkg_url, str(self.inst_storage.tmp_dpath))
            LOG.debug(f'{self.msg_src}: DC/OS aggregated config: Download:'
                      f' {dcoscfg_pkg_fname}: {dcoscfg_pkg_url}')
        except Exception as e:
            raise cr_exc.RCDownloadError(
                f'DC/OS aggregated config: Download: {dcoscfg_pkg_fname}:'
                f' {dcoscfg_pkg_url}: {type(e).__name__}: {e}'
            ) from e

        dcoscfg_pkg_fpath = self.inst_storage.tmp_dpath.joinpath(
            dcoscfg_pkg_fname
        )

        try:
            with tf.TemporaryDirectory(
                dir=str(self.inst_storage.tmp_dpath)
            ) as tmp_dpath:
                cm_utl.unpack(str(dcoscfg_pkg_fpath), tmp_dpath)
                LOG.debug(f'{self.msg_src}: DC/OS aggregated config: Extract:'
                          f' OK')

                values_fpath = Path(tmp_dpath).joinpath(values_fname)
                values = cr_utl.rc_load_json(
                    values_fpath,
                    emheading=f'DC/OS aggregated config: Values: {values_fname}'
                )
                template_fpath = Path(tmp_dpath).joinpath(template_fname)
                template = self.load_dcos_conf_templete(template_fpath)
        except Exception as e:
            if not isinstance(e, cr_exc.RCError):
                raise cr_exc.RCExtractError(
                    f'DC/OS aggregated config: {type(e).__name__}: {e}'
                )
            else:
                raise
        else:
            return {'template': template, 'values': values}
        finally:
            dcoscfg_pkg_fpath.unlink()

    @staticmethod
    def load_dcos_conf_templete(fpath):
        """Load the DC/OS aggregated configuration template from disk.

        :param fpath: pathlib.Path, path to template
        """
        p_key = re.compile(r' *- path: (?P<g1>.*)')
        c_key = re.compile(r' *content: [|].*')
        h_key = re.compile(r' *#.*$')

        with fpath.open() as fp:

            aggregator = {'package': []}
            path = ''
            content = []

            for line in fp:
                pk_match = p_key.match(line)
                ck_match = c_key.match(line)
                hk_match = h_key.match(line)

                if pk_match:

                    if path:
                        item = {'path': path, 'content': ''.join(content)}
                        aggregator['package'].append(item)
                        path = pk_match.group('g1')
                        content = []
                    else:
                        path = pk_match.group('g1')
                elif ck_match:
                    continue
                elif hk_match:
                    continue
                else:
                    if not path:
                        continue
                    else:
                        content.append(line.strip(' '))

            item = {'path': path, 'content': ''.join(content)}
            aggregator['package'].append(item)

        return aggregator


@cmdconf_type(CLI_COMMAND.START)
class CmdConfigStart(CommandConfig):
    """Configuration for the 'start' command."""
    def __init__(self, **cmd_opts):
        """"""
        super(CmdConfigStart, self).__init__(**cmd_opts)
        # Create DC/OS installation storage manager
        self.inst_storage = InstallationStorage(
            root_dpath=cmd_opts.get(CLI_CMDOPT.INST_ROOT),
            cfg_dpath=cmd_opts.get(CLI_CMDOPT.INST_CONF),
            pkgrepo_dpath=cmd_opts.get(CLI_CMDOPT.INST_PKGREPO),
            state_dpath=cmd_opts.get(CLI_CMDOPT.INST_STATE),
            var_dpath=cmd_opts.get(CLI_CMDOPT.INST_VAR)
        )
        LOG.debug(f'{self.__class__.__name__}: inst_storage: istor_nodes:'
                  f' {self.inst_storage.istor_nodes}')
        # Make sure that the installation storage is in consistent state
        self.inst_storage.construct()

        # DC/OS cluster setup parameters
        self.cluster_conf = {}
        LOG.debug(
            f'{self.__class__.__name__}: cluster_conf: {self.cluster_conf}'
        )
