#!/usr/bin/env python
# Copyright 2013-present Barefoot Networks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Runs the BMv2 behavioral model simulator with input from an stf file

from __future__ import print_function
from subprocess import Popen
from threading import Thread
import json
import sys
import re
import os
import stat
import tempfile
import shutil
import difflib
import subprocess
import time
import random
import errno
from string import maketrans
try:
    from scapy.layers.all import *
    from scapy.utils import *
except ImportError:
    pass

SUCCESS = 0
FAILURE = 1

class Options(object):
    def __init__(self):
        self.binary = None
        self.verbose = False
        self.preserveTmp = False

def nextWord(text, sep = None):
    # Split a text at the indicated separator.
    # Note that the separator can be a string.
    # Separator is discarded.
    spl = text.split(sep, 1)
    if len(spl) == 0:
        return '', ''
    elif len(spl) == 1:
        return spl[0].strip(), ''
    else:
        return spl[0].strip(), spl[1].strip()

def ByteToHex(byteStr):
    return ''.join( [ "%02X " % ord( x ) for x in byteStr ] ).strip()

def HexToByte(hexStr):
    bytes = []
    hexStr = ''.join( hexStr.split(" ") )
    for i in range(0, len(hexStr), 2):
        bytes.append( chr( int (hexStr[i:i+2], 16 ) ) )
    return ''.join( bytes )

def reportError(*message):
    print("***", *message)

class Local(object):
    # object to hold local vars accessable to nested functions
    pass

def run_timeout(options, args, timeout, stderr):
    if options.verbose:
        print("Executing ", " ".join(args))
    local = Local()
    local.process = None
    def target():
        procstderr = None
        if stderr is not None:
            procstderr = open(stderr, "w")
        local.process = Popen(args, stderr=procstderr)
        local.process.wait()
    thread = Thread(target=target)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        print("Timeout ", " ".join(args), file=sys.stderr)
        local.process.terminate()
        thread.join()
    if local.process is None:
        # never even started
        reportError("Process failed to start")
        return -1
    if options.verbose:
        print("Exit code ", local.process.returncode)
    return local.process.returncode

timeout = 100

class ConcurrentInteger(object):
    # Generates exclusive integers in a range 0-max
    # in a way which is safe across multiple processes.
    # It uses a simple form of locking using folder names.
    # This is necessary because this script may be invoked
    # concurrently many times by make, and we need the many simulator instances
    # to use different port numbers.
    def __init__(self, folder, max):
        self.folder = folder
        self.max = max
    def lockName(self, value):
        return "lock_" + str(value)
    def release(self, value):
        os.rmdir(self.lockName(value))
    def generate(self):
        # try 10 times
        for i in range(0, 10):
            index = random.randint(0, self.max)
            file = self.lockName(index)
            try:
                os.makedirs(file)
                return index
            except:
                time.sleep(1)
                continue
        return None

class BMV2ActionArg(object):
    def __init__(self, name, width):
        # assert isinstance(name, str)
        # assert isinstance(width, int)
        self.name = name
        self.width = width

class TableKey(object):
    def __init__(self, ternary):
        self.fields = []
        self.ternary = ternary
    def append(self, name):
        self.fields.append(name)

class TableKeyInstance(object):
    def __init__(self, tableKey):
        assert isinstance(tableKey, TableKey)
        self.values = {}
        self.key = tableKey
        for f in tableKey.fields:
            self.values[f] = self.makeMask("0x*", tableKey.ternary)
    def set(self, key, value, ternary):
        array = re.compile("(.*)\$([0-9]+)(.*)");
        m = array.match(key)
        if m:
            key = m.group(1) + "[" + m.group(2) + "]" + m.group(3)

        found = False
        for i in self.key.fields:
            if key == i:
                found = True
        if not found:
            raise Exception("Unexpected key field " + key)
        self.values[key] = self.makeMask(value, ternary)
    def makeMask(self, value, ternary):
        if not ternary:
            return value
        if value.startswith("0x"):
            mask = "F"
            value = value[2:]
            prefix = "0x"
        elif value.startswith("0b"):
            mask = "1"
            value = value[2:]
            prefix = "0b"
        elif value.startswith("0o"):
            mask = "7"
            value = value[2:]
            prefix = "0o"
        values = "0123456789*"
        replacements = (mask * 10) + "0"
        trans = maketrans(values, replacements)
        m = value.translate(trans)
        return prefix + value.replace("*", "0") + "&&&" + prefix + m
    def __str__(self):
        result = ""
        for f in self.key.fields:
            if result != "":
                result += " "
            result += self.values[f]
        return result

class BMV2ActionArguments(object):
    def __init__(self, action):
        assert isinstance(action, BMV2Action)
        self.action = action
        self.values = {}
    def set(self, key, value):
        found = False
        for i in self.action.args:
            if key == i.name:
                found = True
        if not found:
            raise Exception("Unexpected action arg " + key)
        self.values[key] = value
    def __str__(self):
        result = ""
        for f in self.action.args:
            if result != "":
                result += " "
            result += self.values[f.name]
        return result
    def size(self):
        return len(self.action.args)

class BMV2Action(object):
    def __init__(self, jsonAction):
        self.name = jsonAction["name"]
        self.args = []
        for a in jsonAction["runtime_data"]:
            arg = BMV2ActionArg(a["name"], a["bitwidth"])
            self.args.append(arg)
    def __str__(self):
        return self.name
    def makeArgsInstance(self):
        return BMV2ActionArguments(self)

class BMV2Table(object):
    def __init__(self, jsonTable):
        self.match_type = jsonTable["match_type"]
        self.name = jsonTable["name"]
        self.key = TableKey(self.match_type == "ternary")
        self.actions = {}
        for k in jsonTable["key"]:
            name = ""
            for t in k["target"]:
                if name != "":
                    name += "."
                name += t
            self.key.append(name)
        actions = jsonTable["actions"]
        action_ids = jsonTable["action_ids"]
        for i in range(0, len(actions)):
            actionName = actions[i]
            actionId = action_ids[i]
            self.actions[actionName] = actionId
    def __str__(self):
        return self.name
    def makeKeyInstance(self):
        return TableKeyInstance(self.key)

# Represents enough about the program executed to be
# able to invoke the BMV2 simulator, create a CLI file
# and test packets in pcap files.
class RunBMV2(object):
    def __init__(self, folder, options, jsonfile):

        self.clifile = folder + "/cli.txt"
        self.jsonfile = jsonfile
        self.stffile = None
        self.folder = folder
        self.pcapPrefix = "pcap"
        self.interfaces = {}
        self.expected = {}
        self.packetDelay = 0
        self.options = options
        self.json = None
        self.tables = []
        self.actions = []
        self.switchLogFile = "switch.log"  # .txt is added by BMv2
        self.readJson()
    def readJson(self):
        with open(self.jsonfile) as jf:
            self.json = json.load(jf)
        for a in self.json["actions"]:
            self.actions.append(BMV2Action(a))
        for t in self.json["pipelines"][0]["tables"]:
            self.tables.append(BMV2Table(t))
        for t in self.json["pipelines"][1]["tables"]:
            self.tables.append(BMV2Table(t))
    def filename(self, interface, direction):
        return self.folder + "/" + self.pcapPrefix + interface + "_" + direction + ".pcap"
    def do_cli_command(self, cmd):
        if self.options.verbose:
            print(cmd)
        self.cli_stdin.write(cmd + "\n")
        self.cli_stdin.flush()
        self.packetDelay = 0.1
    def do_command(self, cmd):
        first, cmd = nextWord(cmd)
        if first == "":
            pass
        elif first == "add":
            self.do_cli_command(self.parse_table_add(cmd))
        elif first == "setdefault":
            self.do_cli_command(self.parse_table_set_default(cmd))
        elif first == "packet":
            interface, data = nextWord(cmd)
            data = ''.join(data.split())
            time.sleep(self.packetDelay)
            self.interfaces[interface]._write_packet(HexToByte(data))
            self.interfaces[interface].flush()
            self.packetDelay = 0
        elif first == "expect":
            interface, data = nextWord(cmd)
            data = ''.join(data.split())
            self.expected.setdefault(interface, []).append(data)
        else:
            if self.options.verbose:
                print("ignoring stf command:", first, cmd)
    def parse_table_set_default(self, cmd):
        tableName, cmd = nextWord(cmd)
        table = self.tableByName(tableName)
        actionName, cmd = nextWord(cmd, "(")
        action = self.actionByName(table, actionName)
        actionArgs = action.makeArgsInstance()
        cmd = cmd.strip(")")
        while cmd != "":
            word, cmd = nextWord(cmd, ",")
            k, v = nextWord(word, ":")
            actionArgs.set(k, v)
        command = "table_set_default " + tableName + " " + actionName
        if actionArgs.size():
            command += " => " + str(actionArgs)
        return command
    def parse_table_add(self, cmd):
        tableName, cmd = nextWord(cmd)
        table = self.tableByName(tableName)
        key = table.makeKeyInstance()
        actionArgs = None
        actionName = None
        prio, cmd = nextWord(cmd)
        ternary = True
        number = re.compile("[0-9]+")
        if not number.match(prio):
            # not a priority; push back
            cmd = prio + " " + cmd
            prio = ""
            ternary = False
        while cmd != "":
            if actionName != None:
                # parsing action arguments
                word, cmd = nextWord(cmd, ",")
                k, v = nextWord(word, ":")
                actionArgs.set(k, v)
            else:
                # parsing table key
                word, cmd = nextWord(cmd)
                if word.find("(") >= 0:
                    # found action
                    actionName, arg = nextWord(word, "(")
                    action = self.actionByName(table, actionName)
                    actionArgs = action.makeArgsInstance()
                    cmd = arg + cmd
                    cmd = cmd.strip("()")
                else:
                    k, v = nextWord(word, ":")
                    key.set(k, v, ternary)

        if prio != "":
            # Priorities in BMV2 seem to be reversed with respect to the stf file
            # Hopefully 10000 is large enough
            prio = str(10000 - int(prio))
        command = "table_add " + tableName + " " + actionName + " " + str(key) + " => " + str(actionArgs) + " " + prio
        return command
    def actionByName(self, table, actionName):
        id = table.actions[actionName]
        action = self.actions[id]
        return action
    def tableByName(self, tableName):
        for t in self.tables:
            if t.name == tableName:
                return t
        raise Exception("Could not find table " + tableName)
    def interfaceArgs(self):
        # return list of interface names suitable for bmv2
        result = []
        for interface in sorted(self.interfaces):
            result.append("-i " + interface + "@" + self.pcapPrefix + interface)
        return result
    def generate_model_inputs(self, stffile):
        self.stffile = stffile
        with open(stffile) as i:
            for line in i:
                line, comment = nextWord(line, "#")
                first, cmd = nextWord(line)
                if first == "packet" or first == "expect":
                    interface, cmd = nextWord(cmd)
                    if not interface in self.interfaces:
                        # Can't open the interfaces yet, as that would block
                        ifname = self.interfaces[interface] = self.filename(interface, "in")
                        os.mkfifo(ifname)
        return SUCCESS
    def run(self):
        if self.options.verbose:
            print("Running model")
        wait = 0  # Time to wait before model starts running

        concurrent = ConcurrentInteger(os.getcwd(), 1000)
        rand = concurrent.generate()
        if rand is None:
            reportError("Could not find a free port for Thrift")
            return FAILURE
        thriftPort = str(9090 + rand)

        try:
            runswitch = ["simple_switch", "--log-file", self.switchLogFile,
                         "--use-files", str(wait), "--thrift-port", thriftPort,
                         "--device-id", str(rand)] + self.interfaceArgs() + ["../" + self.jsonfile]
            if self.options.verbose:
                print("Running", " ".join(runswitch))
            sw = subprocess.Popen(runswitch, cwd=self.folder)

            # open input interfaces
            # DANGER -- it is critical that we open these fifos in the same order as bmv2,
            # as otherwise we'll deadlock.  Would be nice if we could open nonblocking.
            for interface in sorted(self.interfaces):
                ifname = self.interfaces[interface]
                fp = self.interfaces[interface] = RawPcapWriter(ifname, linktype=0)
                fp._write_header(None)

            runcli = ["simple_switch_CLI", "--thrift-port", thriftPort]
            if self.options.verbose:
                print("Running", " ".join(runcli))
            cli = subprocess.Popen(runcli, cwd=self.folder, stdin=subprocess.PIPE)
            self.cli_stdin = cli.stdin
            with open(self.stffile) as i:
                for line in i:
                    line, comment = nextWord(line, "#")
                    self.do_command(line)
            cli.stdin.close()
            for interface, fp in self.interfaces.iteritems():
                fp.close()
            cli.wait()
            if cli.returncode != 0:
                reportError("CLI process failed with exit code", cli.returncode)
                return FAILURE
            # Give time to the model to execute
            time.sleep(1)
            sw.terminate()
            sw.wait()
            # This only works on Unix: negative returncode is
            # minus the signal number that killed the process.
            if sw.returncode != -15:  # 15 is SIGTERM
                reportError("simple_switch died with return code", sw.returncode);
            elif self.options.verbose:
                print("simple_switch exit code", sw.returncode)
        finally:
            concurrent.release(rand)
        if self.options.verbose:
            print("Execution completed")
        return SUCCESS
    def comparePacket(self, expected, received):
        received = ''.join(ByteToHex(str(received)).split()).upper()
        expected = ''.join(expected.split()).upper()
        if len(received) < len(expected):
            reportError("Received packet too short", len(received), "vs", len(expected))
            return FAILURE
        for i in range(0, len(expected)):
            if expected[i] == "*":
                continue;
            if expected[i] != received[i]:
                reportError("Packet different at position", i, ": expected", expected[i], ", received", received[i])
                return FAILURE
        return SUCCESS
    def showLog(self):
        with open(self.folder + "/" + self.switchLogFile + ".txt") as a:
            log = a.read()
            print("Log file:")
            print(log)
    def checkOutputs(self):
        if self.options.verbose:
            print("Comparing outputs")
        for interface, expected in self.expected.iteritems():
            direction = "out"
            file = self.filename(interface, direction)
            if os.stat(file).st_size == 0:
                packets = []
            else:
                try:
                    packets = rdpcap(file)
                except:
                    reportError("Corrupt pcap file", file)
                    self.showLog()
                    return FAILURE
            if len(expected) != len(packets):
                reportError("Expected", len(expected), "packets on port", interface,
                            "got", len(packets))
                self.showLog()
                return FAILURE
            for i in range(0, len(expected)):
                cmp = self.comparePacket(expected[i], packets[i])
                if cmp != SUCCESS:
                    reportError("Packet", i, "on port", interface, "differs")
                    return FAILURE
        return SUCCESS

def run_model(options, tmpdir, jsonfile, testfile):
    bmv2 = RunBMV2(tmpdir, options, jsonfile)
    result = bmv2.generate_model_inputs(testfile)
    if result != SUCCESS:
        return result
    result = bmv2.run()
    if result != SUCCESS:
        return result
    result = bmv2.checkOutputs()
    return result

######################### main

def usage(options):
    print("usage:", options.binary, "[-v] <json file> <stf file>");

def main(argv):
    options = Options()
    options.binary = argv[0]
    argv = argv[1:]
    while len(argv) > 0 and argv[0][0] == '-':
        if argv[0] == "-b":
            options.preserveTmp = True
        elif argv[0] == "-v":
            options.verbose = True
        else:
            reportError("Uknown option ", argv[0])
            usage(options)
        argv = argv[1:]
    if len(argv) < 2:
        usage(options)
        return FAILURE
    if not os.path.isfile(argv[0]) or not os.path.isfile(argv[1]):
        usage(options)
        return FAILURE

    tmpdir = tempfile.mkdtemp(dir=".")
    result = run_model(options, tmpdir, argv[0], argv[1])
    if options.preserveTmp:
        print("preserving", tmpdir)
    else:
        shutil.rmtree(tmpdir)
    if options.verbose:
        if result == SUCCESS:
            print("SUCCESS")
        else:
            print("FAILURE", result)
    return result

if __name__ == "__main__":
    main(sys.argv)
