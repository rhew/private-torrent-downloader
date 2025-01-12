#!/usr/bin/python3

import base64
import csv
import functools
import re
import urllib.request

VPN_CSV_URL = 'http://www.vpngate.net/api/iphone/'
VPN_FILENAME = 'vpngate.ovpn'

BlockingCountries = ['JP', 'US']

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
    # Japan servers are blocking torrent traffic
    return row1 if int(row1['Score']) > int(row2['Score']) and row1['CountryShort'] not in BlockingCountries else row2

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

    print('Available countries: ' + str(set([row['CountryShort'] for row in csv_data])))

    best = get_best(csv_data)
    print('Selected {} in {} running at {} and {} ms latency from {} choices.'.format(
        best['#HostName'], best['CountryLong'], human_bps(int(best['Speed'])), best['Ping'], len(list(csv_data))))

    print('Writing configuration to temporary file.')
    with open (VPN_FILENAME, 'wb') as ovpnFile:
        decoded_data = base64.b64decode(best['OpenVPN_ConfigData_Base64']).decode('utf-8')
        updated_data = re.sub(
            r'^cipher\s+(\S+)',
            r'data-ciphers \1',
            decoded_data,
            flags=re.MULTILINE
        )
        data = bytearray(updated_data.encode('utf-8'))
        ovpnFile.write(data)
        print('wrote {}'.format(VPN_FILENAME))

main()
