#!/bin/bash

NUM=30
TARGET_BIT_RATE=3M
TIME=30
SLEEP=0.5
SERVER_IP=10.0.0.2

START_PORT=10001

mkdir -p iperf_client_logs

for i in $(seq 1 ${NUM}); do
    iperf3 --client ${SERVER_IP} --port $((START_PORT + i - 1)) --bandwidth ${TARGET_BIT_RATE} --format m --time $(echo "${TIME} - ${SLEEP} * ${i}" | bc) > "iperf_client_logs/client_${i}.log" 2>&1 &
    echo "Started iperf3 client on port $((START_PORT + i - 1))"
    sleep ${SLEEP}
done
echo "${NUM} iperf3 clients (Port ${START_PORT}-$((START_PORT + NUM - 1))) are started"

sleep $(echo "${TIME} - ${SLEEP} * ${NUM}" | bc)
echo "Test finished"

wait