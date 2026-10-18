#!/bin/bash

NUM=30

START_PORT=10001
END_PORT=$((START_PORT + NUM - 1))

mkdir -p iperf_server_logs

for i in $(seq ${START_PORT} ${END_PORT}); do
    iperf3 --server --port ${i} --format m > "iperf_server_logs/server_${i}.log" 2>&1 &
done

echo "${NUM} iperf3 servers (Port ${START_PORT}-${END_PORT}) are started"
wait