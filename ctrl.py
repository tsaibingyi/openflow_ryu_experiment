from ryu.app import simple_switch_stp_13
from ryu.controller import ofp_event
from ryu.controller.handler import (
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, ether_types
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
            1: {2: 64000, 3: 16000, 4: 16000},
            2: {1: 64000, 3: 16000, 4: 16000},
            3: {1: 16000, 2: 16000},
            4: {1: 16000, 2: 16000}
        }
        # Store port statistics for each switch and port (datapath_id : {port_no: [tx_bytes, rx_bytes]})
        self.ports_stats = {}
        # Store port statistics 1 sec ago for each switch and port (datapath_id : {port_no: [tx_bytes, rx_bytes]})
        self.ports_stats_old = {}
        # Store port bitrate (1 sec average) for each switch and port (datapath_id : {port_no: [tx_bitrate(B/s), rx_bitrate(B/s)]})
        self.ports_bitrate = {}
        self.bitrate_threshold = 0.9  # 90% of the port bitrate limit


    # Handler for state change events
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        port_no = list(datapath.ports.keys())
        
        if ev.state == MAIN_DISPATCHER:
            if not datapath.id in self.datapaths:
                self.logger.info('**Register datapath: %04x', datapath.id)
                self.datapaths[datapath.id] = datapath
                self.ports_stats[datapath.id] = {port: [0, 0] for port in port_no if port != ofproto_v1_3.OFPP_LOCAL}
                self.ports_stats_old[datapath.id] = {port: [0, 0] for port in port_no if port != ofproto_v1_3.OFPP_LOCAL}
                self.ports_bitrate[datapath.id] = {port: [0, 0] for port in port_no if port != ofproto_v1_3.OFPP_LOCAL}

        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                self.logger.info('**Unregister datapath: %04x', datapath.id)
                del self.datapaths[datapath.id]

    # Periodically request stats from all datapaths
    def _monitor(self):
        while True:
            for dp in self.datapaths.values():
                self._request_stats(dp)
            hub.sleep(1)

    # Send network port stats request to the datapath
    def _request_stats(self, datapath):
        self.logger.debug('**send stats request: %04x', datapath.id)
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)


    # Handler for port stats reply events
    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        ofproto = ev.msg.datapath.ofproto
        body = ev.msg.body

        for stat in body:
            port_no = stat.port_no
            if port_no == ofproto.OFPP_LOCAL:
                continue
            tx_bytes = stat.tx_bytes
            rx_bytes = stat.rx_bytes
            self.ports_stats_old[dpid][port_no][0] = self.ports_stats[dpid][port_no][0]
            self.ports_stats_old[dpid][port_no][1] = self.ports_stats[dpid][port_no][1]
            self.ports_stats[dpid][port_no][0] = tx_bytes
            self.ports_stats[dpid][port_no][1] = rx_bytes
            self.ports_bitrate[dpid][port_no][0] = (self.ports_stats[dpid][port_no][0] - self.ports_stats_old[dpid][port_no][0]) * 8 / 10**3
            self.ports_bitrate[dpid][port_no][1] = (self.ports_stats[dpid][port_no][1] - self.ports_stats_old[dpid][port_no][1]) * 8 / 10**3
            self.log_port_bitrate()

    # Log the current port bitrate for all datapaths
    def log_port_bitrate(self):
        for dpid, ports in self.ports_bitrate.items():
            for port_no, bitrate in ports.items():
                self.logger.info('Datapath: {:04x}, Port: {}, TX: {:>7,} Kbps, RX: {:>7,} Kbps'.format(dpid, port_no, bitrate[0], bitrate[1]))
        self.logger.info(' ')

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
        if eth.ethertype == ether_types.ETH_TYPE_LLDP or eth.dst.startswith('01:80:c2:'):
            return
        
        src = eth.src 
        dst = eth.dst
        match_ip = pkt.get_protocol(ipv4.ipv4)
        
        # IP flow control for specific switches
        if dpid == 1 and match_ip and in_port == 1:
            self.s1_ip_flow_ctrl(datapath, parser, ofproto, msg, in_port)
            return
        elif dpid == 2 and match_ip and in_port == 2:
            self.s2_ip_flow_ctrl(datapath, parser, ofproto, msg, in_port)
            return
        super(SimpleSwitch, self)._packet_in_handler(ev)

    
    def s1_ip_flow_ctrl(self, datapath, parser, ofproto, msg, in_port):
        s1p2_tx = self.ports_bitrate.get(1, {}).get(2, [0, 0])[0]
        s1p3_tx = self.ports_bitrate.get(1, {}).get(3, [0, 0])[0]
        s2p1_rx = self.ports_bitrate.get(2, {}).get(1, [0, 0])[1]
        s2p3_rx = self.ports_bitrate.get(2, {}).get(3, [0, 0])[1]

        port2_limit = self.port_bitrate_limit[1][2] * self.bitrate_threshold
        port3_limit = self.port_bitrate_limit[1][3] * self.bitrate_threshold

        if max(s1p2_tx, s2p1_rx) >= port2_limit:
            if max(s1p3_tx, s2p3_rx) >= port3_limit:
                out_port = 4
            else:
                out_port = 3
        else:
            out_port = 2

        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                ipv4_src='10.0.0.1',
                                ipv4_dst='10.0.0.2')
        actions = [parser.OFPActionOutput(out_port)]
        
        mod = parser.OFPFlowMod(datapath=datapath, priority=10,
                                match=match,
                                instructions=[parser.OFPInstructionActions(
                                    ofproto.OFPIT_APPLY_ACTIONS, actions)],
                                idle_timeout=5, hard_timeout=15)
        datapath.send_msg(mod)

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                    in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)
        return

    def s2_ip_flow_ctrl(self, datapath, parser, ofproto, msg, in_port):
        s2p1_tx = self.ports_bitrate.get(2, {}).get(1, [0, 0])[0]
        s2p3_tx = self.ports_bitrate.get(2, {}).get(3, [0, 0])[0]
        s1p2_rx = self.ports_bitrate.get(1, {}).get(2, [0, 0])[1]
        s1p3_rx = self.ports_bitrate.get(1, {}).get(3, [0, 0])[1]

        port1_limit = self.port_bitrate_limit[2][1] * self.bitrate_threshold
        port3_limit = self.port_bitrate_limit[2][3] * self.bitrate_threshold

        if max(s2p1_tx, s1p2_rx) >= port1_limit:
            if max(s2p3_tx, s1p3_rx) >= port3_limit:
                out_port = 2
            else:
                out_port = 3
        else:
            out_port = 1

        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                ipv4_src='10.0.0.2',
                                ipv4_dst='10.0.0.1')
        actions = [parser.OFPActionOutput(out_port)]

        mod = parser.OFPFlowMod(datapath=datapath, priority=10,
                                match=match,
                                instructions=[parser.OFPInstructionActions(
                                    ofproto.OFPIT_APPLY_ACTIONS, actions)],
                                idle_timeout=5, hard_timeout=15)
        datapath.send_msg(mod)

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                    in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)
        return
    