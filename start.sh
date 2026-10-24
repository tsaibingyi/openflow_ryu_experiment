#!/bin/bash
clear

echo "Cleaning up Mininet..."
sudo mn -c > /dev/null 2>&1

echo "Cleaning up Ryu controller..."
sudo pkill -f ryu-manager > /dev/null 2>&1

echo "Cleaning up xterm processes..."
sudo pkill -f "server.sh" > /dev/null 2>&1
sudo pkill -f "client.sh" > /dev/null 2>&1
sudo pkill -f "iperf3" > /dev/null 2>&1

echo "All previous processes cleaned."


echo "Starting the Ryu controller..."
xterm -hold -T "Ryu Controller" -e "./ctrl.sh" &
sleep 1
echo "Ryu controller started."

echo "Starting the Mininet network..."
sudo python mn.py