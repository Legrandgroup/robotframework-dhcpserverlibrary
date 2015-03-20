DhcpServerLibrary for Robot Framework
=====================================


## Introduction

DhcpServerLibrary is a [Robot Framework](http://robotframework.org) test
library for testing a DHCP client.

This library allows Robot Framework to interact with a dnsmasq DHCP server, and
to process DHCP events coming from DHCP clients, using Robot Framework
keywords

Currently, only dnsmasq is supported as DHCP server, and it must be installed
separately from this library (see below).
RobotFramework will then be able to be notified when leases are added, updated
and deleted from dnsmasq

DhcpServerLibrary is open source software licensed under
[Apache License 2.0](http://www.apache.org/licenses/LICENSE-2.0.html).

## For users

### Installing dnsmasq

This RobotFramework library uses
[dnsmasq](http://www.thekelleys.org.uk/dnsmasq/doc.html) as a DHCP server.

dnsmasq is indeed able to notify, via D-Bus signals, any change in its lease
file (addition/renewal or deletion of a lease)

dnsmasq is available in most distributions, for example, under Debian, you will
only have to install the dnsmasq package.

The DhcpServerLibrary takes care of starting and stopping dnsmasq on the network
interface specified in the test.

It is thus not needed to configure dnsmasq on your OS before using this library.

Moreove, it is mandatory not to have dnsmasq running in your init scripts
(in /etc/rc*.d/S??dnsmasq)

### Usage restrictions

Due to a current shortcoming inside dnsmasq, D-Bus signals issued by dnsmasq do
not contain the detail of the network interface that is concerned by lease
modification messages.

This means that the DhcpServerLibrary will intercept all notifications
of all dnsmasq processes running on the test machine (if these instances have
the option enable-dbus in their configuration file)

DhcpServerLibrary will thus build a knowledge of all DHCP leases it is aware of,
even if some leases are possibly not in the scope of the network interface on
which DhcpServerLibrary started a dnsmasq instance.
This might not have any impact on the test, but it is better to avoid having
more than one instance of dnsmasq running on the test machine to avoid DHCP
lease poisonning.

Because of this usage restriction, the DhcpServerLibrary library currently uses
the very same PID file than the one provided by the Debian init script, because
we assume there should be only one dnsmasq running at any time.

In the same way, the DhcpServerLibrary library does not allow to be run more
than once concurrently. This is guaranteed by an exception raised if the keywork
**`Start`** is run twice without having run the keyword **`Stop`** in the
meantime.

### Installation

First, get a working instance of
[dnsmasq](http://www.thekelleys.org.uk/dnsmasq/doc.html) running on the
machine that will also run Robot Framework.

To install this libary, run the `./setup.py install` command locate inside the
repository.

### Setting the D-Bus permissions

In order to allow the D-Bus messages used by DhcpServerLibrary (on the system bus),
you will need to setup the permissions accordingly.

Here is a sample permission file to save in /etc/d-bus-1/system.d:

```XML
<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy context="default">
    <allow own="uk.org.thekelleys.dnsmasq"/>
    <allow send_destination="uk.org.thekelleys.dnsmasq"/>
  </policy>
</busconfig>
```

### Robot Framework keywords

The following RobotFramework keywords are made available by this library:
Note: it is advised to go directly inside the python code's docstrings (or via
RIDE's online help) to get a detailed description of keywords).

#### `Start`

*Start the DHCP server*

Note: a network interface must be either:
* have been provided using **`Set Interface`** prior to calling **`Start`**
* be provided as an argument to **`Start`**

#### `Stop`

*Stop the DHCP server*

Warning: It is really mandatory to call **`Stop`** each time **`Start`** is
called. Thus, the best is to take the habit to use **`Stop`** in the teardown
(in case a test fails)

#### `Restart`

*Equivalent to `Start`+`Stop`*

#### `Set Interface`

*Set the network interface on which the **`Start`**  and
**`Restart Monitoring Server`** keywords will be applied*

eg: `eth1`

#### `Get Current Interface`

*Get the network interface configured using **`Set Interface`** *

#### `Stop Monitoring Server`

*Stop monitoring DHCP leases updates on the DHCP server*

Note: This will not stop the DHCP server itself, but once this keyword is used,
the DhcpServerLibrary will not take into account updates to DHCP leases anymore
(until **`Restart Monitoring Server`** is used)
This means that even after using **`Stop Monitoring Server`**, the keyword
**`Stop`** must be run before RobotFramework terminates (or the dnsmasq process
will carry on running)

#### `Restart Monitoring Server`

*Restart monitoring DHCP leases updates on the DHCP server (that would have
been stopped using **`Stop Monitoring Server`** *

#### `Set Lease Time`

*Sets lease duration on the DHCP server*

Note: This keyword will have no impact if invoked after keyword **`Start`**
(lease duration can also be provided as an optional argument of keyword
**`Start`**)

#### `Log Leases`

*Dump all known leases into RobotFramework logs*

#### `Find IP For Mac`

*Recherche l'adresse IP correspondant à une adresse MAC*

Si un bail existe pour cette adresse MAC, l'adresse IP est renvoyée en valeur de retour, sinon, None est renvoyé (mais le mot clé n'échouera pas)

#### `Wait Lease`

*Attend qu'un bail DHCP soit alloué au client DHCP dont l'adresse MAC est fournie en paramètre*

Un paramètre de timeout peut être fourni pour borner le temps d'attente. Si ce timeout n'est pas fourni, le mot clé n'attendra aucun délai (et échouera ou passera immédiatement)
Renvoie l'adresse IP allouée en valeur de retour

#### `Reset Lease Database`

*Oublie tous les clients DHCP appris par le serveur DHCP jusqu'à présent (utile juste avant les mots clés Check Dhcp Client On et Check Dhcp Client Off)*

#### `Check Dhcp Client On`

*Vérifie qu'un client DHCP a actuellement un bail DHCP valide ou le renouvelle dans le délai fourni en paramètre*

Si un délai 0 est fourni, la vérification se fait immédiatement sur la base de données des baux en vigueur au moment de l'invocation de ce mot-clé
Si aucun délai n'est fourni, mais que Set Lease Time a été appelé auparavant, un demi-bail DHCP sera pris comme délai par défaut.
Si aucun délai n'est fourni et que Set Lease Time n'a pas été appelé pour fixer une durée de bail spécifique, une exception sera levée et le testcase échouera.

#### `Check Dhcp Client Off

*Vérifie qu'un client DHCP n'a actuellement pas de bail DHCP valide ou le renouvelle dans le délai fourni en paramètre*

Si un délai 0 est fourni, la vérification se fait immédiatement sur la base de données des baux en vigueur au moment de l'invocation de ce mot-clé
Si aucun délai n'est fourni, mais que Set Lease Time a été appelé auparavant, un demi-bail DHCP sera pris comme délai par défaut.
Si aucun délai n'est fourni et que Set Lease Time n'a pas été appelé pour fixer une durée de bail spécifique, une exception sera levée et le testcase échouera.

## For developpers

### Architecture of DhcpServerLibrary

Le fonctionnement du serveur DHCP dnsmasq nécessite des droits root pour son fonctionnement.

RobotFramework ne s'exécutant pas avec de tels droits, on utilise sudo pour lancer dnsmasq depuis la librairie de test :

    DhcpServerLibrary.py est le module de connexion avec RobotFramework, avec en classe principale DhcpServerLibrary
    Ce module s'exécute dans un processus rattaché à RobotFramework, avec les droits associés (souvent sous l'utilisateur jenkins)
    Il lancera un processus fils serveur DHCP dnsmasq (via sudo) et supervisera celui-ci via D-Bus
    Il interagit avec RobotFramework en implémentant l'interface standard des librairies Python RobotFramework

dnsmasq fournit des informations à DhcpServerLibrary via des signaux D-Bus sur le bus SYSTEM, sous le chemin d'objet /org/uk.thekelleys/dnsmasq

Cet objet implémente une interface de service nommée org.uk.thekelleys.dnsmasq

#### D-Bus signals/methods used by dnsmasq

The following D-Bus signals are sent by dnsmasq (when configured using enable-dbus):

* `DhcpLeaseAdded` when a DHCP lease is allocated to a DHCP client
* `DhcpLeaseUpdated` when a DHCP lease is renewed by a DHCP client
* `DhcpLeaseDeleted` when a DHCP lease is lost by a DHCP client

The following D-Bus method is also invoked by DhcpServerLibrary on dnsmasq:

* `GetVersion()`: To get the version of dnsmasq

#### Outils de diagnostic D-Bus

### D-Bus diagnosis using D-Feet

It is possible du trace D-Bus messages sent on interface
`uk.org.thekelleys.dnsmasq`