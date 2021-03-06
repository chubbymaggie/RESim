import os
import ConfigParser
RESIM_REPO = os.getenv('RESIM')
CORE = os.path.join(RESIM_REPO, 'simics/monitorCore')
if CORE not in sys.path:
    print("using CORE of %s" % CORE)
    sys.path.append(CORE)
import genMonitor
'''
 * This software was created by United States Government employees
 * and may not be copyrighted.
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
 * IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
 * WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT,
 * INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
 * (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
 * SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
 * HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
 * STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
 * ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 * POSSIBILITY OF SUCH DAMAGE.
'''
'''
Intended to be invoked by from a Simics workspace, e.g., via a bash script.
The workspace must contain a configuration file named $RESIM_TARGET.ini
That ini file must include and ENV section and a section for each
component in the simulation.  
'''

class LinkObject():
    def __init__(self, name):
        self.name = name
        cmd = '%s' % name
        self.obj = SIM_run_command(cmd)
        print('self.obj is %s' % self.obj)

def doEthLink(target, eth):
    name = '$%s_%s' % (target, eth)
    cmd = '%s = $%s' % (name, eth)
    run_command(cmd)
    link_object = LinkObject(name)
    return link_object
    
def doSwitch(target, switch):
    name = '$%s_%s' % (target, switch)
    cmd = '%s = $%s_con' % (name, switch)
    run_command(cmd)
    link_object = LinkObject(name)
    return link_object
    
def assignLinkNames(target):
    link_names = []
    link_names.append(doEthLink(target, 'eth0'))
    link_names.append(doEthLink(target, 'eth1'))
    link_names.append(doEthLink(target, 'eth2'))
    link_names.append(doSwitch(target, 'switch0'))
    link_names.append(doSwitch(target, 'switch1'))
    link_names.append(doSwitch(target, 'switch2'))
    return link_names

def linkSwitches(target, comp_dict):
    if comp_dict['ETH0_SWITCH'] != 'NONE':
        cmd = 'connect $eth0 cnt1 = $%s_con' %  comp_dict['ETH0_SWITCH']
        run_command(cmd)
    if comp_dict['ETH1_SWITCH'] != 'NONE':
        cmd = 'connect $eth1 cnt1 = $%s_con' %  comp_dict['ETH1_SWITCH']
        run_command(cmd)
    if comp_dict['ETH2_SWITCH'] != 'NONE':
        cmd = 'connect $eth2 cnt1 = $%s_con' %  comp_dict['ETH2_SWITCH']
        run_command(cmd)
 
   
def createDict(config): 
    comp_dict = {}
    if config.has_section('driver'):
        comp_dict['driver'] = {}
        for name, value in config.items('driver'):
            comp_dict['driver'][name] = value
    for section in config.sections():
        if section in not_a_target and section != 'driver':
            continue
        comp_dict[section] = {}
        print('assign %s CLI variables' % section)
        ''' hack defaults, Simics CLI has no undefine operation '''
        comp_dict[section]['ETH0_SWITCH'] = 'switch0'
        comp_dict[section]['ETH1_SWITCH'] = 'switch1'
        comp_dict[section]['ETH2_SWITCH'] = 'switch2'
        for name, value in config.items(section):
            comp_dict[section][name] = value
    return comp_dict

print('Launch RESim')
SIMICS_WORKSPACE = os.getenv('SIMICS_WORKSPACE')
RESIM_TARGET = os.getenv('RESIM_TARGET')
config = ConfigParser.ConfigParser()
config.optionxform = str
if not RESIM_TARGET.endswith('.ini'):
    ini_file = '%s.ini' % RESIM_TARGET
else:
    ini_file = RESIM_TARGET
cfg_file = os.path.join(SIMICS_WORKSPACE, ini_file)
config.read(cfg_file)



run_command('add-directory -prepend %s/simics/simicsScripts' % RESIM_REPO)
run_command('add-directory -prepend %s/simics/monitorCore' % RESIM_REPO)
run_command('add-directory -prepend %s' % SIMICS_WORKSPACE)

print('assign ENV variables')
for name, value in config.items('ENV'):
    os.environ[name] = value
    #print('assigned %s to %s' % (name, value))

RUN_FROM_SNAP = os.getenv('RUN_FROM_SNAP')

not_a_target=['ENV', 'driver']

comp_dict = createDict(config)
link_dict = {}
if RUN_FROM_SNAP is None:
    run_command('run-command-file ./targets/x86-x58-ich10/create_switches.simics')
    run_command('set-min-latency min-latency = 0.01')
    if config.has_section('driver'):
        run_command('$eth_dev=i82543gc')
        for name in comp_dict['driver']:
            value = comp_dict['driver'][name]
            if name.startswith('$'):
                cmd = "%s=%s" % (name, value)
                run_command(cmd)

        print('Start the %s' % config.get('driver', '$host_name'))
        run_command('run-command-file ./targets/%s' % config.get('driver','SIMICS_SCRIPT'))
        run_command('start-agent-manager')
        done = False
        count = 0
        while not done: 
            run_command('c 50000000000')
            if os.path.isfile('driver-ready.flag'):
                done = True 
            count += 1
            print count
        link_dict['driver'] = assignLinkNames('driver')
        linkSwitches('driver', comp_dict['driver'])
    for section in config.sections():
        if section in not_a_target:
            continue
        print('assign %s CLI variables' % section)
        ''' hack defaults, Simics CLI has no undefine operation '''
        run_command('$eth_dev=i82543gc')
        for name in comp_dict[section]:
            value = comp_dict[section][name]
            if name.startswith('$'):
                cmd = "%s=%s" % (name, value)
                run_command(cmd)
        print('Start the %s' % section)  
        run_command('run-command-file ./targets/%s' % config.get(section,'SIMICS_SCRIPT'))
        link_dict[section] = assignLinkNames(section)
        linkSwitches(section, comp_dict[section])
else:
    print('run from checkpoint %s' % RUN_FROM_SNAP)
    run_command('read-configuration %s' % RUN_FROM_SNAP)
    #run_command('run-command-file ./targets/x86-x58-ich10/switches.simics')
run_command('log-level 0 -all')
'''
Either launch monitor, or generate kernel parameter file depending on CREATE_RESIM_PARAMS
'''
CREATE_RESIM_PARAMS = os.getenv('CREATE_RESIM_PARAMS')
if CREATE_RESIM_PARAMS is not None and CREATE_RESIM_PARAMS.upper() == 'YES':
    gkp = getKernelParams.GetKernelParams()
else:
    cgc = genMonitor.GenMonitor(comp_dict, link_dict)
    cgc.doInit()

