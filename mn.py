#!/usr/bin/env python

from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.node import Host
from mininet.node import OVSKernelSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.link import TCLink

net = None

def myNetwork():
    global net
    net = Mininet( topo=None,
                   build=False,
                   ipBase='10.0.0.0/8',
                   autoSetMacs=True)

    info( '*** Adding controller\n' )
    c1 = net.addController(name='c1', controller=RemoteController, ip='127.0.0.1', port=6653)

    info( '*** Add switches\n')
    s3 = net.addSwitch('s3', cls=OVSKernelSwitch)
    s2 = net.addSwitch('s2', cls=OVSKernelSwitch)
    s4 = net.addSwitch('s4', cls=OVSKernelSwitch)
    s1 = net.addSwitch('s1', cls=OVSKernelSwitch)

    info( '*** Add hosts\n')
    h1 = net.addHost('h1', cls=Host, ip='10.0.0.1', defaultRoute=None)
    h2 = net.addHost('h2', cls=Host, ip='10.0.0.2', defaultRoute=None)

    info( '*** Add links\n')
    net.addLink(h1, s1)
    s1s2 = {'bw':64}
    net.addLink(s1, s2, cls=TCLink , **s1s2)
    net.addLink(s2, h2)
    s1s3 = {'bw':16}
    net.addLink(s1, s3, cls=TCLink , **s1s3)
    s2s3 = {'bw':16}
    net.addLink(s2, s3, cls=TCLink , **s2s3)
    s1s4 = {'bw':16}
    net.addLink(s1, s4, cls=TCLink , **s1s4)
    s2s4 = {'bw':16}
    net.addLink(s2, s4, cls=TCLink , **s2s4)

    info( '*** Starting network\n')
    net.build()
    info( '*** Starting controllers\n')
    for controller in net.controllers:
        controller.start()

    info( '*** Starting switches\n')
    for switch in net.switches:
        switch.start(net.controllers)

    info( '\n*** Post configure switches and hosts\n')

    CLI(net)
    net.stop()

def do_start_server(self, line):
    global net
    h2 = net.get('h2')
    
    cmd = "xterm -hold -e 'bash /media/sf_VboxShare/udp_ctrl/Mininet_CLI_Func/server.sh' &"
    h2.cmd(cmd)
    print("The Server is started on h2.")
CLI.do_start_server = do_start_server

def do_start_test(self, line):
    global net
    h2 = net.get('h2')
    
    cmd = "xterm -hold -e 'bash /media/sf_VboxShare/udp_ctrl/Mininet_CLI_Func/client.sh' &"
    h1 = net.get('h1')
    h1.cmd(cmd)
    print("The Client Test is started on h1.")
CLI.do_start_test = do_start_test

if __name__ == '__main__':
    setLogLevel( 'info' )
    myNetwork()

