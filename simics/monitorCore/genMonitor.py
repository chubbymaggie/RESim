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
Use Simics to monitor processes.  The is the top level module for RESim,
derived from cgcMonitor, which was developed for the DARPA Cyber Grand Challenge.
'''
from simics import *
import os
import errno
import struct
import resim_utils
import memUtils
import taskUtils
import genContextMgr
import bookmarkMgr
import isMonitorRunning
import reverseToCall
import reverseToAddr
import pFamily
import traceOpen
import pageFaultGen
import hapCleaner
import reverseToUser
import findKernelWrite
import syscall
import traceProcs
import cloneChild
import soMap
import elfText
import stopFunction
import trackThreads
import dataWatch
import traceFiles
import stackTrace
import exitMaze
import net
import sharedSyscall
import idaFuns
import traceMgr
import binder
import connector
import diddler
import targetFS

import json
import pickle

class cellConfig():
    '''
    Manage the Simics simulation cells (boxes), CPU's (processor cores).
    TBD expand for multi-processor cells
    '''
    cell_cpu = {}
    cpu_cell = {}
    cell_cpu_list = {}
    cell_context = {}
    def __init__(self, comp_list):
        self.cells = list(comp_list)
        self.loadCellObjects()

    def loadCellObjects(self):
        for cell_name in self.cells:
            obj = SIM_get_object(cell_name)
            self.cell_context[cell_name] = obj.cell_context

        for cell_name in self.cells:
            cmd = '%s.get-processor-list' % cell_name
            proclist = SIM_run_command(cmd)
            cpu = SIM_get_object(proclist[0])
            self.cell_cpu[cell_name] = cpu
            self.cpu_cell[cpu] = cell_name
            self.cell_cpu_list[cell_name] = []
            for proc in proclist:
                self.cell_cpu_list[cell_name].append(SIM_get_object(proc))

    def cpuFromCell(self, cell_name):
        ''' simplification for single-core sims '''
        return self.cell_cpu[cell_name]

    def getCells(self):
        return self.cells

    def cellFromCPU(self, cpu):
        return self.cpu_cell[cpu]
        

class Prec():
    def __init__(self, cpu, proc, pid=None):
        self.cpu = cpu
        self.proc = proc
        self.pid = pid
        self.debugging = False

class GenMonitor():
    ''' Top level RESim class '''
    SIMICS_BUG=False
    PAGE_SIZE = 4096
    def __init__(self, comp_dict, link_dict):
        self.comp_dict = comp_dict
        self.link_dict = link_dict
        self.param = {}
        self.mem_utils = {}
        self.task_utils = {}
        self.context_manager = {}
        #self.proc_list = {}
        self.proc_hap = None
        self.stop_proc_hap = None
        self.proc_break = None
        self.gdb_mailbox = None
        self.stop_hap = None
        self.log_dir = '/tmp/'
        self.mode_hap = None
        self.hack_list = []
        self.traceOpen = {}
        self.sysenter_cycles = {}
        self.traceMgr = {}
        self.soMap = {}
        self.page_faults = {}
        self.rev_to_call = {}
        self.pfamily = {}
        self.traceOpen = {}
        self.traceProcs = {}
        self.dataWatch = {}
        self.traceFiles = {}
        self.sharedSyscall = {}



        ''' dict of syscall.SysCall keyed on call number '''
        self.call_traces = {}
        self.unistd = {}
        self.targetFS = {}
        self.trace_all = {}
        self.track_threads = {}
        self.exit_group_syscall = {}
        self.debug_breaks_set = True
        self.target = None
        self.netInfo = {}
        self.stack_base = {}
        self.maze_exits = {}
        self.exit_maze = []
        self.rev_execution_enabled = False
        self.run_from_snap = None
        self.ida_funs = None
        self.binders = binder.Binder()
        self.connectors = connector.Connector()
        self.auto_maze=False

        self.bookmarks = None

        self.genInit(comp_dict)
        self.reg_list = None

    def genInit(self, comp_dict):
        '''
        remove all previous breakpoints.  
        '''
        self.lgr = resim_utils.getLogger('noname', os.path.join(self.log_dir, 'monitors'))
        self.is_monitor_running = isMonitorRunning.isMonitorRunning(self.lgr)
        SIM_run_command("delete -all")
        self.target = os.getenv('RESIM_TARGET')
        print('using target of %s' % self.target)
        self.cell_config = cellConfig(list(comp_dict.keys()))
        target_cpu = self.cell_config.cpuFromCell(self.target)
        self.lgr.debug('New log, in genInit')
        self.run_from_snap = os.getenv('RUN_FROM_SNAP')
        if self.run_from_snap is not None:
            net_link_file = os.path.join('./', self.run_from_snap, 'net_link.pickle')
            if os.path.isfile(net_link_file):
                self.net_links = pickle.load( open(net_link_file, 'rb') )
                for target in self.net_links:
                    for link in self.net_links[target]:
                        cmd = '%s = %s' % (link.name, link.obj)
                        self.lgr.debug('genInit link cmd is %s' % cmd)
                        SIM_run_command(cmd)

        for cell_name in comp_dict:
            if 'RESIM_PARAM' in comp_dict[cell_name]:
                param_file = comp_dict[cell_name]['RESIM_PARAM']
                print('Cell %s using params from %s' % (cell_name, param_file))
                self.lgr.debug('Cell %s using params from %s' % (cell_name, param_file))
                if not os.path.isfile(param_file):
                    print('Could not find param file at %s' % param_file)
                    self.lgr.debug('Could not find param file at %s' % param_file)
                    return
                self.param[cell_name] = pickle.load( open(param_file, 'rb') ) 
                self.lgr.debug(self.param[cell_name].getParamString())
            else:
                print('Cell %s missing params, it will not be monitored. ' % (cell_name))
                self.lgr.debug('Cell %s missing params ' % (cell_name))
                continue 
            word_size = 4
            if 'OS_TYPE' in comp_dict[cell_name]:
                if comp_dict[cell_name]['OS_TYPE'] == 'LINUX64':
                    word_size = 8

            cpu = self.cell_config.cpuFromCell(cell_name)
            self.mem_utils[cell_name] = memUtils.memUtils(word_size, self.param[cell_name], self.lgr, arch=cpu.architecture)
            self.traceMgr[cell_name] = traceMgr.TraceMgr(self.lgr)

            self.unistd[cell_name] = comp_dict[cell_name]['RESIM_UNISTD']
            root_prefix = comp_dict[cell_name]['RESIM_ROOT_PREFIX']
            self.targetFS[cell_name] = targetFS.TargetFS(root_prefix)
            self.lgr.debug('targetFS for %s is %s' % (cell_name, self.targetFS[cell_name]))

            self.netInfo[cell_name] = net.NetAddresses(self.lgr)
            self.call_traces[cell_name] = {}
            #self.proc_list[cell_name] = {}
            self.stack_base[cell_name] = None
            if self.run_from_snap is not None:
                net_file = os.path.join('./', self.run_from_snap, cell_name, 'net_list.pickle')
                if os.path.isfile(net_file):
                    self.netInfo[cell_name].loadfile(net_file)

    def getTopComponentName(self, cpu):
         if cpu is not None:
             names = cpu.name.split('.')
             return names[0]
         else:
             return None

    def stopModeChanged(self, stop_action, one, exception, error_string):
        cpu, comm, this_pid = self.task_utils[self.target].curProc() 
        eip = self.mem_utils[self.target].getRegValue(cpu, 'eip')
        instruct = SIM_disassemble_address(cpu, eip, 1, 0)
        self.lgr.debug('stopModeChanged eip 0x%x %s' % (eip, instruct[1]))
        SIM_run_alone(SIM_run_command, 'c')

    def modeChangeReport(self, want_pid, one, old, new):
        cpu, comm, this_pid = self.task_utils[self.target].curProc() 
        if want_pid != this_pid:
            self.lgr.debug('mode changed wrong pid, wanted %d got %d' % (want_pid, this_pid))
            return
        new_mode = 'user'
        if new == Sim_CPU_Mode_Supervisor:
            new_mode = 'kernel'
            SIM_break_simulation('mode changed')
        eip = self.mem_utils[self.target].getRegValue(cpu, 'eip')
        callnum = self.mem_utils[self.target].getRegValue(cpu, 'syscall_num')
        instruct = SIM_disassemble_address(cpu, eip, 1, 0)
        self.lgr.debug('modeChangeReport new mode: %s  eip 0x%x %s --  eax 0x%x' % (new_mode, eip, instruct[1], callnum))

    def modeChanged(self, want_pid, one, old, new):
        cpu, comm, this_pid = self.task_utils[self.target].curProc() 
        if want_pid != this_pid:
            self.lgr.debug('mode changed wrong pid, wanted %d got %d' % (want_pid, this_pid))
            return
        cpl = memUtils.getCPL(cpu)
        eip = self.mem_utils[self.target].getRegValue(cpu, 'eip')
        mode = 1
        if new == Sim_CPU_Mode_Supervisor:
            mode = 0
        self.lgr.debug('mode changed cpl reports %d hap reports %d  trigger_obj is %s old: %d  new: %d  eip: 0x%x' % (cpl, mode, str(one), old, new, eip))
        SIM_break_simulation('mode changed, break simulation')
        
    def stopHap(self, stop_action, one, exception, error_string):
        if stop_action is None or stop_action.hap_clean is None:
            print('stopHap error, stop_action None?')
            return 
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        eip = self.getEIP(cpu)
        self.lgr.debug('stopHap pid %d eip 0x%x cycle: 0x%x' % (pid, eip, stop_action.hap_clean.cpu.cycles))
        for hc in stop_action.hap_clean.hlist:
            if hc.hap is not None:
                if hc.htype == 'GenContext':
                    self.lgr.debug('genMonitor stopHap delete GenContext hap %s' % str(hc.hap))
                    self.context_manager[self.target].genDeleteHap(hc.hap)
                else:
                    #self.lgr.debug('stopHap will delete hap %s' % str(hc.hap))
                    SIM_hap_delete_callback_id(hc.htype, hc.hap)
                hc.hap = None
        if self.stop_hap is not None:
            self.lgr.debug('genMonitor stopHap will delete hap %s' % str(self.stop_hap))
            SIM_hap_delete_callback_id("Core_Simulation_Stopped", self.stop_hap)
            self.stop_hap = None
            for bp in stop_action.breakpoints:
                SIM_delete_breakpoint(bp)
            ''' check functions in list '''
            self.lgr.debug('stopHap now run actions %s' % str(stop_action.flist))
            stop_action.run()
            self.lgr.debug('back from stop_action.run')

    def run2Kernel(self, cpu):
        cpl = memUtils.getCPL(cpu)
        if cpl != 0:
            cpu, comm, pid = self.task_utils[self.target].curProc() 
            self.lgr.debug('run2Kernel in user space (%d), set hap' % cpl)
            self.mode_hap = SIM_hap_add_callback_obj("Core_Mode_Change", cpu, 0, self.modeChanged, pid)
            hap_clean = hapCleaner.HapCleaner(cpu)
            hap_clean.add("Core_Mode_Change", self.mode_hap)
            stop_action = hapCleaner.StopAction(hap_clean, None)
            self.stop_hap = SIM_hap_add_callback("Core_Simulation_Stopped", 
        	     self.stopHap, stop_action)
            SIM_continue(0)
        else:
            self.lgr.debug('run2Kernel, already in kernel')

    def run2User(self, cpu, flist=None):
        cpl = memUtils.getCPL(cpu)
        if cpl == 0:
            dumb, dumb, pid = self.task_utils[self.target].curProc() 
            ''' use debug process if defined, otherwise default to current process '''
            debug_pid, dumb, dumb = self.context_manager[self.target].getDebugPid() 
            if debug_pid is not None:
                if debug_pid != pid:
                    ''' debugging, but not this pid.  likely a clone '''
                    if not self.context_manager[self.target].amWatching(pid):
                        ''' stick with original debug pid '''
                        pid = debug_pid
                    
            self.lgr.debug('run2User pid %d in kernel space (%d), set hap' % (pid, cpl))
            self.mode_hap = SIM_hap_add_callback_obj("Core_Mode_Change", cpu, 0, self.modeChanged, pid)
            hap_clean = hapCleaner.HapCleaner(cpu)
            hap_clean.add("Core_Mode_Change", self.mode_hap)
            stop_action = hapCleaner.StopAction(hap_clean, None, flist)
            self.stop_hap = SIM_hap_add_callback("Core_Simulation_Stopped", 
        	     self.stopHap, stop_action)
            SIM_run_alone(SIM_run_command, 'continue')
        else:
            self.lgr.debug('run2User, already in user')
            if flist is not None and len(flist) == 1:
                flist[0].fun(flist[0].args)

    def finishInit(self, cell_name):
        
            self.lgr.debug('finishInit for cell %s' % cell_name)
            if cell_name not in self.param: 
                return
            cpu = self.cell_config.cpuFromCell(cell_name)
            cell = self.cell_config.cell_context[cell_name]
            #self.task_utils[cell_name] = taskUtils.TaskUtils(cpu, cell_name, self.param[cell_name], self.mem_utils[cell_name], 
            #      self.unistd[cell_name], self.run_from_snap, self.lgr)
 
            tu_cur_task_rec = self.task_utils[cell_name].getCurTaskRec()
            if tu_cur_task_rec is None:
                self.lgr.error('could not read tu_cur_task_rec from taskUtils')
                return

            cur_task_rec = self.mem_utils[cell_name].getCurrentTask(self.param[cell_name], cpu)
            self.lgr.debug('stack based rec was 0x%x  mine is 0x%x' % (cur_task_rec, tu_cur_task_rec))
            ''' manages setting haps/breaks based on context swtiching.  TBD will be one per cpu '''
        
            self.context_manager[cell_name] = genContextMgr.GenContextMgr(self, cell_name, self.task_utils[cell_name], self.param[cell_name], cpu, self.lgr) 
            self.page_faults[cell_name] = pageFaultGen.PageFaultGen(self, cell_name, self.param[cell_name], self.cell_config, self.mem_utils[cell_name], 
                   self.task_utils[cell_name], self.context_manager[cell_name], self.lgr)
            self.rev_to_call[cell_name] = reverseToCall.reverseToCall(self, self.param[cell_name], self.task_utils[cell_name], 
                 self.PAGE_SIZE, self.context_manager[cell_name], 'revToCall', self.is_monitor_running, None, self.log_dir)
            self.pfamily[cell_name] = pFamily.Pfamily(cell, self.param[cell_name], self.cell_config, self.mem_utils[cell_name], self.task_utils[cell_name], self.lgr)
            self.traceOpen[cell_name] = traceOpen.TraceOpen(self.param[cell_name], self.mem_utils[cell_name], self.task_utils[cell_name], cpu, cell, self.lgr)
            #self.traceProcs[cell_name] = traceProcs.TraceProcs(cell_name, self.lgr, self.proc_list[cell_name], self.run_from_snap)
            self.traceProcs[cell_name] = traceProcs.TraceProcs(cell_name, self.lgr, self.run_from_snap)
            self.soMap[cell_name] = soMap.SOMap(cell_name, self.context_manager[cell_name], self.task_utils[cell_name], self.targetFS[cell_name], self.run_from_snap, self.lgr)
            self.dataWatch[cell_name] = dataWatch.DataWatch(self, cpu, self.PAGE_SIZE, self.context_manager[cell_name], self.lgr)
            self.traceFiles[cell_name] = traceFiles.TraceFiles(self.traceProcs[cell_name], self.lgr)
            self.sharedSyscall[cell_name] = sharedSyscall.SharedSyscall(self, cpu, cell, cell_name, self.param[cell_name], 
                  self.mem_utils[cell_name], self.task_utils[cell_name], 
                  self.context_manager[cell_name], self.traceProcs[cell_name], self.traceFiles[cell_name], 
                  self.soMap[cell_name], self.dataWatch[cell_name], self.traceMgr[cell_name], self.lgr)


    def doInit(self):
        self.lgr.debug('genMonitor doInit')
        #SIM_run_command('pselect cpu-name = %s' % cpu.name)
        #run_cycles = 90000000
        run_cycles =  9000000
        #run_cycles = 900000
        done = False
        while not done:
            done = True
            for cell_name in self.cell_config.cell_context:
                if cell_name not in self.param:
                    continue
                if cell_name in self.task_utils:
                    continue
                cpu = self.cell_config.cpuFromCell(cell_name)
                ''' run until we get something sane '''
                cur_task_rec = self.mem_utils[cell_name].getCurrentTask(self.param[cell_name], cpu)
                if cur_task_rec is None or cur_task_rec == 0:
                    #print('Current task not yet defined, continue')
                    #self.lgr.debug('doInit Current task not yet defined, continue')
                    done = False
                else:
                    pid = self.mem_utils[cell_name].readWord32(cpu, cur_task_rec + self.param[cell_name].ts_pid)
                    if pid is None:
                        self.lgr.debug('doInit cell %s cur_task_rec 0x%x pid None ' % (cell_name, cur_task_rec))
                        done = False
                        continue
                    self.lgr.debug('doInit cell %s pid is %d' % (cell_name, pid))
                    task_utils = taskUtils.TaskUtils(cpu, cell_name, self.param[cell_name], self.mem_utils[cell_name], 
                            self.unistd[cell_name], self.run_from_snap, self.lgr)
                    tu_cur_task_rec = task_utils.getCurTaskRec()
                    if tu_cur_task_rec is None:
                        self.lgr.debug('doInit cell %s cur_task_rec 0x%x pid %d but None from task_utils ' % (cell_name, cur_task_rec, pid))
                        done = False
                        continue
                    self.lgr.debug('doInit cell %s cur_task_rec 0x%x pid %d from task_utils 0x%x' % (cell_name, cur_task_rec, pid, tu_cur_task_rec))
                    if tu_cur_task_rec != 0:
                        self.task_utils[cell_name] = task_utils
                        self.lgr.debug('doInit Booted enough to get cur_task_rec for cell %s, now call to finishInit' % cell_name)
                        self.finishInit(cell_name)
                        if self.run_from_snap is None and 'DIDDLE' in self.comp_dict[cell_name]:
                            self.is_monitor_running.setRunning(False)
                            self.runToDiddle(self.comp_dict[cell_name]['DIDDLE'], cell_name=cell_name)
                            print('Diddle pending for cell %s, need to run forward' % cell_name)
                    else:
                        self.lgr.debug('doInit cell %s taskUtils got task rec of zero' % cell_name)
                        done = False
            if not done:
                self.lgr.debug('continue %d cycles' % run_cycles)
                SIM_continue(run_cycles)
       


    def tasks(self):
        self.lgr.debug('tasks')
        print('Tasks on cell %s' % self.target)
        tasks = self.task_utils[self.target].getTaskStructs()
        plist = {}
        for t in tasks:
            plist[tasks[t].pid] = t 
        for pid in sorted(plist):
            t = plist[pid]
            print('pid: %d taks_rec: 0x%x  comm: %s children 0x%x 0x%x' % (tasks[t].pid, t, tasks[t].comm, tasks[t].children[0], tasks[t].children[1]))
            

    def setDebugBookmark(self, mark, cpu=None, cycles=None, eip=None, steps=None):
        SIM_run_command('enable-reverse-execution')
        pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
        self.bookmarks.setDebugBookmark(mark, cpu=cpu, cycles=cycles, eip=eip, steps=steps, msg=self.context_manager[self.target].getIdaMessage())

    def debugGroup(self):
        self.debug(group=True)

    def debug(self, group=False):
        self.stopTrace()    
        pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
        cell = self.cell_config.cell_context[self.target]
        if pid is None:
            ''' Our first debug '''
            port = 9123 
            cpu, comm, pid = self.task_utils[self.target].curProc() 
            self.lgr.debug('debug for cpu %s port will be %d.  Pid is %d' % (cpu.name, port, pid))

            self.context_manager[self.target].setDebugPid(pid, self.target)
            if cpu.architecture == 'arm':
                cmd = 'new-gdb-remote cpu=%s architecture=arm port=%d' % (cpu.name, port)
            elif self.mem_utils[self.target].WORD_SIZE == 8:
                cmd = 'new-gdb-remote cpu=%s architecture=x86-64 port=%d' % (cpu.name, port)
            else:
                cmd = 'new-gdb-remote cpu=%s architecture=x86 port=%d' % (cpu.name, port)
            self.lgr.debug('cmd: %s' % cmd)
            SIM_run_command(cmd)
            cmd = 'enable-reverse-execution'
            SIM_run_command(cmd)
            self.rev_execution_enabled = True
            self.bookmarks = bookmarkMgr.bookmarkMgr(self, self.context_manager[self.target], self.lgr)
            self.setDebugBookmark('origin', cpu)
            self.bookmarks.setOrigin(cpu)
            ''' tbd read elf and pass executable pages? NO, would not determine other executable pages '''
            self.rev_to_call[self.target].setup(cpu, [], bookmarks=self.bookmarks, page_faults = self.page_faults[self.target])

            self.context_manager[self.target].watchTasks()
            if group:
                leader_pid = self.task_utils[self.target].getGroupLeaderPid(pid)
                self.lgr.debug('genManager debug, will debug entire process group under leader %d' % leader_pid)
                pid_list = self.task_utils[self.target].getGroupPids(leader_pid)
                for pid in pid_list:
                    self.context_manager[self.target].addTask(pid)

            ''' keep track of threads within our process that are created during debug session '''
            self.track_threads[self.target] = trackThreads.TrackThreads(cpu, self.target, cell, pid, self.context_manager[self.target], 
                    self.task_utils[self.target], self.mem_utils[self.target], self.param[self.target], self.traceProcs[self.target], 
                    self.soMap[self.target], self.targetFS[self.target], self.sharedSyscall[self.target], self.lgr)

            self.lgr.debug('debug set exit_group break')
            self.exit_group_syscall[self.target] = syscall.Syscall(self, self.target, cell, self.param[self.target], self.mem_utils[self.target], 
                           self.task_utils[self.target], 
                           self.context_manager[self.target], None, self.sharedSyscall[self.target], self.lgr, self.traceMgr[self.target],
                           callnum_list=[self.task_utils[self.target].syscallNumber('exit_group')], soMap=self.soMap[self.target], 
                           targetFS=self.targetFS[self.target])

            #self.watchPageFaults(pid)

            self.sharedSyscall[self.target].setDebugging(True)
 
            prog_name = self.traceProcs[self.target].getProg(pid)
            if prog_name is None or prog_name == 'unknown':
                prog_name, dumb = self.task_utils[self.target].getProgName(pid) 
                self.lgr.debug('genMonitor debug pid %d NOT in traceProcs task_utils got %s' % (pid, prog_name))
            if self.targetFS is not None and prog_name is not None:
                sindex = 0
                full_path = self.targetFS[self.target].getFull(prog_name)
                self.lgr.debug('execToText, progname is %s  full: %s' % (prog_name, full_path))
                self.getIDAFuns(full_path)
        else:
            ''' already debugging.  change to current process '''
            cpu, comm, pid = self.task_utils[self.target].curProc() 
        self.is_monitor_running.setRunning(False)


    def show(self):
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        cpl = memUtils.getCPL(cpu)
        eip = self.getEIP(cpu)
        #cur_task_rec = self.task_utils.getCurTaskRec()
        #addr = cur_task_rec+self.param.ts_group_leader
        #val = self.mem_utils.readPtr(cpu, addr)
        #print('current task 0x%x gl_addr 0x%x group_leader 0x%s' % (cur_task_rec, addr, val))
        print('cpu.name is %s PL: %d pid: %d(%s) EIP: 0x%x   current_task symbol at 0x%x (use FS: %r)' % (cpu.name, cpl, pid, 
               comm, eip, self.param[self.target].current_task, self.param[self.target].current_task_fs))
        pfamily = self.pfamily[self.target].getPfamily()
        tabs = ''
        while len(pfamily) > 0:
            prec = pfamily.pop()
            print('%s%5d  %s' % (tabs, prec.pid, prec.proc))
            tabs += '\t'



    def signalHap(self, signal_info, one, exception_number):
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        if signal_info.callnum is None:
            if exception_number in self.hack_list:
                return
            else:
               self.hack_list.append(exception_number)
        if signal_info.pid is not None:
            if pid == signal_info.pid:
                self.lgr.error('signalHap from %d (%s) signal 0x%x at 0x%x' % (pid, comm, exception_number, self.getEIP(cpu)))
                SIM_break_simulation('signal %d' % exception_number)
        else: 
           SIM_break_simulation('signal %d' % exception_number)
           self.lgr.debug('signalHap from %d (%s) signal 0x%x at 0x%x' % (pid, comm, exception_number, self.getEIP(cpu)))
         
    def readStackFrame(self):
        cpu, comm, pid = self.task_utils[self.target].curProc()
        stack_frame = self.task_utils[self.target].frameFromStackSyscall()
        frame_string = taskUtils[self.target].stringFromFrame(stack_frame)
        print(frame_string)

    def int80Hap(self, cpu, one, exception_number):
        cpu, comm, pid = self.task_utils[self.target].curProc()
        eax = self.mem_utils[self.target].getRegValue(cpu, 'eax')
        self.lgr.debug('int80Hap in proc %d (%s), eax: 0x%x' % (pid, comm, eax))
        self.lgr.debug('syscall 0x%d from %d (%s) at 0x%x ' % (eax, pid, comm, self.getEIP(cpu)))
        if eax != 5:
            return
        SIM_break_simulation('syscall')
        print('use si to get address of syscall entry, and further down look for computed call')

    def runToSyscall80(self):
        cpu = self.cell_config.cpuFromCell(self.target)
        self.lgr.debug('runToSyscall80') 
        self.scall_hap = SIM_hap_add_callback_obj_index("Core_Exception", cpu, 0,
                 self.int80Hap, cpu, 0x180) 
        hap_clean = hapCleaner.HapCleaner(cpu)
        hap_clean.add("Core_Exception", self.scall_hap)
        stop_action = hapCleaner.StopAction(hap_clean, [], None)
        self.stop_hap = SIM_hap_add_callback("Core_Simulation_Stopped", 
        	     self.stopHap, stop_action)
        status = self.is_monitor_running.isRunning()
        if not status:
            SIM_run_command('c')

    def runToSignal(self, signal=None, pid=None):
        cpu = self.cell_config.cpuFromCell(self.target)
        self.lgr.debug('runToSignal, signal given is %s' % str(signal)) 

        sig_info = syscall.SyscallInfo(cpu, pid, signal)
        #max_intr = 31
        max_intr = 1028
        if signal is None:
            sig_hap = SIM_hap_add_callback_obj_range("Core_Exception", cpu, 0,
                     self.signalHap, sig_info, 0, max_intr) 
        else:
            sig_hap = SIM_hap_add_callback_obj_index("Core_Exception", cpu, 0,
                     self.signalHap, sig_info, signal) 

        hap_clean = hapCleaner.HapCleaner(cpu)
        hap_clean.add("Core_Exception", sig_hap)
        stop_action = hapCleaner.StopAction(hap_clean, [], None)
        self.stop_hap = SIM_hap_add_callback("Core_Simulation_Stopped", 
        	     self.stopHap, stop_action)
        status = self.is_monitor_running.isRunning()
        if not status:
            SIM_run_command('c')
    
    def getIDAFuns(self, full_path):
        fun_path = full_path+'.funs'
        if not os.path.isfile(fun_path):
            ''' No functions file, check for symbolic links '''
            if os.path.islink(full_path):
                actual = os.readlink(full_path)
                fun_path = actual+'.funs'
            
        if os.path.isfile(fun_path):
            self.ida_funs = idaFuns.IDAFuns(fun_path, self.lgr)
            self.lgr.debug('getIDAFuns using IDA function analysis from %s' % fun_path)
        else:
            self.lgr.warning('No IDA function file at %s' % fun_path)
 
    def execToText(self, flist=None):
        ''' assuming we are in an exec system call, run until execution enters the
            the .text section per the elf header in the file that was execed.'''
        cpu, comm, pid  = self.task_utils[self.target].curProc()
        prog_name, dumb = self.task_utils[self.target].getProgName(pid) 
        if self.targetFS is not None:
            sindex = 0
            full_path = self.targetFS[self.target].getFull(prog_name)
            self.lgr.debug('execToText, progname is %s  full: %s' % (prog_name, full_path))

            text_segment = elfText.getText(full_path)
            if text_segment is not None:
                self.lgr.debug('execToText %s 0x%x - 0x%x' % (prog_name, text_segment.address, text_segment.address+text_segment.size))       
                self.context_manager[self.target].recordText(text_segment.address, text_segment.address+text_segment.size)
                self.soMap[self.target].addText(text_segment.address, text_segment.size, prog_name, pid)
                self.runToText(flist)
                return
            else:
                self.lgr.debug('execToText missing binary, just run to user')
                self.toUser(flist)
                return
        self.lgr.debug('execToText no information about the text segment')
        ''' If here, then no info about the text segment '''
        if flist is not None:
            stopFunction.allFuns(flist)
        

    def watchProc(self, proc):
        ''' TBD remove?  can just use debugProc and then disable reverse-exectution?  Highlight on/off on IDA '''
        plist = self.task_utils[self.target].getPidsForComm(proc)
        if len(plist) > 0:
            self.lgr.debug('watchProc process %s found, run until some instance is scheduled' % proc)
            f1 = stopFunction.StopFunction(self.toUser, [], True)
            flist = [f1]
            self.toRunningProc(proc, plist, flist)
        else:
            self.lgr.debug('watchProc no process %s found, run until execve' % proc)
            #flist = [self.toUser, self.debug]
            ''' run to the execve, then start recording shared object mmaps and run
                until we enter the text segment so we get the SO map '''
            f1 = stopFunction.StopFunction(self.execToText, [], True)
            flist = [f1]
            self.toExecve(proc, flist=flist)

    def cleanToProcHaps(self):
        SIM_delete_breakpoint(self.proc_break)
        SIM_hap_delete_callback_id("Core_Breakpoint_Memop", self.proc_hap)

    def toProc(self, proc):
        plist = self.task_utils[self.target].getPidsForComm(proc)
        if len(plist) > 0 and not (len(plist)==1 and plist[0] == self.task_utils[self.target].getExitPid()):
            self.lgr.debug('toProc process %s found, run until some instance is scheduled' % proc)
            print('%s is running.  Will continue until some instance of it is scheduled' % proc)
            f1 = stopFunction.StopFunction(self.toUser, [], True)
            flist = [f1]
            self.toRunningProc(proc, plist, flist)
        else:
            self.lgr.debug('toProc no process %s found, run until execve' % proc)
            cpu = self.cell_config.cpuFromCell(self.target)
            prec = Prec(cpu, proc, None)
            phys_current_task = self.task_utils[self.target].getPhysCurrentTask()
            self.proc_break = SIM_breakpoint(cpu.physical_memory, Sim_Break_Physical, Sim_Access_Write, 
                             phys_current_task, self.mem_utils[self.target].WORD_SIZE, 0)
            self.lgr.debug('toProc  set break at 0x%x' % (phys_current_task))
            self.proc_hap = SIM_hap_add_callback_index("Core_Breakpoint_Memop", self.runToProc, prec, self.proc_break)
        
            f1 = stopFunction.StopFunction(self.cleanToProcHaps, [], False)
            self.toExecve(proc, [f1])

    def setStackBase(self):
        ''' debug cpu not yet set.  TBD align with debug cpu selection strategy '''
        cpu = self.cell_config.cpuFromCell(self.target)
        esp = self.mem_utils[self.target].getRegValue(cpu, 'esp')
        eip = self.mem_utils[self.target].getRegValue(cpu, 'eip')
        self.stack_base[self.target] = esp
        self.lgr.debug('setStackBase to 0x%x eip is 0x%x' % (esp, eip))

        
    def debugProc(self, proc):
        self.stopTrace()
        plist = self.task_utils[self.target].getPidsForComm(proc)
        if len(plist) > 0 and not (len(plist)==1 and plist[0] == self.task_utils[self.target].getExitPid()):
            self.lgr.debug('debugProc plist len %d plist[0] %d  exitpid %d' % (len(plist), plist[0], self.task_utils[self.target].getExitPid()))

            self.lgr.debug('debugProc process %s found, run until some instance is scheduled' % proc)
            print('%s is running.  Will continue until some instance of it is scheduled' % proc)
            f1 = stopFunction.StopFunction(self.toUser, [], True)
            f2 = stopFunction.StopFunction(self.debug, [], False)
            flist = [f1, f2]
            self.toRunningProc(proc, plist, flist)
        else:
            self.lgr.debug('debugProc no process %s found, run until execve' % proc)
            #flist = [self.toUser, self.debug]
            ''' run to the execve, then start recording shared object mmaps and run
                until we enter the text segment so we get the SO map '''
            f1 = stopFunction.StopFunction(self.execToText, [], True)
            f2 = stopFunction.StopFunction(self.setStackBase, [], False)
            f3 = stopFunction.StopFunction(self.debug, [], False)
            flist = [f1, f2, f3]
            self.toExecve(proc, flist=flist)

    def debugThis(self):
        ''' Intended for use while debugging a process that clones and you want to only watch 
            the current clone '''
        self.context_manager.watchOnlyThis()
        print('now debugging only:')
        self.show()
 
    def debugPid(self, pid):
        self.debugPidList([pid], self.debug)

    def debugPidGroup(self, pid):
        leader_pid = self.task_utils[self.target].getGroupLeaderPid(pid)
        self.lgr.debug('debugPidGroup cell %s pid %d found leader %d' % (self.target, pid, leader_pid))
        pid_list = self.task_utils[self.target].getGroupPids(leader_pid)
        self.debugPidList(pid_list, self.debugGroup)

    def debugPidList(self, pid_list, debug_function):
        self.stopTrace()
        self.lgr.debug('debugPidList cell %s' % self.target)
        f1 = stopFunction.StopFunction(self.toUser, [], True)
        f2 = stopFunction.StopFunction(debug_function, [], False)
        flist = [f1, f2]
        debug_group = False
        if debug_function == self.debugGroup:
            debug_group = True
        self.toRunningProc(None, pid_list, flist, debug_group=True)

    def changedThread(self, cpu, third, forth, memory):
        cur_addr = SIM_get_mem_op_value_le(memory)
        pid = self.mem_utils[self.target].readWord32(cpu, cur_addr + self.param[self.target].ts_pid)
        if pid != 0:
            print('changedThread')
            self.show()

    def runToProc(self, prec, third, forth, memory):
        ''' callback when current_task is updated.  new value is in memory parameter '''
        cpu = prec.cpu
        cur_task_rec = SIM_get_mem_op_value_le(memory)
        pid = self.mem_utils[self.target].readWord32(cpu, cur_task_rec + self.param[self.target].ts_pid)
        #self.lgr.debug('runToProc look for %s pid is %d' % (prec.proc, pid))
        if pid != 0:
            comm = self.mem_utils[self.target].readString(cpu, cur_task_rec + self.param[self.target].ts_comm, 16)
            if (prec.pid is not None and pid in prec.pid) or (prec.pid is None and comm == prec.proc):
                self.lgr.debug('got proc %s pid is %d' % (comm, pid))
                SIM_break_simulation('found %s' % prec.proc)
            else:
                #self.proc_list[self.target][pid] = comm
                #self.lgr.debug('runToProc pid: %d proc: %s' % (pid, comm))
                pass
            
    #def addProcList(self, pid, comm):
    #    #self.lgr.debug('addProcList %d %s' % (pid, comm))
    #    self.proc_list[self.target][pid] = comm
 
    def toUser(self, flist=None): 
        self.lgr.debug('toUser')
        cpu = self.cell_config.cpuFromCell(self.target)
        self.run2User(cpu, flist)

    def runToUserSpace(self):
        self.lgr.debug('runToUserSpace')
        self.is_monitor_running.setRunning(True)
        flist = [self.skipAndMail]
        f1 = stopFunction.StopFunction(self.skipAndMail, [], False)
        self.toUser([f1])

    def toKernel(self): 
        cpu = self.cell_config.cpuFromCell(self.target)
        self.run2Kernel(cpu)

    def toProcPid(self, pid):
        self.toRunningProc(None, [pid], None)

    def inFlist(self, fun_list, the_list):
        for stop_fun in the_list:
            for fun in fun_list:
                if stop_fun.fun == fun:
                    return True
        return False

    def toRunningProc(self, proc, want_pid=None, flist=None, debug_group=False):
        ''' intended for use when process is already running '''
        cpu, comm, pid  = self.task_utils[self.target].curProc()
        ''' if already in proc, just attach debugger '''
        self.lgr.debug('toRunningProc, look for <%s>, current pid %d <%s>' % (proc, pid, comm))
        if flist is not None and self.inFlist([self.debug, self.debugGroup], flist): 
            if pid != self.task_utils[self.target].getExitPid():
                if proc is not None and proc == comm:
                    self.lgr.debug('Already at proc %s, done' % proc)
                    f1 = stopFunction.StopFunction(self.debug, [], False)
                    self.toUser([f1])
                    #self.debug()
                    return
                elif want_pid is not None and pid in want_pid:
                    self.lgr.debug('toRunningProc already at pid %d, done' % pid)
                    self.debug(debug_group)
                    return
        prec = Prec(cpu, proc, want_pid)
        phys_current_task = self.task_utils[self.target].getPhysCurrentTask()
        proc_break = SIM_breakpoint(cpu.physical_memory, Sim_Break_Physical, Sim_Access_Write, 
                             phys_current_task, self.mem_utils[self.target].WORD_SIZE, 0)
        self.lgr.debug('toRunningProc  pid %s set break at 0x%x' % (str(want_pid), phys_current_task))
        self.proc_hap = SIM_hap_add_callback_index("Core_Breakpoint_Memop", self.runToProc, prec, proc_break)
        
        hap_clean = hapCleaner.HapCleaner(cpu)
        hap_clean.add("Core_Breakpoint_Memop", self.proc_hap)
        stop_action = hapCleaner.StopAction(hap_clean, [proc_break], flist)
        self.stop_hap = SIM_hap_add_callback("Core_Simulation_Stopped", 
        	     self.stopHap, stop_action)

        status = self.is_monitor_running.isRunning()
        if not status:
            SIM_run_command('c')
       

    def getEIP(self, cpu=None):
        if cpu is None:
            dum, dum2, cpu = self.context_manager[self.target].getDebugPid() 
            if cpu is None:
                cpu = self.cell_config.cpuFromCell(self.target)
        target = self.cell_config.cpu_cell[cpu]
        eip = self.mem_utils[target].getRegValue(cpu, 'eip')
        return eip

    def getReg(self, reg, cpu):
        target = self.cell_config.cpu_cell[cpu]
        value = self.mem_utils[target].getRegValue(cpu, reg)
        self.lgr.debug('debugGetReg for %s is %x' % (reg, value))
        return value

    class cycleRecord():
        def __init__(self, cycles, steps, eip):
            self.cycles = cycles
            self.steps = steps
            self.eip = eip
        def toString(self):
            if self.steps is not None:
                return 'cycles: 0x%x steps: 0x%x eip: 0x%x' % (self.cycles, self.steps, self.eip)
            else:
                return 'cycles: 0x%x (no steps recorded) eip: 0x%x' % (self.cycles, self.eip)

    def gdbMailbox(self, msg):
        self.gdb_mailbox = msg
        #self.lgr.debug('in gdbMailbox msg set to <%s>' % msg)
        print('gdbMailbox:%s' % msg)

    def emptyMailbox(self):
        if self.gdb_mailbox is not None and self.gdb_mailbox != "None":
            print self.gdb_mailbox
            #self.lgr.debug('emptying mailbox of <%s>' % self.gdb_mailbox)
            self.gdb_mailbox = None

    def runSkipAndMailAlone(self, cycles): 
        pid, dum2, cpu = self.context_manager[self.target].getDebugPid() 
        if cpu is None:
            self.lgr.debug("no cpu in runSkipAndMailAlone")
            return
        current = cpu.cycles
        eip = self.getEIP(cpu)
        instruct = SIM_disassemble_address(cpu, eip, 1, 0)
        self.lgr.debug('skipAndMailAlone current cycle is %x eip: %x %s requested %d cycles' % (current, eip, instruct[1], cycles))
        if cycles > 0:
            previous = current - cycles 
            start = self.bookmarks.getCycle('_start+1')
            if previous > start:
                self.context_manager[self.target].clearExitBreak()
                count = 0
                while current != previous:
                    SIM_run_command('pselect cpu-name = %s' % cpu.name)
                    SIM_run_command('skip-to cycle=%d' % previous)
                    eip = self.getEIP(cpu)
                    current = cpu.cycles
                    instruct = SIM_disassemble_address(cpu, eip, 1, 0)
                    if current != previous:
                        self.lgr.debug('runSkipAndMailAlone, have not yet reached previous %x %x eip: %x' % (current, previous, eip))
                        time.sleep(1)
                    count += 1
                    if count > 3:
                        self.lgr.debug('skipAndMailAlone, will not reach previous, bail')
                        break
                self.lgr.debug('skipAndMailAlone went to previous, cycle now is %x eip: %x %s' % (current, eip, instruct[1]))
                self.context_manager[self.target].resetBackStop()
                self.context_manager[self.target].setExitBreak(cpu)
            else:
                self.lgr.debug('skipAndRunAlone was asked to back up before start of recording')
        self.is_monitor_running.setRunning(False)
        self.lgr.debug('setRunning to false, now set mbox to 0x%x' % eip)
        self.gdbMailbox('0x%x' % eip)
        print('Monitor done')

    def skipAndMail(self, cycles=1):

        dum, dum2, cpu = self.context_manager[self.target].getDebugPid() 
        if cpu is None:
            self.lgr.debug("no cpu in runSkipAndMail")
            return
        #current = SIM_cycle_count(cpu)
        eip = self.getEIP(cpu)
        #instruct = SIM_disassemble_address(cpu, eip, 1, 0)
        cycles =- 1
        if cycles <= 0:
            self.lgr.debug('skipAndMail, set running false, and update mbox directly')
            self.is_monitor_running.setRunning(False)
            self.gdbMailbox('0x%x' % eip)
        else:
            '''
            Reverse one instruction via skip-to, set the mailbox to the new eip.
            Expect the debugger script to forward one instruction
            '''
            self.lgr.debug('skipAndMail, run it alone')
            SIM_run_alone(self.runSkipAndMailAlone, cycles)

        #self.stopTrace()
        self.restoreDebugBreaks()

    def getBookmarkPid(self):
        pid, dum2, cpu = self.context_manager[self.target].getDebugPid() 
        if pid is None:
            pid = self.task_utils[self.target].getExitPid()
        return pid

    def goToOrigin(self):
        pid = self.getBookmarkPid()
        self.lgr.debug('goToOrigin for pid %d' % pid)
        msg = self.bookmarks.goToOrigin()
        self.context_manager[self.target].setIdaMessage(msg)

    def goToDebugBookmark(self, mark):
        self.lgr.debug('goToDebugBookmark %s' % mark)
        mark = mark.replace('|','"')
        pid = self.getBookmarkPid()
        msg = self.bookmarks.goToDebugBookmark(mark)
        self.context_manager[self.target].setIdaMessage(msg)

    def listBookmarks(self):
        pid = self.getBookmarkPid()
        self.bookmarks.listBookmarks()

    def getBookmarks(self):
        pid = self.getBookmarkPid()
        return self.bookmarks.getBookmarks()

    def doReverse(self, extra_back=0):
        if self.rev_execution_enabled:
            dum, dum2, cpu = self.context_manager[self.target].getDebugPid() 
            self.lgr.debug('doReverse entered, extra_back is %s' % str(extra_back))
            self.context_manager[self.target].clearExitBreak()
            self.removeDebugBreaks()
            reverseToWhatever.reverseToWhatever(self, self.context_manager[self.target], cpu, self.lgr, extra_back=extra_back)
            self.lgr.debug('doReverse, back from reverseToWhatever init')
            self.context_manager[self.target].setExitBreak(cpu)
        else:
            print('reverse execution disabled')
            self.skipAndMail()

    def printCycle(self):
        dum, dum2, cpu = self.context_manager[self.target].getDebugPid() 
        cell_name = self.getTopComponentName(cpu)
        current = cpu.cycles
        print 'current cycle for %s is %x' % (cell_name, current)

    ''' more experiments '''
    def reverseStepInstruction(self, num=1):
        dum, dum2, cpu = self.context_manager[self.target].getDebugPid() 
        cell_name = self.getTopComponentName(cpu)
        dum_cpu, comm, pid  = self.task_utils[self.target].curProc()
        eip = self.getEIP()
        self.lgr.debug('reservseStepInstruction starting at %x' % eip)
        my_args = procInfo.procInfo(comm, cpu, pid, None, False)
        self.stopped_reverse_instruction_hap = SIM_hap_add_callback("Core_Simulation_Stopped", 
		    self.stoppedReverseInstruction, my_args)
        self.lgr.debug('reverseStepInstruction, added stop hap')
        SIM_run_alone(SIM_run_command, 'reverse-step-instruction %d' % num)

    def stoppedReverseInstruction(self, my_args, one, exception, error_string):
        cell_name = self.getTopComponentName(my_args.cpu)
        cpu, comm, pid  = self.task_utils[self.target].curProc()
        if pid == my_args.pid:
            eip = self.getEIP()
            self.lgr.debug('stoppedReverseInstruction at %x' % eip)
            print 'stoppedReverseInstruction stopped at ip:%x' % eip
            self.gdbMailbox('0x%x' % eip)
            SIM_hap_delete_callback_id("Core_Simulation_Stopped", self.stopped_reverse_instruction_hap)
        else:
            self.lgr.debug('stoppedReverseInstruction in wrong pid (%d), try again' % pid)
            SIM_run_alone(SIM_run_command, 'reverse-step-instruction')
    
    def reverseToCallInstruction(self, step_into, prev=None):
        if self.rev_execution_enabled:
            self.removeDebugBreaks()
            dum, dum2, cpu = self.context_manager[self.target].getDebugPid() 
            cell_name = self.getTopComponentName(cpu)
            self.removeDebugBreaks()
            self.context_manager[self.target].clearExitBreak()
            self.lgr.debug('reverseToCallInstruction, step_into: %r  on entry, gdb_mailbox: %s' % (step_into, self.gdb_mailbox))
            self.context_manager[self.target].showHaps()
            if prev is not None:
                instruct = SIM_disassemble_address(cpu, prev, 1, 0)
                self.lgr.debug('reverseToCallInstruction instruct is %s at prev: 0x%x' % (instruct[1], prev))
                if instruct[1] == 'int 128' or (not step_into and instruct[1].startswith('call')):
                    self.revToAddr(prev)
                else:
                    self.rev_to_call[self.target].doRevToCall(step_into, prev)
            else:
                self.lgr.debug('prev is none')
                self.rev_to_call[self.target].doRevToCall(step_into, prev)
            self.lgr.debug('reverseToCallInstruction back from call to reverseToCall ')
        else:
            print('reverse execution disabled')
            self.lgr.debug('reverseToCallInstruction reverse execution disabled')
            self.skipAndMail()

    def uncall(self):
        dum, dum2, cpu = self.context_manager[self.target].getDebugPid() 
        cell_name = self.getTopComponentName(cpu)
        dum_cpu, cur_addr, comm, pid = self.task_utils[self.target].currentProcessInfo(cpu)
        self.context_manager[self.target].clearExitBreak()
        self.lgr.debug('cgcMonitor, uncall')
        self.removeDebugBreaks()
        self.rev_to_call[self.target].doUncall()
   
    def getInstance(self):
        return INSTANCE
 
    def revToModReg(self, reg):
        self.lgr.debug('revToModReg for reg %s' % reg)
        self.removeDebugBreaks()
        self.context_manager[self.target].clearExitBreak()
        self.rev_to_call[self.target].doRevToModReg(reg)

    def revToAddr(self, address, extra_back=0):
        if self.rev_execution_enabled:
            pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
            self.lgr.debug('revToAddr 0x%x, extra_back is %d' % (address, extra_back))
            self.removeDebugBreaks()
            self.context_manager[self.target].clearExitBreak()
            reverseToAddr.reverseToAddr(address, self.context_manager[self.target], self.is_monitor_running, self, cpu, self.lgr, extra_back=extra_back)
            self.lgr.debug('back from reverseToAddr')
        else:
            print('reverse execution disabled')
            self.skipAndMail()

    ''' intended for use by gdb, if stopped return the eip.  checks for mailbox messages'''
    def getEIPWhenStopped(self, kernel_ok=False):
        simics_status = SIM_simics_is_running()
        resim_status = self.is_monitor_running.isRunning()
        debug_pid, dum2, cpu = self.context_manager[self.target].getDebugPid() 
        if debug_pid is None:
            retval = 'mailbox:exited'
            print retval
            return retval

        eip = self.getEIP(cpu)
        if resim_status and not simics_status:
            self.lgr.debug('Simics not running, RESim thinks it is running.  Perhaps gdb breakpoint?')
            pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
            SIM_run_command('pselect cpu-name = %s' % cpu.name)
            self.context_manager[self.target].setIdaMessage('Stopped at debugger breakpoint?')
            retval = 'mailbox:0x%x' % eip

        elif not resim_status:
            if cpu is None:
                print('no cpu defined in context manager')
                return
            cell_name = self.getTopComponentName(cpu)
            dum_cpu, comm, pid  = self.task_utils[self.target].curProc()
            self.lgr.debug('getEIPWhenStopped, pid %d' % (pid)) 
            if self.gdb_mailbox is not None:
                self.lgr.debug('getEIPWhenStopped mbox is %s pid is %d (%s) cycle: 0x%x' % (self.gdb_mailbox, pid, comm, cpu.cycles))
                retval = 'mailbox:%s' % self.gdb_mailbox
                print retval
                return retval
            else:
                self.lgr.debug('getEIPWhenStopped, mbox must be empty?')
            cpl = memUtils.getCPL(cpu)
            if cpl == 0 and not kernel_ok:
                self.lgr.debug('getEIPWhenStopped in kernel pid:%d (%s) eip is %x' % (pid, comm, eip))
                retval = 'in kernel'
                print retval
                return retval
            self.lgr.debug('getEIPWhenStopped pid:%d (%s) eip is %x' % (pid, comm, eip))
            if debug_pid != pid:
                self.lgr.debug('getEIPWhenStopped wrong process pid:%d (%s) eip is %x' % (pid, comm, eip))
                retval = 'wrong process'
                print retval
                return retval
            SIM_run_command('pselect cpu-name = %s' % cpu.name)
            retval = 'mailbox:0x%x' % eip
            print retval
            #print 'cmd is %s' % cmd
            #SIM_run_command(cmd)
        else:
            self.lgr.debug('call to getEIPWhenStopped, not stopped at 0x%x' % eip)
            print 'not stopped'
            retval = 'not stopped'
        return retval

    def idaMessage(self):
        self.context_manager[self.target].showIdaMessage()

    def resynch(self):
        debug_pid, debug_cell, debug_cpu = self.context_manager[self.target].getDebugPid() 
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        self.lgr.debug('resynch to pid: %d' % debug_pid)
        #self.is_monitor_running.setRunning(True)
        if self.context_manager[self.target].amWatching(pid):
            self.lgr.debug('rsynch, already in proc')
            f1 = stopFunction.StopFunction(self.skipAndMail, [], False)
            self.toUser([f1])
        else:
            f1 = stopFunction.StopFunction(self.toUser, [], True)
            f2 = stopFunction.StopFunction(self.skipAndMail, [], False)
            flist = [f1, f2]
            self.lgr.debug('rsynch, call toRunningProc for pid %d' % debug_pid)
            pid_list = self.context_manager[self.target].getThreadPids()
            self.toRunningProc(None, pid_list, flist)

    def traceExecve(self, comm=None):
        self.pfamily.traceExecve(comm)

    def watchPageFaults(self, debugging=None):
        self.page_faults[self.target].watchPageFaults(debugging)

    def stopWatchPageFaults(self, pid=None):
        self.page_faults[self.target].stopWatchPageFaults(pid)

    def catchCorruptions(self):
        self.watchPageFaults()

    def traceOpenSyscall(self):
        self.lgr.debug('about to call traceOpen')
        self.traceOpen.traceOpenSyscall()

    def getCell(self):
        return self.cell_config.cell_context[self.target]

    def getCPU(self):
        return self.cell_config.cpuFromCell(self.target)

    def reverseToUser(self):
        self.removeDebugBreaks()
        cpu = self.cell_config.cpuFromCell(self.target)
        cell = self.cell_config.cell_context[self.target]
        rtu = reverseToUser.ReverseToUser(self.param[self.target], self.lgr, cpu, cell)

    def getDebugFirstCycle(self):
        print('start_cycle:%x' % self.bookmarks.getFirstCycle())

    def getFirstCycle(self):
        pid = self.getBookmarkPid()
        return self.bookmarks.getFirstCycle()

    def stopAtKernelWrite(self, addr, rev_to_call=None, num_bytes = 1):
        '''
        Runs backwards until a write to the given address is found.
        '''
        self.context_manager[self.target].clearExitBreak()
        self.removeDebugBreaks()
        pid, dum2, cpu = self.context_manager[self.target].getDebugPid() 
        cell_name = self.getTopComponentName(cpu)
        self.lgr.debug('stopAtKernelWrite, call findKernelWrite for 0x%x num bytes %d' % (addr, num_bytes))
        cell = self.cell_config.cell_context[self.target]
        self.find_kernel_write = findKernelWrite.findKernelWrite(self, cpu, cell, addr, self.task_utils, self.task_utils,
            self.context_manager[self.target], self.param[self.target], self.bookmarks, self.lgr, rev_to_call, num_bytes) 

    def revTaintAddr(self, addr):
        '''
        back track the value at a given memory location, where did it come from?
        '''
        self.lgr.debug('revTaintAddr for 0x%x' % addr)
        self.removeDebugBreaks()
        pid, dum2, cpu = self.context_manager[self.target].getDebugPid() 
        cell_name = self.getTopComponentName(cpu)
        eip = self.getEIP(cpu)
        instruct = SIM_disassemble_address(cpu, eip, 1, 0)
        value = self.mem_utils.readWord32(cpu, addr)
        bm='backtrack START:0x%x inst:"%s" track_addr:0x%x track_value:0x%x' % (eip, instruct[1], addr, value)
        self.bookmarks.setDebugBookmark(bm)
        self.lgr.debug('BT add bookmark: %s' % bm)
        self.context_manager[self.target].setIdaMessage('')
        self.stopAtKernelWrite(addr, self.rev_to_call[self.target])

    def revTaintReg(self, reg):
        ''' back track the value in a given register '''
        self.lgr.debug('revTaintReg for %s' % reg)
        self.removeDebugBreaks()
        pid, dum2, cpu = self.context_manager[self.target].getDebugPid() 
        cell_name = self.getTopComponentName(cpu)
        eip = self.getEIP(cpu)
        instruct = SIM_disassemble_address(cpu, eip, 1, 0)
        reg_num = cpu.iface.int_register.get_number(reg)
        value = cpu.iface.int_register.read(reg_num)
        self.lgr.debug('revTaintReg for reg value %x' % value)
        bm='backtrack START:0x%x inst:"%s" track_reg:%s track_value:0x%x' % (eip, instruct[1], reg, value)
        self.bookmarks.setDebugBookmark(bm)
        self.context_manager[self.target].setIdaMessage('')
        self.rev_to_call[self.target].doRevToModReg(reg, True)

    def rev1(self):
        if self.rev_execution_enabled:
            self.removeDebugBreaks()
            dum, dum2, cpu = self.context_manager[self.target].getDebugPid() 
            new_cycle = cpu.cycles - 1
         
            start_cycles = self.rev_to_call[self.target].getStartCycles()
            if new_cycle >= start_cycles:
                self.is_monitor_running.setRunning(True)
                try:
                    result = SIM_run_command('skip-to cycle=0x%x' % new_cycle)
                except: 
                    print('Reverse execution disabled?')
                    self.skipAndMail()
                    return
                self.lgr.debug('rev1 result from skipt to 0x%x  is %s' % (new_cycle, result))
                self.skipAndMail()
            else:
                self.lgr.debug('rev1, already at first cycle 0x%x' % new_cycle)
                self.skipAndMail()
        else:
            print('reverse execution disabled')
            self.skipAndMail()

    def test1(self):
        
        pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
        cycles = cpu.cycles
        print('first skip-to cycle=0x%x' % cycles)
        for i in range(200):
            cycles = cycles - 1
            cycles = cycles & 0xFFFFFFFFFFFFFFFF
            print('this skip-to cycle=0x%x' % cycles)
            SIM_run_command('skip-to cycle=0x%x' % cycles)
            eip = self.getEIP(cpu)
            cpl = memUtils.getCPL(cpu)
            instruct = SIM_disassemble_address(cpu, eip, 1, 0)
            print('0x%x pl:%s  %s' % (eip, cpl, instruct[1]))
            
    def revOver(self): 
        self.reverseToCallInstruction(False)

    def revInto(self): 
        self.reverseToCallInstruction(True)

    def revToWrite(self, addr):
        self.stopAtKernelWrite(addr)

    def runToSyscall(self, callnum = None):
        cell = self.cell_config.cell_context[self.target]
        self.is_monitor_running.setRunning(True)
        if callnum == 0:
            callnum = None
        if callnum is not None:
            callname = self.task_utils[self.target].syscallName(callnum)
            self.lgr.debug('runToSyscall for  %s' % callname)
            #call_params = [syscall.CallParams(callname, None, break_simulation=True)]        
            call_params = []

            if callnum == 120:
                print('Disabling thread tracking for clone')
                self.stopThreadTrack()
            self.call_traces[self.target][callname] = syscall.Syscall(self, self.target, cell, self.param[self.target], self.mem_utils[self.target], self.task_utils[self.target], 
                 self.context_manager[self.target], None, self.sharedSyscall[self.target], self.lgr, self.traceMgr[self.target],callnum_list=[callnum], 
                 call_params=call_params, stop_on_call=True, targetFS=self.targetFS[self.target])
        else:
            ''' watch all syscalls '''
            self.lgr.debug('runToSyscall for any system call')
            self.trace_all[self.target] = syscall.Syscall(self, self.target, cell, self.param[self.target], self.mem_utils[self.target], self.task_utils[self.target], self.context_manager[self.target], 
                               None, self.sharedSyscall[self.target], self.lgr, self.traceMgr[self.target],None, stop_on_call=True, targetFS=self.targetFS[self.target])
        SIM_run_command('c')

    def traceSyscall(self, callnum=None, soMap=None, call_params=[], trace_procs = False):
        cell = self.cell_config.cell_context[self.target]
        # TBD only set if debugging?
        self.is_monitor_running.setRunning(True)
        self.lgr.debug('traceSyscall for callnum %s' % callnum)
        if trace_procs:
            tp = self.traceProcs[self.target]
        else:
            tp = None
        my_syscall = syscall.Syscall(self, self.target, cell, self.param[self.target], self.mem_utils[self.target], self.task_utils[self.target], 
                           self.context_manager[self.target], tp, self.sharedSyscall[self.target], self.lgr, self.traceMgr[self.target],callnum_list=[callnum], 
                           trace=True, soMap=soMap, call_params=call_params, 
                           binders=self.binders, connectors=self.connectors, targetFS=self.targetFS[self.target])
        return my_syscall

    def traceProcesses(self, new_log=True):
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        call_list = ['vfork','fork', 'clone','execve','open','pipe','pipe2','close','dup','dup2','socketcall', 
                     'exit', 'exit_group', 'waitpid', 'ipc', 'read', 'write', 'gettimeofday']
        if cpu.architecture == 'arm' or self.mem_utils[self.target].WORD_SIZE == 8:
            call_list.remove('socketcall')
            for scall in net.callname[1:]:
                call_list.append(scall.lower())
        if self.mem_utils[self.target].WORD_SIZE == 8:
            call_list.remove('ipc')
            call_list.remove('send')
            call_list.remove('recv')
            call_list.remove('waitpid')
            call_list.append('waitid')

        calls = ' '.join(s for s in call_list)
        print('tracing these system calls: %s' % calls)
        if new_log:
            self.traceMgr[self.target].open('/tmp/syscall_trace.txt', cpu)
        for call in call_list: 
            callnum = self.task_utils[self.target].syscallNumber(call)
            if callnum is None or callnum < 0:
                self.lgr.error('traceProcesses bad call name %s' % call)
                return
            self.call_traces[self.target][call] = self.traceSyscall(callnum=callnum, trace_procs=True, soMap=self.soMap[self.target])

    def stopTrace(self, cell_name=None):
        if cell_name is None:
            cell_name = self.target
        self.lgr.debug('stopTrace from genMonitor')
        for call in self.call_traces[cell_name]:
            callnum=self.task_utils[cell_name].syscallNumber(call)
            self.lgr.debug('stopTrace of call %s' % call)
            self.call_traces[cell_name][call].stopTrace(immediate=True)
        self.call_traces[cell_name].clear()   
        if cell_name in self.trace_all:
            self.trace_all[cell_name].stopTrace(immediate=True)
            del self.trace_all[cell_name]
        for exit in self.exit_maze:
            exit.rmAllBreaks()
        self.traceMgr[cell_name].close()

    def traceFile(self, path):
        self.lgr.debug('traceFile %s' % path)
        outfile = os.path.join('/tmp', os.path.basename(path))
        self.traceFiles[self.target].watchFile(path, outfile)

    def traceFD(self, fd):
        self.lgr.debug('traceFD %d' % fd)
        outfile = '/tmp/output-fd-%d.log' % fd
        self.traceFiles[self.target].watchFD(fd, outfile)

    def exceptHap(self, cpu, one, exception_number):
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        call = self.mem_utils[self.target].getRegValue(cpu, 'r7')
        self.lgr.debug('exeptHap except: %d  pid %d call %d' % (exception_number, pid, call))
        
    def traceAll(self, target=None):
        if target is None:
            target = self.target

        ''' trace all system calls. if a program selected for debugging, watch only that program '''
        self.lgr.debug('traceAll target %s begin' % target)
        if target not in self.cell_config.cell_context:
            print('Unknown target %s' % target)
            return
        cell = self.cell_config.cell_context[target]
        pid, cell_name, cpu = self.context_manager[target].getDebugPid() 
        if pid is not None:
            tf = '/tmp/syscall_trace-%s-%d.txt' % (target, pid)
        else:
            tf = '/tmp/syscall_trace-%s.txt' % target
        cpu, comm, pid = self.task_utils[target].curProc() 
        #except_hap = SIM_hap_add_callback_obj_index("Core_Exception", cpu, 0, self.exceptHap, cpu, 6) 

        self.traceMgr[target].open(tf, cpu)
        #call_list = ['vfork','clone','execve','open','pipe','pipe2','close','dup','dup2','socketcall']
        #call_list = ['vfork', 'clone']
        #calls = ' '.join(s for s in call_list)
        #print('tracing these system calls: %s' % calls)
        #callnum_list = []
        #for call in call_list:
        #    callnum_list.append(self.task_utils.syscallNumber(call))

        #my_syscall = syscall.Syscall(self, cell, self.param, self.mem_utils, self.task_utils, 
        #                   self.context_manager, self.traceProcs, self.lgr, callnum_list=callnum_list, 
        #                   trace=True, trace_fh = self.trace_fh, traceFiles=self.traceFiles)
        self.trace_all[target] = syscall.Syscall(self, target, cell, self.param[target], self.mem_utils[target], self.task_utils[target], 
                           self.context_manager[target], self.traceProcs[target], self.sharedSyscall[target], self.lgr, self.traceMgr[target], callnum_list=None, 
                           trace=True, soMap=self.soMap[target], binders=self.binders, connectors=self.connectors, targetFS=self.targetFS[target])

    def noDebug(self, dumb=None):
        self.lgr.debug('noDebug')
        cmd = 'disable-reverse-execution'
        SIM_run_command(cmd)
        self.rev_execution_enabled = False
        self.removeDebugBreaks(keep_watching=True)
        self.sharedSyscall[self.target].setDebugging(False)

    def restartDebug(self):
        cmd = 'enable-reverse-execution'
        SIM_run_command(cmd)
        self.rev_execution_enabled = True
        self.restoreDebugBreaks(was_watching=True)
        self.sharedSyscall[self.target].setDebugging(True)

    def stopThreadTrack(self):
        if self.track_threads is not None:
            self.lgr.debug('stopThreadTrack')
            self.track_threads.stopTrack()

    def showProcTrace(self):
        pid_comm_map = self.task_utils[self.target].getPidCommMap()
        precs = self.traceProcs[self.target].getPrecs()
        for pid in precs:
            if precs[pid].prog is None and pid in pid_comm_map:
                precs[pid].prog = 'comm: %s' % (pid_comm_map[pid])
        #for pid in precs:
        #    if precs[pid].prog is None and pid in self.proc_list[self.target]:
        #        precs[pid].prog = 'comm: %s' % (self.proc_list[self.target][pid])
        
        self.traceProcs[self.target].showAll()
 
    def toExecve(self, comm, flist=None):
        cell = self.cell_config.cell_context[self.target]
        callnum=self.task_utils[self.target].syscallNumber('execve')
        my_syscall = syscall.Syscall(self, self.target, cell, self.param[self.target], self.mem_utils[self.target], self.task_utils[self.target], 
                           self.context_manager[self.target], self.traceProcs[self.target], self.sharedSyscall[self.target], self.lgr, 
                           self.traceMgr[self.target], callnum_list=[callnum], trace=False, break_on_execve=comm, flist_in = flist, 
                           netInfo=self.netInfo[self.target], targetFS=self.targetFS[self.target])
        SIM_run_command('c')

    def clone(self, nth=1):
        ''' Run until we are in the child of the Nth clone of the current process'''
        #cell = self.cell_config.cell_context[self.target]
        #eh = cloneChild.CloneChild(self, cell, self.param[self.target], self.mem_utils[self.target], self.task_utils[self.target], self.context_manager[self.target], nth, self.lgr)
        #SIM_run_command('c')
        self.runToClone(nth)

    def recordText(self, start, end):
        ''' record IDA's view of text segment, unless we recorded from our own parse of the elf header '''
        self.lgr.debug('.text IDA is 0x%x - 0x%x' % (start, end))
        s, e = self.context_manager[self.target].getText()
        if s is None:
            self.lgr.debug('genMonitor recordText, no text from contextManager, use from IDA')
            cpu, comm, pid = self.task_utils[self.target].curProc() 
            self.context_manager[self.target].recordText(start, end)
            self.soMap[self.target].addText(start, end-start, 'tbd', pid)

    def textHap(self, prec, third, forth, memory):
        ''' callback when text segment is executed '''
        if self.proc_hap is None:
            return
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        if cpu != prec.cpu or pid not in prec.pid:
            self.lgr.debug('text hap, wrong something pid:%d prec pid list %s' % (pid, str(prec.pid)))
            return
        #cur_eip = SIM_get_mem_op_value_le(memory)
        eip = self.getEIP(cpu)
        self.lgr.debug('textHap, must be in text eip is 0x%x' % eip)
        SIM_break_simulation('text hap')
        if prec.debugging:
            self.context_manager[self.target].genDeleteHap(self.proc_hap)
            self.proc_hap = None
            self.skipAndMail()
 
    def restoreDebugBreaks(self, dumb=None, was_watching=False):
        if not self.debug_breaks_set:
            self.lgr.debug('restoreDebugBreaks')
            pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
            if pid is not None:
                if not was_watching:
                    self.context_manager[self.target].watchTasks()
                prec = Prec(cpu, None, pid)
                self.rev_to_call[self.target].watchSysenter(prec)
                if self.target in self.track_threads:
                    self.track_threads[self.target].startTrack()
            cell = self.cell_config.cell_context[self.target]
            cnl = [self.task_utils[self.target].syscallNumber('exit_group')]
            somap = None
            if self.target in self.soMap:
                somap = self.soMap[self.target]
            else:
                self.lgr.debug('restoreDebugBreaks no so map for %s' % self.target)
            self.exit_group_syscall[self.target] = syscall.Syscall(self, self.target, cell, self.param[self.target], 
                           self.mem_utils[self.target], self.task_utils[self.target], 
                           self.context_manager[self.target], None, self.sharedSyscall[self.target], self.lgr, self.traceMgr[self.target], 
                           callnum_list=cnl, soMap=somap)
            self.debug_breaks_set = True

    def noWatchSysEnter(self):
        self.rev_to_call[self.target].noWatchSysenter()

    def removeDebugBreaks(self, keep_watching=False):
        self.lgr.debug('removeDebugBreaks')
        pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
        self.stopWatchPageFaults(pid)
        if not keep_watching:
            self.context_manager[self.target].stopWatchTasks()
        self.rev_to_call[self.target].noWatchSysenter()
        if self.target in self.track_threads:
            self.track_threads[self.target].stopTrack()
        if self.target in self.exit_group_syscall:
            self.exit_group_syscall[self.target].stopTrace()
            del self.exit_group_syscall[self.target]
        self.debug_breaks_set = False

    def revToText(self):
        self.is_monitor_running.setRunning(True)
        start, end = self.context_manager[self.target].getText()
        if start is None:
            print('No text segment defined, has IDA been started with the rev plugin?')
            return
        self.removeDebugBreaks()
        count = end - start
        self.lgr.debug('revToText 0x%x - 0x%x count: 0x%x' % (start, end, count))
        cell = self.cell_config.cell_context[self.target]
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        self.rev_to_call[self.target].setBreakRange(self.target, pid, start, count, cpu, comm, False)
        f1 = stopFunction.StopFunction(self.skipAndMail, [], False)
        f2 = stopFunction.StopFunction(self.rev_to_call[self.target].rmBreaks, [], False)
        flist = [f1, f2]
        hap_clean = hapCleaner.HapCleaner(cpu)
        stop_action = hapCleaner.StopAction(hap_clean, None, flist)
        self.stop_hap = SIM_hap_add_callback("Core_Simulation_Stopped", 
          self.stopHap, stop_action)
        self.lgr.debug('hap set, now reverse')
        SIM_run_command('rev')

    def getSyscall(self, cell_name, callname):
        if callname in self.call_traces[cell_name]:
            return self.call_traces[cell_name][callname]
        elif cell_name in self.trace_all:
            return self.trace_all[cell_name]
        else:
            self.lgr.debug('genMonitor getSyscall, not able to return instance for call %s len self.call_traces %d' % (callname, 
                       len(self.call_traces[cell_name])))
            return None

    def runToText(self, flist = None):
        ''' run until within the currently defined text segment '''
        self.is_monitor_running.setRunning(True)
        start, end = self.context_manager[self.target].getText()
        if start is None:
            print('No text segment defined, has IDA been started with the rev plugin?')
            return
        count = end - start
        cell = self.cell_config.cell_context[self.target]
        self.lgr.debug('runToText range 0x%x 0x%x' % (start, end))
        proc_break = self.context_manager[self.target].genBreakpoint(cell, Sim_Break_Linear, Sim_Access_Execute, start, count, 0)
        pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
        if pid is None:
            self.lgr.debug('runToText, not debugging yet, assume current process')
            cpu, comm, pid = self.task_utils[self.target].curProc() 
            prec = Prec(cpu, None, [pid])
        else:
            pid_list = self.context_manager[self.target].getThreadPids()
            prec = Prec(cpu, None, pid_list)
        if flist is None:
            prec.debugging = True
            f1 = stopFunction.StopFunction(self.skipAndMail, [], False)
            flist = [f1]

        else:
            self.call_traces[self.target]['open'] = self.traceSyscall(callnum=self.task_utils[self.target].syscallNumber('open'), soMap=self.soMap)

        self.proc_hap = self.context_manager[self.target].genHapIndex("Core_Breakpoint_Memop", self.textHap, prec, proc_break, 'text_hap')

        hap_clean = hapCleaner.HapCleaner(cpu)
        hap_clean.add("GenContext", self.proc_hap)
        stop_action = hapCleaner.StopAction(hap_clean, None, flist)
        self.stop_hap = SIM_hap_add_callback("Core_Simulation_Stopped", 
          self.stopHap, stop_action)

        self.context_manager[self.target].watchTasks()
        self.lgr.debug('hap set, now run')
        SIM_run_alone(SIM_run_command, 'continue')

    def remainingCallTraces(self):
        for cell_name in self.call_traces:
            if len(self.call_traces[cell_name]) > 0:
                return True
        return False

    def runTo(self, call, call_params, cell_name=None, run=True):
        if self.is_monitor_running.isRunning():
            print('Monitor is running, try again after it pauses')
            return
        self.is_monitor_running.setRunning(True)
        if cell_name is None:
            cell_name = self.target
        cell = self.cell_config.cell_context[cell_name]
        call_num = self.task_utils[cell_name].syscallNumber(call)
        self.lgr.debug('genMonitor runTo cellname %s call_num %d' % (cell_name, call_num))
        self.call_traces[cell_name][call] = syscall.Syscall(self, cell_name, cell, self.param[cell_name], self.mem_utils[cell_name], 
                               self.task_utils[cell_name], 
                               self.context_manager[cell_name], None, self.sharedSyscall[cell_name], self.lgr, self.traceMgr[cell_name],
                               callnum_list=[call_num], call_params=[call_params], targetFS=self.targetFS[cell_name])
        if run:
            SIM_run_command('c')

    def runToClone(self, nth=None):
        self.lgr.debug('runToClone to %s' % str(nth))
        call_params = syscall.CallParams('clone', None, break_simulation=True)        
        call_params.nth = nth
        self.runTo('clone', call_params)

    def runToConnect(self, addr, nth=None):
        #addr = '192.168.31.52:20480'
        self.lgr.debug('runToConnect to %s' % addr)
        ''' NOTE: socketCallName returns "socket" for x86 '''
        call = self.task_utils[self.target].socketCallName('connect')
        call_params = syscall.CallParams('connect', addr, break_simulation=True)        
        call_params.nth = nth
        self.runTo(call, call_params)

    def runToDiddle(self, dfile, cell_name=None):
        if cell_name is None:
            cell_name = self.target
        diddle = diddler.Diddler(dfile, self.mem_utils[self.target], cell_name, self.lgr)
        call_params = syscall.CallParams('write', diddle, break_simulation=True)        
        if cell_name is None:
            cell_name = self.target
            run = True
        else:
            run = False
        operation = diddle.getOperation()
        self.lgr.debug('runToDiddle file %s cellname %s operation: %s' % (dfile, cell_name, operation))
        self.runTo(operation, call_params, cell_name=cell_name, run=run)

    def runToDiddleRead(self, dfile):
        diddle = diddler.Diddler(dfile, self.mem_utils[self.target], self.lgr)
        call_params = syscall.CallParams('read', diddle, break_simulation=False)        
        cell_name = diddle.getCell()
        self.lgr.debug('runToDiddle read file %s cellname %s' % (dfile, cell_name))
        if cell_name is None:
            cell_name = self.target
            run = True
        else:
            run = False
        self.runTo('read', call_params, cell_name=cell_name, run=run)

    def runToWrite(self, substring):
        call_params = syscall.CallParams('write', substring, break_simulation=True)        
        cell = self.cell_config.cell_context[self.target]
        self.runTo('write', call_params)
        self.lgr.debug('runToWrite to %s' % substring)

    def runToOpen(self, substring):
        call_params = syscall.CallParams('open', substring, break_simulation=True)        
        self.lgr.debug('runToOpen to %s' % substring)
        self.runTo('open', call_params)

    def runToSend(self, substring):
        call = self.task_utils[self.target].socketCallName('send')
        call_params = syscall.CallParams('send', substring, break_simulation=True)        
        self.lgr.debug('runToSend to %s' % substring)
        self.runTo(call, call_params)

    def runToAccept(self, fd):
        call = self.task_utils[self.target].socketCallName('accept')
        call_params = syscall.CallParams('accept', fd, break_simulation=True)        
        self.lgr.debug('runToAccept on FD: %d' % fd)
        self.runTo(call, call_params)
        
    def runToBind(self, addr):
        #addr = '192.168.31.52:20480'
        call = self.task_utils[self.target].socketCallName('bind')
        call_params = syscall.CallParams('bind', addr, break_simulation=True)        
        self.lgr.debug('runToBind to %s' % addr)
        self.runTo(call, call_params)

    def runToIO(self, fd):
        call_params = syscall.CallParams(None, fd, break_simulation=True)        
        cell = self.cell_config.cell_context[self.target]
        self.lgr.debug('runToIO on FD %d' % fd)
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        calls = ['read', 'write', '_llseek', 'socketcall', 'close', 'ioctl', 'select', 'pselect6']
        if cpu.architecture == 'arm' or self.mem_utils[self.target].WORD_SIZE == 8:
            calls.remove('socketcall')
            for scall in net.callname[1:]:
                calls.append(scall.lower())
        if self.mem_utils[self.target].WORD_SIZE == 8:
            calls.remove('_llseek')
            calls.append('lseek')
            calls.remove('send')
            calls.remove('recv')
        callnums = []
        for call in calls:
            callnum=self.task_utils[self.target].syscallNumber(call)
            if callnum < 0:
                self.lgr.debug('runToIO, no call found for %s' % call)
            callnums.append(callnum)

        the_syscall = syscall.Syscall(self, self.target, cell, self.param[self.target], self.mem_utils[self.target], self.task_utils[self.target], 
                               self.context_manager[self.target], None, self.sharedSyscall[self.target], self.lgr, self.traceMgr[self.target],
                               callnums, call_params=[call_params], targetFS=self.targetFS[self.target])
        for call in calls:
            self.call_traces[self.target][call] = the_syscall
        # TBD provide function to override
        SIM_run_command('c')


    def getCurrentSO(self):
        cpu, comm, pid = self[self.target].task_utils[self.target].curProc() 
        eip = self.getEIP(cpu)
        retval = self.getSO(eip)
        return retval

    def getSO(self, eip):
        fname = self.getSOFile(eip)
        self.lgr.debug('getCurrentSO fname for eip 0x%x is %s' % (eip, fname))
        retval = None
        if fname is not None:
            text_seg  = self.soMap[self.target].getSOAddr(fname) 
            if text_seg is None:
                self.lgr.error('getSO no map for %s' % fname)
                return
            if text_seg.address is not None:
                if text_seg.locate is not None:
                    start = text_seg.locate+text_seg.offset
                    end = start + text_seg.size
                else:
                    start = text_seg.address
                    end = text_seg.address + text_seg_size
                retval = ('%s:0x%x-0x%x' % (fname, start, end))
            else:
                print('None')
        else:
            print('None')
        return retval
     
    def showSOMap(self):
        self.lgr.debug('showSOMap')
        self.soMap[self.target].showSO()

    def getSOFile(self, addr):
        fname = self.soMap[self.target].getSOFile(addr)
        return fname

    def showThreads(self):
        pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
        thread_recs = self.context_manager[self.target].getThreadRecs()
        for rec in thread_recs:
            pid = self.mem_utils[self.target].readWord32(cpu, rec + self.param[self.target].ts_pid)
            state = self.mem_utils[self.target].readWord32(cpu, rec)
            self.lgr.debug('thread pid: %d state: 0x%x rec: 0x%x' % (pid, state, rec)) 
            print('thread pid: %d state: 0x%x rec: 0x%x' % (pid, state, rec)) 
            

    def traceRoutable(self):
        call_list = ['vfork','fork', 'clone','execve','socketcall']
        call_params = {}
        call_params['socketcall'] = []
        cp = syscall.CallParams('connect', None)
        cp.param_flags.append(syscall.ROUTABLE)
        call_params['socketcall'].append(cp)

        calls = ' '.join(s for s in call_list)
        print('tracing these system calls: %s' % calls)
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        self.traceMgr[self.target].open('/tmp/syscall_trace.txt', cpu)
        for call in call_list: 
            this_call_params = []
            if call in call_params:
                this_call_params = call_params[call]
            self.call_traces[self.target][call] = self.traceSyscall(callnum=self.task_utils[self.target].syscallNumber(call), call_params=this_call_params, trace_procs=True)

    def traceListen(self):
        ''' generate a syscall trace of processes that bind to an IP address/port '''
        call_list = ['vfork','fork', 'clone','execve','socketcall']
        call_params = {}
        call_params['socketcall'] = []
        cp = syscall.CallParams('bind', None)
        cp.param_flags.append(syscall.AF_INET)
        call_params['socketcall'].append(cp)

        calls = ' '.join(s for s in call_list)
        print('tracing these system calls: %s' % calls)
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        self.traceMgr[self.target].open('/tmp/syscall_trace.txt', cpu)
        for call in call_list: 
            this_call_params = []
            if call in call_params:
                this_call_params = call_params[call]
            self.call_traces[self.target][call] = self.traceSyscall(callnum=self.task_utils[self.target].syscallNumber(call), call_params=this_call_params, trace_procs=True)

    def showBinders(self):
            self.binders.showAll('/tmp/binder.txt')
            self.binders.dumpJson('/tmp/binder.json')

    def dumpBinders(self):
            self.binders = self.call_traces[self.target]['socketcall'].getBinders()
            self.binders.dumpJson('/tmp/binder.json')

    def showConnectors(self):
            self.connectors.showAll('/tmp/connector.txt')
            self.connectors.dumpJson('/tmp/connector.json')

    def dumpConnectors(self):
            self.connectors = self.call_traces[self.target]['socketcall'].getConnectors()
            self.connectors.dumpJson('/tmp/connector.json')

    def stackTrace(self):
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        st = stackTrace.StackTrace(self, cpu, pid, self.soMap[self.target], self.mem_utils[self.target], self.task_utils[self.target], self.stack_base[self.target], self.ida_funs, self.targetFS[self.target], self.lgr)
        st.printTrace()

    def getStackTraceQuiet(self):
        pid, dum2, cpu = self.context_manager[self.target].getDebugPid() 
        if pid is None:
            cpu, comm, pid = self.task_utils[self.target].curProc() 
        st = stackTrace.StackTrace(self, cpu, pid, self.soMap[self.target], self.mem_utils[self.target], self.task_utils[self.target], self.stack_base[self.target], self.ida_funs, self.targetFS[self.target], self.lgr)
        return st

    def getStackTrace(self):
        ''' used by IDA client '''
        self.lgr.debug('getStackTrace')
        pid, dum2, cpu = self.context_manager[self.target].getDebugPid() 
        if pid is None:
            cpu, comm, pid = self.task_utils[self.target].curProc() 
        st = stackTrace.StackTrace(self, cpu, pid, self.soMap[self.target], self.mem_utils[self.target], self.task_utils[self.target], self.stack_base[self.target], self.ida_funs, self.targetFS[self.target], self.lgr)
        j = st.getJson() 
        self.lgr.debug(j)
        #print j
        return j

    def writeRegValue(self, reg, value):
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        reg_num = cpu.iface.int_register.get_number(reg)
        cpu.iface.int_register.write(reg_num, value)
        self.lgr.debug('writeRegValue %s, %x regnum %d' % (reg, value, reg_num))

    def writeWord(self, address, value):
        ''' NOTE: wipes out bookmarks! '''
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        phys_block = cpu.iface.processor_info.logical_to_physical(address, Sim_Access_Read)
        SIM_write_phys_memory(cpu, phys_block.address, value, 4)
        self.lgr.debug('writeWord, disable reverse execution to clear bookmarks, then set origin')
        cmd = 'disable-reverse-execution'
        SIM_run_command(cmd)
        cmd = 'enable-reverse-execution'
        SIM_run_command(cmd)
        self.bookmarks.setOrigin(cpu, self.context_manager[self.target].getIdaMessage())

    def writeString(self, address, string):
        ''' NOTE: wipes out bookmarks! '''
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        self.lgr.debug('writeString 0x%x %s' % (address, string))
        self.mem_utils[self.target].writeString(cpu, address, string)
        self.lgr.debug('writeWord, disable reverse execution to clear bookmarks, then set origin')
        cmd = 'disable-reverse-execution'
        SIM_run_command(cmd)
        cmd = 'enable-reverse-execution'
        SIM_run_command(cmd)
        self.bookmarks.setOrigin(cpu, self.context_manager[self.target].getIdaMessage())

    def watchData(self, start=None, length=None):
        if start is not None:
            self.lgr.debug('watchData 0x%x count %d' % (start, length))
            self.dataWatch[self.target].setRange(start, length) 
        self.is_monitor_running.setRunning(True)
        if self.dataWatch[self.target].watch():
            SIM_run_command('c')
        else: 
            print('no data being watched')

    def isProtectedMemory(self, addr):
        ''' compat with CGC version '''
        return False 

    def showHaps(self):
        for cell_name in self.context_manager:
            print('Cell: %s' % cell_name)
            self.context_manager[cell_name].showHaps()

    def addMazeExit(self):
        ''' Intended for use if it seems a maze exit is nested -- will cause the most recent breakout
            address to be ignored when setting maze exit breakpoints '''
        if len(self.exit_maze) > 0:
            eip = self.exit_maze[-1].getBreakout()
            if eip is not None:
                self.lgr.debug('addMazeExit adding 0x%x to exits' % eip)
                if self.exit_maze[-1] not in self.maze_exits:
                    self.maze_exists[self.exit_maze[-1]] = []
                self.maze_exits[self.exit_maze[-1]].append(eip)

    def getMaze(self):
        maze = self.exit_maze[-1].getMaze()
        
        jmaze = json.dumps(maze)
        print jmaze

    def getMazeExits(self):
        if len(self.exit_maze) > 0:
            if self.exit_maze[-1] in self.maze_exits:
                return self.maze_exits[self.exit_maze[-1]]
        return []

    def doMazeReturn(self):
        if len(self.exit_maze) > 0:
            self.exit_maze[-1].mazeReturn()

    def checkMazeReturn(self):
        for me in self.exit_maze:
            if me.checkJustReturn():
                return me
        return None

    def autoMaze(self):
        self.auto_maze = not self.auto_maze
        self.lgr.debug('auto_maze now %r, run again to toggle.' % self.auto_maze)
        print('auto_maze now %r, run again to toggle.' % self.auto_maze)

    def getAutoMaze(self):
        return self.auto_maze

    def exitMaze(self, syscallname, debugging=False):
        cpu, comm, pid = self.task_utils[self.target].curProc() 
        cpl = memUtils.getCPL(cpu)
        if cpl == 0:
            print('Must first run to user space.')
            return
        cell = self.cell_config.cell_context[self.target]
        self.is_monitor_running.setRunning(True)
        self.lgr.debug('exitMaze, trace_all is %s' % str(self.trace_all[self.target]))
        tod_track = self.trace_all[self.target]
        if tod_track is None: 
            if syscallname in self.call_traces:
                self.lgr.debug('genMonitor exitMaze pid:%d, using syscall defined for %s' % (pid, syscallname))
                tod_track = self.call_traces[self.target][syscallname]
            else:
                self.lgr.debug('genMonitor exitMaze pid:%d, using new syscall for %s' % (pid, syscallname))
                tod_track = syscall.Syscall(self, self.target, cell, self.param[self.target], self.mem_utils[self.target], self.task_utils[self.target], 
                           self.context_manager[self.target], None, self.sharedSyscall[self.target], self.lgr,self.traceMgr, 
                           callnum_list=[self.task_utils[self.target].syscallNumber(syscallname)])
        else:
            self.lgr.debug('genMonitor exitMaze, using new syscall for traceAll')
        one_proc = False
        dbgpid, dumb, dumb1 = self.context_manager[self.target].getDebugPid() 
        if dbgpid is not None:
            one_proc = True
        em = exitMaze.ExitMaze(self, cpu, cell, pid, tod_track, self.context_manager[self.target], self.task_utils[self.target], self.mem_utils[self.target], debugging, one_proc, self.lgr)
        self.exit_maze.append(em)
        em.run()
        #self.exit_maze.showInstructs()

    def plantBreaks(self):
        if len(self.exit_maze) > 0:
            self.exit_maze[-1].plantBreaks() 
        print('Maze exit breaks planted')

    def plantCmpBreaks(self):
        if len(self.exit_maze) > 0:
            self.exit_maze[-1].plantCmpBreaks() 
            print('Maze pruning breaks planted')

    def showParams(self):
        self.param.printParams()

    #def inProcList(self, pid):
    #    if pid in self.proc_list[self.target]:
    #        return True
    #    else:
    #        return False

    #def myTasks(self):
    #    print('Current proc_list for %s' % self.target)
    #    for pid in self.proc_list[self.target]:
    #        print('%d %s' % (pid, self.proc_list[self.target][pid]))

    def writeConfig(self, name):
        cmd = 'write-configuration %s' % name 
        SIM_run_command(cmd)
        for cell_name in self.cell_config.cell_context:
            if cell_name in self.netInfo:
           
                net_file = os.path.join('./', name, cell_name, 'net_list.pickle')
                try:
                    os.mkdir(os.path.dirname(net_file))
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise
                    pass
                self.netInfo[cell_name].pickleit(net_file)
                self.task_utils[cell_name].pickleit(name)
                self.soMap[cell_name].pickleit(name)
                self.traceProcs[cell_name].pickleit(name)

        net_link_file = os.path.join('./', name, 'net_link.pickle')
        pickle.dump( self.link_dict, open( net_link_file, "wb" ) )

    def showCycle(self):
        pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
        cycles = self.bookmarks.getCurrentCycle(cpu)
        print ('cpu cycles since _start: 0x%x' % cycles)
        
    def continueForward(self):
        self.lgr.debug('continueForward')
        self.is_monitor_running.setRunning(True)
        SIM_run_command('c')

    def showNets(self):
        net_commands = self.netInfo.getCommands()
        if len(net_commands) > 0:
           print('Network definition commands:')
        for c in net_commands:
            print c

    def notRunning(self):
        status = self.is_monitor_running.isRunning()
        if status:   
            print('Was running, set to not running')
            self.is_monitor_running.setRunning(False)

    def getMemoryValue(self, addr):
        pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
        value = self.mem_utils[self.target].readWord32(cpu, addr)
        print('0x%x' % value)

    def printRegJson(self):
        self.lgr.debug('printRegJson')
        pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
        self.mem_utils[self.target].printRegJson(cpu)

    def flushTrace(self):
        self.traceMgr[self.target].flush()

    def getCurrentThreadLeaderPid(self):
        pid = self.task_utils[self.target].getCurrentThreadLeaderPid()
        print pid        

    def getGroupPids(self, leader):
        plist = self.task_utils[self.target].getGroupPids(leader)
        if plist is None:
            print('Could not find leader %d' % leader)
            return
        for pid in plist:
            print pid
        
    def reportMode(self):
        pid, cell_name, cpu = self.context_manager[self.target].getDebugPid() 
        self.mode_hap = SIM_hap_add_callback_obj("Core_Mode_Change", cpu, 0, self.modeChangeReport, pid)
        self.stop_hap = SIM_hap_add_callback("Core_Simulation_Stopped", self.stopModeChanged, None)

    def setTarget(self, target):
        if target not in self.cell_config.cell_context:
            print('Unknown target: %s' % target)
            return
        self.target = target  
        print('Target is now: %s' % target)
        self.lgr.debug('Target is now: %s' % target)

 
    
if __name__=="__main__":        
    print('instantiate the GenMonitor') 
    cgc = GenMonitor()
    cgc.doInit()
