DhcpClientLibrary for Robot Framework
=====================================

Introduction
------------

DhcpServerLibrary is a `Robot Framework <http://robotframework.org>`__ test
library for testing a DHCP client.

This library allows Robot Framework to interact with a dnsmasq DHCP server, and
to process DHCP events coming from DHCP clients, using Robot Framework
keywords

Currently, only dnsmasq is supported as DHCP server, and it must be installed
separately from this library (see below).
RobotFramework will then be able to be notified when leases are added, updated and deleted from dnsmasq

DhcpServerLibrary is open source software licensed under `Apache License 2.0
<http://www.apache.org/licenses/LICENSE-2.0.html>`__.

Installation
------------

First, get a working instance of
`dnsmasq <http://www.thekelleys.org.uk/dnsmasq/doc.html>`__ running on the
machine that will also run Robot Framework.

To install this libary, run the ./setup.py script.
