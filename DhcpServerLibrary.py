#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import print_function

import os

import threading

import gobject
import dbus
import dbus.mainloop.glib

import subprocess


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
    
    def add_lease(self, ipv4_address, hw_address):
        """
        Add a new entry in the database with ipv4_address allocated to entry hw_address
        """
        with self.leases_dict_mutex:
            self.leases_dict[hw_address] = ipv4_address
    
    def update_lease(self, ipv4_address, hw_address):
        """
        Update an existing entry in the database with ipv4_address allocated to entry hw_address
        """
        self.add_lease(ipv4_address, hw_address)
        
    def delete_lease(self, hw_address, raise_exceptions = False):
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
    
    def __init__(self, ifname, dhcp_server_pid_file_path = None):
        """
        Instantiate a new DnsmasqDhcpServerWrapper object that represents a DHCP client remotely-controlled via D-Bus
        This RemoteDhcpClientControl object will mimic the status/methods of the remotely-controlled DHCP client so that we can interact with RemoteDhcpClientControl without any knowledge of the actual remotely-controller DHCP client
        """
        if dhcp_server_pid_file_path is None:
            self._dhcp_server_pid_file_path = DnsmasqDhcpServerWrapper.DNSMASQ_DEFAULT_PID_FILE
        self._lease_database = DhcpServerLeaseList()    

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
        
        self._getversion_unlock_event = threading.Event() # Create a new threading event that will allow the GetVersion() D-Bus call below to execute within a timed limit 

        self._getversion_unlock_event.clear()
        self._remote_version = ''
        slave_version = self._dbus_iface.GetVersion(reply_handler = self._getVersionUnlock, error_handler = self._getVersionError)
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
        
        with open(self._dhcp_server_pid_file_path, 'r') as f:
            dnsmasq_pid_str = f.readline()
            
        if not dnsmasq_pid_str:
            raise Exception('EmptyPIDFile')
        
        dnsmasq_pid = int(dnsmasq_pid_str)
        
        args = ['sudo', 'kill', '-SIGHUP', str(dnsmasq_pid)]    # Send SIGHUP to dnsmasq
        subprocess.call(args, stdout=open(os.devnull, 'wb'), stderr=subprocess.STDOUT)

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
        Method called when receiving the DhcpLeaseAdded D-Bus signal from dnsmasq
        """
        logger.debug('Got signal DhcpLeaseAdded for IP=' + str(ipaddr) + ', MAC=' + str(hwaddr))
        self._lease_database.add_lease(str(ipaddr), str(hwaddr))    # Note: ipaddr and hwaddr are of type dbus.String, so convert them to python native str
          
    def _handleDhcpLeaseUpdated(self, ipaddr, hwaddr, hostname, **kwargs):
        """
        Method called when receiving the DhcpLeaseUpdated D-Bus signal from dnsmasq
        """
        logger.debug('Got signal DhcpLeaseUpdated for IP=' + str(ipaddr) + ', MAC=' + str(hwaddr))
        self._lease_database.update_lease(str(ipaddr), str(hwaddr))    # Note: ipaddr and hwaddr are of type dbus.String, so convert them to python native str
        
    def _handleDhcpLeaseDeleted(self, ipaddr, hwaddr, hostname, **kwargs):
        """
        Method called when receiving the DhcpLeaseDeleted D-Bus signal from dnsmasq
        """
        logger.debug('Got signal DhcpLeaseDeleted for IP=' + str(ipaddr) + ', MAC=' + str(hwaddr))
        self._lease_database.delete_lease(str(hwaddr))  # Note: hwaddr is  of type dbus.String, so convert it to python native str
        
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
        """
        return self._lease_database.get_ipv4address_for_hwaddress(mac)
    
class DhcpServerLibrary:
    """ Robot Framework DHCP Library """

    ROBOT_LIBRARY_DOC_FORMAT = 'ROBOT'
    ROBOT_LIBRARY_SCOPE = 'GLOBAL'
    ROBOT_LIBRARY_VERSION = '1.0'

    def __init__(self, ifname = None, dhcp_server_pid_file_path = None):
        """Initialise the library
        dhcp_server_pid_file_path is a PATH to the PID file storing the PID of the running instance of dnsmasq
        ifname is the interface on which we are observing the DHCP server status. If not provided, it will be mandatory to set it using Set Interface and before running Start
        """
        self._dhcp_server_pid_file_path = dhcp_server_pid_file_path 
        self._ifname = ifname
        self._dnsmasq_wrapper = None    # Underlying dnsmasq observer object
        
    def set_interface(self, ifname):
        """Set the current DHCP server interface on which we are working
        This must be done prior to calling Start or subsequent actions will fail
        
        Example:
        | Set Interface | 'eth0' |
        """
        
        # Start an instance of the DHCP dnsmasq observer object on the specified interface
        self._ifname = ifname
        
    def get_current_interface(self, ifname):
        """Get the interface on which the DHCP client is configured to run (it may not be started yet)
        Will return None if no interface has been configured yet
        
        Example:
        | Set Interface | eth1 |
        | Get Current Interface |
        =>
        | 'eth1' |
        """
        
        return self._ifname

    def start(self, ifname = None):
        """Start the DHCP client
        
        Example:
        | Start | eth0 |
        """
        
        if not ifname is None:
             self._ifname = ifname
        
        if self._ifname is None:
            raise Exception('NoInterfaceProvided')
        
        self._dnsmasq_wrapper = DnsmasqDhcpServerWrapper(self._ifname, self._dhcp_server_pid_file_path)
        
        logger.debug('DHCP server is now being observed on ' + self._ifname)
        
    def stop(self):
        """ Stop the DHCP client

        Example:
        | Stop |
        """

        if not self._dnsmasq_wrapper is None:
            self._dnsmasq_wrapper.exit()
        logger.debug('DHCP server not observed anymore on ' + self._ifname)
        
        self._dnsmasq_wrapper = None   # Destroy the dnsmasq wrapper object
        
    
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

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)    # Use Glib's mainloop as the default loop for all subsequent code

if __name__ == '__main__':
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

    client = DhcpServerLibrary('eth1')
    client.start()
    try:
        print('New DHCP events will be displayed in real time on the console')
        print('Press Ctrl+C or enter exit to stop this program')
        print('Press enter to dump all DHCP leases or enter a MAC address to search the corresponding lease)
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
    finally:
        client.stop()
else:
    from robot.api import logger
