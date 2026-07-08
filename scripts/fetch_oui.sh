#!/usr/bin/env bash
# Download the Wireshark manuf file to data/oui/manuf
set -euo pipefail
mkdir -p data/oui
curl -fsSL https://www.wireshark.org/download/automated/data/manuf -o data/oui/manuf
echo "OUI database downloaded: $(wc -l < data/oui/manuf) lines"
