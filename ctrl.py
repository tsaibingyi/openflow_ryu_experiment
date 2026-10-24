from ryu.app import simple_switch_stp_13
from operator import attrgetter
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

# Port bitrate limits for each switch and port (switch_id : [bitrate_limit(Mbps) for each port])
port_bitrate_limit = {1 : [-1, 64, 16, 16],
                      2 : [64, -1, 16, 16],
                      3 : [-1, -1],
                      4 : [-1, -1]}

# Store port statistics for each switch and port (switch_id : [[tx_bytes, rx_bytes] for each port])
ports_stats = {1 : [[0,0], [0,0], [0,0], [0,0]], 
               2 : [[0,0], [0,0], [0,0], [0,0]], 
               3 : [[0,0], [0,0]], 
               4 : [[0,0], [0,0]]}
# Store port statistics 1 sec ago for each switch and port (switch_id : [[tx_bytes, rx_bytes] for each port])
ports_stats_old = {1 : [[0,0], [0,0], [0,0], [0,0]], 
                   2 : [[0,0], [0,0], [0,0], [0,0]], 
                   3 : [[0,0], [0,0]], 
                   4 : [[0,0], [0,0]]}
# Store port bitrate (1 sec average) for each switch and port (switch_id : [[tx_bitrate, rx_bitrate] for each port])
ports_bitrate = {1 : [[0,0], [0,0], [0,0], [0,0]], 
                 2 : [[0,0], [0,0], [0,0], [0,0]], 
                 3 : [[0,0], [0,0]], 
                 4 : [[0,0], [0,0]]}


class SimpleSwitch(simple_switch_stp_13.SimpleSwitch13):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # Initialize the controller app
    def __init__(self, *args, **kwargs):
        super(SimpleSwitch, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.monitor_thread = hub.spawn(self._monitor)

    # Handler for state change events
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if not datapath.id in self.datapaths:
                self.logger.info('**Register datapath: %04x', datapath.id)
                self.datapaths[datapath.id] = datapath
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
            ports_stats_old[dpid][port_no-1][0] = ports_stats[dpid][port_no-1][0]
            ports_stats_old[dpid][port_no-1][1] = ports_stats[dpid][port_no-1][1]
            ports_stats[dpid][port_no-1][0] = tx_bytes
            ports_stats[dpid][port_no-1][1] = rx_bytes
            ports_bitrate[dpid][port_no-1][0] = (ports_stats[dpid][port_no-1][0] - ports_stats_old[dpid][port_no-1][0]) * 8 / 10**3
            ports_bitrate[dpid][port_no-1][1] = (ports_stats[dpid][port_no-1][1] - ports_stats_old[dpid][port_no-1][1]) * 8 / 10**3
            self.log_port_bitrate()

    # Log the current port bitrate for all datapaths
    def log_port_bitrate(self):
        for dpid, ports in ports_bitrate.items():
            for port_no, bitrate in enumerate(ports, start=1):
                self.logger.info('Datapath: {:04x}, Port: {}, TX: {:>7,} Kbps, RX: {:>7,} Kbps'.format(dpid, port_no, bitrate[0], bitrate[1]))
        self.logger.info(' ')