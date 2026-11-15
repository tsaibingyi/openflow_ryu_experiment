#!/bin/bash

mkdir -p iperf_client_logs
iperf3 --client 10.0.0.2 --port 10001 --udp --bandwidth 60M --format m --time 10 > "iperf_client_logs/client.log" 2>&1

wait