#!/bin/bash

mkdir -p iperf_server_logs
iperf3 --server --port 10001 --format m > "iperf_server_logs/server.log" 2>&1

wait