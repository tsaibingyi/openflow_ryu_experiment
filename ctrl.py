import csv
import io
from ryu.app import simple_switch_stp_13
from ryu.controller import ofp_event
from ryu.controller.handler import (
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, udp, ether_types
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
        # Store port bitrate (1 sec average) for each switch and port (datapath_id : {port_no: [tx_bitrate(Kbps), rx_bitrate(Kbps)]})
        self.ports_bitrate = {}
        self.bitrate_threshold = 0.9  # the port bitrate limit
        # Store flow statistics for each switch (datapath_id : [flow_stats])
        self.flow_stats = {}
        # Initialize CSV log file and write header if not exists
        self.time_counter = 0
        self._init_csv_log()
        

    def _init_csv_log(self):
        try:
            # Check if file already exists to decide whether to write header
            with open('rate_log.csv', 'r') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header is None:
                    raise StopIteration
        except (IOError, StopIteration):
            with open('rate_log.csv', 'w') as f:
                writer = csv.writer(f)
                writer.writerow(['time', '1---2:tx', '1-3-2:tx', '1-4-2:tx', '1---2:rx', '1-3-2:rx', '1-4-2:rx'])


    # Delete a flow entry from the switch with optional match fields, priority, output port, and output group
    def del_flow(self, datapath, match_fields=None, priority=None, out_port=None, out_group=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        if match_fields is None:
            match = parser.OFPMatch()
        elif isinstance(match_fields, dict):
            match = parser.OFPMatch(**match_fields)
        else:
            match = match_fields
        
        if out_port is None:
            target_out_port = ofproto.OFPP_ANY
        else:
            target_out_port = out_port
        
        if out_group is None:
            target_out_group = ofproto.OFPG_ANY
        else:
            target_out_group = out_group
        
        target_priority = priority if priority is not None else 1

        mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE_STRICT,
            out_port=target_out_port,
            out_group=target_out_group,
            priority=target_priority,
            match=match
        )
        datapath.send_msg(mod)


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
                if datapath.id == 1:
                    self._install_default_udp_handler(datapath)

        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                self.logger.info('**Unregister datapath: %04x', datapath.id)
                del self.datapaths[datapath.id]
                del self.ports_stats[datapath.id]
                del self.ports_stats_old[datapath.id]
                del self.ports_bitrate[datapath.id]

    def _install_default_udp_handler(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch(eth_type=0x0800, ip_proto=17)  # Match IPv4 UDP packets
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=500,
            match=match,
            instructions=inst
        )
        datapath.send_msg(mod)


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
        link132_bitrate = max(self.ports_bitrate.get(1, {}).get(3, [0, 0])[0],
                              self.ports_bitrate.get(3, {}).get(1, [0, 0])[1],
                              self.ports_bitrate.get(3, {}).get(2, [0, 0])[0],
                              self.ports_bitrate.get(2, {}).get(3, [0, 0])[1])
        link231_bitrate = max(self.ports_bitrate.get(2, {}).get(3, [0, 0])[0],
                              self.ports_bitrate.get(3, {}).get(2, [0, 0])[1],
                              self.ports_bitrate.get(3, {}).get(1, [0, 0])[0],
                              self.ports_bitrate.get(1, {}).get(3, [0, 0])[1])
        link142_bitrate = max(self.ports_bitrate.get(1, {}).get(4, [0, 0])[0],
                              self.ports_bitrate.get(4, {}).get(1, [0, 0])[1],
                              self.ports_bitrate.get(4, {}).get(2, [0, 0])[0],
                              self.ports_bitrate.get(2, {}).get(4, [0, 0])[1])
        link241_bitrate = max(self.ports_bitrate.get(2, {}).get(4, [0, 0])[0],
                              self.ports_bitrate.get(4, {}).get(2, [0, 0])[1],
                              self.ports_bitrate.get(4, {}).get(1, [0, 0])[0],
                              self.ports_bitrate.get(1, {}).get(4, [0, 0])[1])
        # Log the link bitrate to terminal
        self.logger.info('1---2: tx {:>7,} Kbps, rx {:>7,} Kbps'.format(link12_bitrate, link21_bitrate))
        self.logger.info('1-3-2: tx {:>7,} Kbps, rx {:>7,} Kbps'.format(link132_bitrate, link231_bitrate))
        self.logger.info('1-4-2: tx {:>7,} Kbps, rx {:>7,} Kbps'.format(link142_bitrate, link241_bitrate))
        self.logger.info('')
        # Log the link bitrate to CSV file
        self.time_counter += 1
        row_data = [
            self.time_counter,
            link12_bitrate,
            link132_bitrate,
            link142_bitrate,
            link21_bitrate,
            link231_bitrate,
            link241_bitrate
        ]

        try:
            with open('rate_log.csv', 'a') as f:
                writer = csv.writer(f)
                writer.writerow(row_data)
        except IOError as e:
            self.logger.error('Failed to write to rate_log.csv: %s', str(e))
    

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
        if dpid == 1 and match_ip and in_port == 1:
            self._s1_ip_flow_ctrl(datapath, parser, ofproto, msg, in_port)
            return
        # Call the default packet-in handler for other packets
        super(SimpleSwitch, self)._packet_in_handler(ev)


    # Implement IP flow control logic for switch 1 based on port utilization and bitrate limits
    def _s1_ip_flow_ctrl(self, datapath, parser, ofproto, msg, in_port):
        pkt = packet.Packet(msg.data)
        udp_pkt = pkt.get_protocol(udp.udp)

        # Get the current TX and RX bitrate for relevant ports
        s1p2_tx = self.ports_bitrate.get(1, {}).get(2, [0, 0])[0]
        s1p3_tx = self.ports_bitrate.get(1, {}).get(3, [0, 0])[0]
        s2p1_rx = self.ports_bitrate.get(2, {}).get(1, [0, 0])[1]
        s2p3_rx = self.ports_bitrate.get(2, {}).get(3, [0, 0])[1]
        # Get the bitrate limits for relevant ports
        port2_limit = self.port_bitrate_limit[1][2] * self.bitrate_threshold
        port3_limit = self.port_bitrate_limit[1][3] * self.bitrate_threshold

        # Determine the output port based on the current port utilization and limits
        if max(s1p2_tx, s2p1_rx) >= port2_limit:
            if max(s1p3_tx, s2p3_rx) >= port3_limit:
                out_port = 4
            else:
                out_port = 3
        else:
            out_port = 2

        # Determine the flow match based on whether it's a UDP packet or not
        priority = 1000
        if udp_pkt:
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                    ip_proto=17,
                                    ipv4_src='10.0.0.1',
                                    ipv4_dst='10.0.0.2',
                                    udp_dst=udp_pkt.dst_port)
            if udp_pkt.dst_port > 10000 and udp_pkt.dst_port < 10100:
                priority = 2000
        else:
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                    ipv4_src='10.0.0.1',
                                    ipv4_dst='10.0.0.2')
        
        actions = [parser.OFPActionOutput(out_port)]

        if out_port == 2:
            self.replace_stp_drop_rule(target_dpid=2, target_in_port=1)
        elif out_port == 3:
            self.replace_stp_drop_rule(target_dpid=3, target_in_port=1)
            self.replace_stp_drop_rule(target_dpid=2, target_in_port=3)
        elif out_port == 4:
            self.replace_stp_drop_rule(target_dpid=4, target_in_port=1)
            self.replace_stp_drop_rule(target_dpid=2, target_in_port=4)

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

    def replace_stp_drop_rule(self, target_dpid, target_in_port):
        if target_dpid not in self.flow_stats:
            return
        
        for flow in self.flow_stats.get(target_dpid, []):
            datapath = self.datapaths[target_dpid]
            if flow.priority != 65534 or flow.instructions:
                continue

            in_port = flow.match.get('in_port')
            if in_port == target_in_port:
                self.del_flow(datapath=datapath,
                              match_fields=flow.match,
                              priority=flow.priority)

                ofproto = datapath.ofproto
                parser = datapath.ofproto_parser
                mod = parser.OFPFlowMod(datapath=datapath,
                                        priority=100,
                                        match=flow.match,
                                        instructions=[])
                datapath.send_msg(mod)