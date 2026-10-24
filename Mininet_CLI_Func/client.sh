#!/bin/bash

NUM=30
TARGET_BIT_RATE=25M
TIME=30
SLEEP=0.5
SERVER_IP=10.0.0.2

START_PORT=10001
END_PORT=$((START_PORT + NUM - 1))

mkdir -p iperf_client_logs

for i in $(seq ${START_PORT} ${END_PORT}); do
    iperf3 --client ${SERVER_IP} --port ${i} --bandwidth ${TARGET_BIT_RATE} --format m --time ${TIME} > "iperf_client_logs/client_${i}.log" 2>&1 &
    echo "Started iperf3 client on port ${i}"
    sleep ${SLEEP}
done

echo "${NUM} iperf3 clients (Port ${START_PORT}-${END_PORT}) are started"
wait