#!/usr/bin/python3

import base64
import csv
import functools
import urllib.request

VPN_CSV_URL = 'http://www.vpngate.net/api/iphone/'
VPN_FILENAME = 'vpngate.ovpn'

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

def main():
    print('Retrieving VPN list from vpngate.net.')
    with urllib.request.urlopen(VPN_CSV_URL) as response:
        next(response) # skip first line with containing "*vpn_servers"
        csv_data = list(csv.DictReader(response.read().decode('utf-8').splitlines()))

    best = get_best(csv_data)
    print('Selected {} in {} running at {} and {} ms latency from {} choices.'.format(
        best['#HostName'], best['CountryLong'], human_bps(int(best['Speed'])), best['Ping'], len(list(csv_data))))

    print('Writing configuration to temporary file.')
    with open (VPN_FILENAME, 'wb') as ovpnFile:
        data = bytearray(base64.b64decode(best['OpenVPN_ConfigData_Base64']))
        ovpnFile.write(data)
        print('wrote {}'.format(VPN_FILENAME))

main()
