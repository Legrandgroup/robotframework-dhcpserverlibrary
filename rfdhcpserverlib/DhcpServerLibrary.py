#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import print_function

import os

import threading
import atexit

import gobject
import dbus
import dbus.mainloop.glib

import time
import subprocess

client = None

# This cleanup handler is not used when this library is imported in RF, only when run as standalone
if __name__ == '__main__':
    def cleanupAtExit():
        """
        Called when this program is terminated, to perform the same cleanup as expected in Teardown when run within Robotframework
        """
        
        global client
        
        client.stop()

class DhcpServerLeaseList:
    """
    This class stores the information of all leases as published by the DHCP server
    """
    def __init__(self):
        self.leases_dict_mutex = threading.Lock()    # This mutex protects writes to the leases_list attribute
        self.reset()
        
    def reset(self):
        """
        Reset the database to empty
        """
        self.leases_dict = {}
    
    def addLease(self, ipv4_address, hw_address):
        """
        Add a new entry in the database with ipv4_address allocated to entry hw_address
        """
        with self.leases_dict_mutex:
            self.leases_dict[hw_address] = ipv4_address
    
    def updateLease(self, ipv4_address, hw_address):
        """
        Update an existing entry in the database with ipv4_address allocated to entry hw_address
        """
        self.addLease(ipv4_address, hw_address)
        
    def deleteLease(self, hw_address, raise_exceptions = False):
        """
        Delete an entry in the database, from its hw_address key
        If raise_exceptions is set to True, deleting a non-existing key will raise a TypeError exception
        """
        try:
            with self.leases_dict_mutex:
                del self.leases_dict[hw_address]
        except TypeError:
            if raise_exceptions:
                raise
        except KeyError:
            logger.warning('Entry for MAC address ' + hw_address + ' cannot be deleted because it does not exist (maybe database has been reset in the meantime)')
    
    def get_ipv4address_for_hwaddress(self, hw_address):
        """
        Get the ipv4_address value associated to the provided hw_address argument or None if this hw_address was not found
        """
        try:
            return self.leases_dict[hw_address]
        except KeyError:
            return None
        
    def to_tuple_list(self):
        """
        Returns our current database as a list of tuples of (hw_address, ipv4_address)
        """ 
        return self.leases_dict.items()

class DnsmasqDhcpServerWrapper:

    """
    DHCP server monitoring
    This is based on a running instance of dnsmasq acting as DHCP server (see http://www.thekelleys.org.uk/dnsmasq/doc.html)
    """

    DNSMASQ_DBUS_NAME = 'uk.org.thekelleys.dnsmasq'
    DNSMASQ_DBUS_OBJECT_PATH = '/uk/org/thekelleys/dnsmasq'
    DNSMASQ_DBUS_SERVICE_INTERFACE = 'uk.org.thekelleys.dnsmasq'
    DNSMASQ_DEFAULT_PID_FILE = '/var/run/dnsmasq/dnsmasq.pid'   # Default value on Debian
    
    def __init__(self, ifname):
        """
        Instantiate a new DnsmasqDhcpServerWrapper object that observes a dnsmasq DHCP server via D-Bus
        """
        self._lease_database = DhcpServerLeaseList()
        self._ifname = ifname   # We store the interface but dnsmasq does not provide information concerning the interface in its D-Bus announcements... so we cannot use it for now
        # This also means that we can have only one instance of dnsmasq on the machine, or leases for all interfaces will mix in our database
        
        self._watched_macaddr = None    # The MAC address on which we are currently waiting for a lease to be allocated (or renewed)
        self.watched_macaddr_got_lease_event = threading.Event() # At initialisation, event is cleared 

        self._dbus_loop = gobject.MainLoop()
        self._bus = dbus.SystemBus()
        wait_bus_owner_timeout = 5  # Wait for 5s to have an owner for the bus name we are expecting
        logger.debug('Going to wait for an owner on bus name ' + DnsmasqDhcpServerWrapper.DNSMASQ_DBUS_NAME)
        while not self._bus.name_has_owner(DnsmasqDhcpServerWrapper.DNSMASQ_DBUS_NAME):
            time.sleep(0.2)
            wait_bus_owner_timeout -= 0.2
            if wait_bus_owner_timeout <= 0: # We timeout without having an owner for the expected bus name
                raise Exception('No owner found for bus name ' + DnsmasqDhcpServerWrapper.DNSMASQ_DBUS_NAME)
        
        logger.debug('Got an owner for bus name ' + DnsmasqDhcpServerWrapper.DNSMASQ_DBUS_NAME)
        gobject.threads_init()    # Allow the mainloop to run as an independent thread
        dbus.mainloop.glib.threads_init()
        
        dbus_object_name = DnsmasqDhcpServerWrapper.DNSMASQ_DBUS_OBJECT_PATH
        logger.debug('Going to communicate with object ' + dbus_object_name)
        self._dnsmasq_proxy = self._bus.get_object(DnsmasqDhcpServerWrapper.DNSMASQ_DBUS_SERVICE_INTERFACE, dbus_object_name)   # Required to attach to signals
        self._dbus_iface = dbus.Interface(self._dnsmasq_proxy, DnsmasqDhcpServerWrapper.DNSMASQ_DBUS_SERVICE_INTERFACE) # Required to invoke methods
        
        logger.debug("Connected to D-Bus")
        self._dnsmasq_proxy.connect_to_signal("DhcpLeaseAdded",
                                              self._handleDhcpLeaseAdded,
                                              dbus_interface = DnsmasqDhcpServerWrapper.DNSMASQ_DBUS_SERVICE_INTERFACE,
                                              message_keyword='dbus_message')   # Handle the IpConfigApplied signal

        self._dnsmasq_proxy.connect_to_signal("DhcpLeaseUpdated",
                                              self._handleDhcpLeaseUpdated,
                                              dbus_interface = DnsmasqDhcpServerWrapper.DNSMASQ_DBUS_SERVICE_INTERFACE,
                                              message_keyword='dbus_message')   # Handle the IpConfigApplied signal

        self._dnsmasq_proxy.connect_to_signal("DhcpLeaseDeleted",
                                              self._handleDhcpLeaseDeleted,
                                              dbus_interface = DnsmasqDhcpServerWrapper.DNSMASQ_DBUS_SERVICE_INTERFACE,
                                              message_keyword='dbus_message')   # Handle the IpConfigApplied signal
        
        
        self._dbus_loop_thread = threading.Thread(target = self._loopHandleDbus)    # Start handling D-Bus messages in a background thread
        self._dbus_loop_thread.setDaemon(True)    # D-Bus loop should be forced to terminate when main program exits
        self._dbus_loop_thread.start()
        
        self._bus.watch_name_owner(DnsmasqDhcpServerWrapper.DNSMASQ_DBUS_NAME, self._handleBusOwnerChanged) # Install a callback to run when the bus owner changes
        
        self._getversion_unlock_event = threading.Event() # Create a new threading event that will allow the GetVersion() D-Bus call below to execute within a timed limit 

        self._getversion_unlock_event.clear()
        self._remote_version = ''
        self._dbus_iface.GetVersion(reply_handler = self._getVersionUnlock, error_handler = self._getVersionError)
        if not self._getversion_unlock_event.wait(4):   # We give 4s for slave to answer the GetVersion() request
            raise Exception('TimeoutOnGetVersion')
        else:
            logger.debug('dnsmasq version: ' + self._remote_version)
        
        self.reset()

        
    def reset(self):
        """
        Reset the internal database of leases by sending a SIGHUP to dnsmasq
        """
        
        self._lease_database.reset()   # Empty internal database

    def exit(self):
        """
        Terminate the D-Bus handlers and the D-Bus loop
        """
        if self._dbus_iface is None:
            raise Exception('Method invoked on non existing D-Bus interface')
        # Stop the dbus loop
        if not self._dbus_loop is None:
            self._dbus_loop.quit()
        
        self._dbus_loop = None
    
    # D-Bus-related methods
    def _loopHandleDbus(self):
        """
        This method should be run within a thread... This thread's aim is to run the Glib's main loop while the main thread does other actions in the meantime
        This methods will loop infinitely to receive and send D-Bus messages and will only stop looping when the value of self._loopDbus is set to False (or when the Glib's main loop is stopped using .quit()) 
        """
        logger.debug("Starting dbus mainloop")
        self._dbus_loop.run()
        logger.debug("Stopping dbus mainloop")
    
    def _getVersionUnlock(self, return_value):
        """
        This method is used as a callback for asynchronous D-Bus method call to GetVersion()
        It is run as a reply_handler to unlock the wait() on _getversion_unlock_event
        """
        #logger.debug('_getVersionUnlock() called')
        self._remote_version = str(return_value)
        self._getversion_unlock_event.set() # Unlock the wait() on self._getversion_unlock_event
        
    def _getVersionError(self, remote_exception):
        """
        This method is used as a callback for asynchronous D-Bus method call to GetVersion()
        It is run as an error_handler to raise an exception when the call to GetVersion() failed
        """
        logger.error('Error on invocation of GetVersion() to slave, via D-Bus')
        raise Exception('ErrorOnDBusGetVersion')
        
    def _handleDhcpLeaseAdded(self, ipaddr, hwaddr, hostname, **kwargs):
        """
        Callback method called when receiving the DhcpLeaseAdded D-Bus signal from dnsmasq
        """
        # Note: ipaddr and hwaddr are of type dbus.String, so convert them to python native str
        ipaddr = str(ipaddr)
        hwaddr = str(hwaddr).lower()
        logger.info('Got signal DhcpLeaseAdded for IP=' + ipaddr + ', MAC=' + hwaddr)
        self._lease_database.addLease(ipaddr, hwaddr)
        if not self._watched_macaddr is None:    # We are currently waiting for a lease to be allocated (or renewed) on a specific MAC address
            if self._watched_macaddr == hwaddr:   # Both MAC addresses match, so trigger the corresponding event
                self.watched_macaddr_got_lease_event.set()
          
    def _handleDhcpLeaseUpdated(self, ipaddr, hwaddr, hostname, **kwargs):
        """
        Callback method called when receiving the DhcpLeaseUpdated D-Bus signal from dnsmasq
        """
        ipaddr = str(ipaddr)
        hwaddr = str(hwaddr).lower()
        # Note: ipaddr and hwaddr are of type dbus.String, so convert them to python native str
        logger.debug('Got signal DhcpLeaseUpdated for IP=' + ipaddr + ', MAC=' + hwaddr)
        self._lease_database.updateLease(ipaddr, hwaddr)
        if not self._watched_macaddr is None:    # We are currently waiting for a lease to be allocated (or renewed) on a specific MAC address
            if self._watched_macaddr == hwaddr:   # Both MAC addresses match, so trigger the corresponding event
                self.watched_macaddr_got_lease_event.set()
        
    def _handleDhcpLeaseDeleted(self, ipaddr, hwaddr, hostname, **kwargs):
        """
        Method called when receiving the DhcpLeaseDeleted D-Bus signal from dnsmasq
        """
        ipaddr = str(ipaddr)
        hwaddr = str(hwaddr).lower()
        # Note: ipaddr and hwaddr are of type dbus.String, so convert them to python native str
        logger.info('Got signal DhcpLeaseDeleted for IP=' + ipaddr + ', MAC=' + hwaddr)
        self._lease_database.deleteLease(hwaddr)
        
    def _handleBusOwnerChanged(self, new_owner):
        """
        Callback called when our D-Bus bus owner changes 
        """
        if new_owner == '':
            logger.warn('No owner anymore for bus name ' + DnsmasqDhcpServerWrapper.DNSMASQ_DBUS_NAME)
            raise Exception('LostDhcpSlave')
        else:
            pass # Owner exists
    
    def setMacAddrToWatch(self, mac):
        """
        Sets a MAC address to monitor.
        When this MAC address has renews/gets a lease after this method has been called, self.watched_macaddr_got_lease_event threading event will be set  
        """
        self.watched_macaddr_got_lease_event.clear()    # Make sure the threading event is cleared (will be set in _handleDhcpLeaseAdded and _handleDhcpLeaseUpdated)
        self._watched_macaddr = str(mac).lower()    # Store the expected MAC address in lowercase
        
    def getLeasesList(self):
        """
        Returns a list containing each lease object currently in our database as tuples containing:
        - the MAC address as the first element
        - the IPv4 address as the second element
        """
        return self._lease_database.to_tuple_list()
    
    def getIpForMac(self, mac):
        """
        Returns the IP address allocated by the DHCP server to the host whose MAC address matches the provided argument mac
        If this MAC address is unknown, will return None
        MAC address is case insensitive
        """
        mac = str(mac).lower()
        return self._lease_database.get_ipv4address_for_hwaddress(mac)
    
    
class SlaveDhcpServerProcess:
    """
    Slave DHCP server process manipulation
    This class allows to run a DHCP server subprocess as root, and to terminate it
    dhcp_server_daemon_exec_path contains the name of the executable that implements the DHCP server (dnsmasq is the only DHCP server supported)
    ifname is the name of the network interface on which the DHCP server will run
    if log is set to False, no logging will be performed on the logger object 
    """
    
    # The following two variables should match the user and group associated with dnsmasq in your distribution's config file (the values below are the defaults for Debian, if no override exists in /etc/default/dnsmasq)
    DNSMASQ_USER = 'dnsmasq'
    DNSMASQ_GROUP = 'nogroup'
    # This matches the PID file for Debian (this should thus be updated according to your distribution)
    # Having the same PID file as your distribution allows to make sure only one instance of dnsmasq runs on the host (between instances launched by system V and by RF during tests) 
    DNSMASQ_PIDFILE = '/var/run/dnsmasq/dnsmasq.pid'
    
    def __init__(self, dhcp_server_daemon_exec_path, ifname, log = True):
        self._slave_dhcp_server_path = dhcp_server_daemon_exec_path
        self._slave_dhcp_server_pid = None
        self._ifname = ifname
        self._log = log
        self._all_processes_pid = []  # List of all subprocessed launched by us
        self._lease_time = None
    
    def setLeaseTime(self, lease_time):
        """
        Specify the lease duration (in dnsmasq syntax)
        This must be done prior to call start or we will raise an exception
        """
        if not self._slave_dhcp_server_pid is None:
            raise Exception('DhcpServerAlreadyStarted')
        else:
            self._lease_time = lease_time
    
    def start(self):
        """
        Start the slave process
        """
        if self.isRunning():
            raise Exception('DhcpServerAlreadyStarted')
        dnsmasq_user = 'dnsmasq'
        dnsmasq_group = 'nogroup'
        dnsmasq_dir_pidfile = os.path.dirname(SlaveDhcpServerProcess.DNSMASQ_PIDFILE)
        cmd = ['sudo', 'mkdir', dnsmasq_dir_pidfile]
        subprocess.call(cmd, stdout=open(os.devnull, 'wb'), stderr=subprocess.STDOUT)   # We don't care about the result, because directory may already exist but we should not fail for that
        try:    # In a try/catch block to allow for undefined SlaveDhcpServerProcess.DNSMASQ_USER
            dnsmasq_user = ''
            dnsmasq_user = SlaveDhcpServerProcess.DNSMASQ_USER
        except NameError:
            pass

        if dnsmasq_user:
            cmd = ['sudo', 'chown', dnsmasq_user, dnsmasq_dir_pidfile]
            subprocess.check_call(cmd)
        
        try:    # In a try/catch block to allow for undefined SlaveDhcpServerProcess.DNSMASQ_GROUP
            dnsmasq_group = ''
            dnsmasq_group = SlaveDhcpServerProcess.DNSMASQ_GROUP
        except NameError:
            pass
        
        if dnsmasq_group:
            cmd = ['sudo', 'chgrp', dnsmasq_group, dnsmasq_dir_pidfile]
            subprocess.check_call(cmd)
        
        cmd = ['sudo', self._slave_dhcp_server_path]
        cmd += ['-i', self._ifname] # Specify the network interface on which we will serve IP addresses via DHCP 
        if dnsmasq_user:
            cmd += ['-u', dnsmasq_user]
        if dnsmasq_group:
            cmd += ['-g', dnsmasq_group]
        
        cmd += ['--no-resolv']  # Do not use the host's /etc/resolv.conf
        ipv4_dhcp_start_addr = '192.168.0.128'
        ipv4_dhcp_end_addr = '192.168.0.254'
        dhcp_range_arg = '--dhcp-range=' + 'interface:' + self._ifname + ',' + ipv4_dhcp_start_addr + ',' + ipv4_dhcp_end_addr
        if not self._lease_time is None:
            dhcp_range_arg += ',' + str(self._lease_time)
        cmd += [dhcp_range_arg]
        cmd += ['--dhcp-authoritative'] # We are the only DHCP server on this test subnet
        cmd += ['--log-dhcp']   # Log DHCP events to syslog
        cmd += ['--leasefile-ro']   # Do not write to a lease file
        cmd += ['-C', '-']  # Read config from stdin
        cmd += ['-x', SlaveDhcpServerProcess.DNSMASQ_PIDFILE]
        
        subprocess.check_call(cmd + ['--test'], stdin=open(os.devnull, 'rb'))    # Dry-run to check the config (stdin is EOFed in order for -C arg to read no additional config)
        # Note: the only option that we need to provide as a configuration file (here directly on stdin) is enable-dbus
        # This allows D-Bus signals to be sent out when leases are added/deleted
        # Caveat: This is not the same as the --enable-dbus option on the command line
        logger.debug('Running command ' + str(cmd))
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE) # stdin will be used as a pipe to send the config (see -C arg of dnsmasq)
        proc.communicate(input='enable-dbus')
        rc = proc.returncode
        if rc == 0: # There was no error while launching dnsmasq
            pass
        elif rc == 2:   # Address already in use
            logger.error('dnsmasq failed to bind DHCP server socket: Address already in use')
            raise Exception('DhcpPortAlreadyUsed')
        else:
            logger.error('dnsmasq failed to stard')
            raise Exception('SlaveFailed')
        
        # Read the PID from the PID file and add store this to the PID variable below
        with open(SlaveDhcpServerProcess.DNSMASQ_PIDFILE, 'r') as f:
            dnsmasq_pid_str = f.readline()
        
        if not dnsmasq_pid_str:
            raise Exception('EmptyPIDFile')

        self._slave_dhcp_server_pid = int(dnsmasq_pid_str)
        self.addSlavePid(self._slave_dhcp_server_pid) # Add the PID of the child to the list of subprocesses (note: we get sudo's PID here, not the slave PID, that we will get later on via the PID file (see RemoteDhcpClientControl.getPid())
        
    def addSlavePid(self, pid):
        """
        Add a (child) PID to the list of PIDs that we should terminate when kill() is run
        """
        logger.debug('Adding slave PID ' + str(pid))
        if not pid in self._all_processes_pid:  # Make sure we don't add twice a PID
            self._all_processes_pid += [pid] # Add

    def _checkPid(self, pid):        
        """
        Check For the existence of a UNIX PID
        """
        
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        else:
            return True
    
    def _sudoKillSubprocessFromPid(self, pid, log = True, force = False, timeout = 1):
        """
        Kill a process from it PID (first send a SIGINT)
        If argument force is set to True, wait a maximum of timeout seconds after SIGINT and send a SIGKILL if is still alive after this timeout
        """

        if log: logger.info('Sending SIGINT to slave PID ' + str(pid))
        args = ['sudo', 'kill', '-SIGINT', str(pid)]    # Send Ctrl+C to slave DHCP client process
        subprocess.call(args, stdout=open(os.devnull, 'wb'), stderr=subprocess.STDOUT)
        
        if force:
            while self._checkPid(pid):  # Loop if slave process is still running
                time.sleep(0.1)
                timeout -= 0.1
                if timeout <= 0:    # We have reached timeout... send a SIGKILL to the slave process to force termination
                    if log: logger.info('Sending SIGKILL to slave PID ' + str(pid))
                    args = ['sudo', 'kill', '-SIGKILL', str(pid)]    # Send Ctrl+C to slave DHCP client process
                    subprocess.call(args, stdout=open(os.devnull, 'wb'), stderr=subprocess.STDOUT)
                    break

    def killLastPid(self, signum = 'SIGINT', log = True):
        """
        Send a signal to the last PID in the list (the most bottom child, which will discard sudo's PID
        If no signal is specified, we will send a SIGINT, otherwise, we will use the specified signal
        """
        if len(self._all_processes_pid) == 0:
            raise Exception('NoChildPID')
        pid = self._all_processes_pid[-1]   # Get last PID
        if log: logger.info('Sending signal ' + str(signum) + ' to slave PID ' + str(pid))
        args = ['sudo', 'kill', '-' + str(signum), str(pid)]    # Send the requested signal to slave process
        subprocess.call(args, stdout=open(os.devnull, 'wb'), stderr=subprocess.STDOUT)
            
    def killSlavePids(self):
        """
        Stop all PIDs stored in the list self._all_processes_pid
        This list actually contains the list of all recorded slave processes' PIDs
        """
        for pid in self._all_processes_pid:
            self._sudoKillSubprocessFromPid(pid)
            # The code below is commented out, we will just wipe out the whole  self._all_processes_pid[] list below
            #while pid in self._all_processes_pid: self._all_processes_pid.remove(pid)   # Remove references to this child's PID in the list of children
        
        self._all_processes_pid = []    # Empty our list of PIDs
        
        self._slave_dhcp_server_pid = None    

    def kill(self):
        """
        Stop the slave process(es)
        """
        
        self.killSlavePids()
        
    def isRunning(self):
        """
        Is/Are the child process(es) currently running 
        """
        if not self.hasBeenStarted():
            return False
        
        for pid in self._all_processes_pid:
            if not self._checkPid(pid):
                return False
        
        return True
    
    def hasBeenStarted(self):
        """
        Has the child process been started by us
        """
        return (not self._slave_dhcp_server_pid is None)


class DhcpServerLibrary:
    """ Robot Framework DHCP server Library

    This library utilizes Python's
    [http://docs.python.org/2.7/library/subprocess.html|subprocess]
    module and dbus-python [http://dbus.freedesktop.org/doc/dbus-python/doc/tutorial.html]
    as well as the Python module [https://docs.python.org/2.7/library/signal.html]
    
    The library has following usage:

    - Running a DHCP server on a specific network interface and monitor this
      DHCP server to get informations of the current DHCP leases (MAC address and
      associated IP address)

    == Table of contents ==

    - `Requirement on the test machine`
    - `Specifying environment to the library`
    - `Requirements for Setup/Teardown`
    - `Warning on dnsmasq concurrent execution`

    = Requirement on the test machine =
    
    A few checks must be performed on the machine on which this library will
    run :
    - A dnsmasq DHCP server must be installed on the test machine (but NOT
    automatically running via system V init)
    - The D-Bus system bus must have appropriate permissions to allow messages
    on the BUS `uk.org.thekelleys.dnsmasq`. This is usually dones by distribution
    maintainers when installing dnsmasq as a distribution package, if this is
    not the case, a file stored in /etc/d-bus-1/system.d must be created
    For example, the following lines in a file stored in /etc/d-bus-1/system.d
    would do the job (but you may want to setup more restrictive permissions):
    <!DOCTYPE busconfig PUBLIC
    "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
    "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
    <busconfig>
      <policy context="default">
        <allow own="uk.org.thekelleys.dnsmasq"/>
        <allow send_destination="uk.org.thekelleys.dnsmasq"/>
      </policy>
    </busconfig>

    - The pybot process must have permissions to run sudo on kill and on
    the slave DHCP server (dnsmasq)
    
    = Specifying environment to the library =
    
    Before being able to run a DHCP client on an interface, this library must
    be provided with the path to the DHCP server exec (also called slave
    in this library).
    This exec should point to dnsmasq
    
    Also, the network interface on which the DHCP client service will run
    must be provided either :
    - when importing the library with the keyword `Library` 
    - by using the keyword `Set Interface` before using the keyword `Start`
    - by providing it as an optional argument when using the keyword `Start`
        
    = Requirements for Setup/Teardown =

    Whenever `DhcpServerLibrary.Start` is run within a given scope, it is
    mandatory to make sure than `DhcpServerLibrary.Stop` will also be called
    before or at Teardown to avoid runaway DHCP servers that would continue
    to server IP addresses after the test finishes

    = Warning on dnsmasq concurrent execution =
    
    Because it uses dnsmasq, this library does not support concurrent
    execution of more that one DHCP server (on network interfaces).
    The architecture of keywords has been thought to handle several
    interfaces, by switching the interface on which we issue DHCP server
    commands.
    However, although dnsmasq does support multiple simultaneous instances,
    each running on a different network interface, when sending signals over
    D-Bus, it does not mention the interface to which a signal applies (when
    leases are added, updated or deleted).
    Thus having several simultaneous dnsmasq instances running would lead
    all concurrent lease databases to all contain the whole leases for all
    interfaces.
    For now, the library is thus restricted to one DHCP server at a time,
    this meanse keyword `DhcpServerLibrary.Start` can only be run once or
    it will raise an exception. This also means that internally, the python
    code only stores one instance of DHCP wrapper, and thus only one lease
    database  
    
    = Troubleshooting =
    
    When starting dnsmasq, we first perform a --test dry-run of the config
    file. Troubleshooting logs from dnsmasq itself should thus appears on
    stderr
    If dnsmasq is not run as root (no sudo), the `Start` keyword will fail
    with an exception
    When dnsmasq is started, it will try to bind to the DHCP server port:
    if another instance of DHCP server is already running on the specified
    interface, dnsmasq will thus fail and we catch its exit value to raise
    the appropriate exception

    = Example =

    | ***** Settings *****
    | Library    DhcpServerLibrary    /usr/sbin/dnsmasq
    | Suite Setup    `DhcpServerLibrary.Start`   eth1    5m
    | Suite Teardown    `DhcpServerLibrary.Stop`
    |
    | ***** Test Cases *****
    | Example
    |     `DhcpServerLibrary.Restart Monitoring Server` eth1
    |     `DhcpServerLibrary.Wait Lease | 00:04:74:02:19:77 |
    |     `DhcpServerLibrary.Reset Lease Database |
    |     `DhcpServerLibrary.Check Dhcp Client On | 00:04:74:02:19:77 |
    |     ${temp_scalar}=    `DhcpSLibrary.Find IP For Mac`
    """

    ROBOT_LIBRARY_DOC_FORMAT = 'ROBOT'
    ROBOT_LIBRARY_SCOPE = 'GLOBAL'
    ROBOT_LIBRARY_VERSION = '1.0'
    LEASE_DURATION_MARGIN = float(10/100)   # The margin for a lease to expire (we allow the renew to be 10% late comparing to the normal lease expiry

    def __init__(self, dhcp_server_daemon_exec_path, ifname = None):
        """Initialise the library
        dhcp_server_daemon_exec_path is a PATH to the DHCP server executable program (will be run as root via sudo)
        ifname is the interface on which we are observing the DHCP server status. If not provided, it will be mandatory to set it using Set Interface and before (or when) running Start
        """
        self._dhcp_server_daemon_exec_path =  dhcp_server_daemon_exec_path
        self._ifname = ifname   # The interface on which we are currently observing the DHCP server (there could be several DHCP servers on several interfaces, but we are working on only one at a time, and it is kept in this variable)
        self._slave_dhcp_process = None # Slave DHCP server process not started
        self._dnsmasq_wrapper = None    # Underlying dnsmasq observer object
        self._lease_time = None
        
    def set_interface(self, ifname):
        """Set the current DHCP server interface on which we are working
        This must be done prior (or when) the Start keyword is called or subsequent actions will fail
        
        Example:
        | Set Interface | 'eth0' |
        """
        
        self._ifname = ifname
        
    def get_current_interface(self, ifname):
        """Get the interface on which the DHCP server is configured to run (it may not be started yet)
        Will return None if no interface has been configured yet
        
        Example:
        | Set Interface | eth1 |
        | Get Current Interface |
        =>
        | 'eth1' |
        """
        
        return self._ifname

    def set_lease_time(self, lease_time='120'):
        """Set the lease duration of the DHCP server.
        This needs to be done before the DHCP server is started or it will have no effect
        Format can include units, eg: 3m, 5h
        2 minutes is the minimum supported by the DHCP server for now
        
        Example:
        | Set Lease Time | 1h |
        """
        self._lease_time = str(lease_time)
        
    
    def start(self, ifname = None, lease_time = None):
        """Start the DHCP server and monitors its leases
        
        Example:
        | Start | eth0 |
        """
        
        if not self._slave_dhcp_process is None:
            raise Exception('DhcpServerAlreadyStarted') # For now, due to dnsmasq limitations, we can only discuss with (and thus start) one instance on dnsmasq on only one network interface
        
        if not ifname is None:
            self._ifname = ifname
        
        if self._ifname is None:
            raise Exception('NoInterfaceProvided')
        
        if not lease_time is None:
            self.set_lease_time(lease_time)
        
        self._slave_dhcp_process = SlaveDhcpServerProcess(self._dhcp_server_daemon_exec_path, self._ifname)
        if not self._lease_time is None:
            self._slave_dhcp_process.setLeaseTime(self._lease_time)
        self._slave_dhcp_process.start()

        self._monitor_dhcp_server()
        
        
    def restart_monitoring_server(self, ifname = None):
        """ Start again monitoring the leases of a DHCP server that has already started beforehand (using keyword Start)
        
        Example:
        | Restart Monitoring Server |
        or
        | Restart Monitoring Server | eth0 |
        """
        
        if not ifname is None:
            self._ifname = ifname
        
        if self._ifname is None:
            raise Exception('NoInterfaceProvided')

        self._dnsmasq_wrapper = DnsmasqDhcpServerWrapper(self._ifname)
        logger.debug('DHCP server is now being observed on ' + self._ifname)
        if not self._slave_dhcp_process is None:
            self._slave_dhcp_process.killLastPid('SIGHUP')  # Send sighup to repopulate lease database 


    def _monitor_dhcp_server(self, ifname = None):
        """
        Private method to start monitoring the DHCP server
        """
        self.restart_monitoring_server(ifname)
        
        
    def stop_monitoring_server(self):
        """ Stop monitoring the leases of the currently observed DHCP server (but don't stop the DHCP server itself).
        If the server needs to be stopped, use the keyword Stop
        
        Example:
        | Stop Monitoring Server |
        """
        
        if not self._dnsmasq_wrapper is None:
            self._dnsmasq_wrapper.exit()
            logger.debug('DHCP server not observed anymore on ' + self._ifname)
        self._dnsmasq_wrapper = None
        
        
    def stop(self):
        """ Stop the DHCP server

        Example:
        | Stop |
        """

        self.stop_monitoring_server()
        if not self._slave_dhcp_process is None:
            self._slave_dhcp_process.kill()
            logger.debug('DHCP server stopped on ' + self._ifname)
        self._slave_dhcp_process = None # Destroy the slave DHCP object
        
    
    def restart(self):
        """ Restart the DHCP client

        Example:
        | Restart |
        """

        self.stop()
        self.start()
        
        
    def log_leases(self):
        """ Print all current leases to the log
        
        Example:
        | Log Leases |
        
        The list of current leases will be dumped into RobotFramework logs
        """
        
        logger.info('Current leases in DHCP server database (printed as [(hwaddr, ipv4addr),...] tuple list):\n' + str(self._dnsmasq_wrapper.getLeasesList()))

    
    def find_ip_for_mac(self, mac):
        """ Find the IP address allocated by the DHCP server to the machine with the MAC address provided as argument
        Will return None if the MAC address is not known by the DHCP server 
        
        Example:
        | Find IP For Mac | 00:04:74:02:19:77 |
        =>
        | '192.168.0.2' |
        """
        return self._dnsmasq_wrapper.getIpForMac(mac)
    
    
    def reset_lease_database(self):
        """ Forget about all previously known leases learnt from the DHCP server
        
        Example:
        | Reset Lease Database |
        | Check Dhcp Client On | 00:04:74:02:19:77 | 30 |
        """
        self._dnsmasq_wrapper.reset()
        
    
    def check_dhcp_client_on(self, mac, timeout = None):
        """ Check that the machine with the MAC address provided as argument mac is in DHCP client mode (either has already been allocated a lease or will renew its lease during the duration of the check)
        If it is needed to make sure DHCP is still on right now (and not only that a lease has been allocated), call keyword Reset Lease Database before
        Will fail if the MAC address provided has no lease during the specified timeout.
        If timeout is not provided, we will wait for half of the lease time set using keyword Set Lease Time
        If timeout is 0, we will check that the lease is currently known right now, or fail otherwise
        
        Example:
        | Reset Lease Database |
        | Check Dhcp Client On | 00:04:74:02:19:77 |
        """ 
        if timeout is None:
            if self._lease_time is None:
                raise Exception('NoLeaseTimeProvided')
            else:
                timeout = int((DhcpServerLibrary.LEASE_DURATION_MARGIN+1.0) * float(self._lease_time)/ 2) # Calculate the timeout based on lease time and predefined margin
        
        self.wait_lease(mac, timeout)
    
    
    def check_dhcp_client_off(self, mac, timeout = None):
        """ Check that the machine with the MAC address provided as argument mac is not in DHCP client mode (has never been allocated a lease or has lost it before calling this keyword)
        If it is needed to make sure DHCP is off right now (even if a lease may has been allocated previously), call keyword Reset Lease Database before
        Will fail if the MAC address provided has a lease currently valid or that is allocated during the specified timeout.
        If timeout is not provided, we will wait for half of the lease time set using keyword Set Lease Time
        If timeout is 0, we will check that there is no known lease right now, or fail otherwise
        
        Example: 
        | Reset Lease Database |
        | Check Dhcp Client Off | 00:04:74:02:19:77 |
        """
        if timeout is None:
            if self._lease_time is None:
                raise Exception('NoLeaseTimeProvided')
            else:
                timeout = int((DhcpServerLibrary.LEASE_DURATION_MARGIN+1.0) * float(self._lease_time)/ 2) # Calculate the timeout based on lease time and predefined margin
        
        try:   # We work reverse, so we will fail if the lease was obtained. In order to do this, catch exceptions from wait_lease()
            self.wait_lease(mac, timeout)
        except:
            return
        raise Exception('Existing lease for ' + str(mac))

    
    def wait_lease(self, mac, timeout = None):
        """Wait until host with the specified MAC address gets a lease
        Will return immediately if the lease is already valid
        Otherwise, we Will wait until the specified timeout for the lease to be allocated.
        If timeout is 0, None or we have no lease for the specified host during the timeout, this keyword will fail
        
        Example:
        | Wait Lease | 00:04:74:02:19:77 | 30 |
        =>
        | '192.168.0.2' |
        """
        ip = self._dnsmasq_wrapper.getIpForMac(mac)
        if not ip is None:
            logger.info('There is a lease previously seen for device ' + str(mac) + ' associated with IP address ' + str(ip))
            return ip # Succeed
        try:
            if timeout is None:
                raise Exception('NoLeaseFound')   # Should fail, we are not allowed to wait
            timeout = int(timeout)
            if timeout <= 0:
                raise Exception('NoLeaseFound')   # Should fail, we are not allowed to wait
            else:   # There is a timeout, so carry on waiting for this lease during this timeout
                self._dnsmasq_wrapper.setMacAddrToWatch(mac)
                if not self._dnsmasq_wrapper.watched_macaddr_got_lease_event.wait(timeout):
                    raise Exception('NoLeaseFound')
        except Exception as e:
            if e.message != 'NoLeaseFound':   # If we got an exception related to anything else than the no lease found case
                raise   # Raise the exception
            else:	# Raise this unexpected exception
                raise Exception('No lease known for ' + str(mac))
    

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)    # Use Glib's mainloop as the default loop for all subsequent code

if __name__ == '__main__':
    atexit.register(cleanupAtExit)
    try:
        from console_logger import LOGGER as logger
    except ImportError:
        import logging

        logger = logging.getLogger('console_logger')
        logger.setLevel(logging.DEBUG)
        
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)

    try:
        input = raw_input
    except NameError:
        pass

    DHCP_SERVER_DAEMON = '/usr/sbin/dnsmasq'
    client = DhcpServerLibrary(DHCP_SERVER_DAEMON, 'eth1')
    client.start()
    try:
        print('New DHCP events will be displayed in real time on the console')
        print('Press Ctrl+C or enter exit to stop this program')
        print('Press enter to dump all DHCP leases or enter a MAC address to search the corresponding lease')
        while True:
            mac_address = input()
            if mac_address == 'exit':
                raise Exception('ExitOnCLI')
            elif mac_address == '':
                print("Dumping current leases:")
                client.log_leases()
            else:
                ipv4 = client.find_ip_for_mac(mac_address)
                if ipv4 is None:
                    print('MAC address ' + mac_address + ' is not known by DHCP server')
                else:
                    print('Host with MAC address ' + mac_address + ' has IPv4 address ' + str(ipv4))
                    client.reset_lease_database()
                    print('Checking that lease is renewed within 240s')
                    client.check_dhcp_client_on(mac_address, 240)
                    print('DHCP client is On')
                    client.reset_lease_database()
                    print('Checking that lease is not renewed within the next 240s')
                    client.check_dhcp_client_off(mac_address, 240)
                    print('DHCP client is Off')
    finally:
        client.stop()
else:
    from robot.api import logger
