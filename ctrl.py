# -*- coding: utf-8 -*-
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
from ryu.lib import stplib


class SimpleSwitch(simple_switch_stp_13.SimpleSwitch13):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SimpleSwitch, self).__init__(*args, **kwargs)

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout
        )
        datapath.send_msg(mod)

    @set_ev_cls(stplib.EventPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):

        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        # 忽略無法解析的封包
        if eth is None:
            return

        # 忽略 LLDP 與 STP 預留群播封包
        if (eth.ethertype == ether_types.ETH_TYPE_LLDP or eth.dst.startswith('01:80:c2:')):
            return

        dst = eth.dst
        src = eth.src
        dpid = datapath.id

        # 使用父類別的 MAC Learning Table
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        # 查詢目的 MAC 對應的輸出埠
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        # 解析 IPv4 與 UDP
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        udp_pkt = pkt.get_protocol(udp.udp)

        # 判斷是否為目標 UDP 封包
        if ip_pkt and out_port != ofproto.OFPP_FLOOD:

            if udp_pkt:

                # UDP 專用流表
                match = parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_proto=17,
                    ipv4_src=ip_pkt.src,
                    ipv4_dst=ip_pkt.dst,
                    udp_dst=udp_pkt.dst_port
                )

                self.add_flow(
                    datapath,
                    100,
                    match,
                    actions,
                    idle_timeout=30,
                    hard_timeout=60
                )

                self.logger.info(
                    "[UDP Flow Installed] DPID: %s, "
                    "%s -> %s, UDP DstPort: %d, OutPort: %s",
                    dpid,
                    ip_pkt.src,
                    ip_pkt.dst,
                    udp_pkt.dst_port,
                    out_port
                )
            else:
                match = parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ip_proto=ip_pkt.proto,
                    ipv4_src=ip_pkt.src,
                    ipv4_dst=ip_pkt.dst
                )

                self.add_flow(
                    datapath,
                    10,
                    match,
                    actions,
                    idle_timeout=10,
                    hard_timeout=30
                )

        else:
            # 一般 L2 流量
            if out_port != ofproto.OFPP_FLOOD:

                match = parser.OFPMatch(
                    in_port=in_port,
                    eth_type=eth.ethertype,
                    eth_dst=dst,
                    eth_src=src
                )

                self.add_flow(
                    datapath,
                    1,
                    match,
                    actions,
                    idle_timeout=10,
                    hard_timeout=30
                )

        # Packet-Out：轉發當前封包
        data = None

        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )

        datapath.send_msg(out)
