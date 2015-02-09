#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Setup script for DHCP Server Robotframework library"""

from __future__ import with_statement
from setuptools import setup
from os.path import abspath, dirname, join

from rfdhcpserverlib import __lib_version__

def read(fname):
    """read and return fname file content"""
    curdir = dirname(abspath(__file__))
    with open(join(curdir, fname)) as filename:
        return filename.read()

CLASSIFIERS = """
Development Status :: 3 - Alpha
License :: OSI Approved :: Apache Software License
Operating System :: OS Independent
Programming Language :: Python
Topic :: Software Development :: Testing
"""[1:-1]

setup(
    name='robotframework-dhcpserverlibrary',
    version=__lib_version__,
    description='This library allows RobotFramework to test DHCP clients, by communicating with a DHCP server installed on the test platform and to handle lease events using RobotFramework keywords',
    long_description=read('README.rst'),
    author='Lionel Ains',
    author_email='lionel.ains@legrand.fr',
    url='https://github.com/Legrandgroup/robotframework-dhcpserverlibrary',
    license='Apache License 2.0',
    keywords='robotframework testing testautomation dhcp bootp server dnsmasq',
    platforms='any',
    classifiers=CLASSIFIERS.splitlines(),
    packages=['rfdhcpserverlib'],
    install_requires=['robotframework']
)
