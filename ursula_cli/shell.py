#!/usr/bin/env python
# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2015, Craig Tracey <craigtracey@gmail.com>
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

import os
import sys
import time
import shutil
import socket
import logging
import argparse
import subprocess
import vagrant
from distutils.version import LooseVersion
from ConfigParser import ConfigParser, NoOptionError, NoSectionError

import yaml
import ansible
import paramiko
from paramiko import BadHostKeyException, AuthenticationException, SSHException
from paramiko import AutoAddPolicy

LOG = logging.getLogger(__name__)
MINIMUM_ANSIBLE_VERSION = '1.9'


class OpenStackConfigurationError(Exception):
    pass


def init_logfile():
    config = ConfigParser()
    config.read('ansible.cfg')

    try:
        cfg_log = config.get('defaults', 'log_path')
    except (NoOptionError, NoSectionError):
        cfg_log = None

    if cfg_log:
        logfile = os.path.expanduser(cfg_log)
    else:
        logfile = 'ursula.log'

    if not os.path.exists(logfile):
        with open(logfile, 'a'):
            os.utime(logfile, None)


def _initialize_logger(level=logging.DEBUG, logfile=None):
    init_logfile()
    global LOG
    LOG.setLevel(level)

    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(levelname)s: %(message)s")
    handler.setFormatter(formatter)
    LOG.addHandler(handler)


def _check_ansible_version():
    version = ansible.__version__
    if not LooseVersion(version) >= LooseVersion(MINIMUM_ANSIBLE_VERSION):
        raise Exception("You are using ansible-playbook '%s'. "
                        "Current required version is at least: '%s'. You may "
                        "install the correct version with 'pip install -U -r "
                        "requirements.txt'" % (
                            version, MINIMUM_ANSIBLE_VERSION))


def _append_envvar(key, value):
    if key in os.environ:
        os.environ[key] = "%s %s" % (os.environ[key], value)
    else:
        _set_envvar(key, value)


def _set_envvar(key, value):
    os.environ[key] = value


def _set_default_env():
    cm_path = os.path.expanduser('~/.ssh/controlmasters')
    if not os.path.exists(cm_path):
        os.makedirs(cm_path)

    _set_envvar('PYTHONUNBUFFERED', '1')  # needed in order to stream output
    _set_envvar('PYTHONIOENCODING', 'UTF-8')  # needed to handle stdin input
    _set_envvar('ANSIBLE_FORCE_COLOR', 'yes')
    _append_envvar('ANSIBLE_SSH_ARGS', '-o ControlMaster=auto')
    _append_envvar("ANSIBLE_SSH_ARGS",
                   "-o ControlPath=~/.ssh/controlmasters/u-%r@%h:%p")
    _append_envvar("ANSIBLE_SSH_ARGS", "-o ControlPersist=300")


def test_ssh(host, user, key_file):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(AutoAddPolicy())
    try:
        ssh.connect(hostname=host, username=user, key_filename=key_file)
        LOG.debug("Connect to {0} successfully".format(host))
        return True
    except (BadHostKeyException, AuthenticationException,
        SSHException, socket.error) as e:
            LOG.debug(e)
            LOG.debug("Connect to {0} failed. Retrying... ".format(host))


def _run_ansible(inventory, playbook, user='root', module_path='./library',
                 sudo=False, extra_args=[]):
    command = [
        'ansible-playbook',
        '--inventory-file',
        inventory,
        '--user',
        user,
        '--module-path',
        module_path,
        playbook,
    ]

    if sudo:
        command.extend(['--become', '--become-method', 'sudo'])
    command += extra_args

    LOG.debug("Running command: %s with environment: %s",
              " ".join(command), os.environ)
    proc = subprocess.Popen(command, env=os.environ.copy(), shell=False,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)

    for line in iter(proc.stdout.readline, b''):
        print line.rstrip()

    proc.communicate()[0]
    return proc.returncode


def _run_module(inventory, module, module_args, module_hosts='all',
                user='root', module_path='./library', sudo=False,
                extra_args=[]):
    command = [
        'ansible',
        module_hosts,
        '--inventory-file',
        inventory,
        '--module-name',
        module,
        '--user',
        user,
        '--module-path',
        module_path,
    ]

    if sudo:
        command.extend(['--become', '--become-method', 'sudo'])
    command.extend(extra_args)
    command.extend(["--args='{}'".format(module_args)])

    LOG.debug("Running command: %s with environment: %s",
              " ".join(command), os.environ)
    proc = subprocess.Popen(" ".join(command), env=os.environ.copy(),
                            shell=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)

    for line in iter(proc.stdout.readline, b''):
        print line.rstrip()

    proc.communicate()[0]
    return proc.returncode


def _vagrant_ssh_config(environment, vms):
    v = vagrant.Vagrant()
    rel_ssh_config_file = os.path.join(environment, ".ssh_config")
    ssh_config_file = os.path.abspath(rel_ssh_config_file)

    ssh_config = []
    for vm in vms:
        ssh_config.append(v.ssh_config(vm_name=vm))
    f = open(ssh_config_file, 'w')
    for server in ssh_config:
        f.write("%s\n" % server.rstrip())
    f.close()

    if 'not yet ready for SSH' in open(ssh_config_file).read():
        LOG.debug("waiting for Vagrant to be ready for SSH")
        time.sleep(5)
        _vagrant_ssh_config(environment, boxes)

    _append_envvar("ANSIBLE_SSH_ARGS", "-F %s" % ssh_config_file)

    return 0


def _ssh_add(keyfile):
    command = ["ssh-add", keyfile]
    proc = subprocess.Popen(command, env=os.environ.copy(),
                            shell=False,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)

    if proc.returncode:
        raise Exception(
            "Failed to run %s with environment: %s"
            % (" ".join(command), os.environ)
        )


def _run_heat(args, hot):
    try:
        from heatclient.common import utils
        from heatclient.client import Client as Heat_Client
        from keystoneclient.v3 import Client as Keystone_Client
    except ImportError as e:
        LOG.error("You must have python-heatclient in your python path")
        raise Exception(e)

    CREDS = {
        'username': os.environ.get('OS_USERNAME'),
        'password': os.environ.get('OS_PASSWORD'),
        'tenant_name': os.environ.get(
            'OS_TENANT_NAME', os.environ.get('OS_PROJECT_NAME')
        ),
        'project_name': os.environ.get(
            'OS_TENANT_NAME', os.environ.get('OS_PROJECT_NAME')
        ),
        'auth_url': os.environ.get('OS_AUTH_URL'),
    }

    ex_msg = (
        "%s, ensure your environment (probably the stackrc file) is properly "
        "configured with OpenStack credentials"
    )
    # Get name of CREDS key with a value of None and raise an exception
    # because we're missing some obviously important creds data
    if not all(CREDS.values()):
        name = CREDS.keys()[CREDS.values().index(None)]
        namestr = "%s is missing" % name
        raise OpenStackConfigurationError(ex_msg % namestr)

    if args.heat_stack_name:
        stack_name = args.heat_stack_name
    else:
        stack_name = os.path.basename(args.environment)

    STACK = {
        'stack_name': stack_name,
        'template': hot
    }

    if args.heat_parameters:
        STACK['parameters'] = utils.format_parameters(args.heat_parameters)

    LOG.debug("Logging into heat")

    ks_client = Keystone_Client(**CREDS)
    heat_endpoint = ks_client.service_catalog.url_for(
        service_type='orchestration', endpoint_type='publicURL')
    heatclient = Heat_Client('1', heat_endpoint, token=ks_client.auth_token)

    try:
        LOG.debug("Checking for existence of heat stack: %s" % stack_name)
        heatclient.stacks.get(stack_name)
        stack_exists = True
        LOG.debug("Already exists")
    except Exception as e:
        if e.code == 404:
            stack_exists = False
        else:
            raise Exception(e)

    stack_action = 'create'

    if stack_exists and args.heat_stack_update:
        stack_action = 'update'
        LOG.debug("Updating stack")
        heatclient.stacks.update(stack_name, **STACK)
        time.sleep(5)
    elif not stack_exists:
        LOG.debug("Creating stack")
        heatclient.stacks.create(**STACK)
        time.sleep(5)

    while heatclient.stacks.get(stack_name).status == 'IN_PROGRESS':
        LOG.debug("Waiting on stack...")
        time.sleep(5)

    stack = heatclient.stacks.get(stack_name)
    if stack.status != 'COMPLETE':
        raise Exception("stack %s returned an unexpected status (%s:%s)" %
                        (stack_name, stack.status, stack.stack_status_reason))

    LOG.debug("Stack %sd!" % stack_action)

    servers = {}
    floating_ip = None
    private_key = None
    for output in stack.outputs:
        if output['output_key'] == "floating_ip":
            floating_ip = output['output_value']
            LOG.debug("floating_ip : %s" % floating_ip)
        elif output['output_key'] == "private_key":
            private_key = output['output_value']
        else:
            servers[output['output_key']] = output['output_value']
            LOG.debug("server : %s (%s) " %
                      (output['output_key'], output['output_value']))

    ssh_config = """
Host *
  User {user}
  ForwardAgent yes
  UserKnownHostsFile /dev/null
  StrictHostKeyChecking no
  PasswordAuthentication no
""".format(user=args.ursula_user)

    ssh_key_path = os.path.join(args.environment, ".ssh_key")
    ssh_config_path = os.path.join(args.environment, ".ssh_config")

    # write out private key if using one generated by heat
    if private_key:
        ssh_config += "  IdentityFile " + ssh_key_path
        LOG.debug("writing ssh_key to %s" % ssh_key_path)
        with open(ssh_key_path, "w") as text_file:
            text_file.write(private_key)
        os.chmod(ssh_key_path, 0600)
        _ssh_add(ssh_key_path)
    LOG.debug("writing ssh_config to %s" % ssh_config_path)
    with open(ssh_config_path, "w") as text_file:
        text_file.write(ssh_config)

    if floating_ip:
        ssh_config_pre = """
Host floating_ip
  Hostname {floating_ip}
"""
        ssh_config = ssh_config_pre.format(floating_ip=floating_ip)
        with open(ssh_config_path, "a") as text_file:
            text_file.write(ssh_config)

    for server, ip in servers.iteritems():
        ssh_config_pre = """
Host {server}
  Hostname {ip}
"""
        if floating_ip:
            ssh_config_pre += ("  ProxyCommand ssh -W %h:%p -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no"
                               " {user}@{floating_ip}\n\n")
        ssh_config = ssh_config_pre.format(
            server=server, ip=ip, user=args.ursula_user, floating_ip=floating_ip)
        with open(ssh_config_path, "a") as text_file:
            text_file.write(ssh_config)

    ansible_ssh_config_file = os.path.abspath(ssh_config_path)
    if os.path.isfile(ansible_ssh_config_file):
        _append_envvar("ANSIBLE_SSH_ARGS", "-F %s" % ansible_ssh_config_file)

    LOG.debug("waiting for SSH connectivity...")

    user = args.ursula_user

    start_time = time.time()
    timeout = 120
    if floating_ip:
        while not test_ssh(floating_ip, user, ssh_key_path):
            if time.time() > start_time + timeout:
                    sys.exit(-1)
            LOG.debug("waiting for SSH connectivity...")
            time.sleep(5)
    else:
        for ip in servers.itervalues():
            while not test_ssh(str(ip), user, ssh_key_path):
                if time.time() > start_time + timeout:
                    sys.exit(-1)
                LOG.debug("waiting for {} SSH connectivity...".format(ip))
                time.sleep(5)


def _vagrant_copy_yml(environment):
    src = "%s/vagrant.yml" % environment
    dest = ".vagrant/vagrant.yml"
    shutil.copy2(src, dest)


def _run_vagrant(environment):
    log_cm = vagrant.make_file_cm('vagrant.log')
    v = vagrant.Vagrant(out_cm=log_cm, err_cm=log_cm)
    vagrant_config_file = "%s/vagrant.yml" % environment

    if os.path.isfile(vagrant_config_file):
        _set_envvar("SETTINGS_FILE", vagrant_config_file)
        vagrant_config = yaml.load(open(vagrant_config_file, 'r'))
    else:
        vagrant_config = yaml.load(open('vagrant.yml', 'r'))

    vms = vagrant_config['vms'].keys()

    nothing_up = True
    log_cm = vagrant.make_file_cm('vagrant.log')
    for vm in v.status():
        if vm.state == "running":
            nothing_up = False
    print "View Vagrant status at vagrant.log"
    if nothing_up:
        print "Starting %s" % vms
        v.up()
    else:
        for vm in vms:
            state = v.status(vm_name=vm)[0].state
            if state == "not_created":
                print "Starting %s" % vm
                v.up(vm_name=vm)

    rc = _vagrant_ssh_config(environment, vms)
    if rc:
        return rc

    print "**************************************************"
    print "Ursula <3 Vagrant"
    print "To interact with your environment via Vagrant set:"
    print "$ export SETTINGS_FILE=%s" % vagrant_config_file
    print "**************************************************"

    return 0


def run(args, extra_args):
    _set_default_env()

    if not os.path.exists(args.environment):
        raise Exception("Environment '%s' does not exist" % args.environment)

    args.environment = args.environment.rstrip('/').rstrip('\\')

    _set_envvar('URSULA_ENV', os.path.abspath(args.environment))

    inventory = os.path.join(args.environment, 'hosts')
    if not os.path.exists(inventory) or not os.path.isfile(inventory):
        raise Exception("Inventory file '%s' does not exist" % inventory)

    if args.ursula_ssh_config:
        rel_ansible_ssh_config_file = args.ursula_ssh_config
    else:
        rel_ansible_ssh_config_file = os.path.join(args.environment,
                                                   'ssh_config')
    ansible_ssh_config_file = os.path.abspath(rel_ansible_ssh_config_file)
    if os.path.isfile(ansible_ssh_config_file):
        _append_envvar("ANSIBLE_SSH_ARGS", "-F %s" % ansible_ssh_config_file)

    static_known_hosts_file = os.path.abspath(
        os.path.join(args.environment, 'known_hosts')
    )
    if os.path.isfile(static_known_hosts_file):
        _append_envvar("ANSIBLE_SSH_ARGS",
                       "-o UserKnownHostsFile=%s" % static_known_hosts_file)

    if args.ursula_forward:
        _append_envvar("ANSIBLE_SSH_ARGS", "-o ForwardAgent=yes")

    if args.ursula_test:
        extra_args += ['--syntax-check', '--list-tasks']

    if args.provisioner == "vagrant":
        if os.path.exists('envs/example/vagrant.yml'):
            if os.path.isfile('envs/example/vagrant.yml'):
                extra_args += ['--extra-vars', '@envs/example/vagrant.yml']
        rc = _run_vagrant(environment=args.environment)
        if rc:
            return rc
        _vagrant_copy_yml(args.environment)
        if not args.ursula_user:
            args.ursula_user = "vagrant"
        if not args.ursula_sudo:
            args.ursula_sudo = True
    if args.provisioner == "heat":
        heat_file = "%s/heat_stack.yml" % args.environment
        if not os.path.exists(heat_file):
            raise Exception(
                "heat provider requires a heat file at %s" % heat_file)
        with open(heat_file, "r") as myfile:
            hot = myfile.read()
        heat_extra_args = "%s/vars_heat.yml" % args.environment
        if os.path.exists(heat_extra_args) and os.path.isfile(heat_extra_args):
            extra_args += ['--extra-vars', '@%s' % heat_extra_args]
        rc = _run_heat(args=args, hot=hot)
        if rc:
            return rc
        if not args.ursula_user:
            args.ursula_user = "ubuntu"
        if not args.ursula_sudo:
            args.ursula_sudo = True
    else:
        if not args.ursula_user:
            args.ursula_user = "root"
    if args.adhoc:
        args.module = "shell"
        args.module_args = args.adhoc
    if args.module:
        if not args.module_args:
            raise Exception(
                "--module also requires --module-args")
        if not args.module_hosts:
            args.module_hosts = "all"
        rc = _run_module(inventory, args.module, module_args=args.module_args,
                         module_hosts=args.module_hosts, extra_args=extra_args,
                         user=args.ursula_user, sudo=args.ursula_sudo)
    else:
        rc = _run_ansible(inventory, args.playbook, extra_args=extra_args,
                          user=args.ursula_user, sudo=args.ursula_sudo)
    return rc


def parse_args():
    parser = argparse.ArgumentParser(description='A CLI wrapper for ansible')
    parser.add_argument('environment', help='The environment you want to use')
    parser.add_argument('playbook', help='The playbook to run')
    # any args should be namespaced --ursula-$SOMETHING so as not to conflict
    # with ansible-playbook's command line parameters
    parser.add_argument(
        '--ursula-user', help='The user to run as', default=None)
    parser.add_argument('--ursula-ssh-config', help='path to your ssh config')
    parser.add_argument('--ursula-forward', action='store_true',
                        help='Forward SSH agent')
    parser.add_argument('--ursula-test', action='store_true',
                        help='Test syntax for playbook')
    parser.add_argument('--ursula-debug', action='store_true',
                        help='Run this tool in debug mode')
    parser.add_argument('--provisioner',
                        help='The external provisioner to use',
                        default=None, choices=["vagrant", "heat"])
    parser.add_argument('--adhoc',
                        help='Alias for --module=shell --module-args=',
                        default=None)
    parser.add_argument('--module',
                        help='run an arbitrary ansible module.',
                        default=None)
    parser.add_argument('--module-args',
                        help='args to pass to arbitrary module',
                        default=None)
    parser.add_argument('--module-hosts',
                        help='host pattern for arbitrary module',
                        default=None)
    parser.add_argument(
        '--heat-stack-name', default=None,
        help='Name of the heat stack when heat provisioner is used',
    )
    parser.add_argument(
        '--heat-stack-update', default=False, action='store_true',
        help='Update the heat stack',
    )
    parser.add_argument(
        '--heat-parameters', metavar='<KEY1=VALUE1;KEY2=VALUE2...>',
           help='Parameter values used to create the stack. '
                'This can be specified multiple times, or once with '
                'parameters separated by a semicolon.',
           action='append')
    parser.add_argument('--vagrant', action='store_true',
                        help='Provision environment in vagrant')
    parser.add_argument('--ursula-sudo', action='store_true',
                        help='Enable sudo')
    return parser.parse_known_args()


def main():
    args, extra_args = parse_args()
    try:
        log_level = logging.INFO
        if args.ursula_debug:
            log_level = logging.DEBUG
        _initialize_logger(log_level)
        if args.vagrant:
            LOG.warn("--vagrant is depreciated, use --provisioner=vagrant")
            args.provisioner = "vagrant"
        _check_ansible_version()
        if not args.ursula_user:
            if args.provisioner == 'vagrant':
                args.ursula_user = 'vagrant'
            else:
                args.ursula_user = 'ubuntu'
        rc = run(args, extra_args)
        sys.exit(rc)
    except Exception as e:
        LOG.error(e)
        sys.exit(-1)


if __name__ == '__main__':
    main()
