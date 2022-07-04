#!/usr/bin/python3

import base64
import csv
import functools
import os
import subprocess
import tempfile
import urllib.request

VPN_CSV_URL = 'http://www.vpngate.net/api/iphone/'
VPN_FILE_PREFIX = 'vpngate-'
VPN_FILE_SUFFIX = '.ovpn'

def best_vpngate_score(row1, row2):
    try:
        score1 = int(row1['Score'])
    except:
        print('row 1 sucks: {}'.format(row1))
        return row2
    try:
        score2 = int(row2['Score'])
    except:
        print('row 2 sucks: {}'.format(row2))
        return row1
    return row1 if int(row1['Score']) > int(row2['Score']) else row2

def get_best(rows):
    return functools.reduce(best_vpngate_score, rows)

def human_bps(bits):
    units = ['bps', 'kbps', 'Mbps', 'Gbps', 'Tbps', 'Pbps', 'Ebps', 'Zbps', 'Ybps']
    index = 0
    while bits > 1024 and index < len(units):
        bits /= 1024
        index += 1
    return '{0:.2f} '.format(bits) + units[index]

def get_vpngate_vpns(active_only=False):
    command = 'nmcli connection show --active'.split()
    if active_only:
        command.append('--active')
    output = subprocess.check_output(command).decode('utf-8')
    lines = output.splitlines()[1:]
    vpn_ids = [line.split()[0] for line in lines]
    return [vpn_id for vpn_id in vpn_ids if vpn_id.startswith(VPN_FILE_PREFIX)]

def remove_old_vpns():
    active = get_vpngate_vpns(active_only=True)
    for vpn_id in active:
        print('Stoping active connection id {}.'.format(vpn_id))
        command = 'nmcli connection down {}'.format(vpn_id)
        subprocess.check_call(command.split())

    inactive = get_vpngate_vpns(active_only=True)
    for vpn_id in inactive:
        print('Removing VPN id {}.'.format(vpn_id))
        command = 'nmcli connection delete {}'.format(vpn_id)
        subprocess.check_call(command.split())

def main():
    remove_old_vpns()

    print('Retrieving VPN list from vpngate.net.')
    with urllib.request.urlopen(VPN_CSV_URL) as response:
        next(response) # skip first line with containing "*vpn_servers"
        csv_data = list(csv.DictReader(response.read().decode('utf-8').splitlines()))

    best = get_best(csv_data)
    print('Selected {} in {} running at {} and {} ms latency from {} choices.'.format(
        best['#HostName'], best['CountryLong'], human_bps(int(best['Speed'])), best['Ping'], len(list(csv_data))))

    print('Writing configuration to temporary file.')
    temp = tempfile.NamedTemporaryFile(prefix=VPN_FILE_PREFIX, suffix=VPN_FILE_SUFFIX, delete=False)

    try:
        data = bytearray(base64.b64decode(best['OpenVPN_ConfigData_Base64']))
        temp.write(data)
        temp.close()

        # connection_id = os.path.basename(temp.name[:-len(VPN_FILE_SUFFIX)])
        # print('Importing configruation into openVPN as {}.'.format(connection_id))
        # command = 'nmcli connection import type openvpn file {}'.format(temp.name)
        # subprocess.check_call(command.split())

        # print('Connectiing to {}'.format(best['#HostName']))
        # command = 'nmcli connection up id {}'.format(connection_id)
        # subprocess.check_call(command.split())
    finally:
        print(temp.name)
        # os.unlink(temp.name)

    print('Done.')

main()
