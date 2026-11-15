import csv
from ryu.app import simple_switch_stp_13
from ryu.controller import ofp_event
from ryu.controller.handler import (
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, arp, udp, ether_types
from ryu.ofproto import ofproto_v1_3


class SimpleSwitch(simple_switch_stp_13.SimpleSwitch13):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # Initialize the controller app
    def __init__(self, *args, **kwargs):
        super(SimpleSwitch, self).__init__(*args, **kwargs)
        self.monitor_thread = hub.spawn(self._monitor)
        self.datapaths = {}
        # Port bitrate limits for each switch and port (switch_id : {port_no: bitrate_limit(Kbps)})
        self.port_bitrate_limit = {
            1: {2: 64000},
            2: {1: 64000},
        }
        # Store port statistics for each switch and port (datapath_id : {port_no: [tx_bytes, rx_bytes]})
        self.ports_stats = {}
        # Store port statistics 1 sec ago for each switch and port (datapath_id : {port_no: [tx_bytes, rx_bytes]})
        self.ports_stats_old = {}
        # Store port bitrate (1 sec average) for each switch and port (datapath_id : {port_no: [tx_bitrate(Kbps), rx_bitrate(Kbps)]})
        self.ports_bitrate = {}
        self.bitrate_threshold = 0.9  # the port bitrate limit
        # Store flow statistics for each switch (datapath_id : [flow_stats])
        self.flow_stats = {}


    # Handler for state change between MAIN_DISPATCHER and DEAD_DISPATCHER
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        port_no = list(datapath.ports.keys())
        
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.datapaths:
                self.logger.info('**Register datapath: %04x', datapath.id)
                self.datapaths[datapath.id] = datapath
                self.ports_stats[datapath.id] = {port: [0, 0] for port in port_no if port != ofproto_v1_3.OFPP_LOCAL}
                self.ports_stats_old[datapath.id] = {port: [0, 0] for port in port_no if port != ofproto_v1_3.OFPP_LOCAL}
                self.ports_bitrate[datapath.id] = {port: [0, 0] for port in port_no if port != ofproto_v1_3.OFPP_LOCAL}

        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                self.logger.info('**Unregister datapath: %04x', datapath.id)
                del self.datapaths[datapath.id]
                del self.ports_stats[datapath.id]
                del self.ports_stats_old[datapath.id]
                del self.ports_bitrate[datapath.id]


    # Periodically request stats from all datapaths (every 1 second)
    def _monitor(self):
        while True:
            for dp in self.datapaths.values():
                self._request_port_stats(dp)
                self._request_flow_stats(dp)
            self._log_link_bitrate()
            hub.sleep(1)


    # Send network port stats request to {datapath}
    def _request_port_stats(self, datapath):
        self.logger.debug('**send stats request: %04x', datapath.id)
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        ofproto = ev.msg.datapath.ofproto
        body = ev.msg.body

        for stat in body:
            port_no = stat.port_no
            # Skip local port as it is not relevant for bitrate calculation
            if port_no == ofproto.OFPP_LOCAL:
                continue
            # Savety check: initialize dictionaries if not present
            if dpid not in self.ports_stats:
                self.ports_stats[dpid] = {}
                self.ports_stats_old[dpid] = {}
                self.ports_bitrate[dpid] = {}
            if port_no not in self.ports_stats[dpid]:
                self.ports_stats[dpid][port_no] = [0, 0]
                self.ports_stats_old[dpid][port_no] = [0, 0]
                self.ports_bitrate[dpid][port_no] = [0, 0]
            
            tx_bytes = stat.tx_bytes
            rx_bytes = stat.rx_bytes
            # Move current stats to old stats before overwriting with new values
            self.ports_stats_old[dpid][port_no][0] = self.ports_stats[dpid][port_no][0]
            self.ports_stats_old[dpid][port_no][1] = self.ports_stats[dpid][port_no][1]
            # Update current stats with new values
            self.ports_stats[dpid][port_no][0] = tx_bytes
            self.ports_stats[dpid][port_no][1] = rx_bytes
            # Calculate bitrate in Kbps based on the difference between current and old stats
            self.ports_bitrate[dpid][port_no][0] = (self.ports_stats[dpid][port_no][0] - self.ports_stats_old[dpid][port_no][0]) * 8 / 10**3
            self.ports_bitrate[dpid][port_no][1] = (self.ports_stats[dpid][port_no][1] - self.ports_stats_old[dpid][port_no][1]) * 8 / 10**3

    # Log the current bitrate for each link
    def _log_link_bitrate(self):
        link12_bitrate = max(self.ports_bitrate.get(1, {}).get(2, [0, 0])[0],
                             self.ports_bitrate.get(2, {}).get(1, [0, 0])[1])
        link21_bitrate = max(self.ports_bitrate.get(2, {}).get(1, [0, 0])[0],
                             self.ports_bitrate.get(1, {}).get(2, [0, 0])[1])
        # Log the link bitrate to terminal
        self.logger.info('1---2: tx {:>7,} Kbps, rx {:>7,} Kbps'.format(link12_bitrate, link21_bitrate))
    

    # Send flow stats request to {datapath}
    def _request_flow_stats(self, datapath):
        self.logger.debug('**send flow stats request: %04x', datapath.id)
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        body = ev.msg.body
        self.flow_stats[dpid] = body


    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        dpid = datapath.id

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        # Ignore LLDP and reserved multicast packets
        if eth.ethertype == ether_types.ETH_TYPE_LLDP or eth.dst.startswith('01:80:c2:'):
            return
        match_ip = pkt.get_protocol(ipv4.ipv4)
        
        # IP flow control for specific switches
        if dpid == 1:
            self._s1_ip_flow_ctrl(datapath, msg)
            return
        # Call the default packet-in handler for other packets
        super(SimpleSwitch, self)._packet_in_handler(ev)


    # Implement IP flow control logic for switch 1 based on port utilization and bitrate limits
    def _s1_ip_flow_ctrl(self, datapath, msg):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if in_port != 1 and eth.dst == '00:00:00:00:00:01':
            out_port = 1
            actions = [parser.OFPActionOutput(out_port)]
            mod = parser.OFPFlowMod(datapath=datapath, 
                                    priority=1,
                                    match=parser.OFPMatch(eth_dst='00:00:00:00:00:01'),
                                    instructions=[parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)])
            datapath.send_msg(mod)

            data = None
            if msg.buffer_id == ofproto.OFP_NO_BUFFER:
                data = msg.data
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=actions, data=data)
            datapath.send_msg(out)
            return
        
        out_port = 2

        # Extract ARP and UDP packets from the received packet
        arp_pkt = pkt.get_protocol(arp.arp)
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)

        # Determine the flow rules based on the packet type (ARP, UDP, or other IP)
        # ARP packet handling
        if arp_pkt:
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP,
                                    arp_spa='10.0.0.1')
            priority = 1500
            out_port = 2
        # IPv4 packet handling
        elif ipv4_pkt:
            udp_pkt = pkt.get_protocol(udp.udp)
            if udp_pkt:
                match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                        ip_proto=17,
                                        ipv4_src='10.0.0.1',
                                        ipv4_dst='10.0.0.2',
                                        udp_dst=udp_pkt.dst_port)
                if 10000 < udp_pkt.dst_port < 10100:
                    priority = 2000
                else:
                    priority = 1200
            else:
                match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                        ip_proto=pkt.get_protocol(ipv4.ipv4).proto,
                                        ipv4_src='10.0.0.1',
                                        ipv4_dst='10.0.0.2')
                priority = 1000
        # Other IP packet handling
        else:
            match = parser.OFPMatch(eth_type=pkt.get_protocol(ethernet.ethernet).ethertype,
                                    ipv4_src='10.0.0.1',
                                    ipv4_dst='10.0.0.2')
            priority = 1000
            out_port = 2
        
        
        actions = [parser.OFPActionOutput(out_port)]

        # Install a flow entry in the switch
        mod = parser.OFPFlowMod(datapath=datapath, 
                                priority=priority,
                                match=match,
                                instructions=[parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)],
                                idle_timeout=10, hard_timeout=30)
        datapath.send_msg(mod)

        # Send the packet out immediately after installing the flow entry
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                    in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)
        return